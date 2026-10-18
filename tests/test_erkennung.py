"""Tests für die Eingangs-Erkennung und den Extraktions-Dispatcher.

Das Vision-Modul wird nicht importiert: für Eingangstyp C wird es über ``sys.modules``
durch einen Fake ersetzt, damit diese Tests weder ``anthropic`` noch einen API-Schlüssel
brauchen.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from auftragsautomat.extraktion import erkennung
from auftragsautomat.extraktion.email_bestellung import EmailExtraktor
from auftragsautomat.extraktion.erkennung import (
    BILD_ENDUNGEN,
    EMAIL_ENDUNGEN,
    PDF_ENDUNGEN,
    UNTERSTUETZTE_ENDUNGEN,
    ExtraktorNichtVerfuegbar,
    NichtUnterstuetzteDatei,
    erkenne_eingangstyp,
    extrahiere,
    ist_unterstuetzt,
    waehle_extraktor,
)
from auftragsautomat.extraktion.text_pdf import TextPdfExtraktor
from auftragsautomat.models import Eingangstyp, Extraktionsmethode, RohBestellung

VISION_MODUL = "auftragsautomat.extraktion.vision"


@pytest.fixture()
def pdfs(fixtures_ordner: Path) -> Path:
    ordner = fixtures_ordner / "pdfs"
    assert ordner.is_dir(), "Beispiel-PDFs fehlen: python scripts/beispiel_pdfs_erzeugen.py"
    return ordner


@pytest.fixture()
def emails(fixtures_ordner: Path) -> Path:
    ordner = fixtures_ordner / "emails"
    assert ordner.is_dir(), "E-Mail-Fixtures fehlen (tests/fixtures/emails)"
    return ordner


class _FakeVisionExtraktor:
    """Ersatz für vision.VisionExtraktor: merkt sich Aufrufe, ruft keine API."""

    instanzen: list[_FakeVisionExtraktor] = []

    def __init__(self, settings, client=None) -> None:
        self.settings = settings
        self.client = client
        self.aufrufe: list[Path] = []
        _FakeVisionExtraktor.instanzen.append(self)

    def kann_verarbeiten(self, datei: Path) -> bool:
        return True

    def extrahiere(self, datei: Path) -> RohBestellung:
        self.aufrufe.append(Path(datei))
        return RohBestellung(
            eingangstyp=Eingangstyp.PREISLISTE_SCAN,
            quelldatei=Path(datei),
            extraktionsmethode=Extraktionsmethode.CLAUDE_VISION,
        )


@pytest.fixture()
def fake_vision(monkeypatch: pytest.MonkeyPatch) -> type[_FakeVisionExtraktor]:
    modul = types.ModuleType(VISION_MODUL)
    modul.VisionExtraktor = _FakeVisionExtraktor  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, VISION_MODUL, modul)
    _FakeVisionExtraktor.instanzen.clear()
    return _FakeVisionExtraktor


# ---------------------------------------------------------------------------
# Endungen
# ---------------------------------------------------------------------------


def test_unterstuetzte_endungen():
    assert {".pdf", ".eml", ".msg", ".txt", ".png", ".jpg", ".jpeg", ".tif"} <= UNTERSTUETZTE_ENDUNGEN
    assert UNTERSTUETZTE_ENDUNGEN == PDF_ENDUNGEN | BILD_ENDUNGEN | EMAIL_ENDUNGEN
    assert all(e.startswith(".") and e == e.lower() for e in UNTERSTUETZTE_ENDUNGEN)
    assert ".xlsx" not in UNTERSTUETZTE_ENDUNGEN


def test_ist_unterstuetzt(tmp_path: Path):
    assert ist_unterstuetzt(tmp_path / "bestellung.pdf") is True
    assert ist_unterstuetzt(tmp_path / "Bestellung.PDF") is True
    assert ist_unterstuetzt(tmp_path / "mail.eml") is True
    assert ist_unterstuetzt(tmp_path / "scan.JPG") is True
    assert ist_unterstuetzt(tmp_path / "liste.xlsx") is False
    assert ist_unterstuetzt(tmp_path / "ohne_endung") is False


# ---------------------------------------------------------------------------
# erkenne_eingangstyp
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("datei", "typ"),
    [
        ("kundenbestellung_kirchhellen.pdf", Eingangstyp.KUNDENBESTELLUNG_PDF),
        ("kundenbestellung_menrath.pdf", Eingangstyp.KUNDENBESTELLUNG_PDF),
        # Die Route ist A (Text-PDF); den Inhaltstyp B erkennt erst der Text-PDF-Extraktor.
        ("eigene_bestellung_cide.pdf", Eingangstyp.KUNDENBESTELLUNG_PDF),
        ("scan_preisliste.pdf", Eingangstyp.PREISLISTE_SCAN),
    ],
)
def test_erkenne_eingangstyp_fixture_pdfs(pdfs: Path, datei: str, typ: Eingangstyp):
    assert erkenne_eingangstyp(pdfs / datei) == typ


@pytest.mark.parametrize(
    "datei",
    ["bestellung_cide_positionen_im_text.eml", "bestellung_cide_mit_pdf_anhang.eml", "bestellung_bollina.txt"],
)
def test_erkenne_eingangstyp_fixture_emails(emails: Path, datei: str):
    assert erkenne_eingangstyp(emails / datei) == Eingangstyp.EIGENE_BESTELLUNG_EMAIL


@pytest.mark.parametrize("endung", [".eml", ".msg", ".txt", ".EML", ".Txt"])
def test_erkenne_eingangstyp_email_endungen(tmp_path: Path, endung: str):
    datei = tmp_path / f"mail{endung}"
    datei.write_bytes(b"Subject: Test\n\nHallo")
    assert erkenne_eingangstyp(datei) == Eingangstyp.EIGENE_BESTELLUNG_EMAIL


@pytest.mark.parametrize("endung", [".png", ".jpg", ".jpeg", ".tif", ".tiff", ".PNG"])
def test_erkenne_eingangstyp_bilder(tmp_path: Path, endung: str):
    datei = tmp_path / f"scan{endung}"
    datei.write_bytes(b"\x89PNG\r\n\x1a\n")  # Inhalt ist für die Routenwahl egal
    assert erkenne_eingangstyp(datei) == Eingangstyp.PREISLISTE_SCAN


def test_erkenne_eingangstyp_pdf_grossschreibung(pdfs: Path, tmp_path: Path):
    kopie = tmp_path / "BESTELLUNG.PDF"
    kopie.write_bytes((pdfs / "kundenbestellung_kirchhellen.pdf").read_bytes())
    assert erkenne_eingangstyp(kopie) == Eingangstyp.KUNDENBESTELLUNG_PDF


def test_erkenne_eingangstyp_kaputtes_pdf_geht_an_vision(tmp_path: Path):
    kaputt = tmp_path / "kaputt.pdf"
    kaputt.write_bytes(b"%PDF-1.4 kaputt")
    assert erkenne_eingangstyp(kaputt) == Eingangstyp.PREISLISTE_SCAN


@pytest.mark.parametrize("name", ["liste.xlsx", "brief.docx", "ohne_endung", "archiv.zip", "mail.eml.part"])
def test_erkenne_eingangstyp_unbekannte_endung(tmp_path: Path, name: str):
    datei = tmp_path / name
    datei.write_bytes(b"x")
    with pytest.raises(NichtUnterstuetzteDatei) as info:
        erkenne_eingangstyp(datei)
    meldung = str(info.value)
    assert name in meldung
    assert ".pdf" in meldung and ".eml" in meldung  # erlaubte Endungen werden genannt
    assert isinstance(info.value, ValueError)


def test_erkenne_eingangstyp_fehlende_datei(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        erkenne_eingangstyp(tmp_path / "gibt_es_nicht.pdf")


# ---------------------------------------------------------------------------
# waehle_extraktor / extrahiere
# ---------------------------------------------------------------------------


def test_waehle_extraktor_a_und_b(settings):
    assert isinstance(waehle_extraktor(Eingangstyp.KUNDENBESTELLUNG_PDF, settings), TextPdfExtraktor)
    assert isinstance(waehle_extraktor(Eingangstyp.EIGENE_BESTELLUNG_EMAIL, settings), EmailExtraktor)


def test_waehle_extraktor_c_nutzt_vision_mit_client(settings, fake_vision):
    client = object()
    extraktor = waehle_extraktor(Eingangstyp.PREISLISTE_SCAN, settings, claude_client=client)
    assert isinstance(extraktor, fake_vision)
    assert extraktor.settings is settings
    assert extraktor.client is client


def test_extrahiere_text_pdf_kundenbestellung(settings, pdfs: Path):
    roh = extrahiere(pdfs / "kundenbestellung_kirchhellen.pdf", settings)
    assert roh.eingangstyp == Eingangstyp.KUNDENBESTELLUNG_PDF
    assert roh.extraktionsmethode == Extraktionsmethode.PDFPLUMBER
    assert roh.kunde_text == "Weinmarkt Kirchhellen GmbH"
    assert roh.bestellnummer == "B-4712"
    assert len(roh.positionen) == 4


def test_extrahiere_text_pdf_verfeinert_auf_b(settings, pdfs: Path):
    """Route A, aber der Text-PDF-Extraktor erkennt die eigene Bestellung (B)."""
    roh = extrahiere(pdfs / "eigene_bestellung_cide.pdf", settings)
    assert roh.eingangstyp == Eingangstyp.EIGENE_BESTELLUNG_EMAIL
    assert roh.extraktionsmethode == Extraktionsmethode.PDFPLUMBER
    assert roh.belegnummer == "BE-2026-0077"
    assert roh.lieferant_text == "Cide srl, Treviso"


def test_extrahiere_email(settings, emails: Path):
    roh = extrahiere(emails / "bestellung_bollina.txt", settings)
    assert roh.eingangstyp == Eingangstyp.EIGENE_BESTELLUNG_EMAIL
    assert roh.extraktionsmethode == Extraktionsmethode.EMAIL
    assert roh.lieferant_text == "Bollina srl"
    assert roh.kunde_text == "Hegmann"
    assert len(roh.positionen) == 2

    roh = extrahiere(emails / "bestellung_cide_positionen_im_text.eml", settings)
    assert roh.extraktionsmethode == Extraktionsmethode.EMAIL
    assert roh.belegnummer == "BE-2026-0078"
    assert len(roh.positionen) == 3


def test_extrahiere_scan_pdf_an_vision(settings, pdfs: Path, fake_vision):
    client = object()
    roh = extrahiere(pdfs / "scan_preisliste.pdf", settings, claude_client=client)
    assert roh.eingangstyp == Eingangstyp.PREISLISTE_SCAN
    assert roh.extraktionsmethode == Extraktionsmethode.CLAUDE_VISION
    assert roh.quelldatei == pdfs / "scan_preisliste.pdf"
    assert len(fake_vision.instanzen) == 1
    instanz = fake_vision.instanzen[0]
    assert instanz.client is client
    assert instanz.aufrufe == [pdfs / "scan_preisliste.pdf"]


def test_extrahiere_bild_an_vision_ohne_client(settings, tmp_path: Path, fake_vision):
    bild = tmp_path / "preisliste.jpg"
    bild.write_bytes(b"\xff\xd8\xff\xe0")
    roh = extrahiere(bild, settings)
    assert roh.extraktionsmethode == Extraktionsmethode.CLAUDE_VISION
    assert fake_vision.instanzen[0].client is None


def test_extrahiere_vision_modul_fehlt(settings, pdfs: Path, monkeypatch: pytest.MonkeyPatch):
    """Ohne importierbares Vision-Modul gibt es eine klare Fehlermeldung statt eines ImportError."""
    monkeypatch.setitem(sys.modules, VISION_MODUL, None)  # None in sys.modules -> ImportError
    with pytest.raises(ExtraktorNichtVerfuegbar) as info:
        extrahiere(pdfs / "scan_preisliste.pdf", settings)
    assert "Vision" in str(info.value)


def test_waehle_extraktor_vision_ohne_klasse(settings, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setitem(sys.modules, VISION_MODUL, types.ModuleType(VISION_MODUL))
    with pytest.raises(ExtraktorNichtVerfuegbar):
        waehle_extraktor(Eingangstyp.PREISLISTE_SCAN, settings)


def test_vision_wird_erst_bei_bedarf_importiert(settings, pdfs: Path):
    """Für A und B darf erkennung das Vision-Modul nicht als Modulattribut binden (lazy import)."""
    assert not hasattr(erkennung, "vision")
    extrahiere(pdfs / "kundenbestellung_kirchhellen.pdf", settings)
    assert not hasattr(erkennung, "vision")


def test_extrahiere_unbekannte_endung(settings, tmp_path: Path):
    datei = tmp_path / "liste.csv"
    datei.write_text("a;b", encoding="utf-8")
    with pytest.raises(ValueError):
        extrahiere(datei, settings)
