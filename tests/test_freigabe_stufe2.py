"""Tests für die Erweiterungen der Freigabe-Oberfläche (freigabe/seiten_stufe2.py, app.py).

* Reine Funktionen (Upload, Firmenwahl, Rabatt-Spalte, Rabattliste, Berichtstabelle, Vorschläge) ohne Oberfläche.
* Ablauf über ``streamlit.testing.v1.AppTest`` gegen ein temporäres Projektheim (Konfiguration aus
  tests/fixtures und config/, Fixterminüberwachung/Kontaktliste aus tests/fixtures/excel): Seiten "Heute",
  "Dossiers", "Rabatte", "Einstellungen" rendern, "Jetzt prüfen", Buchen zeigt die Nachbereitung, Firmenwahl
  merkt sich die Antwort, Rabatt-Spalte, Vorschläge übernehmen/ablehnen. Alle Daten sind synthetisch.
"""

from __future__ import annotations

import shutil
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from streamlit.testing.v1 import AppTest

from auftragsautomat.config import DateiRefKonfig, Settings, lade_settings
from auftragsautomat.eingang import UploadAbgelehnt
from auftragsautomat.freigabe import app, zustand
from auftragsautomat.freigabe import seiten_stufe2 as stufe2
from auftragsautomat.lexware import nachbereitung as nbm
from auftragsautomat.m365.fabrik import Backends
from auftragsautomat.m365.fake import FakeDateiBackend, FakeExcelBackend, FakeMailBackend
from auftragsautomat.matching.pipeline import erstelle_belegpaket
from auftragsautomat.models import (
    Aktion,
    Belegpaket,
    Eingangstyp,
    Extraktionsmethode,
    Fixtermin,
    Kontakt,
    KontaktAenderung,
    Kontaktgruppe,
    PositionsZuordnung,
    Pruefcode,
    Pruefhinweis,
    RohBestellung,
    RohPosition,
    Schweregrad,
    Terminaenderung,
    Ueberwachungsbericht,
    UeberwachungsPunkt,
)
from auftragsautomat.rabatte import alle_aktionen, lade_gespeicherte_aktionen, speichere_aktionen
from auftragsautomat.termine.register import FixterminRegister
from auftragsautomat.ueberwachung.bericht import schreibe_bericht

REPO = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"
PDF = FIXTURES / "pdfs" / "kundenbestellung_kirchhellen.pdf"
RABATTLISTE_PDF = FIXTURES / "pdfs" / "rabattliste_beispiel.pdf"
FIXTERMIN_XLSX = FIXTURES / "excel" / "fixterminueberwachung_beispiel.xlsx"
KONTAKTLISTE_XLSX = FIXTURES / "excel" / "ansprechpartner_beispiel.xlsx"
APP_DATEI = Path(app.__file__)
REGION_KUNDE = "Beispielmarkt Minden-Hannover"  # Katalog: Region -> fragen (wik_leh oder woe_leh)


# ---------------------------------------------------------------------------
# Hilfen
# ---------------------------------------------------------------------------


def _erzeuge_projektheim(tmp_path: Path) -> Path:
    heim = tmp_path / "heim"
    shutil.copytree(FIXTURES / "config", heim / "config")
    shutil.copytree(FIXTURES / "stammdaten", heim / "stammdaten")
    shutil.copytree(REPO / "config" / "aliase", heim / "config" / "aliase")
    shutil.copytree(REPO / "config" / "vorlagen", heim / "config" / "vorlagen")
    for name in ("signaturen.yaml", "anrede.yaml", "tracking_hillebrand.yaml", "ueberwachung.yaml"):
        shutil.copy(REPO / "config" / name, heim / "config" / name)
    settings_datei = heim / "config" / "settings.yaml"
    daten = yaml.safe_load(settings_datei.read_text(encoding="utf-8"))
    daten["alias_ordner"] = "config/aliase"
    daten["tracking"]["quellen"] = ["mail"]
    daten["kommunikation"]["absender_name"] = "Max Beispiel"
    daten["kommunikation"]["absender_email"] = "logistik@example.org"
    daten["m365"]["fixtermin_datei"]["lokaler_pfad"] = str(FIXTERMIN_XLSX)
    daten["m365"]["kontaktliste_datei"]["lokaler_pfad"] = str(KONTAKTLISTE_XLSX)
    settings_datei.write_text(yaml.safe_dump(daten, allow_unicode=True), encoding="utf-8")
    eingang = heim / "eingang" / "wik_leh"
    eingang.mkdir(parents=True)
    shutil.copy(PDF, eingang / PDF.name)
    (heim / "logs").mkdir()
    return heim


@pytest.fixture()
def projektheim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    heim = _erzeuge_projektheim(tmp_path)
    monkeypatch.setenv("AUFTRAGSAUTOMAT_HOME", str(heim))
    return heim


@pytest.fixture()
def ui_settings(settings: Settings, tmp_path: Path) -> Settings:
    settings.kommunikation.vorlagen_ordner = REPO / "config" / "vorlagen"
    settings.kommunikation.signaturen_datei = REPO / "config" / "signaturen.yaml"
    settings.kommunikation.anrede_datei = REPO / "config" / "anrede.yaml"
    settings.kommunikation.entwuerfe_ordner = tmp_path / "logs" / "entwuerfe"
    settings.dossier.lokaler_ordner = tmp_path / "logs" / "dossiers"
    settings.ablage.lokaler_ordner = tmp_path / "logs" / "ablage"
    settings.rabatte.rabatt_pdf_ordner = tmp_path / "eingang" / "rabatte"
    settings.rabatte.gedaechtnis_datei = tmp_path / "config" / "aktionen.yaml"
    settings.firmenwahl.gedaechtnis_datei = tmp_path / "config" / "firmen_gedaechtnis.yaml"
    settings.ueberwachung.berichte_ordner = tmp_path / "logs" / "ueberwachung"
    settings.m365.fixtermin_datei = DateiRefKonfig(lokaler_pfad=FIXTERMIN_XLSX, blatt="Fixterminüberwachung")
    settings.m365.kontaktliste_datei = DateiRefKonfig(lokaler_pfad=KONTAKTLISTE_XLSX)
    return settings


def _starte_app() -> AppTest:
    at = AppTest.from_file(str(APP_DATEI), default_timeout=300)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def _seite(at: AppTest, seite: str) -> AppTest:
    at.sidebar.radio(key=stufe2.SS_SEITE).set_value(seite).run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def _einlesen(at: AppTest, datei: str = f"wik_leh/{PDF.name}") -> AppTest:
    at.sidebar.selectbox(key="eingangsdatei").set_value(datei)
    at.sidebar.button(key="einlesen").click().run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def _region_roh(settings: Settings, name: str = "region_bestellung.pdf") -> RohBestellung:
    quelle = settings.eingang_ordner / name
    quelle.parent.mkdir(parents=True, exist_ok=True)
    quelle.write_bytes(b"%PDF-1.4\n%synthetisch\n")
    return RohBestellung(
        eingangstyp=Eingangstyp.KUNDENBESTELLUNG_PDF,
        quelldatei=quelle,
        extraktionsmethode=Extraktionsmethode.PDFPLUMBER,
        kunde_text=REGION_KUNDE,
        lieferant_text="Cide",
        bestellnummer="R-2026-0001",
        datum=date(2026, 10, 20),
        liefertag=date(2026, 11, 9),
        positionen=[RohPosition(laufnr=1, bezeichnung="Villa Armellina Gloria Secco Frizzante", menge=Decimal(60), preis=Decimal("6.90"))],
    )


def _fixtermin_aus_fixture(zeile: int = 10) -> Fixtermin:
    register = FixterminRegister(FakeExcelBackend(aus_xlsx=FIXTERMIN_XLSX), DateiRefKonfig(lokaler_pfad=FIXTERMIN_XLSX, blatt="Fixterminüberwachung"))
    ft = register.eintrag(zeile)
    assert ft is not None
    return ft


def _bericht(ft: Fixtermin) -> Ueberwachungsbericht:
    kontakt = Kontakt(gruppe=Kontaktgruppe.LIEFERANT, firma="Weingut Beispiel Srl", name="Giulia Esempio", position="Orders", email="orders@weingut-beispiel.example.org", telefon="+39 0123 456789", quelle="Lieferanten!5")
    return Ueberwachungsbericht(
        zeitpunkt=datetime(2026, 5, 6, 7, 5),
        zeitraum_bis=date(2026, 5, 27),
        punkte=[UeberwachungsPunkt(fixtermin=ft, ampel="gelb", grund="Lieferant hat noch nicht bestätigt", aktionen=["Nachfrage Lieferant"])],
        terminaenderungen=[Terminaenderung(fixtermin=ft, neues_datum=date(2026, 5, 15), grund="Weingut meldet Verladung erst am 14.05.")],
        kontaktaenderungen=[KontaktAenderung(kontakt=kontakt, feld="telefon", alt="+39 0123 456789", neu="+39 0123 456700", quelle="E-Mail-Signatur")],
    )


# ---------------------------------------------------------------------------
# Reine Funktionen
# ---------------------------------------------------------------------------


def test_upload_speichern_und_pruefen(ui_settings: Settings):
    ergebnis = stufe2.upload_speichern_und_pruefen("Kundenbestellung Kirchhellen.pdf", PDF.read_bytes(), ui_settings, firma_id="wik_leh")
    assert ergebnis.datei == ui_settings.eingang_ordner / "wik_leh" / "Kundenbestellung Kirchhellen.pdf"
    assert ergebnis.datei.is_file()
    assert ergebnis.paket.firma_id == "wik_leh" and ergebnis.paket.positionen
    assert ergebnis.paket_pfad.is_file() and ergebnis.paket_pfad.parent == zustand.paket_ordner(ui_settings)
    assert zustand.lade_paket(ergebnis.paket_pfad).roh.quelldatei == ergebnis.datei
    # Firma-Vorgabe weicht von der ermittelten Firma ab -> setze_firma
    ui_settings.eingang.upload_in_firmen_unterordner = False
    anders = stufe2.upload_speichern_und_pruefen("zweite.pdf", PDF.read_bytes(), ui_settings, firma_id="wik_fh_seh")
    assert anders.datei.parent == ui_settings.eingang_ordner and anders.paket.firma_id == "wik_fh_seh" and anders.paket.firma_quelle == "manuell"
    with pytest.raises(UploadAbgelehnt):
        stufe2.upload_speichern_und_pruefen("virus.exe", b"x", ui_settings)
    with pytest.raises(UploadAbgelehnt):
        stufe2.upload_speichern_und_pruefen("leer.pdf", b"", ui_settings)


def test_firmenwahl_kandidaten_und_firma_waehlen(ui_settings: Settings):
    paket = erstelle_belegpaket(_region_roh(ui_settings), ui_settings)
    assert paket.firma_id is None and paket.blockiert
    wahl = stufe2.firmenwahl_kandidaten(paket)
    assert wahl is not None and wahl.kandidaten == ["wik_leh", "woe_leh"] and wahl.blockiert and wahl.kundengruppe == "region"
    assert stufe2.gelernte_firma(paket) is None

    neu = stufe2.firma_waehlen(paket, "woe_leh", ui_settings, merken=True, benutzer="tester")
    assert neu.firma_id == "woe_leh" and neu.firma_merken is True and stufe2.firmenwahl_kandidaten(neu) is None
    daten = yaml.safe_load(ui_settings.firmenwahl.gedaechtnis_datei.read_text(encoding="utf-8"))
    regeln = daten["regeln"] if isinstance(daten, dict) else daten
    assert any(r["firma_id"] == "woe_leh" and "Minden" in r["kunde_kurz"] and r["gelernt_von"] == "tester" for r in regeln)

    # Nächster Auftrag desselben Kunden: Firma aus dem Gedächtnis, Info FIRMA_GELERNT
    erneut = erstelle_belegpaket(_region_roh(ui_settings), ui_settings)
    assert erneut.firma_id == "woe_leh" and erneut.firma_quelle == "gedaechtnis"
    gelernt = stufe2.gelernte_firma(erneut)
    assert gelernt is not None and gelernt.details["gelernt_von"] == "tester" and gelernt.details["gelernt_am"]
    assert stufe2.firmenwahl_kandidaten(erneut) is None

    ohne = Belegpaket(roh=_region_roh(ui_settings))
    assert stufe2.firmenwahl_kandidaten(ohne) is None


def test_rabatt_spalte_logik():
    roh = RohPosition(laufnr=1, bezeichnung="Beispielwein", menge=Decimal(6))
    position = PositionsZuordnung(roh=roh, menge=Decimal(6), vk_preis=Decimal("10.00"))
    assert stufe2.rabatt_farbe(position) is None and stufe2.rabatt_tooltip(position).startswith("Kein Positionsrabatt")
    assert stufe2.setze_rabatt_manuell(position, Decimal(0)) is False  # 0 -> 0: keine Änderung
    assert stufe2.setze_rabatt_manuell(position, Decimal("10")) is True
    assert (position.rabatt_prozent, position.rabatt_quelle) == (Decimal("10"), "manuell")
    assert position.vk_preis_rabattiert == Decimal("9.0000")
    assert stufe2.setze_rabatt_manuell(position, Decimal("10.0")) is False  # gleicher Wert
    assert stufe2.setze_rabatt_manuell(position, None) is True and position.rabatt_prozent is None and position.rabatt_quelle == "manuell"
    with pytest.raises(ValueError):
        stufe2.setze_rabatt_manuell(position, Decimal("101"))
    position.rabatt_quelle = "aktion:excel:Aktionen:7"
    position.hinweise.append(Pruefhinweis(code=Pruefcode.RABATT_VORSCHLAG, schweregrad=Schweregrad.INFO, text="Aktion 10 %", position_laufnr=1))
    assert stufe2.rabatt_farbe(position) == stufe2.GELB and "Aktion excel:Aktionen:7" in stufe2.rabatt_tooltip(position)
    position.hinweise.append(Pruefhinweis(code=Pruefcode.RABATT_ABWEICHUNG, schweregrad=Schweregrad.WARNUNG, text="Dokument 12 %", position_laufnr=1))
    assert stufe2.rabatt_farbe(position) == stufe2.ROT and len(stufe2.rabatt_hinweise(position)) == 2


def test_rabattliste_einlesen_tabelle_und_uebernehmen(ui_settings: Settings):
    assert RABATTLISTE_PDF.is_file(), "Beispiel-PDFs fehlen: python scripts/beispiel_pdfs_erzeugen.py"
    pfad, liste = stufe2.rabattliste_einlesen("Rabattliste Beispiel.pdf", RABATTLISTE_PDF.read_bytes(), ui_settings, jetzt=datetime(2026, 10, 24, 12, 0))
    assert pfad.parent == ui_settings.rabatte.rabatt_pdf_ordner and pfad.is_file()
    assert liste.aktionen and liste.quelldatei == pfad
    with pytest.raises(UploadAbgelehnt):
        stufe2.rabattliste_einlesen("liste.docx", b"x", ui_settings)
    with pytest.raises(UploadAbgelehnt):
        stufe2.rabattliste_einlesen("leer.pdf", b"", ui_settings)

    tabelle = stufe2.aktionen_zu_tabelle(liste.aktionen)
    assert len(tabelle) == len(liste.aktionen) and all(z["uebernehmen"] for z in tabelle)
    zurueck, fehler = stufe2.aktionen_aus_tabelle(tabelle, liste.aktionen)
    assert fehler == [] and [a.aktion_id for a in zurueck] == [a.aktion_id for a in liste.aktionen]
    assert all(a.quelle == b.quelle and a.von == b.von and a.bis == b.bis for a, b in zip(zurueck, liste.aktionen, strict=True))

    # Bearbeitung: eine Zeile abwählen, eine ungültig machen (Bis vor Von), Werte als float/Text wie aus dem Editor
    tabelle[0]["uebernehmen"] = False
    if len(tabelle) > 1:
        tabelle[1]["bis"] = date(2000, 1, 1)
    tabelle.append({"uebernehmen": True, "aktion_id": "", "bezeichnung": "Neu", "von": "01.12.2026", "bis": "31.12.2026", "rabatt_prozent": 5.0, "artikelnummern": "1643; 957"})
    tabelle.append({"uebernehmen": True, "aktion_id": "ohne-rabatt", "von": date(2026, 12, 1), "bis": date(2026, 12, 31)})
    zurueck, fehler = stufe2.aktionen_aus_tabelle(tabelle, liste.aktionen)
    ids = [a.aktion_id for a in zurueck]
    assert liste.aktionen[0].aktion_id not in ids
    assert any("übersprungen" in f and ("Bis" in f or "kein Rabatt" in f) for f in fehler)
    neu = next(a for a in zurueck if a.bezeichnung == "Neu")
    assert neu.von == date(2026, 12, 1) and neu.rabatt_prozent == Decimal("5.0") and neu.artikelnummern == ["1643", "957"] and neu.quelle == "ui:rabatte"
    assert "ohne-rabatt" not in ids

    ziel = stufe2.aktionen_uebernehmen(zurueck, ui_settings)
    assert ziel == ui_settings.rabatte.gedaechtnis_datei and ziel.is_file()
    gespeichert = lade_gespeicherte_aktionen(ziel)
    assert {a.aktion_id for a in gespeichert} == set(ids)
    assert {a.aktion_id for a in alle_aktionen(ui_settings, cache=False)} == set(ids)
    zeilen = stufe2.aktionen_zeilen(gespeichert, heute=date(2026, 12, 15))
    assert any(z["Aktion"] == "Neu" and z["Rabatt %"] == "5" for z in zeilen)
    assert stufe2.aktionen_zeilen(gespeichert, heute=date(2020, 1, 1)) == []


def test_ampel_und_dossier_zeilen(ui_settings: Settings):
    ft = _fixtermin_aus_fixture()
    bericht = _bericht(ft)
    zeilen = stufe2.ampel_zeilen(bericht)
    assert zeilen[0]["Ampel"] == "GELB" and zeilen[0]["Lieferant"] == "Beispielgut" and zeilen[0]["Maßnahmen"] == "Nachfrage Lieferant"
    from auftragsautomat.models import Dossier

    dossier = Dossier(name="Beispielgut-WIK-Musterhandel-4711-06.05.2026", ordner="/tmp/x", kunde_kurz="Musterhandel", lieferant_kurz="Beispielgut", bestellnummer="4711", fixtermin=date(2026, 5, 12), checkliste={"bestellung_gesendet": True, "abgeholt": False}, offene_punkte=["Lieferant bestätigt noch nicht"], vorgeschlagener_zielordner="Aufträge ohne Provision/Musterhandel/2026")
    assert stufe2.checklisten_fortschritt(dossier) == (1, 2)
    zeile = stufe2.dossier_zeilen([dossier])[0]
    assert zeile["Checkliste"] == "1/2" and zeile["Offene Punkte"] == 1 and zeile["Fixtermin"] == "12.05.2026" and zeile["Zielordner-Vorschlag"].startswith("Aufträge ohne Provision")


def test_vorschlaege_uebernehmen_mit_fake_backends(ui_settings: Settings, tmp_path: Path):
    excel = FakeExcelBackend(aus_xlsx=FIXTERMIN_XLSX)
    backends = Backends(excel=excel, mail=FakeMailBackend(tmp_path / "entwuerfe"), datei=FakeDateiBackend(tmp_path / "wurzel"), backend="fake")
    ft = _fixtermin_aus_fixture(10)  # Beispielgut / Musterhandel am 12.05.2026
    aenderung = Terminaenderung(fixtermin=ft, neues_datum=date(2026, 5, 15), grund="Weingut verlädt später")
    neu = stufe2.terminaenderung_uebernehmen(aenderung, ui_settings, dry_run=True, backends=backends)
    assert neu.liefertag == date(2026, 5, 15) and neu.zeile == 21 and aenderung.uebernommen is True
    assert neu.bemerkung and "verschoben von 12.05.2026" in neu.bemerkung
    assert excel.schreibprotokoll and (ui_settings.log_ordner / nbm.VORSCHAU_CSV).is_file()

    kontakt = Kontakt(gruppe=Kontaktgruppe.LIEFERANT, firma="Weingut Beispiel Srl", name="Giulia Esempio", position="Orders", email="orders@weingut-beispiel.example.org", telefon="+39 0123 456789", quelle="Lieferanten!5")
    excel_kontakte = FakeExcelBackend(aus_xlsx=KONTAKTLISTE_XLSX)
    backends_k = Backends(excel=excel_kontakte, mail=backends.mail, datei=backends.datei, backend="fake")
    k_aenderung = KontaktAenderung(kontakt=kontakt, feld="telefon", alt="+39 0123 456789", neu="+39 0123 456700", quelle="E-Mail-Signatur")
    assert stufe2.kontaktaenderung_uebernehmen(k_aenderung, ui_settings, dry_run=True, backends=backends_k) is True
    assert k_aenderung.uebernommen and kontakt.telefon == "+39 0123 456700"
    assert excel_kontakte.schreibprotokoll and excel_kontakte.schreibprotokoll[0].blatt == "Lieferanten"


def test_einstellungen_zeilen(ui_settings: Settings):
    zeilen = dict(stufe2.einstellungen_zeilen(ui_settings, anmeldestatus="Backend 'fake'"))
    assert zeilen["Dry Run (settings.dry_run)"] == "ja"
    assert "fake" in zeilen["Microsoft 365 Backend"] and str(FIXTERMIN_XLSX) in zeilen["Fixterminüberwachung"]
    assert "Firma wik_leh" in zeilen and "angebot_weiterfuehren" in zeilen["Firma wik_leh"]
    assert "Aufträge mit Provision" not in zeilen["Ablage"] or "weinstrasse_agentur" in zeilen["Ablage"]


# ---------------------------------------------------------------------------
# Ablauf in der Oberfläche (AppTest)
# ---------------------------------------------------------------------------


def test_app_seiten_rendern_und_jetzt_pruefen(projektheim: Path):
    at = _starte_app()
    assert at.sidebar.radio(key=stufe2.SS_SEITE).value == "Freigabe"
    assert len(at.sidebar.file_uploader) == 1  # "Auftrag hochladen"
    assert all(b.key != "buchen" for b in at.button)

    _seite(at, "Heute")
    assert any("Fixtermine und Überwachung" in h.value for h in at.header)
    assert any("Noch kein Überwachungsbericht" in i.value for i in at.info)
    assert all(b.key != "einlesen" for b in at.sidebar.button)  # Eingang nur auf der Seite Freigabe
    at.button(key="heute_pruefen").click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert any("Überwachung ausgeführt (Dry Run)" in s.value for s in at.success)
    assert list((projektheim / "logs" / "ueberwachung").glob("ueberwachung_*.json"))
    assert any("Bericht vom" in c.value for c in at.caption)
    assert not at.error

    _seite(at, "Dossiers")
    assert any("Dossiers" in h.value for h in at.header)
    assert any("Noch keine Dossiers" in i.value for i in at.info)

    _seite(at, "Rabatte")
    assert any("Rabatte" in h.value for h in at.header)
    assert any("Keine Aktionen" in i.value for i in at.info)
    assert len(at.file_uploader) == 1 and at.button(key="rabatte_einlesen").disabled

    _seite(at, "Einstellungen")
    assert any(h.value == "Einstellungen" for h in at.header)
    assert len(at.table) == 1
    assert not at.error

    _seite(at, "Freigabe")
    assert any(b.key == "einlesen" for b in at.sidebar.button)


def test_app_rabatte_seite_zeigt_gueltige_aktionen(projektheim: Path):
    heute = date.today()
    speichere_aktionen(
        [Aktion(aktion_id="pdf:test:1:1", bezeichnung="Testaktion", lieferant_kurz="Cide", von=heute, bis=heute, rabatt_prozent=Decimal("10"), quelle="pdf:test:1")],
        projektheim / "config" / "aktionen.yaml",
    )
    at = _starte_app()
    _seite(at, "Rabatte")
    assert any("Aktionen (1 von 1" in s.value for s in at.subheader)
    assert len(at.dataframe) == 1
    assert not at.error


def test_app_buchen_zeigt_nachbereitung_und_dossier_seite(projektheim: Path):
    at = _starte_app()
    _einlesen(at)
    assert at.number_input(key="pos_1_rabatt").value == 0.0
    at.button(key="buchen").click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert any("Buchung erfolgreich" in s.value for s in at.success)
    assert any(s.value.startswith("Nachbereitung") for s in at.subheader)
    markdown = "\n".join(m.value for m in at.markdown)
    assert "**Fixtermin:**" in markdown and "**Dossier:**" in markdown and "E-Mail-Entwürfe (3, nicht gesendet)" in markdown
    assert "Erzeugte Dateien (Namen laut Titulierung)" in markdown and "– Cde –" in markdown
    assert len(at.dataframe) >= 1  # Ablage-Tabelle (nur protokolliert)
    assert (projektheim / "logs" / "pakete" / "kundenbestellung_kirchhellen.nachbereitung.json").is_file()
    assert len(list((projektheim / "logs" / "entwuerfe").glob("*.eml"))) == 3
    dossiers = [p for p in (projektheim / "logs" / "dossiers").iterdir() if p.is_dir()]
    assert len(dossiers) == 1 and (dossiers[0] / "00_Deckblatt.pdf").is_file()
    assert not at.error

    _seite(at, "Dossiers")
    assert len(at.dataframe) == 1
    assert any(dossiers[0].name in e.label for e in at.expander)


def test_app_firmenwahl_merkt_sich_antwort(projektheim: Path):
    settings = lade_settings(projektheim)
    paket = erstelle_belegpaket(_region_roh(settings), settings)
    assert paket.firma_id is None and stufe2.firmenwahl_kandidaten(paket) is not None
    paketdatei = zustand.speichere_paket(paket, zustand.paket_ordner(settings))

    at = _starte_app()
    at.sidebar.selectbox(key="gespeichertes_paket").set_value(str(paketdatei))
    at.sidebar.button(key="paket_laden").click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert [o.split(" – ")[0] for o in at.selectbox(key="firmenwahl_firma").options] == ["wik_leh", "woe_leh"]
    assert at.checkbox(key="firmenwahl_merken").value is True
    assert any("Firma wählen (Kundengruppe region)" in e.value for e in at.error)

    at.selectbox(key="firmenwahl_firma").set_value("woe_leh")
    at.button(key="firmenwahl_uebernehmen").click().run()
    assert not at.exception, [e.value for e in at.exception]
    neu: Belegpaket = at.session_state[app.SS_PAKET]
    assert neu.firma_id == "woe_leh" and neu.firma_quelle == "manuell" and neu.firma_merken is True
    assert at.sidebar.selectbox(key="firma").value == "woe_leh"
    assert any("Firma woe_leh übernommen" in s.value and "gemerkt" in s.value for s in at.success)
    assert all(s.key != "firmenwahl_firma" for s in at.selectbox)
    daten = yaml.safe_load((projektheim / "config" / "firmen_gedaechtnis.yaml").read_text(encoding="utf-8"))
    regeln = daten["regeln"] if isinstance(daten, dict) else daten
    assert any(r["firma_id"] == "woe_leh" and "Minden" in r["kunde_kurz"] for r in regeln)

    # Nächstes Paket desselben Kunden kommt mit FIRMA_GELERNT -> Info "gelernt am/von" in der Oberfläche
    gelernt = erstelle_belegpaket(_region_roh(settings, "region_bestellung_2.pdf"), settings)
    assert gelernt.firma_id == "woe_leh"
    pfad2 = zustand.speichere_paket(gelernt, zustand.paket_ordner(settings))
    at.run()  # Seitenleiste liest die Paketliste neu ein
    at.sidebar.selectbox(key="gespeichertes_paket").set_value(str(pfad2))
    at.sidebar.button(key="paket_laden").click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert any("aus dem Firmen-Gedächtnis" in i.value and "gelernt am" in i.value for i in at.info)
    assert all(s.key != "firmenwahl_firma" for s in at.selectbox)


def test_app_rabatt_spalte_manuell(projektheim: Path):
    at = _starte_app()
    _einlesen(at)
    at.number_input(key="pos_1_rabatt").set_value(10.0)
    at.button(key="korrigieren").click().run()
    assert not at.exception, [e.value for e in at.exception]
    paket: Belegpaket = at.session_state[app.SS_PAKET]
    assert paket.positionen[0].rabatt_prozent == Decimal("10") and paket.positionen[0].rabatt_quelle == "manuell"
    assert paket.positionen[0].vk_preis is not None and paket.positionen[0].vk_preis_rabattiert == paket.positionen[0].vk_preis * Decimal("0.9")
    assert at.number_input(key="pos_1_rabatt").value == 10.0
    assert any("manuell" in m.value for m in at.markdown)
    assert not paket.blockiert
    paketdatei = projektheim / "logs" / "pakete" / "kundenbestellung_kirchhellen.paket.json"
    assert zustand.lade_paket(paketdatei).positionen[0].rabatt_prozent == Decimal("10")

    at.number_input(key="pos_1_rabatt").set_value(0.0)
    at.button(key="korrigieren").click().run()
    assert not at.exception, [e.value for e in at.exception]
    paket = at.session_state[app.SS_PAKET]
    assert paket.positionen[0].rabatt_prozent is None and paket.positionen[0].rabatt_quelle == "manuell"


def test_app_heute_vorschlaege_uebernehmen_und_ablehnen(projektheim: Path):
    ft = _fixtermin_aus_fixture(10)
    schreibe_bericht(_bericht(ft), projektheim / "logs" / "ueberwachung", hinweise=["Testhinweis"])
    at = _starte_app()
    _seite(at, "Heute")
    assert len(at.dataframe) == 1 and any("GELB" in m.value for m in at.markdown)
    assert at.button(key="heute_termin_0_ok") is not None and at.button(key="heute_kontakt_0_nein") is not None

    at.button(key="heute_kontakt_0_nein").click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert any("abgelehnt" in i.value for i in at.info)
    assert all(b.key != "heute_kontakt_0_nein" for b in at.button)

    at.button(key="heute_termin_0_ok").click().run()  # Dry Run: Vorschau, Excel unverändert
    assert not at.exception, [e.value for e in at.exception]
    assert any("auf 15.05.2026 verschoben" in i.value and "Dry Run" in i.value for i in at.info)
    assert (projektheim / "logs" / nbm.VORSCHAU_CSV).is_file()
    assert all(b.key != "heute_termin_0_ok" for b in at.button)
    assert not at.error
