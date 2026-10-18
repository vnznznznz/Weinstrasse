"""UI-Automation für Lexware warenwirtschaft premium (pywinauto, UIA-Backend).

Aufgabe: "Bestellung schreiben" (Einkauf), Fallback "Auftrag schreiben" (Verkauf-AB) und
"Auftrag weiterführen" (Lieferschein) über den Auftragsassistenten von Lexware bedienen.

Aufbau
------
* ``LexwareBackend`` (Protocol): kleine, plattformunabhängige Operationen auf Fenstern und
  Controls. Zwei Implementierungen: ``PywinautoBackend`` (nur Windows, importiert pywinauto erst
  im Konstruktor) und ``fake_backend.FakeLexwareBackend`` (Tests und Dry Run unter Linux).
* ``LexwareControls``: ALLE Control-Bezeichner (automation_id / title / control_type) an einer
  Stelle. Die Werte stammen aus ``docs/recherche/ui_automation_lexware.md``; was dort nicht belegt
  ist, ist beste Annahme und mit ``# TODO-LEXWARE`` markiert. Der Anwender passt sie nach
  ``print_control_identifiers()`` in ``config/lexware_controls.yaml`` an (``lade_controls``).
* ``UiSchreiber``: die Ablauflogik (Schrittfolge, Mengen einzeln tippen, Summenvergleich,
  Dry Run = Abbrechen statt Speichern). Sie kennt nur das Protocol und ist damit ohne Windows
  testbar.

Geschäftsregeln (docs/ARCHITEKTUR.md): Firma vor jedem Beleg prüfen; nichts drucken, nichts
mailen; USt-IdNr.-Dialog -> Nein; Preise aus dem Lexware-Stamm (nur kontrollieren); Dry Run ist
Standard und endet mit "Abbrechen"; Lexware nur einmal starten; unbekannte Dialoge werden NIE
blind bestätigt.
"""

from __future__ import annotations

import dataclasses
import logging
import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field, fields
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import yaml

from auftragsautomat.config import Firma, Settings, projekt_stamm
from auftragsautomat.lexware import instanz
from auftragsautomat.lexware.dialoge import DialogFehler, DialogKontext, behandle_dialoge
from auftragsautomat.models import (
    Belegart,
    Belegergebnis,
    Belegpaket,
    PositionsZuordnung,
)

logger = logging.getLogger(__name__)

__all__ = [
    "AblaufFehler",
    "AssistentSchritt",
    "BackendNichtVerfuegbar",
    "ControlNichtGefunden",
    "ControlSpec",
    "FalscheFirma",
    "Fenster",
    "FensterNichtGefunden",
    "LexwareBackend",
    "LexwareControls",
    "NichtAufWindows",
    "PywinautoBackend",
    "SummenAbweichung",
    "UiAutomationFehler",
    "UiSchreiber",
    "erkenne_schritt",
    "formatiere_menge",
    "lade_controls",
    "parse_betrag",
    "standard_controls_datei",
]

SUMMEN_TOLERANZ = Decimal("0.01")
PREIS_TOLERANZ = Decimal("0.005")
MAX_WEITER_VERSUCHE = 3
DATUMSFORMAT = "%d.%m.%Y"


# ---------------------------------------------------------------------------
# Fehler
# ---------------------------------------------------------------------------


class UiAutomationFehler(Exception):
    """Basisklasse aller Fehler der UI-Automation."""


class NichtAufWindows(UiAutomationFehler):
    """pywinauto-Backend wurde außerhalb von Windows angefordert."""


class BackendNichtVerfuegbar(UiAutomationFehler):
    """pywinauto ist nicht installiert (pip install auftragsautomat[windows])."""


class FensterNichtGefunden(UiAutomationFehler):
    """Hauptfenster, Assistent oder Dialog nicht gefunden."""


class ControlNichtGefunden(UiAutomationFehler):
    """Ein Control aus ``LexwareControls`` existiert im Fenster nicht."""


class FalscheFirma(UiAutomationFehler):
    """Die aktive Lexware-Firma ist nicht die Firma des Belegpakets."""


class SummenAbweichung(UiAutomationFehler):
    """Netto laut Lexware weicht von der erwarteten Summe ab -> nicht speichern."""


class AblaufFehler(UiAutomationFehler):
    """Der Assistent ist nicht im erwarteten Zustand (Schritt, Position, Wert)."""


# ---------------------------------------------------------------------------
# Fenster, Controls
# ---------------------------------------------------------------------------


@dataclass
class Fenster:
    """Handle auf ein Fenster. ``roh`` ist backend-spezifisch (pywinauto-Wrapper oder Fake)."""

    titel: str
    roh: Any = None
    art: str = "fenster"  # "haupt", "assistent", "dialog"

    def __str__(self) -> str:
        return f"{self.art} '{self.titel}'"


@dataclass(frozen=True)
class ControlSpec:
    """Suchkriterien für ein Control (pywinauto ``child_window``-Argumente)."""

    title: str | None = None
    title_re: str | None = None
    auto_id: str | None = None
    control_type: str | None = None
    class_name: str | None = None
    found_index: int | None = None

    def pywinauto_kwargs(self) -> dict[str, Any]:
        return {k: v for k, v in dataclasses.asdict(self).items() if v is not None}

    def beschreibung(self) -> str:
        teile = [f"{k}={v!r}" for k, v in self.pywinauto_kwargs().items()]
        return ", ".join(teile) or "<leer>"


_CONTROLSPEC_FELDER = {f.name for f in fields(ControlSpec)}


@dataclass
class LexwareControls:
    """Alle Bezeichner der Lexware-Oberfläche an EINER Stelle.

    Belegt [V] sind nur die Beschriftungen aus dem Handbuch (Weiter, Zurück, Speichern, Abbrechen,
    Positionsart, Menge, grünes Häkchen, Seite "Summen", Feld "Bestellnummer", "Lieferdatum").
    automation_ids sind nirgends dokumentiert; alle Angaben sind daher Titel-basiert und nach
    ``print_control_identifiers()`` zu prüfen (TODO-LEXWARE an jedem Feld).
    Regexe werden mit ``re.search`` (ignorecase) geprüft; pywinauto ``title_re`` prüft vom Anfang,
    deshalb beginnen alle Titel-Regexe mit ``.*``.
    """

    # --- Prozess / Hauptfenster ------------------------------------------------------------
    PROZESSNAME: str = "Framework.exe"  # [V-Dritt] Hauptprozess von Lexware pro/premium
    HAUPTFENSTER_TITEL_REGEX: str = r".*Lexware.*"  # TODO-LEXWARE: exakter Titel (enthält Firma [V])
    FIRMENWAHL_TITEL_REGEX: str = r".*(Firma|Wechseln).*"  # TODO-LEXWARE: Titel des Dialogs Datei -> Wechseln
    FIRMENLISTE: ControlSpec = field(default_factory=lambda: ControlSpec(control_type="List"))  # TODO-LEXWARE

    # --- Tasten / Menüs ----------------------------------------------------------------------
    TASTE_NEUER_EINKAUFSAUFTRAG: str = "^m"  # [V] Strg+M: Einkaufsauftrag im Assistenten
    TASTE_NEUER_VERKAUFSAUFTRAG: str = "^n"  # [V] Strg+N: Verkaufsauftrag im Assistenten
    MENUE_FIRMA_WECHSELN: tuple[str, ...] = ("Datei", "Wechseln")  # [V]
    MENUE_AUFTRAG_WEITERFUEHREN: tuple[str, ...] = ("Bearbeiten", "Aufträge", "Weiterführen")  # [V]
    MENUE_AUFTRAGSLISTE_VERKAUF: tuple[str, ...] = ("Verkauf", "Aufträge")  # TODO-LEXWARE: Navigation zur Liste
    MENUE_AUFTRAGSLISTE_EINKAUF: tuple[str, ...] = ("Einkauf", "Aufträge")  # TODO-LEXWARE

    # --- Assistent ---------------------------------------------------------------------------
    ASSISTENT_TITEL_REGEX: str = r".*(Auftrag|Bestellung|Angebot|Lieferschein|Rechnung).*"  # TODO-LEXWARE
    ASSISTENT_EINKAUF_TITEL_REGEX: str = r".*(Einkauf|Bestell).*"  # TODO-LEXWARE
    ASSISTENT_VERKAUF_TITEL_REGEX: str = r".*(Verkauf|Auftrag|Lieferschein).*"  # TODO-LEXWARE
    SEITENLISTE: ControlSpec = field(default_factory=lambda: ControlSpec(control_type="List", found_index=0))  # TODO-LEXWARE
    SCHRITT_KOPF_REGEX: str = r"^\s*(\d+\W*)?(Kunde|Lieferant|Adress|Kopf|Rahmen)"  # [A] Seite 1
    SCHRITT_POSITIONEN_REGEX: str = r"^\s*(\d+\W*)?Position"  # [V] Seite 2
    SCHRITT_SUMMEN_REGEX: str = r"^\s*(\d+\W*)?(Summe|Kondition)"  # [V] Seite "Summen"
    WEITER: ControlSpec = field(default_factory=lambda: ControlSpec(title="Weiter", control_type="Button"))  # [V]
    ZURUECK: ControlSpec = field(default_factory=lambda: ControlSpec(title="Zurück", control_type="Button"))  # [V]
    SPEICHERN: ControlSpec = field(default_factory=lambda: ControlSpec(title="Speichern", control_type="Button"))  # [V]
    ABBRECHEN: ControlSpec = field(default_factory=lambda: ControlSpec(title="Abbrechen", control_type="Button"))  # [V]

    # --- Seite 1: Kopfdaten ------------------------------------------------------------------
    AUFTRAGSART: ControlSpec = field(default_factory=lambda: ControlSpec(title="Auftragsart", control_type="ComboBox"))  # TODO-LEXWARE
    KUNDENNUMMER: ControlSpec = field(default_factory=lambda: ControlSpec(title="Kundennummer", control_type="Edit"))  # TODO-LEXWARE
    LIEFERANTENNUMMER: ControlSpec = field(default_factory=lambda: ControlSpec(title="Lieferantennummer", control_type="Edit"))  # TODO-LEXWARE
    PARTNER_NAME: ControlSpec = field(default_factory=lambda: ControlSpec(title="Firma", control_type="Edit"))  # TODO-LEXWARE: Name nach Übernahme
    BELEGDATUM: ControlSpec = field(default_factory=lambda: ControlSpec(title="Belegdatum", control_type="Edit"))  # TODO-LEXWARE
    LIEFERDATUM: ControlSpec = field(default_factory=lambda: ControlSpec(title="Lieferdatum", control_type="Edit"))  # [V] Feldname
    BESTELLNUMMER: ControlSpec = field(default_factory=lambda: ControlSpec(title="Bestellnummer", control_type="Edit"))  # [V] Feldname
    AUFTRAGSBESCHREIBUNG: ControlSpec = field(default_factory=lambda: ControlSpec(title="Auftragsbeschreibung", control_type="Edit"))  # TODO-LEXWARE
    LIEFERADRESSE: ControlSpec = field(default_factory=lambda: ControlSpec(title="Lieferanschrift", control_type="ComboBox"))  # TODO-LEXWARE
    FREIFELD_SPEDITION: ControlSpec = field(default_factory=lambda: ControlSpec(title="Spedition", control_type="Edit"))  # TODO-LEXWARE: Auftrags-Freifeld
    FREIFELD_WEINGUT: ControlSpec = field(default_factory=lambda: ControlSpec(title="Weingut", control_type="Edit"))  # TODO-LEXWARE
    FREIFELD_SHIPMENT: ControlSpec = field(default_factory=lambda: ControlSpec(title="Shipment", control_type="Edit"))  # TODO-LEXWARE
    FREIFELD_KUNDE: ControlSpec = field(default_factory=lambda: ControlSpec(title="Customer", control_type="Edit"))  # TODO-LEXWARE

    # --- Seite 2: Positionen -----------------------------------------------------------------
    POSITIONSART: ControlSpec = field(default_factory=lambda: ControlSpec(title="Positionsart", control_type="ComboBox"))  # [V] Name
    POSITIONSART_STAMMARTIKEL: str = "Stammartikel"  # [V]
    SUCHKRITERIUM: ControlSpec = field(default_factory=lambda: ControlSpec(title="Suchkriterium", control_type="ComboBox"))  # TODO-LEXWARE
    SUCHKRITERIUM_NUMMER: str = "Nummer"  # [V]
    ARTIKELNUMMER: ControlSpec = field(default_factory=lambda: ControlSpec(title="Artikelnummer", control_type="Edit"))  # TODO-LEXWARE
    ARTIKEL_BEZEICHNUNG: ControlSpec = field(default_factory=lambda: ControlSpec(title="Bezeichnung", control_type="Edit"))  # TODO-LEXWARE
    MENGE: ControlSpec = field(default_factory=lambda: ControlSpec(title="Menge", control_type="Edit"))  # [V] Name
    PREIS: ControlSpec = field(default_factory=lambda: ControlSpec(title="Einzelpreis", control_type="Edit"))  # TODO-LEXWARE
    POSITION_UEBERNEHMEN: ControlSpec = field(default_factory=lambda: ControlSpec(title="Übernehmen", control_type="Button"))  # TODO-LEXWARE: grünes Häkchen
    POSITION_VERWERFEN: ControlSpec = field(default_factory=lambda: ControlSpec(title="Verwerfen", control_type="Button"))  # TODO-LEXWARE
    POSITIONSLISTE: ControlSpec = field(default_factory=lambda: ControlSpec(control_type="DataGrid"))  # TODO-LEXWARE: evtl. Table/List

    # --- Seite 3: Summen ---------------------------------------------------------------------
    SUMME_NETTO: ControlSpec = field(default_factory=lambda: ControlSpec(title="Netto", control_type="Edit"))  # TODO-LEXWARE
    SUMME_BRUTTO: ControlSpec = field(default_factory=lambda: ControlSpec(title="Brutto", control_type="Edit"))  # TODO-LEXWARE
    SUMME_STEUER: ControlSpec = field(default_factory=lambda: ControlSpec(title="Steuer", control_type="Edit"))  # TODO-LEXWARE
    LIEFERART: ControlSpec = field(default_factory=lambda: ControlSpec(title="Lieferart", control_type="ComboBox"))  # [V] Name
    ZAHLUNGSBEDINGUNG: ControlSpec = field(default_factory=lambda: ControlSpec(title="Zahlungsbedingung", control_type="ComboBox"))  # [V] Name

    # --- Dialoge -----------------------------------------------------------------------------
    DIALOG_TEXT: ControlSpec = field(default_factory=lambda: ControlSpec(control_type="Text"))  # TODO-LEXWARE
    DIALOG_JA: ControlSpec = field(default_factory=lambda: ControlSpec(title="Ja", control_type="Button"))
    DIALOG_NEIN: ControlSpec = field(default_factory=lambda: ControlSpec(title="Nein", control_type="Button"))
    DIALOG_OK: ControlSpec = field(default_factory=lambda: ControlSpec(title="OK", control_type="Button"))
    DIALOG_ABBRECHEN: ControlSpec = field(default_factory=lambda: ControlSpec(title="Abbrechen", control_type="Button"))
    DIALOG_UEBERSPRINGEN: ControlSpec = field(default_factory=lambda: ControlSpec(title_re=".*(Überspringen|Weder|Nicht drucken|Schließen).*", control_type="Button"))  # TODO-LEXWARE: Wortlaut nicht belegt

    # --- Auftragslisten / Infobereich --------------------------------------------------------
    AUFTRAGSLISTE_VERKAUF: ControlSpec = field(default_factory=lambda: ControlSpec(control_type="DataGrid"))  # TODO-LEXWARE
    AUFTRAGSLISTE_EINKAUF: ControlSpec = field(default_factory=lambda: ControlSpec(control_type="DataGrid"))  # TODO-LEXWARE
    INFO_BELEGNUMMER: ControlSpec = field(default_factory=lambda: ControlSpec(title="Belegnummer", control_type="Text"))  # TODO-LEXWARE: Infobereich Kopfzeile
    AUFTRAGSART_BESTELLUNG: str = "Bestellung"  # [V]
    AUFTRAGSART_AB: str = "Auftragsbestätigung"  # [V]
    AUFTRAGSART_LIEFERSCHEIN: str = "Lieferschein"  # [V]
    SPALTE_BELEGNUMMER: str = "Belegnummer"  # TODO-LEXWARE: Spaltenüberschriften der Listen
    SPALTE_AUFTRAGSART: str = "Auftragsart"
    SPALTE_KUNDENNUMMER: str = "Kundennummer"
    SPALTE_LIEFERANTENNUMMER: str = "Lieferantennummer"
    SPALTE_DATUM: str = "Belegdatum"
    SPALTE_NETTO: str = "Netto"
    SPALTE_STATUS: str = "Status"
    SPALTE_ARTIKELNUMMER: str = "Artikelnummer"
    SPALTE_BEZEICHNUNG: str = "Bezeichnung"
    SPALTE_MENGE: str = "Menge"
    SPALTE_PREIS: str = "Einzelpreis"

    def spec(self, name: str) -> ControlSpec:
        """Liefert die ControlSpec zu einem Bezeichner, sonst ControlNichtGefunden."""
        wert = getattr(self, name, None)
        if not isinstance(wert, ControlSpec):
            raise ControlNichtGefunden(f"Unbekannter Control-Bezeichner '{name}' in LexwareControls")
        return wert

    def titel(self, name: str) -> str:
        """Lesbare Bezeichnung für Logs (Titel oder Bezeichner)."""
        try:
            s = self.spec(name)
        except ControlNichtGefunden:
            return name
        return s.title or s.title_re or s.auto_id or name

    def ist_control(self, name: str) -> bool:
        return isinstance(getattr(self, name, None), ControlSpec)


def standard_controls_datei() -> Path:
    return projekt_stamm() / "config" / "lexware_controls.yaml"


def _feld_aus_yaml(name: str, default: Any, wert: Any) -> Any:
    if isinstance(default, ControlSpec):
        if isinstance(wert, str):
            return ControlSpec(title=wert)
        if isinstance(wert, dict):
            unbekannt = set(wert) - _CONTROLSPEC_FELDER
            if unbekannt:
                raise ValueError(f"{name}: unbekannte Schlüssel {sorted(unbekannt)} (erlaubt: {sorted(_CONTROLSPEC_FELDER)})")
            daten = {k: (int(v) if k == "found_index" and v is not None else (None if v is None else str(v))) for k, v in wert.items()}
            return ControlSpec(**daten)
        raise ValueError(f"{name}: erwartet Text oder Mapping, nicht {type(wert).__name__}")
    if isinstance(default, tuple):
        if isinstance(wert, str):
            return tuple(t.strip() for t in re.split(r"\s*(?:->|>|/)\s*", wert) if t.strip())
        if isinstance(wert, list | tuple):
            return tuple(str(t) for t in wert)
        raise ValueError(f"{name}: erwartet Liste, nicht {type(wert).__name__}")
    if isinstance(default, str):
        if wert is None:
            raise ValueError(f"{name}: darf nicht leer sein")
        return str(wert)
    return wert


def lade_controls(pfad: Path | None = None) -> LexwareControls:
    """Lädt Control-Bezeichner aus einer YAML-Datei; fehlende Einträge bleiben Standard.

    Format je Eintrag: ``NAME: "Titel"`` oder ``NAME: {title:, title_re:, auto_id:, control_type:,
    class_name:, found_index:}``; Menüpfade als Liste; Regexe/Spaltennamen als Text.
    Unbekannte Schlüssel werden mit Warnung ignoriert; ``pfad=None`` oder fehlende Datei liefert
    die Standardwerte.
    """
    controls = LexwareControls()
    if pfad is None:
        return controls
    pfad = Path(pfad)
    if not pfad.exists():
        logger.warning("Control-Datei %s nicht gefunden, verwende Standardwerte", pfad)
        return controls
    roh = yaml.safe_load(pfad.read_text(encoding="utf-8")) or {}
    if not isinstance(roh, dict):
        raise ValueError(f"{pfad}: erwartet ein Mapping NAME -> Wert")
    daten = roh.get("controls", roh)
    if not isinstance(daten, dict):
        raise ValueError(f"{pfad}: 'controls' muss ein Mapping sein")
    bekannt = {f.name for f in fields(LexwareControls)}
    for name, wert in daten.items():
        if name not in bekannt:
            logger.warning("%s: unbekannter Control-Bezeichner '%s' wird ignoriert", pfad, name)
            continue
        setattr(controls, name, _feld_aus_yaml(name, getattr(controls, name), wert))
    logger.info("Control-Bezeichner aus %s geladen (%d Einträge)", pfad, len(daten))
    return controls


# ---------------------------------------------------------------------------
# Backend-Protokoll
# ---------------------------------------------------------------------------


@runtime_checkable
class LexwareBackend(Protocol):
    """Kleine Operationen auf der Lexware-Oberfläche; ``control`` ist immer ein Bezeichner
    aus ``LexwareControls`` (z. B. "WEITER"), nie ein Pixel oder roher Titel."""

    controls: LexwareControls

    def prozess_laeuft(self) -> bool: ...

    def starte(self, exe_pfad: Path) -> None: ...

    def finde_hauptfenster(self, timeout: float | None = None) -> Fenster: ...

    def aktive_firma(self) -> str: ...

    def oeffne_menue(self, pfad: list[str] | tuple[str, ...]) -> None: ...

    def sende_tasten(self, fenster: Fenster, tasten: str) -> None: ...

    def finde_dialog(self, titel_regex: str, timeout: float | None = None) -> Fenster: ...

    def existiert_dialog(self, titel_regex: str) -> bool: ...

    def offene_dialoge(self) -> list[Fenster]: ...

    def aktiver_assistent_schritt(self, dialog: Fenster) -> str: ...

    def klicke(self, dialog: Fenster, control: str) -> None: ...

    def setze_text(self, dialog: Fenster, control: str, text: str, *, einzeln_tippen: bool = False) -> None: ...

    def lese_text(self, dialog: Fenster, control: str) -> str: ...

    def waehle(self, dialog: Fenster, control: str, wert: str) -> None: ...

    def lese_liste(self, dialog: Fenster, control: str) -> list[dict[str, str]]: ...

    def waehle_listeneintrag(self, dialog: Fenster, control: str, suchtext: str) -> None: ...

    def warte_bis(self, praedikat: Callable[[], bool], timeout: float | None = None, beschreibung: str = "") -> bool: ...

    def screenshot(self, pfad: Path) -> Path | None: ...


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------


class AssistentSchritt(StrEnum):
    KOPF = "Kopfdaten"
    POSITIONEN = "Positionen"
    SUMMEN = "Summen"
    UNBEKANNT = "unbekannt"


def erkenne_schritt(text: str | None, controls: LexwareControls | None = None) -> AssistentSchritt:
    """Ordnet den Text der aktiven Assistentenseite einem Schritt zu."""
    if not text:
        return AssistentSchritt.UNBEKANNT
    c = controls or LexwareControls()
    t = text.strip()
    if re.search(c.SCHRITT_POSITIONEN_REGEX, t, re.I):
        return AssistentSchritt.POSITIONEN
    if re.search(c.SCHRITT_SUMMEN_REGEX, t, re.I):
        return AssistentSchritt.SUMMEN
    if re.search(c.SCHRITT_KOPF_REGEX, t, re.I):
        return AssistentSchritt.KOPF
    return AssistentSchritt.UNBEKANNT


_BETRAG_ZEICHEN = re.compile(r"[^\d,.\-]")


def parse_betrag(text: str | None) -> Decimal | None:
    """Liest einen deutschen Betrag ("1.234,50 €", "4,20", "-12,00") als Decimal; "1234.50" ebenfalls."""
    if text is None:
        return None
    t = _BETRAG_ZEICHEN.sub("", str(text)).strip()
    if not t or t in {"-", ",", "."}:
        return None
    if "," in t:
        t = t.replace(".", "").replace(",", ".")
    elif t.count(".") > 1:
        t = t.replace(".", "")
    elif "." in t and len(t.split(".")[-1]) == 3:
        t = t.replace(".", "")  # "1.234" = Tausendertrennung
    try:
        return Decimal(t)
    except InvalidOperation:
        return None


def formatiere_menge(menge: Decimal) -> str:
    """Menge als zu tippender Text: "12", "0,5" (deutsches Komma, keine Nachnullen)."""
    m = Decimal(menge).normalize()
    if m == m.to_integral_value():
        return str(int(m))
    return format(m, "f").replace(".", ",")


def formatiere_betrag(wert: Decimal) -> str:
    q = Decimal(wert).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    ganz, _, nach = f"{q:.2f}".partition(".")
    neg = ganz.startswith("-")
    ganz = ganz.lstrip("-")
    gruppen = []
    while len(ganz) > 3:
        gruppen.insert(0, ganz[-3:])
        ganz = ganz[:-3]
    gruppen.insert(0, ganz)
    return ("-" if neg else "") + ".".join(gruppen) + "," + nach


def _spalte(zeile: dict[str, str], *namen: str) -> str:
    """Liest eine Spalte einer Listenzeile unabhängig von Groß-/Kleinschreibung."""
    gesucht = [n.casefold() for n in namen]
    for schluessel, wert in zeile.items():
        if schluessel.casefold() in gesucht:
            return str(wert or "").strip()
    return ""


def _nur_ziffern(text: str) -> str:
    return re.sub(r"\D", "", text or "")


# ---------------------------------------------------------------------------
# pywinauto-Backend (nur Windows)
# ---------------------------------------------------------------------------

_TYPE_KEYS_SONDERZEICHEN = set("+^%~{}()[]")


def _escape_type_keys(text: str) -> str:
    """Maskiert Sonderzeichen für pywinauto ``type_keys`` (ein Zeichen pro Aufruf)."""
    return "".join("{" + z + "}" if z in _TYPE_KEYS_SONDERZEICHEN else z for z in text)


class PywinautoBackend:
    """Bedient Lexware über pywinauto (Standard: ``backend="uia"``, Fallback ``"win32"``).

    pywinauto wird erst im Konstruktor importiert; außerhalb von Windows wird ``NichtAufWindows``
    geworfen, damit das Modul überall importierbar bleibt. Jede Aktion wird geloggt; bei Fehlern
    legt ``UiSchreiber`` Screenshots in ``settings.lexware.screenshots_ordner`` ab.
    Bitness: 32-Bit-Python empfohlen (Programm unter Program Files (x86)); UIA läuft vermutlich
    auch gemischt.  # TODO-LEXWARE: Backend nach print_control_identifiers() festlegen.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        controls: LexwareControls | None = None,
        backend_name: str | None = None,
    ) -> None:
        if sys.platform != "win32":
            raise NichtAufWindows(
                f"Das pywinauto-Backend läuft nur unter Windows (hier: {sys.platform}). "
                "Für Tests und Dry Run unter Linux: fake_backend.FakeLexwareBackend verwenden."
            )
        try:
            import pywinauto  # noqa: F401
            from pywinauto import Application, Desktop, findwindows, timings
        except ImportError as exc:  # pragma: no cover - nur auf Windows ohne Paket
            raise BackendNichtVerfuegbar(
                "pywinauto ist nicht installiert: pip install 'auftragsautomat[windows]'"
            ) from exc
        self._Application = Application
        self._Desktop = Desktop
        self._findwindows = findwindows
        self._timings = timings
        self.settings = settings
        lx = settings.lexware
        self.backend_name = backend_name or str(lx.backend or "uia")
        if controls is not None:
            self.controls = controls
        else:
            datei = lx.controls_datei
            pfad = Path(datei) if datei else standard_controls_datei()
            self.controls = lade_controls(pfad if pfad.exists() else None)
        if lx.fenster_titel_regex and self.controls.HAUPTFENSTER_TITEL_REGEX == LexwareControls().HAUPTFENSTER_TITEL_REGEX:
            self.controls.HAUPTFENSTER_TITEL_REGEX = lx.fenster_titel_regex
        self.wartezeit = max(float(lx.wartezeit_sekunden), 0.05)
        self.max_wartezeit = float(lx.maximale_wartezeit_sekunden)
        self.tipp_pause = 0.05
        self._app: Any = None
        logger.info("PywinautoBackend initialisiert (backend=%s, Prozess=%s)", self.backend_name, self.controls.PROZESSNAME)

    # ----------------------------------------------------------------- Prozess

    def prozess_laeuft(self) -> bool:
        return instanz.lexware_laeuft(self.controls.PROZESSNAME)

    def starte(self, exe_pfad: Path) -> None:
        logger.warning("Starte Lexware: %s (nur weil kein %s läuft)", exe_pfad, self.controls.PROZESSNAME)
        self._app = self._Application(backend=self.backend_name).start(str(exe_pfad))

    def _verbinde(self) -> Any:
        if self._app is not None:
            try:
                if self._app.is_process_running():
                    return self._app
            except Exception:  # noqa: BLE001 - pywinauto wirft diverse Fehler
                self._app = None
        app = self._Application(backend=self.backend_name)
        try:
            app.connect(path=self.controls.PROZESSNAME, timeout=self.max_wartezeit)
        except Exception as exc:  # noqa: BLE001
            logger.info("connect(path=%s) fehlgeschlagen (%s), versuche Titel", self.controls.PROZESSNAME, exc)
            try:
                app.connect(title_re=self.controls.HAUPTFENSTER_TITEL_REGEX, timeout=self.max_wartezeit)
            except Exception as exc2:  # noqa: BLE001
                raise FensterNichtGefunden(f"Keine laufende Lexware-Instanz gefunden: {exc2}") from exc2
        self._app = app
        return app

    def _timeout(self, timeout: float | None) -> float:
        return self.max_wartezeit if timeout is None else float(timeout)

    # ----------------------------------------------------------------- Fenster

    def finde_hauptfenster(self, timeout: float | None = None) -> Fenster:
        app = self._verbinde()
        spec = app.window(title_re=self.controls.HAUPTFENSTER_TITEL_REGEX)
        try:
            spec.wait("exists visible", timeout=self._timeout(timeout))
        except Exception as exc:  # noqa: BLE001
            raise FensterNichtGefunden(
                f"Hauptfenster (Titel ~ {self.controls.HAUPTFENSTER_TITEL_REGEX!r}) nicht gefunden: {exc}"
            ) from exc
        wrapper = spec.wrapper_object()
        titel = wrapper.window_text()
        logger.debug("Hauptfenster: %r", titel)
        return Fenster(titel=titel, roh=wrapper, art="haupt")

    def aktive_firma(self) -> str:
        # [V] Die aktive Firma steht in der Titelleiste. TODO-LEXWARE: Titelformat prüfen.
        return self.finde_hauptfenster().titel

    def oeffne_menue(self, pfad: list[str] | tuple[str, ...]) -> None:
        haupt = self.finde_hauptfenster()
        text = " -> ".join(pfad)
        logger.info("Menü: %s", text)
        try:
            haupt.roh.set_focus()
            haupt.roh.menu_select(text)
            return
        except Exception as exc:  # noqa: BLE001
            logger.debug("menu_select(%s) fehlgeschlagen (%s), klicke Einträge einzeln", text, exc)
        # Fallback: Menüeinträge einzeln (UIA-MenuItem) anklicken
        for eintrag in pfad:
            try:
                item = haupt.roh.child_window(title=eintrag, control_type="MenuItem")
                item.wait("exists visible enabled", timeout=self.max_wartezeit)
                item.click_input()
            except Exception as exc:  # noqa: BLE001
                raise ControlNichtGefunden(f"Menüeintrag '{eintrag}' (Pfad {text}) nicht gefunden: {exc}") from exc
            time.sleep(self.wartezeit)

    def sende_tasten(self, fenster: Fenster, tasten: str) -> None:
        logger.info("Tasten %r an %s", tasten, fenster)
        fenster.roh.set_focus()
        fenster.roh.type_keys(tasten, set_foreground=True, pause=self.tipp_pause)

    def _hauptfenster_handle(self) -> int | None:
        try:
            return int(self.finde_hauptfenster(timeout=self.wartezeit).roh.handle)
        except Exception:  # noqa: BLE001
            return None

    def _fenster_kandidaten(self, titel_regex: str) -> list[Any]:
        """Sichtbare Fenster mit passendem Titel, ohne das Hauptfenster (dessen Titel kann z. B.
        'Aufträge' enthalten und würde sonst den Assistenten-Regex treffen)."""
        app = self._verbinde()
        haupt = self._hauptfenster_handle()
        gefunden: list[Any] = []
        for quelle in (lambda: app.windows(title_re=titel_regex, visible_only=True),
                       lambda: self._Desktop(backend=self.backend_name).windows(title_re=titel_regex, visible_only=True)):
            try:
                for w in quelle():
                    if haupt is not None and int(w.handle) == haupt:
                        continue
                    gefunden.append(w)
            except Exception as exc:  # noqa: BLE001
                logger.debug("Fenstersuche %r: %s", titel_regex, exc)
            if gefunden:
                break
        return gefunden

    def finde_dialog(self, titel_regex: str, timeout: float | None = None) -> Fenster:
        frist = time.monotonic() + self._timeout(timeout)
        while True:
            kandidaten = self._fenster_kandidaten(titel_regex)
            if kandidaten:
                wrapper = kandidaten[0]
                titel = wrapper.window_text()
                art = "assistent" if re.search(self.controls.ASSISTENT_TITEL_REGEX, titel, re.I) else "dialog"
                logger.debug("Dialog gefunden: %r (%s)", titel, art)
                return Fenster(titel=titel, roh=wrapper, art=art)
            if time.monotonic() >= frist:
                raise FensterNichtGefunden(f"Fenster mit Titel ~ {titel_regex!r} nicht gefunden")
            time.sleep(self.wartezeit)

    def existiert_dialog(self, titel_regex: str) -> bool:
        return bool(self._fenster_kandidaten(titel_regex))

    def offene_dialoge(self) -> list[Fenster]:
        """Alle sichtbaren Top-Level-Fenster des Lexware-Prozesses außer Hauptfenster und Assistent."""
        app = self._verbinde()
        ergebnis: list[Fenster] = []
        try:
            elemente = self._findwindows.find_elements(
                process=app.process, top_level_only=True, visible_only=True, backend=self.backend_name
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("find_elements fehlgeschlagen: %s", exc)
            return ergebnis
        for element in elemente:
            titel = element.name or ""
            if re.search(self.controls.HAUPTFENSTER_TITEL_REGEX, titel, re.I) and not re.search(
                r"(?i)Lexware.*(Hinweis|Fehler|Meldung|Warnung)", titel
            ):
                continue
            if re.search(self.controls.ASSISTENT_TITEL_REGEX, titel, re.I):
                continue
            try:
                wrapper = app.window(handle=element.handle).wrapper_object()
            except Exception:  # noqa: BLE001
                continue
            ergebnis.append(Fenster(titel=titel, roh=wrapper, art="dialog"))
        return ergebnis

    # ----------------------------------------------------------------- Controls

    def _element(self, fenster: Fenster, control: str, *, timeout: float | None = None) -> Any:
        spec = self.controls.spec(control)
        try:
            kind = fenster.roh.child_window(**spec.pywinauto_kwargs())
            kind.wait("exists", timeout=self._timeout(timeout))
            return kind.wrapper_object()
        except Exception as exc:  # noqa: BLE001
            raise ControlNichtGefunden(
                f"Control {control} ({spec.beschreibung()}) in {fenster} nicht gefunden: {exc}"
            ) from exc

    def aktiver_assistent_schritt(self, dialog: Fenster) -> str:
        """Name der aktiven Seite laut Seitenliste links (blau markiert = selektiert).

        TODO-LEXWARE: prüfen, ob UIA die Seitenliste als List/Tree/Tab liefert und ob die aktive
        Seite als 'selected' erscheint. Fallback: sichtbare Überschrift mit Schrittname.
        """
        try:
            liste = self._element(dialog, "SEITENLISTE", timeout=self.wartezeit)
        except ControlNichtGefunden:
            liste = None
        if liste is not None:
            for item in liste.children():
                try:
                    if item.is_selected():
                        return item.window_text()
                except Exception:  # noqa: BLE001
                    continue
            # Fallback: Legacy-State "selected"/"focused"
            for item in liste.children():
                try:
                    zustand = int(item.legacy_properties().get("State", 0))
                except Exception:  # noqa: BLE001
                    continue
                if zustand & 0x2 or zustand & 0x4:  # STATE_SYSTEM_SELECTED / FOCUSED
                    return item.window_text()
        muster = [
            self.controls.SCHRITT_SUMMEN_REGEX,
            self.controls.SCHRITT_POSITIONEN_REGEX,
            self.controls.SCHRITT_KOPF_REGEX,
        ]
        try:
            for text_control in dialog.roh.descendants(control_type="Text"):
                text = text_control.window_text()
                if any(re.search(m, text, re.I) for m in muster) and text_control.is_visible():
                    return text
        except Exception as exc:  # noqa: BLE001
            logger.debug("Schritterkennung über Texte fehlgeschlagen: %s", exc)
        return ""

    def klicke(self, dialog: Fenster, control: str) -> None:
        element = self._element(dialog, control)
        logger.info("Klick auf %s in %s", self.controls.titel(control), dialog)
        try:
            element.set_focus()
        except Exception:  # noqa: BLE001
            pass
        # Genau EIN Klick: "Optimierte Mausbedienung" erhöht bei Mehrfachklick die Positionsmenge.
        try:
            element.click_input()
        except Exception as exc:  # noqa: BLE001
            logger.debug("click_input fehlgeschlagen (%s), versuche invoke()", exc)
            try:
                element.invoke()
            except Exception as exc2:  # noqa: BLE001
                raise ControlNichtGefunden(f"{control} kann nicht geklickt werden: {exc2}") from exc2
        time.sleep(self.wartezeit)

    def setze_text(self, dialog: Fenster, control: str, text: str, *, einzeln_tippen: bool = False) -> None:
        element = self._element(dialog, control)
        logger.info("Eingabe in %s: %r (%s)", self.controls.titel(control), text, "einzeln getippt" if einzeln_tippen else "gesetzt")
        element.set_focus()
        try:
            element.click_input()  # Fokus sicher ins Feld (ein Klick)
        except Exception:  # noqa: BLE001
            pass
        # Inhalt leeren: Strg+A, Entf (AutoSuggest-Felder reagieren nur auf Tastenereignisse)
        element.type_keys("^a{DELETE}", pause=self.tipp_pause, set_foreground=True)
        if einzeln_tippen:
            for zeichen in text:
                element.type_keys(_escape_type_keys(zeichen), pause=self.tipp_pause, with_spaces=True, set_foreground=True)
        else:
            gesetzt = False
            for methode in ("set_edit_text", "set_text"):
                fn = getattr(element, methode, None)
                if fn is None:
                    continue
                try:
                    fn(text)
                    gesetzt = True
                    break
                except Exception:  # noqa: BLE001
                    continue
            if not gesetzt:
                element.type_keys(_escape_type_keys(text), pause=self.tipp_pause, with_spaces=True, set_foreground=True)
        time.sleep(self.wartezeit)

    def lese_text(self, dialog: Fenster, control: str) -> str:
        if control == "DIALOG_TEXT":
            return self._dialog_text(dialog)
        element = self._element(dialog, control)
        for methode in ("get_value", "selected_text", "window_text"):
            fn = getattr(element, methode, None)
            if fn is None:
                continue
            try:
                wert = fn()
            except Exception:  # noqa: BLE001
                continue
            if wert:
                return str(wert)
        try:
            return " ".join(t for t in element.texts() if t).strip()
        except Exception:  # noqa: BLE001
            return ""

    def _dialog_text(self, dialog: Fenster) -> str:
        texte: list[str] = []
        try:
            for element in dialog.roh.descendants(control_type="Text"):
                t = element.window_text().strip()
                if t:
                    texte.append(t)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Dialogtext nicht lesbar: %s", exc)
        if not texte:
            try:
                texte = [t for t in dialog.roh.texts() if t]
            except Exception:  # noqa: BLE001
                pass
        return "\n".join(texte)

    def waehle(self, dialog: Fenster, control: str, wert: str) -> None:
        element = self._element(dialog, control)
        logger.info("Auswahl in %s: %r", self.controls.titel(control), wert)
        try:
            element.select(wert)
        except Exception as exc:  # noqa: BLE001
            logger.debug("select(%r) fehlgeschlagen (%s), öffne Liste", wert, exc)
            try:
                element.expand()
                eintrag = element.child_window(title=wert, control_type="ListItem")
                eintrag.wait("exists visible", timeout=self.max_wartezeit)
                eintrag.click_input()
            except Exception as exc2:  # noqa: BLE001
                raise ControlNichtGefunden(f"Eintrag {wert!r} in {control} nicht wählbar: {exc2}") from exc2
        time.sleep(self.wartezeit)

    def lese_liste(self, dialog: Fenster, control: str) -> list[dict[str, str]]:
        """Liest eine Tabelle (DataGrid/List/Table) als Zeilen {Spaltenüberschrift: Text}.

        TODO-LEXWARE: Ob UIA die Positionsliste und die Auftragslisten mit Zellen liefert, ist
        offen (Risiko: natives Grid). Fallback dann: Export nach Excel (Strg+F6) auswerten.
        """
        element = self._element(dialog, control)
        ueberschriften: list[str] = []
        try:
            for header in element.descendants(control_type="HeaderItem") or element.descendants(control_type="Header"):
                t = header.window_text().strip()
                if t:
                    ueberschriften.append(t)
        except Exception:  # noqa: BLE001
            pass
        zeilen: list[dict[str, str]] = []
        try:
            kandidaten = element.children(control_type="DataItem") or element.children(control_type="ListItem")
        except Exception:  # noqa: BLE001
            kandidaten = []
        for zeile in kandidaten:
            try:
                zellen = [z.window_text().strip() for z in zeile.children()]
                if not zellen:
                    zellen = [t.strip() for t in zeile.texts()]
            except Exception:  # noqa: BLE001
                continue
            if ueberschriften and len(zellen) >= len(ueberschriften):
                zeilen.append(dict(zip(ueberschriften, zellen, strict=False)))
            else:
                zeilen.append({f"spalte_{i}": z for i, z in enumerate(zellen)})
        logger.debug("Liste %s: %d Zeilen, Spalten %s", control, len(zeilen), ueberschriften)
        return zeilen

    def waehle_listeneintrag(self, dialog: Fenster, control: str, suchtext: str) -> None:
        element = self._element(dialog, control)
        logger.info("Markiere Eintrag %r in %s", suchtext, self.controls.titel(control))
        try:
            kandidaten = element.children(control_type="DataItem") or element.children(control_type="ListItem")
        except Exception:  # noqa: BLE001
            kandidaten = []
        for zeile in kandidaten:
            try:
                texte = [zeile.window_text()] + [z.window_text() for z in zeile.children()]
            except Exception:  # noqa: BLE001
                continue
            if any(suchtext == t.strip() for t in texte):
                try:
                    zeile.select()
                except Exception:  # noqa: BLE001
                    zeile.click_input()  # EIN Klick (Enter/Doppelklick würde 'bearbeiten' auslösen)
                time.sleep(self.wartezeit)
                return
        raise ControlNichtGefunden(f"Eintrag {suchtext!r} in {control} nicht gefunden")

    # ----------------------------------------------------------------- Warten / Screenshot

    def warte_bis(self, praedikat: Callable[[], bool], timeout: float | None = None, beschreibung: str = "") -> bool:
        frist = time.monotonic() + self._timeout(timeout)
        while True:
            try:
                if praedikat():
                    logger.debug("Bedingung erfüllt: %s", beschreibung)
                    return True
            except Exception as exc:  # noqa: BLE001
                logger.debug("Prädikat '%s' warf %s, weiter warten", beschreibung, exc)
            if time.monotonic() >= frist:
                logger.warning("Zeitüberschreitung (%.1fs) beim Warten auf: %s", self._timeout(timeout), beschreibung)
                return False
            time.sleep(self.wartezeit)

    def screenshot(self, pfad: Path) -> Path | None:
        pfad = Path(pfad)
        pfad.parent.mkdir(parents=True, exist_ok=True)
        try:
            from PIL import ImageGrab

            ImageGrab.grab().save(pfad)
            logger.info("Screenshot gespeichert: %s", pfad)
            return pfad
        except Exception as exc:  # noqa: BLE001
            logger.warning("Screenshot nicht möglich (%s); schreibe Fensterliste", exc)
        try:
            app = self._verbinde()
            zeilen = [f"{w.window_text()!r}" for w in app.windows()]
            txt = pfad.with_suffix(".txt")
            txt.write_text("\n".join(zeilen), encoding="utf-8")
            return txt
        except Exception as exc2:  # noqa: BLE001
            logger.error("Auch Fensterliste nicht möglich: %s", exc2)
            return None


# ---------------------------------------------------------------------------
# Ablauflogik
# ---------------------------------------------------------------------------


@dataclass
class _Positionsergebnis:
    artikelnummer: str
    menge: Decimal
    preis_gelesen: Decimal | None
    warnungen: list[str] = field(default_factory=list)


class UiSchreiber:
    """Schreibt Belege über den Lexware-Auftragsassistenten (implementiert ``lexware.basis.LexwareSchreiber``).

    * ``schreibe_einkauf_bestellung``: Strg+M -> Auftragsart "Bestellung" -> Lieferant, Freifelder,
      Lieferdatum -> Positionen (Artikelnummer, Menge einzeln getippt, grünes Häkchen, Kontrolle in
      der Positionsliste, EK gegen Stamm) -> Summen (Netto gegen ``paket.summe_netto_ek``) ->
      Dry Run: Abbrechen, scharf: Speichern + Dialoge + Belegnummer aus der Einkaufsauftragsliste.
    * ``schreibe_verkauf_ab``: dasselbe mit Strg+N, Auftragsart "Auftragsbestätigung", Kundennummer,
      Bestellnummer, VK Preisgruppe 1 (nur kontrollieren), Summe gegen ``paket.summe_netto_vk``.
    * ``auftrag_weiterfuehren_lieferschein``: AB in der Verkaufsauftragsliste markieren ->
      Bearbeiten -> Aufträge -> Weiterführen -> Auftragsart "Lieferschein" -> Assistent -> Dry Run/Speichern.

    Ohne ``backend`` wird ``PywinautoBackend`` erzeugt (unter Linux: ``NichtAufWindows``).
    """

    def __init__(
        self,
        settings: Settings,
        firma: Firma,
        backend: LexwareBackend | None = None,
        *,
        controls: LexwareControls | None = None,
        firmenwechsel_erlaubt: bool | None = None,
        heute: Callable[[], date] | None = None,
    ) -> None:
        self.settings = settings
        self.firma = firma
        self.backend: LexwareBackend = backend if backend is not None else PywinautoBackend(settings, controls=controls)
        if controls is not None:
            self.backend.controls = controls
        self.controls: LexwareControls = self.backend.controls
        if firmenwechsel_erlaubt is None:
            firmenwechsel_erlaubt = bool(settings.lexware.firmenwechsel_erlaubt)
        self.firmenwechsel_erlaubt = firmenwechsel_erlaubt
        self._heute = heute or date.today
        self.ablaufprotokoll: list[str] = []
        self._warnungen: list[str] = []
        self._speichern_geklickt = False

    # ----------------------------------------------------------------- Protokoll

    def _protokoll(self, text: str) -> None:
        logger.info("Ablauf: %s", text)
        self.ablaufprotokoll.append(text)

    def _warnung(self, text: str) -> None:
        logger.warning("Lexware-UI: %s", text)
        self._warnungen.append(text)

    def _neuer_lauf(self) -> None:
        self.ablaufprotokoll = []
        self._warnungen = []
        self._speichern_geklickt = False

    def _screenshots_ordner(self) -> Path:
        ordner = self.settings.lexware.screenshots_ordner or (self.settings.log_ordner / "screenshots")
        ordner = Path(ordner)
        ordner.mkdir(parents=True, exist_ok=True)
        return ordner

    def _screenshot(self, name: str) -> Path | None:
        zeit = datetime.now().strftime("%Y%m%d_%H%M%S")
        pfad = self._screenshots_ordner() / f"{zeit}_{self.firma.id}_{name}.png"
        try:
            return self.backend.screenshot(pfad)
        except Exception as exc:  # noqa: BLE001
            logger.error("Screenshot fehlgeschlagen: %s", exc)
            return None

    def _ergebnis(
        self,
        belegart: Belegart,
        *,
        dry_run: bool,
        erfolgreich: bool,
        belegnummer: str | None = None,
        netto: Decimal | None = None,
        brutto: Decimal | None = None,
        fehler: str | None = None,
    ) -> Belegergebnis:
        warnungen = list(self._warnungen) + [f"Ablauf: {z}" for z in self.ablaufprotokoll]
        return Belegergebnis(
            belegart=belegart,
            belegnummer=belegnummer,
            netto=netto,
            brutto=brutto,
            erfolgreich=erfolgreich,
            dry_run=dry_run,
            methode="ui_automation",
            fehler=fehler,
            warnungen=warnungen,
        )

    def _fehlschlag(self, belegart: Belegart, dry_run: bool, exc: BaseException, name: str) -> Belegergebnis:
        text = f"{type(exc).__name__}: {exc}"
        logger.error("Lexware-UI %s (%s) fehlgeschlagen: %s", belegart.value, "Dry Run" if dry_run else "scharf", text)
        pfad = getattr(exc, "screenshot", None) or self._screenshot(name)
        if pfad is not None:
            self._warnung(f"Screenshot: {pfad}")
        if not self._speichern_geklickt and not isinstance(exc, DialogFehler):
            self._sicher_abbrechen()
        else:
            self._warnung("Lexware-Oberfläche wurde im aktuellen Zustand belassen, bitte manuell prüfen")
        return self._ergebnis(belegart, dry_run=dry_run, erfolgreich=False, fehler=text)

    def _sicher_abbrechen(self) -> None:
        """Bestmöglich den offenen Assistenten verlassen, ohne zu speichern (nur vor 'Speichern')."""
        try:
            if not self.backend.existiert_dialog(self.controls.ASSISTENT_TITEL_REGEX):
                return
            assistent = self.backend.finde_dialog(self.controls.ASSISTENT_TITEL_REGEX, timeout=1)
            self._abbrechen(assistent)
        except Exception as exc:  # noqa: BLE001
            self._warnung(f"Assistent konnte nicht automatisch abgebrochen werden ({exc}); bitte manuell prüfen")

    # ----------------------------------------------------------------- Prüfungen

    def _pruefe_paket(self, paket: Belegpaket, *, einkauf: bool, dry_run: bool) -> list[PositionsZuordnung]:
        if paket.blockiert:
            raise AblaufFehler("Belegpaket ist blockiert (Prüfhinweise mit Schweregrad FEHLER)")
        if not dry_run and not paket.freigegeben:
            raise AblaufFehler("Scharfes Schreiben nur nach Freigabe (paket.freigegeben=False)")
        if paket.firma_id and paket.firma_id != self.firma.id:
            raise AblaufFehler(f"Belegpaket gehört zu Firma '{paket.firma_id}', Schreiber ist für '{self.firma.id}'")
        if einkauf and paket.lieferant.lieferant is None:
            raise AblaufFehler("Kein Lieferant zugeordnet")
        if not einkauf and paket.kunde.kunde is None:
            raise AblaufFehler("Kein Kunde zugeordnet")
        positionen = paket.aktive_positionen()
        if not positionen:
            raise AblaufFehler("Keine aktiven Positionen")
        for p in positionen:
            if p.artikel is None:
                raise AblaufFehler(f"Position {p.roh.laufnr} ohne Stammartikel ({p.roh.bezeichnung!r})")
            if p.menge is None or p.menge <= 0:
                raise AblaufFehler(f"Position {p.roh.laufnr} ({p.artikel.artikelnummer}) ohne Menge")
            preis = p.ek_preis if einkauf else p.vk_preis
            if preis is None:
                raise AblaufFehler(f"Position {p.roh.laufnr} ({p.artikel.artikelnummer}) ohne {'EK' if einkauf else 'VK'}-Preis")
        return positionen

    def _belegfelder(self, paket: Belegpaket, dry_run: bool) -> dict[str, str]:
        bf = paket.belegfelder
        kunde = paket.kunde.kunde
        lieferant = paket.lieferant.lieferant
        werte = {
            "FREIFELD_SPEDITION": bf.spedition or self.firma.belegfelder_standard.get("spedition", ""),
            "FREIFELD_WEINGUT": bf.weingut or (lieferant.name if lieferant else ""),
            "FREIFELD_SHIPMENT": bf.shipment or (paket.roh.liefertag.strftime(DATUMSFORMAT) if paket.roh.liefertag else ""),
            "FREIFELD_KUNDE": bf.kunde or (kunde.name if kunde else ""),
        }
        fehlend = [k.removeprefix("FREIFELD_").capitalize() for k, v in werte.items() if not v]
        if fehlend:
            text = f"Belegfelder leer: {', '.join(fehlend)} (Geschäftsregel: Spedition, Weingut, Shipment, Kunde immer füllen)"
            if dry_run:
                self._warnung(text)
            else:
                raise AblaufFehler(text)
        return werte

    # ----------------------------------------------------------------- Vorbereitung

    def _vorbereiten(self) -> Fenster:
        haupt = instanz.stelle_sicher_dass_lexware_laeuft(self.settings, self.backend, screenshot=self._screenshot)
        self._protokoll(f"Lexware läuft, Hauptfenster: {haupt.titel!r}")
        self._pruefe_firma()
        return haupt

    def _firma_passt(self, titel: str) -> bool:
        return self.firma.lexware_firmenname.casefold() in (titel or "").casefold()

    def _pruefe_firma(self) -> None:
        titel = instanz.aktive_firma(self.backend)
        if self._firma_passt(titel):
            self._protokoll(f"Aktive Firma geprüft: {self.firma.lexware_firmenname!r}")
            return
        if not self.firmenwechsel_erlaubt:
            raise FalscheFirma(
                f"Aktive Lexware-Firma laut Titelleiste {titel!r} ist nicht {self.firma.lexware_firmenname!r}; "
                "automatischer Firmenwechsel ist nicht erlaubt (firmenwechsel_erlaubt=False)"
            )
        self._wechsle_firma(titel)

    def _wechsle_firma(self, bisher: str) -> None:
        # [V] Datei -> Wechseln: Liste der Firmen, Auswahl; alle Fenster der alten Firma werden geschlossen.
        self._protokoll(f"Firmenwechsel von {bisher!r} nach {self.firma.lexware_firmenname!r}")
        self.backend.oeffne_menue(self.controls.MENUE_FIRMA_WECHSELN)
        dialog = self.backend.finde_dialog(self.controls.FIRMENWAHL_TITEL_REGEX)
        self.backend.waehle_listeneintrag(dialog, "FIRMENLISTE", self.firma.lexware_firmenname)
        self.backend.klicke(dialog, "DIALOG_OK")  # TODO-LEXWARE: Doppelklick laut Handbuch; OK-Button angenommen
        if not self.backend.warte_bis(lambda: self._firma_passt(self.backend.aktive_firma()), beschreibung="Firmenwechsel"):
            raise FalscheFirma(f"Firmenwechsel nach {self.firma.lexware_firmenname!r} nicht erfolgt (Titel: {self.backend.aktive_firma()!r})")
        self._behandle_dialoge(DialogKontext.START)
        self._protokoll("Firmenwechsel erfolgreich")

    def _behandle_dialoge(self, kontext: DialogKontext) -> None:
        for zeile in behandle_dialoge(self.backend, kontext, screenshot=self._screenshot):
            self._protokoll(zeile)

    # ----------------------------------------------------------------- Assistent

    def _oeffne_assistent(self, haupt: Fenster, *, einkauf: bool) -> Fenster:
        taste = self.controls.TASTE_NEUER_EINKAUFSAUFTRAG if einkauf else self.controls.TASTE_NEUER_VERKAUFSAUFTRAG
        regex = self.controls.ASSISTENT_EINKAUF_TITEL_REGEX if einkauf else self.controls.ASSISTENT_VERKAUF_TITEL_REGEX
        if self.backend.existiert_dialog(self.controls.ASSISTENT_TITEL_REGEX):
            raise AblaufFehler("Es ist bereits ein Auftragsassistent geöffnet; bitte schließen")
        self.backend.sende_tasten(haupt, taste)
        self._protokoll(f"Assistent {'Einkauf (Strg+M)' if einkauf else 'Verkauf (Strg+N)'} aufgerufen")
        assistent = self.backend.finde_dialog(regex)
        self._behandle_dialoge(DialogKontext.ASSISTENT)
        schritt = self._schritt(assistent)
        if schritt != AssistentSchritt.KOPF:
            raise AblaufFehler(f"Assistent {assistent.titel!r} startet nicht auf Seite 1 (erkannt: {schritt.value!r})")
        self._protokoll(f"Assistent geöffnet: {assistent.titel!r}, Schritt 1")
        return assistent

    def _schritt(self, assistent: Fenster) -> AssistentSchritt:
        return erkenne_schritt(self.backend.aktiver_assistent_schritt(assistent), self.controls)

    def _waehle_und_pruefe(self, fenster: Fenster, control: str, wert: str) -> None:
        self.backend.waehle(fenster, control, wert)
        gelesen = self.backend.lese_text(fenster, control)
        if gelesen.strip().casefold() != wert.casefold():
            raise AblaufFehler(f"{self.controls.titel(control)}: {wert!r} gewählt, Feld zeigt {gelesen!r}")
        self._protokoll(f"{self.controls.titel(control)} = {wert!r}")

    def _setze_pruefe(self, fenster: Fenster, control: str, wert: str, *, einzeln: bool = False, pflicht: bool = True) -> str:
        try:
            self.backend.setze_text(fenster, control, wert, einzeln_tippen=einzeln)
            gelesen = self.backend.lese_text(fenster, control)
        except ControlNichtGefunden as exc:
            if pflicht:
                raise
            self._warnung(f"Feld {self.controls.titel(control)} nicht gefunden, Wert {wert!r} nicht gesetzt ({exc})")
            return ""
        if gelesen.strip() != wert.strip():
            text = f"{self.controls.titel(control)}: {wert!r} eingegeben, Feld zeigt {gelesen!r}"
            if pflicht:
                raise AblaufFehler(text)
            self._warnung(text)
        else:
            self._protokoll(f"{self.controls.titel(control)} = {wert!r}")
        return gelesen

    def _setze_partner(self, assistent: Fenster, control: str, nummer: str, erwarteter_name: str) -> None:
        # [V] Nummer eingeben -> Stammdaten werden übernommen; AutoSuggest braucht Tastenereignisse.
        self.backend.setze_text(assistent, control, nummer, einzeln_tippen=True)
        self.backend.warte_bis(
            lambda: bool(self.backend.lese_text(assistent, "PARTNER_NAME").strip()),
            beschreibung="Übernahme der Partner-Stammdaten",
        )
        gelesen_nummer = self.backend.lese_text(assistent, control).strip()
        if gelesen_nummer != nummer:
            raise AblaufFehler(f"{self.controls.titel(control)}: {nummer!r} getippt, Feld zeigt {gelesen_nummer!r}")
        name = self.backend.lese_text(assistent, "PARTNER_NAME").strip()
        if not name:
            raise AblaufFehler(f"Partner {nummer} wurde von Lexware nicht übernommen (Name leer)")
        if erwarteter_name and erwarteter_name.casefold()[:10] not in name.casefold():
            self._warnung(f"Partner {nummer}: Lexware zeigt {name!r}, erwartet {erwarteter_name!r}")
        self._protokoll(f"{self.controls.titel(control)} = {nummer!r} ({name})")

    def _setze_datum(self, assistent: Fenster, control: str, tag: date | None) -> None:
        if tag is None:
            return
        text = tag.strftime(DATUMSFORMAT)
        try:
            self.backend.setze_text(assistent, control, text, einzeln_tippen=True)
            gelesen = self.backend.lese_text(assistent, control)
        except ControlNichtGefunden as exc:
            self._warnung(f"Feld {self.controls.titel(control)} nicht gefunden ({exc})")
            return
        if _nur_ziffern(gelesen) != _nur_ziffern(text):
            # TODO-LEXWARE: maskiertes Datumsfeld nimmt evtl. nur Ziffern an
            self.backend.setze_text(assistent, control, _nur_ziffern(text), einzeln_tippen=True)
            gelesen = self.backend.lese_text(assistent, control)
            if _nur_ziffern(gelesen) != _nur_ziffern(text):
                self._warnung(f"{self.controls.titel(control)}: {text!r} eingegeben, Feld zeigt {gelesen!r}")
                return
        self._protokoll(f"{self.controls.titel(control)} = {text!r}")

    def _setze_belegfelder(self, assistent: Fenster, werte: dict[str, str]) -> None:
        for control, wert in werte.items():
            if wert:
                self._setze_pruefe(assistent, control, wert, pflicht=False)

    def _weiter(self, assistent: Fenster, ziel: AssistentSchritt) -> None:
        """'Weiter' EINMAL klicken und auf den Zielschritt warten; erst nach Timeout erneut (max. 3)."""
        start = self._schritt(assistent)
        for versuch in range(1, MAX_WEITER_VERSUCHE + 1):
            self.backend.klicke(assistent, "WEITER")
            self._protokoll(f"'Weiter' geklickt (Versuch {versuch}), warte auf Schritt {ziel.value!r}")
            self.backend.warte_bis(
                lambda: self._schritt(assistent) == ziel or bool(self.backend.offene_dialoge()),
                beschreibung=f"Schritt {ziel.value}",
            )
            self._behandle_dialoge(DialogKontext.ASSISTENT)
            aktuell = self._schritt(assistent)
            if aktuell == AssistentSchritt.UNBEKANNT:
                # Seitenwechsel läuft evtl. noch (Datenbankzugriffe): nochmals warten, nicht sofort klicken
                self.backend.warte_bis(lambda: self._schritt(assistent) == ziel, beschreibung=f"Schritt {ziel.value} (Übergang)")
                aktuell = self._schritt(assistent)
            if aktuell == ziel:
                self._protokoll(f"Schritt {ziel.value!r} aktiv")
                return
            if aktuell not in (start, AssistentSchritt.UNBEKANNT):
                raise AblaufFehler(f"Assistent steht auf {aktuell.value!r}, erwartet {start.value!r} oder {ziel.value!r}")
            self._warnung(f"'Weiter' zeigte nach Wartezeit keine Wirkung (Versuch {versuch}), Schritt {aktuell.value!r}")
        raise AblaufFehler(f"Schritt {ziel.value!r} nach {MAX_WEITER_VERSUCHE} Versuchen nicht erreicht")

    # ----------------------------------------------------------------- Positionen

    def _tippe_menge(self, assistent: Fenster, menge: Decimal) -> str:
        text = formatiere_menge(menge)
        for versuch in (1, 2):
            self.backend.klicke(assistent, "MENGE")  # Feld markieren (ein Klick)
            self.backend.setze_text(assistent, "MENGE", text, einzeln_tippen=True)
            gelesen = self.backend.lese_text(assistent, "MENGE")
            if parse_betrag(gelesen) == Decimal(menge):
                return gelesen
            self._warnung(f"Menge: {text!r} getippt, Feld zeigt {gelesen!r} (Versuch {versuch})")
        raise AblaufFehler(f"Menge {text!r} wurde vom Mengenfeld nicht übernommen")

    def _erfasse_position(self, assistent: Fenster, p: PositionsZuordnung, *, einkauf: bool, laufende_nr: int) -> _Positionsergebnis:
        assert p.artikel is not None
        nummer = p.artikel.artikelnummer
        erwarteter_preis = p.ek_preis if einkauf else p.vk_preis
        self._waehle_und_pruefe(assistent, "POSITIONSART", self.controls.POSITIONSART_STAMMARTIKEL)
        try:
            self._waehle_und_pruefe(assistent, "SUCHKRITERIUM", self.controls.SUCHKRITERIUM_NUMMER)
        except ControlNichtGefunden as exc:
            self._warnung(f"Suchkriterium nicht wählbar ({exc}); Artikelnummer wird direkt getippt")
        # Artikel über die Nummer (nicht Textsuche), Ziffern einzeln wegen AutoSuggest
        self.backend.setze_text(assistent, "ARTIKELNUMMER", nummer, einzeln_tippen=True)
        uebernommen = self.backend.warte_bis(
            lambda: bool(self.backend.lese_text(assistent, "ARTIKEL_BEZEICHNUNG").strip()),
            beschreibung=f"Artikel {nummer} übernommen",
        )
        if not uebernommen:
            # TODO-LEXWARE: ob Enter nötig ist, um den AutoSuggest-Treffer zu übernehmen
            self.backend.sende_tasten(assistent, "{ENTER}")
            uebernommen = self.backend.warte_bis(
                lambda: bool(self.backend.lese_text(assistent, "ARTIKEL_BEZEICHNUNG").strip()),
                beschreibung=f"Artikel {nummer} übernommen (nach Enter)",
            )
        self._behandle_dialoge(DialogKontext.ASSISTENT)
        bezeichnung = self.backend.lese_text(assistent, "ARTIKEL_BEZEICHNUNG").strip()
        if not uebernommen or not bezeichnung:
            raise AblaufFehler(f"Artikel {nummer} wurde von Lexware nicht gefunden/übernommen")
        gelesen_nummer = self.backend.lese_text(assistent, "ARTIKELNUMMER").strip()
        if gelesen_nummer != nummer:
            raise AblaufFehler(f"Artikelnummer {nummer!r} getippt, Feld zeigt {gelesen_nummer!r}")
        self._protokoll(f"Position {laufende_nr}: Artikel {nummer} ({bezeichnung})")
        self._tippe_menge(assistent, p.menge)
        self._protokoll(f"Position {laufende_nr}: Menge {formatiere_menge(p.menge)} einzeln getippt und zurückgelesen")
        warnungen: list[str] = []
        preis_gelesen = parse_betrag(self.backend.lese_text(assistent, "PREIS"))
        if preis_gelesen is None:
            warnungen.append(f"Position {laufende_nr} ({nummer}): Preis im Assistenten nicht lesbar")
        elif erwarteter_preis is not None and abs(preis_gelesen - erwarteter_preis) > PREIS_TOLERANZ:
            warnungen.append(
                f"Position {laufende_nr} ({nummer}): {'EK' if einkauf else 'VK'} laut Lexware {formatiere_betrag(preis_gelesen)} "
                f"weicht von Stamm {formatiere_betrag(erwarteter_preis)} ab (Lexware-Preis wird nicht überschrieben)"
            )
        self.backend.klicke(assistent, "POSITION_UEBERNEHMEN")  # grünes Häkchen [V]
        self._behandle_dialoge(DialogKontext.ASSISTENT)
        self._pruefe_positionsliste(assistent, nummer, p.menge, laufende_nr)
        for w in warnungen:
            self._warnung(w)
        return _Positionsergebnis(nummer, p.menge, preis_gelesen, warnungen)

    def _pruefe_positionsliste(self, assistent: Fenster, nummer: str, menge: Decimal, laufende_nr: int) -> None:
        c = self.controls
        zeilen = self.backend.lese_liste(assistent, "POSITIONSLISTE")
        if len(zeilen) < laufende_nr:
            raise AblaufFehler(f"Positionsliste hat {len(zeilen)} Zeilen, erwartet mindestens {laufende_nr}")
        passend = [
            z for z in zeilen
            if _spalte(z, c.SPALTE_ARTIKELNUMMER, "Artikel-Nr.", "Nummer") == nummer
            and parse_betrag(_spalte(z, c.SPALTE_MENGE)) == Decimal(menge)
        ]
        if not passend:
            raise AblaufFehler(f"Position {nummer} mit Menge {formatiere_menge(menge)} nicht in der Positionsliste")
        self._protokoll(f"Position {laufende_nr} in der Positionsliste bestätigt ({nummer}, Menge {formatiere_menge(menge)})")

    def _erfasse_positionen(self, assistent: Fenster, positionen: list[PositionsZuordnung], *, einkauf: bool) -> None:
        for i, p in enumerate(positionen, start=1):
            self._erfasse_position(assistent, p, einkauf=einkauf, laufende_nr=i)
        zeilen = self.backend.lese_liste(assistent, "POSITIONSLISTE")
        if len(zeilen) != len(positionen):
            raise AblaufFehler(f"Positionsliste hat {len(zeilen)} Zeilen, erwartet {len(positionen)}")

    # ----------------------------------------------------------------- Summen / Abschluss

    def _lese_summen(self, assistent: Fenster) -> tuple[Decimal, Decimal | None]:
        netto = parse_betrag(self.backend.lese_text(assistent, "SUMME_NETTO"))
        if netto is None:
            raise AblaufFehler("Netto-Summe im Schritt 'Summen' nicht lesbar")
        try:
            brutto = parse_betrag(self.backend.lese_text(assistent, "SUMME_BRUTTO"))
        except ControlNichtGefunden:
            brutto = None
        if brutto is None:
            faktor = Decimal(1) + Decimal(self.firma.opentrans.mwst_satz) / Decimal(100)
            brutto = (netto * faktor).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            self._warnung(f"Brutto nicht lesbar, rechnerisch mit {self.firma.opentrans.mwst_satz} % ermittelt")
        self._protokoll(f"Summen gelesen: netto {formatiere_betrag(netto)}, brutto {formatiere_betrag(brutto)}")
        return netto, brutto

    def _pruefe_summe(self, netto: Decimal, erwartet: Decimal, bezeichnung: str) -> None:
        if abs(netto - erwartet) > SUMMEN_TOLERANZ:
            raise SummenAbweichung(
                f"Netto laut Lexware {formatiere_betrag(netto)} weicht von {bezeichnung} "
                f"{formatiere_betrag(erwartet)} ab (Toleranz {formatiere_betrag(SUMMEN_TOLERANZ)}); nicht gespeichert"
            )
        self._protokoll(f"Summenvergleich bestanden ({bezeichnung} {formatiere_betrag(erwartet)})")

    def _abbrechen(self, assistent: Fenster) -> None:
        self.backend.klicke(assistent, "ABBRECHEN")
        self._protokoll("'Abbrechen' geklickt (Dry Run: nichts gespeichert)")
        self._behandle_dialoge(DialogKontext.DRY_RUN_ABBRUCH)
        if not self.backend.warte_bis(
            lambda: not self.backend.existiert_dialog(self.controls.ASSISTENT_TITEL_REGEX), beschreibung="Assistent geschlossen"
        ):
            self._behandle_dialoge(DialogKontext.DRY_RUN_ABBRUCH)
            if self.backend.existiert_dialog(self.controls.ASSISTENT_TITEL_REGEX):
                raise AblaufFehler("Assistent ließ sich nach 'Abbrechen' nicht schließen")
        self._protokoll("Assistent geschlossen")

    def _speichern(self, assistent: Fenster) -> None:
        self._speichern_geklickt = True
        self.backend.klicke(assistent, "SPEICHERN")
        self._protokoll("'Speichern' geklickt")
        self._behandle_dialoge(DialogKontext.NACH_SPEICHERN)
        if not self.backend.warte_bis(
            lambda: not self.backend.existiert_dialog(self.controls.ASSISTENT_TITEL_REGEX) or bool(self.backend.offene_dialoge()),
            beschreibung="Assistent nach Speichern geschlossen",
        ):
            self._warnung("Assistent nach 'Speichern' noch offen (Option 'in der Erfassungsmaske verbleiben'?)")
        self._behandle_dialoge(DialogKontext.NACH_SPEICHERN)
        if self.backend.existiert_dialog(self.controls.ASSISTENT_TITEL_REGEX):
            # Dialoge können nacheinander erscheinen (Druck, dann USt-IdNr.): noch einmal warten und behandeln
            self.backend.warte_bis(
                lambda: not self.backend.existiert_dialog(self.controls.ASSISTENT_TITEL_REGEX) or bool(self.backend.offene_dialoge()),
                beschreibung="Assistent geschlossen (2. Runde)",
            )
            self._behandle_dialoge(DialogKontext.NACH_SPEICHERN)
        self._protokoll("Beleg gespeichert, nachgelagerte Dialoge behandelt")

    def _lese_belegnummer(self, haupt: Fenster, *, einkauf: bool, auftragsart: str, partnernummer: str) -> str | None:
        """Belegnummer aus der Auftragsliste (Auftragsart, Partner, Belegdatum = heute)."""
        c = self.controls
        liste = "AUFTRAGSLISTE_EINKAUF" if einkauf else "AUFTRAGSLISTE_VERKAUF"
        menue = c.MENUE_AUFTRAGSLISTE_EINKAUF if einkauf else c.MENUE_AUFTRAGSLISTE_VERKAUF
        partner_spalten = (c.SPALTE_LIEFERANTENNUMMER, "Lieferant") if einkauf else (c.SPALTE_KUNDENNUMMER, "Kunde")
        heute = self._heute().strftime(DATUMSFORMAT)

        def kandidaten() -> list[str]:
            try:
                zeilen = self.backend.lese_liste(haupt, liste)
            except ControlNichtGefunden as exc:
                self._warnung(f"Auftragsliste nicht lesbar: {exc}")
                return []
            treffer = []
            for z in zeilen:
                art = _spalte(z, c.SPALTE_AUFTRAGSART, "Art")
                partner = _spalte(z, *partner_spalten)
                datum = _spalte(z, c.SPALTE_DATUM, "Datum")
                nummer = _spalte(z, c.SPALTE_BELEGNUMMER, "Beleg-Nr.", "Nummer")
                if not nummer:
                    continue
                if art and auftragsart.casefold() not in art.casefold():
                    continue
                if partner and partner != partnernummer:
                    continue
                if datum and datum != heute:
                    continue
                treffer.append(nummer)
            return treffer

        nummern = kandidaten()
        if not nummern:
            try:
                self.backend.oeffne_menue(menue)
            except Exception as exc:  # noqa: BLE001
                self._warnung(f"Auftragsliste konnte nicht geöffnet werden: {exc}")
            nummern = kandidaten()
        if not nummern:
            self._warnung(
                f"Belegnummer der neuen {auftragsart} konnte nicht aus der Auftragsliste gelesen werden; "
                "bitte in Lexware prüfen (Beleg wurde gespeichert)"
            )
            return None
        if len(nummern) > 1:
            self._warnung(f"Mehrere {auftragsart}-Belege von heute für Partner {partnernummer}: {nummern}; nehme {nummern[-1]}")
        self._protokoll(f"Belegnummer gelesen: {nummern[-1]}")
        return nummern[-1]

    # ----------------------------------------------------------------- Öffentliche Abläufe

    def schreibe_einkauf_bestellung(self, paket: Belegpaket, *, dry_run: bool = True) -> Belegergebnis:
        """Bestellung im Einkauf anlegen (Lieferant = Weingut, Preise = EK aus dem Stamm, Feld Customer = Endkunde)."""
        self._neuer_lauf()
        modus = "Dry Run" if dry_run else "scharf"
        logger.info("UI Einkauf-Bestellung (%s): Firma %s, Quelldatei %s", modus, self.firma.id, paket.roh.quelldatei.name)
        try:
            positionen = self._pruefe_paket(paket, einkauf=True, dry_run=dry_run)
            lieferant = paket.lieferant.lieferant
            assert lieferant is not None
            belegfelder = self._belegfelder(paket, dry_run)
            haupt = self._vorbereiten()
            assistent = self._oeffne_assistent(haupt, einkauf=True)
            self._waehle_und_pruefe(assistent, "AUFTRAGSART", self.controls.AUFTRAGSART_BESTELLUNG)
            self._setze_partner(assistent, "LIEFERANTENNUMMER", lieferant.lieferantennummer, lieferant.name)
            self._setze_datum(assistent, "LIEFERDATUM", paket.roh.liefertag)
            self._setze_belegfelder(assistent, belegfelder)
            if paket.roh.bestellnummer:
                self._setze_pruefe(assistent, "BESTELLNUMMER", paket.roh.bestellnummer, pflicht=False)
            self._weiter(assistent, AssistentSchritt.POSITIONEN)
            self._erfasse_positionen(assistent, positionen, einkauf=True)
            self._weiter(assistent, AssistentSchritt.SUMMEN)
            netto, brutto = self._lese_summen(assistent)
            self._pruefe_summe(netto, paket.summe_netto_ek, "erwartetem Netto-EK")
            if dry_run:
                self._abbrechen(assistent)
                return self._ergebnis(Belegart.BESTELLUNG, dry_run=True, erfolgreich=True, netto=netto, brutto=brutto)
            self._speichern(assistent)
            nummer = self._lese_belegnummer(
                haupt, einkauf=True, auftragsart=self.controls.AUFTRAGSART_BESTELLUNG, partnernummer=lieferant.lieferantennummer
            )
            return self._ergebnis(Belegart.BESTELLUNG, dry_run=False, erfolgreich=True, belegnummer=nummer, netto=netto, brutto=brutto)
        except Exception as exc:  # noqa: BLE001 - jeder Fehler wird als Belegergebnis gemeldet
            return self._fehlschlag(Belegart.BESTELLUNG, dry_run, exc, "einkauf_fehler")

    def schreibe_verkauf_ab(self, paket: Belegpaket, *, dry_run: bool = True) -> Belegergebnis:
        """Auftragsbestätigung im Verkauf anlegen (Kunde per Kundennummer, Preise = VK Preisgruppe 1 aus dem Stamm)."""
        self._neuer_lauf()
        modus = "Dry Run" if dry_run else "scharf"
        logger.info("UI Verkauf-AB (%s): Firma %s, Quelldatei %s", modus, self.firma.id, paket.roh.quelldatei.name)
        try:
            positionen = self._pruefe_paket(paket, einkauf=False, dry_run=dry_run)
            kunde = paket.kunde.kunde
            assert kunde is not None
            belegfelder = self._belegfelder(paket, dry_run)
            haupt = self._vorbereiten()
            assistent = self._oeffne_assistent(haupt, einkauf=False)
            self._waehle_und_pruefe(assistent, "AUFTRAGSART", self.controls.AUFTRAGSART_AB)
            self._setze_partner(assistent, "KUNDENNUMMER", kunde.kundennummer, kunde.name)
            if paket.roh.bestellnummer:
                self._setze_pruefe(assistent, "BESTELLNUMMER", paket.roh.bestellnummer)
            self._setze_datum(assistent, "LIEFERDATUM", paket.roh.liefertag)
            if paket.roh.lieferadresse:
                # TODO-LEXWARE: abweichende Lieferanschrift evtl. nur am Lieferschein wählbar
                self._setze_pruefe(assistent, "LIEFERADRESSE", paket.roh.lieferadresse, pflicht=False)
            self._setze_belegfelder(assistent, belegfelder)
            self._weiter(assistent, AssistentSchritt.POSITIONEN)
            self._erfasse_positionen(assistent, positionen, einkauf=False)
            self._weiter(assistent, AssistentSchritt.SUMMEN)
            netto, brutto = self._lese_summen(assistent)
            self._pruefe_summe(netto, paket.summe_netto_vk, "erwartetem Netto-VK")
            if dry_run:
                self._abbrechen(assistent)
                return self._ergebnis(Belegart.AUFTRAGSBESTAETIGUNG, dry_run=True, erfolgreich=True, netto=netto, brutto=brutto)
            self._speichern(assistent)
            nummer = self._lese_belegnummer(
                haupt, einkauf=False, auftragsart=self.controls.AUFTRAGSART_AB, partnernummer=kunde.kundennummer
            )
            return self._ergebnis(
                Belegart.AUFTRAGSBESTAETIGUNG, dry_run=False, erfolgreich=True, belegnummer=nummer, netto=netto, brutto=brutto
            )
        except Exception as exc:  # noqa: BLE001
            return self._fehlschlag(Belegart.AUFTRAGSBESTAETIGUNG, dry_run, exc, "verkauf_fehler")

    def auftrag_weiterfuehren_lieferschein(
        self, paket: Belegpaket, ab_belegnummer: str, *, dry_run: bool = True
    ) -> Belegergebnis:
        """Lieferschein aus der AB per 'Auftrag weiterführen' (nur nach Freigabe; Dry Run bricht ab)."""
        self._neuer_lauf()
        modus = "Dry Run" if dry_run else "scharf"
        logger.info("UI Lieferschein aus AB %s (%s): Firma %s", ab_belegnummer, modus, self.firma.id)
        c = self.controls
        try:
            if not ab_belegnummer or not ab_belegnummer.strip():
                raise AblaufFehler("AB-Belegnummer fehlt")
            if not dry_run and not (paket.freigegeben and paket.lieferschein_erstellen):
                raise AblaufFehler("Lieferschein nur nach Freigabe und mit lieferschein_erstellen=True")
            kunde = paket.kunde.kunde
            haupt = self._vorbereiten()
            self.backend.oeffne_menue(c.MENUE_AUFTRAGSLISTE_VERKAUF)
            zeilen = self.backend.lese_liste(haupt, "AUFTRAGSLISTE_VERKAUF")
            treffer = [z for z in zeilen if _spalte(z, c.SPALTE_BELEGNUMMER, "Beleg-Nr.", "Nummer") == ab_belegnummer.strip()]
            if not treffer:
                raise AblaufFehler(f"AB {ab_belegnummer} nicht in der Verkaufsauftragsliste gefunden")
            zeile = treffer[-1]
            status = _spalte(zeile, c.SPALTE_STATUS)
            if re.search(r"\bW\b", status):
                raise AblaufFehler(f"AB {ab_belegnummer} ist bereits weitergeführt (Status {status!r})")
            partner = _spalte(zeile, c.SPALTE_KUNDENNUMMER, "Kunde")
            if kunde is not None and partner and partner != kunde.kundennummer:
                raise AblaufFehler(f"AB {ab_belegnummer} gehört zu Kunde {partner}, Paket zu {kunde.kundennummer}")
            self.backend.waehle_listeneintrag(haupt, "AUFTRAGSLISTE_VERKAUF", ab_belegnummer.strip())
            self._protokoll(f"AB {ab_belegnummer} in der Auftragsliste markiert")
            self.backend.oeffne_menue(c.MENUE_AUFTRAG_WEITERFUEHREN)
            self._protokoll("'Auftrag weiterführen' aufgerufen")
            assistent = self.backend.finde_dialog(c.ASSISTENT_VERKAUF_TITEL_REGEX)
            self._behandle_dialoge(DialogKontext.ASSISTENT)
            if self._schritt(assistent) != AssistentSchritt.KOPF:
                raise AblaufFehler("Assistent nach 'Weiterführen' nicht auf Seite 1")
            self._waehle_und_pruefe(assistent, "AUFTRAGSART", c.AUFTRAGSART_LIEFERSCHEIN)
            self._setze_datum(assistent, "LIEFERDATUM", paket.roh.liefertag)
            self._weiter(assistent, AssistentSchritt.POSITIONEN)
            uebernommen = self.backend.lese_liste(assistent, "POSITIONSLISTE")
            if not uebernommen:
                raise AblaufFehler("Keine Positionen aus der AB übernommen")
            self._protokoll(f"{len(uebernommen)} Positionen aus der AB übernommen")
            erwartet = {p.artikel.artikelnummer for p in paket.aktive_positionen() if p.artikel is not None}
            vorhanden = {_spalte(z, c.SPALTE_ARTIKELNUMMER, "Artikel-Nr.", "Nummer") for z in uebernommen}
            if erwartet and erwartet != vorhanden:
                self._warnung(f"Positionen der AB ({sorted(vorhanden)}) weichen vom Paket ({sorted(erwartet)}) ab")
            self._weiter(assistent, AssistentSchritt.SUMMEN)
            netto, brutto = self._lese_summen(assistent)
            if abs(netto - paket.summe_netto_vk) > SUMMEN_TOLERANZ:
                self._warnung(
                    f"Netto des Lieferscheins {formatiere_betrag(netto)} weicht vom Paket {formatiere_betrag(paket.summe_netto_vk)} ab"
                )
            if dry_run:
                self._abbrechen(assistent)
                return self._ergebnis(Belegart.LIEFERSCHEIN, dry_run=True, erfolgreich=True, netto=netto, brutto=brutto)
            self._speichern(assistent)
            nummer = self._lese_belegnummer(
                haupt, einkauf=False, auftragsart=c.AUFTRAGSART_LIEFERSCHEIN, partnernummer=kunde.kundennummer if kunde else partner
            )
            return self._ergebnis(Belegart.LIEFERSCHEIN, dry_run=False, erfolgreich=True, belegnummer=nummer, netto=netto, brutto=brutto)
        except Exception as exc:  # noqa: BLE001
            return self._fehlschlag(Belegart.LIEFERSCHEIN, dry_run, exc, "lieferschein_fehler")
