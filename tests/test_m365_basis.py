"""Tests für m365.basis: Zelladressen, Serienzahlen, Hilfsfunktionen, Protokolle, Fehlerklassen."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from auftragsautomat.config import DateiRefKonfig
from auftragsautomat.m365 import basis
from auftragsautomat.m365.basis import (
    DateiBackend,
    ExcelBackend,
    M365Fehler,
    M365Konflikt,
    M365NichtAngemeldet,
    M365NichtGefunden,
    MailBackend,
    Zellbereich,
    datei_schluessel,
    datum_zu_serienzahl,
    excel_sitzung,
    html_zu_text,
    parse_adresse,
    serienzahl_zu_datum,
    sicherer_dateiname,
    spaltenbuchstabe,
    spaltenindex,
)
from auftragsautomat.m365.fake import FakeDateiBackend, FakeExcelBackend, FakeMailBackend

# ---------------------------------------------------------------- Zelladressen


@pytest.mark.parametrize("index,buchstabe", [(1, "A"), (26, "Z"), (27, "AA"), (52, "AZ"), (53, "BA"), (703, "AAA")])
def test_spaltenbuchstabe_und_index_sind_umkehrbar(index: int, buchstabe: str):
    assert spaltenbuchstabe(index) == buchstabe
    assert spaltenindex(buchstabe) == index
    assert spaltenindex(buchstabe.lower()) == index


def test_spaltenindex_lehnt_ungueltiges_ab():
    with pytest.raises(ValueError):
        spaltenindex("A1")
    with pytest.raises(ValueError):
        spaltenindex("")
    with pytest.raises(ValueError):
        spaltenbuchstabe(0)


def test_parse_adresse_varianten():
    assert parse_adresse("D7:K7") == Zellbereich(7, 4, 7, 11)
    assert parse_adresse("D7") == Zellbereich(7, 4, 7, 4)
    assert parse_adresse("$D$7:$K$9") == Zellbereich(7, 4, 9, 11)
    assert parse_adresse("'Ressourcen für Auto DropDown'!A1:B2") == Zellbereich(1, 1, 2, 2, "Ressourcen für Auto DropDown")
    assert parse_adresse("Fixterminüberwachung!K9:D7") == Zellbereich(7, 4, 9, 11, "Fixterminüberwachung")


def test_parse_adresse_fehler():
    for schlecht in ("", "7D", "A0", "A1:B", "Blatt!"):
        with pytest.raises(ValueError):
            parse_adresse(schlecht)


def test_zellbereich_eigenschaften():
    b = Zellbereich(5, 4, 9, 11, "Fixterminüberwachung")
    assert (b.zeilen, b.spalten) == (5, 8)
    assert b.adresse == "D5:K9"
    assert b.mit_blatt() == "'Fixterminüberwachung'!D5:K9"
    assert Zellbereich(1, 1, 1, 1).adresse == "A1"
    assert Zellbereich(1, 1, 2, 2, "Aktionen").mit_blatt() == "Aktionen!A1:B2"


# ---------------------------------------------------------------- Serienzahlen


def test_datum_zu_serienzahl_bekannte_werte():
    assert datum_zu_serienzahl(date(1900, 3, 1)) == 61.0
    assert datum_zu_serienzahl(date(2015, 1, 15)) == 42019.0  # Beispiel aus der Graph-Doku
    assert datum_zu_serienzahl(datetime(2026, 10, 18, 12, 0)) == pytest.approx(46313.5)


def test_serienzahl_zu_datum_rundreise_und_typen():
    assert serienzahl_zu_datum(42019) == date(2015, 1, 15)
    assert serienzahl_zu_datum(42019.0) == date(2015, 1, 15)
    assert serienzahl_zu_datum("42019") == date(2015, 1, 15)
    assert serienzahl_zu_datum(Decimal("42019")) == date(2015, 1, 15)
    assert serienzahl_zu_datum(46313.5) == datetime(2026, 10, 18, 12, 0)
    assert serienzahl_zu_datum(date(2026, 1, 1)) == date(2026, 1, 1)
    heute = datetime(2026, 10, 18, 8, 30)
    assert serienzahl_zu_datum(heute) == heute
    for leer in (None, "", "Text", True, 5, 10_000_000):
        assert serienzahl_zu_datum(leer) is None
    for tag in (date(1900, 3, 1), date(2026, 12, 31), date(2099, 2, 28)):
        assert serienzahl_zu_datum(datum_zu_serienzahl(tag)) == tag


# ---------------------------------------------------------------- Hilfen


def test_datei_schluessel_prioritaet(tmp_path: Path):
    assert datei_schluessel(DateiRefKonfig(drive_id="d", item_id="i", web_url="https://x")) == "d/i"
    assert datei_schluessel(DateiRefKonfig(web_url="https://x", lokaler_pfad=tmp_path)) == "https://x"
    assert datei_schluessel(DateiRefKonfig(lokaler_pfad=tmp_path / "a.xlsx")) == str(tmp_path / "a.xlsx")
    assert datei_schluessel(DateiRefKonfig()) == "standard"


def test_excel_sitzung_ohne_sitzung_ist_wirkungslos():
    class Ohne:
        pass

    with excel_sitzung(Ohne(), DateiRefKonfig()):
        pass


def test_excel_sitzung_nutzt_sitzung_des_backends():
    ereignisse: list[str] = []

    class Mit:
        def sitzung(self, datei):
            from contextlib import contextmanager

            @contextmanager
            def cm():
                ereignisse.append("auf")
                yield
                ereignisse.append("zu")

            return cm()

    with excel_sitzung(Mit(), DateiRefKonfig()):
        ereignisse.append("mitte")
    assert ereignisse == ["auf", "mitte", "zu"]


def test_sicherer_dateiname():
    assert sicherer_dateiname('New order: 4711 / "Beispiel" <Test>?') == "New order_ 4711 _ _Beispiel_ _Test_"  # ">?" -> ein Ersatzzeichen
    assert sicherer_dateiname("   ") == "datei"
    assert sicherer_dateiname("a" * 300, maximal=10) == "a" * 10
    assert sicherer_dateiname("Mehr   Leerzeichen  und Tabs") == "Mehr Leerzeichen und Tabs"


def test_html_zu_text():
    html = "<html><head><style>p{}</style></head><body><p>Hallo <b>Frau Beispiel</b>,</p><p>Zeile&nbsp;2<br>Zeile 3</p><table><tr><td>A</td><td>B</td></tr></table></body></html>"
    text = html_zu_text(html)
    assert "p{}" not in text
    assert text.splitlines()[0] == "Hallo Frau Beispiel,"
    assert "Zeile 2\nZeile 3" in text
    assert "A\tB" in text.replace("\t ", "\t") or "A B" in text
    assert html_zu_text(None) == "" and html_zu_text("") == ""


# ---------------------------------------------------------------- Protokolle / Fehler


def test_fakes_erfuellen_die_protokolle(tmp_path: Path):
    assert isinstance(FakeExcelBackend(), ExcelBackend)
    assert isinstance(FakeMailBackend(tmp_path), MailBackend)
    assert isinstance(FakeDateiBackend(tmp_path), DateiBackend)
    assert not isinstance(object(), ExcelBackend)


def test_fehlerklassen_hierarchie_und_text():
    f = M365Konflikt("gesperrt", status=409, code="AccessConflict", inner_code="accessConflict", wartezeit_sekunden=600)
    assert isinstance(f, M365Fehler)
    assert str(f) == "gesperrt | HTTP 409 | Code AccessConflict | Detail accessConflict"
    assert f.wartezeit_sekunden == 600
    assert str(M365NichtGefunden("weg")) == "weg"
    assert issubclass(M365NichtAngemeldet, M365Fehler)
    assert basis.__all__  # Modul exportiert seine öffentliche API
