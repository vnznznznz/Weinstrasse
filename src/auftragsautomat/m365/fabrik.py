"""Backends nach ``settings.m365.backend`` erzeugen und die Anmeldung prüfen (CLI ``m365-anmelden``)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from auftragsautomat.config import Settings
from auftragsautomat.m365.basis import (
    DateiBackend,
    ExcelBackend,
    M365Fehler,
    M365NichtAngemeldet,
    MailBackend,
)
from auftragsautomat.m365.fake import FakeDateiBackend, FakeExcelBackend, FakeMailBackend

__all__ = ["Backends", "anmelden", "erzeuge_backends", "pruefe_anmeldung"]

logger = logging.getLogger(__name__)


@dataclass
class Backends:
    """Die drei Backends eines Laufs plus der Name des gewählten Backends."""

    excel: ExcelBackend
    mail: MailBackend
    datei: DateiBackend
    backend: str = "fake"
    anmeldung: Any = None  # GraphAnmeldung bei Backend "graph"


def erzeuge_backends(settings: Settings) -> Backends:
    """Erzeugt Excel-, Mail- und Datei-Backend laut ``settings.m365.backend``.

    * ``graph``: :class:`GraphAnmeldung` (Token aus dem Cache; ohne Anmeldung wirft erst die erste
      Anfrage :class:`M365NichtAngemeldet`) + :class:`GraphClient` + Graph-Backends.
    * ``lokal``: Outlook/Excel per COM (nur Windows; außerhalb wirft die erste Nutzung
      ``NichtAufWindows``), Dateien über ``lokaler_pfad`` (Standardwurzel ``dossier.lokaler_ordner``).
    * ``fake``: In-Memory-Excel (lädt ``lokaler_pfad``-Dateien lesend), ``.eml``-Entwürfe nach
      ``kommunikation.entwuerfe_ordner``, Dateien unter ``dossier.lokaler_ordner``.
    """
    konfig = settings.m365
    if konfig.backend == "graph":
        from auftragsautomat.m365.auth import GraphAnmeldung
        from auftragsautomat.m365.graph import (
            GraphClient,
            GraphDateiBackend,
            GraphExcelBackend,
            GraphMailBackend,
        )

        anmeldung = GraphAnmeldung(konfig)
        client = GraphClient(anmeldung.token, anfragen_pro_sekunde=konfig.anfragen_pro_sekunde)
        return Backends(
            excel=GraphExcelBackend(client),
            mail=GraphMailBackend(client, postfach=konfig.postfach),
            datei=GraphDateiBackend(client),
            backend="graph",
            anmeldung=anmeldung,
        )
    if konfig.backend == "lokal":
        from auftragsautomat.m365.lokal import ExcelComBackend, LokalerDateiBackend, OutlookComMailBackend

        return Backends(
            excel=ExcelComBackend(),
            mail=OutlookComMailBackend(),
            datei=LokalerDateiBackend(settings.dossier.lokaler_ordner),
            backend="lokal",
        )
    if konfig.backend == "fake":
        return Backends(
            excel=FakeExcelBackend(),
            mail=FakeMailBackend(settings.kommunikation.entwuerfe_ordner),
            datei=FakeDateiBackend(settings.dossier.lokaler_ordner),
            backend="fake",
        )
    raise M365Fehler(f"Unbekanntes m365-Backend '{konfig.backend}' (graph | lokal | fake).")


def pruefe_anmeldung(settings: Settings) -> str:
    """Einzeilige Statusmeldung für die CLI: Backend, Konto, Token-Zustand (ohne Browser zu öffnen)."""
    konfig = settings.m365
    if konfig.backend != "graph":
        return f"Backend '{konfig.backend}': keine Microsoft-365-Anmeldung nötig."
    try:
        from auftragsautomat.m365.auth import GraphAnmeldung

        anmeldung = GraphAnmeldung(konfig)
    except M365Fehler as exc:
        return f"Graph nicht konfiguriert: {exc.meldung}"
    try:
        anmeldung.token()
    except M365NichtAngemeldet as exc:
        return f"Nicht angemeldet ({anmeldung.cache_pfad}): {exc.meldung}"
    except M365Fehler as exc:
        return f"Anmeldung fehlgeschlagen: {exc}"
    konto = anmeldung.konto() or "unbekanntes Konto"
    return f"Angemeldet als {konto} (Tenant {konfig.tenant_id or konfig.autoritaet}, Cache {anmeldung.cache_pfad})."


def anmelden(settings: Settings, *, device_code: bool = False, ausgabe: Callable[[str], Any] = print) -> str:
    """Interaktive Anmeldung (Systembrowser) bzw. per Gerätecode; liefert die Statusmeldung danach."""
    konfig = settings.m365
    if konfig.backend != "graph":
        return f"Backend '{konfig.backend}': keine Microsoft-365-Anmeldung nötig."
    from auftragsautomat.m365.auth import GraphAnmeldung

    anmeldung = GraphAnmeldung(konfig)
    if device_code:
        konto = anmeldung.anmelden_device_code(ausgabe)
    else:
        konto = anmeldung.anmelden_interaktiv(login_hint=settings.kommunikation.absender_email)
    logger.info("Microsoft-365-Anmeldung abgeschlossen für %s", konto or "(Konto unbekannt)")
    return f"Angemeldet als {konto or anmeldung.konto() or 'unbekanntes Konto'}; Token-Cache: {anmeldung.cache_pfad}."
