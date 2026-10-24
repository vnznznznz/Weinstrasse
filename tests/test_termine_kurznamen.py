"""Tests für termine.kurznamen (Kurznamen aus YAML, Matchcode, Name; Abgleich mit der Dropdown-Liste).

Alle Namen und Nummern sind synthetisch; ``config/aliase/kurznamen.yaml`` enthält nur Beispiele.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from auftragsautomat.config import DateiRefKonfig, Settings
from auftragsautomat.m365.fake import FakeExcelBackend
from auftragsautomat.models import (
    Belegpaket,
    Eingangstyp,
    Extraktionsmethode,
    Kunde,
    KundeZuordnung,
    Lieferant,
    LieferantZuordnung,
    MatchStatus,
    RohBestellung,
)
from auftragsautomat.termine.kurznamen import (
    RESSOURCEN_BLATT,
    KurznameErgebnis,
    Kurznamen,
    KurznamenDaten,
    KurznamenFehler,
    erster_wortbestandteil,
    lade_kurznamen_yaml,
    vorhandene_kurznamen,
)

FIXTURE = Path(__file__).parent / "fixtures" / "excel" / "fixterminueberwachung_beispiel.xlsx"


def _kunde(nummer: str = "10001", firma: str = "wik_fh_seh", matchcode: str | None = "Matchkunde", name: str = "Weinhandel Muster GmbH") -> Kunde:
    return Kunde(firma_id=firma, kundennummer=nummer, name=name, matchcode=matchcode)


def _lieferant(nummer: str = "90001", firma: str = "wik_fh_seh", matchcode: str | None = "Matchgut", name: str = "Cantine Beispiel S.r.l.") -> Lieferant:
    return Lieferant(firma_id=firma, lieferantennummer=nummer, name=name, matchcode=matchcode)


# ---------------------------------------------------------------- YAML


def test_yaml_aus_settings_laden(settings: Settings):
    kurznamen = Kurznamen.lade(settings=settings)
    daten = kurznamen.daten
    assert daten.kunden[("wik_fh_seh", "10001")] == "Musterhandel"
    assert daten.kunden[("weinstrasse_agentur", "10002")] == "Beispielmarkt"
    assert daten.lieferanten[("wik_fh_seh", "90001")] == "Beispielgut"
    assert daten.lieferanten[(None, "90002")] == "Musterweine"  # ohne firma: alle Firmen


def test_yaml_fehlt_oder_kaputt(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    with caplog.at_level(logging.INFO):
        leer = lade_kurznamen_yaml(tmp_path / "gibt_es_nicht.yaml")
    assert leer.kunden == {} and leer.lieferanten == {}
    assert "fehlt" in caplog.text
    kaputt = tmp_path / "kurznamen.yaml"
    kaputt.write_text("kunden: [\n", encoding="utf-8")
    with pytest.raises(KurznamenFehler, match="kein gültiges YAML"):
        lade_kurznamen_yaml(kaputt)
    kaputt.write_text("- nur eine Liste\n", encoding="utf-8")
    with pytest.raises(KurznamenFehler, match="Mapping"):
        lade_kurznamen_yaml(kaputt)
    kaputt.write_text("kunden: {kundennummer: 1}\n", encoding="utf-8")
    with pytest.raises(KurznamenFehler, match="Liste"):
        lade_kurznamen_yaml(kaputt)
    with pytest.raises(KurznamenFehler, match="Pfad oder settings"):
        Kurznamen.lade()


def test_yaml_eintraege_tolerant(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    datei = tmp_path / "kurznamen.yaml"
    datei.write_text(
        "kunden:\n"
        "  - kundennummer: 4711\n"  # Zahl statt Text
        "    firma: wik_leh\n"
        "    kurzname: Beispielkunde\n"
        "  - kundennummer: '4712'\n"  # ohne kurzname -> übersprungen
        "    firma: wik_leh\n"
        "lieferanten:\n"
        "  - nummer: '0815'\n"  # alternativer Schlüssel 'nummer'
        "    kurzname: Beispiellieferant\n"
        "rechtsformen: ['S.r.l.s.']\n"
        "generische_woerter: ['Vinothek']\n",
        encoding="utf-8",
    )
    with caplog.at_level(logging.WARNING):
        daten = lade_kurznamen_yaml(datei)
    assert daten.kunden == {("wik_leh", "4711"): "Beispielkunde"}
    assert daten.lieferanten == {(None, "0815"): "Beispiellieferant"}
    assert daten.rechtsformen == {"srls"} and daten.generische_woerter == ["vinothek"]
    assert "übersprungen" in caplog.text


# ---------------------------------------------------------------- Namensableitung


@pytest.mark.parametrize(
    ("name", "erwartet"),
    [
        ("Weinhaus Beispiel GmbH", "Beispiel"),
        ("Cantine Beispiel S.r.l.", "Beispiel"),
        ("Azienda Agricola Muster SpA", "Muster"),
        ("Domaine du Beispiel SAS", "Beispiel"),
        ("Tenuta di Muster", "Muster"),
        ("Beispiel & Söhne OHG", "Beispiel"),
        ("Muster-Markt e.K.", "Muster"),
        ("Weingut Beispiel GmbH & Co. KG", "Beispiel"),
        ("Musterhandel", "Musterhandel"),
        ("GmbH", "GmbH"),  # nichts übrig -> erstes Wort des Originals
        ("", ""),
        (None, ""),
    ],
)
def test_erster_wortbestandteil(name: str | None, erwartet: str):
    assert erster_wortbestandteil(name) == erwartet


def test_erster_wortbestandteil_mit_eigenen_listen():
    assert erster_wortbestandteil("Vinothek Beispiel Srls", generische_woerter=["Vinothek"], rechtsformen=["S.r.l.s."]) == "Beispiel"
    assert erster_wortbestandteil("Vinothek Beispiel") == "Vinothek"  # ohne eigene Liste bleibt Vinothek


# ---------------------------------------------------------------- Ermittlung


def test_reihenfolge_alias_matchcode_name_text(settings: Settings):
    kurznamen = Kurznamen.lade(settings=settings)
    # 1. YAML schlägt den Matchcode
    e = kurznamen.ermittle_kunde(_kunde("10001", matchcode="Anders"))
    assert e == KurznameErgebnis("Musterhandel", "alias", False, "Musterhandel")
    # YAML gilt nur für die genannte Firma
    assert kurznamen.ermittle_kunde(_kunde("10001", firma="woe_leh", matchcode="Anders")).quelle == "matchcode"
    # 2. Matchcode
    assert kurznamen.ermittle_kunde(_kunde("99999")).kurzname == "Matchkunde"
    assert kurznamen.ermittle_kunde(_kunde("99999", matchcode="  ")).quelle == "name"
    # 3. Namensbestandteil
    assert kurznamen.ermittle_kunde(_kunde("99999", matchcode=None)) == KurznameErgebnis("Muster", "name", False, "Muster")
    assert kurznamen.lieferant(_lieferant("99999", matchcode=None)) == "Beispiel"
    # YAML-Eintrag ohne Firma gilt überall
    assert kurznamen.ermittle_lieferant(_lieferant("90002", firma="woe_leh")).kurzname == "Musterweine"
    # 4. ohne Stammdaten: Dokumenttext (ohne Gattungswort), sonst Unbekannt
    assert kurznamen.ermittle_kunde(None, text="Weinmarkt Beispielstadt") == KurznameErgebnis("Beispielstadt", "text", False, "Beispielstadt")
    assert kurznamen.ermittle_kunde(None) == KurznameErgebnis("Unbekannt", "unbekannt", False, "Unbekannt")
    assert kurznamen.lieferant(None, text="   ") == "Unbekannt"
    assert "Unbekannt" not in kurznamen.neue_namen


def test_angleichen_an_dropdown_liste(caplog: pytest.LogCaptureFixture):
    kurznamen = Kurznamen(vorhandene=["Beispielgut", "Musterhandel", " ", "beispielgut"])
    assert kurznamen.vorhandene == ["Beispielgut", "Musterhandel"]  # Dublette und Leeres entfallen
    assert kurznamen.angleichen("beispielgut") == ("Beispielgut", True)  # Groß-/Kleinschreibung
    assert kurznamen.angleichen("Beispielgüt") == ("Beispielgut", True)  # Umlaut
    with caplog.at_level(logging.INFO):
        assert kurznamen.angleichen("Musterhandl") == ("Musterhandel", True)  # rapidfuzz >= 90
    assert "angeglichen" in caplog.text
    assert kurznamen.angleichen("Weinmarkt Beispiel") == ("Weinmarkt Beispiel", False)  # zu verschieden
    assert kurznamen.angleichen("Neukunde") == ("Neukunde", False)
    assert kurznamen.neue_namen == ["Neukunde", "Weinmarkt Beispiel"]
    assert kurznamen.angleichen("") == ("", False)
    assert kurznamen.angleichen("Unbekannt") == ("Unbekannt", False) and "Unbekannt" not in kurznamen.neue_namen
    # Schwelle anpassbar
    streng = Kurznamen(vorhandene=["Musterhandel"], schwelle=99)
    assert streng.angleichen("Musterhandl") == ("Musterhandl", False)


def test_angleichen_wirkt_auf_alle_quellen(settings: Settings):
    kurznamen = Kurznamen.lade(settings=settings, vorhandene=["Musterhandl", "Matchgüt"])
    assert kurznamen.ermittle_kunde(_kunde("10001")) == KurznameErgebnis("Musterhandl", "alias", True, "Musterhandel")
    assert kurznamen.ermittle_lieferant(_lieferant("99999")) == KurznameErgebnis("Matchgüt", "matchcode", True, "Matchgut")
    kurznamen.mit_vorhandenen(["Beispiel"])
    assert kurznamen.ermittle_lieferant(_lieferant("99999", matchcode=None)).vorhanden is True


def test_ohne_dropdown_liste_keine_meldung_im_log(caplog: pytest.LogCaptureFixture):
    kurznamen = Kurznamen()
    with caplog.at_level(logging.INFO):
        assert kurznamen.angleichen("Neukunde") == ("Neukunde", False)
    assert "Ressourcenliste" not in caplog.text
    assert kurznamen.neue_namen == ["Neukunde"]


# ---------------------------------------------------------------- Ressourcen-Blatt


def test_vorhandene_kurznamen_aus_fixture():
    excel = FakeExcelBackend(aus_xlsx=FIXTURE)
    ref = DateiRefKonfig(lokaler_pfad=FIXTURE, blatt="Fixterminüberwachung")
    namen = vorhandene_kurznamen(excel, ref)
    assert namen[:2] == ["Beispielgut", "Musterhandel"]  # Zeilenweise: A2, B2, A3, B3 ...
    assert {"Musterweine", "Beispielmarkt", "Markt Fünf", "Weingut Beispiel"} <= set(namen)
    assert "Lieferanten" not in namen and "Kunden" not in namen  # Überschriften nicht
    assert len(namen) == len({n.casefold() for n in namen})


def test_vorhandene_kurznamen_ohne_blatt_oder_mit_formeltext(caplog: pytest.LogCaptureFixture):
    excel = FakeExcelBackend({"standard": {"Fixterminüberwachung": [["KW"]]}})
    ref = DateiRefKonfig(blatt="Fixterminüberwachung")
    with caplog.at_level(logging.WARNING):
        assert vorhandene_kurznamen(excel, ref) == []
    assert RESSOURCEN_BLATT in caplog.text
    excel = FakeExcelBackend({"standard": {RESSOURCEN_BLATT: [["Lieferant", "Kunde"], ["=SORT(UNIQUE(A:A))", "Beispielmarkt"], [42, "beispielmarkt"]]}})
    assert vorhandene_kurznamen(excel, ref) == ["Beispielmarkt"]


# ---------------------------------------------------------------- Belegpaket


def _paket(kunde: Kunde | None, lieferant: Lieferant | None, *, firma_id: str | None = "wik_fh_seh", kunde_text: str | None = None, lieferant_text: str | None = None) -> Belegpaket:
    roh = RohBestellung(
        eingangstyp=Eingangstyp.KUNDENBESTELLUNG_PDF,
        quelldatei=Path("eingang/bestellung.pdf"),
        extraktionsmethode=Extraktionsmethode.PDFPLUMBER,
        kunde_text=kunde_text,
        lieferant_text=lieferant_text,
    )
    paket = Belegpaket(roh=roh, firma_id=firma_id)
    if kunde is not None:
        paket.kunde = KundeZuordnung(kunde=kunde, status=MatchStatus.SICHER, quelle="alias")
    if lieferant is not None:
        paket.lieferant = LieferantZuordnung(lieferant=lieferant, status=MatchStatus.SICHER, quelle="alias")
    return paket


def test_fuer_paket(settings: Settings):
    kurznamen = Kurznamen.lade(settings=settings, vorhandene=["Beispielgut", "Musterhandel"])
    paket = _paket(_kunde("10001"), _lieferant("90001"))
    assert kurznamen.fuer_paket(paket) == ("Beispielgut", "Musterhandel")
    ohne = _paket(None, None, kunde_text="Weinhaus Neukunde GmbH", lieferant_text="Weingut Beispielgut")
    assert kurznamen.fuer_paket(ohne) == ("Beispielgut", "Neukunde")
    assert kurznamen.neue_namen == ["Neukunde"]
    leer = Kurznamen(KurznamenDaten())
    assert leer.fuer_paket(_paket(None, None)) == ("Unbekannt", "Unbekannt")
