"""Ordner-Watcher: neue Eingangsdateien erkennen und an eine Verarbeitung übergeben.

:func:`starte_watch` beobachtet ``settings.eingang_ordner`` rekursiv (watchdog ``Observer``) und
ruft ``verarbeite(datei)`` für jede neue oder hineinverschobene Datei mit unterstützter Endung
(:data:`~auftragsautomat.extraktion.erkennung.UNTERSTUETZTE_ENDUNGEN`) auf. Es wird **nie
automatisch gebucht** – die Verarbeitung ist Extraktion + Prüfung + Paket speichern
(:func:`verarbeite_standard`), die Freigabe bleibt beim Anwender.

Besonderheiten:

* Eine Datei gilt erst als fertig geschrieben, wenn ihre Größe über ``stabil_sekunden`` (Standard
  2 s) unverändert bleibt und sie geöffnet werden kann. Unter Windows hält der Explorer eine Datei
  während des Kopierens exklusiv offen; dann wird gewartet statt eine halbe Datei zu lesen.
* Bereits verarbeitete Dateien werden gemerkt (:class:`Verarbeitungsliste`: Set im Speicher und
  Datei ``<log_ordner>/verarbeitet.txt``), damit ein Neustart nichts doppelt verarbeitet. Gemerkt
  wird Pfad + Größe + Änderungszeit; eine geänderte Datei gleichen Namens wird erneut verarbeitet.
* ``einmalig=True`` verarbeitet die vorhandenen Dateien und beendet sich (CLI ``watch --einmalig``,
  Tests). Ohne ``einmalig`` läuft die Schleife bis ``Strg+C`` bzw. ``stop_ereignis``.
* Fehler in ``verarbeite`` werden geloggt, die Datei wird als (fehlerhaft) verarbeitet gemerkt und
  der Watcher läuft weiter.
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from auftragsautomat.config import Settings
from auftragsautomat.extraktion.erkennung import UNTERSTUETZTE_ENDUNGEN, ist_unterstuetzt

__all__ = [
    "STABIL_SEKUNDEN",
    "VERARBEITET_DATEINAME",
    "EingangsHandler",
    "Verarbeitungsliste",
    "finde_eingangsdateien",
    "ist_fertig_geschrieben",
    "starte_watch",
    "verarbeite_standard",
    "warte_bis_fertig",
]

logger = logging.getLogger(__name__)

#: Dateiname der Merkliste verarbeiteter Dateien (im Log-Ordner).
VERARBEITET_DATEINAME = "verarbeitet.txt"
#: Sekunden, die die Dateigröße unverändert bleiben muss, bevor verarbeitet wird.
STABIL_SEKUNDEN = 2.0
#: Maximale Wartezeit auf eine fertig geschriebene Datei (große Scans, langsame Netzlaufwerke).
MAX_WARTEZEIT_SEKUNDEN = 300.0
_IGNORIERTE_PRAEFIXE = ("~$", ".~", "._")
_IGNORIERTE_ENDUNGEN = (".tmp", ".part", ".crdownload", ".partial")

Verarbeiter = Callable[[Path], None]


# ---------------------------------------------------------------------------
# Hilfen
# ---------------------------------------------------------------------------


def _ist_kandidat(pfad: Path) -> bool:
    """Unterstützte Endung und kein temporärer Dateiname (Office-Sperrdateien, Downloads)."""
    name = pfad.name
    if name.startswith(_IGNORIERTE_PRAEFIXE) or name.lower().endswith(_IGNORIERTE_ENDUNGEN):
        return False
    return ist_unterstuetzt(pfad)


def finde_eingangsdateien(ordner: Path) -> list[Path]:
    """Alle unterstützten Dateien im Ordner (rekursiv), stabil sortiert."""
    ordner = Path(ordner)
    if not ordner.is_dir():
        return []
    return sorted(p for p in ordner.rglob("*") if p.is_file() and _ist_kandidat(p))


def _kann_geoeffnet_werden(datei: Path) -> bool:
    """Prüft, ob die Datei nicht mehr exklusiv von einem anderen Prozess gehalten wird.

    Zuerst mit Schreibzugriff (unter Windows schlägt das während eines Kopiervorgangs fehl),
    bei fehlender Berechtigung (schreibgeschützte Datei) genügt Lesezugriff.
    """
    # TODO-LEXWARE: Am Windows-PC prüfen, wie Scanner-Software/Explorer/OneDrive die Datei während des
    # Schreibens sperren (Share-Modus); ggf. stabil_sekunden in der CLI erhöhen (große Scans, Netzlaufwerk).
    try:
        with datei.open("rb+"):
            return True
    except PermissionError:
        try:
            with datei.open("rb"):
                logger.debug("%s: nur lesend zu öffnen (schreibgeschützt oder in Benutzung)", datei.name)
                return True
        except OSError:
            return False
    except OSError:
        return False


def ist_fertig_geschrieben(datei: Path, *, stabil_sekunden: float = STABIL_SEKUNDEN) -> bool:
    """True, wenn die Datei existiert, ihre Größe über ``stabil_sekunden`` gleich bleibt, größer als
    0 Byte ist und sie geöffnet werden kann. Blockiert höchstens ``stabil_sekunden``."""
    datei = Path(datei)
    try:
        groesse_vorher = datei.stat().st_size
    except OSError:
        return False
    if stabil_sekunden > 0:
        time.sleep(stabil_sekunden)
    try:
        groesse_nachher = datei.stat().st_size
    except OSError:
        return False
    if groesse_vorher != groesse_nachher or groesse_nachher == 0:
        logger.debug("%s: wird noch geschrieben (%d -> %d Byte)", datei.name, groesse_vorher, groesse_nachher)
        return False
    return _kann_geoeffnet_werden(datei)


def warte_bis_fertig(
    datei: Path,
    *,
    stabil_sekunden: float = STABIL_SEKUNDEN,
    max_wartezeit: float = MAX_WARTEZEIT_SEKUNDEN,
    stop_ereignis: threading.Event | None = None,
) -> bool:
    """Wartet, bis :func:`ist_fertig_geschrieben` zutrifft; False bei Zeitüberschreitung, Abbruch
    oder wenn die Datei verschwindet."""
    datei = Path(datei)
    start = time.monotonic()
    while True:
        if stop_ereignis is not None and stop_ereignis.is_set():
            return False
        if not datei.exists():
            logger.info("%s: Datei ist verschwunden, nichts zu verarbeiten", datei)
            return False
        if ist_fertig_geschrieben(datei, stabil_sekunden=stabil_sekunden):
            return True
        if time.monotonic() - start > max_wartezeit:
            logger.warning("%s: nach %.0f s noch nicht fertig geschrieben, übersprungen", datei, max_wartezeit)
            return False
        time.sleep(max(stabil_sekunden, 0.05))


# ---------------------------------------------------------------------------
# Merkliste
# ---------------------------------------------------------------------------


class Verarbeitungsliste:
    """Merkt verarbeitete Dateien (Pfad, Größe, Änderungszeit) im Speicher und in einer Textdatei.

    Zeilenformat: ``<pfad>\\t<groesse>\\t<mtime>\\t<status>\\t<zeitpunkt>``. Beim Vergleich zählen
    Pfad, Größe und Änderungszeit; so wird eine geänderte Datei gleichen Namens erneut verarbeitet.
    """

    def __init__(self, datei: Path | None) -> None:
        self.datei = Path(datei) if datei is not None else None
        self._eintraege: set[tuple[str, int, int]] = set()
        self._sperre = threading.Lock()
        if self.datei is not None:
            self._lade()

    @staticmethod
    def _schluessel(pfad: Path) -> tuple[str, int, int]:
        try:
            st = pfad.stat()
            return (str(pfad.resolve()), st.st_size, int(st.st_mtime))
        except OSError:
            return (str(pfad.resolve()), -1, -1)

    def _lade(self) -> None:
        assert self.datei is not None
        if not self.datei.is_file():
            return
        anzahl = 0
        try:
            for zeile in self.datei.read_text(encoding="utf-8").splitlines():
                teile = zeile.rstrip("\r\n").split("\t")
                if len(teile) < 3 or not teile[0]:
                    continue
                try:
                    self._eintraege.add((teile[0], int(teile[1]), int(teile[2])))
                    anzahl += 1
                except ValueError:
                    logger.debug("Merkliste: Zeile übersprungen: %r", zeile)
        except OSError as exc:
            logger.warning("Merkliste %s nicht lesbar: %s", self.datei, exc)
        logger.info("Merkliste %s: %d Einträge geladen", self.datei, anzahl)

    def enthaelt(self, pfad: Path) -> bool:
        with self._sperre:
            return self._schluessel(Path(pfad)) in self._eintraege

    def merke(self, pfad: Path, status: str = "ok") -> None:
        schluessel = self._schluessel(Path(pfad))
        with self._sperre:
            self._eintraege.add(schluessel)
            if self.datei is None:
                return
            try:
                self.datei.parent.mkdir(parents=True, exist_ok=True)
                with self.datei.open("a", encoding="utf-8") as f:
                    zeitpunkt = time.strftime("%Y-%m-%d %H:%M:%S")
                    f.write(f"{schluessel[0]}\t{schluessel[1]}\t{schluessel[2]}\t{status}\t{zeitpunkt}\n")
            except OSError as exc:
                logger.error("Merkliste %s nicht beschreibbar: %s", self.datei, exc)

    def __len__(self) -> int:
        with self._sperre:
            return len(self._eintraege)


# ---------------------------------------------------------------------------
# watchdog-Handler
# ---------------------------------------------------------------------------


class EingangsHandler(FileSystemEventHandler):
    """Reicht neue und hineinverschobene Dateien mit unterstützter Endung in eine Warteschlange."""

    def __init__(self, warteschlange: queue.Queue[Path]) -> None:
        super().__init__()
        self.warteschlange = warteschlange

    def _melde(self, pfad_roh: str | bytes) -> None:
        pfad = Path(os.fsdecode(pfad_roh))
        if _ist_kandidat(pfad):
            logger.debug("Watcher: neue Datei gemeldet: %s", pfad)
            self.warteschlange.put(pfad)

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._melde(event.src_path)

    def on_moved(self, event: FileSystemEvent) -> None:
        if not event.is_directory and getattr(event, "dest_path", None):
            self._melde(event.dest_path)

    def on_modified(self, event: FileSystemEvent) -> None:
        # Manche Kopierprogramme erzeugen erst eine leere Datei und füllen sie dann; die Stabilitäts-
        # prüfung sorgt dafür, dass trotzdem nur fertige Dateien verarbeitet werden.
        if not event.is_directory:
            self._melde(event.src_path)


# ---------------------------------------------------------------------------
# Standard-Verarbeitung (Extraktion + Prüfung + Paket speichern, KEIN Buchen)
# ---------------------------------------------------------------------------


def verarbeite_standard(settings: Settings, *, paket_ordner: Path | None = None) -> Verarbeiter:
    """Liefert die Standard-Verarbeitung für den Watcher: extrahieren, Belegpaket erstellen und
    prüfen, Paket speichern, Zusammenfassung loggen. Es wird nichts gebucht."""
    from auftragsautomat.extraktion.erkennung import extrahiere
    from auftragsautomat.freigabe import zustand
    from auftragsautomat.matching.pipeline import erstelle_belegpaket
    from auftragsautomat.matching.pruefung import zusammenfassung_hinweise

    ziel_ordner = Path(paket_ordner) if paket_ordner is not None else zustand.paket_ordner(settings)

    def verarbeite(datei: Path) -> None:
        logger.info("Verarbeite %s", datei)
        roh = extrahiere(datei, settings)
        paket = erstelle_belegpaket(roh, settings)
        pfad = zustand.speichere_paket(paket, ziel_ordner)
        logger.info("%s -> Paket %s (%s)", datei.name, pfad, "BLOCKIERT" if paket.blockiert else "freigabefähig")
        logger.info("Zusammenfassung %s:\n%s", datei.name, zusammenfassung_hinweise(paket))

    return verarbeite


# ---------------------------------------------------------------------------
# Hauptschleife
# ---------------------------------------------------------------------------


def _verarbeite_sicher(
    datei: Path,
    verarbeite: Verarbeiter,
    merkliste: Verarbeitungsliste,
    *,
    stabil_sekunden: float,
    stop_ereignis: threading.Event | None,
) -> bool:
    """Eine Datei prüfen, verarbeiten und merken. True, wenn ``verarbeite`` aufgerufen wurde."""
    if merkliste.enthaelt(datei):
        logger.debug("%s: bereits verarbeitet, übersprungen", datei)
        return False
    if not warte_bis_fertig(datei, stabil_sekunden=stabil_sekunden, stop_ereignis=stop_ereignis):
        return False
    if merkliste.enthaelt(datei):  # Größe/Zeit könnten sich bis hierher geändert haben
        return False
    try:
        verarbeite(datei)
    except Exception as exc:  # noqa: BLE001 - ein Fehler je Datei darf den Watcher nicht beenden
        logger.exception("Verarbeitung von %s fehlgeschlagen", datei)
        merkliste.merke(datei, status=f"fehler: {type(exc).__name__}: {exc}".replace("\n", " ")[:300])
        return True
    merkliste.merke(datei, status="ok")
    return True


def _verarbeite_vorhandene(
    dateien: Iterable[Path],
    verarbeite: Verarbeiter,
    merkliste: Verarbeitungsliste,
    *,
    stabil_sekunden: float,
    stop_ereignis: threading.Event | None,
) -> int:
    anzahl = 0
    for datei in dateien:
        if stop_ereignis is not None and stop_ereignis.is_set():
            break
        if _verarbeite_sicher(
            datei, verarbeite, merkliste, stabil_sekunden=stabil_sekunden, stop_ereignis=stop_ereignis
        ):
            anzahl += 1
    return anzahl


def starte_watch(
    settings: Settings,
    verarbeite: Verarbeiter,
    *,
    einmalig: bool = False,
    stabil_sekunden: float = STABIL_SEKUNDEN,
    stop_ereignis: threading.Event | None = None,
    merkliste: Verarbeitungsliste | None = None,
) -> int:
    """Beobachtet ``settings.eingang_ordner`` und ruft ``verarbeite(datei)`` je neuer Datei auf.

    Zuerst werden vorhandene, noch nicht gemerkte Dateien verarbeitet. Mit ``einmalig=True`` endet
    die Funktion danach; sonst läuft sie, bis ``Strg+C`` gedrückt oder ``stop_ereignis`` gesetzt
    wird. Liefert die Anzahl der verarbeiteten Dateien (inklusive fehlgeschlagener).
    """
    eingang = Path(settings.eingang_ordner)
    eingang.mkdir(parents=True, exist_ok=True)
    if merkliste is None:
        merkliste = Verarbeitungsliste(Path(settings.log_ordner) / VERARBEITET_DATEINAME)
    logger.info(
        "Watcher startet auf %s (%s; Endungen: %s)",
        eingang,
        "einmalig" if einmalig else "dauerhaft",
        ", ".join(sorted(UNTERSTUETZTE_ENDUNGEN)),
    )

    anzahl = _verarbeite_vorhandene(
        finde_eingangsdateien(eingang),
        verarbeite,
        merkliste,
        stabil_sekunden=stabil_sekunden,
        stop_ereignis=stop_ereignis,
    )
    if einmalig:
        logger.info("Watcher (einmalig) beendet: %d Datei(en) verarbeitet", anzahl)
        return anzahl

    warteschlange: queue.Queue[Path] = queue.Queue()
    beobachter = Observer()
    beobachter.schedule(EingangsHandler(warteschlange), str(eingang), recursive=True)
    beobachter.start()
    logger.info("Watcher beobachtet %s – beenden mit Strg+C", eingang)
    try:
        while stop_ereignis is None or not stop_ereignis.is_set():
            try:
                datei = warteschlange.get(timeout=0.5)
            except queue.Empty:
                continue
            if _verarbeite_sicher(
                datei, verarbeite, merkliste, stabil_sekunden=stabil_sekunden, stop_ereignis=stop_ereignis
            ):
                anzahl += 1
    except KeyboardInterrupt:
        logger.info("Watcher durch Strg+C beendet")
    finally:
        beobachter.stop()
        beobachter.join(timeout=5)
    logger.info("Watcher beendet: %d Datei(en) verarbeitet", anzahl)
    return anzahl
