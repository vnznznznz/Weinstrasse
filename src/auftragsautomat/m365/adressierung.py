"""Web-URL einer SharePoint-/OneDrive-Datei in ``drive_id``/``item_id`` auflösen.

Zwei Wege laut ``docs/recherche/graph_excel_outlook.md`` (Abschnitt 3):

* **Weg A** – ``GET /shares/u!{base64url(url)}/driveItem`` mit Header
  ``Prefer: redeemSharingLinkIfNecessary`` (:func:`kodiere_sharing_url`, :func:`aufloesen_ueber_shares`).
  Funktioniert mit "Link kopieren"-URLs und meist auch mit der Adresszeile; kann bei manchen
  Adresszeilen-URLs fehlschlagen.
* **Weg B** – deterministisch über die Site: ``GET /sites/{hostname}:/{site-pfad}`` -> ``site.id``,
  ``GET /sites/{id}/drives`` (Bibliothek per ``webUrl``-Präfix wählen) ->
  ``GET /drives/{drive}/root:/{pfad}`` mit einzeln UTF-8-prozentkodierten Segmenten
  (:func:`zerlege_web_url`, :func:`aufloesen_ueber_site`).

:func:`aufloesen` probiert A und dann B; das Ergebnis (``drive_id``/``item_id``) ist stabil gegen
Umbenennen/Verschieben und gehört einmalig nach ``config/settings.yaml``. Mit bereits gefüllten IDs
ist die Funktion ein No-op (idempotent).
"""

from __future__ import annotations

import base64
import logging
import re
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import quote, unquote, urlsplit

from auftragsautomat.config import DateiRefKonfig
from auftragsautomat.m365.basis import M365Fehler, M365NichtGefunden

__all__ = [
    "WebUrlTeile",
    "aufloesen",
    "aufloesen_ueber_shares",
    "aufloesen_ueber_site",
    "kodiere_pfad",
    "kodiere_sharing_url",
    "zerlege_web_url",
]

logger = logging.getLogger(__name__)


class _Client(Protocol):
    def get(self, pfad: str, **kw: Any) -> Any: ...


def kodiere_sharing_url(url: str) -> str:
    """``u!`` + unpadded base64url der URL (Graph-Doku "Access shared items")."""
    kodiert = base64.b64encode(url.strip().encode("utf-8")).decode("ascii")
    return "u!" + kodiert.rstrip("=").replace("/", "_").replace("+", "-")


def kodiere_pfad(pfad: str) -> str:
    """Pfadsegmente einzeln UTF-8-prozentkodieren (``Aufträge/Fixterminüberwachung.xlsx`` ->
    ``Auftr%C3%A4ge/Fixtermin%C3%BCberwachung.xlsx``); Schrägstriche bleiben Trenner."""
    teile = [quote(unquote(seg), safe="") for seg in pfad.strip("/").split("/") if seg]
    return "/".join(teile)


@dataclass(frozen=True)
class WebUrlTeile:
    """Bestandteile einer SharePoint-/OneDrive-URL."""

    hostname: str
    site_pfad: str  # z. B. "/sites/Buchhaltung" oder "" (Stamm-Site)
    pfad: str  # serverrelativer, dekodierter Pfad ab der Site: "Freigegebene Dokumente/General/x.xlsx"

    @property
    def site_url(self) -> str:
        return f"https://{self.hostname}{self.site_pfad}"


_KURZLINK_RE = re.compile(r"^/:[a-z]:/[rsu]/", re.IGNORECASE)
_SITE_RE = re.compile(r"^(/(?:sites|teams|personal)/[^/]+)(/.*)?$", re.IGNORECASE)


def zerlege_web_url(url: str) -> WebUrlTeile:
    """Zerlegt die Adresszeile (auch ``/:x:/r/…``-Kurzlinks) in Hostname, Site-Pfad und Dateipfad."""
    teile = urlsplit(url.strip())
    if not teile.hostname:
        raise M365Fehler(f"Keine gültige Web-URL: '{url}'")
    pfad = teile.path
    pfad = _KURZLINK_RE.sub("/", pfad)
    pfad = unquote(pfad)
    treffer = _SITE_RE.match(pfad)
    if treffer:
        site_pfad = treffer.group(1)
        rest = treffer.group(2) or ""
    else:
        site_pfad = ""
        rest = pfad
    return WebUrlTeile(teile.hostname.lower(), site_pfad, rest.strip("/"))


def _ids_aus_item(item: dict[str, Any]) -> tuple[str, str]:
    drive_id = (item.get("parentReference") or {}).get("driveId")
    item_id = item.get("id")
    if not drive_id or not item_id:
        raise M365Fehler("Graph-Antwort enthält keine driveId/id (driveItem erwartet).")
    return str(drive_id), str(item_id)


def aufloesen_ueber_shares(client: _Client, web_url: str) -> tuple[str, str]:
    """Weg A: ``/shares/{u!…}/driveItem`` -> (drive_id, item_id)."""
    item = client.get(
        f"/shares/{kodiere_sharing_url(web_url)}/driveItem",
        headers={"Prefer": "redeemSharingLinkIfNecessary"},
    )
    return _ids_aus_item(item)


def aufloesen_ueber_site(client: _Client, web_url: str) -> tuple[str, str]:
    """Weg B: Site -> Bibliothek (per ``webUrl``-Präfix) -> ``root:/{pfad}`` -> (drive_id, item_id)."""
    teile = zerlege_web_url(web_url)
    site = client.get(f"/sites/{teile.hostname}:{teile.site_pfad}" if teile.site_pfad else f"/sites/{teile.hostname}")
    site_id = site.get("id")
    if not site_id:
        raise M365Fehler(f"Site {teile.site_url} nicht gefunden (keine id in der Antwort).")
    drives = client.get(f"/sites/{site_id}/drives").get("value") or []
    voll = f"https://{teile.hostname}{teile.site_pfad}/{teile.pfad}".lower()
    bester: dict[str, Any] | None = None
    laengstes_praefix = ""
    for drive in drives:
        drive_url = unquote(str(drive.get("webUrl") or "")).rstrip("/").lower()
        if drive_url and (voll == drive_url or voll.startswith(drive_url + "/")):
            if len(drive_url) > len(laengstes_praefix):
                bester, laengstes_praefix = drive, drive_url
    if bester is None:
        bekannte = ", ".join(str(d.get("webUrl")) for d in drives) or "keine"
        raise M365NichtGefunden(
            f"Keine Dokumentbibliothek der Site {teile.site_url} passt zu '{web_url}' (Bibliotheken: {bekannte})."
        )
    rest = voll[len(laengstes_praefix) :].strip("/")
    # Originalschreibweise (Groß-/Kleinschreibung) aus dem dekodierten Pfad übernehmen:
    original = f"https://{teile.hostname}{teile.site_pfad}/{teile.pfad}"[len(laengstes_praefix) :].strip("/")
    rel_pfad = original if original.lower() == rest else rest
    if not rel_pfad:
        item = client.get(f"/drives/{bester['id']}/root")
    else:
        item = client.get(f"/drives/{bester['id']}/root:/{kodiere_pfad(rel_pfad)}")
    item_id = item.get("id")
    if not item_id:
        raise M365NichtGefunden(f"Datei '{rel_pfad}' in Bibliothek {bester.get('webUrl')} nicht gefunden.")
    return str(bester["id"]), str(item_id)


def aufloesen(ref: DateiRefKonfig, client: _Client) -> DateiRefKonfig:
    """Liefert eine Kopie der Referenz mit gefüllten ``drive_id``/``item_id`` (idempotent).

    Reihenfolge: vorhandene IDs -> Weg A (``/shares``) -> Weg B (Site/Bibliothek/Pfad). Ohne
    ``web_url`` und ohne IDs -> :class:`M365Fehler`.
    """
    if ref.drive_id and ref.item_id:
        return ref.model_copy()
    if not ref.web_url:
        raise M365Fehler(
            "Datei-Referenz hat weder drive_id/item_id noch web_url – für das Graph-Backend bitte die "
            "Adresszeile der Datei in settings.yaml eintragen."
        )
    fehler_a: M365Fehler | None = None
    # TODO-PRUEFEN: /shares/u!… mit der Adresszeilen-URL (nicht "Link kopieren") und ob Files.ReadWrite +
    # Sites.Read.All für die Buchhaltungs-Site reichen (Checkliste A3/B2 der Recherche).
    try:
        drive_id, item_id = aufloesen_ueber_shares(client, ref.web_url)
        logger.info("Datei-Referenz über /shares aufgelöst: drive %s, item %s", drive_id, item_id)
    except M365Fehler as exc:
        fehler_a = exc
        logger.info("Auflösung über /shares fehlgeschlagen (%s) – versuche Site/Bibliothek/Pfad.", exc)
        try:
            drive_id, item_id = aufloesen_ueber_site(client, ref.web_url)
        except M365Fehler as exc_b:
            raise M365Fehler(
                f"Web-URL '{ref.web_url}' konnte nicht aufgelöst werden. /shares: {fehler_a}; Site-Weg: {exc_b}",
                status=exc_b.status,
                code=exc_b.code,
                inner_code=exc_b.inner_code,
            ) from exc_b
        logger.info("Datei-Referenz über Site aufgelöst: drive %s, item %s", drive_id, item_id)
    return ref.model_copy(update={"drive_id": drive_id, "item_id": item_id})
