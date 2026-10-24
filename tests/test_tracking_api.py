"""Tests für tracking/hillebrand_api.py mit Fake-Transport und JSON-Fixtures (kein Netz)."""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest

from auftragsautomat.config import TrackingKonfig
from auftragsautomat.models import Sendung, SendungStatus
from auftragsautomat.tracking.basis import HillebrandKonfig, lade_hillebrand_konfig
from auftragsautomat.tracking.hillebrand_api import (
    ApiAntwort,
    ApiFehler,
    ApiQuelle,
    HillebrandApi,
    _waehle_sendung,
    _zeitpunkt,
    liste_aus,
)

UMGEBUNG = {
    "HILLEBRAND_API_KEY": "fixture-key",
    "HILLEBRAND_API_SECRET": "fixture-secret",
    "HILLEBRAND_API_BENUTZER": "api.beispiel@example.org",
    "HILLEBRAND_API_PASSWORT": "fixture-passwort",
}


class FakeTransport:
    """Liefert vorbereitete Antworten in Aufrufreihenfolge und protokolliert jeden Aufruf."""

    def __init__(self, antworten: list[ApiAntwort]) -> None:
        self.antworten = list(antworten)
        self.aufrufe: list[dict[str, Any]] = []

    def __call__(
        self,
        methode: str,
        url: str,
        *,
        headers: Mapping[str, str],
        params: Mapping[str, Any] | None = None,
        data: Mapping[str, Any] | None = None,
        timeout: float = 30.0,
    ) -> ApiAntwort:
        self.aufrufe.append(
            {"methode": methode, "url": url, "headers": dict(headers), "params": dict(params or {}), "data": dict(data or {}), "timeout": timeout}
        )
        if not self.antworten:
            raise AssertionError(f"Keine Antwort mehr vorbereitet für {methode} {url}")
        return self.antworten.pop(0)


class Uhr:
    def __init__(self, start: float = 1000.0) -> None:
        self.zeit = start
        self.geschlafen: list[float] = []

    def __call__(self) -> float:
        return self.zeit

    def schlafe(self, sekunden: float) -> None:
        self.geschlafen.append(sekunden)
        self.zeit += sekunden


@pytest.fixture()
def konfig() -> HillebrandKonfig:
    return lade_hillebrand_konfig()


@pytest.fixture()
def api_fixtures(fixtures_ordner: Path) -> dict[str, Any]:
    ordner = fixtures_ordner / "tracking" / "api"
    return {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in ordner.glob("*.json")}


def _ok(daten: Any) -> ApiAntwort:
    return ApiAntwort(200, daten, json.dumps(daten))


def _api(konfig: HillebrandKonfig, transport: FakeTransport, uhr: Uhr | None = None, umgebung: dict | None = None) -> HillebrandApi:
    uhr = uhr or Uhr()
    return HillebrandApi(
        konfig,
        TrackingKonfig(),
        sende=transport,
        umgebung=UMGEBUNG if umgebung is None else umgebung,
        uhr=uhr,
        schlafe=uhr.schlafe,
    )


# ---------------------------------------------------------------- Zugangsdaten / Token


def test_verfuegbar_nur_mit_allen_variablen(konfig: HillebrandKonfig) -> None:
    transport = FakeTransport([])
    api = _api(konfig, transport, umgebung={"HILLEBRAND_API_KEY": "k", "HILLEBRAND_API_SECRET": " "})
    assert not api.zugangsdaten_vorhanden()
    assert api.fehlende_variablen() == ["HILLEBRAND_API_SECRET", "HILLEBRAND_API_BENUTZER", "HILLEBRAND_API_PASSWORT"]
    quelle = ApiQuelle(api)
    assert quelle.name == "api"
    assert not quelle.verfuegbar()
    ergebnis = quelle.aktualisiere(Sendung(spedition="Hillebrand", referenz="DEMB00000"))
    assert ergebnis.status == SendungStatus.UNBEKANNT
    assert "HILLEBRAND_API_PASSWORT" in ergebnis.hinweise[0]
    assert transport.aufrufe == []  # ohne Zugangsdaten kein Netzaufruf
    with pytest.raises(ApiFehler):
        api.token()
    assert ApiQuelle(_api(konfig, transport)).verfuegbar()


def test_token_passwort_grant(konfig: HillebrandKonfig, api_fixtures: dict[str, Any]) -> None:
    transport = FakeTransport([_ok(api_fixtures["token"])])
    api = _api(konfig, transport)
    assert api.token() == "fixture-access-1"
    assert api.token() == "fixture-access-1"  # aus dem Cache
    assert len(transport.aufrufe) == 1
    aufruf = transport.aufrufe[0]
    assert aufruf["methode"] == "POST"
    assert aufruf["url"] == konfig.api.token_url
    basic = base64.b64encode(b"fixture-key:fixture-secret").decode()
    assert aufruf["headers"]["Authorization"] == f"Basic {basic}"
    assert aufruf["headers"]["Content-Type"] == "application/x-www-form-urlencoded"
    assert aufruf["headers"]["Accept"] == "application/json"
    assert aufruf["data"] == {
        "grant_type": "password",
        "username": "api.beispiel@example.org",
        "password": "fixture-passwort",
        "scope": "offline_access",
    }
    assert aufruf["timeout"] == konfig.api.timeout_sekunden


def test_token_erneuerung_mit_refresh_token(konfig: HillebrandKonfig, api_fixtures: dict[str, Any]) -> None:
    uhr = Uhr()
    transport = FakeTransport([_ok(api_fixtures["token"]), _ok(api_fixtures["token_refreshed"])])
    api = _api(konfig, transport, uhr)
    assert api.token() == "fixture-access-1"
    uhr.zeit += 7200 - 61  # kurz vor dem Sicherheitsabstand: noch gültig
    assert api.token() == "fixture-access-1"
    uhr.zeit += 2  # jetzt innerhalb der letzten 60 s -> erneuern
    assert api.token() == "fixture-access-2"
    assert transport.aufrufe[1]["data"] == {
        "grant_type": "refresh_token",
        "refresh_token": "fixture-refresh-1",
        "scope": "offline_access",
    }
    assert "Basic " in transport.aufrufe[1]["headers"]["Authorization"]


def test_refresh_fehlschlag_fuehrt_zu_neuem_login(konfig: HillebrandKonfig, api_fixtures: dict[str, Any]) -> None:
    uhr = Uhr()
    transport = FakeTransport(
        [_ok(api_fixtures["token"]), ApiAntwort(400, {"error": "invalid_grant"}, "invalid_grant"), _ok(api_fixtures["token_refreshed"])]
    )
    api = _api(konfig, transport, uhr)
    api.token()
    uhr.zeit += 10_000
    assert api.token() == "fixture-access-2"
    assert transport.aufrufe[1]["data"]["grant_type"] == "refresh_token"
    assert transport.aufrufe[2]["data"]["grant_type"] == "password"


def test_token_antwort_ohne_expires_in_nutzt_konfig(konfig: HillebrandKonfig) -> None:
    uhr = Uhr()
    transport = FakeTransport([_ok({"access_token": "a", "refresh_token": "r"}), _ok({"access_token": "b"})])
    api = _api(konfig, transport, uhr)
    api.token()
    uhr.zeit += konfig.api.token_gueltigkeit_sekunden - 100
    assert api.token() == "a"
    uhr.zeit += 100
    assert api.token() == "b"
    # Refresh-Token bleibt erhalten, wenn die Antwort keins liefert
    assert api._token is not None and api._token.refresh == "r"


def test_token_fehler_wird_api_fehler(konfig: HillebrandKonfig) -> None:
    transport = FakeTransport([ApiAntwort(401, {"error": "invalid_client"}, "invalid_client")])
    api = _api(konfig, transport)
    with pytest.raises(ApiFehler, match="HTTP 401"):
        api.token()
    transport = FakeTransport([_ok({"token_type": "Bearer"})])
    with pytest.raises(ApiFehler, match="access_token"):
        _api(konfig, transport).token()


# ---------------------------------------------------------------- Aufrufe


def test_sendungen_und_ereignisse_aufrufe(konfig: HillebrandKonfig, api_fixtures: dict[str, Any]) -> None:
    transport = FakeTransport([_ok(api_fixtures["token"]), _ok(api_fixtures["shipments"]), _ok(api_fixtures["events"])])
    api = _api(konfig, transport)
    sendungen = api.sendungen(" DEMB00000 ")
    assert len(sendungen) == 1 and sendungen[0]["id"] == 123456
    get = transport.aufrufe[1]
    assert get["methode"] == "GET"
    assert get["url"] == "https://api.hillebrandgori.com/v6/shipments"
    assert get["headers"] == {"Authorization": "Bearer fixture-access-1", "Accept": "application/json"}
    assert get["params"] == {"references": "DEMB00000", "pageSize": 100, "includeInactiveShipments": "true"}
    ereignisse = api.ereignisse(123456, seit=datetime(2026, 10, 21, 12, 0))
    assert len(ereignisse) == 3
    ev = transport.aufrufe[2]
    assert ev["url"] == "https://api.hillebrandgori.com/v6/shipments/123456/events"
    assert ev["params"] == {"pageSize": 100, "lastUpdateAfter": int(datetime(2026, 10, 21, 12, 0).timestamp())}
    assert api.anzahl_aufrufe == 3
    with pytest.raises(ApiFehler):
        api.sendungen("  ")


def test_401_erneuert_token_einmal(konfig: HillebrandKonfig, api_fixtures: dict[str, Any]) -> None:
    transport = FakeTransport(
        [_ok(api_fixtures["token"]), ApiAntwort(401, None, "expired"), _ok(api_fixtures["token_refreshed"]), _ok(api_fixtures["shipments_leer"])]
    )
    api = _api(konfig, transport)
    assert api.sendungen("DEMB00000") == []
    assert [a["methode"] for a in transport.aufrufe] == ["POST", "GET", "POST", "GET"]
    assert transport.aufrufe[2]["data"]["grant_type"] == "password"  # Token wurde verworfen -> neuer Login
    assert transport.aufrufe[3]["headers"]["Authorization"] == "Bearer fixture-access-2"
    # zweites 401 hintereinander -> Fehler
    transport = FakeTransport([_ok(api_fixtures["token"]), ApiAntwort(401), _ok(api_fixtures["token"]), ApiAntwort(401)])
    with pytest.raises(ApiFehler, match="401"):
        _api(konfig, transport).sendungen("DEMB00000")


def test_http_fehler(konfig: HillebrandKonfig, api_fixtures: dict[str, Any]) -> None:
    transport = FakeTransport([_ok(api_fixtures["token"]), ApiAntwort(500, None, "Internal Server Error " * 30)])
    with pytest.raises(ApiFehler, match="HTTP 500"):
        _api(konfig, transport).sendungen("DEMB00000")


def test_ratenbegrenzung(konfig: HillebrandKonfig, api_fixtures: dict[str, Any]) -> None:
    eng = konfig.model_copy(update={"api": konfig.api.model_copy(update={"limit_pro_minute": 2})})
    uhr = Uhr(start=5000.0)
    transport = FakeTransport([_ok(api_fixtures["token"]), _ok([]), _ok([]), _ok([])])
    api = _api(eng, transport, uhr)
    api.sendungen("A")  # Token + GET = 2 Aufrufe im Fenster
    assert uhr.geschlafen == []
    api.sendungen("B")  # dritter Aufruf: warten, bis der erste aus dem Fenster fällt
    assert len(uhr.geschlafen) == 1 and 0 < uhr.geschlafen[0] <= 60.0
    uhr.zeit += 120
    api.sendungen("C")
    assert len(uhr.geschlafen) == 1  # nach 2 Minuten kein Warten mehr


# ---------------------------------------------------------------- ApiQuelle


def test_apiquelle_aktualisiere(konfig: HillebrandKonfig, api_fixtures: dict[str, Any]) -> None:
    transport = FakeTransport([_ok(api_fixtures["token"]), _ok(api_fixtures["shipments"]), _ok(api_fixtures["events"])])
    quelle = ApiQuelle(_api(konfig, transport))
    original = Sendung(spedition="Hillebrand", referenz="DEMB00000", kundenreferenz="WS-2026-0042")
    ergebnis = quelle.aktualisiere(original)
    assert original.status == SendungStatus.UNBEKANNT
    assert ergebnis.status == SendungStatus.ZUSTELLUNG_GEPLANT  # collected + geplante Zustellung nach Abholung
    assert ergebnis.puenktlich is True
    assert ergebnis.quelle == "api"
    assert ergebnis.letzte_pruefung is not None
    assert ergebnis.geplante_abholung == date(2026, 10, 22)
    assert ergebnis.geplante_zustellung == date(2026, 10, 27)
    assert [m.code for m in ergebnis.meilensteine] == ["shprdy", "shpsupdepcnf", "shpcusarrpln"]
    assert [m.geplant for m in ergebnis.meilensteine] == [False, False, True]
    abholung = ergebnis.meilensteine[1]
    assert abholung.ort == "Treviso"
    assert abholung.zeitpunkt is not None and abholung.zeitpunkt.date() == date(2026, 10, 22)
    assert ergebnis.hinweise == []


def test_apiquelle_verspaetet_ohne_ereignisse(konfig: HillebrandKonfig, api_fixtures: dict[str, Any]) -> None:
    transport = FakeTransport([_ok(api_fixtures["token"]), _ok(api_fixtures["shipments_late"]), _ok({"items": []})])
    ergebnis = ApiQuelle(_api(konfig, transport)).aktualisiere(Sendung(spedition="Hillebrand", referenz="DEMB00009"))
    assert ergebnis.status == SendungStatus.VERSPAETET
    assert ergebnis.puenktlich is False
    assert ergebnis.geplante_zustellung == date(2026, 10, 29)
    assert ergebnis.meilensteine == []


def test_apiquelle_last_event_als_rueckfall(konfig: HillebrandKonfig, api_fixtures: dict[str, Any]) -> None:
    transport = FakeTransport([_ok(api_fixtures["token"]), _ok(api_fixtures["shipments"]), ApiAntwort(500, None, "kaputt")])
    ergebnis = ApiQuelle(_api(konfig, transport)).aktualisiere(Sendung(spedition="Hillebrand", referenz="DEMB00000"))
    assert ergebnis.status == SendungStatus.ABGEHOLT
    assert [m.code for m in ergebnis.meilensteine] == ["shpsupdepcnf"]
    assert any("Ereignisse nicht abrufbar" in h for h in ergebnis.hinweise)


def test_apiquelle_keine_sendung(konfig: HillebrandKonfig, api_fixtures: dict[str, Any]) -> None:
    transport = FakeTransport([_ok(api_fixtures["token"]), _ok(api_fixtures["shipments_leer"])])
    sendung = Sendung(spedition="Hillebrand", referenz="DEMB00000", status=SendungStatus.BESTAETIGT, quelle="mail")
    ergebnis = ApiQuelle(_api(konfig, transport)).aktualisiere(sendung)
    assert ergebnis.status == SendungStatus.BESTAETIGT
    assert ergebnis.quelle == "mail"
    assert ergebnis.hinweise == ["API: keine Sendung zu Referenz DEMB00000 gefunden"]


def test_apiquelle_http_fehler_nie_exception(konfig: HillebrandKonfig, api_fixtures: dict[str, Any]) -> None:
    transport = FakeTransport([_ok(api_fixtures["token"]), ApiAntwort(503, None, "unavailable")])
    ergebnis = ApiQuelle(_api(konfig, transport)).aktualisiere(Sendung(spedition="Hillebrand", referenz="DEMB00000"))
    assert ergebnis.status == SendungStatus.UNBEKANNT
    assert any("HTTP 503" in h for h in ergebnis.hinweise)

    def explodiert(*args: Any, **kwargs: Any) -> ApiAntwort:
        raise RuntimeError("Transport kaputt")

    api = HillebrandApi(konfig, TrackingKonfig(), sende=explodiert, umgebung=UMGEBUNG)
    ergebnis = ApiQuelle(api).aktualisiere(Sendung(spedition="Hillebrand", referenz="DEMB00000"))
    assert any("unerwarteter Fehler" in h and "Transport kaputt" in h for h in ergebnis.hinweise)


def test_apiquelle_ohne_referenz(konfig: HillebrandKonfig) -> None:
    transport = FakeTransport([])
    ergebnis = ApiQuelle(_api(konfig, transport)).aktualisiere(Sendung(spedition="Hillebrand", kundenreferenz="WS-1"))
    assert "ohne Hillebrand-Referenz" in ergebnis.hinweise[0]
    assert transport.aufrufe == []


def test_apiquelle_status_nicht_zugeordnet(konfig: HillebrandKonfig, api_fixtures: dict[str, Any]) -> None:
    transport = FakeTransport([_ok(api_fixtures["token"]), _ok({"items": [{"id": 1, "shipmentStatus": "weird"}]}), _ok([])])
    ergebnis = ApiQuelle(_api(konfig, transport)).aktualisiere(Sendung(spedition="Hillebrand", referenz="DEMB00000"))
    assert ergebnis.status == SendungStatus.UNBEKANNT
    assert any("nicht zugeordnet" in h for h in ergebnis.hinweise)


# ---------------------------------------------------------------- Hilfsfunktionen


def test_liste_aus() -> None:
    assert liste_aus([{"a": 1}, "x"]) == [{"a": 1}]
    assert liste_aus({"items": [{"a": 1}]}) == [{"a": 1}]
    assert liste_aus({"shipmentPagedList": [{"a": 1}, {"b": 2}]}) == [{"a": 1}, {"b": 2}]
    assert liste_aus({"totalCount": 0}) == []
    assert liste_aus(None) == []
    assert liste_aus("text") == []


def test_waehle_sendung() -> None:
    a = {"id": 1, "references": [{"referenceType": "File", "referenceValue": "DEMB00001"}]}
    b = {"id": 2, "references": [{"referenceType": "File", "referenceValue": "DEMB00002"}], "otherReferences": ["WS-7"]}
    assert _waehle_sendung([a, b], "demb00002") == (b, None)
    assert _waehle_sendung([a, b], "WS-7") == (b, None)
    gewaehlt, hinweis = _waehle_sendung([a, b], "DEMB00009")
    assert gewaehlt is a and hinweis and "ohne exakte Referenz" in hinweis
    assert _waehle_sendung([a], "DEMB00009") == (a, None)
    assert _waehle_sendung([], "x") == (None, None)
    gewaehlt, hinweis = _waehle_sendung([a, dict(a)], "DEMB00001")
    assert gewaehlt is a and hinweis and "2 Sendungen" in hinweis


def test_zeitpunkt_formate() -> None:
    sekunden = int(datetime(2026, 10, 22, 12, 0).timestamp())
    assert _zeitpunkt(sekunden) == datetime(2026, 10, 22, 12, 0)
    assert _zeitpunkt(sekunden * 1000) == datetime(2026, 10, 22, 12, 0)
    assert _zeitpunkt(str(sekunden)) == datetime(2026, 10, 22, 12, 0)
    assert _zeitpunkt("2026-10-27T10:00:00Z") == datetime(2026, 10, 27, 10, 0)
    assert _zeitpunkt(date(2026, 1, 2)) == datetime(2026, 1, 2)
    assert _zeitpunkt(None) is None
    assert _zeitpunkt("") is None
    assert _zeitpunkt(True) is None
    assert _zeitpunkt("kein datum") is None
