"""Werktagsrechnung für die Fixterminüberwachung (Wochenenden und gesetzliche Feiertage).

Feiertage kommen aus dem Paket ``holidays`` (Land DE, Bundesland aus
``settings.ueberwachung.bundesland``, Standard ``"NW"``). Alle Funktionen nehmen ``date`` oder
``datetime`` an und rechnen mit dem Kalendertag.

* :func:`ist_werktag` – Montag bis Freitag und kein Feiertag.
* :func:`werktage_zwischen` – Anzahl der Werktage nach ``a`` bis einschließlich ``b``
  ("x Werktage vor dem Fixtermin"); negativ, wenn ``b`` vor ``a`` liegt.
* :func:`addiere_werktage` – ``n`` Werktage vor- oder zurückrechnen.
* :func:`kw` – ISO-Kalenderwoche (Spalte "KW" der Fixterminüberwachung).
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from functools import lru_cache

import holidays

__all__ = [
    "KalenderFehler",
    "STANDARD_BUNDESLAND",
    "WOCHENTAGE_KURZ",
    "addiere_werktage",
    "feiertag",
    "ist_feiertag",
    "ist_werktag",
    "ist_wochenende",
    "kw",
    "naechster_werktag",
    "vorheriger_werktag",
    "werktage_im_zeitraum",
    "werktage_zwischen",
    "wochentag_kurz",
]

logger = logging.getLogger(__name__)

#: Bundesland, wenn die Konfiguration keines nennt (Sitz des Anwenders: Nordrhein-Westfalen).
STANDARD_BUNDESLAND = "NW"
#: Kurzbezeichnungen der Wochentage wie in Spalte "Tag" der Fixterminüberwachung (Montag = 0).
WOCHENTAGE_KURZ: tuple[str, ...] = ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")


class KalenderFehler(ValueError):
    """Ungültige Angabe für die Werktagsrechnung (z. B. unbekanntes Bundesland)."""


def _als_datum(wert: date | datetime) -> date:
    if isinstance(wert, datetime):
        return wert.date()
    if isinstance(wert, date):
        return wert
    raise KalenderFehler(f"Kein Datum: {wert!r}")


@lru_cache(maxsize=128)
def _feiertage(jahr: int, bundesland: str) -> holidays.HolidayBase:
    """Feiertagskalender eines Jahres (gecacht; ``bundesland`` leer = nur bundesweite Feiertage)."""
    try:
        return holidays.country_holidays("DE", subdiv=bundesland or None, years=jahr)
    except NotImplementedError as exc:
        gueltig = ", ".join(sorted(holidays.DE.subdivisions))
        raise KalenderFehler(
            f"Unbekanntes Bundesland '{bundesland}' in ueberwachung.bundesland; gültig sind: {gueltig}"
        ) from exc


def feiertag(datum: date | datetime, bundesland: str = STANDARD_BUNDESLAND) -> str | None:
    """Name des gesetzlichen Feiertags an diesem Tag, sonst ``None``."""
    tag = _als_datum(datum)
    name = _feiertage(tag.year, bundesland or "").get(tag)
    return str(name) if name else None


def ist_feiertag(datum: date | datetime, bundesland: str = STANDARD_BUNDESLAND) -> bool:
    return feiertag(datum, bundesland) is not None


def ist_wochenende(datum: date | datetime) -> bool:
    return _als_datum(datum).weekday() >= 5


def ist_werktag(datum: date | datetime, bundesland: str = STANDARD_BUNDESLAND) -> bool:
    """Montag bis Freitag und kein gesetzlicher Feiertag im Bundesland."""
    tag = _als_datum(datum)
    return not ist_wochenende(tag) and not ist_feiertag(tag, bundesland)


def wochentag_kurz(datum: date | datetime) -> str:
    """``"Mo"`` … ``"So"`` (Spalte "Tag")."""
    return WOCHENTAGE_KURZ[_als_datum(datum).weekday()]


def kw(datum: date | datetime) -> int:
    """ISO-Kalenderwoche (Spalte "KW"); der Jahreswechsel folgt der ISO-Regel (Donnerstag)."""
    return _als_datum(datum).isocalendar()[1]


def werktage_im_zeitraum(a: date | datetime, b: date | datetime, bundesland: str = STANDARD_BUNDESLAND) -> list[date]:
    """Alle Werktage von ``a`` bis ``b`` (beide einschließlich), aufsteigend; leer, wenn ``b`` vor ``a``."""
    von, bis = _als_datum(a), _als_datum(b)
    tage: list[date] = []
    tag = von
    while tag <= bis:
        if ist_werktag(tag, bundesland):
            tage.append(tag)
        tag += timedelta(days=1)
    return tage


def werktage_zwischen(a: date | datetime, b: date | datetime, bundesland: str = STANDARD_BUNDESLAND) -> int:
    """Anzahl der Werktage **nach** ``a`` bis **einschließlich** ``b``.

    Beispiel: Montag -> Freitag derselben Woche = 4; Freitag -> Montag = 1. Liegt ``b`` vor ``a``,
    ist das Ergebnis negativ (Werktage nach ``b`` bis einschließlich ``a``, mit Minuszeichen).
    Damit gilt ``werktage_zwischen(heute, fixtermin) >= 10`` für "mindestens 10 Werktage vor dem Fixtermin".
    """
    von, bis = _als_datum(a), _als_datum(b)
    if bis < von:
        return -werktage_zwischen(bis, von, bundesland)
    return len(werktage_im_zeitraum(von + timedelta(days=1), bis, bundesland))


def addiere_werktage(datum: date | datetime, n: int, bundesland: str = STANDARD_BUNDESLAND) -> date:
    """``n`` Werktage vor (``n < 0``) oder zurück (``n > 0``) rechnen; ``n == 0`` liefert das Datum unverändert."""
    tag = _als_datum(datum)
    schritt = 1 if n > 0 else -1
    rest = abs(n)
    while rest > 0:
        tag += timedelta(days=schritt)
        if ist_werktag(tag, bundesland):
            rest -= 1
    return tag


def naechster_werktag(datum: date | datetime, bundesland: str = STANDARD_BUNDESLAND) -> date:
    """Das Datum selbst, wenn es ein Werktag ist, sonst der nächste Werktag danach."""
    tag = _als_datum(datum)
    while not ist_werktag(tag, bundesland):
        tag += timedelta(days=1)
    return tag


def vorheriger_werktag(datum: date | datetime, bundesland: str = STANDARD_BUNDESLAND) -> date:
    """Das Datum selbst, wenn es ein Werktag ist, sonst der letzte Werktag davor."""
    tag = _als_datum(datum)
    while not ist_werktag(tag, bundesland):
        tag -= timedelta(days=1)
    return tag
