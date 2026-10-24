"""Ende-zu-Ende-Tests der Ausbaustufe 2 (docs/AUSBAUSTUFE_2.md, Geschäftsregeln 13-20) – Dry Run mit Fake-Backends.

Jeder Test arbeitet in einem eigenen Projektstamm in ``tmp_path`` (``AUFTRAGSAUTOMAT_HOME``):

* Konfiguration aus ``tests/fixtures/config`` (Settings, Firmen, Firmenkatalog), Alias-Tabellen, Vorlagen,
  Signaturen, Anrede-, Überwachungs- und Hillebrand-Regeln als Kopie aus ``config/``.
* Stammdaten: ``wik_leh`` aus ``tests/fixtures/stammdaten`` (plus ein synthetischer Kunde für die SAP-Bestellung),
  ``woe_leh``, ``wik_fh_seh`` und ``weinstrasse_agentur`` als echte Lexware-Exporte aus
  ``tests/fixtures/stammdaten_lexware`` (Spaltenzuordnung wie in ``config/firmen.yaml``).
* OneDrive (lokal synchronisiert, Backend ``fake``): Fixterminüberwachung für KW 43-47/2026 und Kontaktliste,
  erzeugt mit den Generatoren aus ``tests/fixtures/excel``; Ordner "Aufträge" für die Ablage.

Alle Ausgaben (Pakete, Entwürfe als ``.eml``, Dossiers, Vorschau-CSV, Protokoll, Ablage) landen im
Projektstamm unter ``tmp_path``; nichts wird gesendet, gedruckt oder in Lexware geschrieben. Alle Namen,
Adressen und Nummern sind synthetisch (Domain example.org, "Frau Beispiel", Referenz "DEMB00000").

Szenarien laut Integrationsauftrag:
  1. Beispiel-PDFs: prüfen -> buchen (Dry Run) -> Nachbereitung (Fixtermin-Vorschau, .eml-Entwürfe, Dossier)
  2. Überwachung nur lesend mit Fake-Posteingang (tests/fixtures/mails); Tracking DEMB00000 mit Fake-Quellen
  3. Rabatte: Aktion aus der Aktionszeiträume-Excel, Rabattlisten-PDF -> rabatte-einlesen -> aktionen.yaml
  4. Firmenwahl: Region -> FIRMA_UNKLAR -> firma-merken -> zweiter Lauf aus dem Gedächtnis (FIRMA_GELERNT)
  5. Prozess je Firma: WS (Angebot -> AB, WS_EN 2x), WIK (Nachbemerkung, WIK EN 2x), Revised Order
  6. Ablage: scharfer Lauf mit Fake-Lexware -> "Aufträge mit/ohne Provision"; Dry Run nur Protokoll
  7. Upload in den Eingang -> Watcher verarbeitet die Datei
  8. Streamlit-Seiten Freigabe, Heute, Dossiers ohne Exception
"""

from __future__ import annotations

import csv
import email
import email.policy
import importlib.util
import json
import logging
import shutil
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from email.message import EmailMessage
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml
from streamlit.testing.v1 import AppTest
from typer.testing import CliRunner

from auftragsautomat import benennung, cli, watcher
from auftragsautomat.config import Settings, lade_settings
from auftragsautomat.eingang import speichere_upload
from auftragsautomat.extraktion.erkennung import extrahiere
from auftragsautomat.freigabe import app, zustand
from auftragsautomat.freigabe import seiten_stufe2 as stufe2
from auftragsautomat.kommunikation.eingang import ereignisse_aus_mails
from auftragsautomat.lexware import nachbereitung as nbm
from auftragsautomat.lexware.buchen import buche
from auftragsautomat.lexware.dry_run import VORSCHAU_ENDUNG
from auftragsautomat.lexware.fake_backend import FakeArtikel, FakeLexwareBackend
from auftragsautomat.lexware.ui_automation import UiSchreiber
from auftragsautomat.m365.fabrik import Backends
from auftragsautomat.m365.fake import FakeDateiBackend, FakeExcelBackend, FakeMailBackend, mail_aus_eml
from auftragsautomat.matching.firma import QUELLE_GEDAECHTNIS
from auftragsautomat.matching.pipeline import erstelle_belegpaket
from auftragsautomat.models import (
    Belegpaket,
    BuchungsErgebnis,
    EntwurfTyp,
    Pruefcode,
    Schweregrad,
    Sendung,
    SendungStatus,
)
from auftragsautomat.protokoll import zusammenfassung
from auftragsautomat.rabatte import leere_cache
from auftragsautomat.stammdaten.laden import lade_stammdaten
from auftragsautomat.tracking.hillebrand_api import ApiAntwort
from auftragsautomat.tracking.kette import erzeuge_kette
from auftragsautomat.ueberwachung import ueberwachen
from auftragsautomat.ueberwachung.bericht import lade_letzten_bericht_mit_zusatz
from auftragsautomat.ueberwachung.regeln import tracking_abgeholt

FIXTURES = Path(__file__).parent / "fixtures"
REPO = Path(__file__).resolve().parents[1]
PDFS = FIXTURES / "pdfs"
MAILS = FIXTURES / "mails"
T = " – "
#: Fester Buchungszeitpunkt für direkte buche()-Aufrufe (Review Befund 17: keine Wanduhr in den Tests);
#: Läufe über die CLI nehmen das Datum aus dem gespeicherten Buchungsergebnis.
JETZT = datetime(2026, 10, 21, 10, 0)
HEUTE = JETZT.date()  # Buchungsdatum = Datum in den Dateinamen (benennung: Stichtag der Buchung)
UEBERWACHUNG_HEUTE = date(2026, 10, 26)  # Montag; die Fixture-Mails stammen aus der Woche davor
ABSENDER = "Max Beispiel"
VERBOTEN = ("auftragsbestätigung", "auftragsbestaetigung", "bestätigen", "bestaetigen", "verbindlich")

KIRCHHELLEN = "kundenbestellung_kirchhellen.pdf"
SPAR = "spar_sap_bestellung.pdf"
LAGER = "lagerbestellung_edeka.pdf"
SHOP = "shop_bestellung_fachhandel.pdf"
REVISED = "kundenbestellung_revised.pdf"
RABATTLISTE = "rabattliste_beispiel.pdf"

runner = CliRunner()


# ---------------------------------------------------------------------------
# Synthetische OneDrive-Dateien (über die Generatoren in tests/fixtures/excel)
# ---------------------------------------------------------------------------


def _generator(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"_e2e_{name}", FIXTURES / "excel" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    modul = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modul)
    return modul


#: Fixtermine, die der Überwachungslauf (Szenario 2) vorfindet (Lieferant, Kunde, Auftrag, Verladung, L, S, A, Bemerkung).
FIXTERMINE: dict[date, list[tuple[str, str, date | None, date | None, bool, bool, bool, str]]] = {
    # Hillebrand bestätigt (Your Ref WIK-2026-0100, Our Ref DEMB00000), das Weingut bestätigt mit Verladung 05.11.
    date(2026, 11, 9): [("Weingut Beispiel", "Beispielmarkt", date(2026, 10, 20), None, False, False, False, "Best.-Nr WIK-2026-0100, Hillebrand")],
    # Das Weingut verschiebt Bestellung 4712 auf den 03.11.2026 -> nur Vorschlag
    date(2026, 10, 30): [("Musterhof", "Weinhandlung", date(2026, 10, 12), None, True, False, False, "Best.-Nr 4712, Hillebrand")],
    # 5 Werktage voraus ohne Bestätigungen (Schwellen 10/5 erreicht) -> Nachfrage beim Weingut (Orders-Kontakt
    # aus der Kontaktliste) und bei der Spedition
    date(2026, 11, 2): [("Cide", "Spar", date(2026, 10, 21), None, False, False, False, "Best.-Nr 4500000009, Spedition Weinlogistik")],
    # morgen fällig, alles erledigt -> grün
    date(2026, 10, 27): [("Cide", "Hegmann", date(2026, 10, 5), date(2026, 10, 22), True, True, True, "Best.-Nr B-4700, Hillebrand | Ref. DEMB00001")],
}
DROPDOWN_LIEFERANTEN = ["Weingut Beispiel", "Musterhof", "Cide", "Colosi", "Bollina", "Quinta"]
DROPDOWN_KUNDEN = ["Beispielmarkt", "Weinhandlung", "Spar", "Hegmann", "Kellerei", "Vinothek"]

_KOPF_L = ["Firma", "Name", "Position", "E-Mail", "Telefon"]
_KOPF_S = ["Name", "Bereich / Position", "E-Mail", "Telefon"]
_KOPF_K = ["Produkt", "Name", "Position", "E-Mail", "Telefon", "Spedition", "Abwicklungsverlauf"]


def _kontaktliste(modul: ModuleType, ziel: Path) -> Path:
    """Kontaktliste der Fixture plus die Weingüter, Kunden und die Spedition der Beispiel-PDFs."""
    lieferanten = list(modul.LIEFERANTEN) + [
        [],
        ["Italien (Agenturweine)"],
        _KOPF_L,
        ["Cide srl", "Frau Beispiel", "Orders", "orders@cide.example.org", "+39 000 0000001"],
        ["Cantine Colosi", "Herr Beispiel", "Orders", "ordini@colosi.example.org", None],
        [],
        ["Portugal"],
        _KOPF_L,
        ["Quinta Beispiel Lda.", "Frau Exemplo", "Orders", "orders@quinta.example.org", None],
    ]
    speditionen = list(modul.SPEDITIONEN) + [
        [],
        _KOPF_S,  # Zeile 14: tbl_Spedition_Weinlogistik
        ["Auftrags-Postfach", "Postfach – Neue Aufträge (ausschließlich hierhin)", "auftraege@weinlogistik.example.org", None],
        ["Info-Postfach", "Sendungsnachfragen, Status", "status@weinlogistik.example.org", None],
    ]
    tabellen = [("tbl_Spedition_Hillebrand", "A4:D8"), ("tbl_Spedition_Beispiel_Logistik", "A10:D12"), ("tbl_Spedition_Weinlogistik", "A14:D16")]
    kunden = list(modul.KUNDEN) + [
        [],
        ["Beispiel Handels AG"],
        _KOPF_K,
        ["Allgemein", "Frau Beispiel", "Disposition", "dispo@handels-beispiel.example.org", None, "Spedition Weinlogistik", "SAP-Bestellung"],
        [],
        ["Hegmann"],
        _KOPF_K,
        ["Allgemein", "Herr Beispiel", "Einkauf", "einkauf@weinmarkt-beispiel.example.org", None, None, None],
        [],
        ["Vinothek Beispiel GmbH"],
        _KOPF_K,
        ["Allgemein", "Frau Beispiel", "Inhaberin", "info@vinothek-beispiel.example.org", None, None, None],
        [],
        ["Musterkellerei Nord GmbH"],
        _KOPF_K,
        ["Allgemein", "Herr Beispiel", "Disposition", "dispo@musterkellerei.example.org", None, None, None],
    ]
    return modul.erzeuge(ziel, lieferanten=lieferanten, speditionen=speditionen, tabellen=tabellen, kunden=kunden)


# ---------------------------------------------------------------------------
# Projektstamm
# ---------------------------------------------------------------------------


def _erzeuge_heim(tmp_path: Path) -> Path:
    heim = tmp_path / "heim"
    shutil.copytree(FIXTURES / "config", heim / "config")
    shutil.copytree(REPO / "config" / "aliase", heim / "config" / "aliase")
    shutil.copytree(REPO / "config" / "vorlagen", heim / "config" / "vorlagen")
    for name in ("signaturen.yaml", "anrede.yaml", "tracking_hillebrand.yaml", "ueberwachung.yaml"):
        shutil.copy(REPO / "config" / name, heim / "config" / name)

    # Stammdaten: wik_leh im einfachen Format (+ Kunde der SAP-Bestellung), die übrigen als Lexware-Export
    shutil.copytree(FIXTURES / "stammdaten", heim / "stammdaten")
    with (heim / "stammdaten" / "wik_leh" / "kunden.csv").open("a", encoding="cp1252") as f:
        f.write("4100;Beispiel Handels AG;Zentrale Einkauf Wein;Handelsstraße 1;5020;Salzburg;AT;1;SPAR;1\n")
    produktion = yaml.safe_load((REPO / "config" / "firmen.yaml").read_text(encoding="utf-8"))
    export = {k: v for k, v in produktion["firmen"]["wik_leh"]["stammdaten"]["csv"].items() if k not in ("artikel", "kunden", "lieferanten", "belege")}
    firmen_datei = heim / "config" / "firmen.yaml"
    firmen = yaml.safe_load(firmen_datei.read_text(encoding="utf-8"))
    for firma_id in ("woe_leh", "wik_fh_seh", "weinstrasse_agentur"):
        shutil.copytree(FIXTURES / "stammdaten_lexware" / firma_id, heim / "daten" / firma_id)
        firmen["firmen"][firma_id]["stammdaten"]["csv"] = {
            "artikel": f"daten/{firma_id}/artikel.csv",
            "kunden": f"daten/{firma_id}/kunden.csv",
            "lieferanten": f"daten/{firma_id}/lieferanten.csv",
            "belege": f"stammdaten/{firma_id}/belege.csv",
            **export,
        }
    firmen_datei.write_text(yaml.safe_dump(firmen, allow_unicode=True, sort_keys=False), encoding="utf-8")

    # OneDrive (lokal synchronisiert): Fixterminüberwachung, Kontaktliste, Ordner "Aufträge"
    onedrive = heim / "OneDrive"
    _generator("erzeuge_fixtermin_beispiel").erzeuge(
        onedrive / "Fixterminüberwachung.xlsx",
        von=date(2026, 10, 19),
        bis=date(2026, 11, 20),
        eintraege=FIXTERMINE,
        lieferanten=DROPDOWN_LIEFERANTEN,
        kunden=DROPDOWN_KUNDEN,
    )
    _kontaktliste(_generator("erzeuge_ansprechpartner_beispiel"), onedrive / "Ansprechpartner Abwicklung.xlsx")
    (onedrive / "Aufträge").mkdir()

    settings_datei = heim / "config" / "settings.yaml"
    daten = yaml.safe_load(settings_datei.read_text(encoding="utf-8"))
    daten["alias_ordner"] = "config/aliase"
    daten["m365"]["fixtermin_datei"]["lokaler_pfad"] = "OneDrive/Fixterminüberwachung.xlsx"
    daten["m365"]["kontaktliste_datei"]["lokaler_pfad"] = "OneDrive/Ansprechpartner Abwicklung.xlsx"
    daten["ablage"]["auftraege_ordner"]["lokaler_pfad"] = "OneDrive/Aufträge"
    daten["kommunikation"]["absender_name"] = ABSENDER
    daten["kommunikation"]["absender_email"] = "logistik@example.org"
    daten["kommunikation"]["cc_standard"] = ["kollegin@example.org"]
    daten["tracking"]["quellen"] = ["mail"]  # Browser/API nur mit Fakes (Szenario 2)
    settings_datei.write_text(yaml.safe_dump(daten, allow_unicode=True, sort_keys=False), encoding="utf-8")
    for ordner in ("eingang", "archiv", "logs"):
        (heim / ordner).mkdir()
    return heim


@pytest.fixture(autouse=True)
def _aufraeumen():
    """Aktionen-Cache leeren; der CLI-Log-Handler zeigt nach dem CliRunner auf einen abgeklemmten stderr."""
    leere_cache()
    yield
    leere_cache()
    if cli._log_handler is not None:
        logging.getLogger().removeHandler(cli._log_handler)
        cli._log_handler = None


@pytest.fixture()
def heim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    heim = _erzeuge_heim(tmp_path)
    monkeypatch.setenv("AUFTRAGSAUTOMAT_HOME", str(heim))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    return heim


# ---------------------------------------------------------------------------
# Hilfen
# ---------------------------------------------------------------------------


def _cli(*args: str, erwartet: int = 0) -> str:
    ergebnis = runner.invoke(cli.app, [str(a) for a in args])
    assert ergebnis.exit_code == erwartet, f"Exit {ergebnis.exit_code} statt {erwartet}:\n{ergebnis.output}"
    return ergebnis.output


def _ins_eingang(heim: Path, name: str, firma: str | None = None, *, als: str | None = None) -> Path:
    ziel = heim / "eingang" / (firma or "") / (als or name)
    ziel.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(PDFS / name, ziel)
    return ziel


def _paket_pfad(heim: Path, datei: Path) -> Path | None:
    """Gespeichertes Paket zur Eingangsdatei (Paketordner laut Settings), ``None`` wenn es keines gibt."""
    for pfad in zustand.liste_pakete(zustand.paket_ordner(lade_settings(heim))):
        if Path(zustand.lade_paket(pfad).roh.quelldatei) == datei:
            return pfad
    return None


def _pruefen(heim: Path, datei: Path) -> tuple[Belegpaket, Path]:
    _cli("pruefen", datei)
    pfad = _paket_pfad(heim, datei)
    assert pfad is not None and pfad.is_file()
    return zustand.lade_paket(pfad), pfad


def _korrigieren(heim: Path, paket: Belegpaket, pfad: Path, werte: dict[str, Any]) -> Belegpaket:
    neu = app.uebernehme_korrekturen(paket, werte, lade_settings(heim))
    zustand.speichere_paket(neu, pfad.parent, dateiname=pfad.name)
    return neu


def _hinweise(paket: Belegpaket, code: Pruefcode) -> list:
    return [h for h in paket.alle_hinweise if h.code == code]


def _eml(pfad: str | Path) -> EmailMessage:
    return email.message_from_bytes(Path(pfad).read_bytes(), policy=email.policy.default)  # type: ignore[return-value]


def _klartext(nachricht: EmailMessage) -> str:
    teil = nachricht.get_body(preferencelist=("plain",))
    assert teil is not None
    return teil.get_content()


def _html(nachricht: EmailMessage) -> str:
    teil = nachricht.get_body(preferencelist=("html",))
    assert teil is not None
    return teil.get_content()


def _ohne_verbotene_woerter(*texte: str) -> None:
    for text in texte:
        klein = text.casefold()
        for wort in VERBOTEN:
            assert wort not in klein, f"verbotenes Wort {wort!r} in {text[:200]!r}"


def _name(kuerzel: str, paket: Belegpaket, heim: Path, *, datum: date = HEUTE) -> str:
    return benennung.name_fuer_paket(kuerzel, paket, lade_settings(heim), datum=datum)


def _vorschau_zeilen(heim: Path) -> list[dict[str, str]]:
    with (heim / "logs" / nbm.VORSCHAU_CSV).open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f, delimiter=";"))


def _order_items(xml: Path) -> list[dict[str, str]]:
    wurzel = ET.parse(xml).getroot()
    return [
        {
            "artikel": item.findtext("{*}ARTICLE_ID/{*}SUPPLIER_AID") or "",
            "menge": item.findtext("{*}QUANTITY") or "",
            "preis": item.findtext("{*}ARTICLE_PRICE/{*}PRICE_AMOUNT") or "",
            "remark": " ".join(r.text or "" for r in item.findall("{*}REMARK")),
        }
        for item in wurzel.findall(".//{*}ORDER_ITEM")
    ]


# ---------------------------------------------------------------------------
# 1. Beispiel-PDFs: prüfen -> buchen (Dry Run) -> Nachbereitung
# ---------------------------------------------------------------------------


@dataclass
class Fall:
    datei: str
    firma: str
    korrekturen: dict[str, Any] = field(default_factory=dict)
    liefertag: date | None = None
    lieferant_kurz: str = ""
    kunde_kurz: str = ""
    slot_frei: bool = True
    entwuerfe: tuple[EntwurfTyp, ...] = ()
    weingueter: int = 1
    revision: bool = False
    kopie_an: str = ""
    bestellung_an: str = "orders@cide.example.org"


FAELLE = [
    Fall(  # Shopsystem-Bestellung (WIK LEH), zwei Weingüter, Liefertag ist ein Samstag -> kein Slot
        KIRCHHELLEN, "wik_leh", liefertag=date(2026, 10, 24), lieferant_kurz="Cide", kunde_kurz="Hegmann", slot_frei=False,
        entwuerfe=(EntwurfTyp.BESTELLUNG_WEINGUT, EntwurfTyp.BESTELLUNG_WEINGUT, EntwurfTyp.AUFTRAGSKOPIE_KUNDE,
                   EntwurfTyp.SPEDITION_AUFTRAG, EntwurfTyp.SPEDITION_AUFTRAG),
        weingueter=2, kopie_an="einkauf@weinmarkt-beispiel.example.org",
    ),
    Fall(  # SAP-Bestellung einer Handelskette, Menge in VE à 6
        SPAR, "wik_leh", liefertag=date(2026, 11, 3), lieferant_kurz="Cide", kunde_kurz="Spar",
        entwuerfe=(EntwurfTyp.BESTELLUNG_WEINGUT, EntwurfTyp.AUFTRAGSKOPIE_KUNDE, EntwurfTyp.SPEDITION_AUFTRAG),
        kopie_an="dispo@handels-beispiel.example.org",
    ),
    Fall(  # Lagerbestellung (Kolli × Inhalt) an WOE; Position 1 ("htr.") wählt der Anwender in der Freigabe
        LAGER, "woe_leh", korrekturen={"positionen": {1: {"zuordnung": "4001"}}}, liefertag=date(2026, 11, 5),
        lieferant_kurz="Quinta", kunde_kurz="Kellerei",
        entwuerfe=(EntwurfTyp.BESTELLUNG_WEINGUT, EntwurfTyp.AUFTRAGSKOPIE_KUNDE, EntwurfTyp.SPEDITION_AUFTRAG),
        kopie_an="dispo@musterkellerei.example.org", bestellung_an="orders@quinta.example.org",
    ),
    Fall(  # Fachhandel über die Weinstrasse Agentur: kein Einkauf, Selbstabholung, Liefertag fehlt im Dokument
        SHOP, "weinstrasse_agentur", korrekturen={"liefertag": date(2026, 11, 12)}, liefertag=date(2026, 11, 12),
        lieferant_kurz="Cide", kunde_kurz="Vinothek",
        entwuerfe=(EntwurfTyp.BESTELLUNG_WEINGUT, EntwurfTyp.AUFTRAGSKOPIE_KUNDE),
        kopie_an="info@vinothek-beispiel.example.org",
    ),
    Fall(  # Revised Order (WIK FH & SEH): drei Weingüter, Kürzel "Neu" / "Neu Kopie"
        REVISED, "wik_fh_seh", liefertag=date(2026, 11, 6), lieferant_kurz="Cide", kunde_kurz="Vinothek",
        entwuerfe=(EntwurfTyp.BESTELLUNG_WEINGUT,) * 3 + (EntwurfTyp.AUFTRAGSKOPIE_KUNDE,) + (EntwurfTyp.SPEDITION_AUFTRAG,) * 3,
        weingueter=3, revision=True, kopie_an="info@vinothek-beispiel.example.org",
    ),
]


@pytest.mark.parametrize("fall", FAELLE, ids=[f.datei.removesuffix(".pdf") for f in FAELLE])
def test_beispiel_pdf_dry_run_mit_nachbereitung(heim: Path, fall: Fall):
    datei = _ins_eingang(heim, fall.datei, fall.firma)
    paket, pfad = _pruefen(heim, datei)
    assert paket.firma_id == fall.firma and paket.firma_quelle == "eingang_ordner"
    if fall.korrekturen:
        assert paket.blockiert
        paket = _korrigieren(heim, paket, pfad, fall.korrekturen)
    assert not paket.blockiert, [h.text for h in paket.alle_hinweise if h.schweregrad is Schweregrad.FEHLER]
    assert paket.ist_revision is fall.revision

    ausgabe = _cli("buchen", pfad, "--freigeben")
    assert "Buchung simuliert (Dry Run)" in ausgabe and "Nachbereitung (Dry Run)" in ausgabe
    paket = zustand.lade_paket(pfad)
    settings = lade_settings(heim)
    nb = nbm.lade_nachbereitung(pfad, settings)
    assert nb is not None and nb.dry_run and nb.ergebnis is not None
    ergebnis: BuchungsErgebnis = nb.ergebnis
    assert ergebnis.erfolgreich and ergebnis.dry_run and ergebnis.ist_revision is fall.revision
    assert len(ergebnis.alle_einkaeufe()) == (fall.weingueter if paket.einkauf_erforderlich else 0)

    # Fixtermin-Vorschau: Datum laut Dokument, Kurznamen aus den Matchcodes, angeglichen an die Dropdown-Liste
    ft = nb.fixtermin
    assert ft is not None and ft.liefertag == fall.liefertag
    assert (ft.lieferant_kurz, ft.kunde_kurz) == (fall.lieferant_kurz, fall.kunde_kurz)
    if fall.slot_frei:
        assert ft.zeile is not None
        zellen = {z["Zelle"]: z["Wert"] for z in _vorschau_zeilen(heim)}
        assert zellen[f"D{ft.zeile}"] == fall.lieferant_kurz and zellen[f"E{ft.zeile}"] == fall.kunde_kurz
        assert any(f"Dry Run: Fixtermin {fall.liefertag:%d.%m.%Y}" in h and "nichts in der Excel geändert" in h for h in nb.hinweise)
        # Regression (Review Befund 12): jedes weitere Weingut bekommt einen eigenen Eintrag mit eigenem Kurznamen
        weitere = nb.weitere_fixtermine
        assert len(weitere) == (fall.weingueter - 1 if paket.einkauf_erforderlich else 0)
        assert all(w.zeile is not None and w.zeile != ft.zeile and w.liefertag == fall.liefertag for w in weitere)
        assert len({w.lieferant_kurz for w in weitere} | {ft.lieferant_kurz}) == len(weitere) + 1
        for w in weitere:
            assert zellen[f"D{w.zeile}"] == w.lieferant_kurz and zellen[f"E{w.zeile}"] == fall.kunde_kurz
    else:
        assert ft.zeile is None
        assert any(f"Fixtermin {fall.liefertag:%d.%m.%Y} nicht eingetragen" in h for h in nb.hinweise)

    # Entwürfe als .eml (nie gesendet), Reihenfolge und Typen laut Firma
    assert tuple(e.typ for e in nb.entwuerfe) == fall.entwuerfe
    for entwurf in nb.entwuerfe:
        assert entwurf.speicherort and Path(entwurf.speicherort).parent == heim / "logs" / "entwuerfe"
        nachricht = _eml(entwurf.speicherort)
        assert nachricht["X-Unsent"] == "1" and nachricht["Message-ID"]
        text = _klartext(nachricht)
        assert ABSENDER in text and settings.firma(fall.firma).name in text  # Signatur
        rumpf = text.split(ABSENDER)[0].strip().splitlines()
        assert 3 <= len([z for z in rumpf if z.strip()]) <= 9, rumpf  # kurzer Text, nicht nur die Signatur
    kopie = next(e for e in nb.entwuerfe if e.typ is EntwurfTyp.AUFTRAGSKOPIE_KUNDE)
    assert kopie.an == [fall.kopie_an] and kopie.cc == ["kollegin@example.org"]  # Empfänger aus der Kontaktliste
    speditionsauftraege = [e for e in nb.entwuerfe if e.typ is EntwurfTyp.SPEDITION_AUFTRAG]
    assert all(e.an == ["auftraege@weinlogistik.example.org"] for e in speditionsauftraege)  # Postfach "Neue Aufträge"
    nachricht = _eml(kopie.speicherort)  # type: ignore[arg-type]
    nummer = paket.roh.bestellnummer
    assert nachricht["Subject"] == f"Ihre Bestellung {nummer} – Auftragskopie zur Information"
    text = _klartext(nachricht)
    assert text.startswith(("Hallo Frau Beispiel", "Hallo Herr Beispiel", "Guten Tag"))
    assert "Vielen Dank für Ihre Bestellung" in text and "Kopie Ihres Auftrags" in text and "falls etwas nicht passt" in text
    assert "Spedition Spedition" not in text
    kuerzel_kopie = "Neu Kopie" if fall.revision else "Kopie"
    name_kopie = _name(kuerzel_kopie, paket, heim, datum=ergebnis.zeitpunkt.date())
    assert f"Anhang fehlt: {name_kopie}" in kopie.hinweise  # Dry Run: PDF nur geplant, Name laut Titulierung
    _ohne_verbotene_woerter(nachricht["Subject"], text, _html(nachricht), name_kopie)
    assert Path(ergebnis.pdfs["Kopie"]).name == name_kopie
    bestellungen = [e for e in nb.entwuerfe if e.typ is EntwurfTyp.BESTELLUNG_WEINGUT]
    assert bestellungen[0].an == [fall.bestellung_an]  # Orders-Kontakt des Weinguts
    erster = _klartext(_eml(bestellungen[0].speicherort))  # type: ignore[arg-type]
    assert erster.startswith(("Dear Ms", "Dear Mr", "Hello")) and "please find attached our" in erster
    assert bestellungen[0].hinweise[-1] == f"Anhang fehlt: {Path(ergebnis.pdfs['Cde']).name}"
    if fall.revision:
        assert all(e.betreff.startswith("Revised order") for e in bestellungen)
        assert f"{T}Neu{T}" in Path(ergebnis.pdfs["Cde"]).name
    # weitere Weingüter: eigene Bestellung mit eigenem Anhang
    for nummer_weingut, entwurf in enumerate(bestellungen[1:], start=2):
        assert entwurf.hinweise[-1] == f"Anhang fehlt: {Path(ergebnis.pdfs[f'Cde ({nummer_weingut})']).name}"
        assert entwurf.bezug["lieferant"] != bestellungen[0].bezug["lieferant"]

    # Dossier im Dry Run lokal: Deckblatt, Index, Kundenbestellung, Mails, Fixtermin
    assert nb.dossier is not None
    ordner = Path(nb.dossier.ordner)
    assert ordner.parent == heim / "logs" / "dossiers"
    assert (ordner / "00_Deckblatt.pdf").read_bytes().startswith(b"%PDF")
    index = json.loads((ordner / "dossier.json").read_text(encoding="utf-8"))
    namen = {d["name"] for d in index["dateien"]}
    assert "00_Deckblatt.pdf" in namen and "07_Fixtermin.txt" in namen
    assert f"01_Kundenbestellung_{_name('Kd', paket, heim, datum=ergebnis.zeitpunkt.date())}" in namen
    assert sum(1 for n in namen if n.startswith("06_Mails/")) == len(fall.entwuerfe)
    assert all((ordner / n).is_file() for n in namen)
    provision = "Aufträge mit Provision" if fall.firma == "weinstrasse_agentur" else "Aufträge ohne Provision"
    assert index["vorgeschlagener_zielordner"].startswith(provision)

    # Ablage: im Dry Run nur protokolliert, nichts kopiert
    assert nb.ablage and all(a.dry_run and not a.abgelegt for a in nb.ablage)
    assert {a.zielordner for a in nb.ablage} == {provision}
    assert not any((heim / "OneDrive" / "Aufträge").iterdir())
    assert datei.exists()  # Dry Run archiviert nicht


# ---------------------------------------------------------------------------
# 2. Überwachung nur lesend mit Fake-Posteingang; Tracking DEMB00000 mit Fake-Quellen
# ---------------------------------------------------------------------------


def _posteingang() -> list:
    return [mail_aus_eml(p) for p in sorted(MAILS.glob("*.eml"))]


def _fake_backends(settings: Settings) -> Backends:
    return Backends(
        excel=FakeExcelBackend(),
        mail=FakeMailBackend(settings.kommunikation.entwuerfe_ordner, _posteingang()),
        datei=FakeDateiBackend(settings.dossier.lokaler_ordner),
        backend="fake",
    )


def _punkt(bericht, lieferant: str, tag: date):
    return next(p for p in bericht.punkte if p.fixtermin.lieferant_kurz == lieferant and p.fixtermin.liefertag == tag)


def test_ueberwachung_nur_lesen_mit_fake_posteingang(heim: Path):
    settings = lade_settings(heim)
    xlsx = heim / "OneDrive" / "Fixterminüberwachung.xlsx"
    vorher = xlsx.read_bytes()
    backends = _fake_backends(settings)

    bericht, pfad = ueberwachen(settings, dry_run=True, heute=UEBERWACHUNG_HEUTE, backends=backends)

    # nichts geschrieben: Excel unverändert, keine Merkdatei, kein Häkchen gesetzt
    assert xlsx.read_bytes() == vorher
    assert backends.excel.schreibprotokoll == []  # type: ignore[attr-defined]
    assert not Path(settings.ueberwachung.merkdatei).exists()
    assert bericht.fehler == []
    assert {(p.fixtermin.lieferant_kurz, p.fixtermin.liefertag) for p in bericht.punkte} == {
        ("Weingut Beispiel", date(2026, 11, 9)), ("Musterhof", date(2026, 10, 30)), ("Cide", date(2026, 11, 2)), ("Cide", date(2026, 10, 27)),
    }

    # Hillebrand-Bestätigung (Your Ref WIK-2026-0100, Our Ref DEMB00000) und Bestätigung des Weinguts: im Dry Run nur
    # angekündigt; bewertet wird wie im scharfen Lauf -> beide bestätigt, keine Nachfrage, Ampel grün
    beispiel = _punkt(bericht, "Weingut Beispiel", date(2026, 11, 9))
    aktionen = " | ".join(beispiel.aktionen)
    assert "Dry Run: Häkchen 'Spedition bestätigt' würde gesetzt" in aktionen
    assert "Dry Run: Häkchen 'Lieferant bestätigt' würde gesetzt" in aktionen
    assert "Dry Run: Referenz DEMB00000 würde in die Bemerkung eingetragen" in aktionen
    assert "Dry Run: Verladedatum 05.11.2026 würde eingetragen" in aktionen
    assert not beispiel.fixtermin.spedition_bestaetigt and not beispiel.fixtermin.lieferant_bestaetigt  # Excel unverändert
    assert beispiel.ampel == "gruen" and "Nachfrage" not in aktionen
    assert beispiel.sendung is not None and beispiel.sendung.referenz == "DEMB00000"
    assert beispiel.sendung.quelle == "mail" and beispiel.sendung.status is SendungStatus.BESTAETIGT

    # Terminänderung des Weinguts (Bestellung 4712 -> 03.11.2026): nur Vorschlag, Excel bleibt
    vorschlag = next(t for t in bericht.terminaenderungen if t.fixtermin.lieferant_kurz == "Musterhof")
    assert vorschlag.neues_datum == date(2026, 11, 3) and vorschlag.uebernommen is False
    assert vorschlag.fixtermin.liefertag == date(2026, 10, 30)

    # 5 Werktage vor dem Fixtermin ohne Bestätigung -> Nachfrage beim Weingut (Entwurf an den Orders-Kontakt)
    cide = _punkt(bericht, "Cide", date(2026, 11, 2))
    assert cide.ampel == "gelb"
    nachfragen = [e for e in bericht.entwuerfe if e.typ is EntwurfTyp.NACHFRAGE_LIEFERANT]
    assert len(nachfragen) == 1 and nachfragen[0].an == ["orders@cide.example.org"]
    statusanfragen = {e.an[0]: e for e in bericht.entwuerfe if e.typ is EntwurfTyp.NACHFRAGE_SPEDITION}
    assert set(statusanfragen) == {"status@weinlogistik.example.org", "info@hillebrand.example.org"}  # Info-Postfächer
    musterhof = _punkt(bericht, "Musterhof", date(2026, 10, 30))
    assert musterhof.ampel == "rot" and "Bestätigung der Spedition überfällig" in musterhof.grund
    nachfrage = _eml(nachfragen[0].speicherort)  # type: ignore[arg-type]
    assert nachfrage["X-Unsent"] == "1" and "4500000009" in nachfrage["Subject"]
    assert _klartext(nachfrage).startswith("Dear Ms Beispiel,") and ABSENDER in _klartext(nachfrage)
    erledigt = _punkt(bericht, "Cide", date(2026, 10, 27))
    assert erledigt.ampel == "gruen"
    assert any(e.typ is EntwurfTyp.ERINNERUNG_INTERN for e in bericht.entwuerfe)
    assert all(Path(e.speicherort).parent == heim / "logs" / "entwuerfe" for e in bericht.entwuerfe)  # type: ignore[arg-type]

    # Kontaktänderung aus der Signatur: nur Vorschlag
    assert any(k.neu and "0000099" in k.neu and not k.uebernommen for k in bericht.kontaktaenderungen)

    # Bericht als Markdown + JSON, die Seite/der Befehl "heute" liest ihn wieder
    md = pfad.read_text(encoding="utf-8")
    assert "Weingut Beispiel" in md and "DEMB00000" in md and "03.11.2026" in md
    geladen = lade_letzten_bericht_mit_zusatz(Path(settings.ueberwachung.berichte_ordner))
    assert geladen is not None and len(geladen[0].punkte) == 4
    ausgabe = _cli("heute")
    assert "Vorschläge Terminänderung" in ausgabe and "Musterhof" in ausgabe and "03.11.2026" in ausgabe


def _api_transport(antworten: dict[str, Any]):
    """Fake-Transport der Hillebrand-API: Token, Sendungssuche und Ereignisse aus tests/fixtures/tracking/api."""
    aufrufe: list[str] = []

    def sende(methode: str, url: str, *, headers: Any, params: Any = None, data: Any = None, timeout: float = 30.0) -> ApiAntwort:
        aufrufe.append(f"{methode} {url}")
        if methode == "POST":
            daten = antworten["token"]
        elif url.rstrip("/").endswith("/events"):
            daten = antworten["events"]
        else:
            daten = antworten["shipments"]
        return ApiAntwort(200, daten, json.dumps(daten))

    sende.aufrufe = aufrufe  # type: ignore[attr-defined]
    return sende


API_UMGEBUNG = {
    "HILLEBRAND_API_KEY": "fixture-key",
    "HILLEBRAND_API_SECRET": "fixture-secret",
    "HILLEBRAND_API_BENUTZER": "api.beispiel@example.org",
    "HILLEBRAND_API_PASSWORT": "fixture-passwort",
}


def test_tracking_demb00000_mit_fake_quellen(heim: Path, monkeypatch: pytest.MonkeyPatch):
    settings = lade_settings(heim)
    antworten = {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in (FIXTURES / "tracking" / "api").glob("*.json")}
    html = (FIXTURES / "tracking" / "html" / "public_tracking.html").read_text(encoding="utf-8")
    geladen: list[str] = []

    def lade_public(url: str) -> str:
        geladen.append(url)
        return html

    settings.tracking.quellen = ["mail", "api", "public_tracking"]
    ereignisse = ereignisse_aus_mails(_posteingang())
    sende = _api_transport(antworten)
    kette = erzeuge_kette(settings, ereignisse, sende=sende, umgebung=API_UMGEBUNG, lade_seite_public=lade_public)
    assert kette.namen == ["mail", "api", "public_tracking"]

    # Mail-Quelle: die Bestätigung trägt "Our Ref: DEMB00000"
    aus_mail = kette.aktualisiere(Sendung(spedition="Hillebrand", referenz="DEMB00000"))
    assert (aus_mail.quelle, aus_mail.status) == ("mail", SendungStatus.BESTAETIGT)
    assert aus_mail.kundenreferenz == "WIK-2026-0100" and sende.aufrufe == []  # type: ignore[attr-defined]

    # ohne passende Mail: API (collected -> abgeholt, Meilensteine aus den Ereignissen)
    ohne_mail = erzeuge_kette(settings, [], sende=sende, umgebung=API_UMGEBUNG, lade_seite_public=lade_public)
    aus_api = ohne_mail.aktualisiere(Sendung(spedition="Hillebrand", referenz="DEMB00000"))
    assert (aus_api.quelle, aus_api.status) == ("api", SendungStatus.ZUSTELLUNG_GEPLANT)  # abgeholt, Anlieferung geplant
    assert {"shprdy", "shpsupdepcnf", "shpcusarrpln"} <= {m.code for m in aus_api.meilensteine}
    assert tracking_abgeholt(aus_api)  # Überwachung setzt daraus das Häkchen "Abgeholt"
    assert geladen == []

    # API ohne Zugangsdaten: öffentliche Sendungsverfolgung (Fake-Seite)
    nur_public = erzeuge_kette(settings, [], umgebung={}, lade_seite_public=lade_public)
    aus_public = nur_public.aktualisiere(Sendung(spedition="Hillebrand", referenz="DEMB00000"))
    assert aus_public.quelle == "public_tracking" and aus_public.status is not SendungStatus.UNBEKANNT
    assert "Quelle 'api' nicht verfügbar" in aus_public.hinweise and len(geladen) == 1

    # CLI "tracking DEMB00000" mit derselben Fake-Kette
    monkeypatch.setattr("auftragsautomat.tracking.kette.erzeuge_kette", lambda *_a, **_k: ohne_mail)
    ausgabe = _cli("tracking", "DEMB00000")
    assert "Sendung DEMB00000" in ausgabe and "Status: zustellung_geplant (Quelle api" in ausgabe and "shpsupdepcnf" in ausgabe


# ---------------------------------------------------------------------------
# 3. Rabatte: Aktionszeiträume-Excel und Rabattlisten-PDF
# ---------------------------------------------------------------------------


def _aktionen_excel(ziel: Path) -> Path:
    from openpyxl import Workbook

    mappe = Workbook()
    blatt = mappe.active
    blatt.title = "Aktionen"
    blatt.append(["Aktionszeiträume (synthetisch)"])
    blatt.append([])
    blatt.append(["Aktion", "Firma", "Kunde", "Lieferant", "Artikelnummer", "Von", "Bis", "Rabatt %"])
    blatt.append(["Herbstaktion Handel", "wik_leh", "Spar", "Cide", "1643", date(2026, 11, 1), date(2026, 11, 15), 10])
    blatt.append(["Winteraktion Handel", "wik_leh", "Spar", "Cide", "957", date(2026, 12, 1), date(2026, 12, 24), 5])
    ziel.parent.mkdir(parents=True, exist_ok=True)
    mappe.save(ziel)
    return ziel


def test_rabatt_aus_aktionszeitraeumen_und_rabattliste(heim: Path):
    # Rabattliste (PDF) einlesen und nach Prüfung übernehmen -> config/aktionen.yaml
    ausgabe = _cli("rabatte-einlesen", PDFS / RABATTLISTE)
    assert "Nur angezeigt" in ausgabe and not (heim / "config" / "aktionen.yaml").exists()
    ausgabe = _cli("rabatte-einlesen", PDFS / RABATTLISTE, "--uebernehmen")
    gedaechtnis = heim / "config" / "aktionen.yaml"
    assert gedaechtnis.is_file() and "Aktionen übernommen" in ausgabe
    gespeichert = yaml.safe_load(gedaechtnis.read_text(encoding="utf-8"))
    eintraege = gespeichert["aktionen"] if isinstance(gespeichert, dict) else gespeichert
    assert len(eintraege) == 4
    assert all(str(e["quelle"]).startswith(f"pdf:{RABATTLISTE}") for e in eintraege)
    riesling = next(e for e in eintraege if "4001" in (e.get("artikelnummern") or []))
    assert (riesling["von"], riesling["bis"], Decimal(str(riesling["rabatt_prozent"]))) == ("2026-11-01", "2026-11-30", Decimal("10"))
    liste = _cli("aktionen", "--datum", "05.11.2026")
    assert "gültig am 05.11.2026" in liste and "4001" in liste

    # Aktionszeiträume-Excel in OneDrive (nur gelesen): 10 % auf 1643 für "Spar" im November
    excel = _aktionen_excel(heim / "OneDrive" / "Aktionszeiträume.xlsx")
    settings_datei = heim / "config" / "settings.yaml"
    daten = yaml.safe_load(settings_datei.read_text(encoding="utf-8"))
    daten["rabatte"]["aktionen_datei"]["lokaler_pfad"] = "OneDrive/Aktionszeiträume.xlsx"
    settings_datei.write_text(yaml.safe_dump(daten, allow_unicode=True, sort_keys=False), encoding="utf-8")
    vorher = excel.read_bytes()
    leere_cache()

    paket, pfad = _pruefen(heim, _ins_eingang(heim, SPAR, "wik_leh"))
    gloria = next(p for p in paket.positionen if p.artikel and p.artikel.artikelnummer == "1643")
    rosato = next(p for p in paket.positionen if p.artikel and p.artikel.artikelnummer == "957")
    assert gloria.rabatt_prozent == Decimal("10") and gloria.rabatt_quelle and gloria.rabatt_quelle.startswith("aktion:excel:Aktionen:")
    assert not rosato.rabatt_prozent  # Winteraktion gilt erst ab Dezember (Liefertag 03.11.2026)
    assert gloria.vk_preis == Decimal("6.90") and gloria.vk_preis_rabattiert == Decimal("6.2100")  # VK bleibt Preisgruppe 1
    assert gloria.ek_preis == Decimal("4.20")
    vorschlag = [h for h in _hinweise(paket, Pruefcode.RABATT_VORSCHLAG) if h.position_laufnr == gloria.roh.laufnr]
    assert len(vorschlag) == 1 and vorschlag[0].schweregrad is Schweregrad.INFO
    assert any(t.aktion.bezeichnung == "Herbstaktion Handel" for t in paket.aktionen)
    assert not paket.blockiert

    ausgabe = _cli("buchen", pfad, "--freigeben")
    assert "Artikel 1643 x 3.420 @ 6,90 - 10 % = 6,21" in ausgabe or "Artikel 1643 x 3420 @ 6,90 - 10 % = 6,21" in ausgabe
    assert "Rabatte je Position" in ausgabe and "aktion:excel:Aktionen:" in ausgabe
    nb = nbm.lade_nachbereitung(pfad, lade_settings(heim))
    assert nb is not None and nb.ergebnis is not None
    ergebnis = nb.ergebnis
    assert ergebnis.einkauf is not None and ergebnis.einkauf.netto == Decimal("17388.00")  # (3420 + 720) x 4,20 EK, kein Rabatt
    assert not any("- 10 %" in w for w in ergebnis.einkauf.warnungen)  # Geschäftsregel 16: nie im Einkauf
    assert ergebnis.verkauf is not None and ergebnis.verkauf.netto == Decimal("26206.20")  # 3420 x 6,21 + 720 x 6,90
    assert ergebnis.verkauf.artefakt is not None
    items = {i["artikel"]: i for i in _order_items(Path(ergebnis.verkauf.artefakt))}
    assert items["1643"]["preis"] == "6.21" and "Rabatt 10" in items["1643"]["remark"]
    assert items["957"]["preis"] == "6.90"
    assert excel.read_bytes() == vorher  # Excel nur gelesen


# ---------------------------------------------------------------------------
# 4. Firmenwahl: Region -> fragen -> merken -> aus dem Gedächtnis
# ---------------------------------------------------------------------------


def test_firmenwahl_region_fragen_merken_und_wiederfinden(heim: Path):
    erste = _ins_eingang(heim, LAGER)  # ohne Firmen-Unterordner
    paket, pfad = _pruefen(heim, erste)
    assert paket.firma_id is None and paket.blockiert and paket.kundengruppe == "region"
    unklar = [h for h in _hinweise(paket, Pruefcode.FIRMA_UNKLAR) if h.schweregrad is Schweregrad.FEHLER]
    assert len(unklar) == 1 and unklar[0].details["kandidaten"] == "wik_leh, woe_leh"
    assert stufe2.firmenwahl_kandidaten(paket).kandidaten == ["wik_leh", "woe_leh"]  # type: ignore[union-attr]

    ausgabe = _cli("firma-merken", pfad, "--firma", "woe_leh")
    assert "Gemerkt: Kunde 'KELLEREI', Lieferant 'QUINTA' -> woe_leh" in ausgabe
    gemerkt = zustand.lade_paket(pfad)
    assert gemerkt.firma_id == "woe_leh" and gemerkt.firma_merken
    assert gemerkt.kunde.kunde is not None and gemerkt.kunde.kunde.kundennummer == "3100"
    regeln = yaml.safe_load((heim / "config" / "firmen_gedaechtnis.yaml").read_text(encoding="utf-8"))
    assert regeln[0]["firma_id"] == "woe_leh" and regeln[0]["kunde_texte"] == ["Musterkellerei Nord GmbH"]

    # zweiter Auftrag derselben Kunde+Lieferant-Kombination: Firma aus dem Gedächtnis, nicht mehr fragen
    zweite = _ins_eingang(heim, LAGER, als="lagerbestellung_nachbestellung.pdf")
    wieder, _ = _pruefen(heim, zweite)
    assert (wieder.firma_id, wieder.firma_quelle) == ("woe_leh", QUELLE_GEDAECHTNIS)
    gelernt = _hinweise(wieder, Pruefcode.FIRMA_GELERNT)
    assert len(gelernt) == 1 and gelernt[0].schweregrad is Schweregrad.INFO
    assert "gelernt am" in gelernt[0].text and gelernt[0].details["kunde"] == "KELLEREI"
    assert not _hinweise(wieder, Pruefcode.FIRMA_UNKLAR)
    assert wieder.kunde.kunde is not None and wieder.kunde.kunde.kundennummer == "3100"
    assert wieder.lieferant.lieferant is not None and wieder.lieferant.lieferant.lieferantennummer == "210"


# ---------------------------------------------------------------------------
# 5. Lexware-Prozess je Firma (Dry Run): WS, WIK, Revised Order, Lieferschein nur wenn erlaubt
# ---------------------------------------------------------------------------


def _paket_aus(heim: Path, name: str, firma: str, **korrekturen: Any) -> Belegpaket:
    settings = lade_settings(heim)
    paket = erstelle_belegpaket(extrahiere(_ins_eingang(heim, name, firma), settings), settings)
    if korrekturen:
        paket = app.uebernehme_korrekturen(paket, korrekturen, settings)
    assert not paket.blockiert, [h.text for h in paket.alle_hinweise if h.schweregrad is Schweregrad.FEHLER]
    paket.freigegeben = True
    return paket


def _vorschau(pfad: Path) -> str:
    return Path(pfad).with_suffix(VORSCHAU_ENDUNG).read_text(encoding="utf-8")


def _protokoll(heim: Path) -> list[dict[str, str]]:
    with (heim / "logs" / "protokoll.csv").open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f, delimiter=";"))


def test_prozess_weinstrasse_agentur_angebot_ws_en_und_kein_lieferschein(heim: Path):
    settings = lade_settings(heim)
    paket = _paket_aus(heim, SHOP, "weinstrasse_agentur", liefertag=date(2026, 11, 12), lieferschein=True)
    assert paket.einkauf_erforderlich is False and paket.lieferschein_erstellen is True

    ergebnis = buche(paket, settings, jetzt=lambda: JETZT)

    assert ergebnis.erfolgreich and ergebnis.dry_run
    assert ergebnis.einkauf is None and not ergebnis.einkauf_weitere  # Agentur: kein Einkauf
    assert ergebnis.angebot is not None and ergebnis.verkauf is not None  # Angebot -> Weiterführen -> AB
    assert ergebnis.lieferschein is None and "Ls" not in ergebnis.pdfs  # lieferschein_erlaubt = false
    assert any("Lieferschein für Weinstrasse Agentur nicht vorgesehen" in w for w in ergebnis.warnungen)
    datum = benennung.datum_text(HEUTE, settings)
    assert {k: Path(v).name for k, v in ergebnis.pdfs.items()} == {
        "Cde": f"CIDE{T}VINOTHEK{T}Cde{T}{datum}.pdf",
        "Speditionsauftrag": f"Selbstabholung{T}Speditionsauftrag{T}CIDE{T}VINOTHEK{T}{datum}.pdf",
        "Kopie": f"CIDE{T}VINOTHEK{T}Kopie{T}{datum}.pdf",
    }
    # "WS_EN" zweimal aus dem Angebot (Cde + Speditionsauftrag), "WS_AB" für die Auftragskopie
    for kuerzel in ("Cde", "Speditionsauftrag"):
        text = _vorschau(ergebnis.pdfs[kuerzel])
        assert "Formular: WS_EN" in text and "Belegart: AN" in text
    assert "Formular: WS_AB" in _vorschau(ergebnis.pdfs["Kopie"]) and "Belegart: AB" in _vorschau(ergebnis.pdfs["Kopie"])
    assert not any(Path(p).exists() for p in ergebnis.pdfs.values())  # Dry Run: keine PDF, nur Vorschau
    assert any("Weiterführen" in w and "simuliert" in w for w in ergebnis.verkauf.warnungen)
    assert any("Lieferdatum in der AB geleert: ja" in w for w in ergebnis.verkauf.warnungen)
    text = zusammenfassung(paket, ergebnis, firma=settings.firma("weinstrasse_agentur"))
    assert "angebot_weiterfuehren" in text and "WS_EN (x2)" in text and "AB=WS_AB" in text
    assert _protokoll(heim)[-1]["beleg_einkauf"] == "" and _protokoll(heim)[-1]["beleg_lieferschein"] == ""


def test_prozess_wik_nachbemerkung_wik_en_und_auftragskopie(heim: Path):
    settings = lade_settings(heim)
    paket = _paket_aus(heim, SPAR, "wik_leh", lieferschein=True)

    ergebnis = buche(paket, settings, jetzt=lambda: JETZT)

    assert ergebnis.erfolgreich and ergebnis.einkauf is not None
    assert ergebnis.einkauf.nachbemerkung == "4500000001"  # Kundenbestellnummer in der Nachbemerkung
    assert "Nachbemerkung: 4500000001 (Kundenbestellnummer)" in ergebnis.einkauf.warnungen
    datum = benennung.datum_text(HEUTE, settings)
    namen = {k: Path(v).name for k, v in ergebnis.pdfs.items()}
    assert namen["Cde"] == f"CIDE{T}WIK{T}4500000001{T}Cde{T}{datum}.pdf"
    assert namen["Speditionsauftrag"] == f"Spedition Weinlogistik{T}Speditionsauftrag{T}CIDE{T}4500000001{T}{datum}.pdf"
    assert namen["Kopie"] == f"CIDE{T}WIK{T}4500000001{T}Kopie{T}{datum}.pdf"
    for kuerzel in ("Cde", "Speditionsauftrag"):  # "WIK EN" zweimal aus der Einkauf-Bestellung
        text = _vorschau(ergebnis.pdfs[kuerzel])
        assert "Formular: WIK EN" in text and "Belegart: BE" in text and "Nachbemerkung: 4500000001" in text
    assert "Formular: WIK AB_LS" in _vorschau(ergebnis.pdfs["Kopie"])
    _ohne_verbotene_woerter(namen["Kopie"], _vorschau(ergebnis.pdfs["Kopie"]).split("Hinweise des Belegs:")[0])
    # Lieferschein ist erlaubt, braucht aber die AB-Nummer – die gibt es erst nach dem Speichern in Lexware
    assert ergebnis.lieferschein is None and any("Lieferschein erst nach Import möglich" in w for w in ergebnis.warnungen)
    zeile = _protokoll(heim)[-1]
    assert zeile["firma"] == "wik_leh" and zeile["Angebot-Nr"] == "(simuliert)" and zeile["Revision"] == "nein"


def test_prozess_revised_order_neu_und_neu_kopie(heim: Path):
    settings = lade_settings(heim)
    paket = _paket_aus(heim, REVISED, "wik_fh_seh")
    assert paket.ist_revision and paket.revision_von == "B-5001"
    assert any(h.code is Pruefcode.REVISION for h in paket.alle_hinweise)

    ergebnis = buche(paket, settings, jetzt=lambda: JETZT)

    assert ergebnis.erfolgreich and ergebnis.ist_revision
    datum = benennung.datum_text(HEUTE, settings)
    namen = {k: Path(v).name for k, v in ergebnis.pdfs.items()}
    assert namen["Cde"] == f"CIDE{T}WIK{T}B-5002{T}Neu{T}{datum}.pdf"
    assert namen["Cde (2)"] == f"COLOSI{T}WIK{T}B-5002{T}Neu{T}{datum}.pdf"
    assert namen["Cde (3)"] == f"BOLLINA{T}WIK{T}B-5002{T}Neu{T}{datum}.pdf"
    assert namen["Kopie"] == f"CIDE{T}WIK{T}B-5002{T}Neu Kopie{T}{datum}.pdf"
    _ohne_verbotene_woerter(namen["Kopie"])
    assert any("Revised Order: Bestellung (Cde) und AB werden neu angelegt" in w and "ersetzt B-5001" in w for w in ergebnis.warnungen)
    assert _protokoll(heim)[-1]["Revision"] == "ja (ersetzt B-5001)"


# ---------------------------------------------------------------------------
# 6. Ablage: scharfer Lauf mit Fake-Lexware -> "Aufträge mit/ohne Provision"
# ---------------------------------------------------------------------------


def _fake_lexware(settings: Settings, firma_id: str) -> FakeLexwareBackend:
    firma = settings.firma(firma_id)
    stamm = lade_stammdaten(firma, cache=False)
    artikel = [FakeArtikel(a.artikelnummer, a.bezeichnung, a.ek_preis or Decimal(0), a.vk_preis_1 or Decimal(0)) for a in stamm.artikel]
    partner = {k.kundennummer: k.name for k in stamm.kunden} | {lf.lieferantennummer: lf.name for lf in stamm.lieferanten}
    return FakeLexwareBackend(firmenname=firma.lexware_firmenname, artikel=artikel, partner=partner, heute=lambda: HEUTE)


def _scharf_buchen(settings: Settings, paket: Belegpaket) -> tuple[BuchungsErgebnis, FakeLexwareBackend]:
    assert paket.firma_id is not None
    fake = _fake_lexware(settings, paket.firma_id)
    schreiber = UiSchreiber(settings, settings.firma(paket.firma_id), fake, heute=lambda: HEUTE)
    return buche(paket, settings, dry_run=False, schreiber=schreiber, jetzt=lambda: JETZT), fake


def _anhaenge(pfad: str | Path) -> list[str]:
    return [str(a.get_filename()) for a in _eml(pfad).iter_attachments()]


def test_ablage_scharfer_fake_lauf_nach_provision(heim: Path):
    settings = lade_settings(heim)
    settings.ablage.aktiv = True
    settings.lexware.pdf_ausgabe = "drucker"
    auftraege = heim / "OneDrive" / "Aufträge"
    datum = benennung.datum_text(HEUTE, settings)

    # WIK LEH: zwei Weingüter, mit Lieferschein
    wik = _paket_aus(heim, KIRCHHELLEN, "wik_leh", lieferschein=True)
    ergebnis, fake = _scharf_buchen(settings, wik)
    assert ergebnis.erfolgreich and not ergebnis.dry_run, ergebnis.warnungen
    assert ergebnis.einkauf and ergebnis.einkauf.belegnummer and ergebnis.verkauf and ergebnis.verkauf.belegnummer
    assert ergebnis.lieferschein is not None and ergebnis.lieferschein.belegnummer
    assert fake.papier == [] and all(Path(p).is_file() for p in ergebnis.pdfs.values())  # nur PDF, nie Papier
    assert ergebnis.archiv is not None and ergebnis.archiv.is_file()  # Quelldatei archiviert (nicht gelöscht)
    ohne = auftraege / "Aufträge ohne Provision"
    assert sorted(p.name for p in ohne.iterdir()) == sorted([
        f"CIDE{T}WIK{T}B-4712{T}Kd{T}{datum}.pdf",
        f"CIDE{T}WIK{T}B-4712{T}Cde{T}{datum}.pdf",
        f"COLOSI{T}WIK{T}B-4712{T}Cde{T}{datum}.pdf",
        f"Spedition Weinlogistik{T}Speditionsauftrag{T}CIDE{T}B-4712{T}{datum}.pdf",
        f"Spedition Weinlogistik{T}Speditionsauftrag{T}COLOSI{T}B-4712{T}{datum}.pdf",
        f"CIDE{T}WIK{T}B-4712{T}Kopie{T}{datum}.pdf",
        f"CIDE{T}WIK{T}B-4712{T}Ls{T}{datum}.pdf",
    ])
    assert all(a.abgelegt and not a.dry_run for a in ergebnis.ablage)
    assert not (auftraege / "Aufträge mit Provision").exists()

    # Entwürfe tragen jetzt die echten PDFs, benannt nach Titulierung
    nb = nbm.lade_nachbereitung(wik, settings)
    assert nb is not None and not nb.dry_run
    typen = [e.typ for e in nb.entwuerfe]
    assert typen.count(EntwurfTyp.BESTELLUNG_WEINGUT) == 2 and typen.count(EntwurfTyp.SPEDITION_AUFTRAG) == 2
    bestellungen = [e for e in nb.entwuerfe if e.typ is EntwurfTyp.BESTELLUNG_WEINGUT]
    assert _anhaenge(bestellungen[0].speicherort) == [f"CIDE{T}WIK{T}B-4712{T}Cde{T}{datum}.pdf"]
    assert _anhaenge(bestellungen[1].speicherort) == [f"COLOSI{T}WIK{T}B-4712{T}Cde{T}{datum}.pdf"]
    assert bestellungen[0].betreff == f"New order {ergebnis.einkauf.belegnummer} – Hegmann"
    assert bestellungen[1].betreff == f"New order {ergebnis.einkauf_weitere[0].belegnummer} – Hegmann"
    kopie = next(e for e in nb.entwuerfe if e.typ is EntwurfTyp.AUFTRAGSKOPIE_KUNDE)
    assert _anhaenge(kopie.speicherort) == [f"CIDE{T}WIK{T}B-4712{T}Kopie{T}{datum}.pdf"]
    _ohne_verbotene_woerter(kopie.betreff, kopie.text_plain, *_anhaenge(kopie.speicherort))
    sped = [e for e in nb.entwuerfe if e.typ is EntwurfTyp.SPEDITION_AUFTRAG]
    assert _anhaenge(sped[0].speicherort) == [
        f"Spedition Weinlogistik{T}Speditionsauftrag{T}CIDE{T}B-4712{T}{datum}.pdf", f"CIDE{T}WIK{T}B-4712{T}Ls{T}{datum}.pdf",
    ]
    assert any(e.typ is EntwurfTyp.LIEFERSCHEIN for e in nb.entwuerfe)
    assert nb.fixtermin is None or nb.fixtermin.zeile is None  # 24.10.2026 ist ein Samstag
    dossier = Path(nb.dossier.ordner)  # type: ignore[union-attr]
    belege = sorted(p.name for p in dossier.iterdir() if p.name.startswith("02_Bestellung_"))
    assert belege == [f"02_Bestellung_CIDE{T}WIK{T}B-4712{T}Cde{T}{datum}.pdf", f"02_Bestellung_COLOSI{T}WIK{T}B-4712{T}Cde{T}{datum}.pdf"]
    # Regression (Review Befund 10): auch im scharfen Lauf liegt die Kundenbestellung (aus dem Archiv) im Dossier
    assert (dossier / f"01_Kundenbestellung_CIDE{T}WIK{T}B-4712{T}Kd{T}{datum}.pdf").is_file()
    assert not [p for p in nb.dossier.offene_punkte if "Quelldatei" in p and "nicht gefunden" in p]  # type: ignore[union-attr]

    # Weinstrasse Agentur -> "Aufträge mit Provision"
    ws = _paket_aus(heim, SHOP, "weinstrasse_agentur", liefertag=date(2026, 11, 12))
    ergebnis_ws, _ = _scharf_buchen(settings, ws)
    assert ergebnis_ws.erfolgreich and not ergebnis_ws.dry_run, ergebnis_ws.warnungen
    mit = auftraege / "Aufträge mit Provision"
    assert sorted(p.name for p in mit.iterdir()) == sorted([
        f"CIDE{T}VINOTHEK{T}Kd{T}{datum}.pdf",
        f"CIDE{T}VINOTHEK{T}Cde{T}{datum}.pdf",
        f"Selbstabholung{T}Speditionsauftrag{T}CIDE{T}VINOTHEK{T}{datum}.pdf",
        f"CIDE{T}VINOTHEK{T}Kopie{T}{datum}.pdf",
    ])
    assert len(list(ohne.iterdir())) == 7  # nichts in den falschen Ordner, nichts überschrieben


def test_ablage_im_dry_run_nur_protokoll(heim: Path):
    settings = lade_settings(heim)
    settings.ablage.aktiv = True
    ergebnis = buche(_paket_aus(heim, SPAR, "wik_leh"), settings, jetzt=lambda: JETZT)
    assert ergebnis.dry_run and ergebnis.ablage
    assert all(a.dry_run and not a.abgelegt and a.zielordner == "Aufträge ohne Provision" for a in ergebnis.ablage)
    assert [a.kuerzel for a in ergebnis.ablage] == ["Kd", "Cde", "Speditionsauftrag", "Kopie"]
    assert not any((heim / "OneDrive" / "Aufträge").iterdir())


# ---------------------------------------------------------------------------
# 7. Upload in den Eingang -> Watcher/verarbeite_standard
# ---------------------------------------------------------------------------


def test_upload_in_den_eingang_wird_verarbeitet_aber_nicht_gebucht(heim: Path):
    settings = lade_settings(heim)
    inhalt = (PDFS / SPAR).read_bytes()
    datei = speichere_upload("Bestellung Handel 4500000001.pdf", inhalt, settings, firma_id="wik_leh")
    assert datei == heim / "eingang" / "wik_leh" / "Bestellung Handel 4500000001.pdf" and datei.read_bytes() == inhalt
    zweite = speichere_upload("Bestellung Handel 4500000001.pdf", inhalt, settings, firma_id="wik_leh")
    assert zweite != datei and zweite.name.startswith("Bestellung Handel 4500000001 ")  # nie überschreiben
    zweite.unlink()
    rabatte = Path(settings.rabatte.rabatt_pdf_ordner)
    rabatte.mkdir(parents=True)
    shutil.copy(PDFS / RABATTLISTE, rabatte / RABATTLISTE)  # Rabattlisten-Upload ist kein Auftrag

    anzahl = watcher.starte_watch(settings, watcher.verarbeite_standard(settings), einmalig=True, stabil_sekunden=0.05)

    assert anzahl == 1
    paket_pfad = _paket_pfad(heim, datei)
    assert paket_pfad is not None
    paket = zustand.lade_paket(paket_pfad)
    assert (paket.firma_id, paket.firma_quelle) == ("wik_leh", "eingang_ordner")
    assert paket.kunde.kunde is not None and paket.kunde.kunde.kundennummer == "4100"
    assert [p.artikel.artikelnummer for p in paket.positionen if p.artikel] == ["1643", "957"]
    assert not paket.blockiert and not paket.freigegeben  # Geschäftsregel 20: buchen nur nach Freigabe
    assert not (heim / "logs" / "protokoll.csv").exists() and not (heim / "logs" / "entwuerfe").exists()
    assert _paket_pfad(heim, rabatte / RABATTLISTE) is None

    # Upload über die Freigabe-UI-Funktion: speichern und sofort prüfen
    ui = stufe2.upload_speichern_und_pruefen("Lagerbestellung.pdf", (PDFS / LAGER).read_bytes(), settings, firma_id="woe_leh")
    assert ui.datei.parent == heim / "eingang" / "woe_leh" and ui.paket.firma_id == "woe_leh"
    assert ui.paket_pfad.is_file() and not ui.paket.freigegeben


# ---------------------------------------------------------------------------
# 8. Streamlit: Seiten Freigabe, Heute, Dossiers
# ---------------------------------------------------------------------------


def test_streamlit_seiten_freigabe_heute_dossiers(heim: Path):
    settings = lade_settings(heim)
    datei = _ins_eingang(heim, SPAR, "wik_leh")
    _, pfad = _pruefen(heim, datei)
    _cli("buchen", pfad, "--freigeben")  # Dry Run -> Dossier unter logs/dossiers
    ueberwachen(settings, dry_run=True, heute=UEBERWACHUNG_HEUTE, backends=_fake_backends(settings))

    at = AppTest.from_file(str(Path(app.__file__)), default_timeout=300)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    at.sidebar.selectbox(key="eingangsdatei").set_value(f"wik_leh/{SPAR}")
    at.sidebar.button(key="einlesen").click().run()
    assert not at.exception, [e.value for e in at.exception]
    paket: Belegpaket = at.session_state[app.SS_PAKET]
    assert paket.kunde.kunde is not None and paket.kunde.kunde.kundennummer == "4100" and not at.button(key="buchen").disabled

    at.sidebar.radio(key=stufe2.SS_SEITE).set_value("Heute").run()
    assert not at.exception, [e.value for e in at.exception]
    inhalt = "\n".join(m.value for m in at.markdown) + "\n".join(str(d.value) for d in at.dataframe)
    assert "Musterhof" in inhalt and "Weingut Beispiel" in inhalt

    at.sidebar.radio(key=stufe2.SS_SEITE).set_value("Dossiers").run()
    assert not at.exception, [e.value for e in at.exception]
    assert len(at.dataframe) >= 1
    assert any("SPAR" in e.label for e in at.expander)

    at.sidebar.radio(key=stufe2.SS_SEITE).set_value("Freigabe").run()
    assert not at.exception, [e.value for e in at.exception]
