"""Gemeinsame pytest-Fixtures.

``settings`` lädt die Test-Konfiguration aus tests/fixtures/config (Stammdaten-CSVs
aus tests/fixtures/stammdaten, Alias-Tabellen aus dem echten config/aliase).
Schreibende Pfade (logs, archiv, lexware_import, Entwürfe, Dossiers, Ablage, Gedächtnisdateien für
Firmenwahl und Aktionen, Merkdatei und Berichte der Überwachung, Tracking-Screenshots) werden pro Test in
ein tmp_path umgeleitet, damit Tests nichts im Repo hinterlassen. Nur gelesene Dateien (E-Mail-Vorlagen,
Signaturen, Anrede-Regeln, Hillebrand-Regeln) kommen aus dem echten config/.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from auftragsautomat.config import Settings, lade_settings

FIXTURES = Path(__file__).parent / "fixtures"
REPO_CONFIG = Path(__file__).resolve().parents[1] / "config"


@pytest.fixture()
def fixtures_ordner() -> Path:
    return FIXTURES


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    s = lade_settings(FIXTURES)
    s.eingang_ordner = tmp_path / "eingang"
    s.archiv_ordner = tmp_path / "archiv"
    s.log_ordner = tmp_path / "logs"
    s.xml_ausgabe_ordner = tmp_path / "logs" / "opentrans"
    s.lexware.screenshots_ordner = tmp_path / "logs" / "screenshots"
    s.lexware.pdf_ausgabe_ordner = tmp_path / "logs" / "pdf"
    s.kommunikation.entwuerfe_ordner = tmp_path / "logs" / "entwuerfe"
    s.dossier.lokaler_ordner = tmp_path / "logs" / "dossiers"
    s.ablage.lokaler_ordner = tmp_path / "logs" / "ablage"
    s.rabatte.rabatt_pdf_ordner = tmp_path / "eingang" / "rabatte"
    # Gedächtnisdateien und Merkdateien würden sonst unter tests/fixtures/config bzw. tests/fixtures/logs landen
    s.rabatte.gedaechtnis_datei = tmp_path / "config" / "aktionen.yaml"
    s.firmenwahl.gedaechtnis_datei = tmp_path / "config" / "firmen_gedaechtnis.yaml"
    s.ueberwachung.merkdatei = tmp_path / "logs" / "nachfragen.json"
    s.ueberwachung.berichte_ordner = tmp_path / "logs" / "ueberwachung"
    s.tracking.screenshots_ordner = tmp_path / "logs" / "tracking"
    # nur gelesen: Vorlagen, Signaturen, Anrede- und Hillebrand-Regeln aus dem echten config/
    s.kommunikation.vorlagen_ordner = REPO_CONFIG / "vorlagen"
    s.kommunikation.signaturen_datei = REPO_CONFIG / "signaturen.yaml"
    s.kommunikation.anrede_datei = REPO_CONFIG / "anrede.yaml"
    s.tracking.hillebrand_konfig = REPO_CONFIG / "tracking_hillebrand.yaml"
    for firma in s.firmen.values():
        if firma.opentrans.import_ordner is not None:
            firma.opentrans.import_ordner = tmp_path / "lexware_import" / firma.id
    for ordner in (s.eingang_ordner, s.archiv_ordner, s.log_ordner, s.xml_ausgabe_ordner):
        ordner.mkdir(parents=True, exist_ok=True)
    return s


@pytest.fixture()
def pdf_ordner(tmp_path: Path) -> Path:
    """Kopie der Beispiel-PDFs (tests/fixtures/pdfs) in ein tmp-Verzeichnis."""
    ziel = tmp_path / "pdfs"
    quelle = FIXTURES / "pdfs"
    if quelle.exists():
        shutil.copytree(quelle, ziel)
    else:
        ziel.mkdir()
    return ziel
