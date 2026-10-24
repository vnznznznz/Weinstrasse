"""Tests für rabatte.aktionen_excel: Aktionszeiträume aus einer Excel über das Fake-Backend.

Die Arbeitsmappen werden je Test mit openpyxl in ein tmp-Verzeichnis geschrieben und über
``FakeExcelBackend`` (lädt ``lokaler_pfad`` lesend) gelesen. Keine echten Microsoft-365-Zugriffe.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from openpyxl import Workbook

from auftragsautomat.config import DateiRefKonfig, RabatteKonfig, Settings
from auftragsautomat.m365.basis import datum_zu_serienzahl
from auftragsautomat.m365.fake import FakeExcelBackend
from auftragsautomat.models import Pruefcode, Schweregrad
from auftragsautomat.rabatte.aktionen_excel import (
    AktionenLadeErgebnis,
    datum_aus,
    erkenne_spalten,
    firma_id_aus,
    lese_aktionen,
    normalisiere_kopf,
    nummern_aus,
    prozent_aus,
)

KOPF = ["Kunde", "Lieferant", "Artikel", "Artikelnummer", "Von", "Bis", "Rabatt %", "Rabatt EUR", "Aktionspreis", "Aktion", "Bemerkung", "Firma"]


def _xlsx(pfad: Path, zeilen: list[list[Any]], *, blatt: str = "Aktionen", weitere_blaetter: tuple[str, ...] = ()) -> Path:
    mappe = Workbook()
    ws = mappe.active
    ws.title = blatt
    for zeile in zeilen:
        ws.append(zeile)
    for name in weitere_blaetter:
        mappe.create_sheet(name).append(["nur Platzhalter"])
    mappe.save(pfad)
    return pfad


def _lese(pfad: Path, settings: Settings, *, blatt: str | None = "Aktionen") -> AktionenLadeErgebnis:
    ref = DateiRefKonfig(lokaler_pfad=pfad, blatt=blatt)
    return lese_aktionen(FakeExcelBackend(), ref, settings.rabatte, firmen=settings.firmen)


def _zeile(**felder: Any) -> list[Any]:
    schluessel = ["kunde", "lieferant", "artikel", "artikelnummer", "von", "bis", "prozent", "eur", "aktionspreis", "aktion", "bemerkung", "firma"]
    return [felder.get(s) for s in schluessel]


# ---------------------------------------------------------------------------
# Zellwerte
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("wert", "erwartet"),
    [
        ("10 %", Decimal("10")),
        ("10", Decimal("10")),
        ("10,5 %", Decimal("10.5")),
        ("0,1", Decimal("10")),
        (0.1, Decimal("10")),
        (10, Decimal("10")),
        (Decimal("0.05"), Decimal("5")),
        ("-10 %", Decimal("10")),
        ("0,5 %", Decimal("0.5")),
        (100, Decimal("100")),
        (150, None),
        (0, None),
        ("", None),
        (None, None),
        ("abc", None),
        (True, None),
    ],
)
def test_prozent_aus(wert: Any, erwartet: Decimal | None):
    assert prozent_aus(wert) == erwartet


@pytest.mark.parametrize(
    ("wert", "erwartet"),
    [
        (date(2026, 11, 1), date(2026, 11, 1)),
        (datetime(2026, 11, 1, 8, 30), date(2026, 11, 1)),
        (datum_zu_serienzahl(date(2026, 11, 1)), date(2026, 11, 1)),
        (int(datum_zu_serienzahl(date(2026, 11, 30))), date(2026, 11, 30)),
        ("01.11.2026", date(2026, 11, 1)),
        ("1.11.26", date(2026, 11, 1)),
        ("2026-11-30", date(2026, 11, 30)),
        (str(int(datum_zu_serienzahl(date(2026, 11, 1)))), date(2026, 11, 1)),
        ("", None),
        ("kein Datum", None),
        (None, None),
        (True, None),
    ],
)
def test_datum_aus(wert: Any, erwartet: date | None):
    assert datum_aus(wert) == erwartet


@pytest.mark.parametrize(
    ("wert", "erwartet"),
    [
        ("1643, 957", ["1643", "957"]),
        ("1643;957\n2001", ["1643", "957", "2001"]),
        (1643, ["1643"]),
        (1643.0, ["1643"]),
        ("1643.0, 957", ["1643", "957"]),
        ("", []),
        (None, []),
    ],
)
def test_nummern_aus(wert: Any, erwartet: list[str]):
    assert nummern_aus(wert) == erwartet


def test_normalisiere_kopf():
    assert normalisiere_kopf("Rabatt %") == "rabattprozent"
    assert normalisiere_kopf("Rabatt €") == "rabatteur"
    assert normalisiere_kopf("Art.-Nr.") == "artnr"
    assert normalisiere_kopf(" Gültig ab ") == "gueltigab"
    assert normalisiere_kopf(None) == ""


def test_erkenne_spalten_konfiguriert_und_synonyme():
    konfig = RabatteKonfig()
    spalten = erkenne_spalten(["Aktion", "Kunde", "Weingut", "Art.-Nr.", "Gültig ab", "Ende", "Rabatt", "Rabatt €", "Notiz", "Mandant"], konfig)
    assert spalten == {
        "bezeichnung": 0,
        "kunde_kurz": 1,
        "lieferant_kurz": 2,
        "artikelnummern": 3,
        "von": 4,
        "bis": 5,
        "rabatt_prozent": 6,
        "rabatt_eur": 7,
        "bemerkung": 8,
        "firma_id": 9,
    }


def test_erkenne_spalten_jede_spalte_nur_einmal():
    konfig = RabatteKonfig()
    spalten = erkenne_spalten(["Von", "Von", "Bis", "Rabatt %", "Rabatt %"], konfig)
    assert spalten == {"von": 0, "bis": 2, "rabatt_prozent": 3}


def test_firma_id_aus(settings: Settings):
    firmen = settings.firmen
    assert firma_id_aus(None, firmen) == (None, None)
    assert firma_id_aus("woe_leh", firmen) == ("woe_leh", None)
    assert firma_id_aus("WOE", firmen) == ("woe_leh", None)
    assert firma_id_aus("Weinstrasse Agentur", firmen) == ("weinstrasse_agentur", None)
    assert firma_id_aus("Wine-of-Excellence LEH", firmen) == ("woe_leh", None)
    firma_id, hinweis = firma_id_aus("WIK", firmen)
    assert firma_id == "WIK"
    assert hinweis is not None and "nicht eindeutig" in hinweis
    firma_id, hinweis = firma_id_aus("Fremdfirma", firmen)
    assert firma_id == "Fremdfirma"
    assert hinweis is not None and "nicht konfiguriert" in hinweis
    assert firma_id_aus("WS", None) == ("WS", None)


# ---------------------------------------------------------------------------
# lese_aktionen
# ---------------------------------------------------------------------------


def test_grundfall_mit_allen_varianten(tmp_path: Path, settings: Settings):
    pfad = _xlsx(
        tmp_path / "aktionen.xlsx",
        [
            KOPF,
            _zeile(kunde="Hegmann", lieferant="Cide", artikel="Gloria Secco Frizzante", artikelnummer="1643, 957", von=datetime(2026, 11, 1), bis=datetime(2026, 11, 30), prozent="10 %", aktion="Herbstaktion", bemerkung="nur Paletten", firma="WOE"),
            _zeile(kunde="Fachhandel", lieferant="Colosi", von=date(2026, 10, 1), bis=date(2026, 10, 31), prozent=0.05, firma="Weinstrasse Agentur"),
            _zeile(kunde="3441", lieferant="Bollina", artikelnummer=3001, von=date(2026, 12, 1), bis=date(2026, 12, 24), eur="1,00 €"),
            _zeile(lieferant="Cide", artikel="Rosato", von=date(2026, 12, 1), bis=date(2026, 12, 24), aktionspreis=5.9, prozent=None),
        ],
    )
    ergebnis = _lese(pfad, settings)
    assert ergebnis.fehler == []
    assert ergebnis.blatt == "Aktionen"
    assert ergebnis.kopfzeile == 1
    assert ergebnis.spalten["von"] == "E" and ergebnis.spalten["rabatt_prozent"] == "G"
    assert [a.aktion_id for a in ergebnis.aktionen] == ["excel:Aktionen:2", "excel:Aktionen:3", "excel:Aktionen:4", "excel:Aktionen:5"]

    a1, a2, a3, a4 = ergebnis.aktionen
    assert (a1.kunde_kurz, a1.lieferant_kurz, a1.artikel_text) == ("Hegmann", "Cide", "Gloria Secco Frizzante")
    assert a1.artikelnummern == ["1643", "957"]
    assert (a1.von, a1.bis) == (date(2026, 11, 1), date(2026, 11, 30))
    assert a1.rabatt_prozent == Decimal("10")
    assert (a1.rabatt_eur, a1.aktionspreis) == (None, None)
    assert (a1.bezeichnung, a1.bemerkung, a1.firma_id) == ("Herbstaktion", "nur Paletten", "woe_leh")
    assert a1.quelle == "excel:Aktionen:2"
    assert a1.bezug == settings.rabatte.bezug
    assert a1.aktiv is True and a1.konfidenz is None

    assert a2.rabatt_prozent == Decimal("5")  # 0.05 = Anteil
    assert a2.firma_id == "weinstrasse_agentur"
    assert a2.artikelnummern == [] and a2.artikel_text is None

    assert a3.kundennummern == ["3441"] and a3.kunde_kurz is None  # reine Nummer in der Kundenspalte
    assert a3.artikelnummern == ["3001"]
    assert a3.rabatt_eur == Decimal("1.00") and a3.rabatt_prozent is None

    assert a4.aktionspreis == Decimal("5.90")
    assert a4.kunde_kurz is None and a4.kundennummern == []


def test_kopfzeile_nicht_in_zeile_1_text_und_serienzahl(tmp_path: Path, settings: Settings):
    serie_von = int(datum_zu_serienzahl(date(2026, 11, 1)))
    pfad = _xlsx(
        tmp_path / "aktionen.xlsx",
        [
            ["Aktionszeiträume 2026"],
            [],
            ["Stand", "20.10.2026"],
            ["Nr.", "Kunde", "Lieferant", "Zusatzspalte", "Von", "Bis", "Rabatt %", "Noch eine Spalte"],
            [1, "Hegmann", "Cide", "egal", serie_von, "30.11.2026", 10, "x"],
            [2, "Menrath", "Bollina", "egal", "2026-12-01", float(datum_zu_serienzahl(date(2026, 12, 24))), "7,5 %", None],
            [],
            [None, None, None, None, None, None, None, None],
        ],
    )
    ergebnis = _lese(pfad, settings)
    assert ergebnis.fehler == []
    assert ergebnis.kopfzeile == 4
    assert "zusatzspalte" not in {k.lower() for k in ergebnis.spalten}
    assert [a.aktion_id for a in ergebnis.aktionen] == ["excel:Aktionen:5", "excel:Aktionen:6"]
    a1, a2 = ergebnis.aktionen
    assert (a1.von, a1.bis, a1.rabatt_prozent) == (date(2026, 11, 1), date(2026, 11, 30), Decimal("10"))
    assert (a2.von, a2.bis, a2.rabatt_prozent) == (date(2026, 12, 1), date(2026, 12, 24), Decimal("7.5"))


def test_fehlende_pflichtspalte_bis(tmp_path: Path, settings: Settings):
    pfad = _xlsx(tmp_path / "aktionen.xlsx", [["Kunde", "Lieferant", "Von", "Rabatt %"], ["Hegmann", "Cide", date(2026, 11, 1), 10]])
    ergebnis = _lese(pfad, settings)
    assert ergebnis.aktionen == []
    assert len(ergebnis.fehler) == 1
    fehler = ergebnis.fehler[0]
    assert fehler.code == Pruefcode.FELD_FEHLT
    assert "Bis" in fehler.text and "Pflichtspalten fehlen" in fehler.text
    assert fehler.details["feld"] == "bis"


def test_fehlende_rabattspalte(tmp_path: Path, settings: Settings):
    pfad = _xlsx(tmp_path / "aktionen.xlsx", [["Kunde", "Von", "Bis", "Bemerkung"], ["Hegmann", date(2026, 11, 1), date(2026, 11, 30), "ohne Rabatt"]])
    ergebnis = _lese(pfad, settings)
    assert ergebnis.aktionen == []
    assert [h.details["feld"] for h in ergebnis.fehler] == ["rabatt"]


def test_keine_kopfzeile(tmp_path: Path, settings: Settings):
    pfad = _xlsx(tmp_path / "aktionen.xlsx", [["Hegmann", "Cide", date(2026, 11, 1), date(2026, 11, 30), 10]])
    ergebnis = _lese(pfad, settings)
    assert ergebnis.aktionen == []
    assert ergebnis.fehler and "keine Kopfzeile" in ergebnis.fehler[0].text


def test_zeilen_mit_fehlern_werden_gemeldet_nicht_abgebrochen(tmp_path: Path, settings: Settings):
    pfad = _xlsx(
        tmp_path / "aktionen.xlsx",
        [
            KOPF,
            _zeile(kunde="Hegmann", von=None, bis=date(2026, 11, 30), prozent=10),  # Von fehlt
            _zeile(kunde="Hegmann", von="irgendwann", bis=date(2026, 11, 30), prozent=10),  # Von unlesbar
            _zeile(kunde="Menrath", von=date(2026, 11, 1), bis=date(2026, 11, 30)),  # keine Rabattangabe
            _zeile(kunde="Menrath", von=date(2026, 11, 1), bis=date(2026, 11, 30), prozent="viel"),  # unlesbar
            _zeile(kunde="Schmidt", von=date(2026, 11, 30), bis=date(2026, 11, 1), prozent=10),  # umgedreht
            _zeile(kunde="Schmidt", von=date(2026, 11, 1), bis=date(2026, 11, 30), prozent=10, firma="WIK"),  # Firma mehrdeutig
        ],
    )
    ergebnis = _lese(pfad, settings)
    assert ergebnis.fehler == []
    assert [a.aktion_id for a in ergebnis.aktionen] == ["excel:Aktionen:6", "excel:Aktionen:7"]
    umgedreht, mehrdeutig = ergebnis.aktionen
    assert (umgedreht.von, umgedreht.bis) == (date(2026, 11, 1), date(2026, 11, 30))
    assert mehrdeutig.firma_id == "WIK"
    warnungen = [h for h in ergebnis.hinweise if h.schweregrad == Schweregrad.WARNUNG]
    assert [h.details["quelle"] for h in warnungen] == [
        "excel:Aktionen:2",
        "excel:Aktionen:3",
        "excel:Aktionen:4",
        "excel:Aktionen:5",
        "excel:Aktionen:6",
    ]
    assert "Von fehlt" in warnungen[0].text
    assert "keine Rabattangabe" in warnungen[2].text
    assert "nicht lesbar" in warnungen[3].text
    assert "umgedreht" in warnungen[4].text
    infos = [h for h in ergebnis.hinweise if h.schweregrad == Schweregrad.INFO]
    assert len(infos) == 1 and "nicht eindeutig" in infos[0].text


def test_blatt_fehlt_mit_mehreren_blaettern(tmp_path: Path, settings: Settings):
    pfad = _xlsx(tmp_path / "aktionen.xlsx", [KOPF], blatt="Liste", weitere_blaetter=("Hilfe",))
    ergebnis = _lese(pfad, settings, blatt="Aktionen")
    assert ergebnis.aktionen == []
    assert ergebnis.fehler and "Blatt 'Aktionen' nicht vorhanden" in ergebnis.fehler[0].text


def test_blatt_fehlt_einziges_blatt_wird_genommen(tmp_path: Path, settings: Settings):
    pfad = _xlsx(
        tmp_path / "aktionen.xlsx",
        [KOPF, _zeile(kunde="Hegmann", von=date(2026, 11, 1), bis=date(2026, 11, 30), prozent=10)],
        blatt="Liste",
    )
    ergebnis = _lese(pfad, settings, blatt="Aktionen")
    assert ergebnis.fehler == []
    assert ergebnis.blatt == "Liste"
    assert [a.aktion_id for a in ergebnis.aktionen] == ["excel:Liste:2"]
    assert any("einzige Blatt" in h.text for h in ergebnis.hinweise)


def test_datei_fehlt_ergibt_fehlerhinweis(tmp_path: Path, settings: Settings):
    ergebnis = _lese(tmp_path / "gibt_es_nicht.xlsx", settings)
    assert ergebnis.aktionen == []
    assert ergebnis.fehler and ergebnis.fehler[0].code == Pruefcode.FELD_FEHLT


def test_aktiv_spalte(tmp_path: Path, settings: Settings):
    pfad = _xlsx(
        tmp_path / "aktionen.xlsx",
        [
            ["Kunde", "Von", "Bis", "Rabatt %", "Aktiv"],
            ["Hegmann", date(2026, 11, 1), date(2026, 11, 30), 10, "nein"],
            ["Menrath", date(2026, 11, 1), date(2026, 11, 30), 10, "ja"],
            ["Schmidt", date(2026, 11, 1), date(2026, 11, 30), 10, None],
        ],
    )
    ergebnis = _lese(pfad, settings)
    assert [a.aktiv for a in ergebnis.aktionen] == [False, True, True]
