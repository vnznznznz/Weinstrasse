"""Eingangs-Erkennung und Dispatcher der Extraktion.

:func:`erkenne_eingangstyp` entscheidet anhand der Datei, welcher Extraktor zuständig ist:

* ``.pdf`` mit Textebene            -> :attr:`Eingangstyp.KUNDENBESTELLUNG_PDF` (A, ``text_pdf``)
* ``.pdf`` ohne Textebene, Bilder    -> :attr:`Eingangstyp.PREISLISTE_SCAN` (C, ``vision``)
* ``.eml`` / ``.msg`` / ``.txt``     -> :attr:`Eingangstyp.EIGENE_BESTELLUNG_EMAIL` (B, ``email_bestellung``)
* alles andere                       -> :class:`NichtUnterstuetzteDatei` (``ValueError``)

Das ist die *Route*; der gewählte Extraktor kann den Typ im Ergebnis noch verfeinern
(``text_pdf`` liefert z. B. B für ein als PDF gedrucktes eigenes Bestellformular).

:func:`extrahiere` ruft den passenden Extraktor auf. Das Vision-Modul wird erst bei Bedarf
importiert (es benötigt ``anthropic``/``PIL``); in Tests kann es über ``sys.modules`` durch
einen Fake ersetzt werden. Die Firma wird NICHT hier ermittelt (siehe ``matching.firma``).
"""

from __future__ import annotations

import importlib
import logging
from pathlib import Path
from typing import Any

from auftragsautomat.config import Settings
from auftragsautomat.extraktion.basis import Extraktor
from auftragsautomat.extraktion.email_bestellung import EMAIL_ENDUNGEN, EmailExtraktor
from auftragsautomat.extraktion.text_pdf import TextPdfExtraktor, ist_text_pdf
from auftragsautomat.models import Eingangstyp, RohBestellung

__all__ = [
    "BILD_ENDUNGEN",
    "EMAIL_ENDUNGEN",
    "PDF_ENDUNGEN",
    "UNTERSTUETZTE_ENDUNGEN",
    "ExtraktorNichtVerfuegbar",
    "NichtUnterstuetzteDatei",
    "erkenne_eingangstyp",
    "extrahiere",
    "ist_unterstuetzt",
    "waehle_extraktor",
]

logger = logging.getLogger(__name__)

PDF_ENDUNGEN: frozenset[str] = frozenset({".pdf"})
BILD_ENDUNGEN: frozenset[str] = frozenset({".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".gif", ".webp"})
#: Alle Endungen, die der Ordner-Watcher aufnehmen darf (Groß-/Kleinschreibung egal).
UNTERSTUETZTE_ENDUNGEN: frozenset[str] = PDF_ENDUNGEN | BILD_ENDUNGEN | EMAIL_ENDUNGEN

_VISION_MODUL = "auftragsautomat.extraktion.vision"


class NichtUnterstuetzteDatei(ValueError):
    """Die Datei hat keine unterstützte Endung."""


class ExtraktorNichtVerfuegbar(RuntimeError):
    """Der zuständige Extraktor kann nicht geladen werden (fehlende Abhängigkeit)."""


def ist_unterstuetzt(datei: Path) -> bool:
    """True, wenn die Dateiendung von einem Extraktor verarbeitet wird (für den Watcher)."""
    return Path(datei).suffix.lower() in UNTERSTUETZTE_ENDUNGEN


def erkenne_eingangstyp(datei: Path) -> Eingangstyp:
    """Bestimmt den Eingangstyp (die Extraktor-Route) anhand von Endung und Textebene.

    Wirft :class:`NichtUnterstuetzteDatei` (ein ``ValueError``) bei unbekannter Endung und
    ``FileNotFoundError``, wenn die Datei fehlt.
    """
    datei = Path(datei)
    endung = datei.suffix.lower()
    if endung not in UNTERSTUETZTE_ENDUNGEN:
        raise NichtUnterstuetzteDatei(
            f"Datei '{datei.name}' kann nicht verarbeitet werden: Endung '{endung or '(keine)'}' wird nicht "
            f"unterstützt. Erlaubt sind {', '.join(sorted(UNTERSTUETZTE_ENDUNGEN))}."
        )
    if not datei.is_file():
        raise FileNotFoundError(f"Eingangsdatei nicht gefunden: {datei}")
    if endung in EMAIL_ENDUNGEN:
        typ = Eingangstyp.EIGENE_BESTELLUNG_EMAIL
    elif endung in BILD_ENDUNGEN:
        typ = Eingangstyp.PREISLISTE_SCAN
    elif ist_text_pdf(datei):
        typ = Eingangstyp.KUNDENBESTELLUNG_PDF
    else:
        typ = Eingangstyp.PREISLISTE_SCAN  # Bild-PDF (Scan) oder nicht lesbar -> Vision versucht es
    logger.debug("%s: Eingangstyp %s erkannt", datei.name, typ.value)
    return typ


def _lade_vision_extraktor(settings: Settings, claude_client: Any | None) -> Extraktor:
    try:
        vision = importlib.import_module(_VISION_MODUL)
    except ImportError as exc:
        raise ExtraktorNichtVerfuegbar(
            f"Vision-Extraktion (Eingangstyp C) nicht verfügbar: Modul {_VISION_MODUL} kann nicht geladen "
            f"werden ({exc}). Sind 'anthropic' und 'Pillow' installiert?"
        ) from exc
    try:
        klasse = vision.VisionExtraktor
    except AttributeError as exc:
        raise ExtraktorNichtVerfuegbar(f"Modul {_VISION_MODUL} stellt keinen VisionExtraktor bereit.") from exc
    return klasse(settings, client=claude_client)


def waehle_extraktor(typ: Eingangstyp, settings: Settings, *, claude_client: Any | None = None) -> Extraktor:
    """Liefert den Extraktor für einen Eingangstyp (A: text_pdf, B: email_bestellung, C: vision)."""
    if typ == Eingangstyp.KUNDENBESTELLUNG_PDF:
        return TextPdfExtraktor(settings)
    if typ == Eingangstyp.EIGENE_BESTELLUNG_EMAIL:
        return EmailExtraktor(settings)
    if typ == Eingangstyp.PREISLISTE_SCAN:
        return _lade_vision_extraktor(settings, claude_client)
    raise ValueError(f"Unbekannter Eingangstyp: {typ!r}")  # pragma: no cover - Enum ist vollständig


def extrahiere(datei: Path, settings: Settings, *, claude_client: Any | None = None) -> RohBestellung:
    """Erkennt den Eingangstyp und extrahiert die Datei mit dem zuständigen Extraktor.

    ``claude_client`` wird nur an den Vision-Extraktor (Typ C) durchgereicht; ohne Angabe legt
    dieser bei Bedarf selbst einen Client an.
    """
    datei = Path(datei)
    typ = erkenne_eingangstyp(datei)
    extraktor = waehle_extraktor(typ, settings, claude_client=claude_client)
    logger.info("%s: Eingangstyp %s -> %s", datei.name, typ.value, type(extraktor).__name__)
    roh = extraktor.extrahiere(datei)
    if roh.eingangstyp != typ:
        logger.info("%s: Extraktor hat den Eingangstyp auf %s verfeinert", datei.name, roh.eingangstyp.value)
    return roh
