"""Tests für tracking/basis.py: Konfiguration, Status-Logik, Datums- und Referenzhelfer."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest
import yaml

from auftragsautomat.models import Meilenstein, Sendung, SendungStatus
from auftragsautomat.tracking.basis import (
    STATUS_RANG,
    HillebrandKonfig,
    KonfigFehler,
    TrackingQuelle,
    finde_datum_tolerant,
    ist_bekannt,
    ist_fortschritt,
    lade_hillebrand_konfig,
    mit_hinweis,
    normalisiere_referenz,
    parse_datum_mit_bezug,
    status_aus_meilensteinen,
    status_mit_puenktlichkeit,
    vereinige_meilensteine,
)

REPO_KONFIG = Path(__file__).resolve().parents[1] / "config" / "tracking_hillebrand.yaml"


@pytest.fixture()
def konfig() -> HillebrandKonfig:
    return lade_hillebrand_konfig(REPO_KONFIG)


# ---------------------------------------------------------------- Konfiguration


def test_konfig_laden_aus_repo(konfig: HillebrandKonfig) -> None:
    assert konfig.spedition == "Hillebrand"
    assert konfig.sprache == "en"
    assert "hillebrandgori.com" in konfig.absender_domains
    assert konfig.api.basis_url.startswith("https://api.hillebrandgori.com")
    assert konfig.api.token_url.startswith("https://login.hillebrand.com/oauth2/")
    assert konfig.api.scope == "offline_access"
    assert konfig.api.limit_pro_minute == 100
    assert konfig.api.pfad_ereignisse.format(id=7) == "/v6/shipments/7/events"
    assert {m.code for m in konfig.meilenstein_muster} >= {"shprdy", "shpsupdeppln", "shpsupdepcnf", "shpcusarrpln", "shpcusarrcnf"}
    assert konfig.status_mapping["delivered"] == SendungStatus.ZUGESTELLT
    assert konfig.on_time_mapping == {"onTime": True, "late": False}
    assert konfig.public_url("DEMB 00000") == "https://my.hillebrandgori.com/public-tracking/DEMB%2000000"
    assert konfig.selektoren.public.timeline
    assert konfig.selektoren.portal.warten_auf


def test_konfig_keine_postfaecher_fest_kodiert() -> None:
    text = REPO_KONFIG.read_text(encoding="utf-8")
    assert "@hillebrandgori.com" not in text
    assert "@hillebrand.com" not in text


def test_konfig_fallback_auf_standard(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("INFO"):
        konfig = lade_hillebrand_konfig(tmp_path / "gibt_es_nicht.yaml")
    assert konfig.spedition == "Hillebrand"
    assert "verwende" in caplog.text


def test_konfig_aus_settings(settings) -> None:
    konfig = lade_hillebrand_konfig(settings.tracking.hillebrand_konfig)
    assert konfig.absender_domains


def test_konfig_ungueltiges_regex(tmp_path: Path) -> None:
    datei = tmp_path / "tracking.yaml"
    datei.write_text(
        yaml.safe_dump({"betreff_regex": {"bestaetigung": "Our Ref: ("}, "referenz_regex": "x"}), encoding="utf-8"
    )
    with pytest.raises(KonfigFehler):
        lade_hillebrand_konfig(datei)


def test_konfig_muss_mapping_sein(tmp_path: Path) -> None:
    datei = tmp_path / "tracking.yaml"
    datei.write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(KonfigFehler):
        lade_hillebrand_konfig(datei)


def test_konfig_minimal_gueltig(tmp_path: Path) -> None:
    datei = tmp_path / "tracking.yaml"
    datei.write_text(
        yaml.safe_dump(
            {
                "betreff_regex": {"bestaetigung": r"Our Ref: (?P<referenz>DEM\d+)"},
                "referenz_regex": r"(?P<referenz>DEM\d+)",
                "absender_domains": ["@Example.ORG", " "],
                "login_hosts": ["Login.Example.org"],
            }
        ),
        encoding="utf-8",
    )
    konfig = lade_hillebrand_konfig(datei)
    assert konfig.absender_domains == ["example.org"]
    assert konfig.login_hosts == ["login.example.org"]
    assert konfig.selektoren.public.timeline is None


# ---------------------------------------------------------------- Hilfen der Konfiguration


def test_absender_bekannt(konfig: HillebrandKonfig) -> None:
    assert konfig.ist_absender_bekannt("dispo.beispiel@hillebrandgori.com")
    assert konfig.ist_absender_bekannt("Noreply <x@mail.hillebrandgori.com>")
    assert not konfig.ist_absender_bekannt("kunde@example.org")
    assert not konfig.ist_absender_bekannt("fake@hillebrandgori.com.example.org")
    assert not konfig.ist_absender_bekannt(None)
    assert not konfig.ist_absender_bekannt("ohne-at")


def test_login_host(konfig: HillebrandKonfig) -> None:
    assert konfig.ist_login_host("https://login.hillebrand.com/oauth2/x/v1/authorize?client_id=1")
    assert konfig.ist_login_host("https://beispiel.okta.com/app/x")
    assert not konfig.ist_login_host("https://my.hillebrandgori.com/public-tracking/DEMB00000")
    assert not konfig.ist_login_host(None)
    assert konfig.ist_login_seite(None, "https://login.hillebrand.com/")
    assert konfig.ist_login_seite('<div id="okta-sign-in">', None)
    assert not konfig.ist_login_seite("<p>Shipment</p>", "https://my.hillebrandgori.com/")


def test_muster_fuer_code_und_text(konfig: HillebrandKonfig) -> None:
    assert konfig.muster_fuer_code("shpsupdepcnf").status == SendungStatus.ABGEHOLT
    assert konfig.muster_fuer_code("SHPCUSARRPLNCHG").code == "shpcusarrpln"  # chg-Variante
    assert konfig.muster_fuer_code("gibt_es_nicht") is None
    assert konfig.muster_fuer_code(None) is None
    assert konfig.muster_fuer_text("Shipment X has departed from Treviso").code == "shpsupdepcnf"
    assert konfig.muster_fuer_text("Shipment X has been delivered to Y").code == "shpcusarrcnf"
    assert konfig.muster_fuer_text("Hallo Welt") is None


def test_status_fuer_text_laengste_uebereinstimmung(konfig: HillebrandKonfig) -> None:
    assert konfig.status_fuer_text("Status: Delivered") == SendungStatus.ZUGESTELLT
    assert konfig.status_fuer_text("ready for transport") == SendungStatus.BESTAETIGT
    assert konfig.status_fuer_text("Pick-up planned") == SendungStatus.ABHOLUNG_GEPLANT
    assert konfig.status_fuer_text("LATE") == SendungStatus.VERSPAETET
    assert konfig.status_fuer_text("on hold") == SendungStatus.PROBLEM
    assert konfig.status_fuer_text("unrelated") is None
    assert konfig.status_fuer_text("") is None
    assert konfig.status_fuer_text("belated") is None  # kein Treffer innerhalb eines Wortes


def test_challenge_und_geplant(konfig: HillebrandKonfig) -> None:
    assert konfig.ist_challenge("<title>Just a moment...</title>")
    assert not konfig.ist_challenge("<title>Shipment</title>")
    assert konfig.ist_geplant_text("Estimated arrival 27 Oct")
    assert not konfig.ist_geplant_text("Arrived at destination")


# ---------------------------------------------------------------- Status-Logik


def _m(code: str, geplant: bool = False, tag: int = 20) -> Meilenstein:
    return Meilenstein(code=code, text=code, zeitpunkt=datetime(2026, 10, tag), geplant=geplant)


def test_status_aus_meilensteinen(konfig: HillebrandKonfig) -> None:
    assert status_aus_meilensteinen([], konfig) == SendungStatus.UNBEKANNT
    assert status_aus_meilensteinen([_m("shprdy")], konfig) == SendungStatus.BESTAETIGT
    # geplante Zustellung zählt erst nach der Abholung
    assert status_aus_meilensteinen([_m("shprdy"), _m("shpcusarrpln", True, 27)], konfig) == SendungStatus.BESTAETIGT
    assert status_aus_meilensteinen([_m("shprdy"), _m("shpsupdeppln", True, 22)], konfig) == SendungStatus.ABHOLUNG_GEPLANT
    assert (
        status_aus_meilensteinen([_m("shpsupdepcnf", tag=22), _m("shpcusarrpln", True, 27)], konfig)
        == SendungStatus.ZUSTELLUNG_GEPLANT
    )
    assert status_aus_meilensteinen([_m("shpcusarrcnf", tag=27), _m("shprdy")], konfig) == SendungStatus.ZUGESTELLT
    # Text statt Code
    assert status_aus_meilensteinen([Meilenstein(code="x", text="Shipment has departed from A")], konfig) == SendungStatus.ABGEHOLT
    # unbekannte Codes ändern nichts
    assert status_aus_meilensteinen([_m("fremd")], konfig) == SendungStatus.UNBEKANNT


def test_status_basis_und_puenktlichkeit(konfig: HillebrandKonfig) -> None:
    assert status_aus_meilensteinen([_m("shprdy")], konfig, basis=SendungStatus.UNTERWEGS) == SendungStatus.UNTERWEGS
    assert status_aus_meilensteinen([_m("shprdy")], konfig, puenktlich=False) == SendungStatus.VERSPAETET
    assert status_aus_meilensteinen([_m("shpcusarrcnf")], konfig, puenktlich=False) == SendungStatus.ZUGESTELLT
    assert status_aus_meilensteinen([], konfig, puenktlich=False) == SendungStatus.UNBEKANNT
    assert status_mit_puenktlichkeit(SendungStatus.ABGEHOLT, None) == SendungStatus.ABGEHOLT
    assert status_mit_puenktlichkeit(SendungStatus.ABGEHOLT, True) == SendungStatus.ABGEHOLT


def test_status_ohne_konfig_standardcodes() -> None:
    assert status_aus_meilensteinen([_m("shpsupdepcnf")]) == SendungStatus.ABGEHOLT


def test_rang_und_fortschritt() -> None:
    assert all(s in STATUS_RANG for s in SendungStatus)
    assert ist_fortschritt(SendungStatus.ABGEHOLT, SendungStatus.BESTAETIGT)
    assert not ist_fortschritt(SendungStatus.BESTAETIGT, SendungStatus.ABGEHOLT)
    assert not ist_fortschritt(None, None)
    assert ist_bekannt(SendungStatus.BEAUFTRAGT)
    assert not ist_bekannt(SendungStatus.UNBEKANNT)
    assert not ist_bekannt(None)


# ---------------------------------------------------------------- Helfer


def test_normalisiere_referenz() -> None:
    assert normalisiere_referenz("demb 00000 [jobDEM00000000]") == "DEMB00000"
    assert normalisiere_referenz("DEMB00000") == "DEMB00000"
    assert normalisiere_referenz("  ") is None
    assert normalisiere_referenz(None) is None


@pytest.mark.parametrize(
    ("text", "bezug", "erwartet"),
    [
        ("27.10.", date(2026, 10, 19), date(2026, 10, 27)),
        ("27.10. ", datetime(2026, 10, 19, 10), date(2026, 10, 27)),
        ("05.01.", date(2026, 12, 18), date(2027, 1, 5)),  # Jahreswechsel
        ("03.11.2026", date(2026, 1, 1), date(2026, 11, 3)),
        ("03.11.26", None, date(2026, 11, 3)),
        ("2026-11-03", None, date(2026, 11, 3)),
        ("31.02.", date(2026, 1, 1), None),
        ("", None, None),
        (None, None, None),
        ("Dienstag", None, None),
    ],
)
def test_parse_datum_mit_bezug(text, bezug, erwartet) -> None:
    assert parse_datum_mit_bezug(text, bezug) == erwartet


@pytest.mark.parametrize(
    ("text", "erwartet"),
    [
        ("22 Oct 2026 08:15 departed", datetime(2026, 10, 22, 8, 15)),
        ("October 22, 2026", datetime(2026, 10, 22)),
        ("22nd October 2026", datetime(2026, 10, 22)),
        ("Shipment arrived on 2026-10-27 at 14:30", datetime(2026, 10, 27, 14, 30)),
        ("am 03.11.2026", datetime(2026, 11, 3)),
        ("laut Transporeon am 27.10. angeliefert", datetime(2026, 10, 27)),
        ("Shipment DEMB00000 ready", None),
        ("", None),
    ],
)
def test_finde_datum_tolerant(text, erwartet) -> None:
    assert finde_datum_tolerant(text, date(2026, 10, 19)) == erwartet


def test_mit_hinweis_kopiert() -> None:
    original = Sendung(spedition="Hillebrand", referenz="DEMB00000", hinweise=["alt"])
    kopie = mit_hinweis(original, "neu")
    assert kopie is not original
    assert kopie.hinweise == ["alt", "neu"]
    assert original.hinweise == ["alt"]
    assert kopie.letzte_pruefung is not None
    assert kopie.status == original.status
    assert mit_hinweis(kopie, "neu").hinweise == ["alt", "neu"]  # keine Doppelung


def test_vereinige_meilensteine() -> None:
    a = [_m("shprdy", tag=20), _m("shpsupdepcnf", tag=22)]
    b = [_m("shpsupdepcnf", tag=22), _m("shpcusarrpln", True, 27), Meilenstein(code="ohne", text="x")]
    ergebnis = vereinige_meilensteine(a, b)
    assert [m.code for m in ergebnis] == ["ohne", "shprdy", "shpsupdepcnf", "shpcusarrpln"]


def test_protokoll_laufzeitpruefung() -> None:
    class Quelle:
        name = "test"

        def verfuegbar(self) -> bool:
            return True

        def aktualisiere(self, sendung: Sendung) -> Sendung:
            return sendung.model_copy()

    assert isinstance(Quelle(), TrackingQuelle)
    assert not isinstance(object(), TrackingQuelle)
