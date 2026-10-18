"""Ende-zu-Ende-Tests über die Kommandozeile auf allen Beispieldateien (tests/fixtures).

Jeder Test arbeitet in einem temporären Projektstamm (``AUFTRAGSAUTOMAT_HOME``): Konfiguration und
Stammdaten aus ``tests/fixtures``, Alias-Tabellen als Kopie von ``config/aliase``, Eingangsordner
``eingang/wik_leh`` mit allen Beispiel-PDFs und -Mails. Geprüft werden die Geschäftsregeln aus
docs/ARCHITEKTUR.md inhaltlich (Firma, Zuordnungen, Sondervarianten, 0,00-€-Positionen,
Summenvermerke, Belegfelder, VK statt PDF-Preis in der openTRANS-XML, Protokoll-CSV, kein Einkauf
bei Weinstrasse Agentur, saubere Fehlermeldung für Scans ohne API-Schlüssel).
"""

from __future__ import annotations

import csv
import logging
import shutil
import xml.etree.ElementTree as ET
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from streamlit.testing.v1 import AppTest
from typer.testing import CliRunner

from auftragsautomat import cli
from auftragsautomat.config import lade_settings
from auftragsautomat.extraktion.erkennung import erkenne_eingangstyp
from auftragsautomat.freigabe import app, zustand
from auftragsautomat.lexware.buchen import buche
from auftragsautomat.matching.pipeline import erstelle_belegpaket
from auftragsautomat.models import (
    Belegpaket,
    Eingangstyp,
    Extraktionsmethode,
    MatchStatus,
    Pruefcode,
    RohBestellung,
    RohPosition,
    Schweregrad,
)

FIXTURES = Path(__file__).parent / "fixtures"
REPO = Path(__file__).resolve().parents[1]
PDFS = FIXTURES / "pdfs"
EMAILS = FIXTURES / "emails"
KIRCHHELLEN = "kundenbestellung_kirchhellen.pdf"
MENRATH = "kundenbestellung_menrath.pdf"
CIDE_PDF = "eigene_bestellung_cide.pdf"
SCAN = "scan_preisliste.pdf"
ALLE_DATEIEN = [KIRCHHELLEN, MENRATH, CIDE_PDF, SCAN] + sorted(p.name for p in EMAILS.iterdir())

runner = CliRunner()


# ---------------------------------------------------------------------------
# Hilfen
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _log_handler_aufraeumen():
    """Der CLI-Log-Handler zeigt nach dem CliRunner auf einen abgeklemmten stderr -> entfernen."""
    yield
    if cli._log_handler is not None:
        logging.getLogger().removeHandler(cli._log_handler)
        cli._log_handler = None


@pytest.fixture()
def heim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Temporärer Projektstamm mit allen Beispieldateien in eingang/wik_leh."""
    heim = tmp_path / "heim"
    shutil.copytree(FIXTURES / "config", heim / "config")
    shutil.copytree(FIXTURES / "stammdaten", heim / "stammdaten")
    shutil.copytree(REPO / "config" / "aliase", heim / "config" / "aliase")
    settings_datei = heim / "config" / "settings.yaml"
    daten = yaml.safe_load(settings_datei.read_text(encoding="utf-8"))
    daten["alias_ordner"] = "config/aliase"
    settings_datei.write_text(yaml.safe_dump(daten, allow_unicode=True), encoding="utf-8")
    eingang = heim / "eingang" / "wik_leh"
    eingang.mkdir(parents=True)
    for name in (KIRCHHELLEN, MENRATH, CIDE_PDF, SCAN):
        shutil.copy(PDFS / name, eingang / name)
    for datei in EMAILS.iterdir():
        shutil.copy(datei, eingang / datei.name)
    (heim / "logs").mkdir()
    monkeypatch.setenv("AUFTRAGSAUTOMAT_HOME", str(heim))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    return heim


def _eingang(heim: Path, name: str) -> Path:
    return heim / "eingang" / "wik_leh" / name


def _cli(*args: str, erwartet: int = 0) -> str:
    ergebnis = runner.invoke(cli.app, list(args))
    assert ergebnis.exit_code == erwartet, f"Exit {ergebnis.exit_code} statt {erwartet}:\n{ergebnis.output}"
    return ergebnis.output


def _json_aus(ausgabe: str) -> str:
    """Das JSON-Dokument aus der CLI-Ausgabe (Logzeilen davor/danach ignorieren)."""
    anfang = ausgabe.index("{")
    ende = ausgabe.rindex("}")
    return ausgabe[anfang : ende + 1]


def _extrahieren(heim: Path, name: str) -> RohBestellung:
    ausgabe = _cli("extrahieren", str(_eingang(heim, name)), "--json")
    return RohBestellung.model_validate_json(_json_aus(ausgabe))


def _pruefen(heim: Path, name: str, *weitere: str) -> tuple[Belegpaket, Path, str]:
    ausgabe = _cli("pruefen", str(_eingang(heim, name)), *weitere)
    pfad = heim / "logs" / "pakete" / f"{Path(name).stem}.paket.json"
    assert pfad.exists(), ausgabe
    return zustand.lade_paket(pfad), pfad, ausgabe


def _hinweise(paket: Belegpaket, code: Pruefcode):
    return [h for h in paket.alle_hinweise if h.code == code]


def _position(paket: Belegpaket, laufnr: int):
    return next(p for p in paket.positionen if p.roh.laufnr == laufnr)


def _xml_dateien(heim: Path) -> list[Path]:
    return sorted((heim / "logs" / "opentrans").glob("*.xml"))


def _order_items(xml: Path) -> list[dict[str, str]]:
    wurzel = ET.parse(xml).getroot()
    items = []
    for item in wurzel.findall(".//{*}ORDER_ITEM"):
        items.append(
            {
                "artikel": item.findtext("{*}ARTICLE_ID/{*}SUPPLIER_AID") or "",
                "menge": item.findtext("{*}QUANTITY") or "",
                "preis": item.findtext("{*}ARTICLE_PRICE/{*}PRICE_AMOUNT") or "",
            }
        )
    return items


def _remark_order(xml: Path) -> str:
    wurzel = ET.parse(xml).getroot()
    return next(r.text or "" for r in wurzel.findall(".//{*}REMARK") if r.get("type") == "order")


def _protokoll_zeilen(heim: Path) -> list[dict[str, str]]:
    with (heim / "logs" / "protokoll.csv").open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f, delimiter=";"))


# ---------------------------------------------------------------------------
# Kirchhellen: Typ A, zwei Weingüter, DAA Neptun, 11+1, Summenvermerk, Dry Run komplett
# ---------------------------------------------------------------------------


def test_kirchhellen_extrahieren(heim: Path):
    roh = _extrahieren(heim, KIRCHHELLEN)
    assert roh.eingangstyp is Eingangstyp.KUNDENBESTELLUNG_PDF
    assert roh.extraktionsmethode is Extraktionsmethode.PDFPLUMBER
    assert roh.kunde_text == "Weinmarkt Kirchhellen GmbH"
    assert roh.kundennummer_dokument == "3441"
    assert roh.bestellnummer == "B-4712"
    assert roh.datum == date(2026, 10, 15) and roh.liefertag == date(2026, 10, 24)
    assert [(p.laufnr, p.bezeichnung, p.menge, p.preis) for p in roh.positionen] == [
        (1, "Villa Armellina Gloria Secco Frizzante", Decimal(60), Decimal("6.90")),
        (2, "Villa Armellina Rosato Secco Frizzante", Decimal(24), Decimal("6.90")),
        (3, "Colosi Nero d'Avola Sicilia DOC", Decimal(12), Decimal("8.40")),
        (4, "DAA Neptun", Decimal(1), Decimal("0.00")),
    ]
    assert roh.positionen[2].jahrgang == 2022 and roh.positionen[2].hinweis == "11+1"
    assert [(s.wert, s.einheit) for s in roh.summenvermerke] == [(Decimal(97), "Fl")]
    assert roh.gesamtmenge == Decimal(97)


def test_kirchhellen_pruefen_und_dry_run_buchen(heim: Path):
    paket, pfad, ausgabe = _pruefen(heim, KIRCHHELLEN)
    assert "freigabefähig" in ausgabe and "BLOCKIERT" not in ausgabe

    # Firma über den Eingangs-Unterordner, Kunde über Alias
    assert (paket.firma_id, paket.firma_quelle) == ("wik_leh", "eingang_ordner")
    assert paket.einkauf_erforderlich is True
    assert paket.kunde.kunde is not None and paket.kunde.kunde.kundennummer == "3441"
    assert paket.kunde.status is MatchStatus.SICHER and paket.kunde.quelle == "alias"

    # Artikel: 1643/957 sicher über Alias, Colosi Nero d'Avola 2022 -> 2001 (nicht 2002/2004)
    p1, p2, p3, p4 = (_position(paket, n) for n in (1, 2, 3, 4))
    assert (p1.artikel.artikelnummer, p1.status, p1.quelle) == ("1643", MatchStatus.SICHER, "alias")
    assert (p2.artikel.artikelnummer, p2.status) == ("957", MatchStatus.SICHER)
    assert (p3.artikel.artikelnummer, p3.status) == ("2001", MatchStatus.SICHER)
    assert (p1.ek_preis, p1.vk_preis, p1.preis_dokument) == (Decimal("4.20"), Decimal("6.90"), Decimal("6.90"))
    assert p3.roh.hinweis == "11+1"  # Hinweis bleibt am Paket erhalten
    assert not _hinweise(paket, Pruefcode.PREIS_ABWEICHUNG)

    # DAA Neptun (0,00 €, Überspringen-Liste): übersprungen und gemeldet
    assert p4.status is MatchStatus.UEBERSPRUNGEN and p4.artikel is None and p4.uebersprungen
    neptun = _hinweise(paket, Pruefcode.NULLPREIS_OHNE_ARTIKEL)
    assert len(neptun) == 1 and neptun[0].position_laufnr == 4 and neptun[0].schweregrad is Schweregrad.WARNUNG

    # Summenvermerk 97 gegen Gesamtmenge 96 (ohne DAA Neptun): Info, nicht blockierend
    assert paket.gesamtmenge == Decimal(96)
    summe = _hinweise(paket, Pruefcode.SUMME_ABWEICHUNG)
    assert len(summe) == 1 and summe[0].schweregrad is Schweregrad.INFO
    assert summe[0].details["wert"] == "97" and summe[0].details["gesamtmenge"] == "96"

    # Zwei Weingüter (Cide + Colosi): Hauptlieferant Cide, Warnung, je Weingut eine Bestellung
    assert paket.lieferant.lieferant is not None and paket.lieferant.lieferant.lieferantennummer == "72"
    assert {k.lieferant.lieferantennummer for k in paket.lieferant.kandidaten} == {"72", "87"}
    mehrere = _hinweise(paket, Pruefcode.MEHRERE_LIEFERANTEN)
    assert len(mehrere) == 1 and mehrere[0].schweregrad is Schweregrad.WARNUNG
    assert mehrere[0].details["weitere"] == "87"
    assert not _hinweise(paket, Pruefcode.KEIN_LIEFERANT)

    # Belegfelder vorbelegt (Geschäftsregel 9)
    assert paket.belegfelder.model_dump() == {
        "spedition": "Spedition Weinlogistik",
        "weingut": "Cide srl / Cantine Colosi",
        "shipment": "24.10.2026",
        "kunde": "Hegmann",
    }
    assert paket.summe_netto_ek == Decimal("414.00") and paket.summe_netto_vk == Decimal("680.40")
    assert not paket.blockiert and not paket.freigegeben

    # Ohne Freigabe wird nicht gebucht
    assert "nicht freigegeben" in _cli("buchen", str(pfad), erwartet=2)

    # Dry Run buchen
    ausgabe = _cli("buchen", str(pfad), "--freigeben")
    assert "Buchung simuliert (Dry Run)" in ausgabe
    assert "Einkauf-Bestellung (72 Cide srl)" in ausgabe and "Einkauf-Bestellung (87 Cantine Colosi)" in ausgabe
    assert "Artikel 1643 x 60 @ 6,90" in ausgabe  # VK Preisgruppe 1 in der AB
    assert "Artikel 1643 x 60 @ 4,20" in ausgabe  # EK in der Einkauf-Bestellung
    assert "Endkunde (Feld Customer): 3441 Hegmann" in ausgabe
    gebucht = zustand.lade_paket(pfad)
    assert gebucht.freigegeben and gebucht.freigegeben_von

    # openTRANS-XML: VK 6,90 (nicht PDF-Preis), ohne DAA Neptun, Bestellnummer, Hinweis 11+1
    xml_dateien = _xml_dateien(heim)
    assert len(xml_dateien) == 1 and "3441" in xml_dateien[0].name and "B-4712" in xml_dateien[0].name
    items = _order_items(xml_dateien[0])
    assert items == [
        {"artikel": "1643", "menge": "60", "preis": "6.90"},
        {"artikel": "957", "menge": "24", "preis": "6.90"},
        {"artikel": "2001", "menge": "12", "preis": "8.40"},
    ]
    wurzel = ET.parse(xml_dateien[0]).getroot()
    assert wurzel.findtext(".//{*}ORDER_ID") == "B-4712"
    assert wurzel.findtext(".//{*}INVOICE_PARTY/{*}PARTY/{*}PARTY_ID") == "3441"
    assert wurzel.findtext(".//{*}TOTAL_AMOUNT") == "680.40"
    remark = _remark_order(xml_dateien[0])
    assert "Pos. 3 (2001): 11+1" in remark and "DAA Neptun" in remark and "Spedition Weinlogistik" in remark

    # Protokoll-CSV und Bericht
    zeilen = _protokoll_zeilen(heim)
    assert len(zeilen) == 1
    zeile = zeilen[0]
    assert zeile["firma"] == "wik_leh" and zeile["dry_run"] == "ja" and zeile["erfolgreich"] == "ja"
    assert zeile["kunde_nr"] == "3441" and zeile["lieferant_nr"] == "72" and zeile["bestellnummer"] == "B-4712"
    assert zeile["beleg_einkauf"] == "(simuliert) + (simuliert)" and zeile["beleg_verkauf"] == "(simuliert)"
    assert zeile["netto_ek"] == "414,00" and zeile["netto_vk"] == "680,40"
    assert zeile["positionen"] == "3" and zeile["uebersprungen"] == "1"
    assert "NULLPREIS_OHNE_ARTIKEL" in zeile["warnungen"] and "87 Cantine Colosi" in zeile["warnungen"]
    assert list((heim / "logs" / "berichte").glob("kundenbestellung_kirchhellen_*.txt"))
    assert _eingang(heim, KIRCHHELLEN).exists()  # Dry Run archiviert nicht

    # Dry Run ist wiederholbar (alte XML wird umbenannt, zweite Protokollzeile)
    _cli("buchen", str(pfad))
    assert len(_xml_dateien(heim)) == 2 and len(_protokoll_zeilen(heim)) == 2


def test_kirchhellen_dublette_blockiert(heim: Path, tmp_path: Path):
    """Dieselbe Kundenbestellnummer wie ein vorhandener Beleg (B-4711 in belege.csv) -> DUBLETTE."""
    paket, pfad, _ = _pruefen(heim, KIRCHHELLEN)
    paket.roh.bestellnummer = "B-4711"
    paket.freigegeben = True
    zustand.speichere_paket(paket, pfad.parent)
    ausgabe = _cli("buchen", str(pfad), erwartet=1)
    assert "Dublette" in ausgabe and "AB-2026-0101" in ausgabe
    assert not _xml_dateien(heim)
    zeile = _protokoll_zeilen(heim)[0]
    assert zeile["erfolgreich"] == "nein" and "DUBLETTE" in zeile["warnungen"]


# ---------------------------------------------------------------------------
# Menrath: Preisabweichung, Magnum nur manuell, Lieferadresse, drei Weingüter
# ---------------------------------------------------------------------------


def test_menrath_pruefen_blockiert_bis_korrektur(heim: Path):
    roh = _extrahieren(heim, MENRATH)
    assert roh.kunde_text == "Weinhaus Menrath" and roh.bestellnummer == "M-2026-88"
    assert roh.lieferadresse == "Lager Menrath, Industriestr. 5, 41066 Mönchengladbach"
    assert roh.liefertag is None  # "KW 44" wird nicht geraten
    assert any("KW 44" in h.text for h in roh.hinweise)

    paket, pfad, ausgabe = _pruefen(heim, MENRATH)
    assert "BLOCKIERT" in ausgabe
    assert paket.kunde.kunde is not None and paket.kunde.kunde.kundennummer == "1392"

    # Gavi: Preis laut PDF 12,00 gegen VK 12,50 -> Warnung (rot), nie stillschweigend übernommen
    gavi = _position(paket, 1)
    assert gavi.artikel.artikelnummer == "3001" and gavi.preis_abweichung == Decimal("0.50")
    abweichung = [h for h in gavi.hinweise if h.code is Pruefcode.PREIS_ABWEICHUNG]
    assert len(abweichung) == 1 and abweichung[0].schweregrad is Schweregrad.WARNUNG
    assert abweichung[0].details["vk_preis"] == "12,50" and abweichung[0].details["preis_dokument"] == "12,00"

    # Magnum 1,5 l: Sondervariante nie automatisch, nur Kandidat mit manuell_erforderlich
    magnum = _position(paket, 3)
    assert magnum.status is MatchStatus.UNSICHER and magnum.artikel is None
    k958 = next(k for k in magnum.kandidaten if k.artikel.artikelnummer == "958")
    assert k958.manuell_erforderlich is True
    assert [h.code for h in magnum.hinweise] == [Pruefcode.VARIANTE_MANUELL, Pruefcode.VK_FEHLT]

    # Liefertag unbekannt -> Shipment leer -> blockiert
    assert "shipment" in [h.details.get("feld") for h in _hinweise(paket, Pruefcode.BELEGFELD_FEHLT)]
    assert paket.blockiert
    assert "blockiert" in _cli("buchen", str(pfad), "--freigeben", erwartet=2)

    # Korrektur wie in der Freigabe: Magnum bestätigen, Liefertag setzen -> drei Weingüter
    settings = lade_settings(heim)
    korrigiert = app.uebernehme_korrekturen(
        paket, {"positionen": {3: {"zuordnung": "958"}}, "liefertag": date(2026, 10, 30)}, settings
    )
    assert not korrigiert.blockiert, [h.text for h in korrigiert.alle_hinweise if h.schweregrad is Schweregrad.FEHLER]
    magnum = _position(korrigiert, 3)
    assert magnum.status is MatchStatus.MANUELL and magnum.artikel.artikelnummer == "958"
    assert (magnum.ek_preis, magnum.vk_preis) == (Decimal("9.50"), Decimal("14.90"))
    assert korrigiert.belegfelder.shipment == "30.10.2026"
    assert korrigiert.belegfelder.weingut == "Bollina srl / Cide srl / Cantine Colosi"
    mehrere = _hinweise(korrigiert, Pruefcode.MEHRERE_LIEFERANTEN)
    assert len(mehrere) == 1 and mehrere[0].details["positionen"] == "138:1;72:3;87:2"
    zustand.speichere_paket(korrigiert, pfad.parent)

    ausgabe = _cli("buchen", str(pfad), "--freigeben")
    assert "Buchung simuliert (Dry Run)" in ausgabe
    assert ausgabe.count("Einkauf-Bestellung (") >= 3
    xml = _xml_dateien(heim)[0]
    items = _order_items(xml)
    assert items == [
        {"artikel": "3001", "menge": "36", "preis": "12.50"},  # VK aus dem Stamm, nicht 12,00
        {"artikel": "2003", "menge": "6", "preis": "8.60"},
        {"artikel": "958", "menge": "6", "preis": "14.90"},
    ]
    wurzel = ET.parse(xml).getroot()
    buyer = wurzel.find(".//{*}BUYER_PARTY/{*}PARTY/{*}ADDRESS")
    assert buyer is not None, "abweichende Lieferadresse muss als BUYER_PARTY übernommen werden"
    buyer_text = " ".join(t.text or "" for t in buyer.iter())
    assert "Lager Menrath" in buyer_text and "Mönchengladbach" in buyer_text
    assert "Lieferadresse: Lager Menrath" in _remark_order(xml)
    zeile = _protokoll_zeilen(heim)[-1]
    assert zeile["kunde_nr"] == "1392" and zeile["beleg_einkauf"].count("(simuliert)") == 3
    assert zeile["netto_ek"] == "369,60"  # 36 x 7,80 + 6 x 5,30 + 6 x 9,50


# ---------------------------------------------------------------------------
# Typ B: eigene Bestellung (PDF und E-Mails)
# ---------------------------------------------------------------------------


def test_eigene_bestellung_pdf_typ_b(heim: Path):
    roh = _extrahieren(heim, CIDE_PDF)
    assert roh.eingangstyp is Eingangstyp.EIGENE_BESTELLUNG_EMAIL
    assert roh.lieferant_text and roh.lieferant_text.startswith("Cide srl")
    assert roh.kunde_text == "Menrath" and roh.belegnummer == "BE-2026-0077"
    assert [p.artikelnummer for p in roh.positionen] == ["1643", "957"]

    paket, pfad, ausgabe = _pruefen(heim, CIDE_PDF)
    assert "freigabefähig" in ausgabe
    assert paket.lieferant.lieferant is not None and paket.lieferant.lieferant.lieferantennummer == "72"
    assert paket.kunde.kunde is not None and paket.kunde.kunde.kundennummer == "1392"
    assert [(p.artikel.artikelnummer, p.status) for p in paket.positionen] == [
        ("1643", MatchStatus.SICHER),
        ("957", MatchStatus.SICHER),
    ]
    assert all(p.quelle == "artikelnummer" for p in paket.positionen)
    assert paket.belegfelder.weingut == "Cide srl" and paket.belegfelder.kunde == "Weinhaus Menrath"
    assert not _hinweise(paket, Pruefcode.MEHRERE_LIEFERANTEN)
    assert paket.summe_netto_ek == Decimal("352.80")

    ausgabe = _cli("buchen", str(pfad), "--freigeben")
    assert "Buchung simuliert (Dry Run)" in ausgabe
    assert "Einkauf-Bestellung an Lieferant 72 Cide srl" in ausgabe
    assert "Endkunde (Feld Customer): 1392 Weinhaus Menrath" in ausgabe
    assert _protokoll_zeilen(heim)[-1]["beleg_einkauf"] == "(simuliert)"


@pytest.mark.parametrize(
    ("name", "lieferant", "kunde_nr", "artikel"),
    [
        ("bestellung_cide_mit_pdf_anhang.eml", "72", "1392", ["1643", "957"]),
        ("bestellung_cide_positionen_im_text.eml", "72", "1392", ["1643", "957", None]),
        ("bestellung_bollina.txt", "138", "3441", ["3001", None]),
    ],
)
def test_email_bestellungen(heim: Path, name: str, lieferant: str, kunde_nr: str, artikel: list[str | None]):
    roh = _extrahieren(heim, name)
    assert roh.eingangstyp is Eingangstyp.EIGENE_BESTELLUNG_EMAIL
    assert roh.extraktionsmethode is Extraktionsmethode.EMAIL
    paket, _, _ = _pruefen(heim, name)
    assert paket.firma_id == "wik_leh"
    assert paket.lieferant.lieferant is not None and paket.lieferant.lieferant.lieferantennummer == lieferant
    assert paket.kunde.kunde is not None and paket.kunde.kunde.kundennummer == kunde_nr
    assert [p.artikel.artikelnummer if p.artikel else None for p in paket.positionen] == artikel
    for p in paket.positionen:
        if p.artikel is None:  # Magnum / 0,375 l: Sondervarianten nur manuell
            assert p.status is MatchStatus.UNSICHER
            assert any(h.code is Pruefcode.VARIANTE_MANUELL for h in p.hinweise)
            assert any(k.manuell_erforderlich for k in p.kandidaten)
            assert paket.blockiert


# ---------------------------------------------------------------------------
# Typ C: Scan ohne API-Schlüssel -> saubere Fehlermeldung
# ---------------------------------------------------------------------------


def test_scan_ohne_api_schluessel(heim: Path):
    scan = _eingang(heim, SCAN)
    assert erkenne_eingangstyp(scan) is Eingangstyp.PREISLISTE_SCAN
    for befehl in ("extrahieren", "pruefen"):
        ergebnis = runner.invoke(cli.app, [befehl, str(scan)])
        assert ergebnis.exit_code == 1
        assert "Fehler: Claude API: kein API-Schlüssel" in ergebnis.output
        assert "ANTHROPIC_API_KEY" in ergebnis.output and "Eingangstyp C" in ergebnis.output
        assert "Traceback" not in ergebnis.output and "TypeError" not in ergebnis.output
    assert not (heim / "logs" / "pakete").exists()


# ---------------------------------------------------------------------------
# Weinstrasse Agentur: keine Einkauf-Bestellung
# ---------------------------------------------------------------------------


def test_weinstrasse_agentur_ohne_einkauf(heim: Path):
    settings = lade_settings(heim)
    quelle = heim / "eingang" / "weinstrasse_agentur" / "provision_sonnenhang.pdf"
    quelle.parent.mkdir(parents=True)
    quelle.write_bytes(b"%PDF-1.4 platzhalter")
    roh = RohBestellung(
        eingangstyp=Eingangstyp.KUNDENBESTELLUNG_PDF,
        quelldatei=quelle,
        extraktionsmethode=Extraktionsmethode.PDFPLUMBER,
        kunde_text="Weingut Sonnenhang",
        lieferant_text="Weingut Sonnenhang",
        bestellnummer="PROV-2026-07",
        datum=date(2026, 10, 1),
        liefertag=date(2026, 10, 20),
        positionen=[
            RohPosition(laufnr=1, bezeichnung="Provision Weinvermittlung", menge=Decimal(1), preis=Decimal("150"), artikelnummer="8001")
        ],
    )
    paket = erstelle_belegpaket(roh, settings)
    assert (paket.firma_id, paket.firma_quelle) == ("weinstrasse_agentur", "eingang_ordner")
    assert paket.einkauf_erforderlich is False
    assert paket.kunde.kunde is not None and paket.kunde.kunde.kundennummer == "9001"
    assert paket.lieferant.lieferant is None
    assert not _hinweise(paket, Pruefcode.KEIN_LIEFERANT)  # kein Lieferant nötig
    assert paket.belegfelder.model_dump() == {
        "spedition": "Selbstabholung",
        "weingut": "Weingut Sonnenhang",
        "shipment": "20.10.2026",
        "kunde": "Weingut Sonnenhang",
    }
    assert not paket.blockiert, [h.text for h in paket.alle_hinweise]
    paket.freigegeben = True
    ergebnis = buche(paket, settings)
    assert ergebnis.erfolgreich and ergebnis.dry_run
    assert ergebnis.einkauf is None and not ergebnis.einkauf_weitere
    assert ergebnis.verkauf is not None and ergebnis.verkauf.netto == Decimal("150")
    assert any("Keine Einkauf-Bestellung" in w for w in ergebnis.warnungen)
    assert ergebnis.protokoll == heim / "logs" / "protokoll.csv"
    zeile = _protokoll_zeilen(heim)[-1]
    assert zeile["firma"] == "weinstrasse_agentur" and zeile["beleg_einkauf"] == "" and zeile["netto_ek"] == ""

    # Dieselbe Firma über die CLI erzwingen: Kunde Hegmann gehört nicht zur Agentur, aber kein KEIN_LIEFERANT
    ausgabe = _cli("pruefen", str(_eingang(heim, KIRCHHELLEN)), "--firma", "weinstrasse_agentur")
    assert "KEIN_KUNDE" in ausgabe and "KEIN_LIEFERANT" not in ausgabe


# ---------------------------------------------------------------------------
# Watcher einmalig über alle Dateien
# ---------------------------------------------------------------------------


def test_watch_einmalig_verarbeitet_alle_dateien(heim: Path):
    ausgabe = _cli("watch", "--einmalig")
    assert "Datei(en) verarbeitet" in ausgabe
    pakete = {p.name for p in zustand.liste_pakete(heim / "logs" / "pakete")}
    erwartet = {f"{Path(n).stem}.paket.json" for n in ALLE_DATEIEN if n != SCAN}
    assert erwartet <= pakete
    assert "scan_preisliste.paket.json" not in pakete  # ohne API-Schlüssel kein Paket, aber kein Abbruch
    assert all(_eingang(heim, n).exists() for n in ALLE_DATEIEN)  # nichts verschoben, nichts gebucht
    assert not (heim / "logs" / "protokoll.csv").exists()


# ---------------------------------------------------------------------------
# Freigabe-Oberfläche: Einlesen, Korrektur, Neu prüfen, Buchen im Dry Run (Menrath)
# ---------------------------------------------------------------------------


def test_app_menrath_korrigieren_und_buchen(heim: Path):
    at = AppTest.from_file(str(Path(app.__file__)), default_timeout=180)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    at.sidebar.selectbox(key="eingangsdatei").set_value(f"wik_leh/{MENRATH}")
    at.sidebar.button(key="einlesen").click().run()
    assert not at.exception, [e.value for e in at.exception]
    paket: Belegpaket = at.session_state[app.SS_PAKET]
    assert paket.blockiert and at.button(key="buchen").disabled
    assert any("VK_FEHLT" in e.value for e in at.error)
    assert any("PREIS_ABWEICHUNG" in w.value for w in at.warning)
    assert at.selectbox(key="pos_3_zuordnung").value == app.OPTION_OFFEN

    # Korrektur: Magnum 958 bestätigen, Liefertag setzen, neu prüfen
    at.selectbox(key="pos_3_zuordnung").set_value("958")
    at.date_input(key="kopf_liefertag").set_value(date(2026, 10, 30))
    at.button(key="korrigieren").click().run()
    assert not at.exception, [e.value for e in at.exception]
    paket = at.session_state[app.SS_PAKET]
    assert not paket.blockiert
    assert _position(paket, 3).artikel is not None and _position(paket, 3).artikel.artikelnummer == "958"
    assert at.text_input(key="bf_shipment").value == "30.10.2026"
    assert not at.button(key="buchen").disabled

    # Buchen im Dry Run: drei Einkauf-Bestellungen, eine AB
    at.button(key="buchen").click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert any("Buchung erfolgreich" in s.value for s in at.success)
    ergebnis = at.session_state[app.SS_ERGEBNIS]
    assert ergebnis.erfolgreich and ergebnis.dry_run
    assert len(ergebnis.alle_einkaeufe()) == 3 and ergebnis.verkauf is not None
    assert (heim / "logs" / "protokoll.csv").exists() and _xml_dateien(heim)
