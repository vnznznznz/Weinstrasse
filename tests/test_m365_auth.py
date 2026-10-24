"""Tests für m365.auth mit einer Fake-msal-App (kein Netz, kein Browser)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import msal
import pytest

from auftragsautomat.config import M365Konfig
from auftragsautomat.m365 import auth
from auftragsautomat.m365.auth import GraphAnmeldung, bereinige_scopes, standard_cache_pfad
from auftragsautomat.m365.basis import M365Fehler, M365NichtAngemeldet


class FakeApp:
    """Nachbildung von msal.PublicClientApplication für die genutzten Methoden."""

    instanzen: list[FakeApp] = []

    def __init__(self, client_id: str, authority: str | None = None, token_cache: Any = None, **kw: Any) -> None:
        self.client_id = client_id
        self.authority = authority
        self.token_cache = token_cache
        self.konten: list[dict[str, Any]] = []
        self.silent_ergebnis: dict[str, Any] | None = None
        self.interaktiv_ergebnis: dict[str, Any] = {}
        self.device_flow: dict[str, Any] = {}
        self.device_ergebnis: dict[str, Any] = {}
        self.aufrufe: list[tuple[str, Any]] = []
        FakeApp.instanzen.append(self)

    def get_accounts(self, username: str | None = None) -> list[dict[str, Any]]:
        return list(self.konten)

    def acquire_token_silent(self, scopes: list[str], account: Any = None, **kw: Any) -> dict[str, Any] | None:
        self.aufrufe.append(("silent", (scopes, account)))
        return self.silent_ergebnis

    def acquire_token_interactive(self, scopes: list[str], **kw: Any) -> dict[str, Any]:
        self.aufrufe.append(("interaktiv", (scopes, kw)))
        if "access_token" in self.interaktiv_ergebnis:
            self.konten.append({"username": "frau.beispiel@example.org"})
        return self.interaktiv_ergebnis

    def initiate_device_flow(self, scopes: list[str] | None = None, **kw: Any) -> dict[str, Any]:
        self.aufrufe.append(("device_flow", scopes))
        return self.device_flow

    def acquire_token_by_device_flow(self, flow: dict[str, Any], **kw: Any) -> dict[str, Any]:
        self.aufrufe.append(("device", flow))
        return self.device_ergebnis

    def remove_account(self, konto: dict[str, Any]) -> None:
        self.konten.remove(konto)


@pytest.fixture()
def fake_msal(monkeypatch: pytest.MonkeyPatch) -> type[FakeApp]:
    FakeApp.instanzen = []
    monkeypatch.setattr(msal, "PublicClientApplication", FakeApp)
    return FakeApp


@pytest.fixture()
def konfig(tmp_path: Path) -> M365Konfig:
    return M365Konfig(
        backend="graph",
        tenant_id="00000000-0000-0000-0000-000000000001",
        client_id="00000000-0000-0000-0000-0000000000aa",
        token_cache=tmp_path / "cache" / "token_cache.bin",
    )


def test_bereinige_scopes_entfernt_reservierte_und_dubletten():
    scopes = ["User.Read", "offline_access", "Files.ReadWrite", "openid", " profile ", "User.Read", ""]
    assert bereinige_scopes(scopes) == ["User.Read", "Files.ReadWrite"]


def test_msal_lehnt_reservierte_scopes_selbst_ab_und_ergaenzt_sie():
    """Beleg für die Dokumentation: msal 1.39 wirft bei offline_access und ergänzt reservierte Scopes selbst."""
    app = msal.PublicClientApplication("00000000-0000-0000-0000-0000000000aa", authority="https://login.microsoftonline.com/organizations", validate_authority=False)
    with pytest.raises(ValueError, match="reserved"):
        app._decorate_scope(["User.Read", "offline_access"])
    dekoriert = set(app._decorate_scope(["User.Read"]))
    assert {"User.Read", "offline_access", "openid", "profile"} <= dekoriert


def test_konstruktor_prueft_konfiguration(fake_msal: type[FakeApp], tmp_path: Path):
    with pytest.raises(M365Fehler, match="client_id"):
        GraphAnmeldung(M365Konfig(tenant_id="t", token_cache=tmp_path / "c.bin"))
    with pytest.raises(M365Fehler, match="tenant_id"):
        GraphAnmeldung(M365Konfig(client_id="c", token_cache=tmp_path / "c.bin"))
    with pytest.raises(M365Fehler, match="bereiche"):
        GraphAnmeldung(M365Konfig(client_id="c", tenant_id="t", bereiche=["offline_access"], token_cache=tmp_path / "c.bin"))
    ohne_tenant = GraphAnmeldung(M365Konfig(client_id="c", autoritaet="https://login.example.org/x", token_cache=tmp_path / "c.bin"))
    assert ohne_tenant.autoritaet == "https://login.example.org/x"


def test_app_wird_mit_autoritaet_cache_und_scopes_erzeugt(fake_msal: type[FakeApp], konfig: M365Konfig, caplog: pytest.LogCaptureFixture):
    with caplog.at_level(logging.WARNING, logger="auftragsautomat.m365.auth"):
        anmeldung = GraphAnmeldung(konfig)
    app = fake_msal.instanzen[-1]
    assert app.client_id == konfig.client_id
    assert app.authority == f"https://login.microsoftonline.com/{konfig.tenant_id}"
    assert app.token_cache is not None and not app.token_cache.is_encrypted
    assert "UNVERSCHLÜSSELT" in caplog.text  # Linux: FilePersistence mit Warnung
    assert anmeldung.scopes == ["User.Read", "Files.ReadWrite", "Sites.Read.All", "Mail.ReadWrite", "Mail.Read"]
    assert "offline_access" not in anmeldung.scopes
    assert anmeldung.cache_pfad == konfig.token_cache and anmeldung.cache_pfad.parent.is_dir()


def test_token_ohne_konto_wirft_nicht_angemeldet(fake_msal: type[FakeApp], konfig: M365Konfig):
    anmeldung = GraphAnmeldung(konfig)
    with pytest.raises(M365NichtAngemeldet, match="m365-anmelden"):
        anmeldung.token()
    assert anmeldung.konto() is None


def test_token_still_aus_cache(fake_msal: type[FakeApp], konfig: M365Konfig):
    anmeldung = GraphAnmeldung(konfig)
    app = fake_msal.instanzen[-1]
    app.konten = [{"username": "frau.beispiel@example.org"}, {"username": "zweit@example.org"}]
    app.silent_ergebnis = {"access_token": "tok-123"}
    assert anmeldung.token() == "tok-123"
    assert anmeldung.konto() == "frau.beispiel@example.org"
    art, (scopes, konto) = app.aufrufe[-1]
    assert art == "silent" and scopes == anmeldung.scopes and konto["username"] == "frau.beispiel@example.org"


def test_token_still_fehlgeschlagen_meldet_interaktion(fake_msal: type[FakeApp], konfig: M365Konfig):
    anmeldung = GraphAnmeldung(konfig)
    app = fake_msal.instanzen[-1]
    app.konten = [{"username": "frau.beispiel@example.org"}]
    app.silent_ergebnis = {"error": "interaction_required", "error_description": "AADSTS50076: MFA nötig\nTrace", "correlation_id": "abc"}
    with pytest.raises(M365NichtAngemeldet) as info:
        anmeldung.token()
    assert "interaction_required" in str(info.value) and "AADSTS50076" in str(info.value) and "abc" in str(info.value)
    assert "Trace" not in str(info.value)
    assert info.value.code == "interaction_required"


def test_anmelden_interaktiv(fake_msal: type[FakeApp], konfig: M365Konfig):
    anmeldung = GraphAnmeldung(konfig)
    app = fake_msal.instanzen[-1]
    app.interaktiv_ergebnis = {"access_token": "tok", "id_token_claims": {"preferred_username": "frau.beispiel@example.org"}}
    assert anmeldung.anmelden_interaktiv(login_hint="frau.beispiel@example.org") == "frau.beispiel@example.org"
    art, (scopes, kw) = app.aufrufe[-1]
    assert art == "interaktiv" and scopes == anmeldung.scopes and kw["login_hint"] == "frau.beispiel@example.org"
    assert anmeldung.konto() == "frau.beispiel@example.org"
    assert anmeldung.abmelden() == 1 and anmeldung.konto() is None


def test_anmelden_interaktiv_fehler(fake_msal: type[FakeApp], konfig: M365Konfig):
    anmeldung = GraphAnmeldung(konfig)
    fake_msal.instanzen[-1].interaktiv_ergebnis = {"error": "access_denied", "error_description": "Abgebrochen"}
    with pytest.raises(M365Fehler, match="access_denied") as info:
        anmeldung.anmelden_interaktiv()
    assert not isinstance(info.value, M365NichtAngemeldet)


def test_anmelden_device_code(fake_msal: type[FakeApp], konfig: M365Konfig):
    anmeldung = GraphAnmeldung(konfig)
    app = fake_msal.instanzen[-1]
    app.device_flow = {"user_code": "ABCD1234", "verification_uri": "https://microsoft.com/devicelogin", "message": "Bitte Code ABCD1234 eingeben"}
    app.device_ergebnis = {"access_token": "tok", "id_token_claims": {"upn": "frau.beispiel@example.org"}}
    ausgaben: list[str] = []
    assert anmeldung.anmelden_device_code(ausgaben.append) == "frau.beispiel@example.org"
    assert ausgaben == ["Bitte Code ABCD1234 eingeben"]
    assert app.aufrufe[-2] == ("device_flow", anmeldung.scopes)
    assert app.aufrufe[-1][0] == "device" and app.aufrufe[-1][1]["user_code"] == "ABCD1234"


def test_anmelden_device_code_start_fehlgeschlagen(fake_msal: type[FakeApp], konfig: M365Konfig):
    anmeldung = GraphAnmeldung(konfig)
    fake_msal.instanzen[-1].device_flow = {"error": "invalid_client", "error_description": "public client flows aus"}
    with pytest.raises(M365Fehler, match="invalid_client"):
        anmeldung.anmelden_device_code(lambda _t: None)


def test_standard_cache_pfad(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData"))
    assert standard_cache_pfad() == tmp_path / "AppData" / "Auftragsautomat" / "token_cache.bin"
    monkeypatch.delenv("APPDATA")
    assert standard_cache_pfad().name == "token_cache.bin" and ".auftragsautomat" in standard_cache_pfad().parts


def test_konfig_ohne_token_cache_nutzt_standardpfad(fake_msal: type[FakeApp], monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData"))
    anmeldung = GraphAnmeldung(M365Konfig(client_id="c", tenant_id="t"))
    assert anmeldung.cache_pfad == tmp_path / "AppData" / "Auftragsautomat" / "token_cache.bin"
    assert auth.RESERVIERTE_SCOPES == {"openid", "profile", "offline_access"}
