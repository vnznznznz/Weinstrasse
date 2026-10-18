"""Buchungs-Orchestrierung: ein freigegebenes Belegpaket nach Lexware schreiben.

:func:`buche` ist die einzige Stelle, an der Belege angelegt werden. Ablauf:

1. **Vorbedingungen** (ohne sie wird nichts geschrieben, auch kein Protokoll):
   ``paket.freigegeben``, nicht ``paket.blockiert``, Firma gesetzt und bekannt, Kunde zugeordnet,
   Lieferant zugeordnet (wenn Einkauf erforderlich), mindestens eine aktive Position,
   Mandantenschutz (Kunde/Lieferant/Artikel gehören zur Firma des Pakets).
2. **Modus und Schreiber**: ein übergebener ``schreiber`` wird für alles verwendet. Sonst gilt die
   doppelte Sicherung: scharf nur, wenn ``dry_run=False`` übergeben wurde UND Lexware erreichbar ist
   (Windows) UND ``settings.dry_run`` aus ist – andernfalls läuft der
   :class:`~auftragsautomat.lexware.dry_run.DryRunSchreiber` und das Ergebnis trägt ``dry_run=True``.
   Unter Windows mit ``settings.dry_run = False``: ``UiSchreiber`` (Einkauf, Lieferschein, AB-Fallback),
   ``OpenTransSchreiber`` für die Verkauf-AB, wenn ``firma.opentrans.lizenziert`` True ist.
3. **Dubletten** unmittelbar vor dem Schreiben erneut prüfen (``matching.pruefung.pruefe_dubletten``
   mit frisch geladenen Stammdaten, Treffer werden ins Paket eingetragen) -> Abbruch bei Treffer.
4. **Reihenfolge**: Einkauf-Bestellung (nur ``paket.einkauf_erforderlich``) -> Verkauf-AB ->
   Lieferschein (nur ``paket.lieferschein_erstellen``, AB erfolgreich und AB-Belegnummer vorhanden;
   per openTRANS gibt es die Nummer erst nach dem Import -> Warnung, kein Lieferschein;
   dafür gibt es :func:`lieferschein_nachziehen`).
   Gehören die Positionen zu mehreren Weingütern (``matching.pruefung.gruppiere_nach_lieferant``),
   wird je Weingut eine eigene Einkauf-Bestellung angelegt (Geschäftsregel 2: Lieferant = Weingut):
   die des Hauptlieferanten in ``ergebnis.einkauf``, die übrigen in ``ergebnis.einkauf_weitere``;
   jede bekommt ein Teilpaket mit nur ihren Positionen und ihrem Weingut im Belegfeld.
   Schlägt der Einkauf scharf fehl, wird KEINE AB angelegt (keine halbe Buchung); im Dry Run
   läuft alles durch und sammelt die Ergebnisse.
5. **Protokoll** (``protokoll.schreibe_protokoll``, Pfad in ``ergebnis.protokoll``) und – nur scharf
   und erfolgreich – Archivierung der Quelldatei (``protokoll.archiviere``, ``ergebnis.archiv``).
   Im Dry Run wird nichts verschoben.

Schreiber dürfen keine Exceptions werfen; falls doch, wird daraus ein ``Belegergebnis`` mit
``erfolgreich=False`` und ``methode="keine"``.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from auftragsautomat.config import Firma, Settings
from auftragsautomat.lexware.basis import LexwareSchreiber
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
    Pruefhinweis,
    Schweregrad,
)
from auftragsautomat.protokoll import archiviere, schreibe_protokoll
from auftragsautomat.stammdaten.basis import Stammdaten

logger = logging.getLogger(__name__)

__all__ = [
    "Schreiberwahl",
    "buche",
    "einkaufs_teilpakete",
    "lieferschein_nachziehen",
    "vorbedingungen",
    "waehle_schreiber",
]

_VERWEIGERT = "Buchung verweigert"


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
    """Dublettenprüfung mit frischen Stammdaten; None = Prüfung nicht möglich (Stammdaten nicht ladbar)."""
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


def _verweigert(paket: Belegpaket, dry_run: bool, zeitpunkt: datetime, gruende: list[str], vorab: list[str] | None = None) -> BuchungsErgebnis:
    logger.error("%s (%s): %s", _VERWEIGERT, paket.roh.quelldatei.name, "; ".join(gruende))
    return BuchungsErgebnis(
        firma_id=paket.firma_id or "unbekannt",
        quelldatei=Path(paket.roh.quelldatei),
        dry_run=dry_run,
        zeitpunkt=zeitpunkt,
        warnungen=[*(vorab or []), f"{_VERWEIGERT}: nichts geschrieben", *gruende],
    )


def _protokollieren(paket: Belegpaket, ergebnis: BuchungsErgebnis, settings: Settings) -> None:
    try:
        pfad = schreibe_protokoll(paket, ergebnis, Path(settings.log_ordner))
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


def buche(
    paket: Belegpaket,
    settings: Settings,
    *,
    dry_run: bool = True,
    schreiber: LexwareSchreiber | None = None,
    stammdaten: Stammdaten | None = None,
) -> BuchungsErgebnis:
    """Bucht ein freigegebenes Belegpaket (Einkauf -> Verkauf-AB -> Lieferschein) und protokolliert.

    ``dry_run=True`` (Standard) simuliert; scharf nur mit ``dry_run=False`` UND erreichbarem Lexware
    (bzw. übergebenem ``schreiber``). ``stammdaten`` dient der erneuten Dublettenprüfung; fehlt es,
    werden die Stammdaten der Firma frisch geladen. Wirft keine Exceptions für fachliche Fehler –
    alles steht im ``BuchungsErgebnis`` (``erfolgreich``, ``warnungen``, Teilergebnisse).
    """
    zeitpunkt = datetime.now()
    quelldatei = Path(paket.roh.quelldatei)
    logger.info(
        "Buchung angefordert (%s): Firma %s, Quelldatei %s, Einkauf=%s, Lieferschein=%s",
        "Dry Run" if dry_run else "scharf",
        paket.firma_id or "-",
        quelldatei.name,
        paket.einkauf_erforderlich,
        paket.lieferschein_erstellen,
    )

    gruende = vorbedingungen(paket, settings)
    if gruende:
        return _verweigert(paket, dry_run, zeitpunkt, gruende)
    assert paket.firma_id is not None
    firma = settings.firma(paket.firma_id)

    warnungen: list[str] = []
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
    )

    # Dubletten unmittelbar vor dem Schreiben (Geschäftsregel 5)
    stammdaten = _stammdaten_sicher(firma, stammdaten, ergebnis.warnungen)
    dubletten = _pruefe_dubletten_erneut(paket, firma, stammdaten, ergebnis.warnungen)
    if dubletten:
        ergebnis.warnungen.append(f"{_VERWEIGERT}: Dublette – nichts geschrieben")
        ergebnis.warnungen.extend(h.text for h in dubletten)
        logger.error("%s (%s): Dublette", _VERWEIGERT, quelldatei.name)
        _protokollieren(paket, ergebnis, settings)
        return ergebnis
    if dubletten is None and not effektiv_dry_run:
        ergebnis.warnungen.append(f"{_VERWEIGERT}: scharf nur mit erfolgreicher Dublettenprüfung – nichts geschrieben")
        logger.error("%s (%s): Dublettenprüfung nicht möglich", _VERWEIGERT, quelldatei.name)
        _protokollieren(paket, ergebnis, settings)
        return ergebnis

    # Vorprüfung der Verkauf-AB (openTRANS), damit scharf kein Einkauf ohne AB entsteht
    if isinstance(wahl.verkauf, OpenTransSchreiber):
        probleme = wahl.verkauf.pruefe_paket(paket)
        if probleme:
            ergebnis.warnungen.append(f"{_VERWEIGERT}: Verkauf-AB per openTRANS nicht möglich – nichts geschrieben")
            ergebnis.warnungen.extend(probleme)
            logger.error("%s (%s): openTRANS-Vorprüfung: %s", _VERWEIGERT, quelldatei.name, "; ".join(probleme))
            _protokollieren(paket, ergebnis, settings)
            return ergebnis

    # 1. Einkauf-Bestellung
    # TODO-LEXWARE: Reihenfolge Einkauf -> Verkauf-AB ist Annahme aus der Spezifikation; am echten System prüfen,
    # ob ein scharf fehlgeschlagener Assistent (Abbruch nach Fehler) wirklich keinen Teilbeleg hinterlässt.
    einkauf_ok = True
    if paket.einkauf_erforderlich:
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
        if not einkauf_ok:
            if effektiv_dry_run:
                ergebnis.warnungen.append(
                    "Dry Run: Einkauf-Bestellung fehlgeschlagen, Verkauf-AB wird trotzdem simuliert "
                    "(scharf würde hier abgebrochen)"
                )
            else:
                ergebnis.warnungen.append(
                    "Einkauf-Bestellung fehlgeschlagen – Verkauf-AB und Lieferschein werden NICHT angelegt "
                    "(keine halbe Buchung)"
                )
    else:
        ergebnis.warnungen.append("Keine Einkauf-Bestellung: für diese Firma nicht vorgesehen (einkauf_erforderlich=False)")

    # 2. Verkauf-AB
    if einkauf_ok or effektiv_dry_run:
        ergebnis.verkauf = _sicher_schreiben(
            lambda: wahl.verkauf.schreibe_verkauf_ab(paket, dry_run=effektiv_dry_run),
            Belegart.AUFTRAGSBESTAETIGUNG,
            effektiv_dry_run,
            paket.summe_netto_vk,
            "Verkauf-AB",
        )

    # 3. Lieferschein
    if paket.lieferschein_erstellen:
        verkauf = ergebnis.verkauf
        if verkauf is None or not verkauf.erfolgreich:
            ergebnis.warnungen.append("Lieferschein nicht erstellt: Verkauf-AB wurde nicht (erfolgreich) angelegt")
        elif not verkauf.belegnummer:
            # TODO-LEXWARE: Nach dem openTRANS-Import vergibt Lexware die AB-Nummer erst beim "Übernehmen" des
            # Webshop-Auftrags; prüfen, ob die Nummer aus der Auftragsliste gelesen und der Lieferschein dann
            # per UI-Automation nachgezogen werden kann (zweiter Lauf mit ab_belegnummer).
            ergebnis.warnungen.append(
                "Lieferschein erst nach Import möglich: AB-Belegnummer liegt noch nicht vor"
                + (" (openTRANS-Webshop-Auftrag muss erst in Lexware übernommen werden)" if verkauf.methode == "opentrans" else "")
            )
        else:
            ab_nummer = verkauf.belegnummer
            ergebnis.lieferschein = _sicher_schreiben(
                lambda: wahl.lieferschein.auftrag_weiterfuehren_lieferschein(paket, ab_nummer, dry_run=effektiv_dry_run),
                Belegart.LIEFERSCHEIN,
                effektiv_dry_run,
                paket.summe_netto_vk,
                "Lieferschein",
            )

    # Konsistenz: meldet ein Teilergebnis Dry Run, obwohl scharf angefordert war, ist nichts gebucht
    teile_alle = [t for t in (ergebnis.einkauf, ergebnis.verkauf, ergebnis.lieferschein) if t is not None]
    teile_alle.extend(ergebnis.einkauf_weitere)
    if not effektiv_dry_run and any(t.dry_run for t in teile_alle):
        ergebnis.warnungen.append(
            "Mindestens ein Teilergebnis meldet Dry Run, obwohl scharf angefordert war – Lauf gilt als Dry Run, "
            "Quelldatei wird nicht archiviert"
        )
        logger.warning("Scharfer Lauf, aber Teilergebnis mit dry_run=True (%s)", quelldatei.name)
        ergebnis.dry_run = True

    logger.info(
        "Buchung %s (%s): %s",
        "ERFOLGREICH" if ergebnis.erfolgreich else "FEHLGESCHLAGEN",
        "Dry Run" if ergebnis.dry_run else "scharf",
        quelldatei.name,
    )

    # 4. Protokoll, 5. Archiv (nur scharf und erfolgreich)
    _protokollieren(paket, ergebnis, settings)
    if ergebnis.erfolgreich and not ergebnis.dry_run:
        _archivieren(paket, ergebnis, settings)
    return ergebnis


def lieferschein_nachziehen(
    paket: Belegpaket,
    ab_belegnummer: str,
    settings: Settings,
    *,
    dry_run: bool = True,
    schreiber: LexwareSchreiber | None = None,
) -> BuchungsErgebnis:
    """Legt nur den Lieferschein zu einer bereits vorhandenen AB an ("Auftrag weiterführen").

    Zweiter Einstieg nach dem openTRANS-Weg: Lexware vergibt die AB-Nummer erst beim Übernehmen des
    Webshop-Auftrags, deshalb kann :func:`buche` den Lieferschein dann nicht anlegen. Vorbedingungen
    wie bei :func:`buche` (Freigabe, nicht blockiert, Firma/Kunde/Positionen, Mandantenschutz);
    ``paket.lieferschein_erstellen`` muss gesetzt sein (Geschäftsregel 10: nur nach Vorlage und
    Freigabe). Es wird protokolliert, aber nie archiviert (die Quelldatei gehört zur Buchung der AB).
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
        firma_id=firma.id, quelldatei=quelldatei, dry_run=effektiv_dry_run, zeitpunkt=zeitpunkt, warnungen=warnungen
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
    logger.info(
        "Lieferschein %s (%s): %s",
        "ERFOLGREICH" if ergebnis.erfolgreich else "FEHLGESCHLAGEN",
        "Dry Run" if ergebnis.dry_run else "scharf",
        quelldatei.name,
    )
    _protokollieren(paket, ergebnis, settings)
    return ergebnis
