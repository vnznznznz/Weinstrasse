"""Tests für kontakte.liste: Kontaktliste "Ansprechpartner Abwicklung.xlsx" lesen.

Fixture: tests/fixtures/excel/ansprechpartner_beispiel.xlsx (synthetisch, erzeugt von
tests/fixtures/excel/erzeuge_ansprechpartner_beispiel.py). Alle Namen, Adressen und Nummern sind erfunden.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

from auftragsautomat.config import DateiRefKonfig, Settings
from auftragsautomat.kontakte import (
    KontakteFehler,
    Kontaktliste,
    bereinige_speditionsname,
    kontaktliste_aus_settings,
    lade_kontaktliste,
    lies_xlsx_lokal,
    quelle_text,
    zerlege_quelle,
)
from auftragsautomat.kontakte.liste import (
    erkenne_kopf,
    feld_aus_kopf,
    finde_schluessel,
    telefon_text,
    zelltext,
)
from auftragsautomat.m365.fake import FakeExcelBackend
from auftragsautomat.models import Kontaktgruppe

FIXTURE = Path(__file__).parent / "fixtures" / "excel" / "ansprechpartner_beispiel.xlsx"
SKRIPT = FIXTURE.with_name("erzeuge_ansprechpartner_beispiel.py")


@pytest.fixture()
def liste(settings: Settings) -> Kontaktliste:
    return lade_kontaktliste(FIXTURE, settings=settings)


# ---------------------------------------------------------------- lokale Datei


def test_fixture_laden_anzahl_je_gruppe(liste: Kontaktliste):
    assert len(liste.kontakte) == 17
    assert len(liste.je_gruppe(Kontaktgruppe.LIEFERANT)) == 7
    assert len(liste.je_gruppe(Kontaktgruppe.SPEDITION)) == 6
    assert len(liste.je_gruppe(Kontaktgruppe.KUNDE)) == 4
    assert list(liste.lieferanten) == [
        "Weingut Beispiel Srl",
        "Cantina Alpha & Cantina Beta & Cantina Gamma",
        "Weingut Musterhof GmbH",
        "Domaine Exemple",
    ]
    assert list(liste.speditionen) == ["Hillebrand", "Beispiel-Logistik"]  # aus tbl_Spedition_*, Schreibweise der Kundenspalte
    assert list(liste.kunden) == ["Beispielmarkt Nord", "Weinhandlung Beispiel"]
    assert liste.blaetter == {"kunde": "Kunden", "lieferant": "Lieferanten", "spedition": "Speditionen"}
    assert liste.hinweise == []
    assert liste.quelle == str(FIXTURE)


def test_lieferanten_land_sprache_quelle_und_sonderfaelle(liste: Kontaktliste):
    giulia = liste.kontakt_fuer_email("orders@weingut-beispiel.example.org")
    assert giulia is not None
    assert giulia.name == "Giulia Esempio"  # Mehrfach-Leerzeichen zusammengefasst
    assert (giulia.firma, giulia.position, giulia.land, giulia.sprache) == ("Weingut Beispiel Srl", "Orders", "Italien", "en")
    assert giulia.quelle == "Lieferanten!5"
    marco = liste.kontakt_fuer_email("export@weingut-beispiel.example.org")
    assert marco is not None
    assert marco.firma == "Weingut Beispiel Srl"  # leere Firma gilt wie die Zeile darüber
    assert marco.telefon == "390123456780"  # Telefon als Zahl, ohne ".0"
    max_muster = liste.kontakt_fuer_email("vertrieb@musterhof.example.org")
    assert max_muster is not None and (max_muster.land, max_muster.sprache) == ("Deutschland", "de")
    marie = liste.kontakt_fuer_email("marie@domaine-exemple.example.org")
    assert marie is not None and marie.position is None and marie.telefon is None and marie.quelle == "Lieferanten!17"
    assert all(k.gruppe == Kontaktgruppe.LIEFERANT for k in liste.lieferanten["Domaine Exemple"])


def test_speditionen_postfaecher_und_tabellennamen(liste: Kontaktliste):
    hillebrand = liste.speditionen["Hillebrand"]
    assert [k.name for k in hillebrand] == ["Auftrags-Postfach", "Info-Postfach", "Petra Beispiel", "Karl Beispiel"]
    assert hillebrand[0].position == "Postfach – Neue Aufträge (ausschließlich hierhin)"
    assert hillebrand[0].email == "neue-auftraege@hillebrand.example.org"
    assert hillebrand[1].position == "Sendungsnachfragen, Status"
    assert hillebrand[2].quelle == "Speditionen!7"
    assert all(k.firma == "Hillebrand" and k.gruppe == Kontaktgruppe.SPEDITION for k in hillebrand)
    logistik = liste.speditionen["Beispiel-Logistik"]
    assert [(k.name, k.position) for k in logistik] == [("Zentrale", "Disposition"), ("Rechnungen", "Buchhaltung")]
    assert liste.finde_spedition("Hillebrand Gori") == "Hillebrand"
    assert liste.finde_spedition("beispiel logistik (für Beispielmarkt Nord)") == "Beispiel-Logistik"
    assert liste.finde_spedition("Fremdspedition") is None
    assert liste.kontakte_der_spedition("Unbekannt") == []


def test_kunden_produkt_spedition_und_verlauf(liste: Kontaktliste):
    markt = liste.kunden["Beispielmarkt Nord"]
    assert [(k.name, k.produkt, k.spedition) for k in markt] == [
        ("Frau Beispiel", "Allgemein", "Beispiel-Logistik (für Beispielmarkt Nord)"),
        ("Herr Beispiel", "Weingut Beispiel", None),
        ("Buchhaltung", "Allgemein", None),
    ]
    assert markt[0].quelle == "Kunden!5" and markt[0].sprache == "de" and markt[0].firma == "Beispielmarkt Nord"
    assert liste.abwicklungsverlauf == {
        "Beispielmarkt Nord": "Bestellung per Mail; Auftragskopie an die Dispo",
        "Weinhandlung Beispiel": "Abholung durch Spedition",
    }
    assert liste.bekannte_speditionen() == ["Hillebrand", "Beispiel-Logistik"]


def test_lies_xlsx_lokal_liefert_werte_und_tabellen():
    werte, tabellen = lies_xlsx_lokal(FIXTURE)
    assert set(werte) == {"Übersicht", "Lieferanten", "Speditionen", "Kunden"}
    assert werte["Lieferanten"][3][:5] == ["Firma", "Name", "Position", "E-Mail", "Telefon"]
    assert tabellen["Speditionen"] == {"tbl_Spedition_Hillebrand": "A4:D8", "tbl_Spedition_Beispiel_Logistik": "A10:D12"}
    assert tabellen["Lieferanten"] == {}
    with pytest.raises(KontakteFehler, match="nicht gefunden"):
        lies_xlsx_lokal(FIXTURE.with_name("gibt_es_nicht.xlsx"))


def test_originaldatei_bleibt_unveraendert(tmp_path: Path, settings: Settings):
    kopie = tmp_path / "liste.xlsx"
    kopie.write_bytes(FIXTURE.read_bytes())
    vorher = kopie.stat().st_mtime_ns
    lade_kontaktliste(kopie, settings=settings)
    assert kopie.read_bytes() == FIXTURE.read_bytes() and kopie.stat().st_mtime_ns == vorher


def test_generatorskript_erzeugt_gleichwertige_datei(tmp_path: Path, settings: Settings):
    spec = importlib.util.spec_from_file_location("erzeuge_ansprechpartner_beispiel", SKRIPT)
    assert spec is not None and spec.loader is not None
    modul = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modul)
    ziel = modul.erzeuge(tmp_path / "neu.xlsx")
    neu = lade_kontaktliste(ziel, settings=settings)
    alt = lade_kontaktliste(FIXTURE, settings=settings)
    assert [k.model_dump() for k in neu.kontakte] == [k.model_dump() for k in alt.kontakte]


# ---------------------------------------------------------------- Backend-Variante


def test_backend_variante_mit_fake_excel(settings: Settings):
    backend = FakeExcelBackend()
    ref = DateiRefKonfig(lokaler_pfad=FIXTURE)
    liste = lade_kontaktliste((backend, ref), settings=settings)
    assert len(liste.kontakte) == 17
    # ohne Tabellenobjekte: Namen aus der E-Mail-Domain, abgeglichen mit Standard-Spedition und Kundenspalte
    assert list(liste.speditionen) == ["Hillebrand", "Beispiel-Logistik"]
    assert any("aus der E-Mail-Domain" in h for h in liste.hinweise)
    assert liste.kontakt_fuer_email("ZOLL@hillebrand.example.org").quelle == "Speditionen!8"  # type: ignore[union-attr]
    assert liste.quelle == str(FIXTURE)


def test_backend_variante_ohne_settings_raet_namen_aus_domain():
    liste = lade_kontaktliste((FakeExcelBackend(), DateiRefKonfig(lokaler_pfad=FIXTURE)))
    assert list(liste.speditionen) == ["Hillebrand", "Beispiel-Logistik"]
    assert any("geraten" in h and "Hillebrand" in h for h in liste.hinweise)


class _VersetztesBackend:
    """Benutzter Bereich beginnt nicht bei A1 (wie Graph ``usedRange`` bei leeren Randzeilen)."""

    def __init__(self, blaetter: dict[str, tuple[str, list[list[Any]]]]) -> None:
        self.blaetter = blaetter

    def blattnamen(self, datei: DateiRefKonfig) -> list[str]:
        return list(self.blaetter)

    def lese_bereich(self, datei: DateiRefKonfig, blatt: str, adresse: str) -> list[list[Any]]:
        raise NotImplementedError

    def lese_benutzten_bereich(self, datei: DateiRefKonfig, blatt: str) -> tuple[str, list[list[Any]]]:
        return self.blaetter[blatt]

    def schreibe_bereich(self, datei, blatt, adresse, werte, *, zahlenformat=None) -> None:
        raise NotImplementedError


def test_backend_mit_versatz_und_fehlenden_blaettern(settings: Settings):
    backend = _VersetztesBackend(
        {
            "Übersicht": ("Übersicht!A1:A1", [["Übersicht"]]),
            "Lieferanten": (
                "Lieferanten!B3:F8",
                [
                    ["Spanien", "", "", "", ""],
                    ["Firma", "Name", "Position", "E-Mail", "Telefon"],
                    ["Bodega Ejemplo", "Carlos Ejemplo", "Orders", "pedidos@bodega-ejemplo.example.org", 34912345678],
                    ["Bodega Ejemplo", "", "Buchhaltung", "", ""],
                    ["", "", "", "", ""],
                    ["Notiz ohne Kopf", "", "", "", ""],
                ],
            ),
        }
    )
    liste = lade_kontaktliste((backend, DateiRefKonfig(drive_id="d", item_id="i")), settings=settings)
    assert [k.quelle for k in liste.kontakte] == ["Lieferanten!5", "Lieferanten!6"]  # Versatz B3 berücksichtigt
    assert liste.kontakte[0].telefon == "34912345678" and liste.kontakte[0].land == "Spanien" and liste.kontakte[0].sprache == "en"
    assert liste.kontakte[1].firma == "Bodega Ejemplo" and liste.kontakte[1].name is None
    assert "Kein Blatt 'Speditionen' gefunden." in liste.hinweise and "Kein Blatt 'Kunden' gefunden." in liste.hinweise
    assert liste.speditionen == {} and liste.kunden == {}


def test_backend_fehler_wird_kontaktefehler(settings: Settings):
    with pytest.raises(KontakteFehler, match="nicht lesbar"):
        lade_kontaktliste((FakeExcelBackend(), DateiRefKonfig(drive_id="d", item_id="i")), settings=settings)


def test_kontaktliste_aus_settings(settings: Settings):
    with pytest.raises(KontakteFehler, match="nicht konfiguriert"):
        kontaktliste_aus_settings(settings)
    settings.m365.kontaktliste_datei = DateiRefKonfig(lokaler_pfad=FIXTURE)
    assert len(kontaktliste_aus_settings(settings).kontakte) == 17
    assert len(kontaktliste_aus_settings(settings, FakeExcelBackend()).kontakte) == 17


# ---------------------------------------------------------------- Hilfsfunktionen


def test_kopfzeilen_erkennung_tolerant():
    assert erkenne_kopf(["Firma", "Name", "Position", "E-Mail", "Telefon"]) == {"firma": 0, "name": 1, "position": 2, "email": 3, "telefon": 4}
    assert erkenne_kopf(["Name", "Bereich / Position", "E-Mail", "Telefon"]) == {"name": 0, "position": 1, "email": 2, "telefon": 3}
    assert erkenne_kopf(["Produkt", "Name", "Position", "e-mail", "Tel.", "Spedition", "Abwicklungsverlauf"]) == {
        "produkt": 0,
        "name": 1,
        "position": 2,
        "email": 3,
        "telefon": 4,
        "spedition": 5,
        "verlauf": 6,
    }
    # Datenzeilen sind keine Kopfzeilen (nur ein Feld bzw. ohne Name/E-Mail)
    assert erkenne_kopf(["Allgemein", "Frau Beispiel", "Disposition", "dispo@x.example.org", "", "Beispiel-Logistik"]) is None
    assert erkenne_kopf(["Italien", "", "", "", ""]) is None
    assert feld_aus_kopf("Firmenname") == "firma" and feld_aus_kopf("Ansprechpartner") == "name" and feld_aus_kopf("x@y.example.org") is None


def test_zelltext_telefon_und_speditionsname():
    assert zelltext(None) == "" and zelltext("  a   b ") == "a b" and zelltext(12.0) == "12" and zelltext(1.5) == "1.5"
    assert zelltext("Zeile 1\nZeile 2") == "Zeile 1; Zeile 2" and zelltext(True) == "WAHR"
    assert telefon_text(4.9020155501e10) == "49020155501" and telefon_text("") is None and telefon_text(" +49 1 ") == "+49 1"
    assert bereinige_speditionsname("Bouché (für Rheinberg)") == "Bouché"
    assert bereinige_speditionsname(" Stark  (for SPAR) ") == "Stark"
    assert bereinige_speditionsname("Hillebrand Gori") == "Hillebrand Gori"
    assert bereinige_speditionsname(None) == ""


def test_quelle_zerlegen():
    assert zerlege_quelle("Lieferanten!7") == ("Lieferanten", 7)
    assert zerlege_quelle("'Blatt mit Leerzeichen'!12") == ("Blatt mit Leerzeichen", 12)
    assert quelle_text("Kunden", 5) == "Kunden!5"
    for kaputt in (None, "", "Lieferanten", "Lieferanten!x", "!3"):
        with pytest.raises(KontakteFehler):
            zerlege_quelle(kaputt)


def test_finde_schluessel_normalisiert_und_unscharf():
    kandidaten = {"Hillebrand": None, "Beispiel-Logistik": None}
    assert finde_schluessel(kandidaten, "hillebrand") == "Hillebrand"
    assert finde_schluessel(kandidaten, "Hillebrand Gori GmbH") == "Hillebrand"
    assert finde_schluessel(kandidaten, "Beispiel Logistik") == "Beispiel-Logistik"
    assert finde_schluessel(kandidaten, "Beispeil-Logistik") == "Beispiel-Logistik"  # Tippfehler, unscharf
    assert finde_schluessel(kandidaten, "Fremd") is None and finde_schluessel(kandidaten, "") is None
