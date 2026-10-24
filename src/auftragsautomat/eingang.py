"""Der eigene Eingang des Tools und der Upload aus der Freigabe-UI.

Design: ``docs/AUSBAUSTUFE_2.md`` Abschnitt 9, Geschäftsregel 20. Der Eingang ist der OneDrive-Ordner
"Eingang" (``eingang.ordner_name``); ``Settings.eingang_ordner`` zeigt auf seinen lokal
synchronisierten Pfad. Dort legen Kunden bzw. Verkäufer ihre Aufträge ab, und dort landen auch die
in der Freigabe-UI hochgeladenen Dateien ("Auftrag hochladen"). Watcher, Freigabe-UI und
Verarbeitung lesen denselben Ordner; hochgeladene Dateien gelten als normaler Eingang
(extrahieren und prüfen ja, buchen nur nach Freigabe).

* :func:`speichere_upload` prüft Dateityp (``eingang.upload_dateitypen``) und Größe
  (``eingang.upload_max_mb``) – Verstöße ergeben :class:`UploadAbgelehnt` mit deutscher Meldung –,
  bereinigt den Dateinamen (``benennung.bereinige``), wählt bei Kollision einen Namen mit Zeitstempel
  bzw. Zähler und schreibt **atomar**: erst in eine temporäre Datei im selben Ordner
  (``.upload_….tmp``, vom Watcher und von OneDrive ignoriert), dann ``os.replace``. So sieht der
  Watcher nur fertige Dateien.
* :func:`liste_eingang` liefert die Dateien im Eingang (rekursiv, nur erlaubte Typen, ohne
  temporäre Dateien und ohne den Rabattlisten-Ordner ``rabatte.rabatt_pdf_ordner``).
"""

from __future__ import annotations

import logging
import os
import tempfile
from collections.abc import Callable
from datetime import datetime
from pathlib import Path, PurePosixPath

from auftragsautomat.benennung import bereinige
from auftragsautomat.config import Settings

__all__ = [
    "IGNORIERTE_ENDUNGEN",
    "IGNORIERTE_PRAEFIXE",
    "TEMP_ENDUNG",
    "TEMP_PRAEFIX",
    "UploadAbgelehnt",
    "eingang_ordner",
    "erlaubte_endungen",
    "freier_pfad",
    "ist_erlaubter_typ",
    "ist_temporaer",
    "liste_eingang",
    "pruefe_upload",
    "schreibe_atomar",
    "speichere_upload",
]

logger = logging.getLogger(__name__)

#: Präfix/Endung der temporären Datei beim atomaren Schreiben. Die Endung ``.tmp`` ignorieren der
#: Watcher (``watcher._IGNORIERTE_ENDUNGEN``) und der OneDrive-Client.
TEMP_PRAEFIX = ".upload_"
TEMP_ENDUNG = ".tmp"
#: Dateinamen, die nie als Eingang gelten (Office-Sperrdateien, Downloads, eigene Temp-Dateien).
IGNORIERTE_PRAEFIXE: tuple[str, ...] = ("~$", ".~", "._", TEMP_PRAEFIX)
IGNORIERTE_ENDUNGEN: tuple[str, ...] = (TEMP_ENDUNG, ".part", ".crdownload", ".partial")
#: Dateiname, wenn vom Originalnamen nach dem Bereinigen nichts übrig bleibt.
_ERSATZNAME = "Auftrag"
#: Höchster Zähler bei Namenskollisionen.
_MAX_ZAEHLER = 999

Uhr = Callable[[], datetime]


class UploadAbgelehnt(ValueError):
    """Der Upload wurde abgelehnt (Dateityp, Größe, leerer Inhalt, Upload deaktiviert, unbekannte Firma)."""


# ---------------------------------------------------------------------------
# Prüfungen
# ---------------------------------------------------------------------------


def erlaubte_endungen(settings: Settings) -> tuple[str, ...]:
    """Erlaubte Endungen laut ``eingang.upload_dateitypen``, klein geschrieben und mit Punkt."""
    endungen: list[str] = []
    for typ in settings.eingang.upload_dateitypen:
        t = typ.strip().lower()
        if not t:
            continue
        if not t.startswith("."):
            t = "." + t
        if t not in endungen:
            endungen.append(t)
    return tuple(endungen)


def ist_erlaubter_typ(datei: Path | str, settings: Settings) -> bool:
    """True, wenn die Endung der Datei in ``eingang.upload_dateitypen`` steht (Groß-/Kleinschreibung egal)."""
    return Path(datei).suffix.lower() in erlaubte_endungen(settings)


def ist_temporaer(datei: Path | str) -> bool:
    """True für Sperr-, Download- und eigene Temp-Dateien, die nie als Eingang gelten."""
    name = Path(datei).name
    return name.startswith(IGNORIERTE_PRAEFIXE) or name.lower().endswith(IGNORIERTE_ENDUNGEN)


def _basisname(dateiname: str) -> str:
    """Nur der Dateiname ohne Ordneranteile (Windows- und POSIX-Trenner), ohne Rand-Leerzeichen."""
    return PurePosixPath((dateiname or "").replace("\\", "/")).name.strip()


def pruefe_upload(dateiname: str, groesse: int, settings: Settings) -> str:
    """Prüft Dateiname, Dateityp und Größe eines Uploads und liefert den bereinigten Zielnamen
    (Stamm über ``benennung.bereinige``, Endung klein geschrieben).

    Wirft :class:`UploadAbgelehnt` (ein ``ValueError``) mit deutscher Meldung, wenn der Upload
    deaktiviert ist (``eingang.upload_erlaubt``), der Name fehlt, der Dateityp nicht in
    ``eingang.upload_dateitypen`` steht, die Datei leer ist oder ``eingang.upload_max_mb`` überschreitet.
    """
    k = settings.eingang
    if not k.upload_erlaubt:
        raise UploadAbgelehnt("Der Upload ist deaktiviert (eingang.upload_erlaubt = false).")
    name = _basisname(dateiname)
    if not name:
        raise UploadAbgelehnt("Der Dateiname fehlt.")
    endung = Path(name).suffix.lower()
    erlaubt = erlaubte_endungen(settings)
    if endung not in erlaubt:
        raise UploadAbgelehnt(
            f"Dateityp '{endung or '(ohne Endung)'}' wird nicht unterstützt. Erlaubt sind: {', '.join(erlaubt)}."
        )
    if groesse <= 0:
        raise UploadAbgelehnt(f"Die Datei '{name}' ist leer (0 Byte).")
    maximal = k.upload_max_mb * 1024 * 1024
    if groesse > maximal:
        raise UploadAbgelehnt(
            f"Die Datei '{name}' ist zu groß ({groesse / (1024 * 1024):.1f} MB); "
            f"erlaubt sind höchstens {k.upload_max_mb} MB."
        )
    stamm = bereinige(name[: -len(endung)] if endung else name, settings) or _ERSATZNAME
    return stamm + endung


# ---------------------------------------------------------------------------
# Zielordner und Dateiname
# ---------------------------------------------------------------------------


def eingang_ordner(settings: Settings, firma_id: str | None = None) -> Path:
    """Zielordner im Eingang: ``settings.eingang_ordner``; mit ``firma_id`` und
    ``eingang.upload_in_firmen_unterordner`` der Unterordner ``Firma.eingang_unterordner``
    (sonst die Firmen-ID). Unbekannte Firma -> :class:`UploadAbgelehnt`. Der Ordner wird nicht angelegt."""
    ordner = Path(settings.eingang_ordner)
    if firma_id and settings.eingang.upload_in_firmen_unterordner:
        try:
            firma = settings.firma(firma_id)
        except KeyError as exc:
            raise UploadAbgelehnt(f"Unbekannte Firma '{firma_id}'. Bekannt: {', '.join(settings.firmen)}.") from exc
        ordner = ordner / (firma.eingang_unterordner or firma.id)
    return ordner


def freier_pfad(ordner: Path, name: str, *, jetzt: datetime) -> Path:
    """Pfad ``ordner/name``; existiert er schon, ``Name <JJJJ-MM-TT HH-MM-SS>.pdf``, danach
    ``Name <Zeit> (2).pdf`` usw. Es wird nie ein vorhandener Pfad zurückgegeben."""
    ziel = ordner / name
    if not ziel.exists():
        return ziel
    endung = Path(name).suffix
    stamm = name[: -len(endung)] if endung else name
    stamm_zeit = f"{stamm} {jetzt:%Y-%m-%d %H-%M-%S}"
    ziel = ordner / f"{stamm_zeit}{endung}"
    zaehler = 2
    while ziel.exists():
        if zaehler > _MAX_ZAEHLER:
            raise UploadAbgelehnt(f"Kein freier Dateiname für '{name}' in {ordner}.")
        ziel = ordner / f"{stamm_zeit} ({zaehler}){endung}"
        zaehler += 1
    return ziel


def schreibe_atomar(ziel: Path, inhalt: bytes) -> Path:
    """Schreibt ``inhalt`` atomar nach ``ziel``: temporäre Datei (``.upload_….tmp``) im selben Ordner,
    ``fsync``, dann ``os.replace``. Der Watcher sieht die Datei erst, wenn sie vollständig ist; bei
    einem Fehler bleibt weder Ziel noch Temp-Datei zurück."""
    ziel = Path(ziel)
    ziel.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=TEMP_PRAEFIX, suffix=TEMP_ENDUNG, dir=ziel.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(inhalt)
            f.flush()
            os.fsync(f.fileno())
        # TODO-PRUEFEN: Am Windows-PC mit OneDrive prüfen, dass os.replace im synchronisierten Ordner
        # "Eingang" durchläuft, während der OneDrive-Client die Temp-Datei (.tmp) ignoriert.
        os.replace(tmp, ziel)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return ziel


# ---------------------------------------------------------------------------
# Öffentliche API
# ---------------------------------------------------------------------------


def speichere_upload(
    dateiname: str,
    inhalt: bytes,
    settings: Settings,
    *,
    firma_id: str | None = None,
    jetzt: Uhr | None = None,
) -> Path:
    """Speichert eine hochgeladene Datei im Eingang und liefert den Zielpfad.

    Prüfung über :func:`pruefe_upload` (Dateityp, Größe; :class:`UploadAbgelehnt`), Zielordner über
    :func:`eingang_ordner` (``eingang/[<Firma.eingang_unterordner>/]``), Dateiname bereinigt, bei
    Kollision Zeitstempel/Zähler (:func:`freier_pfad`), Schreiben atomar (:func:`schreibe_atomar`).
    ``jetzt`` liefert den Zeitstempel für Kollisionsnamen (Standard ``datetime.now``).
    """
    name = pruefe_upload(dateiname, len(inhalt), settings)
    ordner = eingang_ordner(settings, firma_id)
    ordner.mkdir(parents=True, exist_ok=True)
    ziel = freier_pfad(ordner, name, jetzt=(jetzt or datetime.now)())
    if ziel.name != _basisname(dateiname):
        logger.info("Upload '%s' wird als '%s' gespeichert.", dateiname, ziel.name)
    schreibe_atomar(ziel, inhalt)
    logger.info("Upload gespeichert: %s (%d Byte%s)", ziel, len(inhalt), f", Firma {firma_id}" if firma_id else "")
    return ziel


def _liegt_unter(pfad: Path, ordner: Path | None) -> bool:
    if ordner is None:
        return False
    try:
        pfad.resolve().relative_to(ordner.resolve())
    except ValueError:
        return False
    return True


def liste_eingang(settings: Settings) -> list[Path]:
    """Alle Eingangsdateien unter ``settings.eingang_ordner`` (rekursiv, sortiert): nur erlaubte Typen
    (``eingang.upload_dateitypen``), ohne temporäre Dateien und ohne den Rabattlisten-Ordner
    ``rabatte.rabatt_pdf_ordner``, falls er im Eingang liegt."""
    ordner = Path(settings.eingang_ordner)
    if not ordner.is_dir():
        return []
    rabatte = Path(settings.rabatte.rabatt_pdf_ordner) if settings.rabatte.rabatt_pdf_ordner else None
    return sorted(
        p
        for p in ordner.rglob("*")
        if p.is_file() and ist_erlaubter_typ(p, settings) and not ist_temporaer(p) and not _liegt_unter(p, rabatte)
    )
