"""Fixtermin-Register: Zeilen der ``Fixterminüberwachung.xlsx`` lesen, anlegen und aktualisieren.

Struktur der Datei (docs/AUSBAUSTUFE_2.md, Abschnitt 1): Blatt "Fixterminüberwachung", Zeile 1
Titel, Zeile 2 Legende, Zeile 4 Kopfzeile (KW | Datum | Tag | Lieferant | Kunde | Auftragsdatum |
Verladedatum | Bestätigung des Lieferanten | Bestätigung der Spedition | Abgeholt | Bemerkung),
ab Zeile 5 je Werktag fünf Zeilen (Slots). In den ersten vier Slots verweisen die Spalten A–C
per Formel auf die Ankerzeile des Tages (der letzte Slot); Feiertage sind eine einzelne Zeile mit
Bemerkung "Feiertag: …". Die Spalten H–J sind boolesche Werte (Excel-365-Kontrollkästchen).

Regeln des Registers:

* Gelesen wird der benutzte Bereich **einmal** (:meth:`FixterminRegister.lade`); die
  Spaltenindizes kommen aus der Kopfzeile (tolerant gegen abweichende Beschriftungen), nicht aus
  festen Buchstaben. Datumswerte dürfen als ``date``/``datetime`` (COM, Fake), Serienzahl (Graph)
  oder Text vorliegen; Formelverweise in Spalte B (``=B9``) werden auf die Ankerzeile aufgelöst.
* Geschrieben werden **nur die Datenspalten** (im Standardlayout D..K) einer Zeile über
  ``ExcelBackend.schreibe_bereich``; ``None`` lässt Zellen unverändert, ``""`` leert sie.
  Spalten A–C und das Ressourcen-Blatt werden nie beschrieben. Datumswerte gehen als ``date``,
  Häkchen als ``bool`` (die Backends setzen Zahlenformat bzw. JSON-Boolean).
* :meth:`trage_ein` belegt den ersten freien Slot des Tages (Lieferant und Kunde leer); ein
  vorhandener Eintrag (gleicher Tag, Lieferant, Kunde, ggf. Bestellnummer in der Bemerkung) wird
  aktualisiert statt doppelt angelegt. Kein freier Slot -> :class:`RegisterVoll`; Tag ohne Zeile
  (Wochenende, Feiertag, außerhalb des Zeitraums) -> :class:`RegisterFehler` mit klarer Meldung.
* Die Bemerkung trägt kompakt ``Best.-Nr <nr>, AB <nr>, <Spedition>`` (und ``Ref. <Referenz>``
  der Spedition); :func:`bemerkung_teile` liest diese Angaben wieder aus.
* Dry Run: Register über ``FakeExcelBackend(aus_xlsx=…)`` betreiben und die Änderungen mit
  ``FakeExcelBackend.vorschau_csv`` ausgeben; die Datei selbst wird dabei nie geschrieben.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from auftragsautomat.benennung import UNBEKANNT, normalisiere
from auftragsautomat.config import DateiRefKonfig
from auftragsautomat.m365.basis import (
    ExcelBackend,
    M365NichtGefunden,
    Zellbereich,
    excel_sitzung,
    parse_adresse,
    serienzahl_zu_datum,
    spaltenbuchstabe,
    spaltenindex,
)
from auftragsautomat.models import Belegpaket, BuchungsErgebnis, Fixtermin
from auftragsautomat.termine import kalender
from auftragsautomat.termine.kurznamen import Kurznamen

__all__ = [
    "BEMERKUNG_TRENNER",
    "BESTAETIGUNGSFELDER",
    "DATENFELDER",
    "Bestaetigungsfeld",
    "FixterminRegister",
    "RegisterFehler",
    "RegisterVoll",
    "STANDARD_BLATT",
    "STANDARD_KOPFZEILE",
    "STANDARD_SLOTS_JE_TAG",
    "bemerkung_teile",
    "bemerkung_zusammenfuegen",
    "fixtermin_aus_paket",
]

logger = logging.getLogger(__name__)

STANDARD_BLATT = "Fixterminüberwachung"
STANDARD_KOPFZEILE = 4
STANDARD_SLOTS_JE_TAG = 5
#: Trenner zwischen Abschnitten der Bemerkung.
BEMERKUNG_TRENNER = " | "

Bestaetigungsfeld = Literal["lieferant", "spedition", "abgeholt"]
#: Kurzname des Häkchens -> Feld im Modell / Spaltenschlüssel.
BESTAETIGUNGSFELDER: dict[str, str] = {
    "lieferant": "lieferant_bestaetigt",
    "spedition": "spedition_bestaetigt",
    "abgeholt": "abgeholt",
}
#: Datenfelder einer Zeile (Standardlayout D..K) in Blattreihenfolge; nur diese werden geschrieben.
DATENFELDER: tuple[str, ...] = (
    "lieferant",
    "kunde",
    "auftragsdatum",
    "verladedatum",
    "lieferant_bestaetigt",
    "spedition_bestaetigt",
    "abgeholt",
    "bemerkung",
)
#: Spalten, ohne die das Blatt nicht als Fixterminüberwachung gilt.
PFLICHTSPALTEN: tuple[str, ...] = ("datum", "lieferant", "kunde")
#: Bemerkungen, die eine Zeile als gesperrt kennzeichnen (Feiertag, Betriebsruhe).
SPERRWOERTER: tuple[str, ...] = ("feiertag", "betriebsferien", "betriebsruhe", "geschlossen", "kein werktag")

_FORMEL_RE = re.compile(r"^=\s*(?:'[^']*'!|[A-Za-z0-9_]+!)?\$?([A-Za-z]{1,3})\$?(\d+)\s*$")
_BEST_RE = re.compile(r"best\.?\s*-?\s*nr\.?\s*:?\s*([^\s,;|]+)", re.IGNORECASE)
_AB_RE = re.compile(r"(?<![A-Za-z0-9])AB\s*:?\s*(?:Nr\.?\s*:?\s*)?(\d[^\s,;|]*)")
_REF_RE = re.compile(r"\bRef(?:erenz)?\.?\s*:?\s*([A-Za-z0-9][A-Za-z0-9-]*)", re.IGNORECASE)
_WAHR = {"wahr", "true", "ja", "yes", "x", "1", "1.0"}
_FALSCH = {"falsch", "false", "nein", "no", "0", "0.0", ""}
_WOCHENENDE = {5: "Samstag", 6: "Sonntag"}


class RegisterFehler(Exception):
    """Die Fixterminüberwachung kann nicht wie gewünscht beschrieben werden (Struktur, Tag ohne Zeile …)."""


class RegisterVoll(RegisterFehler):
    """Alle Slots des Tages sind belegt."""

    def __init__(self, datum: date, zeilen: list[int]) -> None:
        self.datum = datum
        self.zeilen = list(zeilen)
        super().__init__(
            f"Alle {len(zeilen)} Slots für den {datum:%d.%m.%Y} sind belegt (Zeilen {zeilen[0]}–{zeilen[-1]}); "
            "bitte in der Fixterminüberwachung Zeilen ergänzen oder einen Eintrag zusammenfassen."
        )


# ---------------------------------------------------------------------------
# Werte-Umwandlung
# ---------------------------------------------------------------------------


def _text_zu_datum(text: str) -> date | None:
    t = text.strip()
    for muster in ("%d.%m.%Y", "%Y-%m-%d", "%d.%m.%y"):
        try:
            return datetime.strptime(t, muster).date()
        except ValueError:
            continue
    return None


def _als_datum(wert: Any) -> date | None:
    """Zellwert (date, datetime, Serienzahl, Text) -> ``date``; Leeres/Unpassendes -> ``None``."""
    if wert is None or wert == "" or isinstance(wert, bool):
        return None
    if isinstance(wert, str):
        return _text_zu_datum(wert)
    ergebnis = serienzahl_zu_datum(wert)
    if isinstance(ergebnis, datetime):
        return ergebnis.date()
    return ergebnis


def _als_bool(wert: Any) -> bool:
    """Zellwert eines Kontrollkästchens -> bool (``True``/``"WAHR"``/``1`` …)."""
    if isinstance(wert, bool):
        return wert
    if wert is None:
        return False
    if isinstance(wert, int | float | Decimal):
        return wert != 0
    text = str(wert).strip().casefold()
    if text in _WAHR:
        return True
    if text in _FALSCH:
        return False
    logger.debug("Unbekannter Wert %r für ein Kontrollkästchen; als nicht gesetzt gewertet", wert)
    return False


def _als_text(wert: Any) -> str:
    if wert is None or isinstance(wert, bool):
        return "" if wert is None else ("WAHR" if wert else "FALSCH")
    if isinstance(wert, datetime | date):
        return wert.strftime("%d.%m.%Y")
    if isinstance(wert, float) and wert.is_integer():
        return str(int(wert))
    return str(wert).strip()


def _excel_wert(wert: Any) -> Any:
    """Wert so, wie er an ``schreibe_bereich`` geht: ``None`` unverändert, ``""`` leeren, sonst Typ erhalten."""
    if wert is None or isinstance(wert, bool | date | datetime | Decimal | int | float):
        return wert
    return str(wert)


def _spaltenname(text: Any) -> str | None:
    """Ordnet eine Überschrift der Kopfzeile einem Feldschlüssel zu (tolerant, ``None`` = unbekannt)."""
    n = normalisiere(str(text)) if text not in (None, "") else ""
    if not n:
        return None
    if n in ("kw", "kalenderwoche") or n.startswith("kw "):
        return "kw"
    if n in ("datum", "liefertag", "lieferdatum", "fixtermin", "termin"):
        return "datum"
    if n in ("tag", "wochentag"):
        return "tag"
    if "verlade" in n:
        return "verladedatum"
    if "auftragsdatum" in n or ("auftrag" in n and "datum" in n):
        return "auftragsdatum"
    if "abgeholt" in n or n == "abholung":
        return "abgeholt"
    if "bemerkung" in n or n in ("notiz", "hinweis", "kommentar", "anmerkung"):
        return "bemerkung"
    if "bestaet" in n and "lieferant" in n:
        return "lieferant_bestaetigt"
    if "bestaet" in n and "spedition" in n:
        return "spedition_bestaetigt"
    if n.startswith("lieferant") or n == "weingut":
        return "lieferant"
    if n.startswith("kunde"):
        return "kunde"
    return None


# ---------------------------------------------------------------------------
# Bemerkung
# ---------------------------------------------------------------------------


def bemerkung_teile(text: str | None) -> dict[str, str | None]:
    """Liest ``Best.-Nr``, ``AB``, Spedition und ``Ref.`` aus einer Bemerkung.

    Die Spedition wird nur in einem Abschnitt (zwischen ``|``) erkannt, der auch eine Nummer
    (Best.-Nr, AB oder Ref.) enthält, damit freier Text ("Kunde wünscht Vormittag") nicht als
    Spedition gilt. Die erste Fundstelle gewinnt.
    """
    teile: dict[str, str | None] = {"bestellnummer": None, "ab_nummer": None, "spedition": None, "tracking_referenz": None}
    if not text:
        return teile
    for abschnitt in str(text).split("|"):
        rest: list[str] = []
        kennung = False
        for token in (t.strip() for t in abschnitt.split(",")):
            if not token:
                continue
            if (m := _BEST_RE.search(token)) is not None:
                kennung = True
                teile["bestellnummer"] = teile["bestellnummer"] or m.group(1)
            elif (m := _AB_RE.search(token)) is not None:
                kennung = True
                teile["ab_nummer"] = teile["ab_nummer"] or m.group(1)
            elif (m := _REF_RE.search(token)) is not None:
                kennung = True
                teile["tracking_referenz"] = teile["tracking_referenz"] or m.group(1)
            else:
                rest.append(token)
        if kennung and rest and teile["spedition"] is None:
            teile["spedition"] = rest[0]
    return teile


def bemerkung_zusammenfuegen(bisher: str | None, fixtermin: Fixtermin) -> str:
    """Bisherige Bemerkung + freier Text des Fixtermins + kompakter Abschnitt
    ``Best.-Nr <nr>, AB <nr>, <Spedition>, Ref. <Referenz>``; schon vorhandene Angaben werden nicht wiederholt."""
    basis = (bisher or "").strip()
    frei = (fixtermin.bemerkung or "").strip()
    if frei and normalisiere(frei) not in normalisiere(basis):
        basis = f"{basis}{BEMERKUNG_TRENNER}{frei}" if basis else frei
    kandidaten: list[str] = []
    if fixtermin.bestellnummer:
        kandidaten.append(f"Best.-Nr {fixtermin.bestellnummer.strip()}")
    if fixtermin.ab_nummer:
        kandidaten.append(f"AB {fixtermin.ab_nummer.strip()}")
    if fixtermin.spedition and fixtermin.spedition.strip():
        kandidaten.append(fixtermin.spedition.strip())
    if fixtermin.tracking_referenz:
        kandidaten.append(f"Ref. {fixtermin.tracking_referenz.strip()}")
    basis_norm = normalisiere(basis)
    neu = [k for k in kandidaten if normalisiere(k) not in basis_norm]
    abschnitt = ", ".join(neu)
    if basis and abschnitt:
        return f"{basis}{BEMERKUNG_TRENNER}{abschnitt}"
    return basis or abschnitt


# ---------------------------------------------------------------------------
# Register
# ---------------------------------------------------------------------------


class FixterminRegister:
    """Zugriff auf das Blatt "Fixterminüberwachung" über ein :class:`ExcelBackend`."""

    def __init__(
        self,
        excel: ExcelBackend,
        datei: DateiRefKonfig,
        *,
        kopfzeile: int = STANDARD_KOPFZEILE,
        slots_je_tag: int = STANDARD_SLOTS_JE_TAG,
        blatt: str | None = None,
        bundesland: str = kalender.STANDARD_BUNDESLAND,
    ) -> None:
        self.excel = excel
        self.datei = datei
        self.kopfzeile = kopfzeile
        self.slots_je_tag = max(1, slots_je_tag)
        self.bundesland = bundesland
        self.blatt = blatt or datei.blatt or STANDARD_BLATT
        self.spalten: dict[str, int] = {}
        self._matrix: list[list[Any]] = []
        self._offset_zeile = 1
        self._offset_spalte = 1
        self._datum_je_zeile: dict[int, date] = {}
        self._zeilen_je_datum: dict[date, list[int]] = {}
        self._geladen = False

    # -- Laden ------------------------------------------------------------------------

    @property
    def geladen(self) -> bool:
        return self._geladen

    def lade(self, *, erneut: bool = False) -> None:
        """Liest den benutzten Bereich des Blatts einmal; ``erneut=True`` liest neu (z. B. nach fremden Änderungen)."""
        if self._geladen and not erneut:
            return
        adresse, werte = self._lese_blatt()
        bereich = parse_adresse(adresse)
        self._offset_zeile, self._offset_spalte = bereich.zeile_von, bereich.spalte_von
        self._matrix = [list(zeile) for zeile in werte]
        self._kopfzeile_erkennen()
        self._datumsindex_aufbauen()
        self._geladen = True
        logger.info(
            "Fixterminüberwachung geladen: Blatt '%s', %s Zeilen, Kopfzeile %s, %s Tage",
            self.blatt, len(self._matrix), self.kopfzeile, len(self._zeilen_je_datum),
        )

    def _lese_blatt(self) -> tuple[str, list[list[Any]]]:
        try:
            return self.excel.lese_benutzten_bereich(self.datei, self.blatt)
        except M365NichtGefunden as exc:
            try:
                namen = self.excel.blattnamen(self.datei)
            except M365NichtGefunden as exc2:
                raise RegisterFehler(f"Fixterminüberwachung nicht lesbar: {exc2}") from exc2
            ersatz = next((n for n in namen if "fixtermin" in normalisiere(n)), None)
            if ersatz is None:
                raise RegisterFehler(
                    f"Blatt '{self.blatt}' fehlt in der Fixterminüberwachung (vorhanden: {', '.join(namen) or '-'})."
                ) from exc
            logger.warning("Blatt '%s' fehlt; verwende '%s'", self.blatt, ersatz)
            self.blatt = ersatz
            return self.excel.lese_benutzten_bereich(self.datei, self.blatt)

    @property
    def _letzte_zeile(self) -> int:
        return self._offset_zeile + len(self._matrix) - 1

    @property
    def _breite(self) -> int:
        return max((len(z) for z in self._matrix), default=0)

    def _wert_roh(self, zeile: int, spalte: int) -> Any:
        zi, si = zeile - self._offset_zeile, spalte - self._offset_spalte
        if zi < 0 or si < 0 or zi >= len(self._matrix) or si >= len(self._matrix[zi]):
            return ""
        wert = self._matrix[zi][si]
        return "" if wert is None else wert

    def _setze_cache(self, zeile: int, spalte: int, wert: Any) -> None:
        zi, si = zeile - self._offset_zeile, spalte - self._offset_spalte
        if zi < 0 or si < 0:
            return
        while len(self._matrix) <= zi:
            self._matrix.append([])
        reihe = self._matrix[zi]
        while len(reihe) <= si:
            reihe.append("")
        reihe[si] = wert

    def _spalten_aus(self, zeile: int) -> dict[str, int]:
        zuordnung: dict[str, int] = {}
        for spalte in range(self._offset_spalte, self._offset_spalte + self._breite):
            name = _spaltenname(self._wert_roh(zeile, spalte))
            if name is None:
                continue
            if name in zuordnung:
                logger.warning("Kopfzeile %s: Spalte '%s' doppelt (%s und %s); die erste gilt", zeile, name, spaltenbuchstabe(zuordnung[name]), spaltenbuchstabe(spalte))
                continue
            zuordnung[name] = spalte
        return zuordnung

    def _kopfzeile_erkennen(self) -> None:
        zuordnung = self._spalten_aus(self.kopfzeile)
        if not all(p in zuordnung for p in PFLICHTSPALTEN):
            for zeile in range(self._offset_zeile, min(self._letzte_zeile, self.kopfzeile + 20) + 1):
                kandidat = self._spalten_aus(zeile)
                if all(p in kandidat for p in PFLICHTSPALTEN):
                    logger.warning("Kopfzeile der Fixterminüberwachung in Zeile %s statt %s gefunden", zeile, self.kopfzeile)
                    self.kopfzeile, zuordnung = zeile, kandidat
                    break
            else:
                fehlend = [p for p in PFLICHTSPALTEN if p not in zuordnung]
                raise RegisterFehler(
                    f"Kopfzeile der Fixterminüberwachung nicht gefunden (Blatt '{self.blatt}', erwartet in Zeile "
                    f"{self.kopfzeile}; fehlende Spalten: {', '.join(fehlend)})."
                )
        fehlend_optional = [f for f in DATENFELDER if f not in zuordnung]
        if fehlend_optional:
            logger.warning("Fixterminüberwachung ohne Spalte(n) %s; diese Felder werden nicht geschrieben", ", ".join(fehlend_optional))
        self.spalten = zuordnung

    def _datum_aufloesen(self, wert: Any, zeile: int, tiefe: int = 0) -> date | None:
        if isinstance(wert, str):
            treffer = _FORMEL_RE.match(wert.strip())
            if treffer is not None:
                if tiefe >= 10:
                    return None
                ziel_spalte, ziel_zeile = spaltenindex(treffer.group(1)), int(treffer.group(2))
                if ziel_zeile == zeile and ziel_spalte == self.spalten["datum"]:
                    return None
                return self._datum_aufloesen(self._wert_roh(ziel_zeile, ziel_spalte), ziel_zeile, tiefe + 1)
        return _als_datum(wert)

    @staticmethod
    def _leer_oder_formel(wert: Any) -> bool:
        return wert in (None, "") or (isinstance(wert, str) and wert.strip().startswith("="))

    def _datumsindex_aufbauen(self) -> None:
        spalte = self.spalten["datum"]
        erste, letzte = self.kopfzeile + 1, self._letzte_zeile
        datum_je_zeile: dict[int, date | None] = {
            zeile: self._datum_aufloesen(self._wert_roh(zeile, spalte), zeile) for zeile in range(erste, letzte + 1)
        }
        # Slots ohne auswertbares Datum (unausgewertete Formel, leere Zelle) gehören zur nächsten
        # Ankerzeile, sofern sie innerhalb eines Tagesblocks liegen.
        naechstes: date | None = None
        abstand = 0
        for zeile in range(letzte, erste - 1, -1):
            datum = datum_je_zeile[zeile]
            if datum is not None:
                naechstes, abstand = datum, 0
                continue
            abstand += 1
            if naechstes is not None and abstand < self.slots_je_tag and self._leer_oder_formel(self._wert_roh(zeile, spalte)):
                datum_je_zeile[zeile] = naechstes
        self._datum_je_zeile = {z: d for z, d in datum_je_zeile.items() if d is not None}
        self._zeilen_je_datum = {}
        for zeile, datum in sorted(self._datum_je_zeile.items()):
            self._zeilen_je_datum.setdefault(datum, []).append(zeile)

    # -- Lesen ------------------------------------------------------------------------

    def _wert(self, zeile: int, feld: str) -> Any:
        spalte = self.spalten.get(feld)
        return "" if spalte is None else self._wert_roh(zeile, spalte)

    def _text(self, zeile: int, feld: str) -> str:
        return _als_text(self._wert(zeile, feld))

    def _belegt(self, zeile: int) -> bool:
        return bool(self._text(zeile, "lieferant") or self._text(zeile, "kunde"))

    def _gesperrt(self, zeile: int) -> bool:
        """Zeile ohne Lieferant/Kunde, deren Bemerkung sie als Feiertag/Betriebsruhe kennzeichnet."""
        if self._belegt(zeile):
            return False
        bemerkung = normalisiere(self._text(zeile, "bemerkung"))
        return any(wort in bemerkung for wort in SPERRWOERTER)

    def zeitraum(self) -> tuple[date, date] | None:
        """Erster und letzter Tag, für den das Blatt Zeilen hat (``None`` bei leerem Blatt)."""
        self.lade()
        if not self._zeilen_je_datum:
            return None
        return min(self._zeilen_je_datum), max(self._zeilen_je_datum)

    def zeilen_fuer_datum(self, datum: date | datetime) -> list[int]:
        """Alle Excel-Zeilen (1-basiert) dieses Tages, auch belegte und gesperrte."""
        self.lade()
        tag = datum.date() if isinstance(datum, datetime) else datum
        return list(self._zeilen_je_datum.get(tag, []))

    def datum_der_zeile(self, zeile: int) -> date | None:
        self.lade()
        return self._datum_je_zeile.get(zeile)

    def freie_slots(self, datum: date | datetime) -> list[int]:
        """Zeilen des Tages, in denen Lieferant und Kunde leer sind und die nicht gesperrt sind."""
        return [z for z in self.zeilen_fuer_datum(datum) if not self._belegt(z) and not self._gesperrt(z)]

    def eintrag(self, zeile: int) -> Fixtermin | None:
        """Der Fixtermin einer Zeile; ``None`` für freie, gesperrte oder datumslose Zeilen."""
        self.lade()
        return self._fixtermin_aus_zeile(zeile)

    def _fixtermin_aus_zeile(self, zeile: int) -> Fixtermin | None:
        if not self._belegt(zeile):
            return None
        datum = self._datum_je_zeile.get(zeile)
        if datum is None:
            logger.warning("Zeile %s der Fixterminüberwachung ist belegt, hat aber kein Datum; wird übersprungen", zeile)
            return None
        bemerkung = self._text(zeile, "bemerkung") or None
        teile = bemerkung_teile(bemerkung)
        return Fixtermin(
            lieferant_kurz=self._text(zeile, "lieferant") or UNBEKANNT,
            kunde_kurz=self._text(zeile, "kunde") or UNBEKANNT,
            liefertag=datum,
            auftragsdatum=_als_datum(self._wert(zeile, "auftragsdatum")),
            verladedatum=_als_datum(self._wert(zeile, "verladedatum")),
            lieferant_bestaetigt=_als_bool(self._wert(zeile, "lieferant_bestaetigt")),
            spedition_bestaetigt=_als_bool(self._wert(zeile, "spedition_bestaetigt")),
            abgeholt=_als_bool(self._wert(zeile, "abgeholt")),
            bemerkung=bemerkung,
            bestellnummer=teile["bestellnummer"],
            ab_nummer=teile["ab_nummer"],
            spedition=teile["spedition"],
            tracking_referenz=teile["tracking_referenz"],
            zeile=zeile,
            quelle=f"{self.blatt}!{zeile}",
        )

    def eintraege(self, ab: date | None = None, bis: date | None = None) -> list[Fixtermin]:
        """Alle belegten Slots (Zeilenreihenfolge), optional auf Liefertag ``ab``..``bis`` eingegrenzt."""
        self.lade()
        ergebnis: list[Fixtermin] = []
        for zeile in sorted(self._datum_je_zeile):
            datum = self._datum_je_zeile[zeile]
            if (ab is not None and datum < ab) or (bis is not None and datum > bis):
                continue
            eintrag = self._fixtermin_aus_zeile(zeile)
            if eintrag is not None:
                ergebnis.append(eintrag)
        return ergebnis

    def offene(self, bis: date | None = None, *, ab: date | None = None) -> list[Fixtermin]:
        """Einträge, die noch nicht abgeholt sind (Liefertag bis ``bis``, optional ab ``ab``)."""
        return [e for e in self.eintraege(ab, bis) if not e.abgeholt]

    @staticmethod
    def _nummern_passen(a: Fixtermin, b: Fixtermin) -> tuple[bool, bool]:
        """``(treffer, konflikt)`` für Bestellnummer/AB-Nummer: gleich = Treffer, beide gesetzt und
        verschieden = Konflikt."""
        treffer = konflikt = False
        for feld in ("bestellnummer", "ab_nummer"):
            x, y = (getattr(a, feld) or "").strip(), (getattr(b, feld) or "").strip()
            if x and y:
                if x == y:
                    treffer = True
                else:
                    konflikt = True
        return treffer, konflikt

    def finde(self, fixtermin: Fixtermin) -> Fixtermin | None:
        """Vorhandener Eintrag mit gleichem Tag, Lieferant und Kunde; nennen beide eine Bestell-/AB-Nummer,
        muss sie übereinstimmen (sonst gilt es als anderer Vorgang)."""
        self.lade()
        lieferant, kunde = normalisiere(fixtermin.lieferant_kurz), normalisiere(fixtermin.kunde_kurz)
        kandidaten: list[Fixtermin] = []
        for zeile in self.zeilen_fuer_datum(fixtermin.liefertag):
            eintrag = self._fixtermin_aus_zeile(zeile)
            if eintrag is None:
                continue
            if normalisiere(eintrag.lieferant_kurz) == lieferant and normalisiere(eintrag.kunde_kurz) == kunde:
                kandidaten.append(eintrag)
        if not kandidaten:
            return None
        bewertet = [(e, *self._nummern_passen(e, fixtermin)) for e in kandidaten]
        for eintrag, treffer, _ in bewertet:
            if treffer:
                return eintrag
        for eintrag, _, konflikt in bewertet:
            if not konflikt:
                return eintrag
        return None

    # -- Schreiben -------------------------------------------------------------------

    def _schreibe(self, zeile: int, werte: Mapping[str, Any]) -> None:
        """Schreibt die Datenspalten einer Zeile in einem Zugriff (``None`` = unverändert)."""
        spalten = [self.spalten[f] for f in DATENFELDER if f in self.spalten]
        if not spalten:
            raise RegisterFehler("Fixterminüberwachung ohne Datenspalten; nichts geschrieben.")
        von, bis = min(spalten), max(spalten)
        reihe: list[Any] = [None] * (bis - von + 1)
        geschrieben: list[tuple[int, Any]] = []
        for feld, wert in werte.items():
            spalte = self.spalten.get(feld)
            if spalte is None:
                if wert not in (None, "", False):
                    logger.warning("Spalte für '%s' fehlt im Blatt; Wert %r wird nicht geschrieben", feld, wert)
                continue
            excel_wert = _excel_wert(wert)
            reihe[spalte - von] = excel_wert
            if excel_wert is not None:
                geschrieben.append((spalte, excel_wert))
        if not geschrieben:
            return
        adresse = Zellbereich(zeile, von, zeile, bis).adresse
        self.excel.schreibe_bereich(self.datei, self.blatt, adresse, [reihe])
        for spalte, wert in geschrieben:
            self._setze_cache(zeile, spalte, wert)
        logger.info("Fixterminüberwachung %s!%s: %s", self.blatt, adresse, ", ".join(f"{spaltenbuchstabe(s)}={_als_text(w) or '(leer)'}" for s, w in geschrieben))

    def _meldung_kein_tag(self, datum: date) -> str:
        text = datum.strftime("%d.%m.%Y")
        zeitraum = self.zeitraum()
        if zeitraum is None:
            return f"Die Fixterminüberwachung (Blatt '{self.blatt}') enthält keine Tageszeilen; bitte die Excel prüfen."
        von, bis = zeitraum
        if datum < von or datum > bis:
            return (
                f"Der {text} liegt außerhalb des Zeitraums der Fixterminüberwachung ({von:%d.%m.%Y}–{bis:%d.%m.%Y}); "
                "bitte die Zeilen für diesen Zeitraum in der Excel anlegen."
            )
        if kalender.ist_wochenende(datum):
            naechster = kalender.naechster_werktag(datum, self.bundesland)
            return (
                f"Der {text} ist ein {_WOCHENENDE[datum.weekday()]}; die Fixterminüberwachung führt nur Werktage "
                f"(nächster Werktag: {naechster:%d.%m.%Y})."
            )
        name = kalender.feiertag(datum, self.bundesland)
        if name:
            naechster = kalender.naechster_werktag(datum, self.bundesland)
            return f"Der {text} ist ein Feiertag ({name}) ohne Slots in der Fixterminüberwachung (nächster Werktag: {naechster:%d.%m.%Y})."
        return f"Für den {text} gibt es keine Zeile in der Fixterminüberwachung (Blatt '{self.blatt}'); bitte die Excel prüfen."

    def _freier_slot(self, datum: date) -> int:
        zeilen = self.zeilen_fuer_datum(datum)
        if not zeilen:
            raise RegisterFehler(self._meldung_kein_tag(datum))
        frei = [z for z in zeilen if not self._belegt(z) and not self._gesperrt(z)]
        if frei:
            return frei[0]
        if all(self._gesperrt(z) for z in zeilen):
            grund = self._text(zeilen[0], "bemerkung")
            raise RegisterFehler(
                f"Der {datum:%d.%m.%Y} ist in der Fixterminüberwachung gesperrt ({grund}); bitte einen anderen Liefertag wählen."
            )
        raise RegisterVoll(datum, zeilen)

    def _neu(self, zeile: int, fixtermin: Fixtermin) -> Fixtermin:
        bemerkung = bemerkung_zusammenfuegen("", fixtermin)
        self._schreibe(
            zeile,
            {
                "lieferant": fixtermin.lieferant_kurz.strip() or UNBEKANNT,
                "kunde": fixtermin.kunde_kurz.strip() or UNBEKANNT,
                "auftragsdatum": fixtermin.auftragsdatum or "",
                "verladedatum": fixtermin.verladedatum or "",
                "lieferant_bestaetigt": bool(fixtermin.lieferant_bestaetigt),
                "spedition_bestaetigt": bool(fixtermin.spedition_bestaetigt),
                "abgeholt": bool(fixtermin.abgeholt),
                "bemerkung": bemerkung,
            },
        )
        logger.info("Fixtermin %s / %s am %s in Zeile %s eingetragen", fixtermin.lieferant_kurz, fixtermin.kunde_kurz, fixtermin.liefertag.strftime("%d.%m.%Y"), zeile)
        return fixtermin.model_copy(update={"zeile": zeile, "bemerkung": bemerkung or None, "quelle": f"{self.blatt}!{zeile}"})

    def _aktualisiere(self, vorhanden: Fixtermin, neu: Fixtermin) -> Fixtermin:
        zeile = vorhanden.zeile
        if zeile is None:
            raise RegisterFehler("Vorhandener Eintrag ohne Zeilennummer; Aktualisierung nicht möglich.")
        werte: dict[str, Any] = {}
        for feld in ("auftragsdatum", "verladedatum"):
            wert = getattr(neu, feld)
            if wert is not None and wert != getattr(vorhanden, feld):
                werte[feld] = wert
        for feld in BESTAETIGUNGSFELDER.values():
            if getattr(neu, feld) and not getattr(vorhanden, feld):
                werte[feld] = True
        bemerkung = bemerkung_zusammenfuegen(vorhanden.bemerkung, neu)
        if bemerkung != (vorhanden.bemerkung or ""):
            werte["bemerkung"] = bemerkung
        if werte:
            self._schreibe(zeile, werte)
            logger.info("Fixtermin %s / %s am %s in Zeile %s aktualisiert (%s)", vorhanden.lieferant_kurz, vorhanden.kunde_kurz, vorhanden.liefertag.strftime("%d.%m.%Y"), zeile, ", ".join(werte))
        else:
            logger.info("Fixtermin %s / %s am %s steht bereits in Zeile %s; nichts geändert", vorhanden.lieferant_kurz, vorhanden.kunde_kurz, vorhanden.liefertag.strftime("%d.%m.%Y"), zeile)
        return neu.model_copy(
            update={
                "lieferant_kurz": vorhanden.lieferant_kurz,
                "kunde_kurz": vorhanden.kunde_kurz,
                "auftragsdatum": neu.auftragsdatum or vorhanden.auftragsdatum,
                "verladedatum": neu.verladedatum or vorhanden.verladedatum,
                "lieferant_bestaetigt": neu.lieferant_bestaetigt or vorhanden.lieferant_bestaetigt,
                "spedition_bestaetigt": neu.spedition_bestaetigt or vorhanden.spedition_bestaetigt,
                "abgeholt": neu.abgeholt or vorhanden.abgeholt,
                "bemerkung": bemerkung or None,
                "bestellnummer": neu.bestellnummer or vorhanden.bestellnummer,
                "ab_nummer": neu.ab_nummer or vorhanden.ab_nummer,
                "spedition": neu.spedition or vorhanden.spedition,
                "tracking_referenz": neu.tracking_referenz or vorhanden.tracking_referenz,
                "zeile": zeile,
                "quelle": f"{self.blatt}!{zeile}",
            }
        )

    def trage_ein(self, fixtermin: Fixtermin) -> Fixtermin:
        """Trägt den Fixtermin in den ersten freien Slot seines Liefertags ein oder aktualisiert den
        vorhandenen Eintrag (gleicher Tag, Lieferant, Kunde, ggf. Nummer). Liefert den Eintrag mit ``zeile``."""
        self.lade()
        if not fixtermin.lieferant_kurz.strip() and not fixtermin.kunde_kurz.strip():
            raise RegisterFehler("Fixtermin ohne Lieferant und Kunde kann nicht eingetragen werden.")
        vorhanden = self.finde(fixtermin)
        with excel_sitzung(self.excel, self.datei):
            if vorhanden is not None:
                return self._aktualisiere(vorhanden, fixtermin)
            zeile = self._freier_slot(fixtermin.liefertag)
            return self._neu(zeile, fixtermin)

    def _eintrag_fuer(self, fixtermin: Fixtermin) -> Fixtermin:
        """Der Blatteintrag zu ``fixtermin``: über ``zeile`` (wenn Lieferant/Kunde dort passen), sonst :meth:`finde`."""
        self.lade()
        if fixtermin.zeile is not None:
            eintrag = self._fixtermin_aus_zeile(fixtermin.zeile)
            if (
                eintrag is not None
                and normalisiere(eintrag.lieferant_kurz) == normalisiere(fixtermin.lieferant_kurz)
                and normalisiere(eintrag.kunde_kurz) == normalisiere(fixtermin.kunde_kurz)
            ):
                return eintrag
            logger.warning("Zeile %s passt nicht mehr zu %s / %s; suche den Eintrag über den Liefertag", fixtermin.zeile, fixtermin.lieferant_kurz, fixtermin.kunde_kurz)
        eintrag = self.finde(fixtermin)
        if eintrag is None:
            raise RegisterFehler(
                f"Kein Eintrag {fixtermin.lieferant_kurz} / {fixtermin.kunde_kurz} am {fixtermin.liefertag:%d.%m.%Y} in der Fixterminüberwachung."
            )
        return eintrag

    def setze_bestaetigung(self, fixtermin: Fixtermin, feld: Bestaetigungsfeld, wert: bool = True) -> Fixtermin:
        """Setzt das Häkchen ``lieferant`` / ``spedition`` / ``abgeholt`` (``wert=False`` nimmt es zurück)."""
        try:
            feldname = BESTAETIGUNGSFELDER[feld]
        except KeyError as exc:
            raise ValueError(f"Unbekanntes Bestätigungsfeld '{feld}'; erlaubt: {', '.join(BESTAETIGUNGSFELDER)}") from exc
        eintrag = self._eintrag_fuer(fixtermin)
        assert eintrag.zeile is not None
        with excel_sitzung(self.excel, self.datei):
            self._schreibe(eintrag.zeile, {feldname: bool(wert)})
        return fixtermin.model_copy(update={feldname: bool(wert), "zeile": eintrag.zeile, "quelle": f"{self.blatt}!{eintrag.zeile}"})

    def setze_verladedatum(self, fixtermin: Fixtermin, datum: date | None) -> Fixtermin:
        """Schreibt das Verladedatum (``None`` leert die Zelle)."""
        eintrag = self._eintrag_fuer(fixtermin)
        assert eintrag.zeile is not None
        tag = datum.date() if isinstance(datum, datetime) else datum
        with excel_sitzung(self.excel, self.datei):
            self._schreibe(eintrag.zeile, {"verladedatum": tag if tag is not None else ""})
        return fixtermin.model_copy(update={"verladedatum": tag, "zeile": eintrag.zeile, "quelle": f"{self.blatt}!{eintrag.zeile}"})

    def _leere(self, zeile: int) -> None:
        # TODO-PRUEFEN: Am echten Tenant prüfen, ob False (statt "") in den Kontrollkästchen-Zellen H–J das
        # Kästchen erhält; ein Leeren mit "" könnte das Zellsteuerelement entfernen (Recherche 1.5, [NF]).
        self._schreibe(
            zeile,
            {
                "lieferant": "",
                "kunde": "",
                "auftragsdatum": "",
                "verladedatum": "",
                "lieferant_bestaetigt": False,
                "spedition_bestaetigt": False,
                "abgeholt": False,
                "bemerkung": "",
            },
        )

    def verschiebe(self, fixtermin: Fixtermin, neues_datum: date, grund: str) -> Fixtermin:
        """Verlegt den Eintrag auf ``neues_datum``: alte Zeile leeren, neue Zeile belegen,
        Bemerkung "verschoben von <alt>: <grund>" vor der bisherigen Bemerkung. Häkchen bleiben erhalten."""
        alt = self._eintrag_fuer(fixtermin)
        assert alt.zeile is not None
        neu_tag = neues_datum.date() if isinstance(neues_datum, datetime) else neues_datum
        if neu_tag == alt.liefertag:
            raise RegisterFehler(f"Der Eintrag steht bereits am {neu_tag:%d.%m.%Y}; nichts verschoben.")
        neue_zeile = self._freier_slot(neu_tag)  # prüft vor dem Leeren, damit nichts verloren geht
        vermerk = f"verschoben von {alt.liefertag:%d.%m.%Y}: {grund.strip() or 'ohne Angabe'}"
        bemerkung = f"{vermerk}{BEMERKUNG_TRENNER}{alt.bemerkung}" if alt.bemerkung else vermerk
        neu = alt.model_copy(
            update={
                "firma_id": fixtermin.firma_id or alt.firma_id,
                "liefertag": neu_tag,
                "bemerkung": bemerkung,
                "bestellnummer": fixtermin.bestellnummer or alt.bestellnummer,
                "kundenbestellnummer": fixtermin.kundenbestellnummer or alt.kundenbestellnummer,
                "ab_nummer": fixtermin.ab_nummer or alt.ab_nummer,
                "spedition": fixtermin.spedition or alt.spedition,
                "tracking_referenz": fixtermin.tracking_referenz or alt.tracking_referenz,
                "zeile": None,
            }
        )
        with excel_sitzung(self.excel, self.datei):
            self._leere(alt.zeile)
            ergebnis = self._neu(neue_zeile, neu)
        logger.info("Fixtermin %s / %s von %s (Zeile %s) auf %s (Zeile %s) verschoben: %s", alt.lieferant_kurz, alt.kunde_kurz, alt.liefertag.strftime("%d.%m.%Y"), alt.zeile, neu_tag.strftime("%d.%m.%Y"), neue_zeile, grund)
        return ergebnis


# ---------------------------------------------------------------------------
# Fixtermin aus einem Belegpaket
# ---------------------------------------------------------------------------


def _heute() -> date:
    """Heutiges Datum (eigene Funktion, damit Tests es ersetzen können)."""
    return date.today()


def fixtermin_aus_paket(
    paket: Belegpaket,
    ergebnis: BuchungsErgebnis | None = None,
    kurznamen: Kurznamen | None = None,
    *,
    heute: date | None = None,
) -> Fixtermin:
    """Baut den Fixtermin für die Excel aus Belegpaket und Buchungsergebnis.

    Liefertag = ``roh.liefertag`` (fehlt er: :class:`RegisterFehler`), Auftragsdatum = ``roh.datum``
    oder heute, Spedition = Belegfeld ``spedition``, Bestellnummer = Belegnummer der Einkauf-Bestellung
    (Weinstrasse Agentur: des Angebots, das als "Order" gedruckt wird), AB-Nummer = Belegnummer der
    Verkauf-AB; im Dry Run bleiben die Nummern ``None``. Kurznamen über ``kurznamen``, sonst über
    ``benennung.kurzname_lieferant`` / ``kurzname_kunde`` (Matchcode).
    """
    roh = paket.roh
    if roh.liefertag is None:
        raise RegisterFehler(
            f"Die Bestellung {roh.quelldatei.name} nennt keinen Liefertag; ohne Fixtermin kein Eintrag in der Fixterminüberwachung."
        )
    if kurznamen is not None:
        lieferant_kurz, kunde_kurz = kurznamen.fuer_paket(paket)
    else:
        from auftragsautomat import benennung

        lieferant_kurz, kunde_kurz = benennung.kurzname_lieferant(paket), benennung.kurzname_kunde(paket)
    bestellnummer = ab_nummer = None
    if ergebnis is not None:
        if ergebnis.einkauf is not None and ergebnis.einkauf.belegnummer:
            bestellnummer = ergebnis.einkauf.belegnummer
        elif ergebnis.angebot is not None and ergebnis.angebot.belegnummer:
            bestellnummer = ergebnis.angebot.belegnummer
        if ergebnis.verkauf is not None and ergebnis.verkauf.belegnummer:
            ab_nummer = ergebnis.verkauf.belegnummer
    spedition = (paket.belegfelder.spedition or "").strip() or None
    return Fixtermin(
        firma_id=paket.firma_id,
        lieferant_kurz=lieferant_kurz,
        kunde_kurz=kunde_kurz,
        liefertag=roh.liefertag,
        auftragsdatum=roh.datum or heute or _heute(),
        bestellnummer=bestellnummer,
        kundenbestellnummer=(roh.bestellnummer or "").strip() or None,
        ab_nummer=ab_nummer,
        spedition=spedition,
        quelle=roh.quelldatei.name,
    )
