"""Microsoft-Graph-Backend: HTTP-Client, Excel (Workbook-API), Mail (Entwürfe, Posteingang), Dateien.

Grundlage: ``docs/recherche/graph_excel_outlook.md`` und ``excel_lokal_und_vorlagen.md`` (Teil B).

:class:`GraphClient`
    * Token je Anfrage vom ``token_provider`` (``GraphAnmeldung.token``), ``Authorization: Bearer``.
    * Transport ist injizierbar (``sende(method, url, headers=, json=, data=, params=, stream=)``
      -> ``(status, headers, body)``); Standard ``requests.Session``. Tests fahren mit Fake-Transport.
    * Einfache Ratenbegrenzung (``anfragen_pro_sekunde``), Anfragen strikt sequenziell (Best Practice
      der Excel-API: "only send the next request after receiving a successful response").
    * Wiederholung bei 429/503/504 und transienten Second-Level-Codes mit ``Retry-After`` (sonst
      2/4/8/16 s), höchstens ``max_versuche`` (5).
    * Fehlerabbildung aus ``{"error": {"code", "innerError": {"code"}}}``: ``accessConflict``/
      ``invalidSessionAccessConflict``/``conflictUncategorized``/HTTP 423 -> :class:`M365Konflikt` (NICHT
      wiederholen; ``wartezeit_sekunden``), ``itemNotFound``/404 -> :class:`M365NichtGefunden`,
      401/``invalidSessionAuthentication`` -> :class:`M365NichtAngemeldet`, sonst :class:`M365Fehler`.

:class:`GraphExcelBackend`
    Workbook-Session mit ``persistChanges: true`` je Datei (``sitzung(datei)``-Kontextmanager; Header
    ``workbook-session-id``; bei 404 ``invalidSession*`` wird die Session einmal erneuert; ohne Session
    sind die Aufrufe "sessionless" und werden ebenfalls gespeichert). Range-URLs werden
    URL-kodiert (Blattname mit Umlauten/Leerzeichen). ``schreibe_bereich`` -> ``PATCH {values,
    numberFormat}`` mit ``null`` für unveränderte Zellen; Boolesche Werte als JSON-Boolean (Excel-365-
    Kontrollkästchen), Datumswerte als Excel-Serienzahl mit ``numberFormat "DD.MM.YYYY"``.

:class:`GraphMailBackend`
    ``/me/messages`` bzw. ``/users/{postfach}/messages``. Entwürfe per ``POST`` (Graph legt sie in
    "Entwürfe" ab, ``isDraft`` implizit); **es wird nie gesendet** (``/send`` ist nicht implementiert).
    Anhänge < 3 MB als ``fileAttachment`` (Base64), größere per ``createUploadSession`` + ``PUT``-Chunks
    (``Content-Range``, kein Authorization-Header, < 4 MB je PUT). Suche per ``$filter`` (``receivedDateTime
    ge``, optional ``from/emailAddress/address eq``) mit ``$orderby``; bei ``betreff_enthaelt`` per
    ``$search`` (ohne ``$orderby``/``$filter``, Rest clientseitig).

:class:`GraphDateiBackend`
    ``/drives/{drive}/items/{item}:/{pfad}:/children|content``; Ordner anlegen per ``POST children``
    mit ``folder: {}`` und ``@microsoft.graph.conflictBehavior`` (idempotent); Upload < 4 MB per ``PUT
    content`` (``conflictBehavior=replace``), größer per Upload-Session (Chunks als Vielfache von 320 KiB).
"""

from __future__ import annotations

import base64
import json as json_modul
import logging
import mimetypes
import re
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import quote

from auftragsautomat.config import DateiRefKonfig
from auftragsautomat.m365 import adressierung
from auftragsautomat.m365.basis import (
    DateiInfo,
    M365Fehler,
    M365Konflikt,
    M365NichtAngemeldet,
    M365NichtGefunden,
    Mail,
    MailAnhang,
    MailKopf,
    datei_schluessel,
    datum_zu_serienzahl,
    html_zu_text,
    parse_adresse,
    serienzahl_zu_datum,
    sicherer_dateiname,
)
from auftragsautomat.models import EmailEntwurf

__all__ = [
    "ANHANG_KLEIN_MAX",
    "Antwort",
    "DATEI_KLEIN_MAX",
    "GRAPH_BASIS",
    "GraphClient",
    "GraphDateiBackend",
    "GraphExcelBackend",
    "GraphMailBackend",
    "Transport",
    "UPLOAD_CHUNK",
    "datum_zu_serienzahl",
    "requests_transport",
    "serienzahl_zu_datum",
]

logger = logging.getLogger(__name__)

GRAPH_BASIS = "https://graph.microsoft.com/v1.0"

#: Mail-Anhänge unter 3 MB per einfachem POST (Graph-Doku "Add attachment").
ANHANG_KLEIN_MAX = 3 * 1024 * 1024
#: Dateien unter 4 MB per ``PUT …/content`` (Graph-Doku "Upload small files").
DATEI_KLEIN_MAX = 4 * 1024 * 1024
#: Chunk-Größe für Upload-Sessions: Vielfaches von 320 KiB (OneDrive-Regel) und < 4 MB (Outlook-Regel).
UPLOAD_CHUNK = 10 * 320 * 1024

_KONFLIKT_CODES = frozenset({"accessconflict", "invalidsessionaccessconflict", "conflictuncategorized"})
_NICHT_GEFUNDEN_CODES = frozenset({"itemnotfound", "notfounduncategorized", "erroritemnotfound"})
_NICHT_ANGEMELDET_CODES = frozenset({"invalidsessionauthentication", "unauthorizeduncategorized", "invalidauthenticationtoken"})
_TRANSIENT_CODES = frozenset(
    {
        "transientfailure",
        "toomanyrequestsuncategorized",
        "serviceunavailableuncategorized",
        "gatewaytimeoutuncategorized",
        "servicenotavailable",
        "activitylimitreached",
    }
)
_WIEDERHOLBARE_STATUS = frozenset({429, 503, 504})

Transport = Callable[..., tuple[int, dict[str, str], bytes]]


# ---------------------------------------------------------------------------
# Transport und Client
# ---------------------------------------------------------------------------


def requests_transport(session: Any | None = None, *, zeitlimit: float = 60.0) -> Transport:
    """Standard-Transport über ``requests.Session`` (lazy importiert)."""
    import requests

    sitzung = session or requests.Session()

    def sende(
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json: Any = None,
        data: bytes | None = None,
        params: dict[str, str] | None = None,
        stream: bool = False,
    ) -> tuple[int, dict[str, str], bytes]:
        antwort = sitzung.request(
            method, url, headers=headers, json=json, data=data, params=params, timeout=zeitlimit, stream=stream
        )
        return antwort.status_code, dict(antwort.headers), antwort.content

    return sende


@dataclass
class Antwort:
    """Rohantwort einer Graph-Anfrage (Header-Namen kleingeschrieben)."""

    status: int
    headers: dict[str, str] = field(default_factory=dict)
    roh: bytes = b""

    @property
    def json(self) -> dict[str, Any]:
        if not self.roh:
            return {}
        try:
            daten = json_modul.loads(self.roh.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise M365Fehler(f"Graph-Antwort ist kein JSON (HTTP {self.status}).", status=self.status) from exc
        return daten if isinstance(daten, dict) else {"value": daten}

    @property
    def ist_json(self) -> bool:
        return "json" in self.headers.get("content-type", "").lower()


def _fehler_aus(antwort: Antwort, methode: str, url: str) -> M365Fehler:
    code = inner = meldung = ""
    if antwort.ist_json or antwort.roh[:1] == b"{":
        try:
            fehler = antwort.json.get("error") or {}
        except M365Fehler:
            fehler = {}
        code = str(fehler.get("code") or "")
        inner = str((fehler.get("innerError") or {}).get("code") or "")
        meldung = str(fehler.get("message") or "")
    codes = {c.lower() for c in (code, inner) if c}
    wartezeit = _retry_after(antwort.headers)
    text = f"{methode} {url}: {meldung or antwort.roh[:200].decode('utf-8', 'replace') or 'ohne Meldung'}"
    args: dict[str, Any] = {"status": antwort.status, "code": code or None, "inner_code": inner or None}
    if antwort.status == 423 or codes & _KONFLIKT_CODES:
        return M365Konflikt(
            "Datei gesperrt oder von jemand anderem in Bearbeitung – später erneut versuchen. " + text,
            wartezeit_sekunden=wartezeit or 600.0,
            **args,
        )
    if antwort.status == 401 or codes & _NICHT_ANGEMELDET_CODES:
        return M365NichtAngemeldet("Graph lehnt das Token ab – bitte neu anmelden. " + text, **args)
    if antwort.status == 404 or codes & _NICHT_GEFUNDEN_CODES:
        return M365NichtGefunden("Nicht gefunden: " + text, **args)
    return M365Fehler("Graph-Fehler: " + text, wartezeit_sekunden=wartezeit, **args)


def _retry_after(headers: dict[str, str]) -> float | None:
    wert = headers.get("retry-after")
    if not wert:
        return None
    try:
        return max(0.0, float(wert))
    except ValueError:
        return None  # HTTP-Datum: nicht ausgewertet, Rückfall auf exponentielles Warten


class GraphClient:
    """Dünner HTTP-Client für Graph v1.0 mit Ratenbegrenzung, Wiederholung und Fehlerabbildung."""

    def __init__(
        self,
        token_provider: Callable[[], str],
        *,
        basis: str = GRAPH_BASIS,
        sende: Transport | None = None,
        anfragen_pro_sekunde: float = 4.0,
        max_versuche: int = 5,
        schlaf: Callable[[float], Any] = time.sleep,
        uhr: Callable[[], float] = time.monotonic,
    ) -> None:
        self.token_provider = token_provider
        self.basis = basis.rstrip("/")
        self._sende: Transport = sende or requests_transport()
        self.max_versuche = max(1, max_versuche)
        self._intervall = 1.0 / anfragen_pro_sekunde if anfragen_pro_sekunde and anfragen_pro_sekunde > 0 else 0.0
        self._schlaf = schlaf
        self._uhr = uhr
        self._letzte_anfrage: float | None = None

    # -- Komfortmethoden ------------------------------------------------------------

    def get(self, pfad: str, **kw: Any) -> Any:
        return self.anfrage("GET", pfad, **kw).daten

    def post(self, pfad: str, **kw: Any) -> Any:
        return self.anfrage("POST", pfad, **kw).daten

    def patch(self, pfad: str, **kw: Any) -> Any:
        return self.anfrage("PATCH", pfad, **kw).daten

    def put(self, pfad: str, **kw: Any) -> Any:
        return self.anfrage("PUT", pfad, **kw).daten

    def delete(self, pfad: str, **kw: Any) -> Any:
        return self.anfrage("DELETE", pfad, **kw).daten

    # -- Kern --------------------------------------------------------------------

    def url(self, pfad: str) -> str:
        if pfad.startswith(("http://", "https://")):
            return pfad
        return f"{self.basis}/{pfad.lstrip('/')}"

    def anfrage(
        self,
        methode: str,
        pfad: str,
        *,
        headers: dict[str, str] | None = None,
        json: Any = None,
        data: bytes | None = None,
        params: dict[str, str] | None = None,
        roh: bool = False,
        ohne_token: bool = False,
        stream: bool = False,
    ) -> _AntwortMitDaten:
        """Führt die Anfrage aus (mit Wiederholung) und liefert Status, Header und Daten
        (``dict`` bei JSON, sonst ``bytes``; ``roh=True`` erzwingt ``bytes``)."""
        url = self.url(pfad)
        kopf: dict[str, str] = {"Accept": "application/json"} if not roh else {}
        if not ohne_token:
            kopf["Authorization"] = f"Bearer {self.token_provider()}"
        if headers:
            kopf.update(headers)
        if json is not None and "Content-Type" not in kopf:
            kopf["Content-Type"] = "application/json"
        versuch = 0
        while True:
            versuch += 1
            self._warte_auf_rate()
            status, roh_headers, body = self._sende(
                methode, url, headers=kopf, json=json, data=data, params=params, stream=stream
            )
            antwort = Antwort(status, {str(k).lower(): str(v) for k, v in (roh_headers or {}).items()}, body or b"")
            if 200 <= status < 300:
                if roh or not antwort.ist_json:
                    return _AntwortMitDaten(antwort.status, antwort.headers, antwort.roh, antwort.roh)
                return _AntwortMitDaten(antwort.status, antwort.headers, antwort.roh, antwort.json)
            fehler = _fehler_aus(antwort, methode, url)
            if isinstance(fehler, M365Konflikt):
                logger.warning("%s", fehler)
                raise fehler
            codes = {c.lower() for c in (fehler.code, fehler.inner_code) if c}
            wiederholbar = status in _WIEDERHOLBARE_STATUS or bool(codes & _TRANSIENT_CODES)
            if wiederholbar and versuch < self.max_versuche:
                pause = _retry_after(antwort.headers)
                if pause is None:
                    pause = float(min(2**versuch, 60))
                logger.info(
                    "Graph %s %s: HTTP %s (%s) – Versuch %s/%s nach %.1f s",
                    methode, url, status, fehler.inner_code or fehler.code or "-", versuch + 1, self.max_versuche, pause,
                )
                self._schlaf(pause)
                continue
            raise fehler

    def _warte_auf_rate(self) -> None:
        if self._intervall <= 0:
            return
        jetzt = self._uhr()
        if self._letzte_anfrage is not None:
            rest = self._intervall - (jetzt - self._letzte_anfrage)
            if rest > 0:
                self._schlaf(rest)
                jetzt = self._uhr()
        self._letzte_anfrage = jetzt

    def seiten(self, pfad: str, *, params: dict[str, str] | None = None, headers: dict[str, str] | None = None,
               maximal: int | None = None) -> Iterator[dict[str, Any]]:
        """Iteriert über ``value`` aller Seiten (``@odata.nextLink`` wird komplett übernommen)."""
        url: str | None = pfad
        anzahl = 0
        while url:
            daten = self.get(url, params=params, headers=headers)
            params = None  # nextLink enthält die Parameter bereits
            for eintrag in daten.get("value") or []:
                yield eintrag
                anzahl += 1
                if maximal is not None and anzahl >= maximal:
                    return
            url = daten.get("@odata.nextLink")


@dataclass
class _AntwortMitDaten(Antwort):
    daten: Any = None


# ---------------------------------------------------------------------------
# Gemeinsame Hilfen
# ---------------------------------------------------------------------------


def _zeit(text: str | None) -> datetime | None:
    """Graph-Zeitstempel (``2026-10-18T10:00:00.1234567Z``) -> aware ``datetime`` (UTC)."""
    if not text:
        return None
    wert = text.strip()
    wert = re.sub(r"(\.\d{6})\d+", r"\1", wert)
    if wert.endswith("Z"):
        wert = wert[:-1] + "+00:00"
    try:
        ergebnis = datetime.fromisoformat(wert)
    except ValueError:
        return None
    return ergebnis if ergebnis.tzinfo else ergebnis.replace(tzinfo=UTC)


def _utc(zeit: datetime) -> datetime:
    if zeit.tzinfo is None:
        zeit = zeit.astimezone()  # naive = lokale Zeit
    return zeit.astimezone(UTC)


class _IdCache:
    """Löst ``DateiRefKonfig`` einmal pro Prozess in (drive_id, item_id) auf."""

    def __init__(self, client: GraphClient) -> None:
        self.client = client
        self._ids: dict[str, tuple[str, str]] = {}

    def ids(self, ref: DateiRefKonfig) -> tuple[str, str]:
        if ref.drive_id and ref.item_id:
            return ref.drive_id, ref.item_id
        schluessel = datei_schluessel(ref)
        if schluessel not in self._ids:
            aufgeloest = adressierung.aufloesen(ref, self.client)
            self._ids[schluessel] = (str(aufgeloest.drive_id), str(aufgeloest.item_id))
        return self._ids[schluessel]


# ---------------------------------------------------------------------------
# Excel
# ---------------------------------------------------------------------------


def _json_zellwert(wert: Any) -> Any:
    """Python-Wert -> JSON-Wert für ``values`` (None bleibt null = unverändert)."""
    if wert is None or isinstance(wert, bool | int | float | str):
        return wert
    if isinstance(wert, datetime | date):
        return datum_zu_serienzahl(wert)
    if isinstance(wert, Decimal):
        return int(wert) if wert == wert.to_integral_value() else float(wert)
    return str(wert)


class GraphExcelBackend:
    """Excel-Zugriff über die Workbook-API (nur ``.xlsx`` in OneDrive for Business/SharePoint)."""

    DATUMSFORMAT = "DD.MM.YYYY"
    ZEITFORMAT = "DD.MM.YYYY HH:MM"

    def __init__(self, client: GraphClient) -> None:
        self.client = client
        self._ids = _IdCache(client)
        self._sitzungen: dict[str, str] = {}

    # -- Adressen -------------------------------------------------------------------

    def _mappe(self, datei: DateiRefKonfig) -> str:
        drive_id, item_id = self._ids.ids(datei)
        return f"/drives/{drive_id}/items/{item_id}/workbook"

    @staticmethod
    def _blatt_url(blatt: str) -> str:
        return quote(blatt, safe="")

    @staticmethod
    def _range_url(adresse: str) -> str:
        # Graph-Beispiele: range(address='A1:B2'); Blattname in der Adresse erlaubt ('Blatt'!D7:K7).
        return "range(address='" + quote(adresse, safe="!:'$") + "')"

    # -- Sitzungen ----------------------------------------------------------------

    @contextmanager
    def sitzung(self, datei: DateiRefKonfig) -> Iterator[None]:
        """Persistente Workbook-Session (``persistChanges: true``) für mehrere Zugriffe; ``closeSession``
        am Ende. Verschachtelte Aufrufe nutzen die bestehende Session."""
        schluessel = datei_schluessel(datei)
        if schluessel in self._sitzungen:
            yield
            return
        self._sitzung_anlegen(datei)
        try:
            yield
        finally:
            self._sitzung_schliessen(datei)

    def _sitzung_anlegen(self, datei: DateiRefKonfig) -> str:
        # TODO-PRUEFEN: Welche Second-Level-Codes der Tenant bei abgelaufener Session liefert (Doku: 404 +
        # invalidSessionNotFound/invalidSessionReCreatable); die Erneuerung greift bei allen "invalidSession*"
        # außer invalidSessionAccessConflict. Kontrollkästchen-Zellen: JSON-Boolean soll das Kästchen erhalten
        # (Checkliste C2/C3 der Recherche, an einer KOPIE testen).
        antwort = self.client.post(f"{self._mappe(datei)}/createSession", json={"persistChanges": True})
        sitzung = str(antwort.get("id") or "")
        if not sitzung:
            raise M365Fehler("createSession lieferte keine Session-ID.")
        self._sitzungen[datei_schluessel(datei)] = sitzung
        logger.debug("Workbook-Session %s… für %s angelegt", sitzung[:12], datei_schluessel(datei))
        return sitzung

    def _sitzung_schliessen(self, datei: DateiRefKonfig) -> None:
        sitzung = self._sitzungen.pop(datei_schluessel(datei), None)
        if not sitzung:
            return
        try:
            self.client.post(
                f"{self._mappe(datei)}/closeSession", json={}, headers={"workbook-session-id": sitzung}
            )
        except M365Fehler as exc:  # abgelaufene Session ist kein Fehler
            logger.info("closeSession: %s", exc)

    def _aufruf(self, datei: DateiRefKonfig, methode: str, pfad: str, **kw: Any) -> Any:
        schluessel = datei_schluessel(datei)
        eigene_headers = dict(kw.pop("headers", {}) or {})
        for erneut in (False, True):
            sitzung = self._sitzungen.get(schluessel)
            headers = dict(eigene_headers)
            if sitzung:
                headers["workbook-session-id"] = sitzung
            try:
                return self.client.anfrage(methode, f"{self._mappe(datei)}/{pfad}", headers=headers, **kw).daten
            except M365Konflikt:
                raise
            except M365Fehler as exc:
                codes = {c.lower() for c in (exc.code, exc.inner_code) if c}
                ungueltig = sitzung is not None and any(c.startswith("invalidsession") for c in codes)
                if not ungueltig or erneut:
                    raise
                logger.info("Workbook-Session ungültig (%s) – wird erneuert.", exc.inner_code or exc.code)
                self._sitzungen.pop(schluessel, None)
                self._sitzung_anlegen(datei)
        raise AssertionError("unerreichbar")

    # -- ExcelBackend -------------------------------------------------------------

    def blattnamen(self, datei: DateiRefKonfig) -> list[str]:
        daten = self._aufruf(datei, "GET", "worksheets", params={"$select": "id,name,position"})
        return [str(w.get("name")) for w in daten.get("value") or []]

    def lese_bereich(self, datei: DateiRefKonfig, blatt: str, adresse: str) -> list[list[Any]]:
        daten = self._aufruf(
            datei, "GET", f"worksheets/{self._blatt_url(blatt)}/{self._range_url(adresse)}",
            params={"$select": "address,values"},
        )
        return [list(zeile) for zeile in daten.get("values") or []]

    def lese_benutzten_bereich(self, datei: DateiRefKonfig, blatt: str) -> tuple[str, list[list[Any]]]:
        daten = self._aufruf(
            datei, "GET", f"worksheets/{self._blatt_url(blatt)}/usedRange(valuesOnly=true)",
            params={"$select": "address,values"},
        )
        return str(daten.get("address") or ""), [list(zeile) for zeile in daten.get("values") or []]

    def schreibe_bereich(
        self,
        datei: DateiRefKonfig,
        blatt: str,
        adresse: str,
        werte: list[list[Any | None]],
        *,
        zahlenformat: list[list[str | None]] | None = None,
    ) -> None:
        bereich = parse_adresse(adresse)
        self._pruefe_form(bereich.zeilen, bereich.spalten, werte, "werte")
        if zahlenformat is not None and (
            len(zahlenformat) != len(werte) or any(len(f) != len(w) for f, w in zip(zahlenformat, werte, strict=True))
        ):
            raise ValueError("zahlenformat muss dieselbe Form wie werte haben.")
        json_werte: list[list[Any]] = []
        formate: list[list[str | None]] = [
            list(zeile) if zahlenformat else [None] * len(zeile) for zeile in (zahlenformat or werte)
        ]
        formate_noetig = zahlenformat is not None
        for zi, zeile in enumerate(werte):
            json_zeile: list[Any] = []
            for si, wert in enumerate(zeile):
                json_zeile.append(_json_zellwert(wert))
                if isinstance(wert, datetime | date) and formate[zi][si] is None:
                    # TODO-PRUEFEN: Am echten Tenant bestätigen, dass Serienzahl + numberFormat ein
                    # Datum ergibt und das Format der Zielzelle (Fixterminüberwachung) nicht stört
                    # (Checkliste C5 der Recherche).
                    formate[zi][si] = self.ZEITFORMAT if isinstance(wert, datetime) else self.DATUMSFORMAT
                    formate_noetig = True
            json_werte.append(json_zeile)
        body: dict[str, Any] = {"values": json_werte}
        if formate_noetig:
            body["numberFormat"] = formate
        self._aufruf(datei, "PATCH", f"worksheets/{self._blatt_url(blatt)}/{self._range_url(adresse)}", json=body)
        logger.info("Excel %s!%s geschrieben (%s Zeilen)", blatt, adresse, len(werte))

    @staticmethod
    def _pruefe_form(zeilen: int, spalten: int, matrix: list[list[Any]], name: str) -> None:
        if len(matrix) == 1 and len(matrix[0]) == 1:
            return  # "single input copy": Graph füllt den ganzen Bereich mit dem Einzelwert
        if len(matrix) != zeilen or any(len(z) != spalten for z in matrix):
            raise ValueError(
                f"{name} hat {len(matrix)}x{max((len(z) for z in matrix), default=0)} Einträge, "
                f"der Bereich aber {zeilen}x{spalten} Zellen."
            )


# ---------------------------------------------------------------------------
# Mail
# ---------------------------------------------------------------------------

_SELECT_KOPF = "id,subject,from,receivedDateTime,hasAttachments,parentFolderId,internetMessageId"
_SELECT_MAIL = _SELECT_KOPF + ",body,bodyPreview,toRecipients,ccRecipients"


def _adressen(eintraege: list[dict[str, Any]] | None) -> list[str]:
    return [
        str((e.get("emailAddress") or {}).get("address") or "")
        for e in (eintraege or [])
        if (e.get("emailAddress") or {}).get("address")
    ]


def _kopf_aus(nachricht: dict[str, Any], ordner: str | None) -> MailKopf:
    von = (nachricht.get("from") or {}).get("emailAddress") or {}
    return MailKopf(
        id=str(nachricht.get("id") or ""),
        betreff=str(nachricht.get("subject") or ""),
        absender=str(von.get("address") or ""),
        absender_name=von.get("name"),
        empfangen=_zeit(nachricht.get("receivedDateTime")) or datetime.now(UTC),
        hat_anhaenge=bool(nachricht.get("hasAttachments")),
        ordner=ordner or nachricht.get("parentFolderId"),
    )


class GraphMailBackend:
    """Entwürfe anlegen und Posteingang lesen über Graph. Es wird NIE gesendet."""

    KENNZEICHNUNGS_HEADER = "x-auftragsautomat-typ"

    def __init__(self, client: GraphClient, postfach: str | None = None, *, kennzeichnen: bool = True) -> None:
        self.client = client
        self.postfach = postfach
        self.kennzeichnen = kennzeichnen
        self._basis = f"/users/{quote(postfach, safe='@')}" if postfach else "/me"
        self._unveraenderliche_ids = {"Prefer": 'IdType="ImmutableId"'}

    # -- Entwurf -----------------------------------------------------------------

    def erstelle_entwurf(self, entwurf: EmailEntwurf) -> str:
        fehlend = [str(p) for p in entwurf.anhaenge if not Path(p).is_file()]
        if fehlend:
            raise M365Fehler(f"Anhang nicht gefunden: {', '.join(fehlend)}")
        body: dict[str, Any] = {
            "subject": entwurf.betreff,
            "body": {"contentType": "HTML", "content": entwurf.text_html},
            "toRecipients": [{"emailAddress": {"address": a}} for a in entwurf.an],
            "ccRecipients": [{"emailAddress": {"address": a}} for a in entwurf.cc],
        }
        if self.kennzeichnen:
            # TODO-PRUEFEN: internetMessageHeaders ist beim Erstellen setzbar (Graph-Doku); ob der Header an
            # Entwürfen erhalten bleibt, ist nicht dokumentiert (Recherche 4.3).
            body["internetMessageHeaders"] = [{"name": self.KENNZEICHNUNGS_HEADER, "value": entwurf.typ.value}]
        nachricht = self.client.post(f"{self._basis}/messages", json=body, headers=self._unveraenderliche_ids)
        nachricht_id = str(nachricht.get("id") or "")
        if not nachricht_id:
            raise M365Fehler("Entwurf angelegt, aber Graph lieferte keine Nachrichten-ID.")
        for pfad in entwurf.anhaenge:
            self._anhang_hinzufuegen(nachricht_id, Path(pfad))
        entwurf.speicherort = nachricht_id
        entwurf.erstellt_am = entwurf.erstellt_am or datetime.now(UTC)
        logger.info(
            "Entwurf '%s' angelegt (%s Anhänge); webLink: %s", entwurf.betreff, len(entwurf.anhaenge), nachricht.get("webLink")
        )
        return nachricht_id

    def _anhang_hinzufuegen(self, nachricht_id: str, pfad: Path) -> None:
        daten = pfad.read_bytes()
        content_type = mimetypes.guess_type(pfad.name)[0] or "application/octet-stream"
        basis = f"{self._basis}/messages/{nachricht_id}/attachments"
        if len(daten) < ANHANG_KLEIN_MAX:
            self.client.post(
                basis,
                json={
                    "@odata.type": "#microsoft.graph.fileAttachment",
                    "name": pfad.name,
                    "contentType": content_type,
                    "contentBytes": base64.b64encode(daten).decode("ascii"),
                },
            )
            return
        sitzung = self.client.post(
            f"{basis}/createUploadSession",
            json={"AttachmentItem": {"attachmentType": "file", "name": pfad.name, "size": len(daten)}},
        )
        upload_url = str(sitzung.get("uploadUrl") or "")
        if not upload_url:
            raise M365Fehler(f"createUploadSession für {pfad.name} lieferte keine uploadUrl.")
        _chunks_hochladen(self.client, upload_url, daten)

    # -- Posteingang --------------------------------------------------------------

    def suche_mails(
        self,
        *,
        seit: datetime,
        absender: str | None = None,
        betreff_enthaelt: str | None = None,
        ordner: str | None = None,
        maximal: int = 50,
    ) -> list[MailKopf]:
        seit_utc = _utc(seit)
        pfad = f"{self._basis}/mailFolders/{quote(ordner or 'inbox', safe='')}/messages"
        params: dict[str, str] = {"$select": _SELECT_KOPF, "$top": str(min(max(maximal, 1), 100))}
        if betreff_enthaelt:
            # TODO-PRUEFEN: $search="subject:…" liefert bis zu 1.000 Treffer nach Relevanz/Sendedatum; Zeitraum und
            # Absender werden clientseitig gefiltert (Recherche 4.4, [A]).
            suchtext = betreff_enthaelt.replace("\\", "\\\\").replace('"', '\\"')
            params["$search"] = f'"subject:{suchtext}"'  # $orderby/$filter sind mit $search nicht kombinierbar
        else:
            filter_teile = [f"receivedDateTime ge {seit_utc.strftime('%Y-%m-%dT%H:%M:%SZ')}"]
            if absender:
                filter_teile.append(f"from/emailAddress/address eq '{absender.replace(chr(39), chr(39) * 2)}'")
            params["$filter"] = " and ".join(filter_teile)
            params["$orderby"] = "receivedDateTime desc"
        ergebnis: list[MailKopf] = []
        for nachricht in self.client.seiten(pfad, params=params, headers=self._unveraenderliche_ids, maximal=1000):
            kopf = _kopf_aus(nachricht, ordner or "inbox")
            if kopf.empfangen < seit_utc:
                continue
            if absender and kopf.absender.lower() != absender.lower():
                continue
            ergebnis.append(kopf)
            if len(ergebnis) >= maximal:
                break
        ergebnis.sort(key=lambda k: k.empfangen, reverse=True)
        return ergebnis

    def lese_mail(self, mail_id: str) -> Mail:
        basis = f"{self._basis}/messages/{quote(mail_id, safe='')}"
        nachricht = self.client.get(basis, params={"$select": _SELECT_MAIL}, headers=self._unveraenderliche_ids)
        kopf = _kopf_aus(nachricht, None)
        body = nachricht.get("body") or {}
        inhalt = str(body.get("content") or "")
        html = inhalt if str(body.get("contentType") or "").lower() == "html" else None
        text = html_zu_text(html) if html else inhalt
        anhaenge: list[MailAnhang] = []
        for a in self.client.seiten(f"{basis}/attachments", params={"$select": "id,name,size,contentType,isInline"}):
            anhaenge.append(
                MailAnhang(
                    id=str(a.get("id") or ""),
                    name=str(a.get("name") or "anhang"),
                    groesse=a.get("size"),
                    content_type=a.get("contentType"),
                    inline=bool(a.get("isInline")),
                )
            )
        return Mail(
            kopf=kopf,
            text=text or None,
            html=html,
            an=_adressen(nachricht.get("toRecipients")),
            cc=_adressen(nachricht.get("ccRecipients")),
            anhaenge=anhaenge,
        )

    def speichere_anhang(self, mail_id: str, anhang_id: str, ziel: Path) -> Path:
        basis = f"{self._basis}/messages/{quote(mail_id, safe='')}/attachments/{quote(anhang_id, safe='')}"
        ziel = Path(ziel)
        if ziel.is_dir():
            meta = self.client.get(basis, params={"$select": "name"})
            name = str(meta.get("name") or anhang_id)
            ziel = ziel / sicherer_dateiname(name)
        inhalt = self.client.anfrage("GET", f"{basis}/$value", roh=True).daten
        ziel.parent.mkdir(parents=True, exist_ok=True)
        ziel.write_bytes(inhalt if isinstance(inhalt, bytes) else bytes(inhalt))
        return ziel


def _chunks_hochladen(client: GraphClient, upload_url: str, daten: bytes) -> dict[str, Any]:
    """PUT-Chunks an eine vorauthentifizierte Upload-URL (kein Authorization-Header, Reihenfolge
    einhalten, ``Content-Range: bytes a-b/gesamt``). Liefert den JSON-Body der letzten Antwort."""
    gesamt = len(daten)
    start = 0
    letzte: dict[str, Any] = {}
    while start < gesamt:
        ende = min(start + UPLOAD_CHUNK, gesamt) - 1
        teil = daten[start : ende + 1]
        antwort = client.anfrage(
            "PUT",
            upload_url,
            data=teil,
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Length": str(len(teil)),
                "Content-Range": f"bytes {start}-{ende}/{gesamt}",
            },
            ohne_token=True,
        )
        letzte = antwort.daten if isinstance(antwort.daten, dict) else {}
        if antwort.status in (200, 201) and ende + 1 >= gesamt:
            if antwort.headers.get("location"):
                letzte.setdefault("location", antwort.headers["location"])
            break
        naechste = (letzte.get("nextExpectedRanges") or [f"{ende + 1}-"])[0]
        try:
            start = int(str(naechste).split("-")[0])
        except ValueError:
            start = ende + 1
    return letzte


# ---------------------------------------------------------------------------
# Dateien (OneDrive/SharePoint)
# ---------------------------------------------------------------------------


class GraphDateiBackend:
    """Ordner und Dateien unterhalb eines Drive-Items (Ordner "vorangefertigte Dossiers",
    "Aufträge mit/ohne Provision"). Nichts wird verschoben oder gelöscht."""

    def __init__(self, client: GraphClient) -> None:
        self.client = client
        self._ids = _IdCache(client)

    def _url(self, ref: DateiRefKonfig, relativ: str, aktion: str = "") -> str:
        drive_id, item_id = self._ids.ids(ref)
        wurzel = f"/drives/{drive_id}/items/{item_id}"
        relativ = relativ.strip("/")
        if not relativ:
            return wurzel + aktion
        return f"{wurzel}:/{adressierung.kodiere_pfad(relativ)}" + (":" + aktion if aktion else "")

    @staticmethod
    def _info(item: dict[str, Any], relativ: str) -> DateiInfo:
        name = str(item.get("name") or "")
        return DateiInfo(
            name=name,
            pfad=f"{relativ.strip('/')}/{name}" if relativ.strip("/") else name,
            ist_ordner="folder" in item,
            groesse=item.get("size"),
            geaendert=_zeit(item.get("lastModifiedDateTime")),
            id=item.get("id"),
        )

    def existiert(self, ref: DateiRefKonfig, relativ: str = "") -> bool:
        try:
            self.client.get(self._url(ref, relativ), params={"$select": "id,name"})
        except M365NichtGefunden:
            return False
        return True

    def ordner_anlegen(self, ref: DateiRefKonfig, relativ: str) -> str:
        aktuell = ""
        item_id = str(self.client.get(self._url(ref, ""), params={"$select": "id"}).get("id") or "")
        for segment in [s for s in relativ.strip("/").split("/") if s]:
            naechster = f"{aktuell}/{segment}" if aktuell else segment
            try:
                item = self.client.get(self._url(ref, naechster), params={"$select": "id,name,folder"})
            except M365NichtGefunden:
                try:
                    item = self.client.post(
                        self._url(ref, aktuell, "/children"),
                        json={"name": segment, "folder": {}, "@microsoft.graph.conflictBehavior": "fail"},
                    )
                except M365Fehler as exc:
                    if exc.status != 409:
                        raise
                    item = self.client.get(self._url(ref, naechster), params={"$select": "id,name,folder"})
                logger.info("Ordner '%s' angelegt", naechster)
            if "folder" not in item:
                raise M365Fehler(f"'{naechster}' existiert bereits als Datei, nicht als Ordner.")
            item_id = str(item.get("id") or "")
            aktuell = naechster
        return item_id

    def liste(self, ref: DateiRefKonfig, relativ: str = "") -> list[DateiInfo]:
        eintraege = self.client.seiten(
            self._url(ref, relativ, "/children"),
            params={"$select": "id,name,size,folder,file,lastModifiedDateTime", "$top": "200"},
        )
        return [self._info(item, relativ) for item in eintraege]

    def hochladen(self, lokal: Path, ref: DateiRefKonfig, relativ: str) -> str:
        lokal = Path(lokal)
        if not lokal.is_file():
            raise M365Fehler(f"Lokale Datei fehlt: {lokal}")
        ziel = relativ.strip()
        if not ziel or ziel.endswith("/"):
            ziel = ziel + lokal.name
        daten = lokal.read_bytes()
        if len(daten) < DATEI_KLEIN_MAX:
            item = self.client.put(
                self._url(ref, ziel, "/content"),
                data=daten,
                params={"@microsoft.graph.conflictBehavior": "replace"},
                headers={"Content-Type": mimetypes.guess_type(lokal.name)[0] or "application/octet-stream"},
            )
        else:
            sitzung = self.client.post(
                self._url(ref, ziel, "/createUploadSession"),
                json={"item": {"@microsoft.graph.conflictBehavior": "replace", "name": ziel.rsplit("/", 1)[-1]}},
            )
            upload_url = str(sitzung.get("uploadUrl") or "")
            if not upload_url:
                raise M365Fehler(f"createUploadSession für {ziel} lieferte keine uploadUrl.")
            item = _chunks_hochladen(self.client, upload_url, daten)
        logger.info("Hochgeladen: %s -> %s (%s Bytes)", lokal.name, ziel, len(daten))
        return str(item.get("id") or ziel)

    def herunterladen(self, ref: DateiRefKonfig, relativ: str, ziel: Path) -> Path:
        ziel = Path(ziel)
        if ziel.is_dir():
            ziel = ziel / relativ.strip("/").rsplit("/", 1)[-1]
        inhalt = self.client.anfrage("GET", self._url(ref, relativ, "/content"), roh=True).daten
        ziel.parent.mkdir(parents=True, exist_ok=True)
        ziel.write_bytes(inhalt if isinstance(inhalt, bytes) else bytes(inhalt))
        return ziel

    def schreibe_text(self, ref: DateiRefKonfig, relativ: str, text: str) -> str:
        item = self.client.put(
            self._url(ref, relativ, "/content"),
            data=text.encode("utf-8"),
            params={"@microsoft.graph.conflictBehavior": "replace"},
            headers={"Content-Type": "text/plain; charset=utf-8"},
        )
        return str(item.get("id") or relativ)
