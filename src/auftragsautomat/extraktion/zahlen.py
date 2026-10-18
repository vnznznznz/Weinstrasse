"""Zahlen-, Datums- und Mengen-Parser für deutsche Bestellbelege.

Alle Funktionen sind tolerant gegenüber Leerwerten ("", "-", None) und liefern
dann ``None`` statt einer Exception. Geld und Mengen werden ausschließlich als
``Decimal`` geliefert, niemals als ``float``.

* :func:`parse_dezimal`  – "1.234,50", "1234.50", "6,90 EUR", "6,90 €" -> ``Decimal``
* :func:`parse_datum`    – "15.09.2026", "15.9.26", "2026-09-15", "15. September 2026" -> ``date``
* :func:`parse_jahrgang` – vierstelliger Jahrgang (aus ``matching.volumen`` übernommen)
* :func:`parse_menge`    – "60", "60 Fl", "12 x 6", "2 Kt à 6" -> :class:`Menge`
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from auftragsautomat.matching.volumen import parse_jahrgang

__all__ = [
    "DATUM_MUSTER",
    "EINHEIT_MUSTER",
    "Menge",
    "finde_datum",
    "normalisiere_einheit",
    "parse_datum",
    "parse_dezimal",
    "parse_jahrgang",
    "parse_menge",
]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Dezimalzahlen
# ---------------------------------------------------------------------------

_LEERWERTE = frozenset({"", "-", "–", "—", "./.", "---", "n/a", "k.a.", "k. a.", "keine", "none"})
_WAEHRUNG_MUSTER = re.compile(r"(€|\beuro?\b|\bchf\b|\busd\b|\$|\bct\b|\bcent\b)", re.I)
_TAUSENDER_LEERZEICHEN = re.compile(r"[\s  ']")
_ZAHL_TOKEN = re.compile(r"\d[\d.,  ']*")


def parse_dezimal(text: str | Decimal | int | None) -> Decimal | None:
    """Liest eine deutsche oder englische Dezimalzahl.

    Erkennt Tausenderpunkte ("1.234,50"), englische Schreibweise ("1234.50",
    "1,234.50"), Währungszusätze ("6,90 EUR", "6,90 €", "€ 6,90"), Prozentzeichen
    sowie negative Beträge ("-5,00", "5,00-", "(5,00)"). Leerwerte ("", "-",
    None) ergeben ``None``; nicht lesbare Texte ebenfalls (mit Debug-Log).

    Eine einzelne Punktgruppe mit genau drei Nachkommastellen ("1.234") wird als
    deutscher Tausenderpunkt gelesen, außer der Vorkommateil ist "0" ("0.375").

    >>> parse_dezimal("1.234,50")
    Decimal('1234.50')
    >>> parse_dezimal("6,90 €")
    Decimal('6.90')
    >>> parse_dezimal("-") is None
    True
    """
    if text is None:
        return None
    if isinstance(text, Decimal):
        return text
    if isinstance(text, bool):
        return None
    if isinstance(text, int):
        return Decimal(text)
    if isinstance(text, float):  # sollte nicht vorkommen, aber nicht abstürzen
        return Decimal(str(text))

    roh = str(text).strip()
    if roh.casefold() in _LEERWERTE:
        return None

    s = _WAEHRUNG_MUSTER.sub(" ", roh)
    s = s.replace("%", " ").strip()

    negativ = False
    if s.startswith("(") and s.endswith(")"):
        negativ = True
        s = s[1:-1].strip()
    if s.endswith("-"):
        negativ = True
        s = s[:-1].strip()
    if s[:1] in ("-", "−", "–"):
        negativ = True
        s = s[1:].strip()
    elif s.startswith("+"):
        s = s[1:].strip()

    # Erst die ganze Zeichenkette, sonst das erste zahlähnliche Token verwenden.
    if not re.fullmatch(r"\d[\d.,\s  ']*", s):
        treffer = _ZAHL_TOKEN.search(s)
        if not treffer:
            logger.debug("Keine Dezimalzahl in %r", text)
            return None
        s = treffer.group(0)
    s = _TAUSENDER_LEERZEICHEN.sub("", s).strip(".,")
    if not s:
        return None

    if "," in s and "." in s:
        # Das zuletzt vorkommende Trennzeichen ist das Dezimaltrennzeichen.
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        if s.count(",") > 1:  # englische Tausendertrennung "1,234,567"
            s = s.replace(",", "")
        else:
            s = s.replace(",", ".")
    elif "." in s:
        if s.count(".") > 1:  # deutsche Tausenderpunkte "1.234.567"
            s = s.replace(".", "")
        else:
            vor, nach = s.split(".")
            if len(nach) == 3 and vor != "0" and 1 <= len(vor) <= 3:
                s = vor + nach  # "1.234" -> 1234 (deutscher Tausenderpunkt)

    try:
        wert = Decimal(s)
    except InvalidOperation:
        logger.debug("Dezimalzahl nicht lesbar: %r", text)
        return None
    return -wert if negativ else wert


# ---------------------------------------------------------------------------
# Datum
# ---------------------------------------------------------------------------

_MONATE: dict[str, int] = {
    "januar": 1, "jan": 1, "jänner": 1,
    "februar": 2, "feb": 2,
    "märz": 3, "maerz": 3, "mär": 3, "mrz": 3, "marz": 3,
    "april": 4, "apr": 4,
    "mai": 5,
    "juni": 6, "jun": 6,
    "juli": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sept": 9, "sep": 9,
    "oktober": 10, "okt": 10,
    "november": 11, "nov": 11,
    "dezember": 12, "dez": 12,
}  # fmt: skip

_MONATSNAMEN = "|".join(sorted(_MONATE, key=len, reverse=True))

# Reihenfolge: ISO zuerst (eindeutig), dann numerisch deutsch, dann Monatsname.
DATUM_MUSTER = re.compile(
    r"(?<![\d.])(?:"
    r"(?P<iso_j>\d{4})-(?P<iso_m>\d{1,2})-(?P<iso_t>\d{1,2})"
    r"|(?P<t>\d{1,2})\.\s?(?P<m>\d{1,2})\.\s?(?P<j>\d{4}|\d{2})"
    r"|(?P<st>\d{1,2})/(?P<sm>\d{1,2})/(?P<sj>\d{4}|\d{2})"
    r"|(?P<wt>\d{1,2})\.?\s*(?P<wm>" + _MONATSNAMEN + r")\.?\s+(?P<wj>\d{4}|\d{2})"
    r")(?![\d.]\d)",
    re.I,
)


def _jahr(text: str) -> int:
    jahr = int(text)
    if jahr < 100:
        jahr += 2000 if jahr < 80 else 1900
    return jahr


def finde_datum(text: str | None) -> tuple[date, str] | None:
    """Sucht das erste gültige Datum in einem Text und liefert (Datum, Fundstelle)."""
    if not text:
        return None
    for treffer in DATUM_MUSTER.finditer(text):
        g = treffer.groupdict()
        try:
            if g["iso_j"]:
                wert = date(int(g["iso_j"]), int(g["iso_m"]), int(g["iso_t"]))
            elif g["t"]:
                wert = date(_jahr(g["j"]), int(g["m"]), int(g["t"]))
            elif g["st"]:
                wert = date(_jahr(g["sj"]), int(g["sm"]), int(g["st"]))
            else:
                monat = _MONATE[g["wm"].casefold()]
                wert = date(_jahr(g["wj"]), monat, int(g["wt"]))
        except (ValueError, KeyError):
            logger.debug("Ungültiges Datum übersprungen: %r", treffer.group(0))
            continue
        return wert, treffer.group(0)
    return None


def parse_datum(text: str | date | None) -> date | None:
    """Liest ein Datum ("15.09.2026", "15.9.26", "2026-09-15", "15. September 2026").

    Zweistellige Jahre: 00–79 -> 20xx, 80–99 -> 19xx. Ungültige Daten (31.02.) -> ``None``.
    Steht das Datum in einem längeren Text ("Datum: 15.09.2026"), wird das erste
    gültige Datum geliefert.
    """
    if text is None:
        return None
    if isinstance(text, date):
        return text
    ergebnis = finde_datum(str(text))
    return ergebnis[0] if ergebnis else None


# ---------------------------------------------------------------------------
# Mengen und Einheiten
# ---------------------------------------------------------------------------

# Längste Alternativen zuerst, damit "Stück" nicht als "St" + "ück" gelesen wird.
EINHEIT_MUSTER = (
    r"(?:Flaschen|Flasche|Fla?\.?|Fl|Bottles|Bottle|Btls?\.?|Bt\.?"
    r"|Stück|Stueck|Stck\.?|Stk\.?|St\.?|Pcs|Pc"
    r"|Kartons|Karton|Kart\.?|Krt\.?|Kt\.?|Ktn\.?|Kisten|Kiste|Ki\.?|Cases|Case|Ctn"
    r"|Paletten|Palette|Pal\.?|Paket|Pak\.?|Einheiten|Einheit|Stk"
    r"|er)"
)
_VOLUMEN_EINHEIT = re.compile(r"^(?:l|lt|ltr|liter|cl|ml)$", re.I)

_EINHEIT_NORMAL: dict[str, str] = {
    "fl": "Fl", "fla": "Fl", "flasche": "Fl", "flaschen": "Fl", "bottle": "Fl", "bottles": "Fl",
    "btl": "Fl", "btls": "Fl", "bt": "Fl", "er": "Fl",
    "stk": "Stk", "st": "Stk", "stck": "Stk", "stück": "Stk", "stueck": "Stk", "pcs": "Stk", "pc": "Stk",
    "kt": "Kt", "ktn": "Kt", "karton": "Kt", "kartons": "Kt", "kart": "Kt", "krt": "Kt",
    "kiste": "Kt", "kisten": "Kt", "ki": "Kt", "case": "Kt", "cases": "Kt", "ctn": "Kt",
    "pal": "Pal", "palette": "Pal", "paletten": "Pal",
    "pak": "Pak", "paket": "Pak",
    "einheit": "Stk", "einheiten": "Stk",
}  # fmt: skip

_MENGE_ZAHL = r"\d{1,6}(?:[.,]\d{1,3})?"
_MENGE_GEBINDE = re.compile(
    rf"^\s*(?P<n1>{_MENGE_ZAHL})\s*(?P<e1>{EINHEIT_MUSTER})?\.?\s*(?:x|×|\*|à|á|a|je|mal)\s*"
    rf"(?P<n2>{_MENGE_ZAHL})\s*(?P<e2>{EINHEIT_MUSTER}|l|lt|ltr|liter|cl|ml)?\.?\s*$",
    re.I,
)
_MENGE_EINFACH = re.compile(
    rf"^\s*(?P<n>{_MENGE_ZAHL})\s*(?P<e>{EINHEIT_MUSTER})?\.?\s*(?:x|×)?[\s.,;:]*$",
    re.I,
)
_MENGE_SUCHE = re.compile(rf"(?<![\d.,])(?P<n>{_MENGE_ZAHL})\s*(?P<e>{EINHEIT_MUSTER})?(?!\w)", re.I)


@dataclass(frozen=True)
class Menge:
    """Ergebnis von :func:`parse_menge`.

    ``wert`` ist die Gesamtmenge in ``einheit`` (bei Gebinden umgerechnet auf
    Flaschen). ``gebinde``/``je_gebinde`` sind nur bei "12 x 6" oder "2 Kt à 6"
    gefüllt.
    """

    wert: Decimal
    einheit: str | None = None
    gebinde: Decimal | None = None
    je_gebinde: Decimal | None = None


def normalisiere_einheit(text: str | None) -> str | None:
    """Vereinheitlicht Mengeneinheiten: "Fl.", "Flaschen" -> "Fl"; "Stück" -> "Stk"; "Karton" -> "Kt".

    Unbekannte Einheiten werden bereinigt (ohne Punkt) zurückgegeben, Leerwerte und
    reine Trennzeichen ("x") ergeben ``None``.
    """
    if not text:
        return None
    s = text.strip().strip(".").strip()
    if not s or s.casefold() in {"x", "×", "*"}:
        return None
    return _EINHEIT_NORMAL.get(s.casefold(), s)


def parse_menge(text: str | Decimal | int | None) -> Menge | None:
    """Liest eine Menge mit optionaler Einheit.

    >>> parse_menge("60 Fl")
    Menge(wert=Decimal('60'), einheit='Fl', gebinde=None, je_gebinde=None)
    >>> parse_menge("12 x 6").wert
    Decimal('72')
    >>> parse_menge("2 Kt à 6")
    Menge(wert=Decimal('12'), einheit='Fl', gebinde=Decimal('2'), je_gebinde=Decimal('6'))
    >>> parse_menge("-") is None
    True
    """
    if text is None:
        return None
    if isinstance(text, bool):
        return None
    if isinstance(text, Decimal | int):
        return Menge(wert=Decimal(text))
    roh = str(text).strip()
    if roh.casefold() in _LEERWERTE:
        return None

    treffer = _MENGE_GEBINDE.match(roh)
    if treffer:
        n1 = parse_dezimal(treffer.group("n1"))
        n2 = parse_dezimal(treffer.group("n2"))
        e2_roh = treffer.group("e2")
        if n1 is not None and n2 is not None:
            if e2_roh and _VOLUMEN_EINHEIT.match(e2_roh):
                # "6 x 0,75 l": zweite Zahl ist das Volumen, keine Gebindegröße
                return Menge(wert=n1, einheit=normalisiere_einheit(treffer.group("e1")))
            einheit = normalisiere_einheit(e2_roh) or "Fl"
            return Menge(wert=n1 * n2, einheit=einheit, gebinde=n1, je_gebinde=n2)

    treffer = _MENGE_EINFACH.match(roh)
    if treffer:
        wert = parse_dezimal(treffer.group("n"))
        if wert is not None:
            return Menge(wert=wert, einheit=normalisiere_einheit(treffer.group("e")))

    treffer = _MENGE_SUCHE.search(roh)
    if treffer:
        wert = parse_dezimal(treffer.group("n"))
        if wert is not None:
            logger.debug("Menge aus Freitext gelesen: %r -> %s", text, wert)
            return Menge(wert=wert, einheit=normalisiere_einheit(treffer.group("e")))

    logger.debug("Keine Menge in %r", text)
    return None
