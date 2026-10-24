"""Buchungs-Orchestrierung: ein freigegebenes Belegpaket nach Lexware schreiben.

:func:`buche` ist die einzige Stelle, an der Belege angelegt werden. Ablauf (docs/AUSBAUSTUFE_2.md,
Abschnitt 5; Geschäftsregeln 13-16 und 18):

1. **Vorbedingungen** (ohne sie wird nichts geschrieben, auch kein Protokoll):
   ``paket.freigegeben``, nicht ``paket.blockiert``, Firma gesetzt und bekannt, Kunde zugeordnet,
   Lieferant zugeordnet (wenn Einkauf erforderlich), mindestens eine aktive Position,
   Mandantenschutz (Kunde/Lieferant/Artikel gehören zur Firma des Pakets).
2. **Modus und Schreiber**: ein übergebener ``schreiber`` wird für alles verwendet. Sonst gilt die
   doppelte Sicherung: scharf nur, wenn ``dry_run=False`` übergeben wurde UND Lexware erreichbar ist
   (Windows) UND ``settings.dry_run`` aus ist – andernfalls läuft der
   :class:`~auftragsautomat.lexware.dry_run.DryRunSchreiber` und das Ergebnis trägt ``dry_run=True``.
   Unter Windows mit ``settings.dry_run = False``: ``UiSchreiber`` (Einkauf, Angebot, AB, Lieferschein,
   PDF), ``OpenTransSchreiber`` für die Verkauf-AB, wenn ``firma.opentrans.lizenziert`` True ist.
3. **Dubletten** unmittelbar vor dem Schreiben erneut prüfen (``matching.pruefung.pruefe_dubletten``
   mit frisch geladenen Stammdaten, Treffer werden ins Paket eingetragen) -> Abbruch bei Treffer.
   **Revised Order** (``paket.ist_revision``): Treffer zur ersetzten Bestellung (``revision_von``, sonst
   die eigene Kundenbestellnummer) sind Info statt Fehler; die übrigen Dubletten-Regeln bleiben.
4. **Reihenfolge je Firma**:
   (1) Einkauf-Bestellung je Weingut (nur ``paket.einkauf_erforderlich``; bei Positionen mehrerer
   Weingüter über ``einkaufs_teilpakete``), Nachbemerkung = Kundenbestellnummer laut Firma; danach
   PDF-Ausgabe mit ``Firma.formulare.einkauf_en`` so oft wie ``speditionsauftrag_kopien``: 1. Datei
   Kürzel "Cde" (Revision: "Neu"), 2. Datei "Speditionsauftrag" (Namen aus ``benennung``).
   (2) Verkauf: ``Firma.verkauf_weg`` ``direkt_ab`` = AB direkt (bisheriges Verhalten); sonst Angebot
   (Positionsrabatte) -> bei konfiguriertem ``formulare.angebot_en`` (Weinstrasse Agentur) PDF "Cde" +
   "Speditionsauftrag" aus dem Angebot -> ``angebot_zu_ab`` (Weiterführen/Wandeln, Lieferdatum und
   AB-Text laut Firma leeren) -> PDF "Kopie" mit ``formulare.verkauf_ab`` (nie "Auftragsbestätigung"
   im Namen, Geschäftsregel 18).
   (3) Lieferschein nur mit ``paket.lieferschein_erstellen`` UND ``Firma.lieferschein_erlaubt``
   (sonst Warnung), AB erfolgreich und AB-Belegnummer vorhanden (per openTRANS gibt es die Nummer
   erst nach dem Import -> :func:`lieferschein_nachziehen`); danach PDF "Ls".
   Schlägt der Einkauf oder das Angebot scharf fehl, wird KEINE AB angelegt (keine halbe Buchung);
   im Dry Run läuft alles durch und sammelt die Ergebnisse.
   Dry Run: keine PDF wird erzeugt; ``pdf_ausgeben`` liefert den geplanten Pfad (``BuchungsErgebnis.pdfs``).
5. **Protokoll** (``protokoll.schreibe_protokoll``, Pfad in ``ergebnis.protokoll``) und – nur scharf
   und erfolgreich – Archivierung der Quelldatei (``protokoll.archiviere``, ``ergebnis.archiv``).
   Im Dry Run wird nichts verschoben.
6. **Nachbereitung** (``lexware.nachbereitung.nachbereiten``, nur nach erfolgreicher Buchung): Fixtermin in
   die Fixterminüberwachung (Dry Run: Vorschau-CSV), E-Mail-Entwürfe (nie senden), Dossier, Ablage (nur
   scharf). Fehler dort werden als Warnung vermerkt und ändern ``ergebnis.erfolgreich`` nicht.

Schreiber dürfen keine Exceptions werfen; falls doch, wird daraus ein ``Belegergebnis`` mit
``erfolgreich=False`` und ``methode="keine"``. Kennt ein (fremder) Schreiber Angebot oder PDF-Ausgabe
nicht (``basis.unterstuetzt_angebot`` / ``unterstuetzt_pdf``), wird die AB direkt angelegt bzw. die
PDF übersprungen – jeweils mit Warnung.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from auftragsautomat.config import Firma, Settings
from auftragsautomat.lexware.basis import (
    ANGEBOTSWEGE,
    LexwareSchreiber,
    unterstuetzt_angebot,
    unterstuetzt_pdf,
    weg_beschreibung,
)
from auftragsautomat.lexware.dry_run import DryRunSchreiber
from auftragsautomat.lexware.opentrans import OpenTransSchreiber
from auftragsautomat.models import (
    Belegart,
    Belegergebnis,
    Belegpaket,
    BuchungsErgebnis,
    LieferantZuordnung,
    MatchStatus,
    PositionsZuordnung,
    Pruefcode,
    Pruefhinweis,
    Schweregrad,
)
from auftragsautomat.protokoll import archiviere, schreibe_protokoll
from auftragsautomat.stammdaten.basis import Stammdaten

logger = logging.getLogger(__name__)

__all__ = [
    "KUERZEL_AUFTRAGSKOPIE",
    "KUERZEL_BESTELLUNG",
    "KUERZEL_LIEFERSCHEIN",
    "KUERZEL_SPEDITIONSAUFTRAG",
    "Schreiberwahl",
    "buche",
    "einkaufs_teilpakete",
    "ist_revision",
    "lieferschein_nachziehen",
    "revision_nummern",
    "vorbedingungen",
    "waehle_schreiber",
]

_VERWEIGERT = "Buchung verweigert"
#: Schlüssel in ``BuchungsErgebnis.pdfs`` (Basis-Kürzel; bei einer Revision heißt die Datei "Neu"/"Neu Kopie",
#: der Schlüssel bleibt "Cde"/"Kopie", damit Kommunikation und Dossier sie finden).
KUERZEL_BESTELLUNG = "Cde"
KUERZEL_SPEDITIONSAUFTRAG = "Speditionsauftrag"
KUERZEL_AUFTRAGSKOPIE = "Kopie"
KUERZEL_LIEFERSCHEIN = "Ls"


@dataclass(frozen=True)
class Schreiberwahl:
    """Welcher Schreiber welchen Beleg anlegt und ob der Lauf effektiv ein Dry Run ist."""

    einkauf: LexwareSchreiber
    verkauf: LexwareSchreiber
    lieferschein: LexwareSchreiber
    dry_run: bool
    beschreibung: str


def _ist_windows() -> bool:
    return sys.platform == "win32"


def _erzeuge_ui_schreiber(settings: Settings, firma: Firma) -> LexwareSchreiber:
    """UiSchreiber erst hier importieren/erzeugen (pywinauto nur unter Windows)."""
    from auftragsautomat.lexware.ui_automation import UiSchreiber

    return UiSchreiber(settings, firma)


# ---------------------------------------------------------------------------
# Vorbedingungen und Schreiberwahl
# ---------------------------------------------------------------------------


def vorbedingungen(paket: Belegpaket, settings: Settings) -> list[str]:
    """Gründe, warum das Paket nicht gebucht werden darf (leer = buchbar)."""
    gruende: list[str] = []
    if not paket.freigegeben:
        gruende.append("Paket ist nicht freigegeben (Freigabe in der Oberfläche erforderlich)")
    if paket.blockiert:
        fehler = [h for h in paket.alle_hinweise if h.schweregrad == Schweregrad.FEHLER]
        texte = "; ".join(f"[{h.code.value}] {h.text}" for h in fehler[:5])
        if len(fehler) > 5:
            texte += f"; … (+{len(fehler) - 5})"
        gruende.append(f"Paket ist blockiert durch {len(fehler)} Prüfhinweis(e) mit Schweregrad FEHLER: {texte}")
    firma_id = paket.firma_id
    if not firma_id:
        gruende.append("Keine Firma zugeordnet (Geschäftsregel 1: ohne eindeutige Firma keine Buchung)")
    elif firma_id not in settings.firmen:
        gruende.append(f"Unbekannte Firma '{firma_id}' (bekannt: {', '.join(settings.firmen) or '-'})")

    kunde = paket.kunde.kunde
    if kunde is None:
        gruende.append("Kein Kunde zugeordnet")
    elif firma_id and kunde.firma_id != firma_id:
        gruende.append(f"Mandantenschutz: Kunde {kunde.kundennummer} gehört zu Firma '{kunde.firma_id}', nicht '{firma_id}'")
    lieferant = paket.lieferant.lieferant
    if paket.einkauf_erforderlich and lieferant is None:
        gruende.append("Kein Lieferant zugeordnet, aber Einkauf-Bestellung erforderlich")
    if lieferant is not None and firma_id and lieferant.firma_id != firma_id:
        gruende.append(
            f"Mandantenschutz: Lieferant {lieferant.lieferantennummer} gehört zu Firma '{lieferant.firma_id}', nicht '{firma_id}'"
        )
    aktive = paket.aktive_positionen()
    if not aktive:
        gruende.append("Keine aktive Position (alle übersprungen oder keine vorhanden)")
    for p in aktive:
        if p.artikel is not None and firma_id and p.artikel.firma_id != firma_id:
            gruende.append(
                f"Mandantenschutz: Position {p.roh.laufnr}, Artikel {p.artikel.artikelnummer} gehört zu "
                f"Firma '{p.artikel.firma_id}', nicht '{firma_id}'"
            )
    return gruende


def waehle_schreiber(
    settings: Settings,
    firma: Firma,
    dry_run: bool,
    schreiber: LexwareSchreiber | None,
    warnungen: list[str],
) -> Schreiberwahl:
    """Schreiber und effektiven Modus bestimmen (siehe Modul-Docstring). Hinweise landen in ``warnungen``."""
    if schreiber is not None:
        return Schreiberwahl(schreiber, schreiber, schreiber, dry_run, f"übergebener Schreiber {type(schreiber).__name__}")

    if settings.dry_run or not _ist_windows():
        grund = (
            "settings.dry_run ist aktiv (scharf erst nach Umstellen der Konfiguration)"
            if settings.dry_run
            else "Lexware-UI-Automation läuft nur unter Windows"
        )
        if not dry_run:
            text = f"Scharfer Lauf angefordert, aber {grund}: Lauf wird als Dry Run ausgeführt"
            logger.warning("%s", text)
            warnungen.append(text)
        simulation = DryRunSchreiber(settings, firma)
        return Schreiberwahl(simulation, simulation, simulation, True, f"DryRunSchreiber ({grund})")

    ui = _erzeuge_ui_schreiber(settings, firma)
    lizenziert = firma.opentrans.lizenziert
    if lizenziert is True:
        verkauf: LexwareSchreiber = OpenTransSchreiber(settings, firma)
        beschreibung = "UiSchreiber (Einkauf, Lieferschein) + OpenTransSchreiber (Verkauf-AB)"
    else:
        verkauf = ui
        beschreibung = "UiSchreiber (Einkauf, Verkauf-AB, Lieferschein)"
        if lizenziert is None:
            warnungen.append(
                f"Firma '{firma.id}': opentrans.lizenziert ist noch nicht geprüft (null) – "
                "Verkauf-AB per UI-Automation"
            )
    return Schreiberwahl(ui, verkauf, ui, dry_run, beschreibung)


# ---------------------------------------------------------------------------
# Revised Order
# ---------------------------------------------------------------------------


def ist_revision(paket: Belegpaket) -> bool:
    """Revised Order laut Freigabe (``paket.ist_revision``) oder Extraktion (``roh.ist_revision``)."""
    return bool(paket.ist_revision or paket.roh.ist_revision)


def revision_nummern(paket: Belegpaket) -> set[str]:
    """Normalisierte Nummern der ersetzten Bestellung: ``revision_von`` (Paket, sonst Dokument); fehlt sie,
    die eigene Kundenbestellnummer/Belegnummer (eine Revised Order trägt meist dieselbe Nummer).
    Leer, wenn das Paket keine Revision ist."""
    from auftragsautomat.matching.pruefung import normalisiere_belegnummer

    if not ist_revision(paket):
        return set()
    quelle = (paket.revision_von or paket.roh.revision_von or "").strip()
    if quelle:
        return {normalisiere_belegnummer(quelle)} - {""}
    # TODO-PRUEFEN: ob Revised Orders der Kunden (z. B. LEH) immer dieselbe Bestellnummer tragen; sonst muss
    # die Extraktion revision_von liefern.
    return {normalisiere_belegnummer(n) for n in (paket.roh.bestellnummer, paket.roh.belegnummer) if n} - {""}


def _revision_dubletten_abstufen(paket: Belegpaket, hinweise: list[Pruefhinweis], warnungen: list[str]) -> list[Pruefhinweis]:
    """DUBLETTE-Treffer zur ersetzten Bestellung auf INFO abstufen (in place); liefert die verbleibenden FEHLER."""
    from auftragsautomat.matching.pruefung import normalisiere_belegnummer

    nummern = revision_nummern(paket)
    if not nummern:
        return [h for h in hinweise if h.schweregrad == Schweregrad.FEHLER]
    verbleibend: list[Pruefhinweis] = []
    for h in hinweise:
        if h.code != Pruefcode.DUBLETTE or h.schweregrad != Schweregrad.FEHLER:
            continue
        kandidaten = {
            normalisiere_belegnummer(h.details.get("belegnummer")),
            normalisiere_belegnummer(h.details.get("kundenbestellnummer")),
        } - {""}
        if kandidaten & nummern:
            h.schweregrad = Schweregrad.INFO
            h.text = (
                f"Revised Order: ersetzt {h.details.get('belegart') or 'den'}-Beleg {h.details.get('belegnummer') or '?'} "
                f"(Cde und AB werden neu angelegt) – {h.text}"
            )
            h.details["revision"] = "ersetzt"
            warnungen.append(h.text)
            logger.info("Revised Order: Dubletten-Treffer %s als Info gewertet", h.details.get("belegnummer"))
        else:
            verbleibend.append(h)
    return verbleibend


# ---------------------------------------------------------------------------
# Dubletten
# ---------------------------------------------------------------------------


def _lade_stammdaten(firma: Firma) -> Stammdaten:
    from auftragsautomat.stammdaten.laden import lade_stammdaten

    return lade_stammdaten(firma, cache=False)


def _stammdaten_sicher(firma: Firma, stammdaten: Stammdaten | None, warnungen: list[str]) -> Stammdaten | None:
    """Übergebene Stammdaten oder frisch geladene; None (mit Warnung), wenn sie nicht ladbar sind."""
    if stammdaten is not None:
        return stammdaten
    try:
        return _lade_stammdaten(firma)
    except Exception as exc:  # noqa: BLE001 - jede Ladeursache wird gemeldet, nie verschluckt
        logger.exception("Stammdaten für Firma %s nicht ladbar", firma.id)
        warnungen.append(f"Dublettenprüfung nicht möglich, Stammdaten nicht ladbar: {type(exc).__name__}: {exc}")
        return None


def _pruefe_dubletten_erneut(
    paket: Belegpaket, firma: Firma, stammdaten: Stammdaten | None, warnungen: list[str]
) -> list[Pruefhinweis] | None:
    """Dublettenprüfung mit frischen Stammdaten; None = Prüfung nicht möglich (Stammdaten nicht ladbar).

    Bei einer Revised Order werden Treffer zur ersetzten Bestellung als Info gewertet und nicht geliefert.
    """
    from auftragsautomat.matching.pruefung import pruefe_dubletten

    if stammdaten is None:
        return None
    if stammdaten.firma_id != firma.id:
        warnungen.append(
            f"Dublettenprüfung nicht möglich: übergebene Stammdaten gehören zu Firma '{stammdaten.firma_id}', "
            f"nicht '{firma.id}'"
        )
        return None
    treffer = pruefe_dubletten(paket, stammdaten, eintragen=True)
    treffer = _revision_dubletten_abstufen(paket, treffer, warnungen)
    if treffer:
        logger.error("Dublettenprüfung vor dem Buchen: %d Treffer für %s", len(treffer), paket.roh.quelldatei.name)
    else:
        logger.info("Dublettenprüfung vor dem Buchen: keine Dublette (%d Belege geprüft)", len(stammdaten.belege))
    return treffer


# ---------------------------------------------------------------------------
# Schreiben
# ---------------------------------------------------------------------------


def _sicher_schreiben(
    aktion: Callable[[], Belegergebnis], belegart: Belegart, dry_run: bool, netto: Decimal, name: str
) -> Belegergebnis:
    """Ruft den Schreiber auf; Exceptions werden zu Belegergebnis(erfolgreich=False)."""
    try:
        ergebnis = aktion()
        if not isinstance(ergebnis, Belegergebnis):
            raise TypeError(f"Schreiber lieferte {type(ergebnis).__name__} statt Belegergebnis")
    except Exception as exc:  # noqa: BLE001 - Schreiber-Fehler dürfen die Buchung nie unprotokolliert abbrechen
        logger.exception("%s: Schreiber hat eine Ausnahme ausgelöst", name)
        return Belegergebnis(
            belegart=belegart,
            netto=netto,
            erfolgreich=False,
            dry_run=dry_run,
            methode="keine",
            fehler=f"{type(exc).__name__}: {exc}",
        )
    if ergebnis.erfolgreich:
        logger.info("%s: erfolgreich (Belegnr. %s, Methode %s)", name, ergebnis.belegnummer or "-", ergebnis.methode)
    else:
        logger.error("%s: fehlgeschlagen: %s", name, ergebnis.fehler or "ohne Fehlertext")
    return ergebnis


# ---------------------------------------------------------------------------
# PDF-Ausgabe (Dateinamen ausschließlich aus benennung.py)
# ---------------------------------------------------------------------------


def _dateiname(kuerzel: str, paket: Belegpaket, settings: Settings, datum: date, warnungen: list[str]) -> str | None:
    """Name laut Titulierung (``benennung.name_fuer_paket``); None mit Warnung, wenn nicht bestimmbar."""
    try:
        from auftragsautomat import benennung
    except ImportError as exc:  # pragma: no cover - nur, wenn das Modul parallel noch entsteht
        warnungen.append(f"Benennung nicht verfügbar ({exc}); keine PDF für {kuerzel}")
        return None
    try:
        name = benennung.name_fuer_paket(kuerzel, paket, settings, datum=datum)
    except (ValueError, KeyError) as exc:
        warnungen.append(f"Dateiname für {kuerzel} nicht bestimmbar ({exc}); keine PDF")
        return None
    if kuerzel == KUERZEL_AUFTRAGSKOPIE:
        verstoesse = benennung.pruefe_auftragskopie_name(name, settings)
        if verstoesse:
            warnungen.append(
                f"Name der Auftragskopie enthält verbotene Wörter ({', '.join(verstoesse)}): {name} – "
                "Kunden-/Lieferantenkurznamen prüfen (Geschäftsregel 18)"
            )
    return name


def _pdf(
    ergebnis: BuchungsErgebnis,
    schreiber: LexwareSchreiber,
    beleg: Belegergebnis,
    *,
    schluessel: str,
    formular: str | None,
    dateiname: str,
    dry_run: bool,
) -> Path | None:
    """Eine PDF über den Schreiber ausgeben und in ``ergebnis.pdfs`` eintragen; Fehler sind nur Warnungen."""
    if not unterstuetzt_pdf(schreiber):
        ergebnis.warnungen.append(
            f"PDF {schluessel} ({dateiname}) nicht erzeugt: Schreiber {type(schreiber).__name__} kennt keine PDF-Ausgabe"
        )
        return None
    try:
        pfad = schreiber.pdf_ausgeben(beleg, formular=formular, dateiname=dateiname, dry_run=dry_run)
    except Exception as exc:  # noqa: BLE001 - die Belege sind gespeichert; eine PDF darf die Buchung nicht kippen
        logger.exception("PDF-Ausgabe %s fehlgeschlagen", dateiname)
        ergebnis.warnungen.append(f"PDF {schluessel} ({dateiname}) fehlgeschlagen: {type(exc).__name__}: {exc}")
        return None
    if pfad is None:
        ergebnis.warnungen.append(f"PDF {schluessel} ({dateiname}) nicht erzeugt (siehe Hinweise des Belegs)")
        return None
    pfad = Path(pfad)
    ergebnis.pdfs[schluessel] = pfad
    if beleg.pdf is None:
        beleg.pdf = pfad
    ergebnis.warnungen.append(f"PDF {schluessel}: {pfad}" + (" (Dry Run: geplant, nicht erzeugt)" if dry_run else ""))
    return pfad


def _pdfs_en(
    ergebnis: BuchungsErgebnis,
    schreiber: LexwareSchreiber,
    beleg: Belegergebnis,
    paket: Belegpaket,
    firma: Firma,
    settings: Settings,
    *,
    formular: str,
    dry_run: bool,
    datum: date,
    nummer: int = 0,
) -> None:
    """EN-Formular ``speditionsauftrag_kopien``-mal ausgeben: 1. "Cde" (Revision "Neu"), 2. "Speditionsauftrag"."""
    kopien = max(int(firma.formulare.speditionsauftrag_kopien), 1)
    zusatz = f" ({nummer + 1})" if nummer else ""  # weitere Weingüter: eigene Schlüssel
    for i in range(kopien):
        kuerzel = KUERZEL_BESTELLUNG if i == 0 else KUERZEL_SPEDITIONSAUFTRAG
        name = _dateiname(kuerzel, paket, settings, datum, ergebnis.warnungen)
        if name is None:
            continue
        schluessel = kuerzel + zusatz
        if i >= 2:  # dritte und weitere Kopien des Speditionsauftrags
            name = f"{Path(name).stem} ({i}){Path(name).suffix}"
            schluessel = f"{schluessel} ({i})"
        _pdf(ergebnis, schreiber, beleg, schluessel=schluessel, formular=formular, dateiname=name, dry_run=dry_run)


# ---------------------------------------------------------------------------
# Aufteilung nach Weingut (Einkauf)
# ---------------------------------------------------------------------------


def _teilpaket(paket: Belegpaket, lieferant: Any, positionen: list[PositionsZuordnung], haupt: bool) -> Belegpaket:
    """Kopie des Pakets mit nur den Positionen eines Weinguts; Lieferant und Belegfeld Weingut gesetzt."""
    laufnummern = {p.roh.laufnr for p in positionen}
    teil = paket.model_copy(deep=True)
    teil.positionen = [p for p in teil.positionen if p.roh.laufnr in laufnummern]
    teil.lieferant = LieferantZuordnung(
        lieferant=lieferant,
        kandidaten=list(paket.lieferant.kandidaten) if haupt else [],
        status=paket.lieferant.status if haupt else MatchStatus.SICHER,
        quelle=paket.lieferant.quelle if haupt else "aufteilung",
    )
    teil.belegfelder.weingut = lieferant.name
    return teil


def einkaufs_teilpakete(
    paket: Belegpaket, stammdaten: Stammdaten | None, warnungen: list[str] | None = None
) -> list[tuple[Belegpaket, str]]:
    """Je Weingut ein (Teil-)Paket für die Einkauf-Bestellung, Hauptlieferant zuerst.

    Gehören alle Positionen zum Hauptlieferanten (oder ist keine Aufteilung möglich), ist das
    Ergebnis ``[(paket, "<Lieferant>")]`` – das Originalpaket, unverändert.
    """
    from auftragsautomat.matching.pruefung import gruppiere_nach_lieferant

    lieferant = paket.lieferant.lieferant
    beschreibung_haupt = f"{lieferant.lieferantennummer} {lieferant.name}" if lieferant else "-"
    if stammdaten is None:
        if warnungen is not None and lieferant is not None:
            warnungen.append(
                "Aufteilung der Einkauf-Bestellung nach Weingut nicht geprüft (Stammdaten nicht verfügbar); "
                f"alle Positionen gehen an {beschreibung_haupt}"
            )
        return [(paket, beschreibung_haupt)]
    gruppen = gruppiere_nach_lieferant(paket, stammdaten)
    if len(gruppen) <= 1:
        return [(paket, beschreibung_haupt)]
    teile: list[tuple[Belegpaket, str]] = []
    for gruppe in gruppen:
        teile.append((_teilpaket(paket, gruppe.lieferant, gruppe.positionen, gruppe.haupt), gruppe.beschreibung))
    logger.info(
        "Einkauf wird auf %d Weingüter aufgeteilt: %s",
        len(teile),
        "; ".join(f"{b} ({len(t.aktive_positionen())} Pos.)" for t, b in teile),
    )
    if warnungen is not None:
        warnungen.append(
            f"Positionen mehrerer Weingüter: {len(teile)} Einkauf-Bestellungen ("
            + "; ".join(f"{b}: Pos. {', '.join(str(p.roh.laufnr) for p in t.aktive_positionen())}" for t, b in teile)
            + ")"
        )
    return teile


# ---------------------------------------------------------------------------
# Ergebnis, Protokoll, Archiv
# ---------------------------------------------------------------------------


def _verweigert(paket: Belegpaket, dry_run: bool, zeitpunkt: datetime, gruende: list[str], vorab: list[str] | None = None) -> BuchungsErgebnis:
    logger.error("%s (%s): %s", _VERWEIGERT, paket.roh.quelldatei.name, "; ".join(gruende))
    return BuchungsErgebnis(
        firma_id=paket.firma_id or "unbekannt",
        quelldatei=Path(paket.roh.quelldatei),
        dry_run=dry_run,
        zeitpunkt=zeitpunkt,
        warnungen=[*(vorab or []), f"{_VERWEIGERT}: nichts geschrieben", *gruende],
        ist_revision=ist_revision(paket),
    )


def _protokollieren(paket: Belegpaket, ergebnis: BuchungsErgebnis, settings: Settings, firma: Firma | None = None) -> None:
    try:
        pfad = schreibe_protokoll(paket, ergebnis, Path(settings.log_ordner), firma=firma)
    except Exception as exc:  # noqa: BLE001 - ein Protokollfehler darf das Buchungsergebnis nicht verschlucken
        logger.exception("Protokoll konnte nicht geschrieben werden")
        ergebnis.warnungen.append(f"Protokoll konnte nicht geschrieben werden: {type(exc).__name__}: {exc}")
        return
    ergebnis.protokoll = Path(pfad)
    ergebnis.warnungen.append(f"Protokoll: {pfad}")


def _archivieren(paket: Belegpaket, ergebnis: BuchungsErgebnis, settings: Settings) -> None:
    quelldatei = Path(paket.roh.quelldatei)
    try:
        ziel = archiviere(quelldatei, settings, ergebnis.firma_id)
    except FileNotFoundError:
        logger.warning("Quelldatei %s nicht gefunden, nichts archiviert", quelldatei)
        ergebnis.warnungen.append(f"Quelldatei {quelldatei} nicht gefunden, nichts archiviert")
        return
    except OSError as exc:
        logger.exception("Quelldatei %s konnte nicht archiviert werden", quelldatei)
        ergebnis.warnungen.append(f"Quelldatei konnte nicht archiviert werden: {exc}")
        return
    ergebnis.archiv = Path(ziel)
    ergebnis.warnungen.append(f"Quelldatei archiviert: {ziel}")


# ---------------------------------------------------------------------------
# Buchen
# ---------------------------------------------------------------------------


def _einkauf(
    paket: Belegpaket,
    firma: Firma,
    settings: Settings,
    wahl: Schreiberwahl,
    ergebnis: BuchungsErgebnis,
    stammdaten: Stammdaten | None,
    datum: date,
) -> bool:
    """Schritt (1): Einkauf-Bestellung je Weingut + PDF "Cde"/"Speditionsauftrag". Liefert einkauf_ok."""
    effektiv_dry_run = ergebnis.dry_run
    einkauf_ok = True
    # TODO-LEXWARE: Reihenfolge Einkauf -> Angebot -> AB ist Annahme aus dem Ablaufplan; am echten System prüfen,
    # ob ein scharf fehlgeschlagener Assistent (Abbruch nach Fehler) wirklich keinen Teilbeleg hinterlässt.
    teile = einkaufs_teilpakete(paket, stammdaten if stammdaten and stammdaten.firma_id == firma.id else None, ergebnis.warnungen)
    for nummer, (teilpaket, beschreibung) in enumerate(teile):
        if nummer > 0 and not einkauf_ok and not effektiv_dry_run:
            ergebnis.warnungen.append(f"Einkauf-Bestellung {beschreibung} NICHT angelegt (vorherige Bestellung fehlgeschlagen)")
            continue
        teil = _sicher_schreiben(
            lambda tp=teilpaket: wahl.einkauf.schreibe_einkauf_bestellung(tp, dry_run=effektiv_dry_run),
            Belegart.BESTELLUNG,
            effektiv_dry_run,
            teilpaket.summe_netto_ek,
            f"Einkauf-Bestellung {beschreibung}" if len(teile) > 1 else "Einkauf-Bestellung",
        )
        teil.partner = beschreibung
        if nummer == 0:
            ergebnis.einkauf = teil
        else:
            ergebnis.einkauf_weitere.append(teil)
        einkauf_ok = einkauf_ok and teil.erfolgreich
        if teil.erfolgreich:
            formular = firma.formulare.einkauf_en
            if formular:
                _pdfs_en(
                    ergebnis, wahl.einkauf, teil, teilpaket, firma, settings,
                    formular=formular, dry_run=effektiv_dry_run, datum=datum, nummer=nummer,
                )
            else:
                ergebnis.warnungen.append(
                    f"Keine PDF der Einkauf-Bestellung: formulare.einkauf_en ist für Firma {firma.id} nicht konfiguriert"
                )
    if not einkauf_ok:
        if effektiv_dry_run:
            ergebnis.warnungen.append(
                "Dry Run: Einkauf-Bestellung fehlgeschlagen, Verkauf wird trotzdem simuliert "
                "(scharf würde hier abgebrochen)"
            )
        else:
            ergebnis.warnungen.append(
                "Einkauf-Bestellung fehlgeschlagen – Angebot, Verkauf-AB und Lieferschein werden NICHT angelegt "
                "(keine halbe Buchung)"
            )
    return einkauf_ok


def _verkauf(
    paket: Belegpaket,
    firma: Firma,
    settings: Settings,
    wahl: Schreiberwahl,
    ergebnis: BuchungsErgebnis,
    datum: date,
) -> None:
    """Schritt (2): Verkauf laut ``Firma.verkauf_weg`` (direkt_ab oder Angebot -> AB) + PDF "Kopie"."""
    effektiv_dry_run = ergebnis.dry_run
    schreiber = wahl.verkauf
    weg = firma.verkauf_weg
    ueber_angebot = weg in ANGEBOTSWEGE
    if ueber_angebot and not unterstuetzt_angebot(schreiber):
        ergebnis.warnungen.append(
            f"Verkaufsweg '{weg}': Schreiber {type(schreiber).__name__} kennt kein Angebot – Auftragsbestätigung wird direkt angelegt"
        )
        ueber_angebot = False

    if ueber_angebot:
        angebot = _sicher_schreiben(
            lambda: schreiber.schreibe_angebot(paket, dry_run=effektiv_dry_run),
            Belegart.ANGEBOT,
            effektiv_dry_run,
            paket.summe_netto_vk,
            "Angebot",
        )
        ergebnis.angebot = angebot
        formular_angebot = firma.formulare.angebot_en
        if angebot.erfolgreich and angebot.methode != "keine" and formular_angebot:
            # Weinstrasse Agentur: das Angebot ist der Beleg ans Weingut (Cde) und der Speditionsauftrag
            _pdfs_en(
                ergebnis, schreiber, angebot, paket, firma, settings,
                formular=formular_angebot, dry_run=effektiv_dry_run, datum=datum,
            )
        if not angebot.erfolgreich and not effektiv_dry_run:
            ergebnis.warnungen.append(
                "Angebot fehlgeschlagen – Auftragsbestätigung und Lieferschein werden NICHT angelegt (keine halbe Buchung)"
            )
            return
        if not angebot.erfolgreich:
            ergebnis.warnungen.append("Dry Run: Angebot fehlgeschlagen, Auftragsbestätigung wird trotzdem simuliert")
        ergebnis.verkauf = _sicher_schreiben(
            lambda: schreiber.angebot_zu_ab(angebot, paket, weg=weg, dry_run=effektiv_dry_run),  # type: ignore[arg-type]
            Belegart.AUFTRAGSBESTAETIGUNG,
            effektiv_dry_run,
            paket.summe_netto_vk,
            f"Verkauf-AB ({weg_beschreibung(weg)})",
        )
    else:
        ergebnis.verkauf = _sicher_schreiben(
            lambda: schreiber.schreibe_verkauf_ab(paket, dry_run=effektiv_dry_run),
            Belegart.AUFTRAGSBESTAETIGUNG,
            effektiv_dry_run,
            paket.summe_netto_vk,
            "Verkauf-AB",
        )

    verkauf = ergebnis.verkauf
    if verkauf is not None and verkauf.erfolgreich:
        name = _dateiname(KUERZEL_AUFTRAGSKOPIE, paket, settings, datum, ergebnis.warnungen)
        if name is not None:
            if not firma.formulare.verkauf_ab:
                ergebnis.warnungen.append(
                    f"formulare.verkauf_ab ist für Firma {firma.id} nicht konfiguriert – Auftragskopie mit Lexware-Standardformular"
                )
            _pdf(
                ergebnis, schreiber, verkauf,
                schluessel=KUERZEL_AUFTRAGSKOPIE, formular=firma.formulare.verkauf_ab, dateiname=name, dry_run=effektiv_dry_run,
            )


def _lieferschein(
    paket: Belegpaket,
    firma: Firma,
    settings: Settings,
    wahl: Schreiberwahl,
    ergebnis: BuchungsErgebnis,
    datum: date,
) -> None:
    """Schritt (3): Lieferschein nur mit Anforderung UND Firmenfreigabe, aus der AB; danach PDF "Ls"."""
    effektiv_dry_run = ergebnis.dry_run
    if not paket.lieferschein_erstellen:
        return
    if not firma.lieferschein_erlaubt:
        ergebnis.warnungen.append(f"Lieferschein für {firma.name} nicht vorgesehen (lieferschein_erlaubt=False) – nicht angelegt")
        return
    verkauf = ergebnis.verkauf
    if verkauf is None or not verkauf.erfolgreich:
        ergebnis.warnungen.append("Lieferschein nicht erstellt: Verkauf-AB wurde nicht (erfolgreich) angelegt")
        return
    if not verkauf.belegnummer:
        # TODO-LEXWARE: Nach dem openTRANS-Import vergibt Lexware die AB-Nummer erst beim "Übernehmen" des
        # Webshop-Auftrags; prüfen, ob die Nummer aus der Auftragsliste gelesen und der Lieferschein dann
        # per UI-Automation nachgezogen werden kann (zweiter Lauf mit ab_belegnummer).
        ergebnis.warnungen.append(
            "Lieferschein erst nach Import möglich: AB-Belegnummer liegt noch nicht vor"
            + (" (openTRANS-Webshop-Auftrag muss erst in Lexware übernommen werden)" if verkauf.methode == "opentrans" else "")
        )
        return
    ab_nummer = verkauf.belegnummer
    ergebnis.lieferschein = _sicher_schreiben(
        lambda: wahl.lieferschein.auftrag_weiterfuehren_lieferschein(paket, ab_nummer, dry_run=effektiv_dry_run),
        Belegart.LIEFERSCHEIN,
        effektiv_dry_run,
        paket.summe_netto_vk,
        "Lieferschein",
    )
    if ergebnis.lieferschein.erfolgreich:
        _pdf_lieferschein(paket, firma, settings, wahl.lieferschein, ergebnis, datum)


def _pdf_lieferschein(
    paket: Belegpaket, firma: Firma, settings: Settings, schreiber: LexwareSchreiber, ergebnis: BuchungsErgebnis, datum: date
) -> None:
    assert ergebnis.lieferschein is not None
    name = _dateiname(KUERZEL_LIEFERSCHEIN, paket, settings, datum, ergebnis.warnungen)
    if name is not None:
        _pdf(
            ergebnis, schreiber, ergebnis.lieferschein,
            schluessel=KUERZEL_LIEFERSCHEIN, formular=firma.formulare.lieferschein, dateiname=name, dry_run=ergebnis.dry_run,
        )


def buche(
    paket: Belegpaket,
    settings: Settings,
    *,
    dry_run: bool = True,
    schreiber: LexwareSchreiber | None = None,
    stammdaten: Stammdaten | None = None,
) -> BuchungsErgebnis:
    """Bucht ein freigegebenes Belegpaket (Einkauf -> Angebot -> AB -> Lieferschein, PDFs) und protokolliert.

    ``dry_run=True`` (Standard) simuliert; scharf nur mit ``dry_run=False`` UND erreichbarem Lexware
    (bzw. übergebenem ``schreiber``). ``stammdaten`` dient der erneuten Dublettenprüfung; fehlt es,
    werden die Stammdaten der Firma frisch geladen. Wirft keine Exceptions für fachliche Fehler –
    alles steht im ``BuchungsErgebnis`` (``erfolgreich``, ``warnungen``, Teilergebnisse, ``pdfs``).
    """
    zeitpunkt = datetime.now()
    quelldatei = Path(paket.roh.quelldatei)
    revision = ist_revision(paket)
    logger.info(
        "Buchung angefordert (%s): Firma %s, Quelldatei %s, Einkauf=%s, Lieferschein=%s, Revision=%s",
        "Dry Run" if dry_run else "scharf",
        paket.firma_id or "-",
        quelldatei.name,
        paket.einkauf_erforderlich,
        paket.lieferschein_erstellen,
        revision,
    )

    vorab: list[str] = []
    if revision:
        # Revised Order: Dubletten-Treffer zur ersetzten Bestellung aus der Vorprüfung sind Info (Geschäftsregel 15)
        _revision_dubletten_abstufen(paket, [h for h in paket.hinweise if h.code == Pruefcode.DUBLETTE], vorab)
    gruende = vorbedingungen(paket, settings)
    if gruende:
        return _verweigert(paket, dry_run, zeitpunkt, gruende, vorab)
    assert paket.firma_id is not None
    firma = settings.firma(paket.firma_id)

    warnungen: list[str] = list(vorab)
    try:
        wahl = waehle_schreiber(settings, firma, dry_run, schreiber, warnungen)
    except Exception as exc:  # noqa: BLE001 - z. B. NichtAufWindows / BackendNichtVerfuegbar / Lexware nicht gestartet
        logger.exception("Lexware-Schreiber konnte nicht erzeugt werden")
        return _verweigert(paket, dry_run, zeitpunkt, [f"Lexware-Schreiber nicht verfügbar: {type(exc).__name__}: {exc}"], warnungen)
    effektiv_dry_run = wahl.dry_run
    logger.info("Schreiber: %s; Modus: %s", wahl.beschreibung, "Dry Run" if effektiv_dry_run else "SCHARF")

    ergebnis = BuchungsErgebnis(
        firma_id=firma.id,
        quelldatei=quelldatei,
        dry_run=effektiv_dry_run,
        zeitpunkt=zeitpunkt,
        warnungen=warnungen,
        ist_revision=revision,
    )
    if revision:
        ersetzt = (paket.revision_von or paket.roh.revision_von or "").strip()
        ergebnis.warnungen.append(
            "Revised Order: Bestellung (Cde) und AB werden neu angelegt, nicht geändert"
            + (f"; ersetzt {ersetzt}" if ersetzt else "")
            + " – Dateikürzel 'Neu' / 'Neu Kopie'"
        )
    ergebnis.warnungen.append(f"Verkaufsweg: {weg_beschreibung(firma.verkauf_weg)} ({firma.verkauf_weg})")

    # Dubletten unmittelbar vor dem Schreiben (Geschäftsregel 5)
    stammdaten = _stammdaten_sicher(firma, stammdaten, ergebnis.warnungen)
    dubletten = _pruefe_dubletten_erneut(paket, firma, stammdaten, ergebnis.warnungen)
    if dubletten:
        ergebnis.warnungen.append(f"{_VERWEIGERT}: Dublette – nichts geschrieben")
        ergebnis.warnungen.extend(h.text for h in dubletten)
        logger.error("%s (%s): Dublette", _VERWEIGERT, quelldatei.name)
        _protokollieren(paket, ergebnis, settings, firma)
        return ergebnis
    if dubletten is None and not effektiv_dry_run:
        ergebnis.warnungen.append(f"{_VERWEIGERT}: scharf nur mit erfolgreicher Dublettenprüfung – nichts geschrieben")
        logger.error("%s (%s): Dublettenprüfung nicht möglich", _VERWEIGERT, quelldatei.name)
        _protokollieren(paket, ergebnis, settings, firma)
        return ergebnis

    # Vorprüfung der Verkauf-AB (openTRANS), damit scharf kein Einkauf ohne AB entsteht
    if isinstance(wahl.verkauf, OpenTransSchreiber):
        probleme = wahl.verkauf.pruefe_paket(paket)
        if probleme:
            ergebnis.warnungen.append(f"{_VERWEIGERT}: Verkauf-AB per openTRANS nicht möglich – nichts geschrieben")
            ergebnis.warnungen.extend(probleme)
            logger.error("%s (%s): openTRANS-Vorprüfung: %s", _VERWEIGERT, quelldatei.name, "; ".join(probleme))
            _protokollieren(paket, ergebnis, settings, firma)
            return ergebnis

    datum = zeitpunkt.date()

    # 1. Einkauf-Bestellung je Weingut (+ PDF Cde / Speditionsauftrag)
    if paket.einkauf_erforderlich:
        einkauf_ok = _einkauf(paket, firma, settings, wahl, ergebnis, stammdaten, datum)
    else:
        einkauf_ok = True
        ergebnis.warnungen.append("Keine Einkauf-Bestellung: für diese Firma nicht vorgesehen (einkauf_erforderlich=False)")

    # 2. Verkauf: Angebot -> AB (oder AB direkt) + PDF Kopie
    if einkauf_ok or effektiv_dry_run:
        _verkauf(paket, firma, settings, wahl, ergebnis, datum)

    # 3. Lieferschein (+ PDF Ls)
    _lieferschein(paket, firma, settings, wahl, ergebnis, datum)

    # Konsistenz: meldet ein Teilergebnis Dry Run, obwohl scharf angefordert war, ist nichts gebucht
    teile_alle = [t for t in (ergebnis.angebot, ergebnis.einkauf, ergebnis.verkauf, ergebnis.lieferschein) if t is not None]
    teile_alle.extend(ergebnis.einkauf_weitere)
    if not effektiv_dry_run and any(t.dry_run for t in teile_alle):
        ergebnis.warnungen.append(
            "Mindestens ein Teilergebnis meldet Dry Run, obwohl scharf angefordert war – Lauf gilt als Dry Run, "
            "Quelldatei wird nicht archiviert"
        )
        logger.warning("Scharfer Lauf, aber Teilergebnis mit dry_run=True (%s)", quelldatei.name)
        ergebnis.dry_run = True

    logger.info(
        "Buchung %s (%s): %s; PDFs: %s",
        "ERFOLGREICH" if ergebnis.erfolgreich else "FEHLGESCHLAGEN",
        "Dry Run" if ergebnis.dry_run else "scharf",
        quelldatei.name,
        ", ".join(ergebnis.pdfs) or "-",
    )

    # 4. Protokoll, 5. Archiv (nur scharf und erfolgreich)
    _protokollieren(paket, ergebnis, settings, firma)
    if ergebnis.erfolgreich and not ergebnis.dry_run:
        _archivieren(paket, ergebnis, settings)
    # 6. Nachbereitung (Fixtermin, Entwürfe, Dossier, Ablage) – nur nach erfolgreicher Buchung
    if ergebnis.erfolgreich:
        _nachbereiten(paket, ergebnis, settings)
    return ergebnis


def _nachbereiten(paket: Belegpaket, ergebnis: BuchungsErgebnis, settings: Settings) -> None:
    """Fixtermin eintragen, E-Mail-Entwürfe, Dossier und Ablage (``lexware.nachbereitung``).

    Läuft nach Protokoll und Archivierung; Dry Run liefert Vorschauen (Vorschau-CSV, ``.eml``, lokales
    Dossier). Fehler der Nachbereitung werden nur als Warnung vermerkt und markieren die Buchung
    **nicht** rückwirkend als fehlgeschlagen (die Belege sind bereits gespeichert).
    """
    from auftragsautomat.lexware import nachbereitung

    try:
        nb = nachbereitung.nachbereiten(paket, ergebnis, settings)
    except Exception as exc:  # noqa: BLE001 - die Buchung ist abgeschlossen; nichts darf sie kippen
        logger.exception("Nachbereitung konnte nicht ausgeführt werden")
        ergebnis.warnungen.append(
            f"Nachbereitung nicht ausgeführt ({type(exc).__name__}: {exc}) – Fixtermin, Entwürfe und Dossier "
            "bitte über die CLI nachholen (fixtermin-eintragen, entwuerfe, dossier); die Buchung bleibt gültig"
        )
        return
    ergebnis.warnungen.append(f"Nachbereitung: {nb.kurzfassung}")
    if nb.speicherort is not None:
        ergebnis.warnungen.append(f"Nachbereitung gespeichert: {nb.speicherort}")


def lieferschein_nachziehen(
    paket: Belegpaket,
    ab_belegnummer: str,
    settings: Settings,
    *,
    dry_run: bool = True,
    schreiber: LexwareSchreiber | None = None,
) -> BuchungsErgebnis:
    """Legt nur den Lieferschein zu einer bereits vorhandenen AB an ("Auftrag weiterführen") und gibt ihn als PDF "Ls" aus.

    Zweiter Einstieg nach dem openTRANS-Weg: Lexware vergibt die AB-Nummer erst beim Übernehmen des
    Webshop-Auftrags, deshalb kann :func:`buche` den Lieferschein dann nicht anlegen. Vorbedingungen
    wie bei :func:`buche` (Freigabe, nicht blockiert, Firma/Kunde/Positionen, Mandantenschutz);
    ``paket.lieferschein_erstellen`` und ``Firma.lieferschein_erlaubt`` müssen gesetzt sein
    (Geschäftsregel 10/14). Es wird protokolliert, aber nie archiviert (die Quelldatei gehört zur Buchung der AB).
    """
    zeitpunkt = datetime.now()
    quelldatei = Path(paket.roh.quelldatei)
    ab_belegnummer = (ab_belegnummer or "").strip()
    logger.info(
        "Lieferschein nachziehen (%s): Firma %s, Quelldatei %s, AB %s",
        "Dry Run" if dry_run else "scharf",
        paket.firma_id or "-",
        quelldatei.name,
        ab_belegnummer or "-",
    )
    gruende = vorbedingungen(paket, settings)
    if not ab_belegnummer:
        gruende.append("AB-Belegnummer fehlt (Lieferschein nur zu einer vorhandenen Auftragsbestätigung)")
    if not paket.lieferschein_erstellen:
        gruende.append("Lieferschein ist für dieses Paket nicht angefordert (lieferschein_erstellen=False)")
    if paket.firma_id and paket.firma_id in settings.firmen and not settings.firmen[paket.firma_id].lieferschein_erlaubt:
        gruende.append(f"Lieferschein für {settings.firmen[paket.firma_id].name} nicht vorgesehen (lieferschein_erlaubt=False)")
    if gruende:
        return _verweigert(paket, dry_run, zeitpunkt, gruende)
    assert paket.firma_id is not None
    firma = settings.firma(paket.firma_id)

    warnungen: list[str] = []
    try:
        wahl = waehle_schreiber(settings, firma, dry_run, schreiber, warnungen)
    except Exception as exc:  # noqa: BLE001 - NichtAufWindows / Lexware nicht gestartet
        logger.exception("Lexware-Schreiber konnte nicht erzeugt werden")
        return _verweigert(paket, dry_run, zeitpunkt, [f"Lexware-Schreiber nicht verfügbar: {type(exc).__name__}: {exc}"], warnungen)
    effektiv_dry_run = wahl.dry_run
    ergebnis = BuchungsErgebnis(
        firma_id=firma.id, quelldatei=quelldatei, dry_run=effektiv_dry_run, zeitpunkt=zeitpunkt, warnungen=warnungen,
        ist_revision=ist_revision(paket),
    )
    ergebnis.warnungen.append(f"Nur Lieferschein zu AB {ab_belegnummer} (Einkauf und AB wurden früher gebucht)")
    ergebnis.lieferschein = _sicher_schreiben(
        lambda: wahl.lieferschein.auftrag_weiterfuehren_lieferschein(paket, ab_belegnummer, dry_run=effektiv_dry_run),
        Belegart.LIEFERSCHEIN,
        effektiv_dry_run,
        paket.summe_netto_vk,
        "Lieferschein",
    )
    if not effektiv_dry_run and ergebnis.lieferschein.dry_run:
        ergebnis.warnungen.append("Lieferschein meldet Dry Run, obwohl scharf angefordert war – Lauf gilt als Dry Run")
        ergebnis.dry_run = True
    if ergebnis.lieferschein.erfolgreich:
        _pdf_lieferschein(paket, firma, settings, wahl.lieferschein, ergebnis, zeitpunkt.date())
    logger.info(
        "Lieferschein %s (%s): %s",
        "ERFOLGREICH" if ergebnis.erfolgreich else "FEHLGESCHLAGEN",
        "Dry Run" if ergebnis.dry_run else "scharf",
        quelldatei.name,
    )
    _protokollieren(paket, ergebnis, settings, firma)
    return ergebnis
