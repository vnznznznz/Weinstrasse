"""Stufe 2 (empfohlen): offizielle REST-API von Hillebrand Gori.

Grundlage: Developer-Portal ``https://developer.hillebrandgori.com/`` (OpenAPI 1.2.8), siehe
``docs/recherche/hillebrand_tracking.md`` Abschnitt 1.6. Ablauf:

1. Token: ``POST <token_url>`` mit ``Authorization: Basic base64(apikey:apisecret)`` und Body
   ``grant_type=password&username=…&password=…&scope=offline_access`` -> ``access_token``,
   ``refresh_token``, ``expires_in`` (laut FAQ 2 Stunden).
2. Erneuerung vor Ablauf mit ``grant_type=refresh_token``; schlägt sie fehl, erneut Passwort-Grant.
3. ``GET /v6/shipments?references=<Referenz>`` und ``GET /v6/shipments/{id}/events`` mit
   ``Authorization: Bearer …``; höchstens ``limit_pro_minute`` Aufrufe (gleitendes Fenster).

Zugangsdaten kommen **ausschließlich aus Umgebungsvariablen** (Namen in
``settings.tracking.api_key_env`` usw.), nie aus Dateien. Der HTTP-Transport ist eine injizierbare
Funktion ``sende(methode, url, *, headers, params, data, timeout) -> ApiAntwort``; Standard ist
``requests`` (erst beim ersten Aufruf importiert). Tests arbeiten mit einem Fake-Transport.

Feldnamen der Antworten stammen aus der OpenAPI-Spezifikation; wo die Recherche sie nicht
belegt, wird defensiv mit ``.get`` gelesen (``# TODO-PRUEFEN``).
"""

from __future__ import annotations

import base64
import logging
import os
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from auftragsautomat.config import TrackingKonfig
from auftragsautomat.models import Meilenstein, Sendung, SendungStatus
from auftragsautomat.tracking.basis import (
    HillebrandKonfig,
    ist_bekannt,
    jetzt,
    lade_hillebrand_konfig,
    mit_hinweis,
    normalisiere_referenz,
    status_aus_meilensteinen,
    zeit_schluessel,
)

logger = logging.getLogger(__name__)

__all__ = ["ApiAntwort", "ApiFehler", "ApiQuelle", "HillebrandApi", "Sende", "liste_aus"]


class ApiFehler(RuntimeError):
    """Fehler beim Zugriff auf die Hillebrand-API (Zugangsdaten, HTTP-Status, Antwortformat)."""


@dataclass
class ApiAntwort:
    status: int
    daten: Any = None  # geparstes JSON (dict/list) oder None
    text: str = ""

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


Sende = Callable[..., ApiAntwort]


def _sende_requests(
    methode: str,
    url: str,
    *,
    headers: Mapping[str, str],
    params: Mapping[str, Any] | None = None,
    data: Mapping[str, Any] | None = None,
    timeout: float = 30.0,
) -> ApiAntwort:
    """Standard-Transport mit ``requests`` (lazy import, damit Tests ohne Netz laufen)."""
    import requests

    try:
        antwort = requests.request(
            methode, url, headers=dict(headers), params=dict(params or {}), data=dict(data or {}), timeout=timeout
        )
    except requests.RequestException as exc:
        raise ApiFehler(f"Netzwerkfehler bei {methode} {url}: {exc}") from exc
    try:
        daten = antwort.json()
    except ValueError:
        daten = None
    return ApiAntwort(antwort.status_code, daten, antwort.text or "")


def liste_aus(daten: Any) -> list[dict[str, Any]]:
    """Listenelemente aus einer API-Antwort (nackte Liste oder Container ``items``/``data``/…).

    TODO-PRUEFEN: Feldname des Listen-Containers (``shipmentPagedList_ShipmentSummary_``,
    ``PagedList_EventsSummary_``) am Live-System prüfen.
    """
    if daten is None:
        return []
    if isinstance(daten, list):
        return [d for d in daten if isinstance(d, dict)]
    if isinstance(daten, dict):
        for schluessel in ("items", "data", "results", "shipments", "events", "value", "content"):
            wert = daten.get(schluessel)
            if isinstance(wert, list):
                return [d for d in wert if isinstance(d, dict)]
        for wert in daten.values():
            if isinstance(wert, list) and wert and all(isinstance(d, dict) for d in wert):
                return list(wert)
    return []


@dataclass
class _Token:
    access: str
    refresh: str | None
    ablauf: float  # Zeitpunkt (Uhr des Clients), ab dem das Token nicht mehr benutzt wird


@dataclass
class _Zugang:
    api_key: str | None
    api_secret: str | None
    benutzer: str | None
    passwort: str | None
    fehlend: list[str] = field(default_factory=list)


class HillebrandApi:
    """Client für ``https://api.hillebrandgori.com/`` (nur lesend).

    ``konfig``: Inhalt der ``tracking_hillebrand.yaml`` (Abschnitt ``api``); ``tracking``:
    ``settings.tracking`` mit den Namen der Umgebungsvariablen; ``sende``: Transportfunktion
    (Standard ``requests``); ``umgebung``: Mapping der Umgebungsvariablen (Standard ``os.environ``).
    """

    def __init__(
        self,
        konfig: HillebrandKonfig | None = None,
        tracking: TrackingKonfig | None = None,
        *,
        sende: Sende | None = None,
        umgebung: Mapping[str, str] | None = None,
        uhr: Callable[[], float] = time.monotonic,
        schlafe: Callable[[float], None] = time.sleep,
    ) -> None:
        self._konfig = konfig or lade_hillebrand_konfig()
        self._api = self._konfig.api
        self._tracking = tracking or TrackingKonfig()
        self._sende: Sende = sende or _sende_requests
        self._umgebung: Mapping[str, str] = os.environ if umgebung is None else umgebung
        self._uhr = uhr
        self._schlafe = schlafe
        self._token: _Token | None = None
        self._aufrufe: deque[float] = deque()
        self.anzahl_aufrufe = 0  # alle HTTP-Aufrufe (Token + API), für Tests und Protokoll

    @property
    def konfig(self) -> HillebrandKonfig:
        return self._konfig

    # -- Zugangsdaten -------------------------------------------------------

    def _zugang(self) -> _Zugang:
        namen = {
            "api_key": self._tracking.api_key_env,
            "api_secret": self._tracking.api_secret_env,
            "benutzer": self._tracking.api_benutzer_env,
            "passwort": self._tracking.api_passwort_env,
        }
        werte = {feld: (self._umgebung.get(var) or "").strip() or None for feld, var in namen.items()}
        fehlend = [var for feld, var in namen.items() if werte[feld] is None]
        return _Zugang(werte["api_key"], werte["api_secret"], werte["benutzer"], werte["passwort"], fehlend)

    def fehlende_variablen(self) -> list[str]:
        """Namen der Umgebungsvariablen, die noch nicht gesetzt sind."""
        return self._zugang().fehlend

    def zugangsdaten_vorhanden(self) -> bool:
        return not self._zugang().fehlend

    # -- Token --------------------------------------------------------------

    def token(self) -> str:
        """Gültiges Access-Token (holt oder erneuert bei Bedarf)."""
        if self._token is not None and self._uhr() < self._token.ablauf:
            return self._token.access
        if self._token is not None and self._token.refresh:
            try:
                self._hole_token({"grant_type": "refresh_token", "refresh_token": self._token.refresh})
                return self._token.access
            except ApiFehler as exc:
                logger.warning("Token-Erneuerung fehlgeschlagen, neuer Login: %s", exc)
                self._token = None
        zugang = self._zugang()
        if zugang.fehlend:
            raise ApiFehler("Hillebrand-API: Umgebungsvariablen fehlen: " + ", ".join(zugang.fehlend))
        self._hole_token({"grant_type": "password", "username": zugang.benutzer, "password": zugang.passwort})
        assert self._token is not None
        return self._token.access

    def _hole_token(self, felder: dict[str, Any]) -> None:
        zugang = self._zugang()
        if zugang.api_key is None or zugang.api_secret is None:
            raise ApiFehler("Hillebrand-API: API-Key/Secret fehlen: " + ", ".join(zugang.fehlend))
        basic = base64.b64encode(f"{zugang.api_key}:{zugang.api_secret}".encode()).decode("ascii")
        headers = {
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        }
        daten = {**felder, "scope": self._api.scope}
        self._warte_auf_ratenfenster()
        antwort = self._sende(
            "POST", self._api.token_url, headers=headers, data=daten, timeout=self._api.timeout_sekunden
        )
        if not antwort.ok or not isinstance(antwort.daten, dict):
            raise ApiFehler(f"Token-Anfrage fehlgeschlagen (HTTP {antwort.status}): {_kurz(antwort.text)}")
        access = antwort.daten.get("access_token")
        if not access:
            raise ApiFehler("Token-Antwort ohne access_token")
        gueltig = antwort.daten.get("expires_in")
        try:
            sekunden = float(gueltig) if gueltig is not None else float(self._api.token_gueltigkeit_sekunden)
        except (TypeError, ValueError):
            sekunden = float(self._api.token_gueltigkeit_sekunden)
        bisheriges_refresh = felder.get("refresh_token") if felder.get("grant_type") == "refresh_token" else None
        # 60 s Sicherheitsabstand, damit kein Aufruf mit gerade ablaufendem Token startet
        self._token = _Token(
            access=str(access),
            refresh=antwort.daten.get("refresh_token") or bisheriges_refresh,
            ablauf=self._uhr() + max(sekunden - 60.0, 1.0),
        )
        logger.info("Hillebrand-API: Token erhalten (%s, gültig %.0f s)", felder.get("grant_type"), sekunden)

    # -- Ratenbegrenzung ----------------------------------------------------

    def _warte_auf_ratenfenster(self) -> None:
        limit = max(int(self._api.limit_pro_minute), 1)
        nun = self._uhr()
        while self._aufrufe and nun - self._aufrufe[0] >= 60.0:
            self._aufrufe.popleft()
        if len(self._aufrufe) >= limit:
            pause = 60.0 - (nun - self._aufrufe[0])
            if pause > 0:
                logger.info("Hillebrand-API: Ratenlimit (%d/min) erreicht, warte %.1f s", limit, pause)
                self._schlafe(pause)
            nun = self._uhr()
            while self._aufrufe and nun - self._aufrufe[0] >= 60.0:
                self._aufrufe.popleft()
        self._aufrufe.append(nun)
        self.anzahl_aufrufe += 1

    # -- Aufrufe ------------------------------------------------------------

    def _url(self, pfad: str) -> str:
        return self._api.basis_url.rstrip("/") + "/" + pfad.lstrip("/")

    def _get(self, pfad: str, params: Mapping[str, Any] | None = None) -> Any:
        for versuch in (1, 2):
            headers = {"Authorization": f"Bearer {self.token()}", "Accept": "application/json"}
            self._warte_auf_ratenfenster()
            antwort = self._sende(
                "GET", self._url(pfad), headers=headers, params=params, timeout=self._api.timeout_sekunden
            )
            if antwort.status == 401 and versuch == 1:
                logger.info("Hillebrand-API: 401, Token wird erneuert")
                self._token = None
                continue
            if not antwort.ok:
                raise ApiFehler(f"HTTP {antwort.status} bei GET {pfad}: {_kurz(antwort.text)}")
            return antwort.daten
        raise ApiFehler(f"GET {pfad}: Anmeldung wiederholt abgelehnt (401)")

    def sendungen(self, referenz: str) -> list[dict[str, Any]]:
        """``GET /v6/shipments?references=<referenz>`` (Kundenreferenz, Buchungs-, Container-, B/L-Nummer)."""
        referenz = (referenz or "").strip()
        if not referenz:
            raise ApiFehler("Sendungssuche ohne Referenz")
        params: dict[str, Any] = {
            "references": referenz,
            "pageSize": self._api.seitengroesse,
            "includeInactiveShipments": "true" if self._api.inaktive_einschliessen else "false",
        }
        return liste_aus(self._get(self._api.pfad_sendungen, params))

    def sendung(self, sendung_id: int | str) -> dict[str, Any] | None:
        """``GET /v6/shipments/{id}``."""
        daten = self._get(f"{self._api.pfad_sendungen.rstrip('/')}/{sendung_id}")
        return daten if isinstance(daten, dict) else None

    def ereignisse(self, sendung_id: int | str, seit: datetime | int | None = None) -> list[dict[str, Any]]:
        """``GET /v6/shipments/{id}/events`` (optional nur Änderungen seit ``seit``: ``lastUpdateAfter``)."""
        params: dict[str, Any] = {"pageSize": self._api.seitengroesse}
        if seit is not None:
            params["lastUpdateAfter"] = int(seit.timestamp()) if isinstance(seit, datetime) else int(seit)
        pfad = self._api.pfad_ereignisse.format(id=sendung_id)
        return liste_aus(self._get(pfad, params))


def _kurz(text: str, laenge: int = 200) -> str:
    t = " ".join((text or "").split())
    return t if len(t) <= laenge else t[: laenge - 1] + "…"


# ---------------------------------------------------------------------------
# Abbildung der API-Daten auf das Sendungsmodell
# ---------------------------------------------------------------------------


def _zeitpunkt(wert: Any) -> datetime | None:
    """Unix-Sekunden (laut Developer-Portal), Millisekunden oder ISO-Text -> datetime."""
    if wert is None or wert == "":
        return None
    if isinstance(wert, datetime):
        return wert
    if isinstance(wert, date):
        return datetime(wert.year, wert.month, wert.day)
    if isinstance(wert, bool):
        return None
    if isinstance(wert, (int, float)):
        sekunden = float(wert)
        if sekunden > 1e11:  # Millisekunden
            sekunden /= 1000.0
        try:
            return datetime.fromtimestamp(sekunden)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(wert).strip()
    if text.isdigit():
        return _zeitpunkt(int(text))
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _datum(wert: Any) -> date | None:
    z = _zeitpunkt(wert)
    return z.date() if z else None


def _referenzwerte(sendung: Mapping[str, Any]) -> set[str]:
    werte: set[str] = set()
    for feld in ("references", "otherReferences"):
        eintraege = sendung.get(feld)
        if isinstance(eintraege, list):
            for e in eintraege:
                wert = e.get("referenceValue") if isinstance(e, dict) else e
                norm = normalisiere_referenz(str(wert)) if wert else None
                if norm:
                    werte.add(norm)
    for feld in ("shipmentReference", "reference", "fileNumber", "number"):  # TODO-PRUEFEN: Feldnamen
        wert = sendung.get(feld)
        norm = normalisiere_referenz(str(wert)) if wert else None
        if norm:
            werte.add(norm)
    return werte


def _waehle_sendung(treffer: list[dict[str, Any]], referenz: str) -> tuple[dict[str, Any] | None, str | None]:
    """Die Sendung, deren Referenzen die gesuchte enthalten; sonst der einzige/erste Treffer mit Hinweis."""
    if not treffer:
        return None, None
    gesucht = normalisiere_referenz(referenz)
    passend = [s for s in treffer if gesucht and gesucht in _referenzwerte(s)]
    if len(passend) == 1:
        return passend[0], None
    if len(passend) > 1:
        return passend[0], f"API: {len(passend)} Sendungen mit Referenz {referenz}, erste verwendet"
    if len(treffer) == 1:
        return treffer[0], None
    return treffer[0], f"API: {len(treffer)} Treffer für {referenz} ohne exakte Referenz, erster verwendet"


def _ist_geplant(code: str, text: str, kategorie: str, konfig: HillebrandKonfig) -> bool:
    basis = code[:-3] if code.endswith("chg") else code
    if basis.endswith("pln"):
        return True
    muster = konfig.muster_fuer_code(code)
    if muster is not None:
        return muster.geplant
    return "plan" in kategorie or "estimat" in kategorie or konfig.ist_geplant_text(text)


def _meilenstein_aus_ereignis(e: Mapping[str, Any], konfig: HillebrandKonfig) -> Meilenstein | None:
    code = str(e.get("code") or e.get("eventCode") or "").strip()  # TODO-PRUEFEN: EventSummary.code
    text = str(e.get("text") or e.get("description") or e.get("template") or "").strip()
    if not code and not text:
        return None
    kategorie = str(e.get("category") or "").lower()
    variablen = e.get("templateVariables")
    ort = None
    if isinstance(variablen, dict):
        ort = variablen.get("shipFromLocation") or variablen.get("shipToLocation") or variablen.get("location")
    return Meilenstein(
        code=code or "unbekannt",
        text=text or code,
        zeitpunkt=_zeitpunkt(e.get("date") or e.get("eventDate") or e.get("lastUpdate")),
        ort=str(ort) if ort else (str(e.get("location")) if e.get("location") else None),
        geplant=_ist_geplant(code.lower(), text, kategorie, konfig),
    )


class ApiQuelle:
    """Tracking-Quelle über die offizielle API. ``verfuegbar()`` = alle Umgebungsvariablen gesetzt."""

    name = "api"

    def __init__(self, api: HillebrandApi, konfig: HillebrandKonfig | None = None) -> None:
        self._api = api
        self._konfig = konfig or api.konfig

    def verfuegbar(self) -> bool:
        return self._api.zugangsdaten_vorhanden()

    def aktualisiere(self, sendung: Sendung) -> Sendung:
        try:
            return self._aktualisiere(sendung)
        except ApiFehler as exc:
            logger.warning("Hillebrand-API: %s", exc)
            return mit_hinweis(sendung, f"API: {exc}")
        except Exception as exc:  # nie nach außen
            logger.exception("Hillebrand-API: unerwarteter Fehler")
            return mit_hinweis(sendung, f"API: unerwarteter Fehler {type(exc).__name__}: {exc}")

    def _aktualisiere(self, sendung: Sendung) -> Sendung:
        if not self.verfuegbar():
            fehlend = ", ".join(self._api.fehlende_variablen())
            return mit_hinweis(sendung, f"API nicht verfügbar: Umgebungsvariablen fehlen ({fehlend})")
        referenz = (sendung.referenz or "").strip()
        if not referenz:
            return mit_hinweis(sendung, "API: Sendung ohne Hillebrand-Referenz, Suche nicht möglich")
        treffer = self._api.sendungen(referenz)
        daten, auswahl_hinweis = _waehle_sendung(treffer, referenz)
        if daten is None:
            return mit_hinweis(sendung, f"API: keine Sendung zu Referenz {referenz} gefunden")
        hinweise = list(sendung.hinweise)
        if auswahl_hinweis and auswahl_hinweis not in hinweise:
            hinweise.append(auswahl_hinweis)

        status_roh = daten.get("shipmentStatus") or daten.get("status")  # TODO-PRUEFEN: Feldname shipmentStatus
        status_api = _status_aus_mapping(status_roh, self._konfig)
        puenktlich = _puenktlich(daten.get("onTimeStatus"), self._konfig)
        if puenktlich is None:
            puenktlich = sendung.puenktlich

        meilensteine: list[Meilenstein] = []
        sendung_id = daten.get("id") or daten.get("shipmentId")  # TODO-PRUEFEN: Feldname id
        if sendung_id is not None:
            try:
                for e in self._api.ereignisse(sendung_id):
                    m = _meilenstein_aus_ereignis(e, self._konfig)
                    if m is not None:
                        meilensteine.append(m)
            except ApiFehler as exc:
                hinweise.append(f"API: Ereignisse nicht abrufbar ({exc})")
        if not meilensteine and isinstance(daten.get("lastEvent"), dict):
            m = _meilenstein_aus_ereignis(daten["lastEvent"], self._konfig)
            if m is not None:
                meilensteine.append(m)
        meilensteine.sort(key=lambda m: (zeit_schluessel(m.zeitpunkt), m.geplant))

        status = status_aus_meilensteinen(
            meilensteine, self._konfig, puenktlich=puenktlich, basis=status_api or SendungStatus.UNBEKANNT
        )
        if not ist_bekannt(status):
            hinweise.append(f"API: Status '{status_roh}' nicht zugeordnet und keine Ereignisse")
            return sendung.model_copy(update={"hinweise": hinweise, "letzte_pruefung": jetzt()})

        geplante_abholung = (
            _datum(daten.get("actualDepartureDate"))
            or _datum(daten.get("estimatedDepartureDate"))
            or _datum(daten.get("readyDate"))
            or sendung.geplante_abholung
        )
        geplante_zustellung = (
            _datum(daten.get("actualArrivalDate"))
            or _datum(daten.get("estimatedArrivalDate"))
            or sendung.geplante_zustellung
        )
        return sendung.model_copy(
            update={
                "status": status,
                "puenktlich": puenktlich,
                "meilensteine": meilensteine or list(sendung.meilensteine),
                "geplante_abholung": geplante_abholung,
                "geplante_zustellung": geplante_zustellung,
                "quelle": "api",
                "letzte_pruefung": jetzt(),
                "hinweise": hinweise,
            }
        )


def _status_aus_mapping(status_roh: Any, konfig: HillebrandKonfig) -> SendungStatus | None:
    if status_roh is None:
        return None
    text = str(status_roh).strip()
    for schluessel, status in konfig.status_mapping.items():
        if schluessel.lower() == text.lower():
            return status
    return None


def _puenktlich(on_time_roh: Any, konfig: HillebrandKonfig) -> bool | None:
    if on_time_roh is None:
        return None
    if isinstance(on_time_roh, bool):
        return on_time_roh
    text = str(on_time_roh).strip()
    for schluessel, wert in konfig.on_time_mapping.items():
        if schluessel.lower() == text.lower():
            return wert
    return None
