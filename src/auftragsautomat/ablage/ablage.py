"""Ablage fertiger, benannter Dateien in OneDrive (Ordner "Aufträge").

Design: ``docs/AUSBAUSTUFE_2.md`` Abschnitte 6 und 9, ``docs/ARCHITEKTUR.md`` Abschnitt ``ablage/``,
Geschäftsregeln 19 und 20. Nach Freigabe und erfolgreichem **scharfem** Lauf kopiert das Tool alle
fertigen Dateien eines Auftrags in den OneDrive-Ordner ``ablage.auftraege_ordner`` (der Ordner
"Aufträge"). Der Zielpfad darunter kommt aus ``ablage.struktur``:

* Standard ``"{provision}"``: Weinstrasse Agentur -> "Aufträge mit Provision", alle anderen
  Firmen -> "Aufträge ohne Provision" (``AblageKonfig.provisionsordner``).
* Fester Text ohne Platzhalter ist ebenfalls möglich (z. B. ``struktur: "Sammelordner"``).
* Weitere Platzhalter: ``{firma}`` (``Firma.ablage_ordner`` oder ``Firma.kurz``), ``{jahr}``,
  ``{monat}``, ``{lieferant}``, ``{kunde}`` (Kurznamen laut ``benennung``); alle Werte werden mit
  ``benennung.bereinige`` dateinamentauglich gemacht.

Ablauf von :func:`lege_ab`:

1. Quellen sammeln (:func:`quellen`): ``BuchungsErgebnis.pdfs`` (Dokumentkürzel -> lokale PDF) plus
   das Eingangsdokument als **Kd** (``paket.roh.quelldatei`` bzw. ``ergebnis.archiv``, wenn die
   Quelldatei schon archiviert wurde), sofern ``eingang.als_kundenbestellung_ablegen`` und
   ``ablage.kundenbestellung_ablegen`` gesetzt sind. Alle Zielnamen kommen aus
   :func:`auftragsautomat.benennung.name_fuer_paket` (Titulierung); das Kd behält seine
   Originalendung (``.pdf``, ``.eml``, ``.msg``, ``.png`` ...).
2. Nur-Protokoll (``AblageErgebnis.dry_run = True``, nichts wird kopiert), wenn ``dry_run`` gesetzt
   ist, ``ablage.aktiv`` aus ist oder (``ablage.nur_scharf`` und die Buchung ein Dry Run war).
3. Sonst: Paket muss freigegeben und die Buchung erfolgreich sein; Zielordner idempotent anlegen,
   freien Namen suchen (" (2)", " (3)" ...; Schalter ``vorhandene_ueberschreiben``), hochladen.
   Fehler werden **je Datei** gefangen (``AblageErgebnis.fehler``), die übrigen Dateien werden
   weiter abgelegt.

Das Tool verschiebt nichts, löscht nichts und überschreibt (ohne Schalter) nichts.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from string import Formatter

from auftragsautomat.benennung import (
    AUFTRAGSKOPIE_KUERZEL,
    bereinige,
    entferne_verbotene_woerter,
    kurzname_kunde,
    kurzname_lieferant,
    name_fuer_paket,
    pruefe_auftragskopie_name,
)
from auftragsautomat.config import DateiRefKonfig, Settings
from auftragsautomat.m365.basis import DateiBackend
from auftragsautomat.models import DOKUMENTKUERZEL, AblageErgebnis, Belegpaket, BuchungsErgebnis

__all__ = [
    "KUERZEL_KUNDENBESTELLUNG",
    "PLATZHALTER",
    "UNBEKANNT",
    "AblageFehler",
    "AblageQuelle",
    "ablage_ref",
    "basis_kuerzel",
    "freier_name",
    "lege_ab",
    "platzhalter_werte",
    "quellen",
    "zielordner",
]

logger = logging.getLogger(__name__)

#: Dokumentkürzel des Eingangsdokuments (Kundenbestellung).
KUERZEL_KUNDENBESTELLUNG = "Kd"
#: Erlaubte Platzhalter in ``ablage.struktur``.
PLATZHALTER: tuple[str, ...] = ("provision", "firma", "jahr", "monat", "lieferant", "kunde")
#: Platzhalterwert, wenn Firma/Kunde/Lieferant nicht bekannt sind.
UNBEKANNT = "Unbekannt"
#: Höchste Nummer, die :func:`freier_name` anhängt, bevor er aufgibt.
MAX_KOLLISIONEN = 999

_ENDUNG_RE = re.compile(r"^\.[A-Za-z0-9]{1,5}$")


class AblageFehler(ValueError):
    """Konfigurationsfehler der Ablage (unbekannter Platzhalter, kein freier Dateiname)."""


@dataclass(frozen=True)
class AblageQuelle:
    """Eine Datei, die abgelegt werden soll: Dokumentkürzel, lokaler Pfad, Zielname laut Titulierung."""

    kuerzel: str
    pfad: Path
    zielname: str


def _heute() -> date:
    """Heutiges Datum (eigene Funktion, damit Tests es ersetzen können)."""
    return date.today()


# ---------------------------------------------------------------------------
# Zielordner
# ---------------------------------------------------------------------------


def _segment(text: str | None, settings: Settings) -> str:
    """Dateinamentauglicher Pfadbestandteil; leer -> ``Unbekannt``."""
    bereinigt = bereinige(text or "", settings).strip("/").strip()
    return bereinigt or UNBEKANNT


def platzhalter_werte(paket: Belegpaket, settings: Settings, *, datum: date | None = None) -> dict[str, str]:
    """Werte für die Platzhalter in ``ablage.struktur`` (alle bereits bereinigt).

    ``provision``: ``AblageKonfig.provisionsordner(firma_id)``; ``firma``: ``Firma.ablage_ordner``, sonst
    ``Firma.kurz`` (ohne/unbekannte Firma ``Unbekannt``); ``jahr``/``monat``: ``datum`` (``None`` = heute);
    ``lieferant``/``kunde``: Kurznamen laut ``benennung`` (Matchcode, Name, Dokumenttext).
    """
    stichtag = datum or _heute()
    firma = settings.firmen.get(paket.firma_id) if paket.firma_id else None
    firma_text = (firma.ablage_ordner or firma.kurz) if firma is not None else None
    return {
        "provision": _segment(settings.ablage.provisionsordner(paket.firma_id or ""), settings),
        "firma": _segment(firma_text, settings),
        "jahr": f"{stichtag:%Y}",
        "monat": f"{stichtag:%m}",
        "lieferant": _segment(kurzname_lieferant(paket), settings),
        "kunde": _segment(kurzname_kunde(paket), settings),
    }


def _pruefe_struktur(struktur: str) -> None:
    """Wirft :class:`AblageFehler`, wenn ``struktur`` unbekannte Platzhalter oder kaputte Klammern enthält."""
    try:
        felder = [name for _, name, _, _ in Formatter().parse(struktur) if name is not None]
    except ValueError as exc:
        raise AblageFehler(f"ablage.struktur '{struktur}' ist ungültig: {exc}") from exc
    unbekannt = sorted({f for f in felder if f not in PLATZHALTER})
    if unbekannt:
        raise AblageFehler(
            f"ablage.struktur '{struktur}' enthält unbekannte Platzhalter: {', '.join(unbekannt)}. "
            f"Erlaubt sind {', '.join('{' + p + '}' for p in PLATZHALTER)}."
        )


def zielordner(paket: Belegpaket, settings: Settings, *, datum: date | None = None) -> str:
    """Zielordner relativ zu ``ablage.auftraege_ordner`` (Trenner "/", ohne führenden/abschließenden "/").

    Füllt die Platzhalter von ``ablage.struktur`` (:func:`platzhalter_werte`), bereinigt jedes
    Pfadsegment mit ``benennung.bereinige`` und lässt leere Segmente weg. ``""`` bedeutet: direkt in
    den Ordner "Aufträge". Unbekannte Platzhalter ergeben :class:`AblageFehler`.
    """
    struktur = settings.ablage.struktur.replace("\\", "/")
    _pruefe_struktur(struktur)
    gefuellt = struktur.format_map(platzhalter_werte(paket, settings, datum=datum))
    segmente = [bereinige(s, settings) for s in gefuellt.split("/")]
    return "/".join(s for s in segmente if s)


# ---------------------------------------------------------------------------
# Dateinamen und Quellen
# ---------------------------------------------------------------------------


def _rel(ordner: str, name: str) -> str:
    return f"{ordner}/{name}" if ordner else name


def _stamm_endung(name: str) -> tuple[str, str]:
    """Zerlegt ``Name.pdf`` in ``("Name", ".pdf")``; ein Datum am Ende (``… – 24.10.2026``) gilt nicht
    als Endung."""
    endung = Path(name).suffix
    if endung and _ENDUNG_RE.match(endung) and not endung[1:].isdigit():
        return name[: -len(endung)], endung
    return name, ""


def freier_name(backend: DateiBackend, ref: DateiRefKonfig, ordner: str, name: str) -> str:
    """Erster freier Dateiname in ``ordner``: ``name``, sonst ``Name (2).pdf``, ``Name (3).pdf`` ...

    Prüft über ``DateiBackend.existiert``; nach :data:`MAX_KOLLISIONEN` Versuchen :class:`AblageFehler`.
    """
    # TODO-PRUEFEN: Am echten Tenant prüfen, dass Graph die Pfadadressierung (/drives/{d}/items/{i}:/Ordner/Name)
    # mit Gedankenstrich " – " und Umlauten im Ordner "Aufträge …" auflöst und dass die Kollisionsprüfung
    # dort (OneDrive: Groß-/Kleinschreibung egal) dieselben Treffer liefert wie der lokale Fake (Linux: exakt).
    if not backend.existiert(ref, _rel(ordner, name)):
        return name
    stamm, endung = _stamm_endung(name)
    for nummer in range(2, MAX_KOLLISIONEN + 1):
        kandidat = f"{stamm} ({nummer}){endung}"
        if not backend.existiert(ref, _rel(ordner, kandidat)):
            return kandidat
    raise AblageFehler(f"Kein freier Dateiname für '{name}' in '{ordner or '/'}' (bis ' ({MAX_KOLLISIONEN})' belegt).")


#: Schlüssel weiterer Weingüter in ``BuchungsErgebnis.pdfs``: "Cde (2)", "Speditionsauftrag (3)" …
_WEITERES_WEINGUT_RE = re.compile(r"^(?P<kuerzel>.+?) \((?P<nummer>\d+)\)$")


def basis_kuerzel(schluessel: str) -> tuple[str, int | None]:
    """Dokumentkürzel und Weingut-Nummer eines Schlüssels aus ``BuchungsErgebnis.pdfs``:
    ``"Cde"`` -> ``("Cde", None)``, ``"Cde (2)"`` -> ``("Cde", 2)`` (Bestellung an das zweite Weingut)."""
    treffer = _WEITERES_WEINGUT_RE.match(schluessel)
    if treffer is not None and treffer.group("kuerzel") in DOKUMENTKUERZEL:
        return treffer.group("kuerzel"), int(treffer.group("nummer"))
    return schluessel, None


def _zielname(kuerzel: str, pfad: Path, paket: Belegpaket, settings: Settings, datum: date) -> str:
    """Zielname (siehe :func:`_zielname_roh`); für die Auftragskopie verbindlich ohne verbotene Wörter
    (``pruefe_auftragskopie_name``, Geschäftsregel 18) – auch beim Rückfall auf den Quellnamen."""
    name = _zielname_roh(kuerzel, pfad, paket, settings, datum)
    if basis_kuerzel(kuerzel)[0] in AUFTRAGSKOPIE_KUERZEL and pruefe_auftragskopie_name(name, settings):
        bereinigt = entferne_verbotene_woerter(Path(name).stem, settings) or kuerzel
        logger.warning("Ablage: verbotene Wörter aus dem Namen der Auftragskopie entfernt: %s -> %s", name, bereinigt + Path(name).suffix)
        name = bereinigt + Path(name).suffix
    return name


def _zielname_roh(kuerzel: str, pfad: Path, paket: Belegpaket, settings: Settings, datum: date) -> str:
    """Zielname laut Titulierung (``name_fuer_paket``); bei unbekanntem Kürzel oder Paket ohne Firma
    der bereinigte Dateiname der Quelle (mit Hinweis im Log).

    PDFs weiterer Weingüter (Schlüssel ``"Cde (2)"`` usw.) hat ``lexware.buchen`` bereits mit dem Teilpaket
    des jeweiligen Weinguts benannt (Lieferant im Namen); ihr Name bleibt erhalten – ``name_fuer_paket`` mit
    dem Gesamtpaket würde den Hauptlieferanten einsetzen.
    """
    basis, weingut = basis_kuerzel(kuerzel)
    if weingut is not None:
        logger.info("Ablage: %s ist die Datei des %d. Weinguts – Name aus lexware/ bleibt: %s", kuerzel, weingut, pfad.name)
        return bereinige(pfad.name, settings) or pfad.name
    if kuerzel in DOKUMENTKUERZEL and paket.firma_id:
        try:
            return name_fuer_paket(kuerzel, paket, settings, datum=datum, endung=pfad.suffix)
        except (ValueError, KeyError) as exc:
            logger.warning("Kein Name nach Titulierung für %s (%s): %s – Quellname wird verwendet.", kuerzel, pfad.name, exc)
    else:
        logger.warning(
            "Dokumentkürzel '%s' ist nicht in DOKUMENTKUERZEL oder das Paket hat keine Firma – "
            "Quellname '%s' wird verwendet.",
            kuerzel,
            pfad.name,
        )
    return bereinige(pfad.name, settings) or pfad.name


def _kundenbestellung(paket: Belegpaket, ergebnis: BuchungsErgebnis) -> Path:
    """Pfad des Eingangsdokuments: die archivierte Kopie (nach scharfem Lauf), sonst die Quelldatei."""
    if ergebnis.archiv is not None and Path(ergebnis.archiv).is_file():
        return Path(ergebnis.archiv)
    return Path(paket.roh.quelldatei)


def quellen(
    paket: Belegpaket,
    ergebnis: BuchungsErgebnis,
    settings: Settings,
    *,
    datum: date | None = None,
) -> list[AblageQuelle]:
    """Alle Dateien, die :func:`lege_ab` ablegen würde (Reihenfolge: Kd, dann ``ergebnis.pdfs``).

    Das Eingangsdokument kommt als "Kd" dazu, wenn ``eingang.als_kundenbestellung_ablegen`` und
    ``ablage.kundenbestellung_ablegen`` gesetzt sind und ``ergebnis.pdfs`` noch kein "Kd" enthält.
    ``datum`` (``None`` = Zeitpunkt der Buchung) ist das Datum in den Zielnamen.
    """
    stichtag = datum or ergebnis.zeitpunkt.date()
    liste: list[AblageQuelle] = []
    kd_gewuenscht = settings.eingang.als_kundenbestellung_ablegen and settings.ablage.kundenbestellung_ablegen
    if kd_gewuenscht and KUERZEL_KUNDENBESTELLUNG not in ergebnis.pdfs:
        pfad = _kundenbestellung(paket, ergebnis)
        liste.append(
            AblageQuelle(KUERZEL_KUNDENBESTELLUNG, pfad, _zielname(KUERZEL_KUNDENBESTELLUNG, pfad, paket, settings, stichtag))
        )
    for schluessel, pfad in ergebnis.pdfs.items():
        pfad = Path(pfad)
        kuerzel, _ = basis_kuerzel(schluessel)
        liste.append(AblageQuelle(kuerzel, pfad, _zielname(schluessel, pfad, paket, settings, stichtag)))
    return liste


# ---------------------------------------------------------------------------
# Ablage
# ---------------------------------------------------------------------------


def ablage_ref(settings: Settings) -> DateiRefKonfig:
    """Der OneDrive-Ordner "Aufträge" (``ablage.auftraege_ordner``). Ist er nicht konfiguriert
    (weder ``web_url`` noch IDs noch ``lokaler_pfad``), fällt die Ablage mit Warnung auf den lokalen
    Ordner ``ablage.lokaler_ordner`` zurück (Fake-/lokales Backend); das Graph-Backend meldet dann je
    Datei einen Fehler, weil der Ordner nicht auflösbar ist."""
    ref = settings.ablage.auftraege_ordner
    if ref.web_url or (ref.drive_id and ref.item_id) or ref.lokaler_pfad is not None:
        return ref
    logger.warning(
        "ablage.auftraege_ordner ist nicht konfiguriert (web_url/IDs/lokaler_pfad fehlen); "
        "Ablage landet lokal unter %s.",
        settings.ablage.lokaler_ordner,
    )
    return DateiRefKonfig(lokaler_pfad=Path(settings.ablage.lokaler_ordner))


def _protokoll_grund(dry_run: bool, settings: Settings, ergebnis: BuchungsErgebnis) -> str | None:
    """Grund, warum nur protokolliert wird; ``None`` = scharf ablegen."""
    k = settings.ablage
    if dry_run:
        return "Dry Run"
    if not k.aktiv:
        return "ablage.aktiv = false"
    if k.nur_scharf and ergebnis.dry_run:
        return "Buchung war ein Dry Run (ablage.nur_scharf)"
    return None


def _sperrgrund(paket: Belegpaket, ergebnis: BuchungsErgebnis) -> str | None:
    """Grund, warum scharf nicht abgelegt werden darf (Geschäftsregel 19)."""
    if not paket.freigegeben:
        return "Belegpaket ist nicht freigegeben – keine Ablage."
    if not ergebnis.erfolgreich:
        return "Buchung war nicht erfolgreich – keine Ablage."
    return None


def lege_ab(
    paket: Belegpaket,
    ergebnis: BuchungsErgebnis,
    settings: Settings,
    *,
    datei_backend: DateiBackend,
    dry_run: bool = True,
    datum: date | None = None,
) -> list[AblageErgebnis]:
    """Kopiert die fertigen Dateien des Auftrags nach ``ablage.auftraege_ordner``/:func:`zielordner`.

    Quellen laut :func:`quellen`; ``datum`` (``None`` = Zeitpunkt der Buchung) bestimmt Jahr/Monat im
    Zielordner und das Datum in den Zielnamen. Nur-Protokoll (``AblageErgebnis.dry_run = True``,
    ``abgelegt = False``) bei ``dry_run``, ``ablage.aktiv = False`` oder ``ablage.nur_scharf`` mit einer
    Dry-Run-Buchung. Scharf nur für freigegebene Pakete mit erfolgreicher Buchung; sonst
    ``fehler`` je Datei. Fehlende Quelldateien und Backend-Fehler werden je Datei als ``fehler``
    gemeldet, die übrigen Dateien werden trotzdem abgelegt. Namenskollisionen -> " (2)", " (3)" ...
    (``ablage.vorhandene_ueberschreiben = False``). Die Ergebnisse werden zusätzlich in
    ``ergebnis.ablage`` eingetragen (ersetzt vorherige) und zurückgegeben.
    """
    stichtag = datum or ergebnis.zeitpunkt.date()
    ordner = zielordner(paket, settings, datum=stichtag)
    liste = quellen(paket, ergebnis, settings, datum=stichtag)
    ergebnisse: list[AblageErgebnis] = []

    grund = _protokoll_grund(dry_run, settings, ergebnis)
    if grund is not None:
        for q in liste:
            fehler = None if q.pfad.is_file() else f"Quelldatei fehlt: {q.pfad}"
            ergebnisse.append(
                AblageErgebnis(datei=q.pfad, kuerzel=q.kuerzel, zielordner=ordner, zielname=q.zielname, abgelegt=False, dry_run=True, fehler=fehler)
            )
            logger.info("Ablage nur protokolliert (%s): %s -> %s", grund, q.pfad.name, _rel(ordner, q.zielname))
        ergebnis.ablage = ergebnisse
        return ergebnisse

    sperre = _sperrgrund(paket, ergebnis)
    if sperre is not None:
        logger.warning("Ablage abgelehnt: %s (%s)", sperre, paket.roh.quelldatei)
        for q in liste:
            ergebnisse.append(
                AblageErgebnis(datei=q.pfad, kuerzel=q.kuerzel, zielordner=ordner, zielname=q.zielname, abgelegt=False, dry_run=False, fehler=sperre)
            )
        ergebnis.ablage = ergebnisse
        return ergebnisse

    ref = ablage_ref(settings)
    ordner_fehler: str | None = None
    if ordner:
        try:
            datei_backend.ordner_anlegen(ref, ordner)
        except Exception as exc:  # noqa: BLE001 - Fehler je Datei melden, nicht abbrechen
            ordner_fehler = f"Zielordner '{ordner}' konnte nicht angelegt werden: {exc}"
            logger.exception("Ablage: %s", ordner_fehler)

    for q in liste:
        fehler: str | None = None
        speicherort: str | None = None
        zielname = q.zielname
        if not q.pfad.is_file():
            fehler = f"Quelldatei fehlt: {q.pfad}"
        elif ordner_fehler is not None:
            fehler = ordner_fehler
        else:
            try:
                if not settings.ablage.vorhandene_ueberschreiben:
                    zielname = freier_name(datei_backend, ref, ordner, q.zielname)
                    if zielname != q.zielname:
                        logger.info("Ablage: '%s' existiert schon in '%s' – abgelegt als '%s'.", q.zielname, ordner, zielname)
                speicherort = datei_backend.hochladen(q.pfad, ref, _rel(ordner, zielname))
                logger.info("Abgelegt: %s -> %s (%s)", q.pfad.name, _rel(ordner, zielname), speicherort)
            except Exception as exc:  # noqa: BLE001 - Fehler je Datei melden, Rest weiter ablegen
                fehler = f"{type(exc).__name__}: {exc}"
                logger.exception("Ablage von %s nach %s fehlgeschlagen", q.pfad, _rel(ordner, zielname))
        if fehler is not None and speicherort is None:
            logger.warning("Ablage: %s (%s) nicht abgelegt: %s", q.pfad.name, q.kuerzel, fehler)
        ergebnisse.append(
            AblageErgebnis(
                datei=q.pfad,
                kuerzel=q.kuerzel,
                zielordner=ordner,
                zielname=zielname,
                abgelegt=speicherort is not None,
                dry_run=False,
                speicherort=speicherort,
                fehler=fehler,
            )
        )
    ergebnis.ablage = ergebnisse
    return ergebnisse
