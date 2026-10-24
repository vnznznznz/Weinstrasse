from decimal import Decimal

import pytest

from auftragsautomat.matching.volumen import (
    ist_holzkiste,
    ist_sondervariante,
    normalisiere_bezeichnung,
    parse_jahrgang,
    parse_volumen_cl,
)


@pytest.mark.parametrize(
    "text, erwartet",
    [
        ("Gavi di Gavi 2023 0,75 l", Decimal("75")),
        ("Gavi di Gavi 2023 0.75L", Decimal("75")),
        ("Riesling 750 ml", Decimal("75")),
        ("Riesling 75 cl", Decimal("75")),
        ("Gloria 0,375 l", Decimal("37.5")),
        ("Gloria 0,375", Decimal("37.5")),
        ("Gloria 375 ml", Decimal("37.5")),
        ("Rosato 1,5 l Magnum", Decimal("150")),
        ("Rosato Magnum", Decimal("150")),
        ("Barolo 3,0 l", Decimal("300")),
        ("Nero d'Avola 2022", None),
        ("Gloria Secco 6,90 €", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_volumen_cl(text, erwartet):
    assert parse_volumen_cl(text) == erwartet


@pytest.mark.parametrize(
    "text, erwartet",
    [
        ("Colosi Nero d'Avola 2022 6er Holzkiste", True),
        ("Barolo OHK", True),
        ("Barolo 1 HK", True),
        ("Barolo 2019 0,75 l", False),
        ("Hkl Weinbrand", False),  # "Hkl" ist kein eigenes Wort "HK"
    ],
)
def test_ist_holzkiste(text, erwartet):
    assert ist_holzkiste(text) is erwartet


def test_ist_sondervariante():
    assert ist_sondervariante(Decimal("75")) is False
    assert ist_sondervariante(None) is False
    assert ist_sondervariante(Decimal("37.5")) is True
    assert ist_sondervariante(Decimal("150")) is True
    assert ist_sondervariante(Decimal("75"), holzkiste=True) is True


def test_parse_jahrgang():
    assert parse_jahrgang("Colosi Nero d'Avola Sicilia DOC 2022 0,75 l") == 2022
    assert parse_jahrgang("Gloria Secco Frizzante") is None
    assert parse_jahrgang("Artikel 12022") is None


@pytest.mark.parametrize(
    "text, erwartet",
    [
        ("Villa Armellina Gloria Secco Frizzante 0,75 l", "villa armellina gloria secco frizzante"),
        ("Colosi Nero d'Avola Sicilia DOC 2022 0,75 l", "colosi nero davola sicilia doc"),
        ("Colosi Nero d'Avola 2022 6er Holzkiste", "colosi nero davola"),
        ("Grüner Veltliner Smaragd 2021 0,75 l", "gruener veltliner smaragd"),
        ("  Bollina  Gavi di Gavi DOCG  2023 ", "bollina gavi di gavi docg"),
    ],
)
def test_normalisiere_bezeichnung(text, erwartet):
    assert normalisiere_bezeichnung(text) == erwartet


@pytest.mark.parametrize(
    "text, erwartet",
    [
        # Regression (Integration Stufe 2): Abkürzung mit Punkt direkt vor der Zahl ("htr." = halbtrocken)
        ("Quinta Beispiel Vinho Verde DOC htr.0,75l", Decimal("75")),
        ("Rosé htr.0,375 l", Decimal("37.5")),
        ("Beispielwein Rosso x.750ml", Decimal("75")),
        # ein Punkt hinter einer Ziffer bleibt Dezimal-/Tausenderpunkt und ist kein Volumenbeginn
        ("1.234,50 l", None),
        ("Preis 1.0,75", None),
    ],
)
def test_parse_volumen_punkt_vor_zahl(text, erwartet):
    assert parse_volumen_cl(text) == erwartet


def test_normalisiere_bezeichnung_entfernt_volumen_nach_abkuerzung():
    """Regression: "htr.0,75l" blieb als "htr 0 75l" stehen und verfälschte das Fuzzy-Matching."""
    assert normalisiere_bezeichnung("Quinta Beispiel Vinho Verde DOC htr.0,75l") == "quinta beispiel vinho verde doc htr"
