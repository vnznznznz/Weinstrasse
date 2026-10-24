"""Tests für tracking/hillebrand_portal.py – ohne echten Browserstart (Fake-Lader, Fake-Kontext)."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date
from pathlib import Path

import pytest

from auftragsautomat.config import Settings
from auftragsautomat.models import Sendung, SendungStatus
from auftragsautomat.tracking import hillebrand_portal
from auftragsautomat.tracking.basis import HillebrandKonfig, lade_hillebrand_konfig
from auftragsautomat.tracking.hillebrand_portal import HINWEIS_LOGIN, PortalQuelle, Seite, profilverzeichnis
from auftragsautomat.tracking.hillebrand_public import LaderFehler


@pytest.fixture()
def konfig() -> HillebrandKonfig:
    return lade_hillebrand_konfig()


@pytest.fixture()
def seiten(fixtures_ordner: Path) -> dict[str, str]:
    return {p.stem: p.read_text(encoding="utf-8") for p in (fixtures_ordner / "tracking" / "html").glob("*.html")}


class FakeLader:
    def __init__(self, seite: Seite | None = None, fehler: Exception | None = None) -> None:
        self.seite = seite
        self.fehler = fehler
        self.urls: list[str] = []

    def __call__(self, url: str) -> Seite:
        self.urls.append(url)
        if self.fehler is not None:
            raise self.fehler
        assert self.seite is not None
        return self.seite


# ---------------------------------------------------------------- Profil, URL, Login


def test_profilverzeichnis(settings: Settings, tmp_path: Path) -> None:
    settings.tracking.chrome_profil = None
    nt = profilverzeichnis(settings, umgebung={"LOCALAPPDATA": r"C:\Users\beispiel\AppData\Local"}, plattform="nt")
    assert nt == Path(r"C:\Users\beispiel\AppData\Local") / "Auftragsautomat" / "chrome-profil"
    nt_ohne = profilverzeichnis(settings, umgebung={}, plattform="nt")
    assert nt_ohne == Path.home() / "AppData" / "Local" / "Auftragsautomat" / "chrome-profil"
    posix = profilverzeichnis(settings, umgebung={}, plattform="posix")
    assert posix == Path.home() / ".local" / "share" / "auftragsautomat" / "chrome-profil"
    settings.tracking.chrome_profil = tmp_path / "eigenes-profil"
    assert profilverzeichnis(settings, umgebung={"LOCALAPPDATA": "x"}, plattform="nt") == tmp_path / "eigenes-profil"
    assert PortalQuelle(lade_hillebrand_konfig(), settings, lade_seite=FakeLader()).profil() == tmp_path / "eigenes-profil"
    # nie das Standardprofil von Chrome
    for pfad in (nt, nt_ohne, posix):
        assert "Google" not in pfad.parts and "User Data" not in pfad.parts


def test_profil_standard_ohne_settings() -> None:
    assert profilverzeichnis(None, umgebung={}, plattform="posix").name == "chrome-profil"


def test_suche_url(konfig: HillebrandKonfig, settings: Settings) -> None:
    quelle = PortalQuelle(konfig, settings, lade_seite=FakeLader())
    assert quelle.name == "portal"
    assert quelle.verfuegbar()
    assert quelle.suche_url("DEMB 00000") == "https://my.hillebrandgori.com/public-tracking/DEMB%2000000"
    ohne = konfig.model_copy(update={"portal_tracking_url": None})
    assert PortalQuelle(ohne, settings, lade_seite=FakeLader()).suche_url("X") == ohne.public_url("X")


def test_login_erkennung(konfig: HillebrandKonfig, settings: Settings, seiten: dict[str, str]) -> None:
    quelle = PortalQuelle(konfig, settings, lade_seite=FakeLader())
    assert quelle.ist_login_seite(Seite(url="https://login.hillebrand.com/oauth2/x/v1/authorize", html="<html></html>"))
    assert quelle.ist_login_seite(Seite(url="https://my.hillebrandgori.com/", html=seiten["login_okta"]))
    assert not quelle.ist_login_seite(Seite(url="https://my.hillebrandgori.com/public-tracking/X", html=seiten["public_tracking"]))


def test_docstring_nennt_nutzungsbedingungen() -> None:
    assert "Nutzungsbedingungen" in (hillebrand_portal.__doc__ or "")
    assert "Rückfall" in (hillebrand_portal.__doc__ or "")


# ---------------------------------------------------------------- aktualisiere


def test_aktualisiere_login_seite(konfig: HillebrandKonfig, settings: Settings, seiten: dict[str, str]) -> None:
    lader = FakeLader(Seite(url="https://login.hillebrand.com/", html=seiten["login_okta"]))
    sendung = Sendung(spedition="Hillebrand", referenz="DEMB00000", status=SendungStatus.ABGEHOLT, quelle="api")
    ergebnis = PortalQuelle(konfig, settings, lade_seite=lader).aktualisiere(sendung)
    assert lader.urls == [konfig.portal_suche_url("DEMB00000")]
    assert ergebnis.status == SendungStatus.ABGEHOLT and ergebnis.quelle == "api"
    assert ergebnis.hinweise == [HINWEIS_LOGIN]
    assert "manuell anmelden" in HINWEIS_LOGIN


def test_aktualisiere_daten(konfig: HillebrandKonfig, settings: Settings, seiten: dict[str, str], tmp_path: Path) -> None:
    lader = FakeLader(Seite(url=konfig.portal_suche_url("DEMB00000"), html=seiten["public_tracking"], screenshot=tmp_path / "s.png"))
    original = Sendung(spedition="Hillebrand", referenz="DEMB00000")
    ergebnis = PortalQuelle(konfig, settings, lade_seite=lader).aktualisiere(original)
    assert original.status == SendungStatus.UNBEKANNT
    assert ergebnis.status == SendungStatus.ZUSTELLUNG_GEPLANT
    assert ergebnis.quelle == "portal"
    assert ergebnis.geplante_abholung == date(2026, 10, 22)
    assert ergebnis.geplante_zustellung == date(2026, 10, 27)
    assert len(ergebnis.meilensteine) == 3
    assert ergebnis.hinweise == [f"Portal-Screenshot: {tmp_path / 's.png'}"]


def test_aktualisiere_challenge_und_leer(konfig: HillebrandKonfig, settings: Settings, seiten: dict[str, str]) -> None:
    sendung = Sendung(spedition="Hillebrand", referenz="DEMB00000")
    ergebnis = PortalQuelle(konfig, settings, lade_seite=FakeLader(Seite(url="u", html=seiten["cloudflare_challenge"]))).aktualisiere(sendung)
    assert any("Cloudflare" in h for h in ergebnis.hinweise) and ergebnis.status == SendungStatus.UNBEKANNT
    ergebnis = PortalQuelle(konfig, settings, lade_seite=FakeLader(Seite(url="u", html=""))).aktualisiere(sendung)
    assert any("leere Seite" in h for h in ergebnis.hinweise)
    ergebnis = PortalQuelle(konfig, settings, lade_seite=FakeLader(Seite(url="u", html=seiten["public_tracking_leer"]))).aktualisiere(sendung)
    assert any("keine Sendungsdaten" in h and "aufzeichnen()" in h for h in ergebnis.hinweise)


def test_aktualisiere_fehler_nie_exception(konfig: HillebrandKonfig, settings: Settings) -> None:
    sendung = Sendung(spedition="Hillebrand", referenz="DEMB00000")
    ergebnis = PortalQuelle(konfig, settings, lade_seite=FakeLader(fehler=RuntimeError("Chrome nicht gefunden"))).aktualisiere(sendung)
    assert any("Chrome nicht gefunden" in h for h in ergebnis.hinweise)
    ergebnis = PortalQuelle(konfig, settings, lade_seite=FakeLader(fehler=LaderFehler("Timeout"))).aktualisiere(sendung)
    assert ergebnis.hinweise == ["Portal: Timeout"]
    ergebnis = PortalQuelle(konfig, settings, lade_seite=FakeLader()).aktualisiere(Sendung(spedition="Hillebrand"))
    assert "ohne Hillebrand-Referenz" in ergebnis.hinweise[0]


def test_ohne_playwright(monkeypatch: pytest.MonkeyPatch, konfig: HillebrandKonfig, settings: Settings) -> None:
    monkeypatch.setattr(hillebrand_portal, "playwright_verfuegbar", lambda: False)
    quelle = PortalQuelle(konfig, settings)
    assert not quelle.verfuegbar()
    ergebnis = quelle.aktualisiere(Sendung(spedition="Hillebrand", referenz="DEMB00000"))
    assert ergebnis.hinweise == ["Portal nicht verfügbar: Playwright ist nicht installiert"]


# ---------------------------------------------------------------- Einrichtung


def test_aufzeichnen(konfig: HillebrandKonfig, settings: Settings, seiten: dict[str, str], tmp_path: Path) -> None:
    settings.tracking.screenshots_ordner = tmp_path / "tracking"
    lader = FakeLader(Seite(url="https://my.hillebrandgori.com/public-tracking/DEMB00000", html=seiten["public_tracking"]))
    quelle = PortalQuelle(konfig, settings, lade_seite=lader)
    ordner = quelle.aufzeichnen("DEMB00000")
    assert ordner.parent == tmp_path / "tracking" / "aufzeichnung"
    assert ordner.name.endswith("_DEMB00000")
    assert (ordner / "seite.html").read_text(encoding="utf-8") == seiten["public_tracking"]
    assert (ordner / "url.txt").read_text(encoding="utf-8").strip() == "https://my.hillebrandgori.com/public-tracking/DEMB00000"
    ziel = quelle.aufzeichnen("DEMB00000", tmp_path / "ziel")
    assert ziel == tmp_path / "ziel" and (ziel / "seite.html").exists()
    with pytest.raises(ValueError):
        quelle.aufzeichnen("  ")


def test_aufzeichnen_login_warnt(konfig: HillebrandKonfig, settings: Settings, seiten: dict[str, str], tmp_path: Path, caplog) -> None:
    lader = FakeLader(Seite(url="https://login.hillebrand.com/", html=seiten["login_okta"]))
    with caplog.at_level("WARNING"):
        PortalQuelle(konfig, settings, lade_seite=lader).aufzeichnen("DEMB00000", tmp_path / "a")
    assert "Anmeldeseite" in caplog.text


class _FakeSeite:
    def __init__(self, url_nach_login: str, html: str) -> None:
        self.url = url_nach_login
        self._html = html
        self.aufgerufen: list[str] = []

    def goto(self, url: str, **kwargs) -> None:
        self.aufgerufen.append(url)

    def content(self) -> str:
        return self._html


class _FakeKontext:
    def __init__(self, seite: _FakeSeite) -> None:
        self.pages = [seite]


def test_einrichten_mit_fake_browser(monkeypatch: pytest.MonkeyPatch, konfig: HillebrandKonfig, settings: Settings, seiten: dict[str, str], capsys) -> None:
    gestartet: list[bool] = []

    def fabrik(seite: _FakeSeite):
        @contextmanager
        def _browser(self, *, headless: bool = False):
            gestartet.append(headless)
            yield _FakeKontext(seite)

        return _browser

    gewartet: list[int] = []
    angemeldet = _FakeSeite("https://my.hillebrandgori.com/shipments", seiten["public_tracking"])
    monkeypatch.setattr(PortalQuelle, "_browser", fabrik(angemeldet))
    quelle = PortalQuelle(konfig, settings)
    assert quelle.einrichten(warte=lambda: gewartet.append(1)) is True
    assert gewartet == [1]
    assert gestartet == [False]  # immer sichtbar
    assert angemeldet.aufgerufen == [konfig.portal_url]
    assert "anmelden" in capsys.readouterr().out

    nicht = _FakeSeite("https://login.hillebrand.com/", seiten["login_okta"])
    monkeypatch.setattr(PortalQuelle, "_browser", fabrik(nicht))
    assert PortalQuelle(konfig, settings).einrichten(warte=lambda: None) is False
