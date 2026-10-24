"""Anmeldung an Microsoft Graph mit MSAL (Public Client, delegierte Rechte).

Ablauf laut Recherche ``docs/recherche/graph_excel_outlook.md`` (Abschnitt 2):

1. Eigene App-Registrierung in Entra (Single-Tenant, Plattform "Mobile and desktop applications"
   mit ``http://localhost``, "Allow public client flows" = Yes). ``client_id``/``tenant_id`` stehen
   in ``settings.m365``.
2. ``msal.PublicClientApplication`` mit persistentem Token-Cache (``msal_extensions``): unter
   Windows ``build_encrypted_persistence`` (DPAPI, an das Windows-Konto gebunden), auf anderen
   Plattformen ``FilePersistence`` (Klartext) mit Warnung – dort läuft das Tool ohnehin nur im
   Dry Run/Test.
3. :meth:`GraphAnmeldung.token`: ``get_accounts`` -> ``acquire_token_silent`` für das erste Konto.
   Ohne Konto oder ohne gültiges/erneuerbares Token -> :class:`M365NichtAngemeldet` (die CLI
   ``m365-anmelden`` holt dann interaktiv ein Token). Nie stillschweigend einen Browser öffnen.
4. :meth:`GraphAnmeldung.anmelden_interaktiv` (Systembrowser, PKCE, Redirect ``http://localhost``)
   bzw. :meth:`GraphAnmeldung.anmelden_device_code` (Gerätecode; Rückfall, wenn der Browser-Redirect
   im Netz des Anwenders blockiert ist).

Scopes: ``settings.m365.bereiche`` als Kurzform (``Files.ReadWrite`` …). **``offline_access``,
``openid`` und ``profile`` werden NICHT übergeben**: msal 1.39.0 (``application.py``,
``ClientApplication._decorate_scope``) wirft ``ValueError("You cannot use any scope value that is
reserved …")``, wenn einer dieser reservierten Scopes in der Liste steht, und ergänzt sie selbst
(``decorated = scope_set | reserved_scope``, abzüglich ``exclude_scopes``). Das Refresh-Token für den
unbeaufsichtigten Tagelauf entsteht also automatisch; :func:`bereinige_scopes` filtert die
reservierten Werte aus der Konfiguration heraus, damit eine wohlmeinende Ergänzung in
``settings.yaml`` nicht zum Absturz führt.
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import msal
import msal_extensions

from auftragsautomat.config import M365Konfig
from auftragsautomat.m365.basis import M365Fehler, M365NichtAngemeldet

__all__ = [
    "GraphAnmeldung",
    "RESERVIERTE_SCOPES",
    "STANDARD_AUTORITAET",
    "bereinige_scopes",
    "standard_cache_pfad",
]

logger = logging.getLogger(__name__)

STANDARD_AUTORITAET = "https://login.microsoftonline.com/{tenant}"

#: Von msal selbst ergänzte Scopes (``ClientApplication._decorate_scope``, msal 1.39.0).
RESERVIERTE_SCOPES = frozenset({"openid", "profile", "offline_access"})

_CACHE_DATEINAME = "token_cache.bin"


def bereinige_scopes(bereiche: list[str]) -> list[str]:
    """Entfernt reservierte Scopes (msal ergänzt sie selbst) und Dubletten, behält die Reihenfolge."""
    ergebnis: list[str] = []
    for bereich in bereiche:
        wert = bereich.strip()
        if not wert:
            continue
        if wert.lower() in RESERVIERTE_SCOPES:
            logger.debug("Scope %s wird nicht übergeben – msal ergänzt reservierte Scopes selbst.", wert)
            continue
        if wert not in ergebnis:
            ergebnis.append(wert)
    return ergebnis


def standard_cache_pfad() -> Path:
    """``%APPDATA%/Auftragsautomat/token_cache.bin`` (Windows) bzw. ``~/.auftragsautomat/`` sonst."""
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "Auftragsautomat" / _CACHE_DATEINAME
    return Path.home() / ".auftragsautomat" / _CACHE_DATEINAME


class GraphAnmeldung:
    """Token-Lieferant für :class:`auftragsautomat.m365.graph.GraphClient`.

    ``token`` ist absichtlich eine Methode ohne Argumente, damit sie direkt als ``token_provider``
    übergeben werden kann (``GraphClient(anmeldung.token)``).
    """

    def __init__(self, konfig: M365Konfig, *, cache_pfad: Path | None = None) -> None:
        if not konfig.client_id:
            raise M365Fehler(
                "settings.m365.client_id fehlt (Application (client) ID der eigenen App-Registrierung in Entra)."
            )
        if not konfig.autoritaet and not konfig.tenant_id:
            raise M365Fehler(
                "settings.m365.tenant_id fehlt (Directory (tenant) ID) – oder 'autoritaet' direkt angeben."
            )
        self.konfig = konfig
        self.client_id = konfig.client_id
        self.autoritaet = konfig.autoritaet or STANDARD_AUTORITAET.format(tenant=konfig.tenant_id)
        self.scopes = bereinige_scopes(konfig.bereiche)
        if not self.scopes:
            raise M365Fehler("settings.m365.bereiche ist leer – mindestens 'User.Read' wird gebraucht.")
        self.cache_pfad = Path(cache_pfad or konfig.token_cache or standard_cache_pfad())
        self._cache = self._erzeuge_cache(self.cache_pfad)
        self._app: Any = msal.PublicClientApplication(
            self.client_id, authority=self.autoritaet, token_cache=self._cache
        )
        logger.debug("MSAL-App für %s (Autorität %s, Cache %s)", self.client_id, self.autoritaet, self.cache_pfad)

    # -- Cache -----------------------------------------------------------------

    @staticmethod
    def _erzeuge_cache(pfad: Path) -> Any:
        pfad.parent.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            try:
                persistenz = msal_extensions.build_encrypted_persistence(str(pfad))
            except Exception as exc:  # pragma: no cover - nur auf Windows erreichbar
                raise M365Fehler(
                    f"Verschlüsselter Token-Cache (DPAPI) konnte nicht angelegt werden: {exc}. "
                    "Ist pywin32 installiert (pip install 'auftragsautomat[windows]')?"
                ) from exc
        else:
            logger.warning(
                "Token-Cache %s wird auf %s UNVERSCHLÜSSELT abgelegt (DPAPI gibt es nur unter Windows).",
                pfad,
                sys.platform,
            )
            persistenz = msal_extensions.FilePersistence(str(pfad))
        return msal_extensions.PersistedTokenCache(persistenz)

    # -- Konten / Token ---------------------------------------------------------

    def konten(self) -> list[dict[str, Any]]:
        return list(self._app.get_accounts() or [])

    def konto(self) -> str | None:
        """Benutzername (UPN) des ersten Kontos im Cache, sonst ``None``."""
        konten = self.konten()
        return konten[0].get("username") if konten else None

    def token(self) -> str:
        """Gültiges Zugriffstoken aus dem Cache (bei Bedarf per Refresh-Token erneuert).

        Ohne angemeldetes Konto oder wenn Entra eine Interaktion verlangt (abgelaufenes Refresh-Token,
        Conditional Access) -> :class:`M365NichtAngemeldet`; es wird nie ein Browser geöffnet.
        """
        # TODO-PRUEFEN: Unbeaufsichtigter Tagelauf (Aufgabenplanung): Refresh-Token-Lebensdauer und
        # Conditional Access des Tenants; der Task muss unter demselben Windows-Konto laufen (DPAPI-Cache),
        # Checkliste A5/A6 der Recherche.
        konten = self.konten()
        if not konten:
            raise M365NichtAngemeldet(
                "Kein Microsoft-365-Konto im Token-Cache. Bitte einmalig 'auftragsautomat m365-anmelden' ausführen."
            )
        ergebnis = self._app.acquire_token_silent(self.scopes, account=konten[0]) or {}
        return self._pruefe_ergebnis(ergebnis, stiller_versuch=True)

    def anmelden_interaktiv(self, *, login_hint: str | None = None, port: int | None = None) -> str:
        """Interaktive Anmeldung im Systembrowser (PKCE, Redirect ``http://localhost``).

        Liefert den Benutzernamen des angemeldeten Kontos; das Token liegt danach im Cache.
        """
        ergebnis = self._app.acquire_token_interactive(self.scopes, login_hint=login_hint, port=port) or {}
        self._pruefe_ergebnis(ergebnis, stiller_versuch=False)
        return self._konto_aus(ergebnis)

    def anmelden_device_code(self, ausgabe: Callable[[str], Any] = print) -> str:
        """Anmeldung per Gerätecode: ``ausgabe`` erhält den Text mit URL und Code, dann wird
        blockierend gewartet (Entra erlaubt 15 Minuten). Liefert den Benutzernamen."""
        flow = self._app.initiate_device_flow(scopes=self.scopes) or {}
        if "user_code" not in flow:
            raise M365Fehler(
                "Device-Code-Anmeldung konnte nicht gestartet werden: "
                f"{flow.get('error', '?')}: {flow.get('error_description', '')}".strip()
            )
        ausgabe(flow.get("message") or f"Code {flow['user_code']} unter {flow.get('verification_uri')} eingeben.")
        ergebnis = self._app.acquire_token_by_device_flow(flow) or {}
        self._pruefe_ergebnis(ergebnis, stiller_versuch=False)
        return self._konto_aus(ergebnis)

    def abmelden(self) -> int:
        """Entfernt alle Konten aus dem Cache; liefert die Anzahl."""
        konten = self.konten()
        for konto in konten:
            self._app.remove_account(konto)
        return len(konten)

    # -- intern ----------------------------------------------------------------

    @staticmethod
    def _konto_aus(ergebnis: dict[str, Any]) -> str:
        claims = ergebnis.get("id_token_claims") or {}
        return str(claims.get("preferred_username") or claims.get("upn") or claims.get("name") or "")

    @staticmethod
    def _pruefe_ergebnis(ergebnis: dict[str, Any], *, stiller_versuch: bool) -> str:
        token = ergebnis.get("access_token")
        if token:
            return str(token)
        fehler = ergebnis.get("error") or ("kein Token" if stiller_versuch else "unbekannter Fehler")
        beschreibung = (ergebnis.get("error_description") or "").splitlines()[0] if ergebnis else ""
        korrelation = ergebnis.get("correlation_id")
        meldung = f"Anmeldung fehlgeschlagen ({fehler}): {beschreibung}".rstrip(": ")
        if korrelation:
            meldung += f" [correlation_id {korrelation}]"
        if stiller_versuch:
            raise M365NichtAngemeldet(
                meldung + " – bitte 'auftragsautomat m365-anmelden' ausführen.", code=str(fehler)
            )
        raise M365Fehler(meldung, code=str(fehler))
