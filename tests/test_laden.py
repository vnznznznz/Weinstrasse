"""Tests für stammdaten.laden (Auswahl der Quelle, Cache, lade_alle, Bericht)."""

from __future__ import annotations

import logging
import os
import shutil
from decimal import Decimal
from pathlib import Path

import pytest

from auftragsautomat.config import CsvQuellen, Firma, OdbcQuelle, Settings, StammdatenKonfig
from auftragsautomat.models import Artikel, Beleg, Belegart, Kunde, Lieferant
from auftragsautomat.stammdaten import laden
from auftragsautomat.stammdaten.basis import Stammdaten
from auftragsautomat.stammdaten.csv_quelle import CsvStammdatenQuelle
from auftragsautomat.stammdaten.laden import (
    StammdatenFehler,
    erzeuge_quelle,
    lade_alle,
    lade_stammdaten,
    leere_cache,
    stammdaten_bericht,
)


@pytest.fixture(autouse=True)
def _frischer_cache():
    leere_cache()
    yield
    leere_cache()


def test_lade_alle_vier_firmen(settings: Settings) -> None:
    alle = lade_alle(settings)
    assert set(alle) == {"weinstrasse_agentur", "wik_fh_seh", "wik_leh", "woe_leh"}
    for firma_id, daten in alle.items():
        assert isinstance(daten, Stammdaten)
        assert daten.firma_id == firma_id
    assert len(alle["wik_fh_seh"].artikel) == 2
    assert len(alle["woe_leh"].kunden) == 1
    assert len(alle["weinstrasse_agentur"].lieferanten) == 0


def test_wik_leh_inhalte(settings: Settings) -> None:
    daten = lade_stammdaten(settings.firma("wik_leh"))
    assert len(daten.artikel) == 13
    assert len(daten.kunden) == 5
    assert len(daten.lieferanten) == 4
    assert len(daten.belege) == 3
    artikel = daten.artikel_nach_nummer
    assert artikel["2005"].volumen_cl == Decimal("75")
    assert artikel["2004"].holzkiste is True
    assert artikel["1644"].ist_sondervariante is True
    assert artikel["3003"].aktiv is False
    assert artikel["1643"].vk_preis_1 == Decimal("6.90")
    assert daten.kunden_nach_nummer["3441"].name2 == "Weinmarkt Bottrop-Kirchhellen"
    assert daten.belege[0].kundenbestellnummer == "B-4711"


def test_cache_pro_prozess(settings: Settings) -> None:
    firma = settings.firma("wik_leh")
    erste = lade_stammdaten(firma)
    assert lade_stammdaten(firma) is erste
    ohne_cache = lade_stammdaten(firma, cache=False)
    assert ohne_cache is not erste
    assert len(ohne_cache.artikel) == 13
    # cache=False befüllt den Cache nicht neu
    assert lade_stammdaten(firma) is erste
    leere_cache("wik_leh")
    neu = lade_stammdaten(firma)
    assert neu is not erste
    leere_cache()
    assert lade_stammdaten(firma) is not neu


def test_cache_erkennt_neuen_export(settings: Settings, tmp_path: Path, fixtures_ordner: Path) -> None:
    ordner = tmp_path / "wik_leh"
    shutil.copytree(fixtures_ordner / "stammdaten" / "wik_leh", ordner)
    firma = Firma(
        id="kopie",
        name="Kopie",
        lexware_firmenname="Kopie",
        stammdaten=StammdatenKonfig(
            typ="csv",
            csv=CsvQuellen(
                artikel=ordner / "artikel.csv",
                kunden=ordner / "kunden.csv",
                lieferanten=ordner / "lieferanten.csv",
                belege=ordner / "belege.csv",
            ),
        ),
    )
    erste = lade_stammdaten(firma)
    assert len(erste.artikel) == 13
    assert lade_stammdaten(firma) is erste

    artikel_csv = ordner / "artikel.csv"
    with artikel_csv.open("ab") as f:
        f.write(b"9999;Neuer Wein 0,75 l;Test;150;;0;;1,00;2,00;Fl;NEU;1\r\n")
    stat = artikel_csv.stat()
    os.utime(artikel_csv, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
    zweite = lade_stammdaten(firma)
    assert zweite is not erste
    assert len(zweite.artikel) == 14
    assert "9999" in zweite.artikel_nach_nummer

    # geänderte Konfiguration -> ebenfalls neu laden
    assert firma.stammdaten.csv is not None
    firma.stammdaten.csv.belege = None
    dritte = lade_stammdaten(firma)
    assert dritte is not zweite
    assert dritte.belege == []


def test_lade_alle_ueberspringt_fehlerhafte_firma(settings: Settings, caplog: pytest.LogCaptureFixture) -> None:
    kaputt = settings.firma("wik_fh_seh")
    assert kaputt.stammdaten.csv is not None
    kaputt.stammdaten.csv.artikel = Path("/gibt/es/nicht/artikel.csv")
    with caplog.at_level(logging.ERROR):
        alle = lade_alle(settings)
    assert "wik_fh_seh" not in alle
    assert set(alle) == {"weinstrasse_agentur", "wik_leh", "woe_leh"}
    assert any("wik_fh_seh" in r.getMessage() and r.levelno == logging.ERROR for r in caplog.records)


def test_lade_alle_faengt_unerwartete_fehler(settings: Settings, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    original = laden.erzeuge_quelle

    def kaputt(firma: Firma):
        if firma.id == "woe_leh":
            raise RuntimeError("Überraschung")
        return original(firma)

    monkeypatch.setattr(laden, "erzeuge_quelle", kaputt)
    with caplog.at_level(logging.ERROR):
        alle = lade_alle(settings)
    assert "woe_leh" not in alle
    assert len(alle) == 3
    assert any("woe_leh" in r.getMessage() for r in caplog.records)


def test_erzeuge_quelle_csv(settings: Settings) -> None:
    quelle = erzeuge_quelle(settings.firma("wik_leh"))
    assert isinstance(quelle, CsvStammdatenQuelle)
    assert quelle.firma_id == "wik_leh"


def test_erzeuge_quelle_fehlende_konfiguration() -> None:
    firma = Firma(id="x", name="X", lexware_firmenname="X", stammdaten=StammdatenKonfig(typ="csv", csv=None))
    with pytest.raises(StammdatenFehler, match="stammdaten.csv fehlt"):
        erzeuge_quelle(firma)
    firma = Firma(id="x", name="X", lexware_firmenname="X", stammdaten=StammdatenKonfig(typ="odbc", odbc=None))
    with pytest.raises(StammdatenFehler, match="stammdaten.odbc fehlt"):
        erzeuge_quelle(firma)


def test_erzeuge_quelle_odbc(monkeypatch: pytest.MonkeyPatch) -> None:
    erzeugt: list[tuple[str, OdbcQuelle]] = []

    class FakeOdbc:
        def __init__(self, firma_id: str, konfig: OdbcQuelle) -> None:
            self.firma_id = firma_id
            erzeugt.append((firma_id, konfig))

        def artikel(self) -> list[Artikel]:
            return [Artikel(firma_id=self.firma_id, artikelnummer="1", bezeichnung="Wein 0,375 l")]

        def kunden(self) -> list[Kunde]:
            return [Kunde(firma_id=self.firma_id, kundennummer="1", name="K")]

        def lieferanten(self) -> list[Lieferant]:
            return []

        def belege(self) -> list[Beleg]:
            return [Beleg(firma_id=self.firma_id, belegart=Belegart.AUFTRAGSBESTAETIGUNG, belegnummer="AB-1")]

    monkeypatch.setattr(laden, "OdbcStammdatenQuelle", FakeOdbc)
    konfig = OdbcQuelle(dsn="LXSYDSN")
    firma = Firma(id="odbc_firma", name="O", lexware_firmenname="O", stammdaten=StammdatenKonfig(typ="odbc", odbc=konfig))
    daten = lade_stammdaten(firma)
    assert erzeugt == [("odbc_firma", konfig)]
    assert daten.artikel_nach_nummer["1"].bezeichnung == "Wein 0,375 l"
    assert len(daten.belege) == 1
    assert lade_stammdaten(firma) is daten  # Cache greift auch bei ODBC


def test_echte_odbc_quelle_ohne_pyodbc_wird_sauber_gemeldet(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    monkeypatch.setitem(sys.modules, "pyodbc", None)
    firma = Firma(
        id="odbc_firma",
        name="O",
        lexware_firmenname="O",
        stammdaten=StammdatenKonfig(typ="odbc", odbc=OdbcQuelle(dsn="LXSYDSN")),
    )
    with pytest.raises(StammdatenFehler, match="pyodbc"):
        lade_stammdaten(firma)
    settings = Settings(firmen={"odbc_firma": firma})
    assert lade_alle(settings) == {}


def test_stammdaten_bericht(settings: Settings) -> None:
    bericht = stammdaten_bericht(lade_stammdaten(settings.firma("wik_leh")))
    assert "Stammdaten Firma wik_leh" in bericht
    assert "Artikel: 13" in bericht
    assert "ohne EK-Preis: 0" in bericht
    assert "ohne VK-Preis (Preisgruppe 1): 0" in bericht
    assert "ohne Lieferantennummer: 0" in bericht
    assert "Sondervarianten (37,5 cl / 1,5 l / Holzkiste, nie automatisch): 4 (958, 1644, 2004, 3002)" in bericht
    assert "inaktiv: 1 (3003)" in bericht
    assert "Kunden: 5" in bericht
    assert "ohne Preisgruppe: 0" in bericht
    assert "Lieferanten: 4" in bericht
    assert "Belege (Dublettenprüfung): 3, davon mit Kundenbestellnummer: 1" in bericht


def test_stammdaten_bericht_datenqualitaet() -> None:
    daten = Stammdaten(
        "test",
        artikel=[
            Artikel(firma_id="test", artikelnummer="1", bezeichnung="Ohne Preise", lieferantennummer="99"),
            Artikel(firma_id="test", artikelnummer="2", bezeichnung="Nur EK 0,75 l", volumen_cl=Decimal("75"), ek_preis=Decimal("1"), lieferantennummer="7"),
        ],
        kunden=[Kunde(firma_id="test", kundennummer="1", name="Ohne PG"), Kunde(firma_id="test", kundennummer="2", name="Mit PG", preisgruppe=1, aktiv=False)],
        lieferanten=[Lieferant(firma_id="test", lieferantennummer="7", name="L")],
    )
    bericht = stammdaten_bericht(daten)
    assert "ohne EK-Preis: 1 (1)" in bericht
    assert "ohne VK-Preis (Preisgruppe 1): 2 (1, 2)" in bericht
    assert "ohne Lieferantennummer: 0" in bericht
    assert "mit unbekannter Lieferantennummer: 1 (1)" in bericht
    assert "ohne erkennbares Volumen: 1 (1)" in bericht
    assert "ohne Preisgruppe: 1 (1)" in bericht
    assert "Kunden: 2" in bericht
    assert "    inaktiv: 1 (2)" in bericht
    assert "keine Belege geladen" in bericht


def test_stammdaten_bericht_kuerzt_beispiele() -> None:
    daten = Stammdaten(
        "test",
        artikel=[Artikel(firma_id="test", artikelnummer=str(i), bezeichnung="x") for i in range(15)],
    )
    bericht = stammdaten_bericht(daten)
    assert "ohne EK-Preis: 15 (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, ... +5)" in bericht
