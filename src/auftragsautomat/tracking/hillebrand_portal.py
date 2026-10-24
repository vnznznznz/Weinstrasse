"""Stufe 3 (nur Rückfall): Kundenportal myHillebrandGori per Playwright mit eigenem Chrome-Profil.

**Nutzungsbedingungen:** Die Terms of Use von hillebrandgori.com (Abschnitt 10(6)) untersagen
automatisierten Zugriff auf die Website. Diese Quelle ist deshalb **ausschließlich Rückfallebene**,
wird nur mit Wissen des Hillebrand-Ansprechpartners eingesetzt, läuft **sichtbar**, mit niedriger
Frequenz, mit Screenshots und ohne jede Stealth-Technik. Der von Hillebrand vorgesehene Weg ist
die offizielle API (``hillebrand_api.py``). Siehe ``docs/recherche/hillebrand_tracking.md`` 3.4.

Technik (Recherche Abschnitt 3):

* **Eigenes Profilverzeichnis** (``settings.tracking.chrome_profil``, Standard
  ``%LOCALAPPDATA%\\Auftragsautomat\\chrome-profil``, unter Linux
  ``~/.local/share/auftragsautomat/chrome-profil``). Das normale Chrome-Profil des Anwenders
  kann seit Chrome 136 nicht automatisiert werden und bleibt unberührt.
* ``launch_persistent_context(channel="chrome", headless=False)`` – der installierte Google Chrome;
  Cookies und Okta-Sitzung bleiben im Profil erhalten.
* **Login:** Der Automat gibt nie Passwörter oder Einmalcodes ein. Erkennt er die Okta-Anmeldeseite
  (Host ``login.hillebrand.com`` oder ``login_regex``), endet der Lauf mit Status ``unbekannt``
  und dem Hinweis "bitte einmal manuell anmelden" (:meth:`PortalQuelle.einrichten`).
* Selektoren und URLs aus ``config/tracking_hillebrand.yaml`` (``selektoren.portal``);
  :meth:`PortalQuelle.aufzeichnen` speichert HTML + Screenshot einer Sendungsseite, damit die
  Selektoren am echten Portal eingestellt werden können.

Tests injizieren ``lade_seite(url) -> Seite``; es wird nie ein echter Browser gestartet.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from auftragsautomat.config import Settings
from auftragsautomat.models import Meilenstein, Sendung, SendungStatus
from auftragsautomat.tracking.basis import (
    HillebrandKonfig,
    ist_bekannt,
    jetzt,
    lade_hillebrand_konfig,
    mit_hinweis,
)
from auftragsautomat.tracking.hillebrand_public import (
    LaderFehler,
    erkenne_seitentyp,
    parse_public_tracking_html,
    playwright_verfuegbar,
)

logger = logging.getLogger(__name__)

__all__ = ["HINWEIS_LOGIN", "LadeSeite", "PortalQuelle", "Seite", "profilverzeichnis"]

HINWEIS_LOGIN = (
    "Portal-Login erforderlich: bitte einmal manuell anmelden "
    "(im Automations-Chrome: auftragsautomat tracking login bzw. PortalQuelle.einrichten())"
)


@dataclass
class Seite:
    """Ergebnis eines Seitenabrufs im Portal."""

    url: str  # endgültige URL (nach Weiterleitungen, z. B. auf login.hillebrand.com)
    html: str
    screenshot: Path | None = None


LadeSeite = Callable[[str], Seite]


def profilverzeichnis(
    settings: Settings | None = None,
    *,
    umgebung: Mapping[str, str] | None = None,
    plattform: str | None = None,
) -> Path:
    """Eigenes Chrome-Profil des Automaten (nie das Standardprofil des Anwenders).

    Reihenfolge: ``settings.tracking.chrome_profil`` -> Windows ``%LOCALAPPDATA%/Auftragsautomat/chrome-profil``
    -> sonst ``~/.local/share/auftragsautomat/chrome-profil``.
    """
    if settings is not None and settings.tracking.chrome_profil is not None:
        return Path(settings.tracking.chrome_profil).expanduser()
    umgebung = os.environ if umgebung is None else umgebung
    plattform = plattform or os.name
    lokal = umgebung.get("LOCALAPPDATA")
    if plattform == "nt" and lokal:
        return Path(lokal) / "Auftragsautomat" / "chrome-profil"
    if plattform == "nt":
        return Path.home() / "AppData" / "Local" / "Auftragsautomat" / "chrome-profil"
    return Path.home() / ".local" / "share" / "auftragsautomat" / "chrome-profil"


def _dateiname_sicher(text: str) -> str:
    return re.sub(r"[^\w.-]+", "_", text).strip("_")[:80] or "seite"


class PortalQuelle:
    """Tracking-Quelle über das eingeloggte Kundenportal (nur Rückfall, sichtbarer Browser)."""

    name = "portal"

    def __init__(
        self,
        konfig: HillebrandKonfig | None = None,
        settings: Settings | None = None,
        *,
        lade_seite: LadeSeite | None = None,
    ) -> None:
        self._konfig = konfig or lade_hillebrand_konfig()
        self._settings = settings or Settings()
        self._lade_seite = lade_seite

    @property
    def konfig(self) -> HillebrandKonfig:
        return self._konfig

    @property
    def tracking(self):
        return self._settings.tracking

    def profil(self) -> Path:
        return profilverzeichnis(self._settings)

    def verfuegbar(self) -> bool:
        return self._lade_seite is not None or playwright_verfuegbar()

    def suche_url(self, referenz: str) -> str:
        """URL der Sendungssuche im Portal (``portal_tracking_url``, sonst Public-Tracking-Route)."""
        return self._konfig.portal_suche_url(referenz)

    def ist_login_seite(self, seite: Seite) -> bool:
        return self._konfig.ist_login_seite(seite.html, seite.url)

    # -- TrackingQuelle --------------------------------------------------------

    def aktualisiere(self, sendung: Sendung) -> Sendung:
        try:
            return self._aktualisiere(sendung)
        except LaderFehler as exc:
            logger.warning("Portal: %s", exc)
            return mit_hinweis(sendung, f"Portal: {exc}")
        except Exception as exc:  # nie nach außen
            logger.exception("Portal: unerwarteter Fehler")
            return mit_hinweis(sendung, f"Portal: unerwarteter Fehler {type(exc).__name__}: {exc}")

    def _aktualisiere(self, sendung: Sendung) -> Sendung:
        if not self.verfuegbar():
            return mit_hinweis(sendung, "Portal nicht verfügbar: Playwright ist nicht installiert")
        referenz = (sendung.referenz or "").strip()
        if not referenz:
            return mit_hinweis(sendung, "Portal: Sendung ohne Hillebrand-Referenz, Suche nicht möglich")
        seite = self._lade(self.suche_url(referenz))
        if self.ist_login_seite(seite):
            return mit_hinweis(sendung, HINWEIS_LOGIN)
        typ = erkenne_seitentyp(seite.html, self._konfig, url=seite.url)
        if typ == "challenge":
            return mit_hinweis(sendung, "Portal: Cloudflare-Challenge statt Sendungsdaten")
        if typ == "leer":
            return mit_hinweis(sendung, f"Portal: leere Seite zu {referenz}")
        status, meilensteine = parse_public_tracking_html(
            seite.html, self._konfig, selektoren=self._konfig.selektoren.portal, bezug=jetzt()
        )
        if not ist_bekannt(status) and not meilensteine:
            return mit_hinweis(sendung, f"Portal: keine Sendungsdaten zu {referenz} (Selektoren prüfen, aufzeichnen())")
        return self._ergebnis(sendung, status, meilensteine, seite)

    def _ergebnis(self, sendung: Sendung, status: SendungStatus, meilensteine: list[Meilenstein], seite: Seite) -> Sendung:
        hinweise = list(sendung.hinweise)
        if not ist_bekannt(status):
            hinweise.append("Portal: Timeline gelesen, Status nicht zuordenbar")
        if seite.screenshot is not None:
            hinweise.append(f"Portal-Screenshot: {seite.screenshot}")
        geplante_abholung = sendung.geplante_abholung
        geplante_zustellung = sendung.geplante_zustellung
        for m in meilensteine:
            if m.zeitpunkt is None:
                continue
            if m.code.startswith("shpsupdep"):
                geplante_abholung = m.zeitpunkt.date()
            elif m.code.startswith("shpcusarr"):
                geplante_zustellung = m.zeitpunkt.date()
        return sendung.model_copy(
            update={
                "status": status,
                "meilensteine": meilensteine or list(sendung.meilensteine),
                "geplante_abholung": geplante_abholung,
                "geplante_zustellung": geplante_zustellung,
                "quelle": "portal",
                "letzte_pruefung": jetzt(),
                "hinweise": hinweise,
            }
        )

    # -- Einrichtung -----------------------------------------------------------

    def aufzeichnen(self, referenz: str, ziel: Path | None = None) -> Path:
        """Speichert HTML und Screenshot der Sendungsseite zur Einrichtung der Selektoren.

        Das HTML kann personenbezogene Daten enthalten; es bleibt lokal unter ``ziel`` (Standard
        ``settings.tracking.screenshots_ordner/aufzeichnung/<Zeitstempel>_<Referenz>/``).
        """
        referenz = (referenz or "").strip()
        if not referenz:
            raise ValueError("aufzeichnen() braucht eine Referenz")
        ordner = ziel or (
            Path(self.tracking.screenshots_ordner) / "aufzeichnung" / f"{jetzt():%Y%m%d_%H%M%S}_{_dateiname_sicher(referenz)}"
        )
        ordner.mkdir(parents=True, exist_ok=True)
        seite = self._lade(self.suche_url(referenz), screenshot=ordner / "seite.png")
        (ordner / "seite.html").write_text(seite.html, encoding="utf-8")
        (ordner / "url.txt").write_text(seite.url + "\n", encoding="utf-8")
        if self.ist_login_seite(seite):
            logger.warning("Aufzeichnung zeigt die Anmeldeseite – %s", HINWEIS_LOGIN)
        logger.info("Portal-Aufzeichnung gespeichert: %s", ordner)
        return ordner

    def einrichten(self, *, warte: Callable[[], None] | None = None) -> bool:
        """Öffnet das Portal sichtbar im Automations-Profil und wartet, bis der Anwender Enter drückt.

        Der Anwender meldet sich selbst an (Benutzername, Passwort, ggf. zweiter Faktor); die
        Okta-Sitzung bleibt danach im Profil. Liefert ``True``, wenn die Seite nach dem Warten
        nicht mehr die Anmeldeseite ist.
        """
        warte = warte or _warte_auf_enter
        with self._browser(headless=False) as ctx:
            seite = ctx.pages[0] if ctx.pages else ctx.new_page()
            seite.goto(self._konfig.portal_url, wait_until="domcontentloaded")
            print(  # noqa: T201 - bewusste Konsolenausgabe für die Einrichtung
                "Bitte im geöffneten Chrome bei myHillebrandGori anmelden. "
                "Danach hier Enter drücken, um den Browser zu schließen."
            )
            warte()
            url = seite.url
            try:
                html = seite.content()
            except Exception:  # Seite kann gerade wechseln
                html = ""
        angemeldet = not self._konfig.ist_login_seite(html, url)
        logger.info("Portal-Einrichtung beendet, angemeldet=%s (Profil %s)", angemeldet, self.profil())
        return angemeldet

    # -- Laden -----------------------------------------------------------------

    def _lade(self, url: str, *, screenshot: Path | None = None) -> Seite:
        if self._lade_seite is not None:
            try:
                return self._lade_seite(url)
            except LaderFehler:
                raise
            except Exception as exc:
                raise LaderFehler(f"Seite {url} konnte nicht geladen werden: {type(exc).__name__}: {exc}") from exc
        return self._lade_mit_playwright(url, screenshot=screenshot)

    @contextmanager
    def _browser(self, *, headless: bool = False) -> Iterator[Any]:
        """Persistenter Chrome-Kontext im eigenen Profil (immer sichtbar, Recherche 3.4)."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise LaderFehler("Playwright ist nicht installiert (pip install playwright)") from exc
        profil = self.profil()
        profil.mkdir(parents=True, exist_ok=True)
        b = self._konfig.browser
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                str(profil),
                channel="chrome",
                headless=headless,
                viewport={"width": b.viewport_breite, "height": b.viewport_hoehe},
                slow_mo=b.slow_mo_ms,
            )
            try:
                yield ctx
            finally:
                ctx.close()

    def _lade_mit_playwright(self, url: str, *, screenshot: Path | None = None) -> Seite:
        try:
            from playwright.sync_api import Error as PlaywrightError
            from playwright.sync_api import TimeoutError as PlaywrightTimeout
        except ImportError as exc:
            raise LaderFehler("Playwright ist nicht installiert (pip install playwright)") from exc
        b = self._konfig.browser
        timeout_ms = int(b.timeout_sekunden * 1000)
        sel = self._konfig.selektoren.portal
        login_hosts = self._konfig.login_hosts
        try:
            with self._browser(headless=False) as ctx:
                seite = ctx.pages[0] if ctx.pages else ctx.new_page()
                seite.set_default_timeout(timeout_ms)
                seite.goto(self._konfig.portal_url, wait_until="domcontentloaded", timeout=timeout_ms)
                try:
                    seite.wait_for_url(
                        lambda u: not any(h in u for h in login_hosts), timeout=min(timeout_ms, 15_000)
                    )
                except PlaywrightTimeout:
                    pfad = self._screenshot(seite, "login_erforderlich", screenshot)
                    return Seite(url=seite.url, html=seite.content(), screenshot=pfad)
                seite.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                if sel.warten_auf:
                    try:
                        seite.wait_for_selector(sel.warten_auf, timeout=timeout_ms)
                    except PlaywrightTimeout:
                        logger.info("Portal: '%s' nicht innerhalb %s s erschienen", sel.warten_auf, b.timeout_sekunden)
                pfad = self._screenshot(seite, url.rsplit("/", 1)[-1], screenshot)
                return Seite(url=seite.url, html=seite.content(), screenshot=pfad)
        except (PlaywrightTimeout, PlaywrightError) as exc:
            raise LaderFehler(f"Browserfehler beim Laden von {url}: {exc}") from exc

    def _screenshot(self, seite: Any, name: str, ziel: Path | None) -> Path | None:
        try:
            if ziel is None:
                ordner = Path(self.tracking.screenshots_ordner) / "portal"
                ordner.mkdir(parents=True, exist_ok=True)
                ziel = ordner / f"{jetzt():%Y%m%d_%H%M%S}_{_dateiname_sicher(name)}.png"
            else:
                ziel.parent.mkdir(parents=True, exist_ok=True)
            seite.screenshot(path=str(ziel), full_page=True)
            return ziel
        except Exception as exc:  # Screenshot ist Komfort, nie Abbruchgrund
            logger.info("Screenshot nicht möglich: %s", exc)
            return None


def _warte_auf_enter() -> None:
    input("Enter drücken, wenn die Anmeldung abgeschlossen ist … ")
