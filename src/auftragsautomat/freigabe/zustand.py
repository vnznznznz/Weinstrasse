"""Paket-Zustand: Belegpakete als JSON speichern, laden, auflisten und löschen.

Ein :class:`~auftragsautomat.models.Belegpaket` wird nach dem Prüfen (CLI ``pruefen``, Watcher,
Freigabe-UI) als ``<quelldatei-stamm>.paket.json`` abgelegt, damit die Freigabe später – auch
nach einem Neustart – darauf zugreifen kann und die CLI ``buchen`` ein geprüftes Paket bucht.

* :func:`speichere_paket` schreibt das Paket mit ``model_dump_json(indent=2)`` (Decimal, Datum und
  Enums werden von pydantic verlustfrei serialisiert). Geschrieben wird atomar (temporäre Datei,
  dann ``replace``), damit ein paralleler Leser nie eine halbe Datei sieht.
* :func:`lade_paket` liest mit ``Belegpaket.model_validate_json``; berechnete Felder
  (``blockiert``, ``gesamtmenge`` …) stehen zwar im JSON, werden beim Laden aber ignoriert und neu
  berechnet.
* :func:`liste_pakete` liefert alle Pakete eines Ordners, neueste zuerst.
* :func:`loesche_paket` entfernt ein Paket.

Standardordner ist ``settings.log_ordner / "pakete"`` (:func:`paket_ordner`).
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

from auftragsautomat.config import Settings
from auftragsautomat.models import Belegpaket

__all__ = [
    "PAKET_ENDUNG",
    "PAKET_UNTERORDNER",
    "PaketFehler",
    "PaketInfo",
    "beschreibe_paket",
    "lade_paket",
    "liste_pakete",
    "loesche_paket",
    "paket_dateiname",
    "paket_ordner",
    "speichere_paket",
]

logger = logging.getLogger(__name__)

PAKET_ENDUNG = ".paket.json"
PAKET_UNTERORDNER = "pakete"
_UNSICHERE_ZEICHEN = re.compile(r"[^A-Za-z0-9._-]+")
_MAX_STAMM_LAENGE = 80


class PaketFehler(ValueError):
    """Ein Paket konnte nicht gelesen werden (kein JSON, ungültiger Inhalt)."""


@dataclass(frozen=True)
class PaketInfo:
    """Kurzbeschreibung eines gespeicherten Pakets für Listen (Seitenleiste, CLI)."""

    pfad: Path
    quelldatei: str
    firma_id: str | None
    kunde: str | None
    blockiert: bool
    freigegeben: bool
    geaendert: datetime

    @property
    def anzeige(self) -> str:
        status = "blockiert" if self.blockiert else ("freigegeben" if self.freigegeben else "freigabefähig")
        teile = [self.quelldatei, self.firma_id or "Firma ?", self.kunde or "Kunde ?", status]
        return " – ".join(teile)


def paket_ordner(settings: Settings) -> Path:
    """Standardordner für gespeicherte Pakete: ``<log_ordner>/pakete``."""
    return Path(settings.log_ordner) / PAKET_UNTERORDNER


def _sicherer_stamm(name: str) -> str:
    stamm = _UNSICHERE_ZEICHEN.sub("_", name).strip("._")
    return (stamm or "paket")[:_MAX_STAMM_LAENGE]


def paket_dateiname(paket: Belegpaket) -> str:
    """Dateiname ``<quelldatei-stamm>.paket.json`` (unsichere Zeichen durch ``_`` ersetzt)."""
    return _sicherer_stamm(Path(paket.roh.quelldatei).stem) + PAKET_ENDUNG


def speichere_paket(paket: Belegpaket, ordner: Path, *, dateiname: str | None = None) -> Path:
    """Speichert das Paket als JSON im Ordner (wird angelegt) und liefert den Pfad.

    Der Dateiname ergibt sich aus der Quelldatei (:func:`paket_dateiname`); ``dateiname`` erzwingt
    einen anderen Namen (z. B. um eine umbenannte Paketdatei an Ort und Stelle zu aktualisieren).
    Ein vorhandenes Paket desselben Namens wird ersetzt (der Zustand ist immer der aktuelle).
    """
    ordner = Path(ordner)
    ordner.mkdir(parents=True, exist_ok=True)
    ziel = ordner / (dateiname or paket_dateiname(paket))
    inhalt = paket.model_dump_json(indent=2)
    temp = ziel.with_name(f"{ziel.name}.{os.getpid()}.tmp")
    try:
        temp.write_text(inhalt, encoding="utf-8")
        os.replace(temp, ziel)
    finally:
        if temp.exists():
            try:
                temp.unlink()
            except OSError:  # pragma: no cover - Aufräumen ist nur Kosmetik
                logger.debug("Temporäre Datei %s konnte nicht gelöscht werden", temp)
    logger.info(
        "Paket gespeichert: %s (Firma %s, %s, %d Positionen)",
        ziel,
        paket.firma_id or "-",
        "blockiert" if paket.blockiert else "freigabefähig",
        len(paket.positionen),
    )
    return ziel


def lade_paket(pfad: Path) -> Belegpaket:
    """Lädt ein gespeichertes Paket. ``FileNotFoundError`` wenn die Datei fehlt,
    :class:`PaketFehler` bei ungültigem Inhalt."""
    pfad = Path(pfad)
    if not pfad.is_file():
        raise FileNotFoundError(f"Paketdatei nicht gefunden: {pfad}")
    try:
        inhalt = pfad.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise PaketFehler(f"Paketdatei {pfad} ist nicht UTF-8-kodiert: {exc}") from exc
    try:
        paket = Belegpaket.model_validate_json(inhalt)
    except ValidationError as exc:
        fehlerliste = exc.errors()
        if any(e.get("type") == "json_invalid" for e in fehlerliste):
            grund = "; ".join(str(e.get("msg")) for e in fehlerliste[:3])
            raise PaketFehler(f"Paketdatei {pfad} enthält kein gültiges JSON: {grund}") from exc
        fehler = "; ".join(
            f"{'.'.join(str(t) for t in e.get('loc', ())) or '(Wurzel)'}: {e.get('msg')}" for e in fehlerliste[:5]
        )
        raise PaketFehler(f"Paketdatei {pfad} ist ungültig: {fehler}") from exc
    except ValueError as exc:  # pragma: no cover - pydantic meldet JSON-Fehler als ValidationError
        raise PaketFehler(f"Paketdatei {pfad} enthält kein gültiges JSON: {exc}") from exc
    logger.debug("Paket geladen: %s (Firma %s)", pfad, paket.firma_id or "-")
    return paket


def liste_pakete(ordner: Path) -> list[Path]:
    """Alle ``*.paket.json`` im Ordner, neueste (Änderungszeit) zuerst. Fehlender Ordner -> leere Liste."""
    ordner = Path(ordner)
    if not ordner.is_dir():
        return []
    pakete = [p for p in ordner.glob(f"*{PAKET_ENDUNG}") if p.is_file()]
    pakete.sort(key=lambda p: (p.stat().st_mtime, p.name), reverse=True)
    return pakete


def loesche_paket(pfad: Path) -> bool:
    """Löscht ein gespeichertes Paket; True wenn gelöscht, False wenn es nicht (mehr) existiert."""
    pfad = Path(pfad)
    if not pfad.exists():
        return False
    pfad.unlink()
    logger.info("Paket gelöscht: %s", pfad)
    return True


def beschreibe_paket(pfad: Path) -> PaketInfo:
    """Kurzinfo aus einer Paketdatei ohne vollständige Validierung (für Listen).

    Fehlerhafte Dateien ergeben eine Info mit Fragezeichen statt einer Exception.
    """
    pfad = Path(pfad)
    try:
        geaendert = datetime.fromtimestamp(pfad.stat().st_mtime)
    except OSError:
        geaendert = datetime.fromtimestamp(0)
    quelldatei = pfad.name[: -len(PAKET_ENDUNG)] if pfad.name.endswith(PAKET_ENDUNG) else pfad.stem
    try:
        daten = json.loads(pfad.read_text(encoding="utf-8"))
        roh = daten.get("roh") or {}
        kunde = ((daten.get("kunde") or {}).get("kunde") or {}).get("name")
        return PaketInfo(
            pfad=pfad,
            quelldatei=Path(str(roh.get("quelldatei") or quelldatei)).name,
            firma_id=daten.get("firma_id"),
            kunde=kunde or roh.get("kunde_text"),
            blockiert=bool(daten.get("blockiert", False)),
            freigegeben=bool(daten.get("freigegeben", False)),
            geaendert=geaendert,
        )
    except (OSError, ValueError, AttributeError) as exc:
        logger.warning("Paketdatei %s nicht lesbar: %s", pfad, exc)
        return PaketInfo(
            pfad=pfad,
            quelldatei=quelldatei,
            firma_id=None,
            kunde=None,
            blockiert=True,
            freigegeben=False,
            geaendert=geaendert,
        )
