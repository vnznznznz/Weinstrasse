from datetime import date
from decimal import Decimal

import pytest

from auftragsautomat.extraktion.zahlen import (
    Menge,
    finde_datum,
    normalisiere_einheit,
    parse_datum,
    parse_dezimal,
    parse_jahrgang,
    parse_menge,
)


@pytest.mark.parametrize(
    "text, erwartet",
    [
        ("1.234,50", Decimal("1234.50")),
        ("1234.50", Decimal("1234.50")),
        ("1,234.50", Decimal("1234.50")),
        ("1.234.567", Decimal("1234567")),
        ("1 234,50", Decimal("1234.50")),
        ("6,90 EUR", Decimal("6.90")),
        ("6,90 €", Decimal("6.90")),
        ("6,90€", Decimal("6.90")),
        ("€ 6,90", Decimal("6.90")),
        ("EUR6,90", Decimal("6.90")),
        ("8.40", Decimal("8.40")),
        ("0.375", Decimal("0.375")),
        ("1.234", Decimal("1234")),  # deutscher Tausenderpunkt
        ("0,00", Decimal("0.00")),
        ("60", Decimal("60")),
        ("60.", Decimal("60")),
        ("-5,00", Decimal("-5.00")),
        ("5,00-", Decimal("-5.00")),
        ("(5,00)", Decimal("-5.00")),
        # Regression (Review Befund 16): ",-" ist die Preisschreibweise für glatte Beträge, kein SAP-Minus
        ("12,-", Decimal("12")),
        ("12,- €", Decimal("12")),
        ("1.000,-", Decimal("1000")),
        ("12,– EUR", Decimal("12")),
        ("19 %", Decimal("19")),
        ("ca. 60 Fl", Decimal("60")),
        ("-", None),
        ("–", None),
        ("", None),
        ("   ", None),
        (None, None),
        ("abc", None),
        ("n/a", None),
    ],
)
def test_parse_dezimal(text, erwartet):
    ergebnis = parse_dezimal(text)
    assert ergebnis == erwartet
    if ergebnis is not None:
        assert isinstance(ergebnis, Decimal)


def test_preis_mit_strich_in_der_position_ist_positiv():
    """Regression (Review Befund 16): '12,-' in der Preisspalte darf keinen negativen Dokumentpreis ergeben."""
    from auftragsautomat.extraktion.text_pdf import _baue_position

    position = _baue_position(bezeichnung="Gavi di Gavi 2023 0,75 l", menge="12", preis="12,-", rohtext="r")
    assert position is not None and position.preis == Decimal("12")


def test_parse_dezimal_decimal_und_int_durchreichen():
    assert parse_dezimal(Decimal("1.5")) == Decimal("1.5")
    assert parse_dezimal(12) == Decimal("12")
    assert parse_dezimal(True) is None


@pytest.mark.parametrize(
    "text, erwartet",
    [
        ("15.09.2026", date(2026, 9, 15)),
        ("15.9.26", date(2026, 9, 15)),
        ("1.10.26", date(2026, 10, 1)),
        ("2026-09-15", date(2026, 9, 15)),
        ("15. September 2026", date(2026, 9, 15)),
        ("15. Sept. 2026", date(2026, 9, 15)),
        ("15 Oktober 2026", date(2026, 10, 15)),
        ("3. März 2026", date(2026, 3, 3)),
        ("15/09/2026", date(2026, 9, 15)),
        ("Datum: 15.09.2026 10:30 Uhr", date(2026, 9, 15)),
        ("Bottrop, den 15.10.2026", date(2026, 10, 15)),
        ("31.02.2026", None),  # ungültig
        ("KW 43", None),
        ("Lieferung in der 42. Woche 2026", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_datum(text, erwartet):
    assert parse_datum(text) == erwartet


def test_parse_datum_date_durchreichen():
    assert parse_datum(date(2026, 1, 2)) == date(2026, 1, 2)


def test_finde_datum_liefert_fundstelle():
    assert finde_datum("Liefertermin: 24.10.2026 frei Haus") == (date(2026, 10, 24), "24.10.2026")
    assert finde_datum("ohne Datum") is None


def test_parse_jahrgang():
    assert parse_jahrgang("Colosi Nero d'Avola Sicilia DOC 2022 0,75 l") == 2022
    assert parse_jahrgang("Villa Armellina Gloria Secco Frizzante") is None
    assert parse_jahrgang(None) is None


@pytest.mark.parametrize(
    "text, wert, einheit",
    [
        ("60", Decimal("60"), None),
        ("60 Fl", Decimal("60"), "Fl"),
        ("60 Fl.", Decimal("60"), "Fl"),
        ("12 Flaschen", Decimal("12"), "Fl"),
        ("48 Flaschen,", Decimal("48"), "Fl"),
        ("6 Stück", Decimal("6"), "Stk"),
        ("1 Stk", Decimal("1"), "Stk"),
        ("3 Kt", Decimal("3"), "Kt"),
        ("2 Kartons", Decimal("2"), "Kt"),
        ("36 x", Decimal("36"), None),
        ("1,5", Decimal("1.5"), None),
        ("1.000 Fl", Decimal("1000"), "Fl"),
        ("Menge: 60 Fl", Decimal("60"), "Fl"),
        ("60 Bottles", Decimal("60"), "Fl"),
    ],
)
def test_parse_menge_einfach(text, wert, einheit):
    menge = parse_menge(text)
    assert menge is not None
    assert menge.wert == wert
    assert menge.einheit == einheit
    assert menge.gebinde is None


@pytest.mark.parametrize(
    "text, wert, gebinde, je_gebinde",
    [
        ("12 x 6", Decimal("72"), Decimal("12"), Decimal("6")),
        ("2 Kt à 6", Decimal("12"), Decimal("2"), Decimal("6")),
        ("2 x 6er", Decimal("12"), Decimal("2"), Decimal("6")),
        ("3 Kartons à 12 Fl", Decimal("36"), Decimal("3"), Decimal("12")),
    ],
)
def test_parse_menge_gebinde(text, wert, gebinde, je_gebinde):
    menge = parse_menge(text)
    assert menge == Menge(wert=wert, einheit="Fl", gebinde=gebinde, je_gebinde=je_gebinde)


def test_parse_menge_volumen_ist_keine_gebindegroesse():
    menge = parse_menge("6 x 0,75 l")
    assert menge is not None
    assert menge.wert == Decimal("6")
    assert menge.gebinde is None


@pytest.mark.parametrize("text", ["", "-", None, "abc", "   "])
def test_parse_menge_leer(text):
    assert parse_menge(text) is None


def test_parse_menge_zahlentypen():
    assert parse_menge(Decimal("7")) == Menge(wert=Decimal("7"))
    assert parse_menge(7) == Menge(wert=Decimal("7"))


@pytest.mark.parametrize(
    "text, erwartet",
    [
        ("Fl.", "Fl"),
        ("Flaschen", "Fl"),
        ("Stück", "Stk"),
        ("St.", "Stk"),
        ("Karton", "Kt"),
        ("Kartons", "Kt"),
        ("Kiste", "Kt"),
        ("Pal", "Pal"),
        ("x", None),
        ("", None),
        (None, None),
        ("Dose", "Dose"),  # unbekannt: bereinigt durchreichen
    ],
)
def test_normalisiere_einheit(text, erwartet):
    assert normalisiere_einheit(text) == erwartet
