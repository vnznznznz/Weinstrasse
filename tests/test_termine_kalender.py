"""Tests für termine.kalender (Werktagsrechnung mit Feiertagen).

Bezugszeitraum Mai/Juni 2026: Christi Himmelfahrt 14.05. (Do), Pfingstmontag 25.05. (Mo),
Fronleichnam 04.06. (Do, u. a. in NW, nicht in BE).
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from auftragsautomat.termine import kalender
from auftragsautomat.termine.kalender import (
    KalenderFehler,
    addiere_werktage,
    feiertag,
    ist_feiertag,
    ist_werktag,
    ist_wochenende,
    kw,
    naechster_werktag,
    vorheriger_werktag,
    werktage_im_zeitraum,
    werktage_zwischen,
    wochentag_kurz,
)

MO = date(2026, 5, 11)
MI = date(2026, 5, 13)
HIMMELFAHRT = date(2026, 5, 14)
FR = date(2026, 5, 15)
SA = date(2026, 5, 16)
SO = date(2026, 5, 17)
PFINGSTMONTAG = date(2026, 5, 25)
FRONLEICHNAM = date(2026, 6, 4)


@pytest.mark.parametrize(
    ("datum", "erwartet"),
    [(MO, True), (MI, True), (HIMMELFAHRT, False), (FR, True), (SA, False), (SO, False), (PFINGSTMONTAG, False)],
)
def test_ist_werktag(datum: date, erwartet: bool):
    assert ist_werktag(datum) is erwartet
    assert ist_werktag(datum, "NW") is erwartet


def test_feiertag_je_bundesland():
    assert feiertag(HIMMELFAHRT) == "Christi Himmelfahrt"
    assert ist_feiertag(FRONLEICHNAM, "NW") is True
    assert ist_feiertag(FRONLEICHNAM, "BE") is False  # Fronleichnam gilt nicht in Berlin
    assert ist_werktag(FRONLEICHNAM, "BE") is True
    assert ist_werktag(FRONLEICHNAM, "NW") is False
    assert ist_feiertag(FR) is False and feiertag(FR) is None
    assert feiertag(HIMMELFAHRT, "") == "Christi Himmelfahrt"  # leer = nur bundesweite Feiertage
    assert ist_feiertag(FRONLEICHNAM, "") is False


def test_datetime_wird_wie_date_behandelt():
    assert ist_werktag(datetime(2026, 5, 14, 9, 30)) is False
    assert ist_wochenende(datetime(2026, 5, 16, 23, 59)) is True
    assert kw(datetime(2026, 5, 11, 0, 0)) == 20
    with pytest.raises(KalenderFehler):
        ist_werktag("2026-05-11")  # type: ignore[arg-type]


def test_werktage_zwischen_ueberspringt_feiertag_und_wochenende():
    assert werktage_zwischen(MO, FR) == 3  # Di, Mi, Fr (Do = Himmelfahrt)
    assert werktage_zwischen(MO, MO) == 0
    assert werktage_zwischen(FR, MO) == -3
    assert werktage_zwischen(date(2026, 5, 22), date(2026, 5, 26)) == 1  # Sa, So, Pfingstmontag, Di
    assert werktage_zwischen(MI, SO) == 1  # nur der Freitag
    assert werktage_zwischen(date(2026, 6, 3), date(2026, 6, 5), "NW") == 1
    assert werktage_zwischen(date(2026, 6, 3), date(2026, 6, 5), "BE") == 2


def test_addiere_werktage_vor_und_zurueck():
    assert addiere_werktage(MI, 1) == FR  # Himmelfahrt übersprungen
    assert addiere_werktage(FR, -1) == MI
    assert addiere_werktage(date(2026, 5, 22), 1) == date(2026, 5, 26)  # Wochenende + Pfingstmontag
    assert addiere_werktage(MO, 10) == date(2026, 5, 27)
    assert addiere_werktage(date(2026, 5, 27), -10) == MO
    assert addiere_werktage(SA, 0) == SA  # 0 lässt das Datum unverändert, auch am Wochenende
    assert addiere_werktage(datetime(2026, 5, 13, 12, 0), 1) == FR


def test_naechster_und_vorheriger_werktag():
    assert naechster_werktag(SA) == date(2026, 5, 18)
    assert naechster_werktag(date(2026, 5, 23)) == date(2026, 5, 26)  # Sa -> Di (Pfingstmontag)
    assert naechster_werktag(MO) == MO
    assert vorheriger_werktag(SO) == FR
    assert vorheriger_werktag(HIMMELFAHRT) == MI


def test_werktage_im_zeitraum_und_kw():
    assert werktage_im_zeitraum(MO, FR) == [MO, date(2026, 5, 12), MI, FR]
    assert werktage_im_zeitraum(FR, MO) == []
    assert kw(MO) == 20 and kw(date(2026, 6, 5)) == 23
    assert kw(date(2027, 1, 1)) == 53  # ISO-Woche: 1.1.2027 gehört noch zur KW 53/2026
    assert wochentag_kurz(MO) == "Mo" and wochentag_kurz(SO) == "So"


def test_unbekanntes_bundesland():
    with pytest.raises(KalenderFehler, match="Unbekanntes Bundesland 'XX'"):
        ist_werktag(MO, "XX")


def test_standard_bundesland_passt_zur_konfiguration(settings):
    assert kalender.STANDARD_BUNDESLAND == settings.ueberwachung.bundesland == "NW"
