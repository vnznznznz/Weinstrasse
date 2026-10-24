"""Tests für eingang.py (eigener Eingang des Tools, Upload aus der Freigabe-UI).

Alle Dateinamen und Inhalte sind synthetisch; es wird nur in tmp_path geschrieben.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

import pytest

from auftragsautomat import eingang
from auftragsautomat.config import Settings
from auftragsautomat.eingang import (
    TEMP_ENDUNG,
    TEMP_PRAEFIX,
    UploadAbgelehnt,
    eingang_ordner,
    erlaubte_endungen,
    freier_pfad,
    ist_erlaubter_typ,
    ist_temporaer,
    liste_eingang,
    pruefe_upload,
    schreibe_atomar,
    speichere_upload,
)
from auftragsautomat.watcher import finde_eingangsdateien

PDF = b"%PDF-1.4\n%synthetische Kundenbestellung\n"
JETZT = datetime(2026, 10, 24, 15, 30, 0)


def _uhr(zeit: datetime = JETZT):
    return lambda: zeit


# ---------------------------------------------------------------------------
# Prüfung
# ---------------------------------------------------------------------------


def test_erlaubte_endungen_normalisiert(settings: Settings):
    settings.eingang.upload_dateitypen = [".pdf", "MSG", " .Eml ", "pdf", ""]
    assert erlaubte_endungen(settings) == (".pdf", ".msg", ".eml")
    assert ist_erlaubter_typ("Bestellung.PDF", settings) and ist_erlaubter_typ(Path("x/y.msg"), settings)
    assert not ist_erlaubter_typ("Bestellung.txt", settings)


def test_pruefe_upload_dateityp(settings: Settings):
    with pytest.raises(UploadAbgelehnt) as exc:
        pruefe_upload("virus.exe", 10, settings)
    assert "'.exe'" in str(exc.value) and ".pdf" in str(exc.value) and "nicht unterstützt" in str(exc.value)
    with pytest.raises(UploadAbgelehnt, match="ohne Endung"):
        pruefe_upload("Bestellung", 10, settings)
    with pytest.raises(UploadAbgelehnt, match="Dateiname fehlt"):
        pruefe_upload("   ", 10, settings)
    with pytest.raises(UploadAbgelehnt, match="Dateiname fehlt"):
        pruefe_upload("", 10, settings)
    with pytest.raises(UploadAbgelehnt, match="ohne Endung"):
        pruefe_upload("ordner/", 10, settings)  # nur ein Ordnername, keine Datei
    with pytest.raises(UploadAbgelehnt, match="ohne Endung"):
        pruefe_upload(" .pdf", 10, settings)  # ".pdf" ist ein Punktname ohne Endung


def test_pruefe_upload_groesse(settings: Settings):
    settings.eingang.upload_max_mb = 1
    assert pruefe_upload("Bestellung.pdf", 1024 * 1024, settings) == "Bestellung.pdf"
    with pytest.raises(UploadAbgelehnt) as exc:
        pruefe_upload("Bestellung.pdf", 1024 * 1024 + 1, settings)
    assert "zu groß" in str(exc.value) and "1 MB" in str(exc.value)
    with pytest.raises(UploadAbgelehnt, match="leer"):
        pruefe_upload("Bestellung.pdf", 0, settings)


def test_pruefe_upload_deaktiviert(settings: Settings):
    settings.eingang.upload_erlaubt = False
    with pytest.raises(UploadAbgelehnt, match="deaktiviert"):
        pruefe_upload("Bestellung.pdf", 10, settings)


def test_pruefe_upload_bereinigt_namen(settings: Settings):
    assert pruefe_upload("Bestellung.PDF", 10, settings) == "Bestellung.pdf"
    assert pruefe_upload("C:\\Users\\beispiel\\Downloads\\Bestellung 4711.pdf", 10, settings) == "Bestellung 4711.pdf"
    assert pruefe_upload("../../etc/Bestellung.pdf", 10, settings) == "Bestellung.pdf"
    name = pruefe_upload('Best:ell*ung?<4711>|".pdf', 10, settings)
    assert name.endswith(".pdf") and not any(z in name for z in '\\/:*?"<>|')
    assert pruefe_upload("Müller & Söhne – Auftrag.eml", 10, settings) == "Müller & Söhne – Auftrag.eml"
    assert pruefe_upload("???.pdf", 10, settings) == "-.pdf"


def test_pruefe_upload_leerer_stamm(settings: Settings):
    # Steuerzeichen vor der Endung -> Ersatzzeichen; nur Punkte/Leerzeichen -> Ersatzname
    assert pruefe_upload("\x01\x02.pdf", 10, settings) == "-.pdf"
    assert pruefe_upload(". .pdf", 10, settings) == "Auftrag.pdf"
    assert pruefe_upload("...pdf", 10, settings) == "Auftrag.pdf"


# ---------------------------------------------------------------------------
# Zielordner und Namen
# ---------------------------------------------------------------------------


def test_eingang_ordner_je_firma(settings: Settings):
    assert eingang_ordner(settings) == settings.eingang_ordner
    assert eingang_ordner(settings, "wik_fh_seh") == settings.eingang_ordner / "wik_fh_seh"
    settings.firma("wik_fh_seh").eingang_unterordner = None
    assert eingang_ordner(settings, "wik_fh_seh") == settings.eingang_ordner / "wik_fh_seh"
    settings.firma("wik_fh_seh").eingang_unterordner = "WIK FH"
    assert eingang_ordner(settings, "wik_fh_seh") == settings.eingang_ordner / "WIK FH"
    settings.eingang.upload_in_firmen_unterordner = False
    assert eingang_ordner(settings, "wik_fh_seh") == settings.eingang_ordner
    settings.eingang.upload_in_firmen_unterordner = True
    with pytest.raises(UploadAbgelehnt, match="Unbekannte Firma 'nix'"):
        eingang_ordner(settings, "nix")


def test_freier_pfad(tmp_path: Path):
    assert freier_pfad(tmp_path, "Bestellung.pdf", jetzt=JETZT) == tmp_path / "Bestellung.pdf"
    (tmp_path / "Bestellung.pdf").write_bytes(b"1")
    mit_zeit = tmp_path / "Bestellung 2026-10-24 15-30-00.pdf"
    assert freier_pfad(tmp_path, "Bestellung.pdf", jetzt=JETZT) == mit_zeit
    mit_zeit.write_bytes(b"2")
    assert freier_pfad(tmp_path, "Bestellung.pdf", jetzt=JETZT) == tmp_path / "Bestellung 2026-10-24 15-30-00 (2).pdf"
    (tmp_path / "Bestellung 2026-10-24 15-30-00 (2).pdf").write_bytes(b"3")
    assert freier_pfad(tmp_path, "Bestellung.pdf", jetzt=JETZT) == tmp_path / "Bestellung 2026-10-24 15-30-00 (3).pdf"


def test_freier_pfad_gibt_auf(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(eingang, "_MAX_ZAEHLER", 2)
    (tmp_path / "x.pdf").write_bytes(b"1")
    (tmp_path / "x 2026-10-24 15-30-00.pdf").write_bytes(b"1")
    (tmp_path / "x 2026-10-24 15-30-00 (2).pdf").write_bytes(b"1")
    with pytest.raises(UploadAbgelehnt, match="Kein freier Dateiname"):
        freier_pfad(tmp_path, "x.pdf", jetzt=JETZT)


# ---------------------------------------------------------------------------
# Upload speichern
# ---------------------------------------------------------------------------


def test_speichere_upload_im_eingang(settings: Settings):
    pfad = speichere_upload("Bestellung 4711.pdf", PDF, settings)
    assert pfad == settings.eingang_ordner / "Bestellung 4711.pdf"
    assert pfad.read_bytes() == PDF
    assert [p.name for p in settings.eingang_ordner.iterdir()] == ["Bestellung 4711.pdf"]  # keine Temp-Datei übrig
    assert finde_eingangsdateien(settings.eingang_ordner) == [pfad]  # der Watcher nimmt sie auf


def test_speichere_upload_in_firmen_unterordner(settings: Settings):
    pfad = speichere_upload("Bestellung.pdf", PDF, settings, firma_id="wik_leh")
    assert pfad == settings.eingang_ordner / "wik_leh" / "Bestellung.pdf" and pfad.is_file()
    settings.eingang.upload_in_firmen_unterordner = False
    pfad2 = speichere_upload("Bestellung.pdf", PDF, settings, firma_id="wik_leh")
    assert pfad2 == settings.eingang_ordner / "Bestellung.pdf" and pfad2.is_file()
    with pytest.raises(UploadAbgelehnt, match="Unbekannte Firma"):
        settings.eingang.upload_in_firmen_unterordner = True
        speichere_upload("Bestellung.pdf", PDF, settings, firma_id="gibt_es_nicht")


def test_speichere_upload_kollision(settings: Settings):
    erste = speichere_upload("Bestellung.pdf", PDF, settings, jetzt=_uhr())
    zweite = speichere_upload("Bestellung.pdf", PDF + b"2", settings, jetzt=_uhr())
    dritte = speichere_upload("Bestellung.pdf", PDF + b"3", settings, jetzt=_uhr())
    assert erste.name == "Bestellung.pdf"
    assert zweite.name == "Bestellung 2026-10-24 15-30-00.pdf"
    assert dritte.name == "Bestellung 2026-10-24 15-30-00 (2).pdf"
    assert erste.read_bytes() == PDF and zweite.read_bytes() == PDF + b"2" and dritte.read_bytes() == PDF + b"3"


def test_speichere_upload_lehnt_ab_und_schreibt_nichts(settings: Settings):
    settings.eingang.upload_max_mb = 1
    with pytest.raises(UploadAbgelehnt):
        speichere_upload("Bestellung.pdf", b"x" * (1024 * 1024 + 1), settings)
    with pytest.raises(UploadAbgelehnt):
        speichere_upload("Bestellung.txt", PDF, settings)
    with pytest.raises(UploadAbgelehnt):
        speichere_upload("Bestellung.pdf", b"", settings)
    assert list(settings.eingang_ordner.iterdir()) == []


def test_speichere_upload_pfadausbruch(settings: Settings):
    pfad = speichere_upload("..\\..\\Bestellung.pdf", PDF, settings)
    assert pfad.parent == settings.eingang_ordner and pfad.name == "Bestellung.pdf"
    pfad = speichere_upload("/etc/passwd.pdf", PDF, settings)
    assert pfad == settings.eingang_ordner / "passwd.pdf"


# ---------------------------------------------------------------------------
# Atomares Schreiben
# ---------------------------------------------------------------------------


def test_schreibe_atomar_watcher_sieht_nur_fertige_datei(settings: Settings, monkeypatch: pytest.MonkeyPatch):
    ziel = settings.eingang_ordner / "Bestellung.pdf"
    beobachtet: dict[str, object] = {}
    original_replace = os.replace

    def replace_mit_pruefung(quelle, ziel_pfad):
        quelle = Path(quelle)
        beobachtet["tmp_name"] = quelle.name
        beobachtet["tmp_inhalt"] = quelle.read_bytes()
        beobachtet["ziel_vorher"] = Path(ziel_pfad).exists()
        beobachtet["watcher_vorher"] = finde_eingangsdateien(settings.eingang_ordner)
        beobachtet["eingang_vorher"] = liste_eingang(settings)
        original_replace(quelle, ziel_pfad)

    monkeypatch.setattr(os, "replace", replace_mit_pruefung)
    assert schreibe_atomar(ziel, PDF) == ziel
    assert ziel.read_bytes() == PDF
    tmp_name = str(beobachtet["tmp_name"])
    assert tmp_name.startswith(TEMP_PRAEFIX) and tmp_name.endswith(TEMP_ENDUNG)
    assert beobachtet["tmp_inhalt"] == PDF
    assert beobachtet["ziel_vorher"] is False
    assert beobachtet["watcher_vorher"] == [] and beobachtet["eingang_vorher"] == []  # Temp-Datei wird ignoriert
    assert [p.name for p in settings.eingang_ordner.iterdir()] == ["Bestellung.pdf"]


def test_schreibe_atomar_fehler_hinterlaesst_nichts(settings: Settings, monkeypatch: pytest.MonkeyPatch):
    def kaputt(fd):
        raise OSError("Datenträger voll")

    monkeypatch.setattr(os, "fsync", kaputt)
    ziel = settings.eingang_ordner / "Bestellung.pdf"
    with pytest.raises(OSError, match="Datenträger voll"):
        schreibe_atomar(ziel, PDF)
    assert not ziel.exists()
    assert list(settings.eingang_ordner.iterdir()) == []


def test_schreibe_atomar_legt_ordner_an(tmp_path: Path):
    ziel = tmp_path / "neu" / "tiefer" / "Bestellung.pdf"
    schreibe_atomar(ziel, PDF)
    assert ziel.read_bytes() == PDF


def test_ist_temporaer():
    assert ist_temporaer(".upload_abc123.tmp")
    assert ist_temporaer("~$Bestellung.pdf")
    assert ist_temporaer("Bestellung.pdf.part")
    assert ist_temporaer("Bestellung.crdownload")
    assert not ist_temporaer("Bestellung.pdf")
    assert not ist_temporaer("tmp Bestellung.pdf")


# ---------------------------------------------------------------------------
# Eingang auflisten
# ---------------------------------------------------------------------------


def test_liste_eingang(settings: Settings):
    e = settings.eingang_ordner
    (e / "wik_leh").mkdir()
    (e / "rabatte").mkdir()
    settings.rabatte.rabatt_pdf_ordner = e / "rabatte"
    dateien = {
        "b Bestellung.pdf": True,
        "A Bestellung.PDF": True,
        "wik_leh/Bestellung.eml": True,
        "wik_leh/Scan.jpg": True,
        "notiz.txt": False,
        "~$Bestellung.pdf": False,
        ".upload_x.tmp": False,
        "Bestellung.pdf.part": False,
        "rabatte/Rabattliste.pdf": False,
    }
    for name in dateien:
        (e / name).write_bytes(b"x")
    erwartet = sorted(e / name for name, drin in dateien.items() if drin)
    assert liste_eingang(settings) == erwartet
    assert liste_eingang(settings) == sorted(liste_eingang(settings))


def test_liste_eingang_ohne_ordner(settings: Settings, tmp_path: Path):
    settings.eingang_ordner = tmp_path / "gibt_es_nicht"
    assert liste_eingang(settings) == []


def test_upload_erscheint_in_liste_und_beim_watcher(settings: Settings):
    pfad = speichere_upload("Bestellung.pdf", PDF, settings, firma_id="woe_leh")
    assert liste_eingang(settings) == [pfad]
    assert finde_eingangsdateien(settings.eingang_ordner) == [pfad]
