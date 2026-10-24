"""Protokoll, Bericht und Archiv des Auftrags-Automaten.

* :func:`schreibe_protokoll` hängt je Buchung eine Zeile an ``<log_ordner>/protokoll.csv`` an
  (UTF-8 mit BOM, Trennzeichen ``;`` – Excel öffnet die Datei per Doppelklick mit korrekten
  Umlauten und Spalten) und schreibt zusätzlich einen lesbaren Bericht
  ``<log_ordner>/berichte/<quelldatei-stamm>_<JJJJMMTT-HHMMSS>.txt``.
* :func:`zusammenfassung` erzeugt den mehrzeiligen Berichtstext (CLI, Freigabe-UI, Bericht):
  Kopf, Positionstabelle mit Preisabweichungen, Summen, Belegnummern, alle Prüfhinweise nach
  Schweregrad, Hinweise der Buchung und der Dry-Run-Hinweis.
* :func:`archiviere` verschiebt die Quelldatei nach ``<archiv_ordner>/<firma>/<JJJJ>/``;
  vorhandene Dateien werden nie überschrieben (Suffix ``_2``, ``_3`` …).

Zahlen stehen im deutschen Format (``1.234,50``), Datumsangaben als ``TT.MM.JJJJ``.
Preisabweichungen (VK laut Stamm ≠ Preis laut Dokument) werden immer ausgewiesen –
Geschäftsregel 6: nie stillschweigend übernehmen.
"""

from __future__ import annotations

import csv
import logging
import re
import shutil
from collections.abc import Callable
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from auftragsautomat.config import Firma, Settings
from auftragsautomat.models import (
    Belegergebnis,
    Belegpaket,
    BuchungsErgebnis,
    MatchStatus,
    PositionsZuordnung,
    Pruefhinweis,
    Schweregrad,
)

logger = logging.getLogger(__name__)

__all__ = [
    "BERICHTE_UNTERORDNER",
    "PROTOKOLL_DATEINAME",
    "PROTOKOLL_SPALTEN",
    "archiviere",
    "formatiere_betrag",
    "formatiere_datum",
    "formatiere_menge",
    "protokoll_zeile",
    "schreibe_protokoll",
    "zusammenfassung",
]

PROTOKOLL_DATEINAME = "protokoll.csv"
BERICHTE_UNTERORDNER = "berichte"
PROTOKOLL_TRENNZEICHEN = ";"
WARNUNGEN_TRENNZEICHEN = " | "
#: Spalten der protokoll.csv (Reihenfolge ist Teil des Vertrags; Excel-Auswertungen hängen daran).
PROTOKOLL_SPALTEN: tuple[str, ...] = (
    "zeitpunkt",
    "firma",
    "dry_run",
    "quelldatei",
    "kunde_nr",
    "kunde_name",
    "lieferant_nr",
    "lieferant_name",
    "bestellnummer",
    "beleg_einkauf",
    "beleg_verkauf",
    "beleg_lieferschein",
    "netto_ek",
    "brutto_ek",
    "netto_vk",
    "brutto_vk",
    "positionen",
    "uebersprungen",
    "warnungen",
    "erfolgreich",
    # Erweiterung Lexware-Prozess (Angebot -> AB, Rabatte, PDF-Dateien, Revised Order); angehängt,
    # damit die bisherigen Spalten unverändert bleiben.
    "Angebot-Nr",
    "Rabatte",
    "Dateien",
    "Revision",
)

_MAX_BEZEICHNUNG = 44
_MAX_SUFFIX_VERSUCHE = 999
_UNSICHERE_ZEICHEN = re.compile(r"[^A-Za-z0-9._-]+")
_GRAD_REIHENFOLGE = (Schweregrad.FEHLER, Schweregrad.WARNUNG, Schweregrad.INFO)
_GRAD_TITEL = {Schweregrad.FEHLER: "FEHLER", Schweregrad.WARNUNG: "WARNUNG", Schweregrad.INFO: "INFO"}


# ---------------------------------------------------------------------------
# Formatierung (deutsches Zahlenformat)
# ---------------------------------------------------------------------------


def _tausender(ganzzahl: int) -> str:
    return f"{ganzzahl:,}".replace(",", ".")


def formatiere_betrag(wert: Decimal | None) -> str:
    """Decimal -> "1.234,50" (zwei Nachkommastellen, kaufmännisch gerundet); None -> ""."""
    if wert is None:
        return ""
    betrag = Decimal(wert).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    vorzeichen = "-" if betrag < 0 else ""
    betrag = abs(betrag)
    ganz = int(betrag)
    nachkomma = int((betrag - ganz) * 100)
    return f"{vorzeichen}{_tausender(ganz)},{nachkomma:02d}"


def formatiere_menge(wert: Decimal | None) -> str:
    """Decimal -> "60" bzw. "0,5" (keine unnötigen Nachkommastellen, Tausenderpunkt); None -> ""."""
    if wert is None:
        return ""
    menge = Decimal(wert)
    if menge == menge.to_integral_value():
        ganz = int(menge)
        return ("-" if ganz < 0 else "") + _tausender(abs(ganz))
    text = f"{menge.normalize():f}"
    vorzeichen = "-" if text.startswith("-") else ""
    ganz_text, _, bruch = text.lstrip("-").partition(".")
    return f"{vorzeichen}{_tausender(int(ganz_text))},{bruch}"


def formatiere_datum(wert: date | datetime | None, *, mit_zeit: bool = False) -> str:
    """date/datetime -> "TT.MM.JJJJ" (bzw. "TT.MM.JJJJ HH:MM:SS"); None -> "-"."""
    if wert is None:
        return "-"
    if isinstance(wert, datetime) and mit_zeit:
        return wert.strftime("%d.%m.%Y %H:%M:%S")
    return wert.strftime("%d.%m.%Y")


def _ja_nein(wert: bool) -> str:
    return "ja" if wert else "nein"


def _kuerze(text: str | None, maximal: int) -> str:
    text = (text or "").replace("\n", " ").strip()
    if len(text) <= maximal:
        return text
    return text[: maximal - 1] + "…"


def _einzeilig(text: str) -> str:
    return re.sub(r"\s*[\r\n]+\s*", " / ", text.strip())


def _sicherer_dateiteil(text: str, maximal: int = 60) -> str:
    bereinigt = _UNSICHERE_ZEICHEN.sub("_", text).strip("._")
    return bereinigt[:maximal] or "unbenannt"


def _freier_pfad(ziel: Path) -> Path:
    """Liefert ``ziel`` oder, falls belegt, ``<stem>_2<suffix>``, ``_3`` … (nie überschreiben)."""
    if not ziel.exists():
        return ziel
    for nr in range(2, _MAX_SUFFIX_VERSUCHE + 1):
        kandidat = ziel.with_name(f"{ziel.stem}_{nr}{ziel.suffix}")
        if not kandidat.exists():
            return kandidat
    zeit = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    return ziel.with_name(f"{ziel.stem}_{zeit}{ziel.suffix}")


# ---------------------------------------------------------------------------
# Bausteine für Protokoll und Bericht
# ---------------------------------------------------------------------------


def _teile(ergebnis: BuchungsErgebnis) -> list[tuple[str, Belegergebnis | None]]:
    """Alle Teilbelege mit Namen; weitere Einkauf-Bestellungen (mehrere Weingüter) nach der ersten,
    das Angebot (Weg Angebot -> AB) vor der Verkauf-AB, sofern vorhanden."""
    einkauf_name = "Einkauf-Bestellung"
    if ergebnis.einkauf is not None and ergebnis.einkauf_weitere and ergebnis.einkauf.partner:
        einkauf_name = f"Einkauf-Bestellung ({ergebnis.einkauf.partner})"
    teile: list[tuple[str, Belegergebnis | None]] = [(einkauf_name, ergebnis.einkauf)]
    teile.extend(
        (f"Einkauf-Bestellung ({teil.partner or f'weiteres Weingut {nr}'})", teil)
        for nr, teil in enumerate(ergebnis.einkauf_weitere, start=2)
    )
    if ergebnis.angebot is not None:
        teile.append(("Angebot", ergebnis.angebot))
    teile.append(("Verkauf-AB", ergebnis.verkauf))
    teile.append(("Lieferschein", ergebnis.lieferschein))
    return teile


def _rabatte_text(paket: Belegpaket) -> str:
    """Positionsrabatte: "Pos. 1 (1643): 10 % (aktion:herbst); Pos. 2 (957): 5 %" oder leer."""
    teile: list[str] = []
    for p in paket.aktive_positionen():
        if not p.rabatt_prozent:
            continue
        artikelnr = p.artikel.artikelnummer if p.artikel else "?"
        prozent = formatiere_menge(Decimal(p.rabatt_prozent))
        quelle = f" ({p.rabatt_quelle})" if p.rabatt_quelle else ""
        teile.append(f"Pos. {p.roh.laufnr} ({artikelnr}): {prozent} %{quelle}")
    return "; ".join(teile)


def _dateien_text(ergebnis: BuchungsErgebnis) -> str:
    """Erzeugte/geplante PDFs: "Cde=<Name>; Kopie=<Name>" (nur Dateinamen, keine Pfade)."""
    return "; ".join(f"{k}={Path(v).name}" for k, v in ergebnis.pdfs.items())


def _revision_text(paket: Belegpaket, ergebnis: BuchungsErgebnis) -> str:
    ist_revision = ergebnis.ist_revision or paket.revision_wirksam
    if not ist_revision:
        return "nein"
    ersetzt = (paket.revision_von or paket.roh.revision_von or "").strip()
    return f"ja (ersetzt {ersetzt})" if ersetzt else "ja"


def _nachbemerkung_text(ergebnis: BuchungsErgebnis) -> str:
    texte = [t.nachbemerkung for t in ergebnis.alle_einkaeufe() if t.nachbemerkung]
    return " / ".join(dict.fromkeys(texte))


def _formulare_text(firma: Firma) -> str:
    f = firma.formulare
    teile = [
        f"Einkauf EN={f.einkauf_en or '-'}" + (f" (x{f.speditionsauftrag_kopien})" if f.einkauf_en else ""),
        f"Angebot EN={f.angebot_en or '-'}" + (f" (x{f.speditionsauftrag_kopien})" if f.angebot_en else ""),
        f"AB={f.verkauf_ab or 'Standard'}",
        f"Lieferschein={f.lieferschein or 'Standard'}" + ("" if firma.lieferschein_erlaubt else " (nicht vorgesehen)"),
    ]
    return "; ".join(teile)


def _verkaufsweg_text(ergebnis: BuchungsErgebnis, firma: Firma | None) -> str:
    if firma is not None:
        from auftragsautomat.lexware.basis import weg_beschreibung

        text = f"{firma.verkauf_weg} ({weg_beschreibung(firma.verkauf_weg)})"
        if firma.verkauf_weg != "direkt_ab":
            text += (
                f"; in der AB Lieferdatum {'leeren' if firma.lieferdatum_in_ab_loeschen else 'behalten'}, "
                f"AB-Text {'leeren' if firma.ab_text_loeschen else 'behalten'}"
            )
        return text
    if ergebnis.angebot is None:
        return "Auftragsbestätigung direkt (ohne Angebot)"
    if ergebnis.angebot.methode == "keine":
        return "Angebot nicht möglich (openTRANS) – Auftragsbestätigung direkt"
    return "Angebot -> Auftragsbestätigung"


def _summe(werte: list[Decimal | None]) -> Decimal | None:
    vorhanden = [w for w in werte if w is not None]
    if not vorhanden:
        return None
    return sum(vorhanden, Decimal(0))


def _netto(teil: Belegergebnis | None, fallback: Decimal | None) -> Decimal | None:
    if teil is None:
        return None
    return teil.netto if teil.netto is not None else fallback


def _belegnummer_text(teil: Belegergebnis | None) -> str:
    if teil is None:
        return ""
    if teil.belegnummer:
        return teil.belegnummer
    if teil.erfolgreich and teil.methode == "keine":
        return "(entfällt)"  # z. B. Angebot per openTRANS nicht möglich, AB direkt
    if teil.erfolgreich:
        return "(simuliert)" if teil.dry_run else "(noch nicht vergeben)"
    return "FEHLER"


def _hinweis_text(h: Pruefhinweis) -> str:
    pos = f"Pos. {h.position_laufnr}: " if h.position_laufnr is not None else ""
    return f"[{h.code.value}] {pos}{_einzeilig(h.text)}"


def _ist_ablaufzeile(text: str) -> bool:
    return text.startswith("Ablauf: ")


def _buchungs_hinweise(ergebnis: BuchungsErgebnis, *, mit_ablauf: bool) -> list[str]:
    """Alle Hinweise der Buchung: Belegebene, dann je Teilbeleg Fehler und Warnungen."""
    zeilen = [_einzeilig(w) for w in ergebnis.warnungen if w.strip()]
    for name, teil in _teile(ergebnis):
        if teil is None:
            continue
        if teil.fehler:
            zeilen.append(f"{name} FEHLER: {_einzeilig(teil.fehler)}")
        for w in teil.warnungen:
            if not w.strip() or (not mit_ablauf and _ist_ablaufzeile(w)):
                continue
            zeilen.append(f"{name}: {_einzeilig(w)}")
    return zeilen


def _pruefhinweise_fuer_protokoll(paket: Belegpaket) -> list[str]:
    return [
        f"Prüfung {_hinweis_text(h)}"
        for h in paket.alle_hinweise
        if h.schweregrad in (Schweregrad.FEHLER, Schweregrad.WARNUNG)
    ]


def protokoll_zeile(paket: Belegpaket, ergebnis: BuchungsErgebnis) -> dict[str, str]:
    """Eine Zeile der protokoll.csv als Spaltenname -> Text (alle Werte als Zeichenketten)."""
    kunde = paket.kunde.kunde
    lieferant = paket.lieferant.lieferant
    aktive = paket.aktive_positionen()
    warnungen = _buchungs_hinweise(ergebnis, mit_ablauf=False) + _pruefhinweise_fuer_protokoll(paket)
    einkaeufe = ergebnis.alle_einkaeufe()
    if len(einkaeufe) > 1:
        netto_ek = _summe([t.netto for t in einkaeufe])
        brutto_ek = _summe([t.brutto for t in einkaeufe])
        beleg_einkauf = " + ".join(_belegnummer_text(t) for t in einkaeufe)
    else:
        netto_ek = _netto(ergebnis.einkauf, paket.summe_netto_ek)
        brutto_ek = ergebnis.einkauf.brutto if ergebnis.einkauf else None
        beleg_einkauf = _belegnummer_text(ergebnis.einkauf)
    netto_vk = _netto(ergebnis.verkauf, paket.summe_netto_vk)
    return {
        "zeitpunkt": formatiere_datum(ergebnis.zeitpunkt, mit_zeit=True),
        "firma": ergebnis.firma_id,
        "dry_run": _ja_nein(ergebnis.dry_run),
        "quelldatei": str(ergebnis.quelldatei),
        "kunde_nr": kunde.kundennummer if kunde else "",
        "kunde_name": kunde.name if kunde else (paket.roh.kunde_text or ""),
        "lieferant_nr": lieferant.lieferantennummer if lieferant else "",
        "lieferant_name": lieferant.name if lieferant else (paket.roh.lieferant_text or ""),
        "bestellnummer": paket.roh.bestellnummer or paket.roh.belegnummer or "",
        "beleg_einkauf": beleg_einkauf,
        "beleg_verkauf": _belegnummer_text(ergebnis.verkauf),
        "beleg_lieferschein": _belegnummer_text(ergebnis.lieferschein),
        "netto_ek": formatiere_betrag(netto_ek),
        "brutto_ek": formatiere_betrag(brutto_ek),
        "netto_vk": formatiere_betrag(netto_vk),
        "brutto_vk": formatiere_betrag(ergebnis.verkauf.brutto if ergebnis.verkauf else None),
        "positionen": str(len(aktive)),
        "uebersprungen": str(len(paket.positionen) - len(aktive)),
        "warnungen": WARNUNGEN_TRENNZEICHEN.join(w.replace("|", "/") for w in warnungen),
        "erfolgreich": _ja_nein(ergebnis.erfolgreich),
        "Angebot-Nr": _belegnummer_text(ergebnis.angebot),
        "Rabatte": _rabatte_text(paket),
        "Dateien": _dateien_text(ergebnis),
        "Revision": _revision_text(paket, ergebnis),
    }


# ---------------------------------------------------------------------------
# Zusammenfassung (Bericht)
# ---------------------------------------------------------------------------


def _tabelle(kopf: list[str], zeilen: list[list[str]], rechts: set[int]) -> list[str]:
    """Einfache Texttabelle mit festen Spaltenbreiten; Spalten in ``rechts`` rechtsbündig."""
    breiten = [len(k) for k in kopf]
    for zeile in zeilen:
        for i, zelle in enumerate(zeile):
            breiten[i] = max(breiten[i], len(zelle))

    def _zeile(werte: list[str]) -> str:
        teile = []
        for i, wert in enumerate(werte):
            teile.append(wert.rjust(breiten[i]) if i in rechts else wert.ljust(breiten[i]))
        return "  " + " | ".join(teile).rstrip()

    ausgabe = [_zeile(kopf), "  " + "-+-".join("-" * b for b in breiten)]
    ausgabe.extend(_zeile(z) for z in zeilen)
    return ausgabe


def _positionszeile(p: PositionsZuordnung) -> list[str]:
    if p.uebersprungen:
        artikelnr = "-"
        bezeichnung = _kuerze(p.roh.bezeichnung, _MAX_BEZEICHNUNG)
        menge = formatiere_menge(p.roh.menge if p.menge == 0 else p.menge)
        markierung = "übersprungen"
    else:
        artikelnr = p.artikel.artikelnummer if p.artikel else "?"
        bezeichnung = _kuerze(p.artikel.bezeichnung if p.artikel else p.roh.bezeichnung, _MAX_BEZEICHNUNG)
        menge = formatiere_menge(p.menge)
        markierung = ""
        if p.artikel is None or p.status in (MatchStatus.UNSICHER, MatchStatus.KEIN_TREFFER):
            markierung = "? nicht zugeordnet"
        elif p.preis_abweichung is not None and p.preis_abweichung != 0:
            vorzeichen = "+" if p.preis_abweichung > 0 else ""
            markierung = f"!! VK {vorzeichen}{formatiere_betrag(p.preis_abweichung)} zum Dokument"
        elif p.preis_dokument is None:
            markierung = "(kein Preis im Dokument)"
    return [
        str(p.roh.laufnr),
        artikelnr,
        bezeichnung,
        menge,
        formatiere_betrag(p.ek_preis) if not p.uebersprungen else "",
        formatiere_betrag(p.vk_preis) if not p.uebersprungen else "",
        formatiere_betrag(p.preis_dokument),
        markierung,
    ]


def _teil_zeilen(name: str, teil: Belegergebnis | None, paket: Belegpaket) -> list[str]:
    if teil is None:
        if name.startswith("Einkauf-Bestellung") and not paket.einkauf_erforderlich:
            grund = "nicht erforderlich (Firma ohne Einkauf-Bestellung)"
        elif name == "Lieferschein" and not paket.lieferschein_erstellen:
            grund = "nicht angefordert"
        else:
            grund = "nicht angelegt"
        return [f"  {name + ':':<20} {grund}"]
    status = "OK" if teil.erfolgreich else "FEHLGESCHLAGEN"
    if teil.erfolgreich and teil.dry_run:
        status = "OK (Dry Run, nicht gespeichert)"
    zeilen = [
        f"  {name + ':':<20} {status}; Belegnr. {teil.belegnummer or '-'}; "
        f"netto {formatiere_betrag(teil.netto) or '-'}; brutto {formatiere_betrag(teil.brutto) or '-'}; "
        f"Methode {teil.methode}"
    ]
    if teil.artefakt is not None:
        zeilen.append(f"  {'':<20} Datei: {teil.artefakt}")
    if teil.fehler:
        zeilen.append(f"  {'':<20} Fehler: {_einzeilig(teil.fehler)}")
    return zeilen


def zusammenfassung(paket: Belegpaket, ergebnis: BuchungsErgebnis, *, firma: Firma | None = None) -> str:
    """Mehrzeiliger Bericht einer Buchung (für CLI, Freigabe-UI und die Bericht-Datei).

    Mit ``firma`` zeigt der Kopf zusätzlich Verkaufsweg und Formulare laut Konfiguration; ohne ``firma``
    wird der Verkaufsweg aus dem Ergebnis abgeleitet.
    """
    roh = paket.roh
    kunde = paket.kunde.kunde
    lieferant = paket.lieferant.lieferant
    aktive = paket.aktive_positionen()
    uebersprungen = len(paket.positionen) - len(aktive)
    modus = "DRY RUN (nichts in Lexware gespeichert)" if ergebnis.dry_run else "SCHARF"
    titel = f"Buchungsbericht Auftrags-Automat – {formatiere_datum(ergebnis.zeitpunkt, mit_zeit=True)}"

    zeilen: list[str] = [titel, "=" * len(titel), ""]
    kopf = [
        ("Modus", modus),
        ("Firma", ergebnis.firma_id),
        ("Quelldatei", str(ergebnis.quelldatei)),
        ("Eingangstyp", f"{roh.eingangstyp.value} ({roh.extraktionsmethode.value})"),
        (
            "Kunde",
            f"{kunde.kundennummer} {kunde.name}" + (f" ({kunde.name2})" if kunde.name2 else "")
            if kunde
            else f"- (Dokument: '{roh.kunde_text or '-'}')",
        ),
        (
            "Lieferant",
            f"{lieferant.lieferantennummer} {lieferant.name}" + (f" ({lieferant.name2})" if lieferant.name2 else "")
            if lieferant
            else f"- (Dokument: '{roh.lieferant_text or '-'}')",
        ),
        ("Belegnr. Dokument", roh.belegnummer or "-"),
        ("Bestellnummer", roh.bestellnummer or "-"),
        ("Bestelldatum", formatiere_datum(roh.datum)),
        ("Liefertag", formatiere_datum(roh.liefertag)),
    ]
    if roh.lieferadresse:
        kopf.append(("Lieferadresse", _einzeilig(roh.lieferadresse)))
    bf = paket.belegfelder
    kopf.append(
        (
            "Belegfelder",
            f"Spedition={bf.spedition or '-'}; Weingut={bf.weingut or '-'}; "
            f"Shipment={bf.shipment or '-'}; Kunde={bf.kunde or '-'}",
        )
    )
    kopf.append(("Verkaufsweg", _verkaufsweg_text(ergebnis, firma)))
    if firma is not None:
        kopf.append(("Formulare", _formulare_text(firma)))
    nachbemerkung = _nachbemerkung_text(ergebnis)
    kopf.append(("Nachbemerkung", nachbemerkung or "-"))
    revision = _revision_text(paket, ergebnis)
    if revision != "nein":
        kopf.append(("Revision", f"{revision} – Bestellung und AB werden neu angelegt (Kürzel Neu / Neu Kopie)"))
    kopf.append(("Freigabe", f"{_ja_nein(paket.freigegeben)}" + (f" ({paket.freigegeben_von})" if paket.freigegeben_von else "")))
    zeilen.extend(f"{name + ':':<19}{wert}" for name, wert in kopf)

    zeilen += ["", "Positionen", "----------"]
    if paket.positionen:
        zeilen.extend(
            _tabelle(
                ["Pos", "Artikelnr", "Bezeichnung", "Menge", "EK", "VK", "Preis Dok.", "Abweichung"],
                [_positionszeile(p) for p in paket.positionen],
                rechts={0, 3, 4, 5, 6},
            )
        )
    else:
        zeilen.append("  (keine Positionen)")
    zeilen.append(
        f"  Gesamtmenge: {formatiere_menge(paket.gesamtmenge)} in {len(aktive)} aktiven Positionen"
        + (f", {uebersprungen} übersprungen" if uebersprungen else "")
    )
    for vermerk in roh.summenvermerke:
        zeilen.append(f"  Summenvermerk im Dokument: '{vermerk.rohtext}' ({formatiere_menge(vermerk.wert)} {vermerk.einheit or ''})".rstrip())
    abweichungen = [p for p in aktive if p.preis_abweichung is not None and p.preis_abweichung != 0]
    if abweichungen:
        zeilen.append(
            f"  !! {len(abweichungen)} Position(en) mit Preisabweichung zum Dokument – gebucht wird IMMER der "
            "Lexware-VK (Preisgruppe 1), nie der Dokumentpreis"
        )
    rabatte = _rabatte_text(paket)
    zeilen.append(
        f"  Rabatte je Position (nur im Verkauf, VK bleibt Preisgruppe 1): {rabatte}" if rabatte else "  Rabatte je Position: keine"
    )

    zeilen += ["", "Summen", "------"]
    brutto_ek = _summe([t.brutto for t in ergebnis.alle_einkaeufe()])
    zeilen.append(
        f"  Netto EK (Stamm):          {formatiere_betrag(paket.summe_netto_ek):>12}"
        + (f"   Brutto EK: {formatiere_betrag(brutto_ek)}" if brutto_ek is not None else "")
    )
    if ergebnis.einkauf_weitere:
        zeilen.append(
            f"  Einkauf aufgeteilt auf {len(ergebnis.alle_einkaeufe())} Weingüter: "
            + "; ".join(
                f"{t.partner or '?'} netto {formatiere_betrag(t.netto) or '-'}" for t in ergebnis.alle_einkaeufe()
            )
        )
    zeilen.append(
        f"  Netto VK (Preisgruppe 1):  {formatiere_betrag(paket.summe_netto_vk):>12}"
        + (f"   Brutto VK: {formatiere_betrag(ergebnis.verkauf.brutto)}" if ergebnis.verkauf and ergebnis.verkauf.brutto is not None else "")
    )

    zeilen += ["", "Belege", "------"]
    for name, teil in _teile(ergebnis):
        zeilen.extend(_teil_zeilen(name, teil, paket))

    zeilen += ["", "Dateien (PDF laut Titulierung)", "------------------------------"]
    if ergebnis.pdfs:
        geplant = " (Dry Run: geplant, nicht erzeugt)" if ergebnis.dry_run else ""
        zeilen.extend(f"  {kuerzel + ':':<20} {pfad}{geplant}" for kuerzel, pfad in ergebnis.pdfs.items())
    else:
        zeilen.append("  keine")

    zeilen += ["", "Prüfhinweise", "------------"]
    hinweise = paket.alle_hinweise
    if not hinweise:
        zeilen.append("  keine")
    for grad in _GRAD_REIHENFOLGE:
        gruppe = [h for h in hinweise if h.schweregrad == grad]
        if not gruppe:
            continue
        zeilen.append(f"  {_GRAD_TITEL[grad]} ({len(gruppe)})")
        zeilen.extend(f"    {_hinweis_text(h)}" for h in gruppe)

    buchungs_hinweise = _buchungs_hinweise(ergebnis, mit_ablauf=True)
    zeilen += ["", "Hinweise der Buchung", "--------------------"]
    if buchungs_hinweise:
        zeilen.extend(f"  - {h}" for h in buchungs_hinweise)
    else:
        zeilen.append("  keine")

    zeilen.append("")
    zeilen.append(f"Ergebnis: {'ERFOLGREICH' if ergebnis.erfolgreich else 'FEHLGESCHLAGEN / NICHT GEBUCHT'}")
    if ergebnis.dry_run:
        zeilen.append(
            "DRY RUN: Es wurde nichts in Lexware gespeichert. Belege sind nur simuliert bzw. als "
            "openTRANS-XML im Ausgabeordner abgelegt; die Quelldatei bleibt im Eingang."
        )
    else:
        zeilen.append("SCHARF: Belege wurden in Lexware angelegt bzw. zum Import bereitgestellt (siehe Hinweise).")
    return "\n".join(zeilen) + "\n"


# ---------------------------------------------------------------------------
# Protokoll schreiben
# ---------------------------------------------------------------------------


def _csv_anhaengen(pfad: Path, zeile: dict[str, str]) -> None:
    """Hängt eine Zeile an die CSV an; Kopfzeile und BOM nur bei neuer/leerer Datei."""
    neu = not pfad.exists() or pfad.stat().st_size == 0
    if not neu:
        try:
            with pfad.open("r", encoding="utf-8-sig", newline="") as f:
                erste = f.readline().rstrip("\r\n")
            erwartet = PROTOKOLL_TRENNZEICHEN.join(PROTOKOLL_SPALTEN)
            if erste != erwartet:
                logger.warning(
                    "Kopfzeile von %s weicht vom erwarteten Format ab; Zeile wird trotzdem angehängt", pfad
                )
        except OSError as exc:
            logger.warning("Kopfzeile von %s nicht lesbar (%s); Zeile wird trotzdem angehängt", pfad, exc)
    with pfad.open("a", encoding="utf-8-sig" if neu else "utf-8", newline="") as f:
        schreiber = csv.DictWriter(
            f,
            fieldnames=list(PROTOKOLL_SPALTEN),
            delimiter=PROTOKOLL_TRENNZEICHEN,
            quoting=csv.QUOTE_MINIMAL,
            lineterminator="\r\n",
            extrasaction="ignore",
        )
        if neu:
            schreiber.writeheader()
        schreiber.writerow(zeile)


def schreibe_protokoll(
    paket: Belegpaket, ergebnis: BuchungsErgebnis, log_ordner: Path, *, firma: Firma | None = None
) -> Path:
    """Protokollzeile anhängen und Bericht schreiben; liefert den Pfad der protokoll.csv.

    Der Bericht landet unter ``<log_ordner>/berichte/<quelldatei-stamm>_<JJJJMMTT-HHMMSS>.txt``
    (bei Namenskollision mit Suffix). Schlägt nur der Bericht fehl, wird das geloggt; die
    CSV-Zeile ist dann trotzdem geschrieben. ``firma`` ergänzt den Bericht um Verkaufsweg und Formulare.
    """
    log_ordner = Path(log_ordner)
    log_ordner.mkdir(parents=True, exist_ok=True)
    pfad = log_ordner / PROTOKOLL_DATEINAME
    zeile = protokoll_zeile(paket, ergebnis)
    _csv_anhaengen(pfad, zeile)
    logger.info(
        "Protokoll: %s (%s, %s, Kunde %s, erfolgreich=%s)",
        pfad,
        ergebnis.firma_id,
        "Dry Run" if ergebnis.dry_run else "scharf",
        zeile["kunde_nr"] or "-",
        zeile["erfolgreich"],
    )

    berichte = log_ordner / BERICHTE_UNTERORDNER
    try:
        berichte.mkdir(parents=True, exist_ok=True)
        stamm = _sicherer_dateiteil(Path(ergebnis.quelldatei).stem)
        bericht = _freier_pfad(berichte / f"{stamm}_{ergebnis.zeitpunkt:%Y%m%d-%H%M%S}.txt")
        bericht.write_text(zusammenfassung(paket, ergebnis, firma=firma), encoding="utf-8")
        logger.info("Bericht geschrieben: %s", bericht)
    except OSError as exc:
        logger.error("Bericht konnte nicht geschrieben werden (%s): %s", berichte, exc)
    return pfad


# ---------------------------------------------------------------------------
# Archiv
# ---------------------------------------------------------------------------


def archiviere(
    datei: Path,
    settings: Settings,
    firma_id: str,
    *,
    jahr: int | None = None,
    jetzt: Callable[[], datetime] | None = None,
) -> Path:
    """Verschiebt ``datei`` nach ``<archiv_ordner>/<firma_id>/<JJJJ>/<name>`` und liefert den Zielpfad.

    Vorhandene Dateien werden nie überschrieben (Suffix ``_2``, ``_3`` …). ``jahr`` ist
    standardmäßig das Jahr von ``jetzt()`` (Standard ``datetime.now``). Fehlt die Datei, wird
    ``FileNotFoundError`` ausgelöst.
    """
    datei = Path(datei)
    if not datei.is_file():
        raise FileNotFoundError(f"Quelldatei {datei} existiert nicht oder ist keine Datei")
    jahr = jahr or (jetzt or datetime.now)().year
    ziel_ordner = Path(settings.archiv_ordner) / _sicherer_dateiteil(firma_id or "unbekannt") / f"{jahr:04d}"
    ziel_ordner.mkdir(parents=True, exist_ok=True)
    ziel = ziel_ordner / datei.name
    if ziel.exists() and ziel.resolve() == datei.resolve():
        logger.info("Quelldatei %s liegt bereits im Archiv", datei)
        return ziel
    ziel = _freier_pfad(ziel)
    shutil.move(str(datei), str(ziel))
    logger.info("Quelldatei archiviert: %s -> %s", datei, ziel)
    return ziel
