"""Einmalstart und Andocken an die laufende Lexware-Instanz.

Recherche (docs/recherche/ui_automation_lexware.md, Abschnitt 5): Der Hauptprozess heißt
``Framework.exe`` [V-Dritt]; eine zweite Instanz bringt "Die Anwendung kann nur einmal gestartet
werden". Konsequenz: nie starten, wenn der Prozess läuft – immer andocken. Kommandozeilenparameter
für Firma/Benutzer sind nicht belegt [NF]; ein Anmeldedialog (Benutzerverwaltung) wird als
unbekannter Dialog gemeldet.
"""

from __future__ import annotations

import csv
import logging
import subprocess
import sys
from collections.abc import Callable
from io import StringIO
from pathlib import Path
from typing import TYPE_CHECKING

from auftragsautomat.lexware.dialoge import DialogKontext, LexwareDoppelstart, behandle_dialoge

if TYPE_CHECKING:
    from auftragsautomat.config import Settings
    from auftragsautomat.lexware.ui_automation import Fenster, LexwareBackend

logger = logging.getLogger(__name__)

__all__ = [
    "PROZESSNAME_STANDARD",
    "InstanzFehler",
    "LexwareNichtGestartet",
    "aktive_firma",
    "lexware_laeuft",
    "parse_tasklist",
    "stelle_sicher_dass_lexware_laeuft",
]

PROZESSNAME_STANDARD = "Framework.exe"  # TODO-LEXWARE: im Task-Manager (Details) bestätigen


class InstanzFehler(Exception):
    """Lexware konnte nicht gefunden oder gestartet werden."""


class LexwareNichtGestartet(InstanzFehler):
    """Lexware läuft nicht und darf/kann nicht gestartet werden."""


def parse_tasklist(ausgabe: str) -> list[str]:
    """Liest die Prozessnamen aus ``tasklist /FO CSV /NH`` (erste Spalte, Anführungszeichen)."""
    namen: list[str] = []
    for zeile in csv.reader(StringIO(ausgabe)):
        if zeile and zeile[0].strip() and not zeile[0].lower().startswith("info:"):
            namen.append(zeile[0].strip())
    return namen


def _prozessliste_windows() -> list[str]:  # pragma: no cover - nur Windows
    try:
        import psutil  # type: ignore[import-not-found]

        return [p.info["name"] or "" for p in psutil.process_iter(["name"])]
    except ImportError:
        pass
    ergebnis = subprocess.run(
        ["tasklist", "/FO", "CSV", "/NH"],
        capture_output=True,
        text=True,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if ergebnis.returncode != 0:
        logger.warning("tasklist fehlgeschlagen (%s): %s", ergebnis.returncode, ergebnis.stderr.strip())
        return []
    return parse_tasklist(ergebnis.stdout)


def lexware_laeuft(
    prozessname: str = PROZESSNAME_STANDARD,
    *,
    prozessliste: Callable[[], list[str]] | None = None,
    plattform: str | None = None,
) -> bool:
    """True, wenn ein Prozess ``prozessname`` läuft (Windows: psutil, sonst tasklist; anderswo False).

    ``prozessliste`` erlaubt Tests das Einschleusen einer Prozessliste.
    """
    plattform = plattform or sys.platform
    if prozessliste is None:
        if plattform != "win32":
            logger.debug("Prozessprüfung für %s nur unter Windows möglich (hier %s)", prozessname, plattform)
            return False
        prozessliste = _prozessliste_windows
    gesucht = prozessname.casefold()
    treffer = [n for n in prozessliste() if n and n.casefold() == gesucht]
    logger.debug("Prozess %s: %d Treffer", prozessname, len(treffer))
    return bool(treffer)


def aktive_firma(backend: LexwareBackend) -> str:
    """Titel des Hauptfensters (die aktive Firma steht laut Handbuch in der Titelleiste)."""
    titel = backend.aktive_firma()
    if not titel or not titel.strip():
        raise InstanzFehler("Lexware-Hauptfenster hat keinen Titel; aktive Firma nicht feststellbar")
    return titel.strip()


def stelle_sicher_dass_lexware_laeuft(
    settings: Settings,
    backend: LexwareBackend,
    *,
    screenshot: Callable[[str], Path | None] | None = None,
) -> Fenster:
    """Dockt an die laufende Instanz an; startet Lexware nur, wenn kein Prozess läuft.

    Nie doppelt starten (Einmalstart-Sperre). Erscheint trotzdem "nur einmal gestartet",
    wird ``LexwareDoppelstart`` (mit Screenshot) geworfen. Liefert das Hauptfenster.
    """
    prozessname = getattr(backend.controls, "PROZESSNAME", PROZESSNAME_STANDARD)
    if backend.prozess_laeuft():
        logger.info("%s läuft bereits – docke an, kein Neustart", prozessname)
        fenster = backend.finde_hauptfenster()
        behandle_dialoge(backend, DialogKontext.START, screenshot=screenshot)
        return fenster

    exe = settings.lexware.exe_pfad
    if exe is None:
        raise LexwareNichtGestartet(
            f"{prozessname} läuft nicht und settings.lexware.exe_pfad ist nicht konfiguriert; "
            "bitte Lexware manuell starten"
        )
    exe = Path(exe)
    if not exe.exists():
        raise LexwareNichtGestartet(f"{prozessname} läuft nicht und {exe} existiert nicht")
    # Zweite Prüfung unmittelbar vor dem Start (Rennbedingung mit manuellem Start)
    if backend.prozess_laeuft():
        logger.info("%s inzwischen gestartet – docke an", prozessname)
        return backend.finde_hauptfenster()
    logger.warning("Starte Lexware über %s", exe)
    backend.starte(exe)
    timeout = max(float(settings.lexware.maximale_wartezeit_sekunden), 1.0) * 4  # zweistufiger Start (Rahmen, Module)
    gestartet = backend.warte_bis(
        lambda: backend.prozess_laeuft() or bool(backend.offene_dialoge()),
        timeout=timeout,
        beschreibung=f"Prozess {prozessname} nach Start",
    )
    try:
        behandle_dialoge(backend, DialogKontext.START, screenshot=screenshot)
    except LexwareDoppelstart:
        logger.error("Einmalstart-Sperre: Lexware lief offenbar doch schon – Abbruch")
        raise
    if not gestartet and not backend.prozess_laeuft():
        raise LexwareNichtGestartet(f"{prozessname} ist nach {timeout:.0f}s nicht gestartet")
    fenster = backend.finde_hauptfenster(timeout=timeout)
    logger.info("Lexware gestartet, Hauptfenster %r", fenster.titel)
    return fenster
