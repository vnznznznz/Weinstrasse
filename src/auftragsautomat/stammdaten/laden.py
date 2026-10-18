"""Stammdaten einer Firma laden (CSV oder ODBC, nur lesend) mit Cache pro Prozess.

* :func:`lade_stammdaten` wählt anhand von ``Firma.stammdaten.typ`` die Quelle und liefert
  :class:`~auftragsautomat.stammdaten.basis.Stammdaten` (mit Indizes).
* Der Cache ist prozessweit und erkennt neu exportierte CSV-Dateien an ihrer Änderungszeit
  und geänderte Konfigurationen an einer Signatur; :func:`leere_cache` leert ihn explizit.
* :func:`lade_alle` lädt alle Firmen aus den Settings; Fehler einer Firma werden geloggt und
  die Firma übersprungen, die übrigen Firmen werden trotzdem geladen.
* :func:`stammdaten_bericht` fasst Kennzahlen und Datenqualität für CLI/UI zusammen
  (``auftragsautomat stammdaten-pruefen``).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from auftragsautomat.config import Firma, Settings
from auftragsautomat.stammdaten.basis import Stammdaten, StammdatenQuelle
from auftragsautomat.stammdaten.csv_quelle import CsvStammdatenQuelle, StammdatenFehler
from auftragsautomat.stammdaten.odbc_quelle import OdbcStammdatenQuelle

__all__ = [
    "StammdatenFehler",
    "erzeuge_quelle",
    "lade_alle",
    "lade_stammdaten",
    "leere_cache",
    "stammdaten_bericht",
]

logger = logging.getLogger(__name__)

# firma_id -> (Signatur, Stammdaten)
_CACHE: dict[str, tuple[str, Stammdaten]] = {}

_MAX_BEISPIELE = 10


def erzeuge_quelle(firma: Firma) -> StammdatenQuelle:
    """Wählt die Stammdatenquelle einer Firma anhand von ``firma.stammdaten.typ`` (csv/odbc)."""
    konfig = firma.stammdaten
    if konfig.typ == "csv":
        if konfig.csv is None:
            raise StammdatenFehler(f"Firma {firma.id}: stammdaten.typ ist 'csv', aber stammdaten.csv fehlt")
        return CsvStammdatenQuelle(firma.id, konfig.csv)
    if konfig.typ == "odbc":
        if konfig.odbc is None:
            raise StammdatenFehler(f"Firma {firma.id}: stammdaten.typ ist 'odbc', aber stammdaten.odbc fehlt")
        return OdbcStammdatenQuelle(firma.id, konfig.odbc)
    raise StammdatenFehler(f"Firma {firma.id}: unbekannter Stammdatentyp '{konfig.typ}' (erlaubt: csv, odbc)")


def _signatur(firma: Firma) -> str:
    """Signatur der Konfiguration plus Änderungszeiten der CSV-Dateien (erkennt neue Exporte)."""
    teile: dict[str, object] = {"konfig": firma.stammdaten.model_dump(mode="json")}
    csv = firma.stammdaten.csv
    if firma.stammdaten.typ == "csv" and csv is not None:
        zeiten: dict[str, float | None] = {}
        for name in ("artikel", "kunden", "lieferanten", "belege"):
            pfad = getattr(csv, name)
            if pfad is None:
                zeiten[name] = None
                continue
            pfad = Path(pfad)
            try:
                stat = pfad.stat()
                zeiten[name] = stat.st_mtime_ns + stat.st_size  # Zeit und Größe
            except OSError:
                zeiten[name] = None
        teile["dateien"] = zeiten
    return json.dumps(teile, sort_keys=True, default=str)


def lade_stammdaten(firma: Firma, *, cache: bool = True) -> Stammdaten:
    """Lädt die Stammdaten einer Firma (CSV oder ODBC). Mit ``cache=True`` aus dem Prozess-Cache,
    sofern Konfiguration und CSV-Dateien unverändert sind."""
    signatur = _signatur(firma) if cache else ""
    if cache:
        eintrag = _CACHE.get(firma.id)
        if eintrag is not None:
            alte_signatur, daten = eintrag
            if alte_signatur == signatur:
                logger.debug("Stammdaten für Firma %s aus dem Cache", firma.id)
                return daten
            logger.info("Stammdaten für Firma %s haben sich geändert; lade neu", firma.id)

    quelle = erzeuge_quelle(firma)
    logger.info("Lade Stammdaten für Firma %s über %s", firma.id, type(quelle).__name__)
    daten = Stammdaten.aus_quelle(quelle)
    logger.info("%r", daten)
    if cache:
        _CACHE[firma.id] = (signatur, daten)
    return daten


def leere_cache(firma_id: str | None = None) -> None:
    """Leert den Stammdaten-Cache (eine Firma oder alle)."""
    if firma_id is None:
        _CACHE.clear()
        logger.debug("Stammdaten-Cache geleert")
    else:
        if _CACHE.pop(firma_id, None) is not None:
            logger.debug("Stammdaten-Cache für Firma %s geleert", firma_id)


def lade_alle(settings: Settings, *, cache: bool = True) -> dict[str, Stammdaten]:
    """Lädt die Stammdaten aller Firmen. Fehler je Firma werden geloggt und die Firma übersprungen."""
    ergebnis: dict[str, Stammdaten] = {}
    for firma_id, firma in settings.firmen.items():
        try:
            ergebnis[firma_id] = lade_stammdaten(firma, cache=cache)
        except StammdatenFehler as exc:
            logger.error("Firma %s übersprungen, Stammdaten nicht ladbar: %s", firma_id, exc)
        except Exception:
            logger.exception("Firma %s übersprungen, unerwarteter Fehler beim Laden der Stammdaten", firma_id)
    logger.info("Stammdaten geladen für %d von %d Firmen", len(ergebnis), len(settings.firmen))
    return ergebnis


def _beispiele(nummern: list[str]) -> str:
    if not nummern:
        return ""
    gezeigt = ", ".join(nummern[:_MAX_BEISPIELE])
    rest = len(nummern) - _MAX_BEISPIELE
    return f" ({gezeigt}{f', ... +{rest}' if rest > 0 else ''})"


def stammdaten_bericht(stammdaten: Stammdaten) -> str:
    """Mehrzeiliger Bericht mit Anzahlen und Datenqualität (für CLI und Freigabe-UI)."""
    artikel = stammdaten.artikel
    ohne_ek = [a.artikelnummer for a in artikel if a.ek_preis is None]
    ohne_vk = [a.artikelnummer for a in artikel if a.vk_preis_1 is None]
    ohne_lieferant = [a.artikelnummer for a in artikel if not a.lieferantennummer]
    unbekannter_lieferant = [
        a.artikelnummer
        for a in artikel
        if a.lieferantennummer and a.lieferantennummer not in stammdaten.lieferanten_nach_nummer
    ]
    sondervarianten = [a.artikelnummer for a in artikel if a.ist_sondervariante]
    inaktive = [a.artikelnummer for a in artikel if not a.aktiv]
    ohne_volumen = [a.artikelnummer for a in artikel if a.volumen_cl is None]
    kunden_ohne_pg = [k.kundennummer for k in stammdaten.kunden if k.preisgruppe is None]
    inaktive_kunden = [k.kundennummer for k in stammdaten.kunden if not k.aktiv]
    belege_mit_bestellnr = sum(1 for b in stammdaten.belege if b.kundenbestellnummer)

    zeilen = [
        f"Stammdaten Firma {stammdaten.firma_id}",
        f"  Artikel: {len(artikel)}",
        f"    ohne EK-Preis: {len(ohne_ek)}{_beispiele(ohne_ek)}",
        f"    ohne VK-Preis (Preisgruppe 1): {len(ohne_vk)}{_beispiele(ohne_vk)}",
        f"    ohne Lieferantennummer: {len(ohne_lieferant)}{_beispiele(ohne_lieferant)}",
        f"    mit unbekannter Lieferantennummer: {len(unbekannter_lieferant)}{_beispiele(unbekannter_lieferant)}",
        f"    ohne erkennbares Volumen: {len(ohne_volumen)}{_beispiele(ohne_volumen)}",
        f"    Sondervarianten (37,5 cl / 1,5 l / Holzkiste, nie automatisch): {len(sondervarianten)}{_beispiele(sondervarianten)}",
        f"    inaktiv: {len(inaktive)}{_beispiele(inaktive)}",
        f"  Kunden: {len(stammdaten.kunden)}",
        f"    ohne Preisgruppe: {len(kunden_ohne_pg)}{_beispiele(kunden_ohne_pg)}",
        f"    inaktiv: {len(inaktive_kunden)}{_beispiele(inaktive_kunden)}",
        f"  Lieferanten: {len(stammdaten.lieferanten)}",
        f"  Belege (Dublettenprüfung): {len(stammdaten.belege)}, davon mit Kundenbestellnummer: {belege_mit_bestellnr}",
    ]
    if not stammdaten.belege:
        zeilen.append("  Hinweis: keine Belege geladen; Dubletten können nur in Lexware selbst geprüft werden.")
    return "\n".join(zeilen)
