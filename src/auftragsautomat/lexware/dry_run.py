"""Dry-Run-Schreiber: simuliert Einkauf-Bestellung, Angebot, Verkauf-AB, Lieferschein und PDF-Ausgabe ohne Lexware.

:class:`DryRunSchreiber` erfüllt ``lexware.basis.LexwareSchreiber`` und wird verwendet, wenn
kein Lexware erreichbar ist (Linux, Tests, CLI ohne ``--scharf``, ``settings.dry_run``):

* Einkauf-Bestellung: nur Simulation. Netto = ``paket.summe_netto_ek`` (EK aus dem Stamm),
  Brutto mit ``firma.opentrans.mwst_satz``; die Positionen stehen als Zeilen
  ``"Artikel 1643 x 60 @ 4,20"`` in den Warnungen, ebenso die Belegfelder und die Nachbemerkung
  (Kundenbestellnummer, wenn ``Firma.bestellnummer_in_nachbemerkung``).
* Angebot: nur Simulation (VK Preisgruppe 1, Positionsrabatte in %, Lieferdatum).
* Angebot -> AB ("Weiterführen" / "Auftrag wandeln"): erzeugt die openTRANS-XML wie die direkte
  Verkauf-AB und protokolliert, welche Felder in der AB geleert würden (Lieferdatum, AB-Text).
* Verkauf-AB: erzeugt die openTRANS-XML tatsächlich über
  :class:`~auftragsautomat.lexware.opentrans.OpenTransSchreiber` (immer ``dry_run=True``, also in
  ``settings.xml_ausgabe_ordner``), sofern Kunde und Positionen vollständig sind; sonst
  ``Belegergebnis(erfolgreich=False)`` mit allen Gründen.
* Lieferschein: nur Simulation ("Auftrag weiterführen" gibt es nur in der Lexware-Oberfläche).
* PDF-Ausgabe: erzeugt keine PDF, liefert den geplanten Pfad
  ``settings.lexware.pdf_ausgabe_ordner / dateiname`` und schreibt dort eine Textvorschau
  ``<stamm>.txt`` (Formular, Belegart, Positionen, Nachbemerkung).

Der Schreiber simuliert IMMER, auch wenn ``dry_run=False`` übergeben wird; jedes Ergebnis trägt
``dry_run=True`` und ``belegnummer=None``.
"""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from auftragsautomat.config import Firma, Settings
from auftragsautomat.lexware.basis import (
    ANGEBOTSWEGE,
    Verkaufsweg,
    nachbemerkung_einkauf,
    pdf_zielpfad,
    weg_beschreibung,
)
from auftragsautomat.lexware.opentrans import OpenTransSchreiber
from auftragsautomat.models import Belegart, Belegergebnis, Belegpaket
from auftragsautomat.protokoll import formatiere_betrag, formatiere_datum, formatiere_menge

logger = logging.getLogger(__name__)

__all__ = ["DryRunSchreiber", "positionszeilen", "rabattzeilen"]

_SCHARF_IGNORIERT = "DryRunSchreiber: scharfer Lauf angefordert, es wird trotzdem nur simuliert (dry_run=True)"
VORSCHAU_ENDUNG = ".txt"


def _prozent(wert: Decimal) -> str:
    return formatiere_menge(Decimal(wert))


def positionszeilen(paket: Belegpaket, preis: str) -> list[str]:
    """Positionszeilen für Warnungen/Berichte: "Artikel 1643 x 60 @ 4,20" (``preis`` = "ek" | "vk").

    Verkaufspositionen mit Positionsrabatt: "Artikel 1643 x 60 @ 6,90 - 10 % = 6,21".
    """
    zeilen: list[str] = []
    for p in paket.aktive_positionen():
        artikelnr = p.artikel.artikelnummer if p.artikel else "?"
        wert = p.ek_preis if preis == "ek" else p.vk_preis
        preis_text = formatiere_betrag(wert) if wert is not None else "?"
        zeile = f"Artikel {artikelnr} x {formatiere_menge(p.menge)} @ {preis_text}"
        if preis == "vk" and p.rabatt_prozent and p.vk_preis_rabattiert is not None:
            zeile += f" - {_prozent(p.rabatt_prozent)} % = {formatiere_betrag(p.vk_preis_rabattiert)}"
        zeilen.append(zeile)
    return zeilen


def rabattzeilen(paket: Belegpaket) -> list[str]:
    """Rabatte je Position: "Pos. 1 (1643): 10 % (aktion:herbst)"; leer, wenn keine Position rabattiert ist."""
    zeilen: list[str] = []
    for p in paket.aktive_positionen():
        if not p.rabatt_prozent:
            continue
        artikelnr = p.artikel.artikelnummer if p.artikel else "?"
        zeilen.append(f"Pos. {p.roh.laufnr} ({artikelnr}): {_prozent(p.rabatt_prozent)} % ({p.rabatt_quelle or 'ohne Quelle'})")
    return zeilen


class DryRunSchreiber:
    """Simuliert alle Belege und die PDF-Ausgabe (implementiert ``lexware.basis.LexwareSchreiber``)."""

    def __init__(self, settings: Settings, firma: Firma, *, opentrans: OpenTransSchreiber | None = None) -> None:
        self.settings = settings
        self.firma = firma
        self.opentrans = opentrans if opentrans is not None else OpenTransSchreiber(settings, firma)

    # ------------------------------------------------------------------ Helfer

    def _brutto(self, netto: Decimal) -> Decimal:
        # TODO-LEXWARE: Brutto = Netto x (1 + MwSt-Satz der Firma) auf Belegebene; Lexware rundet ggf. je Position
        # bzw. nutzt den Steuersatz des Artikels - Abweichungen im Cent-Bereich am echten Beleg prüfen.
        faktor = Decimal(1) + Decimal(self.firma.opentrans.mwst_satz) / Decimal(100)
        return (netto * faktor).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    def _pruefe_gemeinsam(self, paket: Belegpaket, preis: str) -> list[str]:
        """Prüfungen, die für Einkauf und Lieferschein gelten (Firma, aktive Positionen, Artikel, Menge, Preis)."""
        probleme: list[str] = []
        if paket.firma_id and paket.firma_id != self.firma.id:
            probleme.append(f"Paket gehört zu Firma '{paket.firma_id}', Schreiber ist für '{self.firma.id}'")
        aktive = paket.aktive_positionen()
        if not aktive:
            probleme.append("Keine aktive Position (alle übersprungen oder keine vorhanden)")
        for p in aktive:
            bez = f"Position {p.roh.laufnr} ('{p.roh.bezeichnung[:40]}')"
            if p.artikel is None:
                probleme.append(f"{bez}: kein Artikel zugeordnet")
            elif p.artikel.firma_id != self.firma.id:
                probleme.append(
                    f"{bez}: Artikel {p.artikel.artikelnummer} gehört zu Firma '{p.artikel.firma_id}', nicht '{self.firma.id}'"
                )
            if p.menge is None or p.menge <= 0:
                probleme.append(f"{bez}: Menge {p.menge} ist nicht größer als 0")
            wert = p.ek_preis if preis == "ek" else p.vk_preis
            if wert is None:
                probleme.append(f"{bez}: kein {'EK' if preis == 'ek' else 'VK'}-Preis aus dem Stamm")
            elif wert <= 0:
                probleme.append(f"{bez}: {'EK' if preis == 'ek' else 'VK'}-Preis {wert} ist nicht größer als 0")
        return probleme

    def _belegfelder_zeilen(self, paket: Belegpaket) -> list[str]:
        bf = paket.belegfelder
        zeilen = [
            f"Belegfelder: Spedition={bf.spedition or '-'}; Weingut={bf.weingut or '-'}; "
            f"Shipment={bf.shipment or '-'}; Customer={bf.kunde or '-'}"
        ]
        fehlend = bf.fehlende()
        if fehlend:
            zeilen.append(f"Belegfelder leer (in Lexware Pflicht): {', '.join(fehlend)}")
        return zeilen

    @staticmethod
    def _scharf_hinweis(dry_run: bool) -> list[str]:
        if dry_run:
            return []
        logger.warning("%s", _SCHARF_IGNORIERT)
        return [_SCHARF_IGNORIERT]

    def _fehlschlag(self, belegart: Belegart, paket: Belegpaket, probleme: list[str], netto: Decimal) -> Belegergebnis:
        fehler = "; ".join(probleme)
        logger.error("Dry Run %s für %s nicht möglich: %s", belegart.value, paket.roh.quelldatei.name, fehler)
        return Belegergebnis(
            belegart=belegart,
            belegnummer=None,
            netto=netto,
            brutto=self._brutto(netto),
            erfolgreich=False,
            dry_run=True,
            methode="dry_run",
            fehler=fehler,
        )

    # ----------------------------------------------------- Einkauf-Bestellung

    def schreibe_einkauf_bestellung(self, paket: Belegpaket, *, dry_run: bool = True) -> Belegergebnis:
        """Simuliert die Einkauf-Bestellung (Lieferant = Weingut, Preise = EK, Customer = Endkunde, Nachbemerkung)."""
        logger.info("Dry Run Einkauf-Bestellung: Firma %s, Quelldatei %s", self.firma.id, paket.roh.quelldatei.name)
        netto = paket.summe_netto_ek
        probleme: list[str] = []
        if not paket.einkauf_erforderlich:
            probleme.append("Für dieses Paket ist keine Einkauf-Bestellung vorgesehen (einkauf_erforderlich=False)")
        lieferant = paket.lieferant.lieferant
        if lieferant is None:
            probleme.append("Kein Lieferant (Weingut) zugeordnet")
        elif lieferant.firma_id != self.firma.id:
            probleme.append(
                f"Lieferant {lieferant.lieferantennummer} gehört zu Firma '{lieferant.firma_id}', nicht '{self.firma.id}'"
            )
        probleme.extend(self._pruefe_gemeinsam(paket, "ek"))
        if probleme:
            return self._fehlschlag(Belegart.BESTELLUNG, paket, probleme, netto)
        assert lieferant is not None

        kunde = paket.kunde.kunde
        nachbemerkung = nachbemerkung_einkauf(paket, self.firma)
        if nachbemerkung:
            nachbemerkung_zeile = f"Nachbemerkung: {nachbemerkung} (Kundenbestellnummer)"
        elif self.firma.bestellnummer_in_nachbemerkung:
            nachbemerkung_zeile = "Nachbemerkung: leer (Dokument nennt keine Kundenbestellnummer)"
        else:
            nachbemerkung_zeile = "Nachbemerkung: keine (bestellnummer_in_nachbemerkung=False)"
        warnungen = [
            f"Dry Run: Einkauf-Bestellung an Lieferant {lieferant.lieferantennummer} {lieferant.name} "
            "nur simuliert, nichts in Lexware gespeichert",
            *self._scharf_hinweis(dry_run),
            f"Endkunde (Feld Customer): {kunde.kundennummer + ' ' + kunde.name if kunde else '-'}",
            f"Lieferdatum: {formatiere_datum(paket.roh.liefertag)}",
            *positionszeilen(paket, "ek"),
            f"Summe netto EK: {formatiere_betrag(netto)}",
            *self._belegfelder_zeilen(paket),
            nachbemerkung_zeile,
        ]
        logger.info("Dry Run Einkauf-Bestellung simuliert: %d Positionen, netto %s", len(paket.aktive_positionen()), formatiere_betrag(netto))
        return Belegergebnis(
            belegart=Belegart.BESTELLUNG,
            belegnummer=None,
            netto=netto,
            brutto=self._brutto(netto),
            erfolgreich=True,
            dry_run=True,
            methode="dry_run",
            warnungen=warnungen,
            nachbemerkung=nachbemerkung,
        )

    # ----------------------------------------------------------------- Angebot

    def schreibe_angebot(self, paket: Belegpaket, *, dry_run: bool = True) -> Belegergebnis:
        """Simuliert das Angebot im Verkauf (Kunde, VK Preisgruppe 1, Positionsrabatte, Lieferdatum)."""
        logger.info("Dry Run Angebot: Firma %s, Quelldatei %s", self.firma.id, paket.roh.quelldatei.name)
        netto = paket.summe_netto_vk
        probleme = self.opentrans.pruefe_paket(paket)
        if probleme:
            return self._fehlschlag(Belegart.ANGEBOT, paket, probleme, netto)
        kunde = paket.kunde.kunde
        assert kunde is not None
        rabatte = rabattzeilen(paket)
        warnungen = [
            f"Dry Run: Angebot für Kunde {kunde.kundennummer} {kunde.name} nur simuliert, nichts in Lexware gespeichert "
            f"(Verkaufsweg {weg_beschreibung(self.firma.verkauf_weg)})",
            *self._scharf_hinweis(dry_run),
            f"Lieferdatum: {formatiere_datum(paket.roh.liefertag)}",
            f"Bestellnummer: {paket.roh.bestellnummer or '-'}",
            *positionszeilen(paket, "vk"),
            ("Rabatte je Position: " + "; ".join(rabatte)) if rabatte else "Rabatte je Position: keine",
            f"Summe netto VK: {formatiere_betrag(netto)}",
            *self._belegfelder_zeilen(paket),
        ]
        logger.info("Dry Run Angebot simuliert: %d Positionen (%d mit Rabatt), netto %s", len(paket.aktive_positionen()), len(rabatte), formatiere_betrag(netto))
        return Belegergebnis(
            belegart=Belegart.ANGEBOT,
            belegnummer=None,
            netto=netto,
            brutto=self._brutto(netto),
            erfolgreich=True,
            dry_run=True,
            methode="dry_run",
            warnungen=warnungen,
        )

    def angebot_zu_ab(
        self,
        angebot: Belegergebnis,
        paket: Belegpaket,
        *,
        weg: Verkaufsweg = "angebot_weiterfuehren",
        dry_run: bool = True,
    ) -> Belegergebnis:
        """Simuliert "Weiterführen" bzw. "Auftrag wandeln" vom Angebot zur AB; die AB entsteht wie bei
        :meth:`schreibe_verkauf_ab` als openTRANS-XML im Ausgabeordner."""
        logger.info("Dry Run Angebot -> AB per %s: Firma %s", weg, self.firma.id)
        if weg not in ANGEBOTSWEGE:
            return self._fehlschlag(
                Belegart.AUFTRAGSBESTAETIGUNG,
                paket,
                [f"Unbekannter Verkaufsweg '{weg}' (erlaubt: {', '.join(ANGEBOTSWEGE)})"],
                paket.summe_netto_vk,
            )
        ergebnis = self.schreibe_verkauf_ab(paket, dry_run=dry_run)
        hinweise = [
            f"Dry Run: {weg_beschreibung(weg)} simuliert"
            + ("" if angebot.erfolgreich else " (Angebot war nicht erfolgreich)"),
            f"Lieferdatum in der AB geleert: {'ja' if self.firma.lieferdatum_in_ab_loeschen else 'nein'}; "
            f"AB-Text geleert: {'ja' if self.firma.ab_text_loeschen else 'nein'}",
        ]
        if self.firma.lieferdatum_in_ab_loeschen and paket.roh.liefertag is not None:
            hinweise.append(
                "Hinweis: die openTRANS-XML enthält DELIVERY_DATE; in der AB wird das Lieferdatum laut Firma geleert "
                "(Fixtermin steht in der Fixterminüberwachung)"
            )
        ergebnis.warnungen[0:0] = hinweise
        return ergebnis

    # ------------------------------------------------------------- Verkauf-AB

    def _alte_dry_run_datei_wegraeumen(self, paket: Belegpaket) -> str | None:
        """Benennt eine gleichnamige XML aus einem früheren Dry Run um, damit der Lauf wiederholbar ist.

        Betrifft ausschließlich ``settings.xml_ausgabe_ordner`` (unser Ausgabeordner), nie den
        Lexware-Importordner.
        """
        try:
            ziel = Path(self.settings.xml_ausgabe_ordner) / self.opentrans.dateiname(paket)
        except Exception as exc:  # noqa: BLE001 - Dateiname nur ermittelbar bei vollständigem Paket
            logger.debug("Dateiname für Dry-Run-XML nicht ermittelbar: %s", exc)
            return None
        if not ziel.exists():
            return None
        zeit = datetime.now().strftime("%Y%m%d-%H%M%S")
        alt = ziel.with_name(f"{ziel.stem}_alt_{zeit}{ziel.suffix}")
        nr = 2
        while alt.exists():
            alt = ziel.with_name(f"{ziel.stem}_alt_{zeit}_{nr}{ziel.suffix}")
            nr += 1
        try:
            ziel.rename(alt)
        except OSError as exc:
            logger.warning("Vorhandene Dry-Run-XML %s konnte nicht umbenannt werden: %s", ziel, exc)
            return None
        logger.info("Vorhandene Dry-Run-XML umbenannt: %s -> %s", ziel.name, alt.name)
        return f"Vorhandene Dry-Run-XML aus früherem Lauf umbenannt nach {alt.name}"

    def schreibe_verkauf_ab(self, paket: Belegpaket, *, dry_run: bool = True) -> Belegergebnis:
        """Erzeugt die openTRANS-XML im Ausgabeordner (Dry Run), wenn Kunde und Positionen vollständig sind."""
        logger.info("Dry Run Verkauf-AB: Firma %s, Quelldatei %s", self.firma.id, paket.roh.quelldatei.name)
        probleme = self.opentrans.pruefe_paket(paket)
        if probleme:
            return self._fehlschlag(Belegart.AUFTRAGSBESTAETIGUNG, paket, probleme, paket.summe_netto_vk)
        hinweis = self._alte_dry_run_datei_wegraeumen(paket)
        ergebnis = self.opentrans.schreibe_verkauf_ab(paket, dry_run=True)
        ergebnis.dry_run = True
        ergebnis.belegnummer = None
        ergebnis.warnungen.extend(self._scharf_hinweis(dry_run))
        if hinweis:
            ergebnis.warnungen.append(hinweis)
        if ergebnis.erfolgreich:
            ergebnis.warnungen.append("Dry Run: Verkauf-AB simuliert; Positionen mit VK Preisgruppe 1:")
            ergebnis.warnungen.extend(positionszeilen(paket, "vk"))
            rabatte = rabattzeilen(paket)
            if rabatte:
                ergebnis.warnungen.append("Rabatte je Position: " + "; ".join(rabatte))
            ergebnis.warnungen.extend(self._belegfelder_zeilen(paket))
        return ergebnis

    # ----------------------------------------------------------- Lieferschein

    def auftrag_weiterfuehren_lieferschein(
        self, paket: Belegpaket, ab_belegnummer: str, *, dry_run: bool = True
    ) -> Belegergebnis:
        """Simuliert "Auftrag weiterführen" (AB -> Lieferschein); nur nach Vorlage und Freigabe."""
        logger.info("Dry Run Lieferschein zu AB %s: Firma %s", ab_belegnummer or "-", self.firma.id)
        netto = paket.summe_netto_vk
        probleme: list[str] = []
        if not (ab_belegnummer or "").strip():
            probleme.append("AB-Belegnummer fehlt (Lieferschein nur aus einer vorhandenen AB)")
        if not paket.lieferschein_erstellen:
            probleme.append("Lieferschein ist für dieses Paket nicht angefordert (lieferschein_erstellen=False)")
        if not self.firma.lieferschein_erlaubt:
            probleme.append(f"Lieferschein für {self.firma.name} nicht vorgesehen (lieferschein_erlaubt=False)")
        probleme.extend(self._pruefe_gemeinsam(paket, "vk"))
        if probleme:
            return self._fehlschlag(Belegart.LIEFERSCHEIN, paket, probleme, netto)
        warnungen = [
            f"Dry Run: Lieferschein zu AB {ab_belegnummer.strip()} nur simuliert "
            "('Auftrag weiterführen' nur per Lexware-Oberfläche/UI-Automation)",
            *self._scharf_hinweis(dry_run),
            *positionszeilen(paket, "vk"),
            f"Summe netto VK: {formatiere_betrag(netto)}",
        ]
        return Belegergebnis(
            belegart=Belegart.LIEFERSCHEIN,
            belegnummer=None,
            netto=netto,
            brutto=self._brutto(netto),
            erfolgreich=True,
            dry_run=True,
            methode="dry_run",
            warnungen=warnungen,
        )

    # ------------------------------------------------------------ PDF-Ausgabe

    def pdf_ausgeben(
        self, beleg: Belegergebnis, *, formular: str | None, dateiname: str, dry_run: bool = True
    ) -> Path | None:
        """Erzeugt keine PDF: liefert den geplanten Pfad und schreibt dort eine Textvorschau ``<stamm>.txt``."""
        ziel = pdf_zielpfad(self.settings, dateiname)
        modus = self.settings.lexware.pdf_ausgabe
        vorschau = ziel.with_suffix(VORSCHAU_ENDUNG)
        zeilen = [
            "Dry-Run-Vorschau der PDF-Ausgabe (keine PDF erzeugt, nichts in Lexware gespeichert)",
            f"Geplante Datei: {ziel}",
            f"Belegart: {beleg.belegart.value}; Belegnummer: {beleg.belegnummer or '(Dry Run, keine Nummer)'}",
            f"Formular: {formular or 'Lexware-Standardformular'}; Ausgabe scharf per: {modus}"
            + (" (keine PDF im scharfen Betrieb, nur Protokoll)" if modus == "keine" else ""),
            f"Netto: {formatiere_betrag(beleg.netto) or '-'}; Brutto: {formatiere_betrag(beleg.brutto) or '-'}",
            f"Nachbemerkung: {beleg.nachbemerkung or '-'}",
            "",
            "Hinweise des Belegs:",
            *(f"  - {w}" for w in beleg.warnungen if not w.startswith("Ablauf: ")),
            "",
            f"Erzeugt am {formatiere_datum(datetime.now(), mit_zeit=True)} von DryRunSchreiber",
        ]
        try:
            vorschau.parent.mkdir(parents=True, exist_ok=True)
            vorschau.write_text("\n".join(zeilen) + "\n", encoding="utf-8")
            beleg.warnungen.append(f"Dry Run: PDF {ziel.name} (Formular {formular or 'Standard'}) nur geplant; Vorschau: {vorschau}")
            logger.info("Dry Run PDF-Vorschau geschrieben: %s", vorschau)
        except OSError as exc:
            logger.warning("Dry-Run-Vorschau %s konnte nicht geschrieben werden: %s", vorschau, exc)
            beleg.warnungen.append(f"Dry Run: PDF {ziel.name} (Formular {formular or 'Standard'}) nur geplant; Vorschau nicht schreibbar ({exc})")
        if not dry_run:
            beleg.warnungen.append(_SCHARF_IGNORIERT)
        if beleg.pdf is None:
            beleg.pdf = ziel
        return ziel
