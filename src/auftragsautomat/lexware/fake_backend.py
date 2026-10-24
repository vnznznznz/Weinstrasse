"""Fake-Backend für die Lexware-UI-Automation (Tests und Dry Run ohne Windows).

Simuliert Hauptfenster, Firmenwechsel, den dreiseitigen Auftragsassistenten (Kunde/Lieferant ->
Positionen -> Summen), Felder mit AutoSuggest-Verhalten (Werte nur bei Einzeltastendruck), die
Summenberechnung aus getippten Mengen, Stammpreisen und Positionsrabatten, verzögertes "Weiter",
Dialoge nach dem Speichern und zufällig auftauchende Dialoge, Auftragslisten, "Auftrag weiterführen"
(Angebot -> AB -> Lieferschein), "Auftrag wandeln", Standardtexte (AB-Text), die Nachbemerkung auf der
Seite Summen sowie die PDF-Ausgabe (Druckdialog mit Formular/Drucker, Dateinamen-Dialog des
PDF-Druckers, PDF-Export). Alle Aktionen landen in ``aktionen`` (z. B. ``"klicke:WEITER"``,
``"tippe:MENGE:1"``), damit Tests die Reihenfolge prüfen können; erzeugte PDFs stehen in ``gedruckt``
bzw. ``exportiert``, ein Druck auf einen Nicht-PDF-Drucker in ``papier`` (darf nie vorkommen).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

from auftragsautomat.lexware.ui_automation import (
    ControlNichtGefunden,
    Fenster,
    FensterNichtGefunden,
    LexwareControls,
    formatiere_betrag,
    formatiere_menge,
    parse_betrag,
)

logger = logging.getLogger(__name__)

__all__ = ["FakeArtikel", "FakeBeleg", "FakeDialog", "FakeLexwareBackend"]

_AUFTRAGSARTEN = {
    "einkauf": ("Bestellanfrage", "Bestellung", "Wareneingang", "Eingangsrechnung", "Gutschrift"),
    "verkauf": ("Angebot", "Auftragsbestätigung", "Lieferschein", "Rechnung", "Gutschrift"),
}
_NAECHSTE_STUFE = {"Angebot": "Auftragsbestätigung", "Auftragsbestätigung": "Lieferschein", "Lieferschein": "Rechnung"}
_PRAEFIX = {"Bestellung": "BE", "Bestellanfrage": "BA", "Auftragsbestätigung": "AB", "Lieferschein": "LS", "Rechnung": "RE", "Angebot": "AN"}
_KOPF_FELDER = {
    "KUNDENNUMMER", "LIEFERANTENNUMMER", "BELEGDATUM", "LIEFERDATUM", "BESTELLNUMMER", "AUFTRAGSBESCHREIBUNG",
    "LIEFERADRESSE", "FREIFELD_SPEDITION", "FREIFELD_WEINGUT", "FREIFELD_SHIPMENT", "FREIFELD_KUNDE", "AB_TEXT",
}
_POSITIONS_FELDER = {"ARTIKELNUMMER", "ARTIKEL_BEZEICHNUNG", "MENGE", "PREIS", "RABATT"}
_SUMMEN_FELDER = {"SUMME_NETTO", "SUMME_BRUTTO", "SUMME_STEUER", "LIEFERART", "ZAHLUNGSBEDINGUNG", "NACHBEMERKUNG"}
_DIALOG_WIRKUNGEN_EIGEN = {"drucken", "dateidialog", "export", "wandeln"}


@dataclass
class FakeArtikel:
    artikelnummer: str
    bezeichnung: str
    ek_preis: Decimal
    vk_preis: Decimal


@dataclass
class FakeDialog:
    titel: str
    text: str
    buttons: tuple[str, ...] = ("DIALOG_JA", "DIALOG_NEIN")
    wirkung: str | None = None  # "verwerfen", "firmenwahl", "drucken", "dateidialog", "export", "wandeln"
    auswahl: str | None = None
    felder: dict[str, str] = field(default_factory=dict)  # Eingabe-/Auswahlfelder des Dialogs
    kontext: Any = None  # z. B. der Beleg, der gedruckt wird


@dataclass
class FakeBeleg:
    belegnummer: str
    auftragsart: str
    partnernummer: str
    datum: date
    netto: Decimal
    positionen: list[tuple[str, Decimal, Decimal]] = field(default_factory=list)  # (artikelnummer, menge, preis effektiv)
    status: str = ""
    felder: dict[str, str] = field(default_factory=dict)
    rabatte: list[tuple[str, Decimal]] = field(default_factory=list)  # (artikelnummer, Rabatt %)


@dataclass
class _Position:
    nr: str
    bez: str
    menge: Decimal
    preis: Decimal  # Listenpreis (EK bzw. VK)
    rabatt: Decimal = Decimal(0)  # Positionsrabatt in %

    @property
    def preis_effektiv(self) -> Decimal:
        if not self.rabatt:
            return self.preis
        return (self.preis * (Decimal(100) - self.rabatt) / Decimal(100)).quantize(Decimal("0.0001"))

    @property
    def zeile(self) -> Decimal:
        return (self.menge * self.preis_effektiv).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


@dataclass
class _Assistent:
    art: str  # "einkauf" / "verkauf"
    titel: str
    schritt: str
    felder: dict[str, str] = field(default_factory=dict)
    eingabe: dict[str, str] = field(default_factory=dict)
    positionen: list[_Position] = field(default_factory=list)
    ziel_schritt: str | None = None
    verzoegerung: int = 0
    weiter_ignoriert: int = 0
    netto: Decimal = Decimal(0)
    brutto: Decimal = Decimal(0)
    quelle: FakeBeleg | None = None
    geschlossen: bool = False

    @property
    def kopfschritt(self) -> str:
        return "Lieferant" if self.art == "einkauf" else "Kunde"


class FakeLexwareBackend:
    """Implementiert ``ui_automation.LexwareBackend`` ohne Windows.

    Wichtige Schalter:
    * ``weiter_erster_klick_ignorieren``: der erste Klick auf "Weiter" bleibt wirkungslos.
    * ``weiter_verzoegerung_abfragen``: Seitenwechsel erst nach n Abfragen des aktiven Schritts.
    * ``dialoge_bei_schritt``: Dialoge, die beim Erreichen eines Schritts erscheinen.
    * ``dialoge_nach_speichern``: Dialoge nach "Speichern" (Druck/E-Mail, USt-IdNr. ...).
    * ``nachfrage_verwerfen``: "Abbrechen" fragt "Änderungen verwerfen?".
    * ``menge_nur_einzeln``: Mengenfeld übernimmt Werte nur bei ``einzeln_tippen=True``.
    * ``felder_fehlend``: Controls, die (wie am echten System möglich) nicht existieren.
    * ``standardtexte``: Auftragsart -> vorbelegter AB-Text (Feld AB_TEXT), wie Lexware-Standardtexte.
    * ``drucker``: verfügbare Drucker im Druckdialog (erster = Standarddrucker, kein PDF!).
    * ``formulare``: verfügbare Formulare (None = jedes Formular ist wählbar).
    """

    def __init__(
        self,
        *,
        firmenname: str = "Wein-Import-Kreis LEH",
        firmen: list[str] | None = None,
        artikel: list[FakeArtikel] | None = None,
        partner: dict[str, str] | None = None,
        belege: list[FakeBeleg] | None = None,
        controls: LexwareControls | None = None,
        laeuft: bool = True,
        weiter_erster_klick_ignorieren: bool = False,
        weiter_verzoegerung_abfragen: int = 0,
        dialoge_bei_schritt: dict[str, list[FakeDialog]] | None = None,
        dialoge_nach_speichern: list[FakeDialog] | None = None,
        dialoge_beim_start: list[FakeDialog] | None = None,
        nachfrage_verwerfen: bool = True,
        menge_nur_einzeln: bool = True,
        felder_fehlend: set[str] | None = None,
        doppelstart_beim_starten: bool = False,
        mwst_satz: Decimal = Decimal("19"),
        heute: Callable[[], date] | None = None,
        max_abfragen: int = 5,
        standardtexte: dict[str, str] | None = None,
        drucker: list[str] | None = None,
        formulare: list[str] | None = None,
    ) -> None:
        self.controls = controls or LexwareControls()
        self.firmenname = firmenname
        self.firmen = list(firmen or [firmenname])
        if firmenname not in self.firmen:
            self.firmen.append(firmenname)
        self.artikel: dict[str, FakeArtikel] = {a.artikelnummer: a for a in artikel or []}
        self.partner: dict[str, str] = dict(partner or {})
        self.belege: list[FakeBeleg] = list(belege or [])
        self.laeuft = laeuft
        self.weiter_erster_klick_ignorieren = weiter_erster_klick_ignorieren
        self.weiter_verzoegerung_abfragen = weiter_verzoegerung_abfragen
        self.dialoge_bei_schritt = {k: list(v) for k, v in (dialoge_bei_schritt or {}).items()}
        self.dialoge_nach_speichern = list(dialoge_nach_speichern or [])
        self.dialoge: list[FakeDialog] = list(dialoge_beim_start or [])
        self.nachfrage_verwerfen = nachfrage_verwerfen
        self.menge_nur_einzeln = menge_nur_einzeln
        self.felder_fehlend = set(felder_fehlend or ())
        self.doppelstart_beim_starten = doppelstart_beim_starten
        self.mwst_satz = Decimal(mwst_satz)
        self._heute = heute or date.today
        self.max_abfragen = max_abfragen
        self.standardtexte: dict[str, str] = dict(standardtexte or {})
        self.drucker: list[str] = list(drucker or ["Standarddrucker (Papier)", "Microsoft Print to PDF"])
        self.formulare: list[str] | None = list(formulare) if formulare is not None else None
        self.aktionen: list[str] = []
        self.screenshots: list[Path] = []
        self.gespeicherte_belege: list[FakeBeleg] = []
        self.gedruckt: list[dict[str, Any]] = []  # PDF-Ausgaben über den Druckdialog
        self.exportiert: list[dict[str, Any]] = []  # PDF-Ausgaben über den Export
        self.papier: list[dict[str, Any]] = []  # Drucke auf einen Nicht-PDF-Drucker (darf nie vorkommen)
        self.assistent: _Assistent | None = None
        self.markierter_beleg: FakeBeleg | None = None
        self.aktive_liste: str | None = None
        self.gestartet_mit: Path | None = None
        self._zaehler: dict[str, int] = {}

    # ----------------------------------------------------------------- Hilfen

    def _aktion(self, text: str) -> None:
        logger.debug("Fake: %s", text)
        self.aktionen.append(text)

    def anzahl(self, praefix: str) -> int:
        return sum(1 for a in self.aktionen if a == praefix or a.startswith(praefix + ":"))

    def hauptfenster_titel(self) -> str:
        # TODO-LEXWARE: angenommenes Titelformat; belegt ist nur, dass die Firma im Titel steht.
        return f"Lexware premium 2026 - {self.firmenname} - Warenwirtschaft"

    def beleg(self, belegnummer: str) -> FakeBeleg | None:
        """Gespeicherter Beleg nach Nummer (für Tests)."""
        for b in self.belege:
            if b.belegnummer == belegnummer:
                return b
        return None

    def _assistent_aus(self, fenster: Fenster) -> _Assistent:
        a = fenster.roh
        if not isinstance(a, _Assistent) or a.geschlossen or a is not self.assistent:
            raise FensterNichtGefunden(f"{fenster} ist kein offener Assistent")
        if self.dialoge:
            raise ControlNichtGefunden(f"Assistent ist durch Dialog {self.dialoge[0].titel!r} blockiert")
        return a

    def _assistent_lesen(self, fenster: Fenster) -> _Assistent:
        a = fenster.roh
        if not isinstance(a, _Assistent) or a.geschlossen:
            raise FensterNichtGefunden(f"{fenster} ist kein offener Assistent")
        return a

    def _schritt_wechseln(self, a: _Assistent, neu: str) -> None:
        if self.weiter_verzoegerung_abfragen > 0:
            a.ziel_schritt = neu
            a.verzoegerung = self.weiter_verzoegerung_abfragen
        else:
            self._schritt_setzen(a, neu)

    def _schritt_setzen(self, a: _Assistent, neu: str) -> None:
        a.schritt = neu
        a.ziel_schritt = None
        a.verzoegerung = 0
        for d in self.dialoge_bei_schritt.pop(neu, []):
            self.dialoge.append(d)
        if neu == "Summen":
            # Lexware rundet je Position (Menge x rabattierter Preis), dann Summe
            a.netto = sum((p.zeile for p in a.positionen), Decimal(0)).quantize(Decimal("0.01"))
            a.brutto = (a.netto * (Decimal(1) + self.mwst_satz / Decimal(100))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    def _neue_belegnummer(self, auftragsart: str) -> str:
        n = self._zaehler.get(auftragsart, 0) + 1
        self._zaehler[auftragsart] = n
        return f"{_PRAEFIX.get(auftragsart, 'XX')}-{self._heute().year}-{n:04d}"

    def _setze_auftragsart(self, a: _Assistent, auftragsart: str) -> None:
        a.felder["AUFTRAGSART"] = auftragsart
        a.titel = re.sub(r" - .*$", f" - {auftragsart}", a.titel)
        # Standardtext je Auftragsart (wie Verwaltung -> Texte -> Standardtexte): AB-Text vorbelegen
        text = self.standardtexte.get(auftragsart)
        if text is not None:
            a.felder["AB_TEXT"] = text

    def _oeffne_assistent(self, art: str, auftragsart: str, *, quelle: FakeBeleg | None = None) -> None:
        if self.assistent is not None and not self.assistent.geschlossen:
            raise ControlNichtGefunden("Es ist bereits ein Assistent geöffnet")
        titel = f"{'Einkaufsauftrag' if art == 'einkauf' else 'Verkaufsauftrag'} neu anlegen - {auftragsart}"
        a = _Assistent(art=art, titel=titel, schritt="Lieferant" if art == "einkauf" else "Kunde")
        a.felder["BELEGDATUM"] = self._heute().strftime("%d.%m.%Y")
        a.eingabe["POSITIONSART"] = "Stammartikel"
        a.eingabe["SUCHKRITERIUM"] = "Alle (AutoSuggest)"
        if quelle is not None:
            a.quelle = quelle
            partner_feld = "LIEFERANTENNUMMER" if art == "einkauf" else "KUNDENNUMMER"
            a.felder[partner_feld] = quelle.partnernummer
            a.felder["PARTNER_NAME"] = self.partner.get(quelle.partnernummer, "")
            a.felder.update(quelle.felder)  # Lieferdatum, Bestellnummer, Freifelder, AB-Text bleiben erhalten [V]
            rabatte = dict(quelle.rabatte)
            for nr, menge, preis in quelle.positionen:
                bez = self.artikel[nr].bezeichnung if nr in self.artikel else f"Artikel {nr}"
                rabatt = rabatte.get(nr, Decimal(0))
                listenpreis = preis
                if rabatt:
                    listenpreis = (preis * Decimal(100) / (Decimal(100) - rabatt)).quantize(Decimal("0.01"))
                a.positionen.append(_Position(nr, bez, menge, listenpreis, rabatt))
        self._setze_auftragsart(a, auftragsart)
        self.assistent = a
        self._aktion(f"assistent:{titel}")

    # ----------------------------------------------------------------- Protocol: Prozess / Fenster

    def prozess_laeuft(self) -> bool:
        return self.laeuft

    def starte(self, exe_pfad: Path) -> None:
        self._aktion(f"starte:{exe_pfad}")
        self.gestartet_mit = Path(exe_pfad)
        self.laeuft = True
        if self.doppelstart_beim_starten:
            self.dialoge.append(
                FakeDialog("Lexware", "Die Anwendung kann nur einmal gestartet werden.", buttons=("DIALOG_OK",))
            )

    def finde_hauptfenster(self, timeout: float | None = None) -> Fenster:
        if not self.laeuft:
            raise FensterNichtGefunden("Lexware läuft nicht (Fake)")
        return Fenster(titel=self.hauptfenster_titel(), roh="haupt", art="haupt")

    def aktive_firma(self) -> str:
        return self.finde_hauptfenster().titel

    def _markierten_beleg(self, funktion: str) -> FakeBeleg:
        if self.markierter_beleg is None:
            raise ControlNichtGefunden(f"{funktion}: kein Auftrag markiert")
        return self.markierter_beleg

    def _weiterfuehren(self) -> None:
        beleg = self._markierten_beleg("Weiterführen")
        if "W" in beleg.status.split():
            raise ControlNichtGefunden(f"Auftrag {beleg.belegnummer} ist bereits weitergeführt")
        naechste = _NAECHSTE_STUFE.get(beleg.auftragsart)
        if naechste is None:
            raise ControlNichtGefunden(f"Auftragsart {beleg.auftragsart} kann nicht weitergeführt werden")
        self._oeffne_assistent("verkauf", naechste, quelle=beleg)

    def oeffne_menue(self, pfad: list[str] | tuple[str, ...]) -> None:
        pfad = tuple(pfad)
        self._aktion("menue:" + " -> ".join(pfad))
        c = self.controls
        if pfad == tuple(c.MENUE_FIRMA_WECHSELN):
            self.dialoge.append(
                FakeDialog("Firma wechseln", "Bitte wählen Sie die Firma:", buttons=("DIALOG_OK", "DIALOG_ABBRECHEN"), wirkung="firmenwahl")
            )
        elif pfad == tuple(c.MENUE_AUFTRAG_WEITERFUEHREN):
            self._weiterfuehren()
        elif pfad == tuple(c.MENUE_AUFTRAG_WANDELN):
            beleg = self._markierten_beleg("Wandeln")
            self.dialoge.append(
                FakeDialog(
                    "Auftrag wandeln",
                    f"Auftrag {beleg.belegnummer} in eine andere Auftragsart wandeln:",
                    buttons=("DIALOG_OK", "DIALOG_ABBRECHEN"),
                    wirkung="wandeln",
                    felder={"WANDELN_ZIELART": ""},
                    kontext=beleg,
                )
            )
        elif pfad == tuple(c.MENUE_AUFTRAGSLISTE_VERKAUF):
            self.aktive_liste = "verkauf"
        elif pfad == tuple(c.MENUE_AUFTRAGSLISTE_EINKAUF):
            self.aktive_liste = "einkauf"
        elif pfad == tuple(c.MENUE_BELEG_DRUCKEN):
            beleg = self._markierten_beleg("Drucken")
            self.dialoge.append(
                FakeDialog(
                    "Drucken",
                    f"Beleg {beleg.belegnummer} ({beleg.auftragsart}) drucken",
                    buttons=("DRUCK_STARTEN", "DIALOG_ABBRECHEN"),
                    wirkung="drucken",
                    felder={"DRUCK_FORMULAR": "Standard", "DRUCK_DRUCKER": self.drucker[0]},
                    kontext=beleg,
                )
            )
        elif pfad == tuple(c.MENUE_BELEG_EXPORT_PDF):
            beleg = self._markierten_beleg("PDF-Export")
            self.dialoge.append(
                FakeDialog(
                    "PDF-Export",
                    f"Beleg {beleg.belegnummer} als PDF exportieren",
                    buttons=("EXPORT_OK", "DIALOG_ABBRECHEN"),
                    wirkung="export",
                    felder={"EXPORT_FORMULAR": "Standard", "EXPORT_DATEINAME": ""},
                    kontext=beleg,
                )
            )
        else:
            raise ControlNichtGefunden(f"Menüpfad {' -> '.join(pfad)} unbekannt (Fake)")

    def sende_tasten(self, fenster: Fenster, tasten: str) -> None:
        self._aktion(f"tasten:{tasten}")
        c = self.controls
        if tasten == c.TASTE_NEUER_EINKAUFSAUFTRAG:
            self._oeffne_assistent("einkauf", "Bestellung")
        elif tasten == c.TASTE_NEUER_VERKAUFSAUFTRAG:
            self._oeffne_assistent("verkauf", "Auftragsbestätigung")
        elif tasten == "{ENTER}":
            return
        else:
            logger.debug("Fake: Tasten %r ohne Wirkung", tasten)

    def _passende_fenster(self, titel_regex: str) -> list[Fenster]:
        gefunden: list[Fenster] = []
        a = self.assistent
        if a is not None and not a.geschlossen and re.search(titel_regex, a.titel, re.I):
            gefunden.append(Fenster(titel=a.titel, roh=a, art="assistent"))
        for d in self.dialoge:
            if re.search(titel_regex, d.titel, re.I):
                gefunden.append(Fenster(titel=d.titel, roh=d, art="dialog"))
        return gefunden

    def finde_dialog(self, titel_regex: str, timeout: float | None = None) -> Fenster:
        gefunden = self._passende_fenster(titel_regex)
        if not gefunden:
            raise FensterNichtGefunden(f"Kein Fenster mit Titel ~ {titel_regex!r} (Fake)")
        return gefunden[0]

    def existiert_dialog(self, titel_regex: str) -> bool:
        return bool(self._passende_fenster(titel_regex))

    def offene_dialoge(self) -> list[Fenster]:
        return [Fenster(titel=d.titel, roh=d, art="dialog") for d in self.dialoge]

    # ----------------------------------------------------------------- Protocol: Controls

    def aktiver_assistent_schritt(self, dialog: Fenster) -> str:
        a = self._assistent_lesen(dialog)
        if a.ziel_schritt is not None:
            a.verzoegerung -= 1
            if a.verzoegerung <= 0:
                self._schritt_setzen(a, a.ziel_schritt)
        return a.schritt

    def klicke(self, dialog: Fenster, control: str) -> None:
        self._aktion(f"klicke:{control}")
        if control in self.felder_fehlend:
            raise ControlNichtGefunden(f"Control {control} existiert nicht (Fake: felder_fehlend)")
        roh = dialog.roh
        if isinstance(roh, FakeDialog):
            self._klick_dialog(roh, control)
            return
        if roh == "haupt":
            if control == "WEITERFUEHREN_BUTTON":
                self._weiterfuehren()
                return
            raise ControlNichtGefunden(f"Button {control} im Hauptfenster unbekannt (Fake)")
        a = self._assistent_aus(dialog)
        if control == "WEITER":
            self._weiter(a)
        elif control == "ZURUECK":
            if a.schritt == "Positionen":
                self._schritt_setzen(a, a.kopfschritt)
            elif a.schritt == "Summen":
                self._schritt_setzen(a, "Positionen")
            else:
                raise ControlNichtGefunden("'Zurück' auf Seite 1 nicht aktiv")
        elif control == "ABBRECHEN":
            if self.nachfrage_verwerfen:
                self.dialoge.append(
                    FakeDialog("Abbrechen", "Möchten Sie die Änderungen wirklich verwerfen?", wirkung="verwerfen")
                )
            else:
                a.geschlossen = True
        elif control == "SPEICHERN":
            self._speichern(a)
        elif control == "MENGE":
            if a.schritt != "Positionen":
                raise ControlNichtGefunden("Mengenfeld nur auf Seite 'Positionen'")
        elif control == "POSITION_UEBERNEHMEN":
            self._position_uebernehmen(a)
        elif control == "POSITION_VERWERFEN":
            for k in _POSITIONS_FELDER:
                a.eingabe.pop(k, None)
        elif control == "REGISTER_NACHBEMERKUNG":
            if a.schritt != "Summen":
                raise ControlNichtGefunden("Register 'Nachbemerkung' nur auf Seite 'Summen'")
        else:
            raise ControlNichtGefunden(f"Button {control} im Assistenten unbekannt (Fake)")

    def _klick_dialog(self, d: FakeDialog, control: str) -> None:
        if d not in self.dialoge:
            raise FensterNichtGefunden(f"Dialog {d.titel!r} ist nicht mehr offen")
        if control not in d.buttons:
            raise ControlNichtGefunden(f"Dialog {d.titel!r} hat keinen Button {control} (vorhanden: {d.buttons})")
        self.dialoge.remove(d)
        if d.wirkung == "verwerfen" and control == "DIALOG_JA" and self.assistent is not None:
            self.assistent.geschlossen = True
        elif d.wirkung == "firmenwahl" and control == "DIALOG_OK" and d.auswahl:
            self.firmenname = d.auswahl
            if self.assistent is not None:
                self.assistent.geschlossen = True  # [V] alle Fenster der alten Firma werden geschlossen
            self._aktion(f"firma:{d.auswahl}")
        elif d.wirkung == "wandeln" and control == "DIALOG_OK":
            self._wandeln(d)
        elif d.wirkung == "drucken" and control == "DRUCK_STARTEN":
            self._druck_starten(d)
        elif d.wirkung == "dateidialog" and control == "DATEIDIALOG_SPEICHERN":
            self._pdf_schreiben(d, d.felder.get("DATEIDIALOG_NAME", ""), self.gedruckt)
        elif d.wirkung == "export" and control == "EXPORT_OK":
            self._pdf_schreiben(d, d.felder.get("EXPORT_DATEINAME", ""), self.exportiert)

    def _wandeln(self, d: FakeDialog) -> None:
        zielart = d.felder.get("WANDELN_ZIELART", "")
        beleg: FakeBeleg = d.kontext
        if zielart in _AUFTRAGSARTEN["verkauf"]:
            art = "verkauf"
        elif zielart in _AUFTRAGSARTEN["einkauf"]:
            art = "einkauf"
        else:
            raise ControlNichtGefunden(f"Auftrag wandeln: Zielauftragsart {zielart!r} unbekannt")
        self._aktion(f"gewandelt:{beleg.belegnummer}:{zielart}")
        self._oeffne_assistent(art, zielart, quelle=beleg)

    def _druck_starten(self, d: FakeDialog) -> None:
        beleg: FakeBeleg = d.kontext
        formular = d.felder.get("DRUCK_FORMULAR", "")
        drucker = d.felder.get("DRUCK_DRUCKER", "")
        if "pdf" in drucker.casefold():
            self.dialoge.append(
                FakeDialog(
                    "Druckausgabe speichern unter",
                    "Dateiname für die PDF-Ausgabe wählen",
                    buttons=("DATEIDIALOG_SPEICHERN", "DIALOG_ABBRECHEN"),
                    wirkung="dateidialog",
                    felder={"DATEIDIALOG_NAME": ""},
                    kontext={"beleg": beleg, "formular": formular, "drucker": drucker},
                )
            )
            return
        # Papierdruck: darf nie vorkommen – wird protokolliert, damit Tests es erkennen
        self.papier.append({"beleg": beleg.belegnummer, "formular": formular, "drucker": drucker})
        beleg.status = (beleg.status + " D").strip()
        self._aktion(f"papier:{beleg.belegnummer}:{drucker}")

    def _pdf_schreiben(self, d: FakeDialog, pfad_text: str, ziel_liste: list[dict[str, Any]]) -> None:
        if not pfad_text.strip():
            raise ControlNichtGefunden("Dateiname ist leer")
        kontext = d.kontext if isinstance(d.kontext, dict) else {"beleg": d.kontext, "formular": d.felder.get("EXPORT_FORMULAR", "")}
        beleg: FakeBeleg = kontext["beleg"]
        pfad = Path(pfad_text)
        pfad.parent.mkdir(parents=True, exist_ok=True)
        pfad.write_bytes(f"%PDF-1.4 Fake {beleg.belegnummer} {kontext.get('formular', '')}\n".encode())
        beleg.status = (beleg.status + " D").strip()
        ziel_liste.append({"beleg": beleg.belegnummer, "formular": kontext.get("formular", ""), "drucker": kontext.get("drucker"), "pfad": pfad})
        self._aktion(f"pdf:{beleg.belegnummer}:{pfad.name}")

    def _weiter(self, a: _Assistent) -> None:
        if a.schritt == "Summen":
            raise ControlNichtGefunden("'Weiter' auf der letzten Seite nicht vorhanden")
        if self.weiter_erster_klick_ignorieren and a.weiter_ignoriert == 0:
            a.weiter_ignoriert += 1
            logger.debug("Fake: erster Klick auf 'Weiter' ignoriert")
            return
        if a.schritt == a.kopfschritt:
            if not a.felder.get("PARTNER_NAME"):
                self.dialoge.append(FakeDialog("Hinweis", "Bitte geben Sie einen gültigen Kunden/Lieferanten an.", buttons=("DIALOG_OK",)))
                return
            self._schritt_wechseln(a, "Positionen")
        elif a.schritt == "Positionen":
            if a.eingabe.get("ARTIKELNUMMER"):
                self.dialoge.append(
                    FakeDialog("Hinweis", "Die aktuelle Position wurde noch nicht übernommen oder verworfen.", buttons=("DIALOG_OK",))
                )
                return
            self._schritt_wechseln(a, "Summen")

    def _position_uebernehmen(self, a: _Assistent) -> None:
        if a.schritt != "Positionen":
            raise ControlNichtGefunden("Übernehmen nur auf Seite 'Positionen'")
        nr = a.eingabe.get("ARTIKELNUMMER", "")
        bez = a.eingabe.get("ARTIKEL_BEZEICHNUNG", "")
        menge = parse_betrag(a.eingabe.get("MENGE", ""))
        preis = parse_betrag(a.eingabe.get("PREIS", ""))
        rabatt = parse_betrag(a.eingabe.get("RABATT", "")) or Decimal(0)
        if not nr or not bez or menge is None or menge <= 0 or preis is None:
            raise ControlNichtGefunden(f"Position unvollständig (Artikel {nr!r}, Menge {a.eingabe.get('MENGE')!r})")
        if rabatt < 0 or rabatt > 100:
            raise ControlNichtGefunden(f"Rabatt {rabatt} % ungültig")
        a.positionen.append(_Position(nr, bez, menge, preis, rabatt))
        for k in _POSITIONS_FELDER:
            a.eingabe.pop(k, None)

    def _speichern(self, a: _Assistent) -> None:
        if a.schritt != "Summen":
            raise ControlNichtGefunden("'Speichern' nur auf der Seite 'Summen' (Fake)")
        if not a.positionen:
            raise ControlNichtGefunden("Speichern ohne Positionen nicht möglich")
        auftragsart = a.felder["AUFTRAGSART"]
        partner = a.felder.get("LIEFERANTENNUMMER") or a.felder.get("KUNDENNUMMER") or ""
        beleg = FakeBeleg(
            belegnummer=self._neue_belegnummer(auftragsart),
            auftragsart=auftragsart,
            partnernummer=partner,
            datum=self._heute(),
            netto=a.netto,
            positionen=[(p.nr, p.menge, p.preis_effektiv) for p in a.positionen],
            felder={k: v for k, v in a.felder.items() if k not in {"AUFTRAGSART", "PARTNER_NAME"}},
            rabatte=[(p.nr, p.rabatt) for p in a.positionen if p.rabatt],
        )
        self.belege.append(beleg)
        self.gespeicherte_belege.append(beleg)
        if a.quelle is not None:
            a.quelle.status = (a.quelle.status + " W").strip()
        a.geschlossen = True
        self._aktion(f"gespeichert:{beleg.belegnummer}")
        self.dialoge.extend(FakeDialog(d.titel, d.text, d.buttons, d.wirkung) for d in self.dialoge_nach_speichern)

    def _dialogfeld_setzen(self, d: FakeDialog, control: str, text: str) -> None:
        if d not in self.dialoge:
            raise FensterNichtGefunden(f"Dialog {d.titel!r} ist nicht mehr offen")
        if control not in d.felder:
            raise ControlNichtGefunden(f"Dialog {d.titel!r} hat kein Feld {control} (vorhanden: {sorted(d.felder)})")
        if control == "DRUCK_DRUCKER" and text not in self.drucker:
            raise ControlNichtGefunden(f"Drucker {text!r} nicht vorhanden (Fake: {self.drucker})")
        if control in {"DRUCK_FORMULAR", "EXPORT_FORMULAR"} and self.formulare is not None and text not in self.formulare:
            raise ControlNichtGefunden(f"Formular {text!r} nicht vorhanden (Fake: {self.formulare})")
        d.felder[control] = text

    def setze_text(self, dialog: Fenster, control: str, text: str, *, einzeln_tippen: bool = False) -> None:
        if einzeln_tippen and text:
            for zeichen in text:
                self._aktion(f"tippe:{control}:{zeichen}")
        else:
            self._aktion(f"setze:{control}:{text}")
        if control in self.felder_fehlend:
            raise ControlNichtGefunden(f"Control {control} existiert nicht (Fake: felder_fehlend)")
        roh = dialog.roh
        if isinstance(roh, FakeDialog):
            self._dialogfeld_setzen(roh, control, text)
            return
        a = self._assistent_aus(dialog)
        if control in _KOPF_FELDER:
            if a.schritt != a.kopfschritt:
                raise ControlNichtGefunden(f"Feld {control} nur auf Seite 1 (aktiv: {a.schritt})")
            a.felder[control] = text
            if control in {"KUNDENNUMMER", "LIEFERANTENNUMMER"}:
                # AutoSuggest: Stammdaten werden nur bei Tastenereignissen übernommen
                a.felder["PARTNER_NAME"] = self.partner.get(text, "") if einzeln_tippen else ""
            return
        if control in _POSITIONS_FELDER:
            if a.schritt != "Positionen":
                raise ControlNichtGefunden(f"Feld {control} nur auf Seite 'Positionen' (aktiv: {a.schritt})")
            if control == "ARTIKELNUMMER":
                a.eingabe["ARTIKELNUMMER"] = text
                art = self.artikel.get(text) if einzeln_tippen else None
                if art is None:
                    a.eingabe["ARTIKEL_BEZEICHNUNG"] = ""
                    a.eingabe["PREIS"] = ""
                else:
                    a.eingabe["ARTIKEL_BEZEICHNUNG"] = art.bezeichnung
                    a.eingabe["PREIS"] = formatiere_betrag(art.ek_preis if a.art == "einkauf" else art.vk_preis)
                return
            if control == "MENGE":
                if einzeln_tippen or not self.menge_nur_einzeln:
                    a.eingabe["MENGE"] = text
                else:
                    logger.debug("Fake: Menge %r per set_text ignoriert (nur Tastendruck)", text)
                return
            if control == "RABATT":
                if a.art == "einkauf":
                    raise ControlNichtGefunden("Positionsrabatt gibt es im Einkaufsassistenten nicht (Fake)")
                a.eingabe["RABATT"] = text
                return
            raise ControlNichtGefunden(f"Feld {control} ist nicht editierbar")
        if control == "NACHBEMERKUNG":
            if a.schritt != "Summen":
                raise ControlNichtGefunden(f"Nachbemerkung nur auf Seite 'Summen' (aktiv: {a.schritt})")
            a.felder["NACHBEMERKUNG"] = text
            return
        raise ControlNichtGefunden(f"Feld {control} unbekannt (Fake)")

    def lese_text(self, dialog: Fenster, control: str) -> str:
        roh = dialog.roh
        if control == "DIALOG_TEXT":
            return roh.text if isinstance(roh, FakeDialog) else ""
        if control in self.felder_fehlend:
            raise ControlNichtGefunden(f"Control {control} existiert nicht (Fake: felder_fehlend)")
        if isinstance(roh, FakeDialog):
            if control in roh.felder:
                return roh.felder[control]
            raise ControlNichtGefunden(f"Dialog {roh.titel!r} hat kein Feld {control}")
        a = self._assistent_lesen(dialog)
        if control in {"AUFTRAGSART", "PARTNER_NAME"} or control in _KOPF_FELDER:
            return a.felder.get(control, "")
        if control in {"POSITIONSART", "SUCHKRITERIUM"} or control in _POSITIONS_FELDER:
            if a.schritt != "Positionen":
                raise ControlNichtGefunden(f"Feld {control} nur auf Seite 'Positionen' (aktiv: {a.schritt})")
            return a.eingabe.get(control, "")
        if control in _SUMMEN_FELDER:
            if a.schritt != "Summen":
                raise ControlNichtGefunden(f"Feld {control} nur auf Seite 'Summen' (aktiv: {a.schritt})")
            if control == "SUMME_NETTO":
                return formatiere_betrag(a.netto)
            if control == "SUMME_BRUTTO":
                return formatiere_betrag(a.brutto)
            if control == "SUMME_STEUER":
                return formatiere_betrag(a.brutto - a.netto)
            return a.felder.get(control, "")
        if control == "INFO_BELEGNUMMER":
            return ""
        raise ControlNichtGefunden(f"Feld {control} unbekannt (Fake)")

    def waehle(self, dialog: Fenster, control: str, wert: str) -> None:
        self._aktion(f"waehle:{control}:{wert}")
        if control in self.felder_fehlend:
            raise ControlNichtGefunden(f"Control {control} existiert nicht (Fake: felder_fehlend)")
        roh = dialog.roh
        if isinstance(roh, FakeDialog):
            self._dialogfeld_setzen(roh, control, wert)
            return
        a = self._assistent_aus(dialog)
        if control == "AUFTRAGSART":
            if a.schritt != a.kopfschritt:
                raise ControlNichtGefunden("Auftragsart nur auf Seite 1 wählbar")
            if wert not in _AUFTRAGSARTEN[a.art]:
                raise ControlNichtGefunden(f"Auftragsart {wert!r} im {a.art} nicht verfügbar")
            self._setze_auftragsart(a, wert)
            return
        if control in {"POSITIONSART", "SUCHKRITERIUM"}:
            if a.schritt != "Positionen":
                raise ControlNichtGefunden(f"{control} nur auf Seite 'Positionen'")
            a.eingabe[control] = wert
            return
        if control in {"LIEFERART", "ZAHLUNGSBEDINGUNG"}:
            a.felder[control] = wert
            return
        raise ControlNichtGefunden(f"Auswahlfeld {control} unbekannt (Fake)")

    def _belegzeile(self, b: FakeBeleg, einkauf: bool) -> dict[str, str]:
        c = self.controls
        partner_spalte = c.SPALTE_LIEFERANTENNUMMER if einkauf else c.SPALTE_KUNDENNUMMER
        return {
            c.SPALTE_BELEGNUMMER: b.belegnummer,
            c.SPALTE_AUFTRAGSART: b.auftragsart,
            partner_spalte: b.partnernummer,
            c.SPALTE_DATUM: b.datum.strftime("%d.%m.%Y"),
            c.SPALTE_NETTO: formatiere_betrag(b.netto),
            c.SPALTE_STATUS: b.status,
        }

    def lese_liste(self, dialog: Fenster, control: str) -> list[dict[str, str]]:
        c = self.controls
        if control in self.felder_fehlend:
            raise ControlNichtGefunden(f"Control {control} existiert nicht (Fake: felder_fehlend)")
        if control == "POSITIONSLISTE":
            a = self._assistent_lesen(dialog)
            if a.schritt == a.kopfschritt:
                raise ControlNichtGefunden("Positionsliste erst ab Seite 'Positionen'")
            return [
                {
                    c.SPALTE_ARTIKELNUMMER: p.nr,
                    c.SPALTE_BEZEICHNUNG: p.bez,
                    c.SPALTE_MENGE: formatiere_menge(p.menge),
                    c.SPALTE_PREIS: formatiere_betrag(p.preis),
                    c.SPALTE_RABATT: formatiere_menge(p.rabatt) if p.rabatt else "",
                }
                for p in a.positionen
            ]
        if control in {"AUFTRAGSLISTE_VERKAUF", "AUFTRAGSLISTE_EINKAUF"}:
            if dialog.roh != "haupt":
                raise ControlNichtGefunden("Auftragslisten gibt es nur im Hauptfenster")
            einkauf = control == "AUFTRAGSLISTE_EINKAUF"
            arten = _AUFTRAGSARTEN["einkauf" if einkauf else "verkauf"]
            return [self._belegzeile(b, einkauf) for b in self.belege if b.auftragsart in arten]
        raise ControlNichtGefunden(f"Liste {control} unbekannt (Fake)")

    def waehle_listeneintrag(self, dialog: Fenster, control: str, suchtext: str) -> None:
        self._aktion(f"markiere:{control}:{suchtext}")
        roh = dialog.roh
        if control == "FIRMENLISTE":
            if not isinstance(roh, FakeDialog) or roh.wirkung != "firmenwahl":
                raise ControlNichtGefunden("Firmenliste nur im Dialog 'Firma wechseln'")
            if suchtext not in self.firmen:
                raise ControlNichtGefunden(f"Firma {suchtext!r} nicht in der Liste {self.firmen}")
            roh.auswahl = suchtext
            return
        if control in {"AUFTRAGSLISTE_VERKAUF", "AUFTRAGSLISTE_EINKAUF"}:
            arten = _AUFTRAGSARTEN["einkauf" if control == "AUFTRAGSLISTE_EINKAUF" else "verkauf"]
            for b in self.belege:
                if b.belegnummer == suchtext and b.auftragsart in arten:
                    self.markierter_beleg = b
                    return
            raise ControlNichtGefunden(f"Beleg {suchtext!r} nicht in der Liste")
        raise ControlNichtGefunden(f"Liste {control} unbekannt (Fake)")

    def warte_bis(self, praedikat: Callable[[], bool], timeout: float | None = None, beschreibung: str = "") -> bool:
        self._aktion(f"warte:{beschreibung}")
        for _ in range(self.max_abfragen):
            try:
                if praedikat():
                    return True
            except Exception as exc:  # noqa: BLE001
                logger.debug("Fake: Prädikat %s warf %s", beschreibung, exc)
        return False

    def screenshot(self, pfad: Path) -> Path | None:
        pfad = Path(pfad)
        pfad.parent.mkdir(parents=True, exist_ok=True)
        zustand: dict[str, Any] = {
            "firma": self.firmenname,
            "assistent": None if self.assistent is None or self.assistent.geschlossen else {
                "titel": self.assistent.titel, "schritt": self.assistent.schritt, "felder": self.assistent.felder,
            },
            "dialoge": [(d.titel, d.text) for d in self.dialoge],
        }
        pfad.write_text(repr(zustand), encoding="utf-8")
        self.screenshots.append(pfad)
        self._aktion(f"screenshot:{pfad.name}")
        return pfad
