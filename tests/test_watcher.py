"""Tests für watcher.starte_watch, die Merkliste und die Fertig-geschrieben-Prüfung."""

from __future__ import annotations

import queue
import shutil
import threading
import time
from pathlib import Path

import pytest
from watchdog.events import DirCreatedEvent, FileCreatedEvent, FileMovedEvent

from auftragsautomat import watcher
from auftragsautomat.config import Settings
from auftragsautomat.freigabe import zustand

FIXTURES = Path(__file__).parent / "fixtures"
PDF = FIXTURES / "pdfs" / "kundenbestellung_kirchhellen.pdf"


def _kopiere_pdf(ziel_ordner: Path, name: str = "kundenbestellung_kirchhellen.pdf") -> Path:
    ziel_ordner.mkdir(parents=True, exist_ok=True)
    ziel = ziel_ordner / name
    shutil.copy(PDF, ziel)
    return ziel


def test_einmalig_verarbeitet_vorhandene_dateien_und_merkt_sie(settings: Settings):
    pdf = _kopiere_pdf(settings.eingang_ordner / "wik_leh")
    (settings.eingang_ordner / "notizen.docx").write_bytes(b"x")  # nicht unterstützt
    (settings.eingang_ordner / "~$temp.pdf").write_bytes(b"x")  # temporäre Datei
    verarbeitet: list[Path] = []

    anzahl = watcher.starte_watch(settings, verarbeitet.append, einmalig=True, stabil_sekunden=0.05)

    assert anzahl == 1
    assert verarbeitet == [pdf]
    merkdatei = settings.log_ordner / watcher.VERARBEITET_DATEINAME
    assert merkdatei.exists()
    inhalt = merkdatei.read_text(encoding="utf-8")
    assert str(pdf.resolve()) in inhalt and "\tok\t" in inhalt

    # Zweiter Lauf: nichts Neues
    anzahl2 = watcher.starte_watch(settings, verarbeitet.append, einmalig=True, stabil_sekunden=0.05)
    assert anzahl2 == 0 and len(verarbeitet) == 1

    # Neue Merkliste aus der Datei geladen -> Datei bleibt bekannt
    liste = watcher.Verarbeitungsliste(merkdatei)
    assert len(liste) == 1 and liste.enthaelt(pdf)


def test_geaenderte_datei_gleichen_namens_wird_erneut_verarbeitet(settings: Settings):
    pdf = _kopiere_pdf(settings.eingang_ordner)
    verarbeitet: list[Path] = []
    watcher.starte_watch(settings, verarbeitet.append, einmalig=True, stabil_sekunden=0.05)
    assert len(verarbeitet) == 1

    pdf.write_bytes(pdf.read_bytes() + b"\n%geaendert")
    watcher.starte_watch(settings, verarbeitet.append, einmalig=True, stabil_sekunden=0.05)
    assert len(verarbeitet) == 2


def test_fehler_in_verarbeitung_beendet_watcher_nicht(settings: Settings, caplog: pytest.LogCaptureFixture):
    _kopiere_pdf(settings.eingang_ordner, "a.pdf")
    _kopiere_pdf(settings.eingang_ordner, "b.pdf")
    aufrufe: list[str] = []

    def verarbeite(datei: Path) -> None:
        aufrufe.append(datei.name)
        if datei.name == "a.pdf":
            raise RuntimeError("Stammdaten kaputt")

    with caplog.at_level("ERROR", logger="auftragsautomat.watcher"):
        anzahl = watcher.starte_watch(settings, verarbeite, einmalig=True, stabil_sekunden=0.05)

    assert anzahl == 2 and aufrufe == ["a.pdf", "b.pdf"]
    assert "Verarbeitung von" in caplog.text and "a.pdf" in caplog.text
    inhalt = (settings.log_ordner / watcher.VERARBEITET_DATEINAME).read_text(encoding="utf-8")
    assert "fehler: RuntimeError: Stammdaten kaputt" in inhalt
    # Fehlerhafte Datei wird nicht endlos wiederholt
    assert watcher.starte_watch(settings, verarbeite, einmalig=True, stabil_sekunden=0.05) == 0


def test_ist_fertig_geschrieben(tmp_path: Path):
    leer = tmp_path / "leer.pdf"
    leer.write_bytes(b"")
    assert watcher.ist_fertig_geschrieben(leer, stabil_sekunden=0.01) is False
    assert watcher.ist_fertig_geschrieben(tmp_path / "fehlt.pdf", stabil_sekunden=0.01) is False
    voll = tmp_path / "voll.pdf"
    voll.write_bytes(b"%PDF-1.4 daten")
    assert watcher.ist_fertig_geschrieben(voll, stabil_sekunden=0.01) is True


def test_warte_bis_fertig_bei_wachsender_datei(tmp_path: Path):
    datei = tmp_path / "wachsend.pdf"
    datei.write_bytes(b"a")

    def schreibe_weiter() -> None:
        for _ in range(3):
            time.sleep(0.05)
            with datei.open("ab") as f:
                f.write(b"b" * 10)

    t = threading.Thread(target=schreibe_weiter)
    t.start()
    assert watcher.warte_bis_fertig(datei, stabil_sekunden=0.1, max_wartezeit=5) is True
    t.join()
    assert datei.stat().st_size == 31
    # Zeitüberschreitung: Datei verschwindet
    assert watcher.warte_bis_fertig(tmp_path / "weg.pdf", stabil_sekunden=0.01, max_wartezeit=0.1) is False


def test_finde_eingangsdateien_rekursiv_und_gefiltert(tmp_path: Path):
    (tmp_path / "wik_leh").mkdir()
    (tmp_path / "wik_leh" / "b.PDF").write_bytes(b"x")
    (tmp_path / "a.eml").write_bytes(b"x")
    (tmp_path / "c.png").write_bytes(b"x")
    (tmp_path / "d.xlsx").write_bytes(b"x")
    (tmp_path / "e.part").write_bytes(b"x")
    (tmp_path / "._f.pdf").write_bytes(b"x")
    gefunden = [p.relative_to(tmp_path).as_posix() for p in watcher.finde_eingangsdateien(tmp_path)]
    assert gefunden == ["a.eml", "c.png", "wik_leh/b.PDF"]
    assert watcher.finde_eingangsdateien(tmp_path / "gibt_es_nicht") == []


def test_eingangs_handler_filtert_ereignisse(tmp_path: Path):
    warteschlange: queue.Queue[Path] = queue.Queue()
    handler = watcher.EingangsHandler(warteschlange)
    handler.on_created(FileCreatedEvent(str(tmp_path / "neu.pdf")))
    handler.on_created(FileCreatedEvent(str(tmp_path / "egal.docx")))
    handler.on_created(DirCreatedEvent(str(tmp_path / "ordner.pdf")))
    handler.on_moved(FileMovedEvent(str(tmp_path / "x.tmp"), str(tmp_path / "fertig.eml")))
    gemeldet = []
    while not warteschlange.empty():
        gemeldet.append(warteschlange.get_nowait().name)
    assert gemeldet == ["neu.pdf", "fertig.eml"]


def test_verarbeitungsliste_ohne_datei_und_mit_kaputten_zeilen(tmp_path: Path):
    liste = watcher.Verarbeitungsliste(None)
    datei = tmp_path / "x.pdf"
    datei.write_bytes(b"x")
    assert not liste.enthaelt(datei)
    liste.merke(datei)
    assert liste.enthaelt(datei) and len(liste) == 1

    merkdatei = tmp_path / "verarbeitet.txt"
    merkdatei.write_text("kaputt\nnur\tzwei\n/pfad/y.pdf\tabc\t1\n", encoding="utf-8")
    liste2 = watcher.Verarbeitungsliste(merkdatei)
    assert len(liste2) == 0


def test_verarbeite_standard_speichert_paket_ohne_zu_buchen(settings: Settings, caplog: pytest.LogCaptureFixture):
    pdf = _kopiere_pdf(settings.eingang_ordner / "wik_leh")
    verarbeite = watcher.verarbeite_standard(settings)
    with caplog.at_level("INFO", logger="auftragsautomat.watcher"):
        verarbeite(pdf)
    pakete = zustand.liste_pakete(zustand.paket_ordner(settings))
    assert len(pakete) == 1 and pakete[0].name == "kundenbestellung_kirchhellen.paket.json"
    paket = zustand.lade_paket(pakete[0])
    assert paket.firma_id == "wik_leh"
    assert paket.freigegeben is False
    assert any(p.artikel is not None and p.artikel.artikelnummer == "1643" for p in paket.positionen)
    assert "Zusammenfassung" in caplog.text
    assert not (settings.log_ordner / "protokoll.csv").exists()  # nichts gebucht


def test_dauerbetrieb_erkennt_neue_datei(settings: Settings):
    """Observer-Betrieb: Datei nach dem Start anlegen -> wird verarbeitet; Ende über stop_ereignis."""
    verarbeitet: list[Path] = []
    stop = threading.Event()
    ergebnis: list[int] = []

    def lauf() -> None:
        ergebnis.append(
            watcher.starte_watch(settings, verarbeitet.append, einmalig=False, stabil_sekunden=0.1, stop_ereignis=stop)
        )

    t = threading.Thread(target=lauf, daemon=True)
    t.start()
    time.sleep(0.5)  # Observer starten lassen
    pdf = _kopiere_pdf(settings.eingang_ordner / "woe_leh", "neu.pdf")
    frist = time.monotonic() + 15
    while time.monotonic() < frist and not verarbeitet:
        time.sleep(0.1)
    stop.set()
    t.join(timeout=10)

    assert not t.is_alive()
    assert verarbeitet == [pdf]
    assert ergebnis == [1]
