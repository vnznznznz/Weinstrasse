"""Tests für die Kommandozeile (typer) mit CliRunner; Settings kommen aus der conftest-Fixture."""

from __future__ import annotations

import csv
import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from auftragsautomat import __version__, cli
from auftragsautomat.config import OdbcQuelle, Settings
from auftragsautomat.freigabe import zustand
from auftragsautomat.matching.pipeline import pruefe_erneut
from auftragsautomat.models import Belegpaket, KundeZuordnung, LieferantZuordnung, MatchStatus, RohBestellung
from auftragsautomat.stammdaten import odbc_quelle

FIXTURES = Path(__file__).parent / "fixtures"
PDF = FIXTURES / "pdfs" / "kundenbestellung_kirchhellen.pdf"

runner = CliRunner()


@pytest.fixture(autouse=True)
def _log_handler_aufraeumen():
    """Der CLI-Log-Handler zeigt nach dem CliRunner auf einen abgeklemmten stderr -> entfernen."""
    import logging

    yield
    if cli._log_handler is not None:
        logging.getLogger().removeHandler(cli._log_handler)
        cli._log_handler = None


@pytest.fixture()
def cli_settings(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setattr(cli, "_lade_settings", lambda: settings)
    return settings


def _pdf_im_eingang(settings: Settings) -> Path:
    ziel = settings.eingang_ordner / "wik_leh" / PDF.name
    ziel.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(PDF, ziel)
    return ziel


def _pruefe_und_speichere(settings: Settings) -> Path:
    pdf = _pdf_im_eingang(settings)
    ergebnis = runner.invoke(cli.app, ["pruefen", str(pdf)])
    assert ergebnis.exit_code == 0, ergebnis.output
    pakete = zustand.liste_pakete(zustand.paket_ordner(settings))
    assert len(pakete) == 1
    return pakete[0]


def _mache_freigabefaehig(pfad: Path, settings: Settings, *, freigeben: bool = True) -> Belegpaket:
    """Kirchhellen (Cide + Colosi): Hauptlieferant Cide wie in der UI bestätigen und freigeben."""
    paket = zustand.lade_paket(pfad)
    kandidat = next(k for k in paket.lieferant.kandidaten if k.lieferant.lieferantennummer == "72")
    paket.lieferant = LieferantZuordnung(
        lieferant=kandidat.lieferant, kandidaten=paket.lieferant.kandidaten, status=MatchStatus.MANUELL, quelle="manuell"
    )
    paket = pruefe_erneut(paket, settings)
    assert not paket.blockiert
    paket.freigegeben = freigeben
    zustand.speichere_paket(paket, pfad.parent)
    return paket


def test_version():
    ergebnis = runner.invoke(cli.app, ["version"])
    assert ergebnis.exit_code == 0
    assert ergebnis.output.strip() == f"auftragsautomat {__version__}"


def test_ohne_argumente_zeigt_hilfe():
    ergebnis = runner.invoke(cli.app, [])
    assert "pruefen" in ergebnis.output and "buchen" in ergebnis.output


def test_home_option_setzt_umgebungsvariable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AUFTRAGSAUTOMAT_HOME", "vorher")
    ergebnis = runner.invoke(cli.app, ["--home", str(tmp_path), "version"])
    assert ergebnis.exit_code == 0
    import os

    assert os.environ["AUFTRAGSAUTOMAT_HOME"] == str(tmp_path.resolve())


def test_pruefen_erzeugt_paket_und_zeigt_artikel(cli_settings: Settings):
    pdf = _pdf_im_eingang(cli_settings)
    ergebnis = runner.invoke(cli.app, ["pruefen", str(pdf)])
    assert ergebnis.exit_code == 0, ergebnis.output
    assert "1643" in ergebnis.output
    assert "Hegmann" in ergebnis.output
    assert "freigabefähig" in ergebnis.output
    assert "MEHRERE_LIEFERANTEN" in ergebnis.output  # Cide + Colosi -> zwei Einkauf-Bestellungen
    pakete = zustand.liste_pakete(zustand.paket_ordner(cli_settings))
    assert [p.name for p in pakete] == ["kundenbestellung_kirchhellen.paket.json"]
    paket = zustand.lade_paket(pakete[0])
    assert paket.firma_id == "wik_leh" and paket.kunde.kunde is not None and paket.kunde.kunde.kundennummer == "3441"


def test_pruefen_json_und_paket_ordner(cli_settings: Settings, tmp_path: Path):
    pdf = _pdf_im_eingang(cli_settings)
    ordner = tmp_path / "meine_pakete"
    ergebnis = runner.invoke(cli.app, ["pruefen", str(pdf), "--json", "--paket-ordner", str(ordner), "--firma", "wik_leh"])
    assert ergebnis.exit_code == 0, ergebnis.output
    paket = Belegpaket.model_validate_json(ergebnis.stdout)
    assert paket.firma_id == "wik_leh" and paket.firma_quelle == "manuell"
    assert (ordner / "kundenbestellung_kirchhellen.paket.json").exists()


def test_pruefen_fehler(cli_settings: Settings, tmp_path: Path):
    ergebnis = runner.invoke(cli.app, ["pruefen", str(tmp_path / "fehlt.pdf")])
    assert ergebnis.exit_code == 1
    assert "Fehler" in ergebnis.output and "fehlt.pdf" in ergebnis.output
    ergebnis = runner.invoke(cli.app, ["pruefen", str(PDF), "--firma", "gibt_es_nicht"])
    assert ergebnis.exit_code == 1 and "Unbekannte Firma" in ergebnis.output
    unbekannt = tmp_path / "liste.xlsx"
    unbekannt.write_bytes(b"x")
    ergebnis = runner.invoke(cli.app, ["pruefen", str(unbekannt)])
    assert ergebnis.exit_code == 1 and "nicht unterstützt" in ergebnis.output


def test_extrahieren(cli_settings: Settings):
    ergebnis = runner.invoke(cli.app, ["extrahieren", str(PDF)])
    assert ergebnis.exit_code == 0, ergebnis.output
    assert "Villa Armellina Gloria" in ergebnis.output and "B-4712" in ergebnis.output
    ergebnis = runner.invoke(cli.app, ["extrahieren", str(PDF), "--json"])
    assert ergebnis.exit_code == 0
    roh = RohBestellung.model_validate_json(ergebnis.stdout)
    assert len(roh.positionen) == 4


def test_buchen_dry_run_erfolgreich(cli_settings: Settings):
    pfad = _pruefe_und_speichere(cli_settings)
    _mache_freigabefaehig(pfad, cli_settings)

    ergebnis = runner.invoke(cli.app, ["buchen", str(pfad)])

    assert ergebnis.exit_code == 0, ergebnis.output
    assert "DRY RUN" in ergebnis.output and "ERFOLGREICH" in ergebnis.output
    assert "simuliert (Dry Run)" in ergebnis.output
    protokoll = cli_settings.log_ordner / "protokoll.csv"
    assert protokoll.exists()
    with protokoll.open(encoding="utf-8-sig", newline="") as f:
        zeilen = list(csv.DictReader(f, delimiter=";"))
    assert len(zeilen) == 1
    assert zeilen[0]["dry_run"] == "ja" and zeilen[0]["erfolgreich"] == "ja" and zeilen[0]["kunde_nr"] == "3441"
    assert list(cli_settings.xml_ausgabe_ordner.glob("*.xml"))  # openTRANS-XML im Ausgabeordner
    assert (cli_settings.eingang_ordner / "wik_leh" / PDF.name).exists()  # Dry Run archiviert nicht
    aktualisiert = zustand.lade_paket(pfad)
    assert aktualisiert.freigegeben is True


def test_buchen_mit_lieferschein_und_freigeben_flag(cli_settings: Settings):
    pfad = _pruefe_und_speichere(cli_settings)
    _mache_freigabefaehig(pfad, cli_settings, freigeben=False)

    ergebnis = runner.invoke(cli.app, ["buchen", str(pfad), "--freigeben", "--lieferschein"])

    assert ergebnis.exit_code == 0, ergebnis.output
    paket = zustand.lade_paket(pfad)
    assert paket.freigegeben is True and paket.freigegeben_von and paket.freigegeben_am is not None
    assert paket.lieferschein_erstellen is True
    assert "Lieferschein" in ergebnis.output


def test_buchen_ohne_freigabe_wird_verweigert(cli_settings: Settings):
    pfad = _pruefe_und_speichere(cli_settings)
    _mache_freigabefaehig(pfad, cli_settings, freigeben=False)
    ergebnis = runner.invoke(cli.app, ["buchen", str(pfad)])
    assert ergebnis.exit_code == 2
    assert "nicht freigegeben" in ergebnis.output
    assert not (cli_settings.log_ordner / "protokoll.csv").exists()


def test_buchen_blockiertes_paket_wird_verweigert(cli_settings: Settings):
    pfad = _pruefe_und_speichere(cli_settings)
    paket = zustand.lade_paket(pfad)
    assert not paket.blockiert
    paket.kunde = KundeZuordnung(status=MatchStatus.MANUELL, quelle="manuell")  # Anwender entfernt den Kunden
    paket = pruefe_erneut(paket, cli_settings)
    assert paket.blockiert
    paket.freigegeben = True
    zustand.speichere_paket(paket, pfad.parent)
    ergebnis = runner.invoke(cli.app, ["buchen", str(pfad)])
    assert ergebnis.exit_code == 2
    assert "blockiert" in ergebnis.output and "KEIN_KUNDE" in ergebnis.output


def test_buchen_scharf_fragt_nach(cli_settings: Settings):
    pfad = _pruefe_und_speichere(cli_settings)
    _mache_freigabefaehig(pfad, cli_settings, freigeben=False)

    abgebrochen = runner.invoke(cli.app, ["buchen", str(pfad), "--scharf"], input="n\n")
    assert abgebrochen.exit_code == 1
    assert "Abgebrochen" in abgebrochen.output
    assert zustand.lade_paket(pfad).freigegeben is False
    assert not (cli_settings.log_ordner / "protokoll.csv").exists()

    # Bestätigt -> freigegeben; settings.dry_run ist aktiv, also trotzdem nur Dry Run
    bestaetigt = runner.invoke(cli.app, ["buchen", str(pfad), "--scharf"], input="y\n")
    assert bestaetigt.exit_code == 0, bestaetigt.output
    assert "settings.dry_run ist aktiv" in bestaetigt.output
    assert "DRY RUN" in bestaetigt.output
    assert zustand.lade_paket(pfad).freigegeben is True


def test_buchen_scharf_mit_ja_ohne_rueckfrage(cli_settings: Settings):
    pfad = _pruefe_und_speichere(cli_settings)
    _mache_freigabefaehig(pfad, cli_settings, freigeben=False)
    ergebnis = runner.invoke(cli.app, ["buchen", str(pfad), "--scharf", "--ja", "--freigeben"])
    assert ergebnis.exit_code == 0, ergebnis.output
    assert "Wirklich in Lexware schreiben?" not in ergebnis.output
    assert "DRY RUN" in ergebnis.output  # Nicht-Windows / settings.dry_run -> simuliert


def test_lieferschein_nachziehen_cli(cli_settings: Settings):
    pfad = _pruefe_und_speichere(cli_settings)
    nicht_frei = runner.invoke(cli.app, ["lieferschein", str(pfad), "--ab", "AB-2026-0101"])
    assert nicht_frei.exit_code == 2 and "nicht freigegeben" in nicht_frei.output
    _mache_freigabefaehig(pfad, cli_settings)
    ergebnis = runner.invoke(cli.app, ["lieferschein", str(pfad), "--ab", "AB-2026-0101"])
    assert ergebnis.exit_code == 0, ergebnis.output
    assert "Lieferschein simuliert (Dry Run)" in ergebnis.output
    assert "Nur Lieferschein zu AB AB-2026-0101" in ergebnis.output
    assert "Einkauf-Bestellung:  nicht angelegt" in ergebnis.output
    assert zustand.lade_paket(pfad).lieferschein_erstellen is True
    with (cli_settings.log_ordner / "protokoll.csv").open(encoding="utf-8-sig", newline="") as f:
        zeile = list(csv.DictReader(f, delimiter=";"))[-1]
    assert zeile["beleg_lieferschein"] == "(simuliert)" and zeile["beleg_verkauf"] == ""


def test_buchen_fehlende_paketdatei(cli_settings: Settings, tmp_path: Path):
    ergebnis = runner.invoke(cli.app, ["buchen", str(tmp_path / "nix.paket.json")])
    assert ergebnis.exit_code == 1 and "nicht gefunden" in ergebnis.output
    kaputt = tmp_path / "kaputt.paket.json"
    kaputt.write_text("{", encoding="utf-8")
    ergebnis = runner.invoke(cli.app, ["buchen", str(kaputt)])
    assert ergebnis.exit_code == 1 and "JSON" in ergebnis.output


def test_freigabe_startet_streamlit(monkeypatch: pytest.MonkeyPatch):
    aufrufe: list[tuple[list[str], dict[str, str]]] = []

    def fake_prozess(kommando: list[str], umgebung: dict[str, str]) -> int:
        aufrufe.append((kommando, umgebung))
        return 0

    monkeypatch.setattr(cli, "_starte_prozess", fake_prozess)
    ergebnis = runner.invoke(cli.app, ["freigabe", "--port", "8600", "--kein-browser"])
    assert ergebnis.exit_code == 0, ergebnis.output
    assert len(aufrufe) == 1
    kommando, umgebung = aufrufe[0]
    assert kommando[1:4] == ["-m", "streamlit", "run"]
    assert kommando[4].endswith(str(Path("freigabe") / "app.py")) and Path(kommando[4]).is_file()
    assert "--server.port" in kommando and "8600" in kommando
    assert "--server.headless" in kommando
    assert "AUFTRAGSAUTOMAT_HOME" in umgebung
    assert str(Path(cli.__file__).resolve().parents[1]) in umgebung["PYTHONPATH"]

    monkeypatch.setattr(cli, "_starte_prozess", lambda k, u: 3)
    ergebnis = runner.invoke(cli.app, ["freigabe"])
    assert ergebnis.exit_code == 3


def test_watch_einmalig(cli_settings: Settings):
    _pdf_im_eingang(cli_settings)
    ergebnis = runner.invoke(cli.app, ["watch", "--einmalig"])
    assert ergebnis.exit_code == 0, ergebnis.output
    assert "1 Datei(en) verarbeitet" in ergebnis.output
    assert "nicht automatisch gebucht" in ergebnis.output
    pakete = zustand.liste_pakete(zustand.paket_ordner(cli_settings))
    assert len(pakete) == 1
    assert (cli_settings.log_ordner / "verarbeitet.txt").exists()
    assert not (cli_settings.log_ordner / "protokoll.csv").exists()


def test_stammdaten_pruefen(cli_settings: Settings):
    ergebnis = runner.invoke(cli.app, ["stammdaten-pruefen", "--firma", "wik_leh"])
    assert ergebnis.exit_code == 0, ergebnis.output
    assert "wik_leh" in ergebnis.output and "Artikel" in ergebnis.output

    alle = runner.invoke(cli.app, ["stammdaten-pruefen"])
    assert alle.exit_code == 0, alle.output
    for firma_id in cli_settings.firmen:
        assert firma_id in alle.output

    unbekannt = runner.invoke(cli.app, ["stammdaten-pruefen", "--firma", "nix"])
    assert unbekannt.exit_code == 1 and "Unbekannte Firma" in unbekannt.output


def test_stammdaten_pruefen_meldet_ladefehler(cli_settings: Settings):
    cli_settings.firmen["wik_leh"].stammdaten.csv.artikel = cli_settings.eingang_ordner / "fehlt.csv"
    ergebnis = runner.invoke(cli.app, ["stammdaten-pruefen"])
    assert ergebnis.exit_code == 1
    assert "FEHLER" in ergebnis.output and "wik_leh" in ergebnis.output


def test_odbc_erkunden(cli_settings: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    ohne = runner.invoke(cli.app, ["odbc-erkunden", "--firma", "wik_leh"])
    assert ohne.exit_code == 1 and "keine ODBC-Konfiguration" in ohne.output

    unbekannt = runner.invoke(cli.app, ["odbc-erkunden", "--firma", "nix"])
    assert unbekannt.exit_code == 1 and "Unbekannte Firma" in unbekannt.output

    cli_settings.firmen["wik_leh"].stammdaten.odbc = OdbcQuelle(dsn="LexwareTest")
    aufrufe: list[tuple[object, Path, str | None]] = []

    def fake_bericht(konfig, ziel, *, eintraege=None, praefix=None):  # noqa: ANN001
        aufrufe.append((konfig, Path(ziel), praefix))
        Path(ziel).parent.mkdir(parents=True, exist_ok=True)
        Path(ziel).write_text("# Schema", encoding="utf-8")
        return Path(ziel)

    monkeypatch.setattr(odbc_quelle, "schreibe_schema_bericht", fake_bericht)
    ergebnis = runner.invoke(cli.app, ["odbc-erkunden", "--firma", "wik_leh", "--praefix", "FK_"])
    assert ergebnis.exit_code == 0, ergebnis.output
    assert aufrufe[0][0].dsn == "LexwareTest" and aufrufe[0][2] == "FK_"
    assert aufrufe[0][1] == cli_settings.log_ordner / "odbc_schema_wik_leh.md"
    assert "Schema-Bericht geschrieben" in ergebnis.output

    ziel = tmp_path / "schema.md"
    ergebnis = runner.invoke(cli.app, ["odbc-erkunden", "--firma", "wik_leh", "--ziel", str(ziel)])
    assert ergebnis.exit_code == 0 and aufrufe[1][1] == ziel

    def kaputt(konfig, ziel, *, eintraege=None, praefix=None):  # noqa: ANN001
        raise RuntimeError("pyodbc fehlt")

    monkeypatch.setattr(odbc_quelle, "schreibe_schema_bericht", kaputt)
    ergebnis = runner.invoke(cli.app, ["odbc-erkunden", "--firma", "wik_leh"])
    assert ergebnis.exit_code == 1 and "pyodbc fehlt" in ergebnis.output


def test_beispiel_pdfs(tmp_path: Path):
    ordner = tmp_path / "pdfs"
    ergebnis = runner.invoke(cli.app, ["beispiel-pdfs", str(ordner)])
    assert ergebnis.exit_code == 0, ergebnis.output
    erzeugt = sorted(p.name for p in ordner.glob("*.pdf"))
    assert "kundenbestellung_kirchhellen.pdf" in erzeugt and len(erzeugt) == 8  # 4 Stufe 1 + 4 echte Belegformate
    assert "8 Beispiel-PDF(s) erzeugt" in ergebnis.output


def test_beispielskript_fehlt(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(cli, "projekt_stamm", lambda: tmp_path)
    monkeypatch.setattr(cli, "DEFAULT_PROJEKT_STAMM", tmp_path)
    ergebnis = runner.invoke(cli.app, ["beispiel-pdfs", str(tmp_path / "out")])
    assert ergebnis.exit_code == 1 and "nicht gefunden" in ergebnis.output


def test_logging_konfiguration_mehrfach(cli_settings: Settings):
    for stufe in ("DEBUG", "unsinn"):
        ergebnis = runner.invoke(cli.app, ["--log-level", stufe, "version"])
        assert ergebnis.exit_code == 0
    import logging

    handler = [h for h in logging.getLogger().handlers if h is cli._log_handler]
    assert len(handler) == 1
    assert json.dumps({"ok": True})  # json-Import wird für die Paketprüfung oben benutzt
