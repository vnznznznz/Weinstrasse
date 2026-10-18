"""Tests für die Vision-Extraktion (extraktion.vision) – ohne echte API-Aufrufe.

Der Claude-Client wird durch ein Fake-Objekt ersetzt (``messages.parse(**kwargs)`` liefert eine
vorbereitete Antwort). Ein Test fährt zusätzlich durch den echten SDK-Client mit einem
``httpx2.MockTransport``, damit Schema-Transformation und ``parsed_output`` des SDK geprüft werden.
"""

from __future__ import annotations

import base64
import io
import json
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import anthropic
import httpx2
import pytest
from PIL import Image, ImageDraw

from auftragsautomat.config import Settings
from auftragsautomat.extraktion import vision
from auftragsautomat.extraktion.vision import (
    VisionAbgelehnt,
    VisionAbgeschnitten,
    VisionApiFehler,
    VisionEingabeFehler,
    VisionErgebnis,
    VisionExtraktor,
    VisionFehler,
    VisionPosition,
    VisionSummenvermerk,
    erzeuge_client,
    erzeuge_inhaltsbloecke,
    pdf_hat_textebene,
    wandle_in_rohbestellung,
)
from auftragsautomat.models import Eingangstyp, Extraktionsmethode, Pruefcode, Schweregrad

# ---------------------------------------------------------------------------
# Testdateien
# ---------------------------------------------------------------------------


def _preislisten_bild(breite: int = 600, hoehe: int = 800) -> Image.Image:
    bild = Image.new("RGB", (breite, hoehe), (247, 245, 240))
    zeichnen = ImageDraw.Draw(bild)
    zeichnen.text((40, 40), "Cide srl - Preisliste 2026", fill=(0, 0, 0))
    zeichnen.text((40, 90), "Villa Armellina Gloria Secco Frizzante 0,75 l   6,90   [12]", fill=(0, 0, 0))
    zeichnen.text((40, 120), "Villa Armellina Rosato Secco Frizzante 0,75 l   6,90", fill=(0, 0, 0))
    zeichnen.text((40, 700), "180 R", fill=(30, 30, 120))
    return bild


def _erzeuge_bild_pdf(ziel: Path, seiten: int = 1) -> Path:
    """PDF nur aus Bildern (keine Textebene), wie ein Scanner es liefert."""
    bilder = [_preislisten_bild() for _ in range(seiten)]
    bilder[0].save(ziel, format="PDF", save_all=True, append_images=bilder[1:])
    return ziel


def _erzeuge_text_pdf(ziel: Path) -> Path:
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(str(ziel), pagesize=(595, 842), invariant=1)
    c.drawString(72, 780, "Bestellung B-4712 Weinmarkt Kirchhellen")
    c.drawString(72, 760, "60 Fl Villa Armellina Gloria Secco Frizzante 6,90")
    c.showPage()
    c.save()
    return ziel


@pytest.fixture()
def scan_pdf(tmp_path: Path, fixtures_ordner: Path) -> Path:
    vorhanden = fixtures_ordner / "pdfs" / "scan_preisliste.pdf"
    if vorhanden.exists():
        return vorhanden
    return _erzeuge_bild_pdf(tmp_path / "scan_preisliste.pdf")


@pytest.fixture()
def text_pdf(tmp_path: Path) -> Path:
    return _erzeuge_text_pdf(tmp_path / "text_bestellung.pdf")


@pytest.fixture()
def scan_png(tmp_path: Path) -> Path:
    ziel = tmp_path / "scan.png"
    _preislisten_bild().save(ziel, format="PNG")
    return ziel


# ---------------------------------------------------------------------------
# Fake-Client
# ---------------------------------------------------------------------------


@dataclass
class FakeNutzung:
    input_tokens: int = 2345
    output_tokens: int = 678
    cache_read_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None


@dataclass
class FakeStopDetails:
    category: str | None
    explanation: str | None
    type: str = "refusal"


@dataclass
class FakeAntwort:
    parsed_output: VisionErgebnis | None
    stop_reason: str = "end_turn"
    stop_details: FakeStopDetails | None = None
    usage: FakeNutzung = field(default_factory=FakeNutzung)
    model: str = "claude-opus-5-5"
    id: str = "msg_test"


class FakeMessages:
    def __init__(self, antwort: Any = None, fehler: Exception | None = None) -> None:
        self.antwort = antwort
        self.fehler = fehler
        self.aufrufe: list[dict[str, Any]] = []

    def parse(self, **kwargs: Any) -> Any:
        self.aufrufe.append(kwargs)
        if self.fehler is not None:
            raise self.fehler
        return self.antwort


class FakeClient:
    def __init__(self, antwort: Any = None, fehler: Exception | None = None) -> None:
        self.messages = FakeMessages(antwort, fehler)

    @property
    def letzter_aufruf(self) -> dict[str, Any]:
        return self.messages.aufrufe[-1]


def position(**werte: Any) -> VisionPosition:
    basis: dict[str, Any] = {
        "laufnr": 1,
        "bezeichnung": "Villa Armellina Gloria Secco Frizzante",
        "jahrgang": None,
        "menge_text": None,
        "menge": None,
        "einheit": None,
        "preis": 6.9,
        "volumen_text": "0,75 l",
        "artikelnummer": None,
        "hinweis": None,
        "durchgestrichen": False,
        "konfidenz_menge": 1.0,
        "konfidenz_preis": 0.99,
        "rohtext": "Villa Armellina Gloria Secco Frizzante 0,75 l 6,90",
    }
    basis.update(werte)
    return VisionPosition(**basis)


def ergebnis(**werte: Any) -> VisionErgebnis:
    basis: dict[str, Any] = {
        "kunde_text": "Weinmarkt Kirchhellen",
        "lieferant_text": "Cide srl",
        "belegnummer": None,
        "bestellnummer": "B-4712",
        "datum": "15.10.2026",
        "liefertag": "24.10.2026",
        "lieferadresse": None,
        "positionen": [
            position(laufnr=1, menge_text="12", menge=12.0, einheit="Fl", konfidenz_menge=0.95),
            position(
                laufnr=2,
                bezeichnung="Villa Armellina Rosato Secco Frizzante",
                rohtext="Villa Armellina Rosato Secco Frizzante 0,75 l 6,90",
            ),
            position(
                laufnr=3,
                bezeichnung="Colosi Nero d'Avola Sicilia DOC",
                jahrgang=2022,
                menge_text="6",
                menge=6.0,
                preis=8.4,
                hinweis="11+1",
                konfidenz_menge=0.9,
                rohtext="Colosi Nero d'Avola Sicilia DOC 2022 0,75 l 8,40 6 11+1",
            ),
        ],
        "summenvermerke": [VisionSummenvermerk(wert=18.0, einheit="R", rohtext="18 R", konfidenz=0.9)],
        "seitenzahl": 1,
        "anmerkungen": ["Randnotiz unleserlich"],
    }
    basis.update(werte)
    return VisionErgebnis(**basis)


# ---------------------------------------------------------------------------
# kann_verarbeiten / Textebene
# ---------------------------------------------------------------------------


def test_pdf_ohne_textebene_wird_erkannt(settings: Settings, scan_pdf: Path, text_pdf: Path) -> None:
    assert pdf_hat_textebene(scan_pdf) is False
    assert pdf_hat_textebene(text_pdf) is True
    extraktor = VisionExtraktor(settings)
    assert extraktor.kann_verarbeiten(scan_pdf) is True
    assert extraktor.kann_verarbeiten(text_pdf) is False


def test_kann_verarbeiten_bilder_und_fremde_dateien(settings: Settings, scan_png: Path, tmp_path: Path) -> None:
    extraktor = VisionExtraktor(settings)
    assert extraktor.kann_verarbeiten(scan_png) is True
    tif = tmp_path / "scan.tif"
    _preislisten_bild().save(tif, format="TIFF")
    assert extraktor.kann_verarbeiten(tif) is True
    txt = tmp_path / "bestellung.txt"
    txt.write_text("36 x Gavi", encoding="utf-8")
    assert extraktor.kann_verarbeiten(txt) is False
    assert extraktor.kann_verarbeiten(tmp_path / "gibt_es_nicht.pdf") is False
    kaputt = tmp_path / "kaputt.pdf"
    kaputt.write_bytes(b"kein pdf")
    assert extraktor.kann_verarbeiten(kaputt) is False
    with pytest.raises(VisionEingabeFehler):
        pdf_hat_textebene(kaputt)


# ---------------------------------------------------------------------------
# Request-Aufbau
# ---------------------------------------------------------------------------


def test_anfrage_pdf_dokumentblock_vor_text(settings: Settings, scan_pdf: Path) -> None:
    client = FakeClient(FakeAntwort(ergebnis()))
    extraktor = VisionExtraktor(settings, client=client)
    extraktor.extrahiere(scan_pdf)

    aufruf = client.letzter_aufruf
    assert aufruf["model"] == settings.claude.modell == "claude-opus-5-5"
    assert aufruf["max_tokens"] == settings.claude.max_tokens
    assert aufruf["output_config"] == {"effort": settings.claude.effort}
    assert aufruf["output_format"] is VisionErgebnis
    assert "Preisliste" in aufruf["system"]
    for verboten in ("thinking", "temperature", "tool_choice", "tools"):
        assert verboten not in aufruf
    assert aufruf["timeout"] >= 600

    nachrichten = aufruf["messages"]
    assert len(nachrichten) == 1 and nachrichten[0]["role"] == "user"  # kein Prefill
    inhalt = nachrichten[0]["content"]
    assert [block["type"] for block in inhalt] == ["document", "text"]
    quelle = inhalt[0]["source"]
    assert quelle["type"] == "base64"
    assert quelle["media_type"] == "application/pdf"
    assert "\n" not in quelle["data"]
    assert base64.b64decode(quelle["data"]) == scan_pdf.read_bytes()
    assert "Preisliste" in inhalt[1]["text"]


def test_anfrage_bild_png(settings: Settings, scan_png: Path) -> None:
    client = FakeClient(FakeAntwort(ergebnis()))
    roh = VisionExtraktor(settings, client=client).extrahiere(scan_png)
    inhalt = client.letzter_aufruf["messages"][0]["content"]
    assert [block["type"] for block in inhalt] == ["image", "text"]
    assert inhalt[0]["source"] == {
        "type": "base64",
        "media_type": "image/png",
        "data": base64.standard_b64encode(scan_png.read_bytes()).decode("ascii"),
    }
    assert roh.eingangstyp == Eingangstyp.PREISLISTE_SCAN
    assert roh.seiten == 1  # aus seitenzahl der Antwort, da kein PDF


def test_anfrage_tiff_wird_nach_png_gewandelt(settings: Settings, tmp_path: Path) -> None:
    tif = tmp_path / "scan.tif"
    seiten = [_preislisten_bild(), _preislisten_bild(400, 300)]
    seiten[0].save(tif, format="TIFF", save_all=True, append_images=seiten[1:])

    bloecke = erzeuge_inhaltsbloecke(tif)
    assert len(bloecke) == 2
    for block, original in zip(bloecke, seiten, strict=True):
        assert block["type"] == "image"
        assert block["source"]["media_type"] == "image/png"
        daten = base64.b64decode(block["source"]["data"])
        assert daten.startswith(b"\x89PNG")
        with Image.open(io.BytesIO(daten)) as bild:
            assert bild.size == original.size


def test_anfrage_unbekannter_dateityp(settings: Settings, tmp_path: Path) -> None:
    datei = tmp_path / "bestellung.docx"
    datei.write_bytes(b"x")
    with pytest.raises(VisionEingabeFehler, match="nicht unterstützt"):
        VisionExtraktor(settings, client=FakeClient()).extrahiere(datei)
    with pytest.raises(VisionEingabeFehler, match="nicht gefunden"):
        erzeuge_inhaltsbloecke(tmp_path / "fehlt.png")


def test_extrahiere_mit_modell(settings: Settings, scan_png: Path) -> None:
    client = FakeClient(FakeAntwort(ergebnis()))
    roh = VisionExtraktor(settings, client=client).extrahiere_mit_modell(scan_png, "claude-sonnet-5-5")
    assert client.letzter_aufruf["model"] == "claude-sonnet-5-5"
    assert roh.extraktionsmethode == Extraktionsmethode.CLAUDE_VISION


# ---------------------------------------------------------------------------
# Umwandlung in RohBestellung
# ---------------------------------------------------------------------------


def test_umwandlung_in_rohbestellung(settings: Settings, scan_pdf: Path) -> None:
    client = FakeClient(FakeAntwort(ergebnis()))
    extraktor = VisionExtraktor(settings, client=client)
    roh = extraktor.extrahiere(scan_pdf)

    assert roh.eingangstyp == Eingangstyp.PREISLISTE_SCAN
    assert roh.extraktionsmethode == Extraktionsmethode.CLAUDE_VISION
    assert roh.quelldatei == scan_pdf
    assert roh.kunde_text == "Weinmarkt Kirchhellen"
    assert roh.lieferant_text == "Cide srl"
    assert roh.bestellnummer == "B-4712"
    assert roh.datum == date(2026, 10, 15)
    assert roh.liefertag == date(2026, 10, 24)
    assert roh.seiten == 1

    assert [p.laufnr for p in roh.positionen] == [1, 2, 3]
    p1, p2, p3 = roh.positionen
    assert p1.menge == Decimal("12") and isinstance(p1.menge, Decimal)
    assert p1.preis == Decimal("6.90") and isinstance(p1.preis, Decimal)
    assert p1.einheit == "Fl"
    assert p1.volumen_cl == Decimal("75")
    assert p1.konfidenz_menge == pytest.approx(0.95)
    assert p1.konfidenz_preis == pytest.approx(0.99)
    # Zeile ohne Menge bleibt erhalten, ohne Hinweise
    assert p2.menge is None and p2.preis == Decimal("6.90")
    assert not [h for h in roh.hinweise if h.position_laufnr == 2]
    assert p3.jahrgang == 2022 and p3.hinweis == "11+1" and p3.preis == Decimal("8.40")
    assert roh.gesamtmenge == Decimal("18")

    assert len(roh.summenvermerke) == 1
    vermerk = roh.summenvermerke[0]
    assert vermerk.wert == Decimal("18") and vermerk.einheit == "R" and vermerk.rohtext == "18 R"
    summen_hinweise = [h for h in roh.hinweise if h.code == Pruefcode.SUMMENVERMERK]
    assert len(summen_hinweise) == 1
    assert summen_hinweise[0].schweregrad == Schweregrad.INFO
    assert "18 R" in summen_hinweise[0].text

    # Keine Warnungen bei guter Konfidenz, Anmerkungen landen im Rohtext
    assert not [h for h in roh.hinweise if h.code in (Pruefcode.KONFIDENZ_NIEDRIG, Pruefcode.DURCHGESTRICHEN)]
    assert "Randnotiz unleserlich" in (roh.rohtext or "")
    assert extraktor.letztes_ergebnis is not None
    assert extraktor.letzte_nutzung["input_tokens"] == 2345
    assert extraktor.letzte_nutzung["output_tokens"] == 678


def test_hinweise_bei_niedriger_konfidenz_und_durchgestrichen(settings: Settings, scan_png: Path) -> None:
    antwort = ergebnis(
        positionen=[
            position(laufnr=1, menge_text="1?", menge=12.0, konfidenz_menge=0.4),
            position(laufnr=2, menge_text="6", menge=6.0, konfidenz_menge=0.97, durchgestrichen=True),
            position(laufnr=3, menge=None, konfidenz_menge=0.1, durchgestrichen=True),  # ohne Menge: keine Hinweise
            position(laufnr=4, menge_text="3", menge=3.0, konfidenz_menge=0.99, konfidenz_preis=0.3),
        ],
        summenvermerke=[],
    )
    roh = VisionExtraktor(settings, client=FakeClient(FakeAntwort(antwort))).extrahiere(scan_png)

    niedrig = [h for h in roh.hinweise if h.code == Pruefcode.KONFIDENZ_NIEDRIG]
    assert [h.position_laufnr for h in niedrig] == [1, 4]
    assert all(h.schweregrad == Schweregrad.WARNUNG for h in niedrig)
    assert "1?" in niedrig[0].text and "40 %" in niedrig[0].text
    assert niedrig[1].details["feld"] == "preis"

    durch = [h for h in roh.hinweise if h.code == Pruefcode.DURCHGESTRICHEN]
    assert [h.position_laufnr for h in durch] == [2]
    assert durch[0].schweregrad == Schweregrad.WARNUNG
    assert roh.positionen[1].durchgestrichen is True
    assert roh.positionen[1].menge == Decimal("6")  # Position bleibt erhalten ...
    assert roh.gesamtmenge == Decimal("15")  # ... zählt aber nicht zur Gesamtmenge

    assert roh.positionen[2].menge is None and roh.positionen[2].durchgestrichen is True
    assert not [h for h in roh.hinweise if h.position_laufnr == 3]


def test_konfidenz_schwelle_aus_settings(settings: Settings, scan_png: Path) -> None:
    settings.matching.konfidenz_schwelle = 0.5
    antwort = ergebnis(positionen=[position(laufnr=1, menge_text="12", menge=12.0, konfidenz_menge=0.6)])
    roh = VisionExtraktor(settings, client=FakeClient(FakeAntwort(antwort))).extrahiere(scan_png)
    assert not [h for h in roh.hinweise if h.code == Pruefcode.KONFIDENZ_NIEDRIG]
    settings.matching.konfidenz_schwelle = 0.8
    roh = VisionExtraktor(settings, client=FakeClient(FakeAntwort(antwort))).extrahiere(scan_png)
    assert len([h for h in roh.hinweise if h.code == Pruefcode.KONFIDENZ_NIEDRIG]) == 1


def test_umwandlung_randfaelle(tmp_path: Path) -> None:
    antwort = ergebnis(
        kunde_text=None,
        lieferant_text="  ",
        datum="irgendwann",
        liefertag=None,
        positionen=[
            position(laufnr=7, menge_text="12", menge=None),  # Menge nur als Text -> nachparsen
            position(laufnr=7, bezeichnung="", rohtext="Gavi di Gavi 2023 0,75 l 12,00", preis=12.0, jahrgang=None),
            position(laufnr=9, bezeichnung="", rohtext=None),  # wird übersprungen
            position(laufnr=10, menge=-3.0, menge_text="-3", preis=None, konfidenz_preis=None),
        ],
        summenvermerke=[VisionSummenvermerk(wert=float("nan"), einheit=None, rohtext="??", konfidenz=0.2)],
        seitenzahl=None,
        anmerkungen=[],
    )
    roh = wandle_in_rohbestellung(antwort, tmp_path / "scan.png", 0.8)

    assert [p.laufnr for p in roh.positionen] == [1, 2, 4]  # doppelte Laufnummern -> neu vergeben, Pos. 9 fehlt
    assert roh.positionen[0].menge == Decimal("12")
    assert roh.positionen[1].bezeichnung == "Gavi di Gavi 2023 0,75 l 12,00"
    assert roh.positionen[1].jahrgang == 2023 and roh.positionen[1].volumen_cl == Decimal("75")
    assert roh.positionen[2].menge is None and roh.positionen[2].preis is None
    assert roh.summenvermerke == []
    assert roh.kunde_text is None and roh.lieferant_text is None
    assert roh.datum is None and roh.seiten is None

    fehlend = [h for h in roh.hinweise if h.code == Pruefcode.FELD_FEHLT]
    felder = {h.details.get("feld") for h in fehlend}
    assert {"kunde", "lieferant", "datum"} <= felder
    assert all(h.schweregrad == Schweregrad.WARNUNG for h in fehlend if h.details.get("feld") == "datum")


def test_keine_positionen_blockiert(tmp_path: Path) -> None:
    roh = wandle_in_rohbestellung(ergebnis(positionen=[], summenvermerke=[]), tmp_path / "leer.png", 0.8)
    assert roh.positionen == []
    assert any(h.code == Pruefcode.FELD_FEHLT and h.schweregrad == Schweregrad.FEHLER for h in roh.hinweise)


def test_keine_mengen_warnung(tmp_path: Path) -> None:
    roh = wandle_in_rohbestellung(
        ergebnis(positionen=[position(laufnr=1), position(laufnr=2, menge=0.0, menge_text="0")], summenvermerke=[]),
        tmp_path / "leer.png",
        0.8,
    )
    assert len(roh.positionen) == 2
    assert roh.positionen[1].menge == Decimal("0")
    assert not [h for h in roh.hinweise if h.position_laufnr is not None]
    assert any(h.code == Pruefcode.FELD_FEHLT and h.details.get("feld") == "menge" for h in roh.hinweise)


# ---------------------------------------------------------------------------
# stop_reason / Fehlerbehandlung
# ---------------------------------------------------------------------------


def test_refusal_wird_zur_exception(settings: Settings, scan_png: Path) -> None:
    antwort = FakeAntwort(
        parsed_output=None,
        stop_reason="refusal",
        stop_details=FakeStopDetails(category="general_harms", explanation="Testbegründung"),
    )
    with pytest.raises(VisionAbgelehnt) as info:
        VisionExtraktor(settings, client=FakeClient(antwort)).extrahiere(scan_png)
    assert info.value.kategorie == "general_harms"
    assert info.value.erklaerung == "Testbegründung"
    assert "general_harms" in str(info.value) and "Testbegründung" in str(info.value)
    assert isinstance(info.value, VisionFehler)


def test_max_tokens_wird_zur_exception(settings: Settings, scan_png: Path) -> None:
    antwort = FakeAntwort(parsed_output=None, stop_reason="max_tokens")
    with pytest.raises(VisionAbgeschnitten, match="max_tokens"):
        VisionExtraktor(settings, client=FakeClient(antwort)).extrahiere(scan_png)


def test_leere_strukturierte_ausgabe(settings: Settings, scan_png: Path) -> None:
    antwort = FakeAntwort(parsed_output=None, stop_reason="end_turn")
    with pytest.raises(VisionFehler, match="keine strukturierte Ausgabe"):
        VisionExtraktor(settings, client=FakeClient(antwort)).extrahiere(scan_png)


def _http_antwort(status: int, kopf: dict[str, str] | None = None) -> httpx2.Response:
    anfrage = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return httpx2.Response(status, request=anfrage, headers=kopf or {})


@pytest.mark.parametrize(
    "fehler, erwartet",
    [
        (anthropic.AuthenticationError("invalid x-api-key", response=_http_antwort(401), body=None), "Anmeldung"),
        (
            anthropic.RateLimitError("rate limited", response=_http_antwort(429, {"retry-after": "7"}), body=None),
            "Ratenlimit",
        ),
        (anthropic.InternalServerError("overloaded", response=_http_antwort(529), body=None), "529"),
        (anthropic.BadRequestError("bad request", response=_http_antwort(400), body=None), "400"),
        (anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.anthropic.com")), "nicht erreichbar"),
        (anthropic.APITimeoutError(request=httpx2.Request("POST", "https://api.anthropic.com")), "nicht erreichbar"),
    ],
)
def test_api_fehler_deutsche_meldungen(settings: Settings, scan_png: Path, fehler: Exception, erwartet: str) -> None:
    with pytest.raises(VisionApiFehler, match=erwartet) as info:
        VisionExtraktor(settings, client=FakeClient(fehler=fehler)).extrahiere(scan_png)
    assert info.value.__cause__ is fehler
    if isinstance(fehler, anthropic.RateLimitError):
        assert "7 s" in str(info.value)


# ---------------------------------------------------------------------------
# Client-Erzeugung
# ---------------------------------------------------------------------------


class FakeAnthropic:
    aufrufe: list[dict[str, Any]] = []

    def __init__(self, **kwargs: Any) -> None:
        FakeAnthropic.aufrufe.append(kwargs)
        self.messages = FakeMessages(FakeAntwort(ergebnis()))


def test_client_standard_umgebungsvariable(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(anthropic, "Anthropic", FakeAnthropic)
    FakeAnthropic.aufrufe.clear()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    erzeuge_client(settings)
    assert FakeAnthropic.aufrufe == [{}]  # ohne Key-Argument, das SDK liest ANTHROPIC_API_KEY selbst


def test_client_abweichende_umgebungsvariable(settings: Settings, monkeypatch: pytest.MonkeyPatch, scan_png: Path) -> None:
    monkeypatch.setattr(anthropic, "Anthropic", FakeAnthropic)
    FakeAnthropic.aufrufe.clear()
    settings.claude.api_key_env = "WEINSTRASSE_CLAUDE_KEY"
    monkeypatch.delenv("WEINSTRASSE_CLAUDE_KEY", raising=False)
    extraktor = VisionExtraktor(settings)  # ohne Client: darf noch nicht scheitern
    assert extraktor.kann_verarbeiten(scan_png) is True
    with pytest.raises(VisionApiFehler, match="WEINSTRASSE_CLAUDE_KEY"):
        extraktor.extrahiere(scan_png)

    monkeypatch.setenv("WEINSTRASSE_CLAUDE_KEY", "sk-anders")
    roh = VisionExtraktor(settings).extrahiere(scan_png)
    assert FakeAnthropic.aufrufe == [{"api_key": "sk-anders"}]
    assert len(roh.positionen) == 3


# ---------------------------------------------------------------------------
# Durchstich mit echtem SDK-Client (MockTransport, kein Netz)
# ---------------------------------------------------------------------------


def test_echter_sdk_client_mit_mock_transport(settings: Settings, scan_pdf: Path) -> None:
    """Prüft Schema-Transformation, Request-Body und parsed_output des echten SDK."""
    gesendet: list[dict[str, Any]] = []
    antwort_json = ergebnis().model_dump(mode="json")

    def handler(anfrage: httpx2.Request) -> httpx2.Response:
        gesendet.append(json.loads(anfrage.content))
        body = {
            "id": "msg_mock",
            "type": "message",
            "role": "assistant",
            "model": "claude-opus-5-5",
            "content": [{"type": "text", "text": json.dumps(antwort_json)}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 4321, "output_tokens": 987},
        }
        return httpx2.Response(200, json=body, headers={"request-id": "req_mock"})

    client = anthropic.Anthropic(
        api_key="sk-test",
        max_retries=0,
        http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)),
    )
    extraktor = VisionExtraktor(settings, client=client)
    roh = extraktor.extrahiere(scan_pdf)

    assert len(gesendet) == 1
    body = gesendet[0]
    assert body["model"] == "claude-opus-5-5"
    assert body["max_tokens"] == settings.claude.max_tokens
    assert body["output_config"]["effort"] == settings.claude.effort
    schema = body["output_config"]["format"]
    assert schema["type"] == "json_schema"
    assert schema["schema"]["additionalProperties"] is False
    assert "positionen" in schema["schema"]["properties"]
    assert "thinking" not in body and "temperature" not in body and "tool_choice" not in body
    inhalt = body["messages"][0]["content"]
    assert inhalt[0]["type"] == "document" and inhalt[1]["type"] == "text"

    assert len(roh.positionen) == 3
    assert roh.positionen[0].menge == Decimal("12")
    assert extraktor.letzte_nutzung["input_tokens"] == 4321
    assert extraktor.letzte_nutzung["request_id"] == "req_mock"


def test_modul_exportiert_prompts() -> None:
    assert "handschriftlich" in vision.SYSTEM_PROMPT
    assert "JEDE Zeile" in vision.SYSTEM_PROMPT
    assert "durchgestrichen" in vision.SYSTEM_PROMPT
    assert "Nichts erfinden" in vision.SYSTEM_PROMPT
