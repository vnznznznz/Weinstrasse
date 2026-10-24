"""Tests für rabatte.rabattliste_pdf: Text-PDF (Tabelle + Zeilen-Regex), Claude Vision mit
Fake-Client, Verhalten ohne API-Schlüssel und das YAML-Gedächtnis. Keine echten API-Aufrufe."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml

from auftragsautomat.config import Settings
from auftragsautomat.models import Aktion, Extraktionsmethode, Pruefcode, Schweregrad
from auftragsautomat.rabatte.rabattliste_pdf import (
    RabattlisteFehler,
    RabattlisteVision,
    RabattZeileVision,
    aktionen_aus_text,
    lade_gespeicherte_aktionen,
    lese_rabattliste,
    speichere_aktionen,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def rabattliste_pdf(fixtures_ordner: Path) -> Path:
    pfad = fixtures_ordner / "pdfs" / "rabattliste_beispiel.pdf"
    assert pfad.is_file(), "Beispiel-PDFs fehlen: python scripts/beispiel_pdfs_erzeugen.py"
    return pfad


@pytest.fixture()
def scan_pdf(fixtures_ordner: Path) -> Path:
    pfad = fixtures_ordner / "pdfs" / "scan_preisliste.pdf"
    assert pfad.is_file(), "Beispiel-PDFs fehlen: python scripts/beispiel_pdfs_erzeugen.py"
    return pfad


@pytest.fixture()
def bestellung_pdf(fixtures_ordner: Path) -> Path:
    return fixtures_ordner / "pdfs" / "kundenbestellung_kirchhellen.pdf"


@pytest.fixture()
def ohne_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)


def _text_pdf(ziel: Path, zeilen: list[str]) -> Path:
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(str(ziel), pagesize=(595, 842), invariant=1)
    y = 780
    for zeile in zeilen:
        c.drawString(60, y, zeile)
        y -= 18
    c.showPage()
    c.save()
    return ziel


@dataclass
class FakeStopDetails:
    category: str | None = None
    explanation: str | None = None


@dataclass
class FakeAntwort:
    parsed_output: Any
    stop_reason: str = "end_turn"
    stop_details: FakeStopDetails | None = None
    usage: Any = None
    model: str = "claude-opus-5-5"


@dataclass
class FakeMessages:
    antwort: Any = None
    fehler: Exception | None = None
    aufrufe: list[dict[str, Any]] = field(default_factory=list)

    def parse(self, **kwargs: Any) -> Any:
        self.aufrufe.append(kwargs)
        if self.fehler is not None:
            raise self.fehler
        return self.antwort


class FakeClient:
    def __init__(self, antwort: Any = None, fehler: Exception | None = None) -> None:
        self.messages = FakeMessages(antwort, fehler)


def _zeile(**werte: Any) -> RabattZeileVision:
    basis: dict[str, Any] = {
        "laufnr": 1,
        "seite": 1,
        "kunde": None,
        "lieferant": "Cantine Colosi",
        "artikel": "Nero d'Avola Sicilia DOC 2022",
        "artikelnummer": None,
        "von": "01.11.2026",
        "bis": "30.11.2026",
        "rabatt_prozent": 10.0,
        "rabatt_eur": None,
        "aktionspreis": None,
        "bezeichnung": None,
        "bemerkung": None,
        "konfidenz": 0.95,
        "rohtext": "Nero d'Avola 2022 01.11.-30.11.2026 -10 %",
    }
    basis.update(werte)
    return RabattZeileVision(**basis)


def _vision(**werte: Any) -> RabattlisteVision:
    basis: dict[str, Any] = {
        "titel": "Rabattliste",
        "kunde": None,
        "lieferant": None,
        "zeitraum_von": None,
        "zeitraum_bis": None,
        "zeilen": [_zeile()],
        "anmerkungen": [],
    }
    basis.update(werte)
    return RabattlisteVision(**basis)


def _antwort(**werte: Any) -> FakeAntwort:
    """Antwortobjekt wie vom SDK: die strukturierte Ausgabe steht in ``parsed_output``."""
    return FakeAntwort(parsed_output=_vision(**werte))


def _aktion(aktion_id: str, **felder: Any) -> Aktion:
    basis: dict[str, Any] = {
        "aktion_id": aktion_id,
        "von": date(2026, 11, 1),
        "bis": date(2026, 11, 30),
        "rabatt_prozent": Decimal("10"),
        "quelle": "pdf:liste.pdf:1",
    }
    basis.update(felder)
    return Aktion(**basis)


# ---------------------------------------------------------------------------
# Text-PDF
# ---------------------------------------------------------------------------


def test_fixture_entspricht_dem_skript(rabattliste_pdf: Path, tmp_path: Path):
    """Die eingecheckte Rabattliste ist byte-identisch mit dem Ergebnis des Beispielskripts."""
    import importlib.util

    skript_pfad = Path(__file__).resolve().parents[1] / "scripts" / "beispiel_pdfs_erzeugen.py"
    spec = importlib.util.spec_from_file_location("beispiel_pdfs_erzeugen", skript_pfad)
    assert spec is not None and spec.loader is not None
    skript = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(skript)
    pfade = skript.erzeuge_alle(tmp_path, mit_rabattliste=True)
    assert pfade[-1].name == "rabattliste_beispiel.pdf"
    assert pfade[-1].read_bytes() == rabattliste_pdf.read_bytes()
    assert len(skript.erzeuge_alle(tmp_path / "ohne")) == 8  # Standard bleibt bei den acht Belegen


def test_fixture_tabelle_und_freitext(rabattliste_pdf: Path, settings: Settings):
    liste = lese_rabattliste(rabattliste_pdf, settings)
    assert liste.extraktionsmethode == Extraktionsmethode.PDFPLUMBER
    assert liste.quelldatei == rabattliste_pdf
    assert liste.eingelesen_am is not None
    assert liste.rohtext and "Rabattliste Herbst 2026" in liste.rohtext
    assert not any(h.schweregrad == Schweregrad.FEHLER for h in liste.hinweise)
    assert [a.aktion_id for a in liste.aktionen] == [
        "pdf:rabattliste_beispiel.pdf:1:1",
        "pdf:rabattliste_beispiel.pdf:1:2",
        "pdf:rabattliste_beispiel.pdf:1:3",
        "pdf:rabattliste_beispiel.pdf:1:4",
    ]
    assert {a.quelle for a in liste.aktionen} == {"pdf:rabattliste_beispiel.pdf:1"}

    a1, a2, a3, a4 = liste.aktionen
    # Tabellenzeilen (Zellen mit Zeilenumbruch im Datum werden richtig gelesen)
    assert (a1.kunde_kurz, a1.lieferant_kurz) == ("Vinothek Beispiel", "Weingut Beispiel")
    assert a1.artikel_text == "Riesling trocken 2023 0,75 l"
    assert a1.artikelnummern == ["4001"]
    assert (a1.von, a1.bis, a1.rabatt_prozent) == (date(2026, 11, 1), date(2026, 11, 30), Decimal("10"))
    assert a1.konfidenz == 0.9 and a1.bezug == settings.rabatte.bezug
    assert (a2.kunde_kurz, a2.lieferant_kurz, a2.artikelnummern, a2.rabatt_prozent) == (
        "Fachhandel",
        "Cantine Colosi",
        ["2001"],
        Decimal("5"),
    )
    assert (a2.von, a2.bis) == (date(2026, 11, 1), date(2026, 11, 15))
    # leere Kundenzelle -> alle Kunden; Aktionspreis statt Prozent
    assert a3.kunde_kurz is None and a3.kundennummern == []
    assert (a3.lieferant_kurz, a3.rabatt_prozent, a3.aktionspreis) == ("Bollina", None, Decimal("11.50"))
    assert (a3.von, a3.bis) == (date(2026, 10, 15), date(2026, 12, 31))
    # Freitextzeile außerhalb der Tabelle (Zeilen-Regex)
    assert a4.konfidenz == 0.7
    assert (a4.von, a4.bis, a4.rabatt_prozent) == (date(2026, 12, 1), date(2026, 12, 24), Decimal("8"))
    assert a4.artikel_text == "Villa Armellina Gloria Secco Frizzante 0,75 l"
    assert a4.lieferant_kurz == "Weingut Beispiel"  # Seitenkopf "Lieferant: …"


def test_zeilen_regex_mit_seitenvorgaben(settings: Settings):
    zeilen = [
        "Rabattliste Winter",
        "Kunde: Vinothek Beispiel",
        "Lieferant: Weingut Beispiel",
        "Aktionszeitraum 01.11.–30.11.2026",
        "Riesling trocken 2023 Art.-Nr. 4001 Rabatt 10 %",
        "Gavi di Gavi 2023 vom 1.12. bis 24.12.2026 Aktionspreis 11,50 €",
        "Nero d'Avola 2022 1,00 € Rabatt",
        "Preise zzgl. 19 % MwSt.",
        "Spätburgunder 2022 -5 %, Aktion 15.10.2026 - 31.10.2026",
    ]
    aktionen, hinweise, vorgaben = aktionen_aus_text(zeilen, quelle="pdf:liste.pdf:1", settings=settings)
    assert hinweise == []
    assert vorgaben["kunde_kurz"] == "Vinothek Beispiel"
    assert vorgaben["lieferant_kurz"] == "Weingut Beispiel"
    assert (vorgaben["von"], vorgaben["bis"]) == (date(2026, 11, 1), date(2026, 11, 30))
    assert [a.aktion_id for a in aktionen] == ["pdf:liste.pdf:1:1", "pdf:liste.pdf:1:2", "pdf:liste.pdf:1:3", "pdf:liste.pdf:1:4"]
    riesling, gavi, nero, spaet = aktionen
    assert (riesling.artikelnummern, riesling.rabatt_prozent) == (["4001"], Decimal("10"))
    assert riesling.artikel_text == "Riesling trocken 2023"
    assert (riesling.von, riesling.bis) == (date(2026, 11, 1), date(2026, 11, 30))  # aus der Vorgabe
    assert (riesling.kunde_kurz, riesling.lieferant_kurz) == ("Vinothek Beispiel", "Weingut Beispiel")
    assert (gavi.von, gavi.bis) == (date(2026, 12, 1), date(2026, 12, 24))  # "1.12." ohne Jahr
    assert gavi.aktionspreis == Decimal("11.50") and gavi.rabatt_prozent is None
    assert gavi.artikel_text == "Gavi di Gavi 2023"
    assert nero.rabatt_eur == Decimal("1.00") and nero.artikel_text == "Nero d'Avola 2022"
    assert (spaet.rabatt_prozent, spaet.von, spaet.bis) == (Decimal("5"), date(2026, 10, 15), date(2026, 10, 31))
    assert all(a.konfidenz == 0.7 for a in aktionen)


def test_text_pdf_nur_zeilen(tmp_path: Path, settings: Settings):
    pdf = _text_pdf(
        tmp_path / "liste.pdf",
        [
            "Aktionen Fachhandel Weingut Beispiel",
            "Kunde: Vinothek Beispiel",
            "Riesling trocken 2023 01.11.2026 - 30.11.2026 Rabatt 10 %",
            "Spaetburgunder 2022 01.11.2026 - 30.11.2026 Aktionspreis 7,90 EUR",
        ],
    )
    liste = lese_rabattliste(pdf, settings, client=FakeClient(fehler=AssertionError("Vision darf nicht laufen")))
    assert liste.extraktionsmethode == Extraktionsmethode.PDFPLUMBER
    assert [a.artikel_text for a in liste.aktionen] == ["Riesling trocken 2023", "Spaetburgunder 2022"]
    assert liste.aktionen[0].kunde_kurz == "Vinothek Beispiel"
    assert liste.aktionen[1].aktionspreis == Decimal("7.90")


def test_text_pdf_ohne_treffer_faellt_auf_vision_zurueck(bestellung_pdf: Path, settings: Settings):
    client = FakeClient(_antwort(zeilen=[_zeile(konfidenz=0.9)]))
    liste = lese_rabattliste(bestellung_pdf, settings, client=client)
    assert len(client.messages.aufrufe) == 1
    assert liste.extraktionsmethode == Extraktionsmethode.CLAUDE_VISION
    assert len(liste.aktionen) == 1
    assert liste.aktionen[0].aktion_id == "pdf:kundenbestellung_kirchhellen.pdf:1:1"


def test_datei_fehlt(tmp_path: Path, settings: Settings):
    with pytest.raises(FileNotFoundError):
        lese_rabattliste(tmp_path / "fehlt.pdf", settings)


def test_kaputte_datei(tmp_path: Path, settings: Settings):
    kaputt = tmp_path / "kaputt.pdf"
    kaputt.write_bytes(b"%PDF-1.4 das ist kein PDF")
    with pytest.raises(RabattlisteFehler):
        lese_rabattliste(kaputt, settings, vision_erzwingen=False)


# ---------------------------------------------------------------------------
# Claude Vision
# ---------------------------------------------------------------------------


def test_vision_scan_mit_fake_client(scan_pdf: Path, settings: Settings):
    antwort = _antwort(
        lieferant="Cantine Colosi",
        zeitraum_von="01.11.2026",
        zeitraum_bis="30.11.2026",
        zeilen=[
            _zeile(laufnr=1, von=None, bis=None, konfidenz=0.95),
            _zeile(laufnr=2, artikel="Grillo Sicilia DOC 2023", artikelnummer="2003", rabatt_prozent=None, aktionspreis=7.9, konfidenz=0.4, rohtext="Grillo 2023 AP 7,90"),
            _zeile(laufnr=3, artikel="Salina Bianco", von="kein Datum", bis=None, konfidenz=0.9),
            _zeile(laufnr=4, seite=2, kunde="Fachhandel", artikel="Nerello Mascalese 2022", rabatt_prozent=None, rabatt_eur=0.5, konfidenz=0.85),
        ],
        anmerkungen=["Rand unleserlich"],
    )
    client = FakeClient(antwort)
    liste = lese_rabattliste(scan_pdf, settings, client=client)

    anfrage = client.messages.aufrufe[0]
    assert anfrage["output_format"] is RabattlisteVision
    assert anfrage["model"] == settings.claude.modell
    assert anfrage["output_config"] == {"effort": settings.claude.effort}
    assert anfrage["messages"][0]["content"][0]["type"] == "document"
    assert "thinking" not in anfrage and "temperature" not in anfrage

    assert liste.extraktionsmethode == Extraktionsmethode.CLAUDE_VISION
    assert liste.rohtext and "Anmerkungen: Rand unleserlich" in liste.rohtext
    assert [a.aktion_id for a in liste.aktionen] == [
        "pdf:scan_preisliste.pdf:1:1",
        "pdf:scan_preisliste.pdf:1:2",
        "pdf:scan_preisliste.pdf:2:1",
    ]
    a1, a2, a4 = liste.aktionen
    assert (a1.von, a1.bis) == (date(2026, 11, 1), date(2026, 11, 30))  # Zeitraum aus dem Listenkopf
    assert a1.lieferant_kurz == "Cantine Colosi" and a1.konfidenz == 0.95
    assert a2.aktionspreis == Decimal("7.9") and a2.artikelnummern == ["2003"] and a2.konfidenz == 0.4
    assert (a4.kunde_kurz, a4.rabatt_eur) == ("Fachhandel", Decimal("0.5"))
    codes = [(h.code, h.schweregrad) for h in liste.hinweise]
    assert (Pruefcode.KONFIDENZ_NIEDRIG, Schweregrad.WARNUNG) in codes
    niedrig = [h for h in liste.hinweise if h.code == Pruefcode.KONFIDENZ_NIEDRIG]
    assert len(niedrig) == 1 and niedrig[0].details["aktion_id"] == "pdf:scan_preisliste.pdf:1:2"
    assert any(h.code == Pruefcode.FELD_FEHLT and "Von" in h.text for h in liste.hinweise)  # Zeile 3 übersprungen


def test_vision_ohne_api_key_gibt_hinweis_statt_traceback(scan_pdf: Path, settings: Settings, ohne_api_key: None):
    liste = lese_rabattliste(scan_pdf, settings)
    assert liste.aktionen == []
    assert liste.extraktionsmethode == Extraktionsmethode.PDFPLUMBER
    fehler = [h for h in liste.hinweise if h.code == Pruefcode.FELD_FEHLT and h.schweregrad == Schweregrad.FEHLER]
    assert fehler and "ANTHROPIC_API_KEY" in fehler[0].text
    assert fehler[0].details["feld"] == "vision"


def test_vision_ablehnung_und_abschneiden(scan_pdf: Path, settings: Settings):
    abgelehnt = FakeClient(FakeAntwort(parsed_output=None, stop_reason="refusal", stop_details=FakeStopDetails("x", "Grund")))
    liste = lese_rabattliste(scan_pdf, settings, client=abgelehnt)
    assert liste.aktionen == [] and any("abgelehnt" in h.text and "Grund" in h.text for h in liste.hinweise)

    abgeschnitten = FakeClient(FakeAntwort(parsed_output=None, stop_reason="max_tokens"))
    liste = lese_rabattliste(scan_pdf, settings, client=abgeschnitten)
    assert liste.aktionen == [] and any("abgeschnitten" in h.text for h in liste.hinweise)

    ohne_ausgabe = FakeClient(FakeAntwort(parsed_output=None))
    liste = lese_rabattliste(scan_pdf, settings, client=ohne_ausgabe)
    assert liste.aktionen == [] and any("keine strukturierte Ausgabe" in h.text for h in liste.hinweise)


def test_vision_api_fehler_als_hinweis(scan_pdf: Path, settings: Settings):
    import anthropic
    import httpx2

    anfrage = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    fehler = anthropic.AuthenticationError(
        "ungültiger Schlüssel", response=httpx2.Response(401, request=anfrage), body=None
    )
    liste = lese_rabattliste(scan_pdf, settings, client=FakeClient(fehler=fehler))
    assert liste.aktionen == []
    assert any("Anmeldung fehlgeschlagen" in h.text for h in liste.hinweise)

    liste = lese_rabattliste(scan_pdf, settings, client=FakeClient(fehler=TypeError("api_key fehlt")))
    assert any("Anmeldung nicht möglich" in h.text for h in liste.hinweise)


def test_vision_erzwingen_bei_text_pdf(rabattliste_pdf: Path, settings: Settings):
    client = FakeClient(_antwort())
    liste = lese_rabattliste(rabattliste_pdf, settings, client=client, vision_erzwingen=True)
    assert len(client.messages.aufrufe) == 1
    assert liste.extraktionsmethode == Extraktionsmethode.CLAUDE_VISION
    assert len(liste.aktionen) == 1


# ---------------------------------------------------------------------------
# Gedächtnis (YAML)
# ---------------------------------------------------------------------------


def test_speichern_und_laden_roundtrip(tmp_path: Path):
    datei = tmp_path / "config" / "aktionen.yaml"
    aktionen = [
        _aktion("pdf:liste.pdf:1:1", kunde_kurz="Vinothek Beispiel", artikelnummern=["4001"], rabatt_prozent=Decimal("10.50"), konfidenz=0.9, bemerkung="nur Paletten"),
        _aktion("pdf:liste.pdf:1:2", rabatt_prozent=None, aktionspreis=Decimal("11.50"), aktiv=False, bezug="auftragsdatum"),
        _aktion("pdf:liste.pdf:1:3", rabatt_prozent=None, rabatt_eur=Decimal("1.00"), firma_id="wik_leh", kundennummern=["3441"]),
    ]
    assert speichere_aktionen(aktionen, datei) == datei
    text = datei.read_text(encoding="utf-8")
    assert text.startswith("# Aktionen aus Rabattlisten-PDFs")
    roh = yaml.safe_load(text)
    erste = roh["aktionen"][0]
    assert erste["rabatt_prozent"] == "10.50"  # Decimal als Text
    assert erste["von"] == "2026-11-01" and erste["bis"] == "2026-11-30"  # ISO
    assert "rabatt_eur" not in erste and "aktiv" not in erste
    assert roh["aktionen"][1]["aktiv"] is False and roh["aktionen"][1]["aktionspreis"] == "11.50"

    geladen = lade_gespeicherte_aktionen(datei)
    assert geladen == aktionen
    assert geladen[0].rabatt_prozent == Decimal("10.50")
    assert geladen[1].bezug == "auftragsdatum" and geladen[1].aktiv is False
    assert geladen[2].kundennummern == ["3441"] and geladen[2].rabatt_eur == Decimal("1.00")


def test_speichern_fuehrt_zusammen_und_ersetzt(tmp_path: Path):
    datei = tmp_path / "aktionen.yaml"
    speichere_aktionen([_aktion("a"), _aktion("b")], datei)
    speichere_aktionen([_aktion("b", rabatt_prozent=Decimal("7")), _aktion("c")], datei)
    geladen = lade_gespeicherte_aktionen(datei)
    assert [a.aktion_id for a in geladen] == ["a", "b", "c"]
    assert geladen[1].rabatt_prozent == Decimal("7")
    speichere_aktionen([_aktion("d")], datei, ersetzen=True)
    assert [a.aktion_id for a in lade_gespeicherte_aktionen(datei)] == ["d"]
    assert not list(tmp_path.glob("*.tmp"))


def test_laden_fehlende_und_kaputte_datei(tmp_path: Path):
    assert lade_gespeicherte_aktionen(tmp_path / "fehlt.yaml") == []
    leer = tmp_path / "leer.yaml"
    leer.write_text("", encoding="utf-8")
    assert lade_gespeicherte_aktionen(leer) == []
    kaputt = tmp_path / "kaputt.yaml"
    kaputt.write_text("aktionen: [unvollständig", encoding="utf-8")
    with pytest.raises(RabattlisteFehler):
        lade_gespeicherte_aktionen(kaputt)
    liste = tmp_path / "liste.yaml"
    liste.write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(RabattlisteFehler):
        lade_gespeicherte_aktionen(liste)


def test_laden_ueberspringt_ungueltige_eintraege(tmp_path: Path):
    datei = tmp_path / "aktionen.yaml"
    datei.write_text(
        "aktionen:\n"
        "  - aktion_id: ok\n    von: 2026-11-01\n    bis: 2026-11-30\n    rabatt_prozent: '10,5'\n    quelle: pdf:x:1\n"
        "  - aktion_id: ohne_datum\n    quelle: pdf:x:1\n"
        "  - nur ein Text\n"
        "  - aktion_id: zahl\n    von: 2026-11-01\n    bis: 2026-11-30\n    rabatt_prozent: 7.5\n    quelle: pdf:x:1\n",
        encoding="utf-8",
    )
    geladen = lade_gespeicherte_aktionen(datei)
    assert [a.aktion_id for a in geladen] == ["ok", "zahl"]
    assert geladen[0].rabatt_prozent == Decimal("10.5")
    assert geladen[1].rabatt_prozent == Decimal("7.5")
