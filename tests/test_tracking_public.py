"""Tests für tracking/hillebrand_public.py: HTML-Parser, Selektoren, Seitentypen, Quelle mit Fake-Lader."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest

from auftragsautomat.config import TrackingKonfig
from auftragsautomat.models import Sendung, SendungStatus
from auftragsautomat.tracking import hillebrand_public
from auftragsautomat.tracking.basis import HillebrandKonfig, lade_hillebrand_konfig
from auftragsautomat.tracking.hillebrand_public import (
    LaderFehler,
    PublicTrackingQuelle,
    erkenne_seitentyp,
    parse_html,
    parse_public_tracking_html,
    waehle,
)


@pytest.fixture()
def konfig() -> HillebrandKonfig:
    return lade_hillebrand_konfig()


@pytest.fixture()
def html_ordner(fixtures_ordner: Path) -> Path:
    ordner = fixtures_ordner / "tracking" / "html"
    assert ordner.is_dir()
    return ordner


@pytest.fixture()
def seiten(html_ordner: Path) -> dict[str, str]:
    return {p.stem: p.read_text(encoding="utf-8") for p in html_ordner.glob("*.html")}


# ---------------------------------------------------------------- DOM und Selektoren


def test_parse_html_tolerant() -> None:
    wurzel = parse_html("<ul><li>eins<li>zwei</ul><p>drei<br>vier</p><img src=x>")
    assert [k.text() for k in waehle(wurzel, "li")] == ["eins", "zwei"]
    assert waehle(wurzel, "p")[0].text() == "drei\nvier"
    assert waehle(wurzel, "img")[0].attrs["src"] == "x"
    assert parse_html(None).text() == ""
    assert parse_html("<script>var x = 1;</script><style>p{}</style><b>sichtbar</b>").text() == "sichtbar"


def test_waehle_selektoren(seiten: dict[str, str]) -> None:
    wurzel = parse_html(seiten["public_tracking"])
    assert [k.text() for k in waehle(wurzel, "jfh-tracking-info-card .status")] == ["In transit"]
    assert len(waehle(wurzel, "ul.timeline > li")) == 3
    assert len(waehle(wurzel, "li.done")) == 2
    assert len(waehle(wurzel, "li.done, li.planned")) == 3
    assert len(waehle(wurzel, "jfh-tracking-timeline li.timeline-item.done")) == 2
    assert [k.attrs["value"] for k in waehle(wurzel, "input[value=DEMB00000]")] == ["DEMB00000"]
    assert len(waehle(wurzel, "input[placeholder]")) == 1
    assert waehle(wurzel, "#nix") == []
    assert waehle(wurzel, "jfh-tracking-info-card > li") == []  # li sind keine direkten Kinder
    assert waehle(wurzel, "") == []
    assert waehle(wurzel, None) == []
    with pytest.raises(ValueError):
        waehle(wurzel, "li::before")


def test_knoten_hilfen() -> None:
    wurzel = parse_html('<div id="a" class="x y"><span>t</span></div>')
    div = waehle(wurzel, "#a")[0]
    assert div.id == "a" and div.klassen == {"x", "y"}
    assert [k.tag for k in div.alle()] == ["span"]
    assert div.elemente()[0].eltern is div


# ---------------------------------------------------------------- Seite auswerten


def test_erkenne_seitentyp(seiten: dict[str, str], konfig: HillebrandKonfig) -> None:
    assert erkenne_seitentyp(seiten["public_tracking"], konfig) == "daten"
    assert erkenne_seitentyp(seiten["cloudflare_challenge"], konfig) == "challenge"
    assert erkenne_seitentyp(seiten["login_okta"], konfig) == "login"
    assert erkenne_seitentyp("<p>x</p>", konfig, url="https://login.hillebrand.com/") == "login"
    assert erkenne_seitentyp("", konfig) == "leer"
    assert erkenne_seitentyp("<html><body><script>x</script></body></html>", konfig) == "leer"


def test_parse_public_tracking_html(seiten: dict[str, str], konfig: HillebrandKonfig) -> None:
    status, meilensteine = parse_public_tracking_html(seiten["public_tracking"], konfig, bezug=datetime(2026, 10, 23))
    assert status == SendungStatus.ZUSTELLUNG_GEPLANT
    assert [m.code for m in meilensteine] == ["shprdy", "shpsupdepcnf", "shpcusarrpln"]
    assert [m.geplant for m in meilensteine] == [False, False, True]
    assert meilensteine[0].zeitpunkt == datetime(2026, 10, 20)
    assert meilensteine[1].zeitpunkt == datetime(2026, 10, 22, 8, 15)
    assert meilensteine[1].ort == "Treviso"
    assert meilensteine[2].zeitpunkt == datetime(2026, 10, 27)
    assert meilensteine[2].ort == "Beispielstadt"
    assert "has departed" in meilensteine[1].text


def test_parse_leer(seiten: dict[str, str], konfig: HillebrandKonfig) -> None:
    assert parse_public_tracking_html(seiten["public_tracking_leer"], konfig) == (SendungStatus.UNBEKANNT, [])
    assert parse_public_tracking_html("", konfig) == (SendungStatus.UNBEKANNT, [])
    assert parse_public_tracking_html(None, konfig) == (SendungStatus.UNBEKANNT, [])


def test_parse_status_regex_rueckfall(konfig: HillebrandKonfig) -> None:
    html = "<html><body><h1>Shipment DEMB00000</h1><p>Status: Delivered</p></body></html>"
    assert parse_public_tracking_html(html, konfig) == (SendungStatus.ZUGESTELLT, [])
    html = "<html><body><p>Status: Exception</p></body></html>"
    assert parse_public_tracking_html(html, konfig)[0] == SendungStatus.PROBLEM
    html = "<html><body><p>Status: Delayed</p><jfh-tracking-timeline><li>Shipment has departed from A on 1 Oct 2026</li></jfh-tracking-timeline></body></html>"
    assert parse_public_tracking_html(html, konfig)[0] == SendungStatus.VERSPAETET
    html = "<html><body><p>Status: Delayed</p></body></html>"
    assert parse_public_tracking_html(html, konfig)[0] == SendungStatus.VERSPAETET


def test_parse_timeline_ohne_eintrag_selektor(konfig: HillebrandKonfig) -> None:
    """Ohne 'eintrag'-Selektor werden li/tr-Zeilen der Timeline gelesen; unbekannte Texte bleiben Meilensteine."""
    sel = konfig.selektoren.public.model_copy(update={"eintrag": None, "status": None, "status_regex": None})
    html = (
        "<jfh-tracking-timeline><table><tr><td>Planned to depart 22.10.2026</td></tr>"
        "<tr><td class=actual>Irgendwas ohne Muster</td></tr></table></jfh-tracking-timeline>"
    )
    status, meilensteine = parse_public_tracking_html(html, konfig, selektoren=sel)
    assert status == SendungStatus.ABHOLUNG_GEPLANT
    assert [m.code for m in meilensteine] == ["timeline", "shpsupdeppln"]
    assert meilensteine[1].geplant and meilensteine[1].zeitpunkt == datetime(2026, 10, 22)
    assert not meilensteine[0].geplant


def test_parse_mit_portal_selektoren(seiten: dict[str, str], konfig: HillebrandKonfig) -> None:
    status, meilensteine = parse_public_tracking_html(seiten["public_tracking"], konfig, selektoren=konfig.selektoren.portal)
    assert status == SendungStatus.ZUSTELLUNG_GEPLANT and len(meilensteine) == 3


# ---------------------------------------------------------------- Quelle


class FakeLader:
    def __init__(self, antworten: dict[str, str] | None = None, fehler: Exception | None = None) -> None:
        self.antworten = antworten or {}
        self.fehler = fehler
        self.urls: list[str] = []

    def __call__(self, url: str) -> str:
        self.urls.append(url)
        if self.fehler is not None:
            raise self.fehler
        begriff = url.rsplit("/", 1)[-1]
        return self.antworten.get(begriff, self.antworten.get("*", ""))


def test_quelle_mit_fixture_lader(seiten: dict[str, str], konfig: HillebrandKonfig) -> None:
    lader = FakeLader({"DEMB00000": seiten["public_tracking"]})
    quelle = PublicTrackingQuelle(konfig, lade_seite=lader)
    assert quelle.name == "public_tracking"
    assert quelle.verfuegbar()
    original = Sendung(spedition="Hillebrand", referenz="DEMB00000", kundenreferenz="WS-2026-0042")
    ergebnis = quelle.aktualisiere(original)
    assert lader.urls == [konfig.public_url("DEMB00000")]
    assert original.status == SendungStatus.UNBEKANNT
    assert ergebnis.status == SendungStatus.ZUSTELLUNG_GEPLANT
    assert ergebnis.quelle == "public_tracking"
    assert ergebnis.geplante_abholung == date(2026, 10, 22)
    assert ergebnis.geplante_zustellung == date(2026, 10, 27)
    assert len(ergebnis.meilensteine) == 3
    assert ergebnis.letzte_pruefung is not None
    assert ergebnis.hinweise == []


def test_quelle_challenge_und_login(seiten: dict[str, str], konfig: HillebrandKonfig) -> None:
    sendung = Sendung(spedition="Hillebrand", referenz="DEMB00000", status=SendungStatus.BESTAETIGT, quelle="mail")
    ergebnis = PublicTrackingQuelle(konfig, lade_seite=FakeLader({"*": seiten["cloudflare_challenge"]})).aktualisiere(sendung)
    assert ergebnis.status == SendungStatus.BESTAETIGT and ergebnis.quelle == "mail"
    assert any("Cloudflare" in h for h in ergebnis.hinweise)
    ergebnis = PublicTrackingQuelle(konfig, lade_seite=FakeLader({"*": seiten["login_okta"]})).aktualisiere(sendung)
    assert ergebnis.status == SendungStatus.BESTAETIGT
    assert any("Anmeldeseite" in h for h in ergebnis.hinweise)


def test_quelle_rueckfall_auf_kundenreferenz(seiten: dict[str, str], konfig: HillebrandKonfig) -> None:
    lader = FakeLader({"DEMB00000": seiten["public_tracking_leer"], "WS-2026-0042": seiten["public_tracking"]})
    ergebnis = PublicTrackingQuelle(konfig, lade_seite=lader).aktualisiere(
        Sendung(spedition="Hillebrand", referenz="DEMB00000", kundenreferenz="WS-2026-0042")
    )
    assert lader.urls == [konfig.public_url("DEMB00000"), konfig.public_url("WS-2026-0042")]
    assert ergebnis.status == SendungStatus.ZUSTELLUNG_GEPLANT
    assert ergebnis.hinweise == ["Public Tracking: keine Sendungsdaten zu DEMB00000"]


def test_quelle_keine_daten(seiten: dict[str, str], konfig: HillebrandKonfig) -> None:
    ergebnis = PublicTrackingQuelle(konfig, lade_seite=FakeLader({"*": seiten["public_tracking_leer"]})).aktualisiere(
        Sendung(spedition="Hillebrand", referenz="DEMB00000")
    )
    assert ergebnis.status == SendungStatus.UNBEKANNT
    assert ergebnis.hinweise == ["Public Tracking: keine Sendungsdaten zu DEMB00000"]
    assert ergebnis.letzte_pruefung is not None


def test_quelle_lader_fehler_nie_exception(konfig: HillebrandKonfig) -> None:
    ergebnis = PublicTrackingQuelle(konfig, lade_seite=FakeLader(fehler=RuntimeError("Timeout 30 s"))).aktualisiere(
        Sendung(spedition="Hillebrand", referenz="DEMB00000")
    )
    assert ergebnis.status == SendungStatus.UNBEKANNT
    assert any("Timeout 30 s" in h for h in ergebnis.hinweise)
    ergebnis = PublicTrackingQuelle(konfig, lade_seite=FakeLader(fehler=LaderFehler("Browser fehlt"))).aktualisiere(
        Sendung(spedition="Hillebrand", referenz="DEMB00000")
    )
    assert ergebnis.hinweise == ["Public Tracking: Browser fehlt"]


def test_quelle_ohne_referenz(konfig: HillebrandKonfig) -> None:
    lader = FakeLader()
    ergebnis = PublicTrackingQuelle(konfig, lade_seite=lader).aktualisiere(Sendung(spedition="Hillebrand"))
    assert "ohne Referenz" in ergebnis.hinweise[0]
    assert lader.urls == []


def test_quelle_ohne_playwright(monkeypatch: pytest.MonkeyPatch, konfig: HillebrandKonfig) -> None:
    monkeypatch.setattr(hillebrand_public, "playwright_verfuegbar", lambda: False)
    quelle = PublicTrackingQuelle(konfig, tracking=TrackingKonfig(sichtbar=False))
    assert not quelle.verfuegbar()
    ergebnis = quelle.aktualisiere(Sendung(spedition="Hillebrand", referenz="DEMB00000"))
    assert ergebnis.hinweise == ["Public Tracking nicht verfügbar: Playwright ist nicht installiert"]


def test_quelle_url_bau(konfig: HillebrandKonfig) -> None:
    assert PublicTrackingQuelle(konfig, lade_seite=FakeLader()).url_fuer("jobDEM 1") == (
        "https://my.hillebrandgori.com/public-tracking/jobDEM%201"
    )
