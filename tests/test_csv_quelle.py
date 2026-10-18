"""Tests für stammdaten.csv_quelle (CSV-Exporte, Parser, Zeilenumwandler)."""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from auftragsautomat.config import CsvQuellen, Settings
from auftragsautomat.models import Belegart
from auftragsautomat.stammdaten.basis import Stammdaten, StammdatenQuelle
from auftragsautomat.stammdaten.csv_quelle import (
    CsvStammdatenQuelle,
    StammdatenFehler,
    als_text,
    feldnamen,
    lies_csv_zeilen,
    parse_belegart,
    parse_bool,
    parse_datum,
    parse_dezimal,
    parse_int,
    zeile_zu_artikel,
    zeile_zu_beleg,
    zeile_zu_kunde,
    zeile_zu_lieferant,
)

ARTIKEL_KOPF = "artikelnummer;bezeichnung;warengruppe;lieferantennummer;volumen_cl;holzkiste;jahrgang;ek_preis;vk_preis_1;einheit;matchcode;aktiv"


def _schreibe(pfad: Path, text: str, kodierung: str = "cp1252") -> Path:
    pfad.parent.mkdir(parents=True, exist_ok=True)
    pfad.write_bytes(text.encode(kodierung))
    return pfad


def _konfig(ordner: Path, **extra: object) -> CsvQuellen:
    return CsvQuellen(
        artikel=ordner / "artikel.csv",
        kunden=ordner / "kunden.csv",
        lieferanten=ordner / "lieferanten.csv",
        belege=ordner / "belege.csv",
        **extra,  # type: ignore[arg-type]
    )


@pytest.fixture()
def wik_leh(settings: Settings) -> CsvStammdatenQuelle:
    firma = settings.firma("wik_leh")
    assert firma.stammdaten.csv is not None
    return CsvStammdatenQuelle(firma.id, firma.stammdaten.csv)


# ---------------------------------------------------------------------------
# Fixtures aus tests/fixtures/stammdaten/wik_leh
# ---------------------------------------------------------------------------


def test_quelle_erfuellt_protokoll(wik_leh: CsvStammdatenQuelle) -> None:
    assert isinstance(wik_leh, StammdatenQuelle)
    assert wik_leh.firma_id == "wik_leh"


def test_wik_leh_anzahlen(wik_leh: CsvStammdatenQuelle) -> None:
    assert len(wik_leh.artikel()) == 13
    assert len(wik_leh.kunden()) == 5
    assert len(wik_leh.lieferanten()) == 4
    assert len(wik_leh.belege()) == 3


def test_wik_leh_artikel_werte(wik_leh: CsvStammdatenQuelle) -> None:
    artikel = {a.artikelnummer: a for a in wik_leh.artikel()}
    gloria = artikel["1643"]
    assert gloria.firma_id == "wik_leh"
    assert gloria.ek_preis == Decimal("4.20")
    assert gloria.vk_preis_1 == Decimal("6.90")
    assert isinstance(gloria.ek_preis, Decimal)
    assert gloria.volumen_cl == Decimal("75")
    assert gloria.holzkiste is False
    assert gloria.jahrgang is None
    assert gloria.warengruppe == "Cide"
    assert gloria.lieferantennummer == "72"
    assert gloria.einheit == "Fl"
    assert gloria.matchcode == "GLORIA"
    assert gloria.aktiv is True
    assert gloria.ist_sondervariante is False
    # volumen_cl leer -> aus der Bezeichnung "0,75 l"
    assert artikel["2005"].volumen_cl == Decimal("75")
    # Holzkiste 1 -> True, Sondervariante
    assert artikel["2004"].holzkiste is True
    assert artikel["2004"].ist_sondervariante is True
    assert artikel["2004"].jahrgang == 2022
    # 0,375 l -> Sondervariante
    assert artikel["1644"].volumen_cl == Decimal("37.5")
    assert artikel["1644"].ist_sondervariante is True
    # 1,5 l Magnum
    assert artikel["958"].volumen_cl == Decimal("150")
    assert artikel["958"].ist_sondervariante is True
    # inaktiv
    assert artikel["3003"].aktiv is False
    assert artikel["2001"].jahrgang == 2022


def test_wik_leh_kunden_mit_umlauten(wik_leh: CsvStammdatenQuelle) -> None:
    kunden = {k.kundennummer: k for k in wik_leh.kunden()}
    hegmann = kunden["3441"]
    assert hegmann.name == "Hegmann"
    assert hegmann.name2 == "Weinmarkt Bottrop-Kirchhellen"
    assert hegmann.strasse == "Hauptstraße 1"  # cp1252 korrekt dekodiert
    assert hegmann.plz == "46244"
    assert hegmann.preisgruppe == 1
    assert kunden["2200"].preisgruppe == 2
    assert kunden["1392"].ort == "Mönchengladbach"
    assert kunden["2300"].name == "Weinhandlung Müller-Lüdenscheid"


def test_wik_leh_lieferanten(wik_leh: CsvStammdatenQuelle) -> None:
    lieferanten = {lf.lieferantennummer: lf for lf in wik_leh.lieferanten()}
    assert lieferanten["72"].name == "Cide srl"
    assert lieferanten["72"].name2 == "Villa Armellina"
    assert lieferanten["150"].ort == "Bad Dürkheim"
    assert lieferanten["138"].name2 is None


def test_wik_leh_belege(wik_leh: CsvStammdatenQuelle) -> None:
    belege = wik_leh.belege()
    ab = belege[0]
    assert ab.belegart is Belegart.AUFTRAGSBESTAETIGUNG
    assert ab.belegnummer == "AB-2026-0101"
    assert ab.kundenbestellnummer == "B-4711"
    assert ab.partnernummer == "3441"
    assert ab.datum == date(2026, 9, 15)
    assert ab.netto == Decimal("414.00")
    be = belege[1]
    assert be.belegart is Belegart.BESTELLUNG
    assert be.kundenbestellnummer is None
    assert be.partnernummer == "72"


def test_stammdaten_aus_quelle(wik_leh: CsvStammdatenQuelle) -> None:
    daten = Stammdaten.aus_quelle(wik_leh)
    assert daten.artikel_nach_nummer["2005"].volumen_cl == Decimal("75")
    assert daten.kunden_nach_nummer["1392"].name == "Weinhaus Menrath"
    assert daten.lieferanten_nach_nummer["87"].name == "Cantine Colosi"


def test_alle_firmen_ladbar(settings: Settings) -> None:
    for firma in settings.firmen.values():
        assert firma.stammdaten.csv is not None
        quelle = CsvStammdatenQuelle(firma.id, firma.stammdaten.csv)
        daten = Stammdaten.aus_quelle(quelle)
        assert daten.firma_id == firma.id
        assert len(daten.artikel) >= 1


# ---------------------------------------------------------------------------
# Spaltenzuordnung, Pflichtspalten, Kodierung
# ---------------------------------------------------------------------------


def test_spaltenzuordnung_case_insensitiv_und_bom(tmp_path: Path) -> None:
    # Lexware-Exportspalten, anders benannt, mit UTF-8-BOM und gemischter Schreibweise
    text = (
        "Artikelnummer;BEZEICHNUNG;Warengruppe;Lieferantennummer 1;EK Preis Lf.1;Preis 1;Gesperrt;Unbekannt\n"
        '"4711";"Gavi di Gavi 2023 0,75 l";"Bollina";"138";"7,80";"12,50";"N";"egal"\n'
        '"4712";"Gavi di Gavi 2023 0,375 l";"Bollina";"138";"4,50";"7,20";"J";"egal"\n'
    )
    _schreibe(tmp_path / "artikel.csv", text, "utf-8-sig")
    konfig = _konfig(
        tmp_path,
        spalten_artikel={
            "lieferantennummer": "Lieferantennummer 1",
            "ek_preis": "EK Preis Lf.1",
            "vk_preis_1": "Preis 1",
            "gesperrt": "Gesperrt",
        },
    )
    artikel = CsvStammdatenQuelle("test", konfig).artikel()
    assert [a.artikelnummer for a in artikel] == ["4711", "4712"]
    assert artikel[0].ek_preis == Decimal("7.80")
    assert artikel[0].vk_preis_1 == Decimal("12.50")
    assert artikel[0].lieferantennummer == "138"
    assert artikel[0].aktiv is True
    assert artikel[0].volumen_cl == Decimal("75")
    assert artikel[0].jahrgang == 2023
    assert artikel[1].aktiv is False  # Gesperrt = J
    assert artikel[1].volumen_cl == Decimal("37.5")


def test_fehlende_pflichtspalte(tmp_path: Path) -> None:
    _schreibe(tmp_path / "kunden.csv", "kundennummer;ort\n1;Essen\n")
    with pytest.raises(StammdatenFehler) as info:
        CsvStammdatenQuelle("test", _konfig(tmp_path)).kunden()
    meldung = str(info.value)
    assert "kunden.csv" in meldung
    assert "name" in meldung
    assert "kundennummer" in meldung
    assert "Gefundene Spalten" in meldung


def test_fehlende_pflichtspalte_mit_zuordnung_nennt_exportspalte(tmp_path: Path) -> None:
    _schreibe(tmp_path / "lieferanten.csv", "Lieferantennummer;Ort\n1;Gavi\n")
    konfig = _konfig(tmp_path, spalten_lieferanten={"name": "Firma"})
    with pytest.raises(StammdatenFehler, match="Firma"):
        CsvStammdatenQuelle("test", konfig).lieferanten()


def test_fehlende_datei(tmp_path: Path) -> None:
    with pytest.raises(StammdatenFehler, match="artikel.csv"):
        CsvStammdatenQuelle("test", _konfig(tmp_path)).artikel()


def test_leere_datei(tmp_path: Path) -> None:
    _schreibe(tmp_path / "artikel.csv", "\n\n")
    with pytest.raises(StammdatenFehler, match="leer"):
        CsvStammdatenQuelle("test", _konfig(tmp_path)).artikel()


def test_belege_fehlen_liefert_leere_liste_mit_warnung(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    konfig = _konfig(tmp_path)
    with caplog.at_level(logging.WARNING, logger="auftragsautomat.stammdaten.csv_quelle"):
        assert CsvStammdatenQuelle("test", konfig).belege() == []
    assert any("belege.csv" in r.getMessage() and r.levelno == logging.WARNING for r in caplog.records)


def test_belege_nicht_konfiguriert(tmp_path: Path) -> None:
    konfig = _konfig(tmp_path)
    konfig.belege = None
    assert CsvStammdatenQuelle("test", konfig).belege() == []


def test_kodierung_fallback_utf8(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    text = "lieferantennummer;name;ort\n1;Weingut Müller;Bad Dürkheim\n"
    _schreibe(tmp_path / "lieferanten.csv", text, "utf-8-sig")
    # Mit cp1252 würde der BOM stören -> Erkennung über BOM
    konfig = _konfig(tmp_path, kodierung="cp1252")
    with caplog.at_level(logging.WARNING):
        lieferanten = CsvStammdatenQuelle("test", konfig).lieferanten()
    assert lieferanten[0].name == "Weingut Müller"
    assert lieferanten[0].ort == "Bad Dürkheim"
    assert any("BOM" in r.getMessage() for r in caplog.records)

    # Decodierfehler mit der konfigurierten Kodierung -> utf-8-sig als Fallback
    _schreibe(tmp_path / "lieferanten.csv", text, "utf-8")
    konfig = _konfig(tmp_path, kodierung="ascii")
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        lieferanten = CsvStammdatenQuelle("test", konfig).lieferanten()
    assert lieferanten[0].name == "Weingut Müller"
    assert any("utf-8-sig" in r.getMessage() for r in caplog.records)


def test_unbekannte_kodierung(tmp_path: Path) -> None:
    _schreibe(tmp_path / "artikel.csv", ARTIKEL_KOPF + "\n")
    with pytest.raises(StammdatenFehler, match="Kodierung"):
        CsvStammdatenQuelle("test", _konfig(tmp_path, kodierung="gibt-es-nicht")).artikel()


def test_anderes_trennzeichen(tmp_path: Path) -> None:
    _schreibe(tmp_path / "kunden.csv", "kundennummer\tname\tpreisgruppe\n7\tHotel Seeblick\t1\n")
    kunden = CsvStammdatenQuelle("test", _konfig(tmp_path, trennzeichen="tab")).kunden()
    assert kunden[0].name == "Hotel Seeblick"
    assert kunden[0].preisgruppe == 1


def test_zeile_ohne_pflichtwert_wird_uebersprungen(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    text = ARTIKEL_KOPF + "\n;Ohne Nummer;;;;;;;;;;\n42;Mit Nummer 0,75 l;;;;;;1,00;2,00;;;1\n\n"
    _schreibe(tmp_path / "artikel.csv", text)
    with caplog.at_level(logging.WARNING):
        artikel = CsvStammdatenQuelle("test", _konfig(tmp_path)).artikel()
    assert [a.artikelnummer for a in artikel] == ["42"]
    assert any("Zeile 2" in r.getMessage() and "übersprungen" in r.getMessage() for r in caplog.records)


def test_doppelte_nummer_wird_gemeldet(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    text = "kundennummer;name\n1;A\n1;B\n"
    _schreibe(tmp_path / "kunden.csv", text)
    with caplog.at_level(logging.WARNING):
        kunden = CsvStammdatenQuelle("test", _konfig(tmp_path)).kunden()
    assert len(kunden) == 2
    assert any("doppelt" in r.getMessage() for r in caplog.records)


def test_lies_csv_zeilen_ignoriert_unbekannte_spalten_und_kurze_zeilen(tmp_path: Path) -> None:
    text = "belegart;belegnummer;extra;datum\nAB;AB-1;x;01.02.2026\nLS;LS-1\n"
    pfad = _schreibe(tmp_path / "belege.csv", text)
    zeilen = lies_csv_zeilen(pfad, _konfig(tmp_path), "belege")
    assert zeilen[0][0] == 2
    assert zeilen[0][1]["belegart"] == "AB"
    assert "extra" not in zeilen[0][1]
    assert zeilen[1][1]["datum"] is None
    with pytest.raises(StammdatenFehler, match="Unbekannte Stammdatentabelle"):
        lies_csv_zeilen(pfad, _konfig(tmp_path), "sonstiges")


def test_feldnamen_folgen_den_modellen() -> None:
    assert "firma_id" not in feldnamen("artikel")
    assert "artikelnummer" in feldnamen("artikel")
    assert "ist_sondervariante" not in feldnamen("artikel")  # computed_field
    assert "inaktiv" in feldnamen("kunden")
    assert feldnamen("belege")[:2] == ("belegart", "belegnummer")


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "wert, erwartet",
    [
        ("4,20", Decimal("4.20")),
        ("1.234,50", Decimal("1234.50")),
        ("6,90 €", Decimal("6.90")),
        ("6,90 EUR", Decimal("6.90")),
        ("4.20", Decimal("4.20")),
        ("1.234.567", Decimal("1234567")),
        ("-12,5", Decimal("-12.5")),
        ("", None),
        ("  ", None),
        ("-", None),
        (None, None),
        (Decimal("3.5"), Decimal("3.5")),
        (7, Decimal("7")),
        (4.2, Decimal("4.2")),
        (True, Decimal("1")),
        ("abc", None),
    ],
)
def test_parse_dezimal(wert: object, erwartet: Decimal | None) -> None:
    ergebnis = parse_dezimal(wert)
    assert ergebnis == erwartet
    if ergebnis is not None:
        assert isinstance(ergebnis, Decimal)


@pytest.mark.parametrize(
    "wert, erwartet",
    [
        ("1", True),
        ("0", False),
        ("ja", True),
        ("Nein", False),
        ("J", True),
        ("n", False),
        ("true", True),
        ("FALSE", False),
        ("wahr", True),
        ("falsch", False),
        ("x", True),
        (1, True),
        (0, False),
        (Decimal(1), True),
        (True, True),
        (False, False),
    ],
)
def test_parse_bool(wert: object, erwartet: bool) -> None:
    assert parse_bool(wert) is erwartet


def test_parse_bool_leer_und_unbekannt(caplog: pytest.LogCaptureFixture) -> None:
    assert parse_bool(None) is False
    assert parse_bool("", standard=True) is True
    with caplog.at_level(logging.WARNING):
        assert parse_bool("vielleicht", standard=True) is True
    assert any("Wahrheitswert" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize(
    "wert, erwartet",
    [
        ("15.09.2026", date(2026, 9, 15)),
        ("1.2.2026", date(2026, 2, 1)),
        ("15.09.26", date(2026, 9, 15)),
        ("2026-09-15", date(2026, 9, 15)),
        ("2026-09-15 00:00:00", date(2026, 9, 15)),
        ("2026-09-15T10:30:00", date(2026, 9, 15)),
        ("15.09.2026 10:30", date(2026, 9, 15)),
        ("20260915", date(2026, 9, 15)),
        (date(2026, 1, 2), date(2026, 1, 2)),
        ("", None),
        (None, None),
        ("31.02.2026", None),
        ("gestern", None),
    ],
)
def test_parse_datum(wert: object, erwartet: date | None) -> None:
    assert parse_datum(wert) == erwartet


def test_parse_datum_datetime() -> None:
    from datetime import datetime

    assert parse_datum(datetime(2026, 9, 15, 12, 0)) == date(2026, 9, 15)


@pytest.mark.parametrize(
    "wert, erwartet",
    [
        ("AB", Belegart.AUFTRAGSBESTAETIGUNG),
        ("ab", Belegart.AUFTRAGSBESTAETIGUNG),
        ("Auftragsbestätigung", Belegart.AUFTRAGSBESTAETIGUNG),
        ("Auftragsbestaetigung", Belegart.AUFTRAGSBESTAETIGUNG),
        ("Auftrag", Belegart.AUFTRAGSBESTAETIGUNG),
        ("LS", Belegart.LIEFERSCHEIN),
        ("Lieferschein", Belegart.LIEFERSCHEIN),
        ("RE", Belegart.RECHNUNG),
        ("RG", Belegart.RECHNUNG),
        ("Rechnung", Belegart.RECHNUNG),
        ("BE", Belegart.BESTELLUNG),
        ("Bestellung", Belegart.BESTELLUNG),
        (1, Belegart.AUFTRAGSBESTAETIGUNG),
        (2, Belegart.LIEFERSCHEIN),
        (3, Belegart.RECHNUNG),
        (5, Belegart.BESTELLUNG),
        ("Angebot", Belegart.SONSTIGE),
        ("", Belegart.SONSTIGE),
        (None, Belegart.SONSTIGE),
        (Belegart.LIEFERSCHEIN, Belegart.LIEFERSCHEIN),
    ],
)
def test_parse_belegart(wert: object, erwartet: Belegart) -> None:
    assert parse_belegart(wert) is erwartet


@pytest.mark.parametrize(
    "wert, erwartet",
    [
        ("3441", "3441"),
        ("  Text  ", "Text"),
        ("", None),
        (None, None),
        (3441, "3441"),
        (Decimal("3441"), "3441"),
        (Decimal("3441.5"), "3441.5"),
        (3441.0, "3441"),
        (True, "1"),
        (b"M\xfcller", "Müller"),
    ],
)
def test_als_text(wert: object, erwartet: str | None) -> None:
    assert als_text(wert) == erwartet


def test_parse_int() -> None:
    assert parse_int("2023") == 2023
    assert parse_int("1,0") == 1
    assert parse_int(Decimal("2")) == 2
    assert parse_int("") is None
    assert parse_int("1,5") is None
    assert parse_int("abc") is None


# ---------------------------------------------------------------------------
# Zeilenumwandler (auch für ODBC-Zeilen mit nativen Werten)
# ---------------------------------------------------------------------------


def test_zeile_zu_artikel_leitet_felder_ab() -> None:
    artikel = zeile_zu_artikel(
        "f",
        {"artikelnummer": 12, "bezeichnung": "Barolo 2019 OHK 1,5 l", "ek_preis": Decimal("20.5"), "vk_preis_1": 30.0},
    )
    assert artikel.artikelnummer == "12"
    assert artikel.volumen_cl == Decimal("150")
    assert artikel.holzkiste is True
    assert artikel.jahrgang == 2019
    assert artikel.ek_preis == Decimal("20.5")
    assert artikel.vk_preis_1 == Decimal("30")
    assert artikel.aktiv is True
    assert artikel.ist_sondervariante is True


def test_zeile_zu_artikel_explizite_werte_schlagen_ableitung() -> None:
    artikel = zeile_zu_artikel(
        "f",
        {"artikelnummer": "1", "bezeichnung": "Wein Holzkiste 1,5 l", "volumen_cl": "75", "holzkiste": "0", "jahrgang": "2020"},
    )
    assert artikel.volumen_cl == Decimal("75")
    assert artikel.holzkiste is False
    assert artikel.jahrgang == 2020


def test_zeile_zu_artikel_inaktiv_und_gesperrt() -> None:
    assert zeile_zu_artikel("f", {"artikelnummer": "1", "bezeichnung": "x", "inaktiv": "J"}).aktiv is False
    assert zeile_zu_artikel("f", {"artikelnummer": "1", "bezeichnung": "x", "gesperrt": 1}).aktiv is False
    assert zeile_zu_artikel("f", {"artikelnummer": "1", "bezeichnung": "x", "aktiv": "1", "gesperrt": "N"}).aktiv is True
    assert zeile_zu_artikel("f", {"artikelnummer": "1", "bezeichnung": "x", "aktiv": "0"}).aktiv is False


def test_zeile_zu_artikel_pflichtfelder() -> None:
    with pytest.raises(StammdatenFehler, match="artikelnummer"):
        zeile_zu_artikel("f", {"bezeichnung": "x"})
    with pytest.raises(StammdatenFehler, match="bezeichnung"):
        zeile_zu_artikel("f", {"artikelnummer": "1", "bezeichnung": "  "})


def test_zeile_zu_kunde_name_ersatz(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO):
        kunde = zeile_zu_kunde("f", {"kundennummer": 5, "name": None, "name2": "Firma GmbH", "preisgruppe": Decimal("2")})
    assert kunde.name == "Firma GmbH"
    assert kunde.name2 is None
    assert kunde.preisgruppe == 2
    with pytest.raises(StammdatenFehler, match="name"):
        zeile_zu_kunde("f", {"kundennummer": "5"})


def test_zeile_zu_lieferant() -> None:
    lieferant = zeile_zu_lieferant("f", {"lieferantennummer": "87", "name": "Cantine Colosi", "inaktiv": "N"})
    assert lieferant.name == "Cantine Colosi"
    assert lieferant.aktiv is True


def test_zeile_zu_beleg_native_werte(caplog: pytest.LogCaptureFixture) -> None:
    from datetime import datetime

    beleg = zeile_zu_beleg(
        "f",
        {"belegart": 5, "belegnummer": 1001, "partnernummer": Decimal("72"), "datum": datetime(2026, 9, 15, 8, 0), "netto": 252.0},
    )
    assert beleg.belegart is Belegart.BESTELLUNG
    assert beleg.belegnummer == "1001"
    assert beleg.partnernummer == "72"
    assert beleg.datum == date(2026, 9, 15)
    assert beleg.netto == Decimal("252")
    with caplog.at_level(logging.WARNING):
        leer = zeile_zu_beleg("f", {"belegart": "", "belegnummer": "X-1"})
    assert leer.belegart is Belegart.SONSTIGE
    assert any("Belegart leer" in r.getMessage() for r in caplog.records)
