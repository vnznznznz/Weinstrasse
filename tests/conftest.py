"""Gemeinsame pytest-Fixtures.

``settings`` lädt die Test-Konfiguration aus tests/fixtures/config (Stammdaten-CSVs
aus tests/fixtures/stammdaten, Alias-Tabellen aus dem echten config/aliase).
Schreibende Pfade (logs, archiv, lexware_import) werden pro Test in ein tmp_path
umgeleitet, damit Tests nichts im Repo hinterlassen.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from auftragsautomat.config import Settings, lade_settings

FIXTURES = Path(__file__).parent / "fixtures"


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
