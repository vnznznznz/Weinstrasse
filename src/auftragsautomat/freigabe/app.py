"""Freigabe-Oberfläche des Auftrags-Automaten (Streamlit).

Start: ``auftragsautomat freigabe`` (ruft ``streamlit run <diese Datei>``) oder direkt
``streamlit run src/auftragsautomat/freigabe/app.py``. Die App braucht keine Argumente; die
Konfiguration kommt aus ``config/`` des Projektstamms (Umgebungsvariable ``AUFTRAGSAUTOMAT_HOME``).

Aufbau:

* **Seitenleiste**: Dry-Run-Schalter (Standard aus ``settings.dry_run``; scharf nur mit zusätzlicher
  Bestätigung), Eingangsdateien aus ``settings.eingang_ordner`` (rekursiv) mit "Einlesen & prüfen",
  gespeicherte Pakete (``logs/pakete``), Firma-Auswahl (Standard = ermittelte Firma, Wechsel ->
  :func:`~auftragsautomat.matching.pipeline.setze_firma`).
* **Hauptbereich**: Kopf (Kunde/Lieferant mit Kandidaten, Belegnr., Bestellnr., Datum, Liefertag,
  Lieferadresse, Belegfelder), Positionstabelle (Dokumenttext, Zuordnung mit Kandidaten,
  "– überspringen –" und "Artikelnummer eingeben", Menge, EK, VK, Preis laut Dokument, Abweichung
  rot, Status), Summen und Summenvermerke, Hinweise nach Schweregrad, Buttons "Korrigieren / Neu
  prüfen", "Freigeben & Buchen" (gesperrt wenn blockiert), "Alias speichern" und die Checkbox
  "Lieferschein erstellen".

Ausbaustufe 2 (Logik in :mod:`auftragsautomat.freigabe.seiten_stufe2`): Seitennavigation in der
Seitenleiste ("Freigabe" | "Heute" | "Dossiers" | "Rabatte" | "Einstellungen"), "Auftrag hochladen"
(Datei landet im Eingang und wird sofort geprüft), Firmenwahl bei ``FIRMA_UNKLAR`` mit Häkchen
"für diesen Kunden und Lieferanten merken", Info ``FIRMA_GELERNT``, Spalte "Rabatt %" in der
Positionstabelle (manuell -> Quelle "manuell"; RABATT_VORSCHLAG gelb, RABATT_UNSICHER/RABATT_ABWEICHUNG
rot), nach dem Buchen die erzeugten Dateinamen, Ablage-Ergebnisse und die Nachbereitung (Fixtermin,
Entwürfe, Dossier).

Der Zustand liegt in ``st.session_state`` (``paket``) und wird nach jeder Änderung über
:mod:`auftragsautomat.freigabe.zustand` gespeichert. Alle Aktionen sind in Funktionen zerlegt
(:func:`uebernehme_korrekturen`, :func:`freigeben_und_buchen`, :func:`speichere_aliase` …), damit
sie ohne Oberfläche testbar sind. Keine Exception erreicht die Oberfläche: Fehler werden geloggt und
als ``st.error`` angezeigt.
"""

from __future__ import annotations

import getpass
import html
import importlib.util
import logging
import os
import sys
from collections.abc import Callable, Mapping
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

if importlib.util.find_spec("auftragsautomat") is None:  # `streamlit run app.py` ohne installiertes Paket
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import streamlit as st  # noqa: E402

from auftragsautomat.config import Settings, lade_settings, projekt_stamm  # noqa: E402
from auftragsautomat.extraktion.erkennung import extrahiere  # noqa: E402
from auftragsautomat.extraktion.zahlen import parse_dezimal  # noqa: E402
from auftragsautomat.freigabe import seiten_stufe2 as stufe2  # noqa: E402
from auftragsautomat.freigabe import zustand  # noqa: E402
from auftragsautomat.lexware.buchen import buche  # noqa: E402
from auftragsautomat.matching import aliase as alias_modul  # noqa: E402
from auftragsautomat.matching.artikel import ordne_manuell  # noqa: E402
from auftragsautomat.matching.pipeline import erstelle_belegpaket, pruefe_erneut, setze_firma  # noqa: E402
from auftragsautomat.matching.pruefung import WEINGUT_TRENNER  # noqa: E402
from auftragsautomat.models import (  # noqa: E402
    Artikel,
    Belegpaket,
    BuchungsErgebnis,
    KundeZuordnung,
    LieferantZuordnung,
    MatchStatus,
    PositionsZuordnung,
    Pruefhinweis,
    Schweregrad,
)
from auftragsautomat.protokoll import formatiere_betrag, formatiere_menge, zusammenfassung  # noqa: E402
from auftragsautomat.stammdaten import laden  # noqa: E402
from auftragsautomat.stammdaten.basis import Stammdaten  # noqa: E402
from auftragsautomat.watcher import finde_eingangsdateien  # noqa: E402

__all__ = [
    "OPTION_ARTIKELNUMMER",
    "OPTION_KEINE",
    "OPTION_OFFEN",
    "OPTION_UEBERSPRINGEN",
    "KorrekturFehler",
    "benutzername",
    "einlesen_und_pruefen",
    "freigeben_und_buchen",
    "lese_korrekturen",
    "main",
    "speichere_aliase",
    "uebernehme_korrekturen",
]

logger = logging.getLogger(__name__)

# Schlüssel in st.session_state
SS_PAKET = "paket"
SS_PAKET_PFAD = "paket_pfad"
SS_MELDUNGEN = "meldungen"
SS_ERGEBNIS = "buchungs_ergebnis"
SS_ZUSAMMENFASSUNG = "zusammenfassung"
# Widget-Schlüssel, die bei einem neuen Paket zurückgesetzt werden (Präfixe; "firma" deckt auch "firmenwahl_" ab)
_WIDGET_PRAEFIXE = ("kopf_", "bf_", "pos_", "lieferschein", "firma")

# Werte der Auswahllisten (stabil, unabhängig von der Anzeige)
OPTION_KEINE = ""
OPTION_OFFEN = "__offen__"
OPTION_UEBERSPRINGEN = "__ueberspringen__"
OPTION_ARTIKELNUMMER = "__artikelnummer__"

_STATUS_TEXT = {
    MatchStatus.SICHER: ("OK", "#1a7f37"),
    MatchStatus.MANUELL: ("OK (manuell)", "#1a7f37"),
    MatchStatus.UNSICHER: ("? unsicher", "#b35c00"),
    MatchStatus.KEIN_TREFFER: ("!! kein Treffer", "#c00000"),
    MatchStatus.UEBERSPRUNGEN: ("übersprungen", "#6b7280"),
}
_ROT = "#c00000"
_BELEGFELDER = ("spedition", "weingut", "shipment", "kunde")


class KorrekturFehler(ValueError):
    """Eine Korrektur des Anwenders ist nicht anwendbar (unbekannte Artikel-/Kundennummer, Menge)."""


# ---------------------------------------------------------------------------
# Hilfen ohne Oberfläche (testbar)
# ---------------------------------------------------------------------------


def konfiguriere_logging() -> None:
    """Log-Ausgabe des Pakets auf stderr (einmalig, auch bei wiederholter Ausführung des Skripts)."""
    paket_logger = logging.getLogger("auftragsautomat")
    if any(getattr(h, "_freigabe_app", False) for h in paket_logger.handlers):
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler._freigabe_app = True  # type: ignore[attr-defined]
    paket_logger.addHandler(handler)
    if paket_logger.level == logging.NOTSET:
        paket_logger.setLevel(logging.INFO)


def lade_konfiguration() -> Settings:
    """Settings aus ``config/`` des Projektstamms (``AUFTRAGSAUTOMAT_HOME`` oder Repo)."""
    settings = lade_settings()
    logger.debug("Konfiguration aus %s geladen (%d Firmen)", projekt_stamm(), len(settings.firmen))
    return settings


def benutzername() -> str:
    """Angemeldeter Benutzer (Windows-Benutzername) für ``freigegeben_von``."""
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001 - getuser kann ohne Benutzerdatenbank scheitern
        return os.environ.get("USERNAME") or os.environ.get("USER") or "unbekannt"


def lade_stammdaten_sicher(settings: Settings, firma_id: str | None) -> Stammdaten | None:
    """Stammdaten der Firma (Cache); None ohne Firma oder wenn sie nicht ladbar sind (Fehler geloggt)."""
    if not firma_id or firma_id not in settings.firmen:
        return None
    try:
        return laden.lade_stammdaten(settings.firma(firma_id))
    except Exception as exc:  # noqa: BLE001 - UI darf nicht abstürzen; Ursache wird angezeigt
        logger.exception("Stammdaten für %s nicht ladbar", firma_id)
        meldung("error", f"Stammdaten für Firma {firma_id} nicht ladbar: {type(exc).__name__}: {exc}")
        return None


def einlesen_und_pruefen(datei: Path, settings: Settings, *, firma_id: str | None = None) -> Belegpaket:
    """Datei extrahieren und das geprüfte Belegpaket erstellen (Firma optional erzwingen)."""
    roh = extrahiere(Path(datei), settings)
    return erstelle_belegpaket(roh, settings, firma_id=firma_id)


def _als_decimal(wert: object, laufnr: int) -> Decimal | None:
    if wert is None or wert == "":
        return None
    try:
        if isinstance(wert, Decimal):
            ergebnis = wert
        elif isinstance(wert, bool):
            raise InvalidOperation
        elif isinstance(wert, int):
            ergebnis = Decimal(wert)
        elif isinstance(wert, float):
            ergebnis = Decimal(str(wert))
        else:
            ergebnis = parse_dezimal(str(wert))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise KorrekturFehler(f"Position {laufnr}: Menge '{wert}' ist keine Zahl") from exc
    if ergebnis is None:
        raise KorrekturFehler(f"Position {laufnr}: Menge '{wert}' ist keine Zahl")
    if ergebnis < 0:
        raise KorrekturFehler(f"Position {laufnr}: Menge darf nicht negativ sein ({ergebnis})")
    return ergebnis


def _text_oder_none(wert: object) -> str | None:
    if wert is None:
        return None
    text = str(wert).strip()
    return text or None


def _artikel_fuer(position: PositionsZuordnung, nummer: str, stammdaten: Stammdaten | None) -> Artikel:
    for k in position.kandidaten:
        if k.artikel.artikelnummer == nummer:
            return k.artikel
    if stammdaten is None:
        raise KorrekturFehler(f"Position {position.roh.laufnr}: ohne Firma/Stammdaten kann kein Artikel gewählt werden")
    artikel = stammdaten.artikel_nach_nummer.get(nummer)
    if artikel is None:
        raise KorrekturFehler(f"Position {position.roh.laufnr}: Artikelnummer '{nummer}' nicht im Stamm der Firma {stammdaten.firma_id}")
    return artikel


def _ueberspringe(position: PositionsZuordnung) -> None:
    if position.uebersprungen:
        return
    logger.info("Position %s '%s' manuell übersprungen", position.roh.laufnr, position.roh.bezeichnung)
    position.artikel = None
    position.status = MatchStatus.UEBERSPRUNGEN
    position.quelle = "manuell"
    position.ek_preis = None
    position.vk_preis = None
    position.hinweise = [h for h in position.hinweise if h.schweregrad != Schweregrad.FEHLER]


def _korrigiere_position(
    position: PositionsZuordnung, werte: Mapping[str, Any], stammdaten: Stammdaten | None
) -> PositionsZuordnung:
    laufnr = position.roh.laufnr
    menge = _als_decimal(werte.get("menge"), laufnr)
    zuordnung = werte.get("zuordnung")
    if zuordnung == OPTION_UEBERSPRINGEN:
        _ueberspringe(position)
        return position
    if zuordnung == OPTION_ARTIKELNUMMER:
        nummer = _text_oder_none(werte.get("artikelnummer"))
        if nummer is None:
            raise KorrekturFehler(f"Position {laufnr}: bitte eine Artikelnummer eingeben")
        return ordne_manuell(position, _artikel_fuer(position, nummer, stammdaten), menge=menge)
    if zuordnung and zuordnung != OPTION_OFFEN:
        aktuell = position.artikel.artikelnummer if position.artikel is not None else None
        if aktuell != zuordnung or position.uebersprungen:
            return ordne_manuell(position, _artikel_fuer(position, zuordnung, stammdaten), menge=menge)
    if menge is not None and menge != position.menge:
        logger.info("Position %s: Menge %s -> %s (Anwender)", laufnr, position.menge, menge)
        position.menge = menge
    if "rabatt" in werte and not position.uebersprungen:
        rabatt = _als_decimal(werte.get("rabatt"), laufnr)
        try:
            stufe2.setze_rabatt_manuell(position, rabatt)
        except ValueError as exc:
            raise KorrekturFehler(str(exc)) from exc
    return position


def uebernehme_korrekturen(
    paket: Belegpaket,
    werte: Mapping[str, Any],
    settings: Settings,
    stammdaten: Stammdaten | None = None,
) -> Belegpaket:
    """Überträgt Korrekturen des Anwenders ins Paket und prüft erneut (``pipeline.pruefe_erneut``).

    ``werte`` (alle Schlüssel optional): ``kunde``/``lieferant`` (Nummer oder "" = keiner),
    ``belegnummer``, ``bestellnummer``, ``lieferadresse`` (Text), ``datum``, ``liefertag`` (date |
    None), ``belegfelder`` (dict spedition/weingut/shipment/kunde), ``positionen`` ({laufnr:
    {"zuordnung": Artikelnummer | OPTION_*, "artikelnummer": str, "menge": Zahl, "rabatt": Prozent}}),
    ``lieferschein`` (bool). Ein geänderter Rabatt wird mit Quelle "manuell" übernommen (0 = kein
    Rabatt; Aktionen schlagen dann nichts mehr vor). Nicht anwendbare Korrekturen lösen
    :class:`KorrekturFehler` aus (das Paket bleibt dann unverändert, weil auf einer Kopie gearbeitet
    wird). Die Freigabe wird zurückgesetzt.
    """
    neu = paket.model_copy(deep=True)
    roh = neu.roh
    if stammdaten is None:
        stammdaten = lade_stammdaten_sicher(settings, neu.firma_id)

    for feld in ("belegnummer", "bestellnummer", "lieferadresse"):
        if feld in werte:
            setattr(roh, feld, _text_oder_none(werte[feld]))
    for feld in ("datum", "liefertag"):
        if feld in werte:
            wert = werte[feld]
            if wert is not None and not isinstance(wert, date):
                raise KorrekturFehler(f"{feld}: '{wert}' ist kein Datum")
            setattr(roh, feld, wert)

    belegfelder = werte.get("belegfelder")
    if isinstance(belegfelder, Mapping):
        for feld in _BELEGFELDER:
            if feld in belegfelder:
                setattr(neu.belegfelder, feld, _text_oder_none(belegfelder[feld]))

    if "kunde" in werte:
        gewuenscht = _text_oder_none(werte["kunde"]) or OPTION_KEINE
        aktuell = neu.kunde.kunde.kundennummer if neu.kunde.kunde is not None else OPTION_KEINE
        if gewuenscht != aktuell:
            alter_name = neu.kunde.kunde.name if neu.kunde.kunde is not None else None
            if gewuenscht == OPTION_KEINE:
                # Status MANUELL ohne Kunde: die Prüfung meldet KEIN_KUNDE, pruefe_erneut matcht nicht erneut
                neu.kunde = KundeZuordnung(kandidaten=neu.kunde.kandidaten, status=MatchStatus.MANUELL, quelle="manuell")
                logger.info("Kunde vom Anwender entfernt")
            else:
                kunde = stammdaten.kunden_nach_nummer.get(gewuenscht) if stammdaten is not None else None
                if kunde is None:
                    raise KorrekturFehler(f"Kundennummer '{gewuenscht}' nicht im Stamm der Firma {neu.firma_id or '?'}")
                neu.kunde = KundeZuordnung(
                    kunde=kunde, kandidaten=neu.kunde.kandidaten, status=MatchStatus.MANUELL, quelle="manuell"
                )
                if neu.belegfelder.kunde in (None, alter_name):
                    neu.belegfelder.kunde = kunde.name
                logger.info("Kunde manuell gesetzt: %s %s", kunde.kundennummer, kunde.name)

    if "lieferant" in werte:
        gewuenscht = _text_oder_none(werte["lieferant"]) or OPTION_KEINE
        aktuell = neu.lieferant.lieferant.lieferantennummer if neu.lieferant.lieferant is not None else OPTION_KEINE
        if gewuenscht != aktuell:
            alter_name = neu.lieferant.lieferant.name if neu.lieferant.lieferant is not None else None
            if gewuenscht == OPTION_KEINE:
                neu.lieferant = LieferantZuordnung(
                    kandidaten=neu.lieferant.kandidaten, status=MatchStatus.MANUELL, quelle="manuell"
                )
                logger.info("Lieferant vom Anwender entfernt")
            else:
                lieferant = stammdaten.lieferanten_nach_nummer.get(gewuenscht) if stammdaten is not None else None
                if lieferant is None:
                    raise KorrekturFehler(
                        f"Lieferantennummer '{gewuenscht}' nicht im Stamm der Firma {neu.firma_id or '?'}"
                    )
                neu.lieferant = LieferantZuordnung(
                    lieferant=lieferant,
                    kandidaten=neu.lieferant.kandidaten,
                    status=MatchStatus.MANUELL,
                    quelle="manuell",
                )
                weingut = neu.belegfelder.weingut
                vorbelegt = weingut in (None, alter_name) or (
                    bool(alter_name) and bool(weingut) and str(weingut).startswith(f"{alter_name}{WEINGUT_TRENNER}")
                )
                if vorbelegt:
                    # Vorbelegung verwerfen; pruefe_erneut belegt mit dem neuen Weingut (ggf. plus weitere) vor
                    neu.belegfelder.weingut = None
                logger.info("Lieferant manuell gesetzt: %s %s", lieferant.lieferantennummer, lieferant.name)

    positionen = werte.get("positionen")
    if isinstance(positionen, Mapping):
        nach_laufnr = {p.roh.laufnr: i for i, p in enumerate(neu.positionen)}
        for laufnr_roh, pw in positionen.items():
            try:
                laufnr = int(laufnr_roh)
            except (TypeError, ValueError) as exc:
                raise KorrekturFehler(f"Ungültige Positionsnummer '{laufnr_roh}'") from exc
            index = nach_laufnr.get(laufnr)
            if index is None:
                raise KorrekturFehler(f"Position {laufnr} gibt es nicht im Paket")
            neu.positionen[index] = _korrigiere_position(neu.positionen[index], pw or {}, stammdaten)

    if "lieferschein" in werte:
        neu.lieferschein_erstellen = bool(werte["lieferschein"])

    neu.freigegeben = False
    neu.freigegeben_am = None
    neu.freigegeben_von = None
    return pruefe_erneut(neu, settings, stammdaten=stammdaten)


def freigeben_und_buchen(
    paket: Belegpaket, settings: Settings, *, dry_run: bool
) -> tuple[BuchungsErgebnis, str]:
    """Paket freigeben (Zeitpunkt, Benutzer) und buchen; liefert Ergebnis und Zusammenfassung."""
    paket.freigegeben = True
    paket.freigegeben_am = datetime.now()
    paket.freigegeben_von = benutzername()
    logger.info(
        "Freigabe durch %s (%s) für %s", paket.freigegeben_von, "Dry Run" if dry_run else "SCHARF", paket.roh.quelldatei
    )
    ergebnis = buche(paket, settings, dry_run=dry_run)
    return ergebnis, zusammenfassung(paket, ergebnis)


def speichere_aliase(paket: Belegpaket, settings: Settings) -> list[str]:
    """Speichert Aliase für manuell zugeordnete Kunden, Lieferanten und Artikel; liefert Meldungen."""
    meldungen: list[str] = []
    firma_id = paket.firma_id
    if not firma_id:
        return ["Kein Alias gespeichert: dem Paket ist keine Firma zugeordnet."]
    ordner = Path(settings.alias_ordner)
    roh = paket.roh

    def _speichere(beschreibung: str, aktion: Callable[[], Path]) -> None:
        try:
            pfad = aktion()
        except alias_modul.AliasKonflikt as exc:
            meldungen.append(f"Konflikt bei {beschreibung}: {exc}")
        except alias_modul.AliasFehler as exc:
            meldungen.append(f"Alias für {beschreibung} nicht gespeichert: {exc}")
        else:
            meldungen.append(f"Alias gespeichert: {beschreibung} ({Path(pfad).name})")

    kunde = paket.kunde.kunde
    if kunde is not None and paket.kunde.status == MatchStatus.MANUELL and roh.kunde_text:
        _speichere(
            f"Kunde '{roh.kunde_text}' -> {kunde.kundennummer} {kunde.name}",
            lambda: alias_modul.speichere_alias_kunde(ordner, kunde.kundennummer, firma_id, roh.kunde_text, anzeigename=kunde.name),
        )
    lieferant = paket.lieferant.lieferant
    if lieferant is not None and paket.lieferant.status == MatchStatus.MANUELL and roh.lieferant_text:
        _speichere(
            f"Lieferant '{roh.lieferant_text}' -> {lieferant.lieferantennummer} {lieferant.name}",
            lambda: alias_modul.speichere_alias_lieferant(
                ordner, lieferant.lieferantennummer, firma_id, roh.lieferant_text, anzeigename=lieferant.name
            ),
        )
    for position in paket.positionen:
        artikel = position.artikel
        if artikel is None or position.status != MatchStatus.MANUELL:
            continue
        bezeichnung = position.roh.bezeichnung.strip()
        if not bezeichnung:
            continue
        _speichere(
            f"Artikel '{bezeichnung}' -> {artikel.artikelnummer}",
            lambda a=artikel, b=bezeichnung: alias_modul.speichere_alias_artikel(ordner, a.artikelnummer, firma_id, b),
        )
    if not meldungen:
        meldungen.append("Nichts zu speichern: keine manuell zugeordneten Kunden, Lieferanten oder Artikel.")
    return meldungen


# ---------------------------------------------------------------------------
# Session-State
# ---------------------------------------------------------------------------


def initialisiere_zustand() -> None:
    st.session_state.setdefault(SS_PAKET, None)
    st.session_state.setdefault(SS_PAKET_PFAD, None)
    st.session_state.setdefault(SS_MELDUNGEN, [])


def meldung(art: str, text: str) -> None:
    """Meldung für die nächste Darstellung vormerken (überlebt ``st.rerun``)."""
    st.session_state.setdefault(SS_MELDUNGEN, []).append({"art": art, "text": text})


def zeige_meldungen() -> None:
    meldungen = st.session_state.get(SS_MELDUNGEN) or []
    for m in meldungen:
        art, text = m.get("art", "info"), m.get("text", "")
        if art == "success":
            st.success(text)
        elif art == "error":
            st.error(text)
        elif art == "warning":
            st.warning(text)
        else:
            st.info(text)
    st.session_state[SS_MELDUNGEN] = []


def widgets_zuruecksetzen() -> None:
    """Widget-Zustände des Pakets verwerfen, damit die Oberfläche die Paketwerte neu zeigt."""
    for schluessel in list(st.session_state.keys()):
        if isinstance(schluessel, str) and schluessel.startswith(_WIDGET_PRAEFIXE):
            st.session_state.pop(schluessel, None)


def setze_paket(paket: Belegpaket | None, pfad: Path | None = None) -> None:
    st.session_state[SS_PAKET] = paket
    st.session_state[SS_PAKET_PFAD] = pfad
    st.session_state.pop(SS_ERGEBNIS, None)
    st.session_state.pop(SS_ZUSAMMENFASSUNG, None)
    widgets_zuruecksetzen()


def speichere_zustand(paket: Belegpaket, settings: Settings) -> Path | None:
    try:
        pfad = zustand.speichere_paket(paket, zustand.paket_ordner(settings))
    except OSError as exc:
        logger.exception("Paket konnte nicht gespeichert werden")
        meldung("warning", f"Paket konnte nicht gespeichert werden: {exc}")
        return None
    st.session_state[SS_PAKET_PFAD] = pfad
    return pfad


# ---------------------------------------------------------------------------
# Auswahllisten
# ---------------------------------------------------------------------------


def kunden_optionen(paket: Belegpaket, stammdaten: Stammdaten | None) -> tuple[list[str], dict[str, str]]:
    werte = [OPTION_KEINE]
    labels = {OPTION_KEINE: "– kein Kunde –"}
    for k in paket.kunde.kandidaten:
        nr = k.kunde.kundennummer
        if nr not in labels:
            werte.append(nr)
            labels[nr] = f"{nr} {k.kunde.name}{' (' + k.kunde.ort + ')' if k.kunde.ort else ''} – Kandidat {k.score:.0f}"
    aktuell = paket.kunde.kunde
    if aktuell is not None and aktuell.kundennummer not in labels:
        werte.append(aktuell.kundennummer)
        labels[aktuell.kundennummer] = f"{aktuell.kundennummer} {aktuell.name}"
    if stammdaten is not None:
        for kunde in sorted(stammdaten.kunden, key=lambda k: (k.name.lower(), k.kundennummer)):
            if kunde.kundennummer not in labels:
                werte.append(kunde.kundennummer)
                labels[kunde.kundennummer] = f"{kunde.kundennummer} {kunde.name}{' (' + kunde.ort + ')' if kunde.ort else ''}"
    return werte, labels


def lieferanten_optionen(paket: Belegpaket, stammdaten: Stammdaten | None) -> tuple[list[str], dict[str, str]]:
    werte = [OPTION_KEINE]
    labels = {OPTION_KEINE: "– kein Lieferant –"}
    for k in paket.lieferant.kandidaten:
        nr = k.lieferant.lieferantennummer
        if nr not in labels:
            werte.append(nr)
            labels[nr] = f"{nr} {k.lieferant.name} – Kandidat {k.score:.0f}"
    aktuell = paket.lieferant.lieferant
    if aktuell is not None and aktuell.lieferantennummer not in labels:
        werte.append(aktuell.lieferantennummer)
        labels[aktuell.lieferantennummer] = f"{aktuell.lieferantennummer} {aktuell.name}"
    if stammdaten is not None:
        for lf in sorted(stammdaten.lieferanten, key=lambda x: (x.name.lower(), x.lieferantennummer)):
            if lf.lieferantennummer not in labels:
                werte.append(lf.lieferantennummer)
                labels[lf.lieferantennummer] = f"{lf.lieferantennummer} {lf.name}"
    return werte, labels


def artikel_optionen(position: PositionsZuordnung) -> tuple[list[str], dict[str, str], str]:
    """Werte, Anzeigetexte und Vorauswahl für die Zuordnungsliste einer Position."""
    werte = [OPTION_OFFEN, OPTION_UEBERSPRINGEN, OPTION_ARTIKELNUMMER]
    labels = {
        OPTION_OFFEN: "– offen (bitte zuordnen) –",
        OPTION_UEBERSPRINGEN: "– überspringen –",
        OPTION_ARTIKELNUMMER: "Artikelnummer eingeben …",
    }
    for k in position.kandidaten:
        nr = k.artikel.artikelnummer
        if nr in labels:
            continue
        zusatz = ""
        if k.manuell_erforderlich or k.artikel.ist_sondervariante:
            zusatz += " [Sondervariante]"
        if not k.artikel.aktiv:
            zusatz += " [inaktiv]"
        werte.append(nr)
        labels[nr] = f"{nr} {k.artikel.bezeichnung} – {k.score:.0f}{zusatz}"
    artikel = position.artikel
    if artikel is not None and artikel.artikelnummer not in labels:
        werte.append(artikel.artikelnummer)
        labels[artikel.artikelnummer] = f"{artikel.artikelnummer} {artikel.bezeichnung}"
    if position.uebersprungen:
        vorauswahl = OPTION_UEBERSPRINGEN
    elif artikel is not None:
        vorauswahl = artikel.artikelnummer
    else:
        vorauswahl = OPTION_OFFEN
    return werte, labels, vorauswahl


def firmen_optionen(settings: Settings) -> tuple[list[str], dict[str, str]]:
    werte = [OPTION_KEINE, *settings.firmen]
    labels = {OPTION_KEINE: "– automatisch ermitteln –"}
    for firma_id, firma in settings.firmen.items():
        labels[firma_id] = f"{firma_id} – {firma.name}"
    return werte, labels


def _index(werte: list[str], wert: str | None) -> int:
    try:
        return werte.index(wert if wert is not None else OPTION_KEINE)
    except ValueError:
        return 0


# ---------------------------------------------------------------------------
# Seitenleiste
# ---------------------------------------------------------------------------


def _einlesen(settings: Settings, relativ: str | None) -> None:
    if not relativ:
        meldung("warning", "Bitte zuerst eine Eingangsdatei auswählen.")
        return
    datei = Path(settings.eingang_ordner) / relativ
    # Eine in der Seitenleiste gewählte Firma gilt nur als Vorgabe, solange kein Paket geladen ist;
    # sonst wird die Firma der neuen Datei automatisch ermittelt.
    firma_id = (st.session_state.get("firma") or None) if st.session_state.get(SS_PAKET) is None else None
    try:
        with st.spinner(f"Lese {datei.name} …"):
            paket = einlesen_und_pruefen(datei, settings, firma_id=firma_id)
    except Exception as exc:  # noqa: BLE001 - jede Ursache wird angezeigt, die App läuft weiter
        logger.exception("Einlesen von %s fehlgeschlagen", datei)
        meldung("error", f"Einlesen von {datei.name} fehlgeschlagen: {type(exc).__name__}: {exc}")
        return
    setze_paket(paket)
    pfad = speichere_zustand(paket, settings)
    status = "BLOCKIERT – bitte korrigieren" if paket.blockiert else "freigabefähig"
    meldung("success", f"{datei.name} eingelesen und geprüft: Firma {paket.firma_id or '?'}, {len(paket.positionen)} Positionen, {status}.")
    if pfad is not None:
        meldung("info", f"Paket gespeichert: {pfad}")


def _paket_laden(pfad_text: str | None) -> None:
    if not pfad_text:
        meldung("warning", "Bitte ein gespeichertes Paket auswählen.")
        return
    pfad = Path(pfad_text)
    try:
        paket = zustand.lade_paket(pfad)
    except (FileNotFoundError, zustand.PaketFehler) as exc:
        logger.error("Paket %s nicht ladbar: %s", pfad, exc)
        meldung("error", str(exc))
        return
    setze_paket(paket, pfad)
    meldung("success", f"Paket {pfad.name} geladen.")


def _paket_loeschen(pfad_text: str | None) -> None:
    if not pfad_text:
        meldung("warning", "Bitte ein gespeichertes Paket auswählen.")
        return
    pfad = Path(pfad_text)
    try:
        geloescht = zustand.loesche_paket(pfad)
    except OSError as exc:
        meldung("error", f"Paket konnte nicht gelöscht werden: {exc}")
        return
    if st.session_state.get(SS_PAKET_PFAD) == pfad:
        setze_paket(None)
    meldung("info", f"Paket {pfad.name} gelöscht." if geloescht else f"Paket {pfad.name} war schon weg.")


def _firma_wechseln(settings: Settings, paket: Belegpaket, firma_id: str) -> None:
    try:
        with st.spinner(f"Ordne Firma {firma_id} zu und matche neu …"):
            neu = setze_firma(paket, firma_id, settings)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Firmenwechsel auf %s fehlgeschlagen", firma_id)
        meldung("error", f"Firma konnte nicht auf {firma_id} gesetzt werden: {type(exc).__name__}: {exc}")
        st.session_state.pop("firma", None)
        return
    setze_paket(neu)
    speichere_zustand(neu, settings)
    meldung("success", f"Firma auf {firma_id} gesetzt; Paket neu zugeordnet und geprüft.")


def ui_kontext(settings: Settings) -> stufe2.UiKontext:
    """Rückrufe der App für die Seiten der Ausbaustufe 2."""
    return stufe2.UiKontext(
        melde=meldung,
        setze_paket=setze_paket,
        speichere=lambda p: speichere_zustand(p, settings),
        benutzer=benutzername,
        dry_run=lambda: bool(st.session_state.get("dry_run", settings.dry_run)),
    )


def aktuelle_seite() -> str:
    """Gewählte Seite der Seitenleiste (Standard "Freigabe")."""
    seite = st.session_state.get(stufe2.SS_SEITE)
    return seite if seite in stufe2.SEITEN else stufe2.SEITEN[0]


def zeige_seitenleiste(settings: Settings) -> bool:
    """Seitenleiste zeichnen; True, wenn eine Aktion das Paket geändert hat (dann ``st.rerun``)."""
    geaendert = False
    kontext = ui_kontext(settings)
    with st.sidebar:
        st.header("Auftrags-Automat")
        st.caption(f"Projektstamm: {projekt_stamm()}")
        st.radio("Seite", stufe2.SEITEN, key=stufe2.SS_SEITE, horizontal=True)
        nur_freigabe = aktuelle_seite() == stufe2.SEITEN[0]

        st.subheader("Modus")
        dry_run = st.toggle("Dry Run (nur simulieren)", value=settings.dry_run, key="dry_run")
        if dry_run:
            st.caption("Es wird nichts in Lexware gespeichert; openTRANS-XML landet im Ausgabeordner.")
        else:
            st.warning("SCHARF: Belege werden in Lexware angelegt.")
            st.checkbox("Ich habe den Dry Run geprüft", key="scharf_bestaetigt")
            if settings.dry_run:
                st.caption("Hinweis: settings.dry_run ist aktiv – die Buchung läuft trotzdem als Dry Run.")
        if not nur_freigabe:
            pfad = st.session_state.get(SS_PAKET_PFAD)
            if pfad:
                st.caption(f"Aktuelles Paket: {Path(pfad).name}")
            return False

        st.subheader("Eingang")
        eingang = Path(settings.eingang_ordner)
        dateien = finde_eingangsdateien(eingang)
        relative = [p.relative_to(eingang).as_posix() for p in dateien]
        if relative:
            auswahl = st.selectbox("Eingangsdatei", relative, index=None, key="eingangsdatei", placeholder="Datei wählen …")
        else:
            auswahl = None
            st.info(f"Keine Dateien in {eingang}.")
        if st.button("Einlesen & prüfen", key="einlesen", type="primary", width="stretch"):
            _einlesen(settings, auswahl)
            geaendert = True

        if not geaendert and stufe2.zeige_upload(settings, kontext):
            geaendert = True

        st.subheader("Gespeicherte Pakete")
        pakete = zustand.liste_pakete(zustand.paket_ordner(settings))
        if pakete:
            infos = {str(p): zustand.beschreibe_paket(p).anzeige for p in pakete}
            gewaehlt = st.selectbox(
                "Paket", list(infos), index=None, key="gespeichertes_paket", format_func=lambda v: infos.get(v, v), placeholder="Paket wählen …"
            )
            spalte_a, spalte_b = st.columns(2)
            if spalte_a.button("Paket laden", key="paket_laden", width="stretch"):
                _paket_laden(gewaehlt)
                geaendert = True
            if spalte_b.button("Paket löschen", key="paket_loeschen", width="stretch"):
                _paket_loeschen(gewaehlt)
                geaendert = True
        else:
            st.caption("Noch keine gespeicherten Pakete.")

        st.subheader("Firma")
        paket: Belegpaket | None = st.session_state.get(SS_PAKET)
        werte, labels = firmen_optionen(settings)
        if not geaendert:
            wahl = st.selectbox(
                "Firma (Lexware-Mandant)",
                werte,
                index=_index(werte, paket.firma_id if paket is not None else OPTION_KEINE),
                key="firma",
                format_func=lambda v: labels.get(v, v),
            )
            if paket is not None and wahl and wahl != paket.firma_id:
                _firma_wechseln(settings, paket, wahl)
                geaendert = True
            elif paket is not None and not wahl and paket.firma_id:
                st.caption("Die Firma eines geladenen Pakets kann nur gewechselt, nicht entfernt werden.")
        pfad = st.session_state.get(SS_PAKET_PFAD)
        if pfad:
            st.caption(f"Aktuelles Paket: {Path(pfad).name}")
    return geaendert


# ---------------------------------------------------------------------------
# Hauptbereich
# ---------------------------------------------------------------------------


def zeige_kopf(paket: Belegpaket, settings: Settings, stammdaten: Stammdaten | None) -> None:
    roh = paket.roh
    st.subheader(f"Beleg: {Path(roh.quelldatei).name}")
    firma_text = paket.firma_id or "– unbekannt –"
    st.markdown(
        f"**Firma:** {firma_text} (Quelle: {paket.firma_quelle or '-'}) · "
        f"**Eingangstyp:** {roh.eingangstyp.value} ({roh.extraktionsmethode.value}) · "
        f"**Einkauf-Bestellung:** {'ja' if paket.einkauf_erforderlich else 'nein'}"
    )
    if paket.firma_id and stammdaten is None:
        st.warning(f"Stammdaten der Firma {paket.firma_id} sind nicht geladen – Zuordnungen können nicht geändert werden.")
    if stufe2.zeige_firmenwahl(paket, settings, ui_kontext(settings)):
        st.rerun()

    links, rechts = st.columns(2)
    with links:
        werte, labels = kunden_optionen(paket, stammdaten)
        aktuell = paket.kunde.kunde.kundennummer if paket.kunde.kunde else OPTION_KEINE
        st.selectbox(
            f"Kunde (Dokument: {roh.kunde_text or '-'}; Status {paket.kunde.status.value})",
            werte,
            index=_index(werte, aktuell),
            key="kopf_kunde",
            format_func=lambda v: labels.get(v, v),
        )
        werte_l, labels_l = lieferanten_optionen(paket, stammdaten)
        aktuell_l = paket.lieferant.lieferant.lieferantennummer if paket.lieferant.lieferant else OPTION_KEINE
        st.selectbox(
            f"Lieferant / Weingut (Dokument: {roh.lieferant_text or '-'}; Status {paket.lieferant.status.value})",
            werte_l,
            index=_index(werte_l, aktuell_l),
            key="kopf_lieferant",
            format_func=lambda v: labels_l.get(v, v),
        )
        st.text_input("Belegnummer (Dokument)", value=roh.belegnummer or "", key="kopf_belegnummer")
        st.text_input("Bestellnummer (Kunde)", value=roh.bestellnummer or "", key="kopf_bestellnummer")
    with rechts:
        st.date_input("Datum", value=roh.datum, key="kopf_datum", format="DD.MM.YYYY")
        st.date_input("Liefertag", value=roh.liefertag, key="kopf_liefertag", format="DD.MM.YYYY")
        st.text_area("Lieferadresse", value=roh.lieferadresse or "", key="kopf_lieferadresse", height=100)
        if roh.kundennummer_dokument:
            st.caption(f"Kundennummer laut Dokument: {roh.kundennummer_dokument}")

    st.markdown("**Belegfelder (Lexware: Spedition, Weingut, Shipment, Kunde)**")
    spalten = st.columns(4)
    for spalte, feld in zip(spalten, _BELEGFELDER, strict=True):
        with spalte:
            st.text_input(feld.capitalize(), value=getattr(paket.belegfelder, feld) or "", key=f"bf_{feld}")
    if roh.rohtext:
        with st.expander("Rohtext des Dokuments"):
            st.text(roh.rohtext)


def _dokumenttext(p: PositionsZuordnung) -> str:
    roh = p.roh
    teile = []
    if roh.jahrgang:
        teile.append(f"Jg. {roh.jahrgang}")
    if roh.menge is not None:
        teile.append(f"{formatiere_menge(roh.menge)} {roh.einheit or ''}".strip())
    if roh.preis is not None:
        teile.append(f"{formatiere_betrag(roh.preis)} €")
    if roh.volumen_cl is not None:
        teile.append(f"{formatiere_menge(roh.volumen_cl)} cl")
    if roh.artikelnummer:
        teile.append(f"Art.-Nr. {roh.artikelnummer}")
    if roh.hinweis:
        teile.append(f"Hinweis: {roh.hinweis}")
    if roh.durchgestrichen:
        teile.append("durchgestrichen")
    text = f"**{roh.laufnr}.** {html.escape(roh.bezeichnung)}"
    if teile:
        text += f"  \n<small>{html.escape(' · '.join(teile))}</small>"
    if p.artikel is not None:
        text += f"  \n<small>→ {html.escape(p.artikel.artikelnummer)} {html.escape(p.artikel.bezeichnung)}</small>"
    return text


def _abweichung_html(p: PositionsZuordnung) -> str:
    abweichung = p.preis_abweichung
    if p.uebersprungen or abweichung is None:
        return "–"
    if abweichung == 0:
        return "0,00"
    vorzeichen = "+" if abweichung > 0 else ""
    return f"<span style='color:{_ROT};font-weight:600'>{vorzeichen}{formatiere_betrag(abweichung)}</span>"


def _status_html(p: PositionsZuordnung) -> str:
    text, farbe = _STATUS_TEXT.get(p.status, (p.status.value, "#000"))
    fehler = any(h.schweregrad == Schweregrad.FEHLER for h in p.hinweise)
    if fehler and not p.uebersprungen:
        farbe = _ROT
        text += " (Fehler)"
    return f"<span style='color:{farbe};font-weight:600'>{text}</span>"


def _menge_widget(p: PositionsZuordnung) -> None:
    menge = p.menge if p.menge is not None else Decimal(0)
    schluessel = f"pos_{p.roh.laufnr}_menge"
    if menge == menge.to_integral_value():
        st.number_input("Menge", min_value=0, value=int(menge), step=1, key=schluessel, label_visibility="collapsed")
    else:
        st.number_input(
            "Menge", min_value=0.0, value=float(menge), step=0.01, format="%.2f", key=schluessel, label_visibility="collapsed"
        )


def _rabatt_widget(p: PositionsZuordnung) -> None:
    """Spalte "Rabatt %": editierbar (Quelle als Tooltip), Hinweise RABATT_* farbig darunter."""
    schluessel = f"pos_{p.roh.laufnr}_rabatt"
    wert = float(p.rabatt_prozent) if p.rabatt_prozent is not None else 0.0
    st.number_input(
        "Rabatt %",
        min_value=0.0,
        max_value=100.0,
        value=wert,
        step=0.5,
        format="%.2f",
        key=schluessel,
        label_visibility="collapsed",
        help=stufe2.rabatt_tooltip(p),
        disabled=p.uebersprungen,
    )
    farbe = stufe2.rabatt_farbe(p)
    if farbe:
        for h in stufe2.rabatt_hinweise(p):
            st.markdown(f"<small style='color:{farbe}'>[{h.code.value}] {html.escape(h.text)}</small>", unsafe_allow_html=True)
    elif p.rabatt_quelle:
        st.markdown(f"<small style='color:#6b7280'>{html.escape(stufe2.rabatt_tooltip(p).splitlines()[0])}</small>", unsafe_allow_html=True)


def zeige_positionen(paket: Belegpaket, stammdaten: Stammdaten | None) -> None:
    st.subheader(f"Positionen ({len(paket.positionen)})")
    if not paket.positionen:
        st.warning("Keine Positionen erkannt.")
        return
    breiten = [3.0, 3.0, 1.0, 0.8, 0.8, 1.1, 0.9, 0.9, 1.2]
    kopf = st.columns(breiten)
    for spalte, titel in zip(kopf, ("Dokument", "Zuordnung (Lexware)", "Menge", "EK", "VK", "Rabatt %", "Preis Dok.", "Abweichung", "Status"), strict=True):
        spalte.markdown(f"**{titel}**")
    for p in paket.positionen:
        laufnr = p.roh.laufnr
        zeile = st.columns(breiten)
        zeile[0].markdown(_dokumenttext(p), unsafe_allow_html=True)
        with zeile[1]:
            werte, labels, vorauswahl = artikel_optionen(p)
            wahl = st.selectbox(
                "Zuordnung",
                werte,
                index=_index(werte, vorauswahl),
                key=f"pos_{laufnr}_zuordnung",
                format_func=lambda v, labels=labels: labels.get(v, v),
                label_visibility="collapsed",
                disabled=stammdaten is None and not p.kandidaten,
            )
            if wahl == OPTION_ARTIKELNUMMER:
                st.text_input("Artikelnummer", key=f"pos_{laufnr}_artikelnummer", placeholder="Artikelnummer", label_visibility="collapsed")
        with zeile[2]:
            _menge_widget(p)
        zeile[3].markdown(formatiere_betrag(p.ek_preis) or "–")
        vk_text = formatiere_betrag(p.vk_preis) or "–"
        if p.rabatt_prozent and p.vk_preis_rabattiert is not None and not p.uebersprungen:
            vk_text += f"  \n<small>→ {html.escape(formatiere_betrag(p.vk_preis_rabattiert))}</small>"
        zeile[4].markdown(vk_text, unsafe_allow_html=True)
        with zeile[5]:
            _rabatt_widget(p)
        zeile[6].markdown(formatiere_betrag(p.preis_dokument) or "–")
        zeile[7].markdown(_abweichung_html(p), unsafe_allow_html=True)
        zeile[8].markdown(_status_html(p), unsafe_allow_html=True)
        for h in p.hinweise:
            if h.schweregrad == Schweregrad.FEHLER and h.code not in stufe2.RABATT_CODES:
                zeile[8].markdown(f"<small style='color:{_ROT}'>{html.escape(h.text)}</small>", unsafe_allow_html=True)


def zeige_summen(paket: Belegpaket) -> None:
    spalten = st.columns(4)
    spalten[0].metric("Gesamtmenge (aktiv)", formatiere_menge(paket.gesamtmenge))
    spalten[1].metric("Netto EK (Stamm)", f"{formatiere_betrag(paket.summe_netto_ek)} €")
    spalten[2].metric("Netto VK (Preisgruppe 1)", f"{formatiere_betrag(paket.summe_netto_vk)} €")
    with spalten[3]:
        if paket.roh.summenvermerke:
            for s in paket.roh.summenvermerke:
                text = f"Summenvermerk: {formatiere_menge(s.wert)} {s.einheit or ''} („{s.rohtext}“)"
                if s.wert != paket.gesamtmenge and s.wert != paket.roh.gesamtmenge:
                    st.markdown(f"<span style='color:{_ROT};font-weight:600'>{html.escape(text)}</span>", unsafe_allow_html=True)
                else:
                    st.markdown(text)
        else:
            st.caption("Kein Summenvermerk im Dokument.")


def _hinweis_text(h: Pruefhinweis) -> str:
    pos = f" Pos. {h.position_laufnr}:" if h.position_laufnr is not None else ""
    return f"[{h.code.value}]{pos} {h.text}"


def zeige_hinweise(paket: Belegpaket) -> None:
    hinweise = paket.alle_hinweise
    fehler = [h for h in hinweise if h.schweregrad == Schweregrad.FEHLER]
    warnungen = [h for h in hinweise if h.schweregrad == Schweregrad.WARNUNG]
    infos = [h for h in hinweise if h.schweregrad == Schweregrad.INFO]
    st.subheader(f"Hinweise ({len(fehler)} Fehler, {len(warnungen)} Warnungen, {len(infos)} Infos)")
    if paket.blockiert:
        st.error("Das Paket ist BLOCKIERT: Fehler müssen vor der Freigabe behoben werden.")
    for h in fehler:
        st.error(_hinweis_text(h))
    for h in warnungen:
        st.warning(_hinweis_text(h))
    if infos:
        with st.expander(f"Infos ({len(infos)})", expanded=False):
            for h in infos:
                st.info(_hinweis_text(h))


def lese_korrekturen(paket: Belegpaket) -> dict[str, Any]:
    """Aktuelle Widget-Werte aus ``st.session_state`` als Korrektur-Dict (siehe
    :func:`uebernehme_korrekturen`)."""
    zustand_ = st.session_state
    werte: dict[str, Any] = {}
    for schluessel, feld in (
        ("kopf_kunde", "kunde"),
        ("kopf_lieferant", "lieferant"),
        ("kopf_belegnummer", "belegnummer"),
        ("kopf_bestellnummer", "bestellnummer"),
        ("kopf_lieferadresse", "lieferadresse"),
        ("kopf_datum", "datum"),
        ("kopf_liefertag", "liefertag"),
        ("lieferschein", "lieferschein"),
    ):
        if schluessel in zustand_:
            werte[feld] = zustand_[schluessel]
    belegfelder = {feld: zustand_[f"bf_{feld}"] for feld in _BELEGFELDER if f"bf_{feld}" in zustand_}
    if belegfelder:
        werte["belegfelder"] = belegfelder
    positionen: dict[int, dict[str, Any]] = {}
    for p in paket.positionen:
        laufnr = p.roh.laufnr
        pw: dict[str, Any] = {}
        if f"pos_{laufnr}_zuordnung" in zustand_:
            pw["zuordnung"] = zustand_[f"pos_{laufnr}_zuordnung"]
        if f"pos_{laufnr}_artikelnummer" in zustand_:
            pw["artikelnummer"] = zustand_[f"pos_{laufnr}_artikelnummer"]
        if f"pos_{laufnr}_menge" in zustand_:
            pw["menge"] = zustand_[f"pos_{laufnr}_menge"]
        if f"pos_{laufnr}_rabatt" in zustand_:
            pw["rabatt"] = zustand_[f"pos_{laufnr}_rabatt"]
        if pw:
            positionen[laufnr] = pw
    if positionen:
        werte["positionen"] = positionen
    return werte


def _korrigieren(paket: Belegpaket, settings: Settings, stammdaten: Stammdaten | None) -> Belegpaket | None:
    try:
        with st.spinner("Übernehme Korrekturen und prüfe erneut …"):
            neu = uebernehme_korrekturen(paket, lese_korrekturen(paket), settings, stammdaten)
    except KorrekturFehler as exc:
        logger.warning("Korrektur nicht anwendbar: %s", exc)
        meldung("error", f"Korrektur nicht übernommen: {exc}")
        return None
    except Exception as exc:  # noqa: BLE001
        logger.exception("Erneute Prüfung fehlgeschlagen")
        meldung("error", f"Erneute Prüfung fehlgeschlagen: {type(exc).__name__}: {exc}")
        return None
    setze_paket(neu, st.session_state.get(SS_PAKET_PFAD))
    speichere_zustand(neu, settings)
    status = "weiterhin BLOCKIERT" if neu.blockiert else "freigabefähig"
    meldung("success", f"Korrekturen übernommen und erneut geprüft: {status}.")
    return neu


def _buchen(paket: Belegpaket, settings: Settings, stammdaten: Stammdaten | None, *, dry_run: bool) -> None:
    try:
        with st.spinner("Übernehme Änderungen und prüfe erneut …"):
            neu = uebernehme_korrekturen(paket, lese_korrekturen(paket), settings, stammdaten)
    except KorrekturFehler as exc:
        meldung("error", f"Nicht gebucht – Korrektur nicht übernommen: {exc}")
        return
    except Exception as exc:  # noqa: BLE001
        logger.exception("Prüfung vor der Buchung fehlgeschlagen")
        meldung("error", f"Nicht gebucht – Prüfung fehlgeschlagen: {type(exc).__name__}: {exc}")
        return
    setze_paket(neu, st.session_state.get(SS_PAKET_PFAD))
    if neu.blockiert:
        speichere_zustand(neu, settings)
        meldung("error", "Nicht gebucht: nach der erneuten Prüfung ist das Paket blockiert (siehe Hinweise).")
        return
    # TODO-LEXWARE: Scharfer Lauf nur am Windows-PC prüfbar: die UI-Automation (pywinauto) läuft im
    # Streamlit-Skript-Thread; sicherstellen, dass der Browser Lexware nicht den Fokus nimmt und die
    # Oberfläche während der Automation nicht neu gerendert wird (kein Klick in der App bis zur Meldung).
    try:
        with st.spinner("Buche …" if not dry_run else "Simuliere Buchung (Dry Run) …"):
            ergebnis, text = freigeben_und_buchen(neu, settings, dry_run=dry_run)
    except Exception as exc:  # noqa: BLE001 - buche wirft fachlich nicht; technische Fehler trotzdem anzeigen
        logger.exception("Buchung fehlgeschlagen")
        meldung("error", f"Buchung fehlgeschlagen: {type(exc).__name__}: {exc}")
        return
    speichere_zustand(neu, settings)
    st.session_state[SS_ERGEBNIS] = ergebnis
    st.session_state[SS_ZUSAMMENFASSUNG] = text
    modus = "Dry Run – nichts in Lexware gespeichert" if ergebnis.dry_run else "SCHARF"
    if ergebnis.erfolgreich:
        meldung("success", f"Buchung erfolgreich ({modus}).")
    else:
        meldung("error", f"Buchung fehlgeschlagen / nicht gebucht ({modus}) – Details im Ergebnis unten.")
    for w in ergebnis.warnungen:
        if w.startswith(("Protokoll:", "Quelldatei archiviert:")):
            meldung("info", w)


def zeige_aktionen(paket: Belegpaket, settings: Settings, stammdaten: Stammdaten | None) -> bool:
    """Checkbox Lieferschein und Buttons; True, wenn das Paket geändert wurde (dann ``st.rerun``)."""
    st.checkbox("Lieferschein erstellen (Auftrag weiterführen)", value=paket.lieferschein_erstellen, key="lieferschein")
    dry_run = bool(st.session_state.get("dry_run", settings.dry_run))
    bestaetigt = bool(st.session_state.get("scharf_bestaetigt", False))
    gesperrt_grund = None
    if paket.blockiert:
        gesperrt_grund = "Buchen ist gesperrt: das Paket ist blockiert (Fehler beheben, dann 'Korrigieren / Neu prüfen')."
    elif not dry_run and not bestaetigt:
        gesperrt_grund = "Scharfes Buchen erfordert die Bestätigung 'Ich habe den Dry Run geprüft' in der Seitenleiste."
    spalten = st.columns(3)
    korrigieren = spalten[0].button("Korrigieren / Neu prüfen", key="korrigieren", width="stretch")
    buchen = spalten[1].button(
        "Freigeben & Buchen" + (" (Dry Run)" if dry_run else " (SCHARF)"),
        key="buchen",
        type="primary",
        disabled=gesperrt_grund is not None,
        width="stretch",
    )
    alias = spalten[2].button("Alias speichern", key="alias_speichern", width="stretch")
    if gesperrt_grund:
        st.caption(gesperrt_grund)

    if korrigieren:
        _korrigieren(paket, settings, stammdaten)
        return True
    if buchen:
        if gesperrt_grund:
            meldung("error", gesperrt_grund)
            return True
        _buchen(paket, settings, stammdaten, dry_run=dry_run)
        return True
    if alias:
        try:
            for text in speichere_aliase(paket, settings):
                meldung("error" if text.startswith("Konflikt") else "info", text)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Alias speichern fehlgeschlagen")
            meldung("error", f"Alias speichern fehlgeschlagen: {type(exc).__name__}: {exc}")
        return True
    return False


def zeige_ergebnis(settings: Settings | None = None, paket: Belegpaket | None = None) -> None:
    text = st.session_state.get(SS_ZUSAMMENFASSUNG)
    ergebnis: BuchungsErgebnis | None = st.session_state.get(SS_ERGEBNIS)
    if not text or ergebnis is None:
        return
    st.subheader("Ergebnis der letzten Buchung")
    pfade = [w for w in ergebnis.warnungen if w.startswith(("Protokoll:", "Quelldatei archiviert:"))]
    for teil in (*ergebnis.alle_einkaeufe(), ergebnis.verkauf, ergebnis.lieferschein):
        if teil is not None and teil.artefakt is not None:
            pfade.append(f"{teil.belegart.value}{f' ({teil.partner})' if teil.partner else ''}: {teil.artefakt}")
    if ergebnis.einkauf_weitere:
        st.info(
            f"Einkauf auf {len(ergebnis.alle_einkaeufe())} Weingüter aufgeteilt: "
            + "; ".join(t.partner or "?" for t in ergebnis.alle_einkaeufe())
        )
    for pfad in pfade:
        st.markdown(f"- {pfad}")
    stufe2.zeige_dateien_und_ablage(ergebnis)
    st.code(text, language=None)
    if settings is not None and paket is not None:
        try:
            stufe2.zeige_nachbereitung(paket, settings)
        except Exception as exc:  # noqa: BLE001 - Anzeige der Nachbereitung darf das Ergebnis nicht verdecken
            logger.exception("Nachbereitung konnte nicht angezeigt werden")
            st.error(f"Nachbereitung konnte nicht angezeigt werden: {type(exc).__name__}: {exc}")


def zeige_seite(settings: Settings, seite: str) -> None:
    """Seiten der Ausbaustufe 2 ("Heute", "Dossiers", "Rabatte", "Einstellungen"); Logik in ``seiten_stufe2``."""
    kontext = ui_kontext(settings)
    try:
        if seite == "Heute":
            geaendert = stufe2.zeige_seite_heute(settings, kontext)
        elif seite == "Dossiers":
            stufe2.zeige_seite_dossiers(settings, kontext)
            geaendert = False
        elif seite == "Rabatte":
            geaendert = stufe2.zeige_seite_rabatte(settings, kontext)
        else:
            stufe2.zeige_seite_einstellungen(settings, kontext)
            geaendert = False
    except Exception as exc:  # noqa: BLE001 - Darstellungsfehler dürfen die App nicht beenden
        logger.exception("Seite %s konnte nicht dargestellt werden", seite)
        st.error(f"Seite „{seite}“ konnte nicht dargestellt werden: {type(exc).__name__}: {exc}")
        return
    if geaendert:
        st.rerun()


# ---------------------------------------------------------------------------
# Einstieg
# ---------------------------------------------------------------------------


def main() -> None:
    st.set_page_config(page_title="Auftrags-Automat – Freigabe", layout="wide")
    konfiguriere_logging()
    initialisiere_zustand()
    st.title("Freigabe – Auftrags-Automat für Lexware premium")

    try:
        settings = lade_konfiguration()
    except Exception as exc:  # noqa: BLE001 - Konfigurationsfehler lesbar anzeigen statt Traceback
        logger.exception("Konfiguration konnte nicht geladen werden")
        st.error(f"Konfiguration konnte nicht geladen werden ({projekt_stamm()}): {type(exc).__name__}: {exc}")
        st.stop()

    if zeige_seitenleiste(settings):
        st.rerun()
    zeige_meldungen()

    seite = aktuelle_seite()
    if seite != stufe2.SEITEN[0]:
        zeige_seite(settings, seite)
        return

    paket: Belegpaket | None = st.session_state.get(SS_PAKET)
    if paket is None:
        st.info("Bitte links eine Eingangsdatei einlesen, einen Auftrag hochladen oder ein gespeichertes Paket laden.")
        zeige_ergebnis(settings)
        return

    try:
        stammdaten = lade_stammdaten_sicher(settings, paket.firma_id)
        zeige_kopf(paket, settings, stammdaten)
        zeige_positionen(paket, stammdaten)
        zeige_summen(paket)
        geaendert = zeige_aktionen(paket, settings, stammdaten)
    except Exception as exc:  # noqa: BLE001 - Darstellungsfehler dürfen die App nicht beenden
        logger.exception("Darstellung des Pakets fehlgeschlagen")
        st.error(f"Darstellung fehlgeschlagen: {type(exc).__name__}: {exc}")
        return
    if geaendert:
        st.rerun()
    zeige_hinweise(paket)
    zeige_ergebnis(settings, paket)


if __name__ == "__main__":
    main()
