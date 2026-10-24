"""Volumen, Holzkisten und Normalisierung von Artikelbezeichnungen.

Standardvolumen ist 75 cl. 37,5 cl, 1,5 l und Holzkisten sind Sondervarianten,
die nie automatisch gewählt werden dürfen (siehe ``Artikel.ist_sondervariante``).
"""

from __future__ import annotations

import re
import unicodedata
from decimal import Decimal, InvalidOperation

STANDARD_VOLUMEN_CL = Decimal("75")

#: Lookbehind vor einer Volumenzahl: keine Ziffer, kein Komma, kein Punkt hinter einer Ziffer.
_VOR_ZAHL = r"(?<![\d,])(?<!\d\.)"

# Reihenfolge ist wichtig: spezifische Muster zuerst.
_VOLUMEN_MUSTER: list[tuple[re.Pattern[str], Decimal | None]] = [
    # Wortformen
    (re.compile(r"\b(doppelmagnum|double\s*magnum)\b", re.I), Decimal("300")),
    (re.compile(r"\bjeroboam\b", re.I), Decimal("300")),
    (re.compile(r"\bmagnum\b", re.I), Decimal("150")),
    (re.compile(r"\b(halbe\s+flasche|halbflasche|demi|piccolo)\b", re.I), Decimal("37.5")),
    # Zahl + Einheit (deutsche oder englische Dezimaltrennung). Vor der Zahl darf keine Ziffer, kein
    # Komma und kein Dezimalpunkt einer Zahl stehen ("1.234,50"); ein Punkt nach einem Wort zählt nicht
    # als Dezimalpunkt, damit Abkürzungen wie "htr.0,75l" (halbtrocken) gelesen werden.
    (re.compile(_VOR_ZAHL + r"(\d+(?:[.,]\d+)?)\s*(?:ml|milliliter)\b", re.I), None),
    (re.compile(_VOR_ZAHL + r"(\d+(?:[.,]\d+)?)\s*(?:cl|zentiliter|centiliter)\b", re.I), None),
    (re.compile(_VOR_ZAHL + r"(\d+(?:[.,]\d+)?)\s*(?:l|lt|ltr|liter|litre)\b(?!\w)", re.I), None),
    # Nackte Dezimalzahl, die nur ein Volumen sein kann: 0,75 / 0,375 / 1,5 / 0,5 / 0,2
    (re.compile(_VOR_ZAHL + r"(0[.,]\d{1,3}|1[.,]5|3[.,]0)(?![\d,.%])(?!\s*(?:€|eur|%))", re.I), None),
]

_HOLZKISTE_MUSTER = re.compile(
    r"(?<![\w])(holzkiste|holzkisten|holzbox|ohk|hk|kiste|kisten|wooden\s*case|cassa\s*legno|caisse\s*bois)(?![\w])",
    re.I,
)

_JAHRGANG_MUSTER = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")


def _zu_decimal(text: str) -> Decimal | None:
    try:
        return Decimal(text.replace(",", "."))
    except InvalidOperation:
        return None


def parse_volumen_cl(text: str | None) -> Decimal | None:
    """Liest ein Flaschenvolumen aus einem Text und liefert es in cl.

    >>> parse_volumen_cl("Gavi di Gavi 2023 0,75 l")
    Decimal('75')
    >>> parse_volumen_cl("Rosato 1,5 l Magnum")
    Decimal('150')
    >>> parse_volumen_cl("Gloria 0,375")
    Decimal('37.5')
    >>> parse_volumen_cl("Riesling 750 ml")
    Decimal('75')
    >>> parse_volumen_cl("Nero d'Avola 2022") is None
    True
    """
    if not text:
        return None
    for muster, fest in _VOLUMEN_MUSTER:
        treffer = muster.search(text)
        if not treffer:
            continue
        if fest is not None:
            return fest
        wert = _zu_decimal(treffer.group(1))
        if wert is None or wert <= 0:
            continue
        einheit = treffer.group(0)[len(treffer.group(1)) :].strip().lower()
        if einheit.startswith("ml") or einheit.startswith("milli"):
            cl = wert / Decimal(10)
        elif einheit.startswith("cl") or einheit.startswith("zenti") or einheit.startswith("centi"):
            cl = wert
        elif einheit:  # l, lt, ltr, liter
            cl = wert * Decimal(100)
        else:
            # nackte Dezimalzahl: < 5 wird als Liter gelesen
            cl = wert * Decimal(100) if wert < 5 else wert
        return _normalisiere_decimal(cl)
    return None


def _normalisiere_decimal(wert: Decimal) -> Decimal:
    wert = wert.normalize()
    # Decimal('75E+1') vermeiden
    if wert == wert.to_integral_value():
        return Decimal(int(wert))
    return wert


def ist_holzkiste(text: str | None) -> bool:
    """Erkennt Holzkisten-Varianten ("Holzkiste", "OHK", "HK", "6er Kiste")."""
    if not text:
        return False
    return bool(_HOLZKISTE_MUSTER.search(text))


def ist_sondervariante(volumen_cl: Decimal | None, holzkiste: bool = False) -> bool:
    if holzkiste:
        return True
    return volumen_cl is not None and volumen_cl != STANDARD_VOLUMEN_CL


def parse_jahrgang(text: str | None) -> int | None:
    """Liest einen vierstelligen Jahrgang (1900–2099) aus einem Text."""
    if not text:
        return None
    treffer = _JAHRGANG_MUSTER.search(text)
    return int(treffer.group(1)) if treffer else None


def _ohne_akzente(text: str) -> str:
    text = text.replace("ß", "ss").replace("ä", "ae").replace("ö", "oe").replace("ü", "ue")
    return "".join(
        c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)
    )


def normalisiere_bezeichnung(text: str | None) -> str:
    """Normalisiert eine Artikelbezeichnung für das Fuzzy-Matching.

    Entfernt Volumen, Jahrgang, Mengeneinheiten, Holzkisten-Hinweise, Satzzeichen und
    Akzente; alles in Kleinbuchstaben, einfache Leerzeichen.

    >>> normalisiere_bezeichnung("Villa Armellina Gloria Secco Frizzante 0,75 l")
    'villa armellina gloria secco frizzante'
    >>> normalisiere_bezeichnung("Colosi Nero d'Avola Sicilia DOC 2022 0,75 l")
    'colosi nero davola sicilia doc'
    """
    if not text:
        return ""
    t = _ohne_akzente(text)
    t = t.lower()
    for muster, _ in _VOLUMEN_MUSTER:
        t = muster.sub(" ", t)
    t = _HOLZKISTE_MUSTER.sub(" ", t)
    t = _JAHRGANG_MUSTER.sub(" ", t)
    t = re.sub(r"\b\d+\s*(?:er|x)\b", " ", t)  # "6er", "12 x"
    t = re.sub(r"\b(fl|flasche|flaschen|stk|stueck|kt|karton|kartons|btl|bottle|bottles)\b", " ", t)
    t = re.sub(r"['’`´]", "", t)  # d'Avola -> davola
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()
