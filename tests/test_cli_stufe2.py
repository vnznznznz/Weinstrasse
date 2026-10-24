"""Tests für die CLI-Befehle der Ausbaustufe 2 (typer, CliRunner) – nur mit Fake-Backends, nichts verlässt tmp_path.

Synthetische Daten: Paket "Beispielmarkt Nord GmbH" / "Weingut Beispiel Srl" (Firma wik_leh), Kontaktliste und
Fixterminüberwachung aus tests/fixtures/excel, Rabattliste tests/fixtures/pdfs/rabattliste_beispiel.pdf.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from auftragsautomat import cli
from auftragsautomat.config import DateiRefKonfig, Settings
from auftragsautomat.freigabe import zustand
from auftragsautomat.lexware import nachbereitung as nbm
from auftragsautomat.models import (
    Aktion,
    Artikel,
    Belegfelder,
    Belegpaket,
    Eingangstyp,
    Extraktionsmethode,
    Kunde,
    KundeZuordnung,
    Lieferant,
    LieferantZuordnung,
    MatchStatus,
    PositionsZuordnung,
    RohBestellung,
    RohPosition,
)
from auftragsautomat.rabatte import speichere_aktionen

REPO = Path(__file__).resolve().parents[1]
REPO_CONFIG = REPO / "config"
FIXTURES = Path(__file__).parent / "fixtures"
FIXTERMIN_XLSX = FIXTURES / "excel" / "fixterminueberwachung_beispiel.xlsx"
KONTAKTLISTE_XLSX = FIXTURES / "excel" / "ansprechpartner_beispiel.xlsx"
RABATTLISTE_PDF = FIXTURES / "pdfs" / "rabattliste_beispiel.pdf"
FIRMA = "wik_leh"

runner = CliRunner()


@pytest.fixture(autouse=True)
def _log_handler_aufraeumen():
    yield
    if cli._log_handler is not None:
        logging.getLogger().removeHandler(cli._log_handler)
        cli._log_handler = None


@pytest.fixture()
def cli_settings(settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    settings.lexware.pdf_ausgabe_ordner = tmp_path / "logs" / "pdf"
    settings.kommunikation.vorlagen_ordner = REPO_CONFIG / "vorlagen"
    settings.kommunikation.signaturen_datei = REPO_CONFIG / "signaturen.yaml"
    settings.kommunikation.anrede_datei = REPO_CONFIG / "anrede.yaml"
    settings.kommunikation.entwuerfe_ordner = tmp_path / "logs" / "entwuerfe"
    settings.kommunikation.absender_name = "Max Beispiel"
    settings.kommunikation.absender_email = "logistik@example.org"
    settings.dossier.lokaler_ordner = tmp_path / "logs" / "dossiers"
    settings.ablage.lokaler_ordner = tmp_path / "logs" / "ablage"
    settings.tracking.hillebrand_konfig = REPO_CONFIG / "tracking_hillebrand.yaml"
    settings.tracking.quellen = ["mail"]
    settings.tracking.screenshots_ordner = tmp_path / "logs" / "tracking"
    settings.ueberwachung.berichte_ordner = tmp_path / "logs" / "ueberwachung"
    settings.ueberwachung.merkdatei = tmp_path / "logs" / "nachfragen.json"
    settings.rabatte.gedaechtnis_datei = tmp_path / "config" / "aktionen.yaml"
    settings.rabatte.rabatt_pdf_ordner = tmp_path / "eingang" / "rabatte"
    settings.firmenwahl.gedaechtnis_datei = tmp_path / "config" / "firmen_gedaechtnis.yaml"
    settings.m365.fixtermin_datei = DateiRefKonfig(lokaler_pfad=FIXTERMIN_XLSX, blatt="Fixterminüberwachung")
    settings.m365.kontaktliste_datei = DateiRefKonfig(lokaler_pfad=KONTAKTLISTE_XLSX)
    monkeypatch.setattr(cli, "_lade_settings", lambda: settings)
    return settings


def _paket(settings: Settings, *, liefertag: date | None = date(2026, 5, 15)) -> Belegpaket:
    quelle = settings.eingang_ordner / FIRMA / "bestellung_beispielmarkt.pdf"
    quelle.parent.mkdir(parents=True, exist_ok=True)
    quelle.write_bytes(b"%PDF-1.4\n%synthetisch\n")
    roh = RohBestellung(
        eingangstyp=Eingangstyp.KUNDENBESTELLUNG_PDF,
        quelldatei=quelle,
        extraktionsmethode=Extraktionsmethode.PDFPLUMBER,
        kunde_text="Beispielmarkt Nord",
        lieferant_text="Weingut Beispiel",
        bestellnummer="4711",
        datum=date(2026, 5, 4),
        liefertag=liefertag,
        positionen=[RohPosition(laufnr=1, bezeichnung="Beispielwein Rosso 0,75 l", menge=Decimal(120), preis=Decimal("6.90"))],
    )
    artikel = Artikel(firma_id=FIRMA, artikelnummer="9001", bezeichnung="Beispielwein Rosso 0,75 l", lieferantennummer="150", ek_preis=Decimal("4.20"), vk_preis_1=Decimal("6.90"))
    return Belegpaket(
        roh=roh,
        firma_id=FIRMA,
        kunde=KundeZuordnung(kunde=Kunde(firma_id=FIRMA, kundennummer="2500", name="Beispielmarkt Nord GmbH", matchcode="Beispielmarkt"), status=MatchStatus.SICHER),
        lieferant=LieferantZuordnung(lieferant=Lieferant(firma_id=FIRMA, lieferantennummer="150", name="Weingut Beispiel Srl", matchcode="Beispiel"), status=MatchStatus.SICHER),
        positionen=[PositionsZuordnung(roh=roh.positionen[0], artikel=artikel, status=MatchStatus.SICHER, menge=Decimal(120), ek_preis=Decimal("4.20"), vk_preis=Decimal("6.90"), preis_dokument=Decimal("6.90"))],
        belegfelder=Belegfelder(spedition="Hillebrand", weingut="Weingut Beispiel Srl", shipment="15.05.2026", kunde="Beispielmarkt Nord GmbH"),
        freigegeben=True,
    )


def _paket_json(settings: Settings, **kw) -> Path:
    return zustand.speichere_paket(_paket(settings, **kw), zustand.paket_ordner(settings))


def _aktion(aktion_id: str = "pdf:test:1:1", von: date = date(2026, 11, 1), bis: date = date(2026, 11, 30)) -> Aktion:
    return Aktion(aktion_id=aktion_id, bezeichnung="Herbstaktion Beispiel", lieferant_kurz="Beispiel", von=von, bis=bis, rabatt_prozent=Decimal("10"), quelle="pdf:test:1")


# ---------------------------------------------------------------------------
# Hilfe, Microsoft 365
# ---------------------------------------------------------------------------


def test_hilfe_listet_neue_befehle():
    ergebnis = runner.invoke(cli.app, ["--help"])
    assert ergebnis.exit_code == 0
    for befehl in ("m365-anmelden", "m365-pruefen", "fixtermin-eintragen", "entwuerfe", "dossier", "ablegen", "benennen", "firma-merken", "ueberwachen", "heute", "tracking", "tracking-einrichten", "kontakte-pruefen", "rabatte-einlesen", "aktionen"):
        assert befehl in ergebnis.output


def test_m365_anmelden_fake_backend_braucht_keine_anmeldung(cli_settings: Settings):
    ergebnis = runner.invoke(cli.app, ["m365-anmelden"])
    assert ergebnis.exit_code == 0, ergebnis.output
    assert "keine Microsoft-365-Anmeldung nötig" in ergebnis.output
    ergebnis = runner.invoke(cli.app, ["m365-anmelden", "--nur-status", "--device-code"])
    assert ergebnis.exit_code == 0 and "fake" in ergebnis.output


def test_m365_pruefen_liest_blaetter_und_kontakte(cli_settings: Settings):
    ergebnis = runner.invoke(cli.app, ["m365-pruefen"])
    assert ergebnis.exit_code == 0, ergebnis.output
    assert "Fixterminüberwachung: 2 Blätter" in ergebnis.output and "Ressourcen für Auto DropDown" in ergebnis.output
    assert "Kontaktliste:" in ergebnis.output and "Kontakte (4 Lieferanten, 2 Speditionen, 2 Kunden)" in ergebnis.output
    assert "Dossier-Ordner: nicht konfiguriert" in ergebnis.output


def test_m365_pruefen_unkonfiguriert_und_fehler(cli_settings: Settings, tmp_path: Path):
    cli_settings.m365.fixtermin_datei = DateiRefKonfig(blatt="Fixterminüberwachung")
    cli_settings.m365.kontaktliste_datei = DateiRefKonfig(lokaler_pfad=tmp_path / "fehlt.xlsx")
    ergebnis = runner.invoke(cli.app, ["m365-pruefen"])
    assert ergebnis.exit_code == 1
    assert "Fixterminüberwachung: nicht konfiguriert" in ergebnis.output and "Kontaktliste nicht lesbar" in ergebnis.output


# ---------------------------------------------------------------------------
# Fixtermin, Entwürfe, Dossier, Ablage, Benennung, Firma
# ---------------------------------------------------------------------------


def test_fixtermin_eintragen_dry_run_vorschau(cli_settings: Settings):
    pfad = _paket_json(cli_settings)
    ergebnis = runner.invoke(cli.app, ["fixtermin-eintragen", str(pfad)])
    assert ergebnis.exit_code == 0, ergebnis.output
    assert "Fixtermin (Dry Run): 15.05.2026 | Beispiel | Beispielmarkt" in ergebnis.output
    assert "Zeile 21 (Vorschau, nicht geschrieben)" in ergebnis.output and "Vorschau-CSV" in ergebnis.output
    assert (cli_settings.log_ordner / nbm.VORSCHAU_CSV).is_file()


def test_fixtermin_eintragen_ohne_liefertag(cli_settings: Settings):
    pfad = _paket_json(cli_settings, liefertag=None)
    ergebnis = runner.invoke(cli.app, ["fixtermin-eintragen", str(pfad)])
    assert ergebnis.exit_code == 1
    assert "Kein Liefertag" in ergebnis.output and "Kein Fixtermin eingetragen" in ergebnis.output


def test_fixtermin_eintragen_scharf_mit_settings_dry_run_wird_dry_run(cli_settings: Settings):
    pfad = _paket_json(cli_settings)
    ergebnis = runner.invoke(cli.app, ["fixtermin-eintragen", str(pfad), "--scharf"])
    assert ergebnis.exit_code == 0, ergebnis.output
    assert "settings.dry_run ist aktiv" in ergebnis.output and "(Dry Run)" in ergebnis.output


def test_entwuerfe_als_eml(cli_settings: Settings):
    pfad = _paket_json(cli_settings)
    ergebnis = runner.invoke(cli.app, ["entwuerfe", str(pfad)])
    assert ergebnis.exit_code == 0, ergebnis.output
    assert "3 Entwürfe (Dry Run, nie gesendet)" in ergebnis.output
    assert "orders@weingut-beispiel.example.org" in ergebnis.output and "neue-auftraege@hillebrand.example.org" in ergebnis.output
    assert "Auftragskopie zur Information" in ergebnis.output and "Auftragsbestätigung" not in ergebnis.output.split("Hinweise:")[0]
    emls = sorted(cli_settings.kommunikation.entwuerfe_ordner.glob("*.eml"))
    assert len(emls) == 3


def test_dossier_cli(cli_settings: Settings):
    pfad = _paket_json(cli_settings)
    ergebnis = runner.invoke(cli.app, ["dossier", str(pfad)])
    assert ergebnis.exit_code == 0, ergebnis.output
    assert "Dossier (Dry Run): Beispiel-WIK-Beispielmarkt-4711-" in ergebnis.output
    assert "00_Deckblatt.pdf (deckblatt)" in ergebnis.output and "Offene Punkte" in ergebnis.output
    ordner = [p for p in cli_settings.dossier.lokaler_ordner.iterdir() if p.is_dir()]
    assert len(ordner) == 1 and (ordner[0] / "dossier.json").is_file()
    # zweiter Lauf: fortschreiben, keine Duplikate
    ergebnis = runner.invoke(cli.app, ["dossier", str(pfad)])
    assert ergebnis.exit_code == 0
    assert len([p for p in cli_settings.dossier.lokaler_ordner.iterdir() if p.is_dir()]) == 1


def test_ablegen_dry_run_ohne_buchung(cli_settings: Settings):
    pfad = _paket_json(cli_settings)
    ergebnis = runner.invoke(cli.app, ["ablegen", str(pfad)])
    assert ergebnis.exit_code == 0, ergebnis.output
    assert "Ablage (Dry Run)" in ergebnis.output and "nur protokolliert" in ergebnis.output
    assert "Kd" in ergebnis.output and "Aufträge ohne Provision" in ergebnis.output
    assert "Kein Buchungsergebnis gespeichert" in ergebnis.output
    assert not cli_settings.ablage.lokaler_ordner.exists() or not list(cli_settings.ablage.lokaler_ordner.rglob("*.pdf"))


def test_buchen_dann_ablegen_nutzt_gespeichertes_ergebnis(cli_settings: Settings):
    pfad = _paket_json(cli_settings)
    ergebnis = runner.invoke(cli.app, ["buchen", str(pfad)])
    assert ergebnis.exit_code == 0, ergebnis.output
    assert "Nachbereitung (Dry Run)" in ergebnis.output and "Fixtermin: 15.05.2026" in ergebnis.output
    assert nbm.lade_nachbereitung(pfad, cli_settings) is not None
    ergebnis = runner.invoke(cli.app, ["ablegen", str(pfad)])
    assert ergebnis.exit_code == 0, ergebnis.output
    assert "Kein Buchungsergebnis gespeichert" not in ergebnis.output
    assert "Cde" in ergebnis.output and "Kopie" in ergebnis.output


def test_ablegen_mischt_keine_hausmuster_pdfs_ins_ergebnis(cli_settings: Settings):
    """Regression (Review Befund 3): 'ablegen' legt nach Hausmuster gefundene Dateien nicht unter dem Namen
    des Auftrags ab, sondern nennt sie nur als Vorschlag; das gespeicherte Ergebnis bleibt unverändert."""
    from auftragsautomat.benennung import name_fuer_paket

    pfad = _paket_json(cli_settings)
    assert runner.invoke(cli.app, ["buchen", str(pfad)]).exit_code == 0
    vorher = nbm.lade_nachbereitung(pfad, cli_settings).ergebnis.pdfs
    ordner = Path(cli_settings.lexware.pdf_ausgabe_ordner)
    ordner.mkdir(parents=True, exist_ok=True)
    fremd = ordner / name_fuer_paket("Kopie", zustand.lade_paket(pfad), cli_settings, datum=date(2026, 5, 4))
    fremd.write_bytes(b"%PDF-1.4 andere Datei")
    ergebnis = runner.invoke(cli.app, ["ablegen", str(pfad)])
    assert ergebnis.exit_code == 0, ergebnis.output
    assert "Vorschlag (nicht abgelegt): PDF Kopie" in ergebnis.output
    assert nbm.lade_nachbereitung(pfad, cli_settings).ergebnis.pdfs == vorher
    assert all(Path(p) != fremd for p in vorher.values())


def test_benennen(cli_settings: Settings):
    pfad = _paket_json(cli_settings)
    ergebnis = runner.invoke(cli.app, ["benennen", str(pfad), "--kuerzel", "Cde", "--datum", "06.05.2026"])
    assert ergebnis.exit_code == 0, ergebnis.output
    assert ergebnis.output.strip() == "Beispiel – WIK – 4711 – Cde – 06.05.2026.pdf"
    ergebnis = runner.invoke(cli.app, ["benennen", str(pfad), "--kuerzel", "Speditionsauftrag", "--datum", "06.05.2026"])
    assert ergebnis.output.strip() == "Hillebrand – Speditionsauftrag – Beispiel – 4711 – 06.05.2026.pdf"
    ergebnis = runner.invoke(cli.app, ["benennen", str(pfad), "--kuerzel", "Xyz"])
    assert ergebnis.exit_code == 1 and "bekannte Kürzel" in ergebnis.output
    ergebnis = runner.invoke(cli.app, ["benennen", str(pfad), "--kuerzel", "Cde", "--datum", "gestern"])
    assert ergebnis.exit_code == 1 and "kein Datum" in ergebnis.output


def test_firma_merken(cli_settings: Settings):
    pfad = _paket_json(cli_settings)
    ergebnis = runner.invoke(cli.app, ["firma-merken", str(pfad), "--firma", "woe_leh"])
    assert ergebnis.exit_code == 0, ergebnis.output
    assert "Firma woe_leh gesetzt" in ergebnis.output and "Gemerkt: Kunde 'Beispielmarkt Nord'" in ergebnis.output
    daten = yaml.safe_load(cli_settings.firmenwahl.gedaechtnis_datei.read_text(encoding="utf-8"))
    regeln = daten["regeln"] if isinstance(daten, dict) else daten
    assert any(r["firma_id"] == "woe_leh" and r["kunde_kurz"] == "Beispielmarkt Nord" for r in regeln)
    assert zustand.lade_paket(pfad).firma_id == "woe_leh"
    ergebnis = runner.invoke(cli.app, ["firma-merken", str(pfad), "--firma", "gibtsnicht"])
    assert ergebnis.exit_code == 1 and "Unbekannte Firma" in ergebnis.output


def test_paketdatei_fehlt(cli_settings: Settings, tmp_path: Path):
    for befehl in ("fixtermin-eintragen", "entwuerfe", "dossier", "ablegen"):
        ergebnis = runner.invoke(cli.app, [befehl, str(tmp_path / "fehlt.paket.json")])
        assert ergebnis.exit_code == 1 and "nicht gefunden" in ergebnis.output


# ---------------------------------------------------------------------------
# Überwachung, Tracking, Kontakte
# ---------------------------------------------------------------------------


def test_ueberwachen_nur_lesen_und_heute(cli_settings: Settings):
    ergebnis = runner.invoke(cli.app, ["heute"])
    assert ergebnis.exit_code == 0 and "Kein Überwachungsbericht" in ergebnis.output

    ergebnis = runner.invoke(cli.app, ["ueberwachen", "--nur-lesen", "--tage", "30"])
    assert ergebnis.exit_code == 0, ergebnis.output
    assert "Überwachung (Dry Run, nur lesen)" in ergebnis.output and "Bericht:" in ergebnis.output
    berichte = sorted(cli_settings.ueberwachung.berichte_ordner.glob("ueberwachung_*.md"))
    assert len(berichte) == 1 and berichte[0].with_suffix(".json").is_file()
    assert cli_settings.ueberwachung.tage_voraus == 30
    assert not cli_settings.ueberwachung.merkdatei.exists()  # Dry Run: Merkdatei wird nicht geschrieben

    ergebnis = runner.invoke(cli.app, ["heute"])
    assert ergebnis.exit_code == 0, ergebnis.output
    assert "Überwachung vom" in ergebnis.output

    ergebnis = runner.invoke(cli.app, ["ueberwachen", "--tage", "-1"])
    assert ergebnis.exit_code == 1


def test_ueberwachen_fehler_wird_gemeldet(cli_settings: Settings):
    cli_settings.firmen.clear()  # ohne Firma kann der Lauf nicht verdrahtet werden
    ergebnis = runner.invoke(cli.app, ["ueberwachen", "--nur-lesen"])
    assert ergebnis.exit_code == 1 and "Überwachungslauf fehlgeschlagen" in ergebnis.output


def test_tracking_ueber_mailquelle_unbekannt(cli_settings: Settings):
    ergebnis = runner.invoke(cli.app, ["tracking", "DEMB00000"])
    assert ergebnis.exit_code == 0, ergebnis.output
    assert "Sendung DEMB00000" in ergebnis.output and "Status: unbekannt" in ergebnis.output and "Kette: mail" in ergebnis.output
    ergebnis = runner.invoke(cli.app, ["tracking", "4711", "--kundenreferenz", "--spedition", "Beispiel-Logistik"])
    assert ergebnis.exit_code == 0 and "unsere Ref. 4711" in ergebnis.output and "Beispiel-Logistik" in ergebnis.output
    ergebnis = runner.invoke(cli.app, ["tracking", "   "])
    assert ergebnis.exit_code == 1


def test_tracking_einrichten_mit_fake_portal(cli_settings: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    from auftragsautomat.tracking import hillebrand_portal

    monkeypatch.setattr(hillebrand_portal.PortalQuelle, "verfuegbar", lambda self: True)
    monkeypatch.setattr(hillebrand_portal.PortalQuelle, "profil", lambda self: tmp_path / "chrome-profil")
    monkeypatch.setattr(hillebrand_portal.PortalQuelle, "einrichten", lambda self, *, warte=None: True)
    ergebnis = runner.invoke(cli.app, ["tracking-einrichten"])
    assert ergebnis.exit_code == 0, ergebnis.output
    assert "Chrome-Profil" in ergebnis.output and "Portal eingerichtet" in ergebnis.output

    monkeypatch.setattr(hillebrand_portal.PortalQuelle, "einrichten", lambda self, *, warte=None: False)
    ergebnis = runner.invoke(cli.app, ["tracking-einrichten"])
    assert ergebnis.exit_code == 1 and "weiterhin die Anmeldung" in ergebnis.output

    monkeypatch.setattr(hillebrand_portal.PortalQuelle, "verfuegbar", lambda self: False)
    ergebnis = runner.invoke(cli.app, ["tracking-einrichten"])
    assert ergebnis.exit_code == 1 and "Playwright" in ergebnis.output


def test_kontakte_pruefen(cli_settings: Settings):
    ergebnis = runner.invoke(cli.app, ["kontakte-pruefen"])
    assert ergebnis.exit_code == 0, ergebnis.output
    assert "Lieferanten (4)" in ergebnis.output and "Orders: orders@weingut-beispiel.example.org" in ergebnis.output
    assert "Hillebrand" in ergebnis.output and "Neue Aufträge: neue-auftraege@hillebrand.example.org" in ergebnis.output
    assert "Sendungsnachfragen: info@hillebrand.example.org" in ergebnis.output
    assert "Beispielmarkt Nord" in ergebnis.output and "Spedition: Beispiel-Logistik" in ergebnis.output

    cli_settings.m365.kontaktliste_datei = DateiRefKonfig()
    ergebnis = runner.invoke(cli.app, ["kontakte-pruefen"])
    assert ergebnis.exit_code == 1 and "nicht konfiguriert" in ergebnis.output


# ---------------------------------------------------------------------------
# Rabatte / Aktionen
# ---------------------------------------------------------------------------


def test_aktionen_aus_gedaechtnis_mit_datum_und_firma(cli_settings: Settings):
    ergebnis = runner.invoke(cli.app, ["aktionen"])
    assert ergebnis.exit_code == 0 and "Keine Aktionsquellen" in ergebnis.output

    speichere_aktionen([_aktion(), _aktion("pdf:test:1:2", date(2027, 1, 1), date(2027, 1, 31))], cli_settings.rabatte.gedaechtnis_datei)
    ergebnis = runner.invoke(cli.app, ["aktionen", "--datum", "15.11.2026"])
    assert ergebnis.exit_code == 0, ergebnis.output
    assert "pdf:test:1:1" in ergebnis.output and "pdf:test:1:2" not in ergebnis.output and "1 von 2" in ergebnis.output
    ergebnis = runner.invoke(cli.app, ["aktionen", "--alle", "--firma", "wik_leh"])
    assert ergebnis.exit_code == 0 and "pdf:test:1:2" in ergebnis.output and "2 von 2" in ergebnis.output
    ergebnis = runner.invoke(cli.app, ["aktionen", "--datum", "heute?"])
    assert ergebnis.exit_code == 1
    ergebnis = runner.invoke(cli.app, ["aktionen", "--firma", "gibtsnicht"])
    assert ergebnis.exit_code == 1


def test_rabatte_einlesen_und_uebernehmen(cli_settings: Settings):
    assert RABATTLISTE_PDF.is_file(), "Beispiel-PDFs fehlen: python scripts/beispiel_pdfs_erzeugen.py"
    ergebnis = runner.invoke(cli.app, ["rabatte-einlesen", str(RABATTLISTE_PDF)])
    assert ergebnis.exit_code == 0, ergebnis.output
    assert "Rabattliste rabattliste_beispiel.pdf (pdfplumber" in ergebnis.output and "Nur angezeigt" in ergebnis.output
    assert not cli_settings.rabatte.gedaechtnis_datei.exists()

    ergebnis = runner.invoke(cli.app, ["rabatte-einlesen", str(RABATTLISTE_PDF), "--uebernehmen"])
    assert ergebnis.exit_code == 0, ergebnis.output
    assert "übernommen nach" in ergebnis.output
    daten = yaml.safe_load(cli_settings.rabatte.gedaechtnis_datei.read_text(encoding="utf-8"))
    assert daten["aktionen"] and all(a["quelle"].startswith("pdf:") for a in daten["aktionen"])

    ergebnis = runner.invoke(cli.app, ["aktionen", "--alle"])
    assert ergebnis.exit_code == 0 and "Aktionen (alle): 4 von 4" in ergebnis.output


def test_rabatte_einlesen_datei_fehlt(cli_settings: Settings, tmp_path: Path):
    ergebnis = runner.invoke(cli.app, ["rabatte-einlesen", str(tmp_path / "fehlt.pdf")])
    assert ergebnis.exit_code == 1 and "nicht gefunden" in ergebnis.output


def test_effektiver_dry_run_hilfe(cli_settings: Settings):
    assert cli._effektiver_dry_run(cli_settings, False) is True
    assert cli._effektiver_dry_run(cli_settings, True) is True  # settings.dry_run aktiv
    cli_settings.dry_run = False
    assert cli._effektiver_dry_run(cli_settings, True) is False
    assert cli._effektiver_dry_run(cli_settings, False) is True
    assert datetime.now().year >= 2026  # Sicherheitsnetz für Datumsannahmen der Fixtures
