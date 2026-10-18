"""Stammdaten aus Lexware-CSV-Exporten (``Datei -> Export -> ASCII``), NUR LESEND.

Format (siehe ``tests/fixtures/stammdaten/<firma>/*.csv``):

* Trennzeichen ``;`` (konfigurierbar), Textkennzeichen ``"``, Kodierung cp1252
  (Lexware "ANSI (Windows)"). Beginnt die Datei mit einem UTF-8-BOM oder scheitert die
  Dekodierung mit ``konfig.kodierung``, wird ``utf-8-sig`` versucht (mit Warnung im Log).
* Kopfzeile mit Spaltennamen. Standard: Spaltenname = Feldname des Modells (:class:`Artikel`,
  :class:`Kunde`, :class:`Lieferant`, :class:`Beleg`). Abweichende Exportspalten werden über
  ``CsvQuellen.spalten_artikel`` usw. zugeordnet (unser Feldname -> Spaltenname im Export).
  Der Vergleich ist unabhängig von Groß-/Kleinschreibung, Leerraum, Anführungszeichen und BOM.
  Unbekannte Spalten werden ignoriert, fehlende Pflichtspalten ergeben einen
  :class:`StammdatenFehler` mit Dateiname und erwarteten Spalten.
* Deutsche Dezimalzahlen ("4,20", "1.234,50"), Wahrheitswerte 0/1, ja/nein, J/N, true/false,
  wahr/falsch, leere Werte -> ``None``.
* ``volumen_cl``/``holzkiste``/``jahrgang`` leer -> aus der Bezeichnung abgeleitet
  (``matching.volumen``).
* Zusätzlich zu den Modellfeldern werden die negierten Lexware-Exportfelder ``inaktiv``
  und ``gesperrt`` verstanden (z. B. ``spalten_kunden: {inaktiv: "Inaktiv"}``).

Die Parser (:func:`parse_dezimal`, :func:`parse_bool`, :func:`parse_datum`,
:func:`parse_belegart`) und die Zeilenumwandler (:func:`zeile_zu_artikel` usw.) werden
auch von ``odbc_quelle`` verwendet; sie akzeptieren deshalb sowohl Strings als auch
native Werte (``int``, ``Decimal``, ``date`` ...).
"""

from __future__ import annotations

import codecs
import csv
import io
import logging
import math
import re
from collections.abc import Callable, Mapping
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from auftragsautomat.config import CsvQuellen
from auftragsautomat.matching.volumen import ist_holzkiste, parse_jahrgang, parse_volumen_cl
from auftragsautomat.models import Artikel, Beleg, Belegart, Kunde, Lieferant

__all__ = [
    "MODELLE",
    "PFLICHTFELDER",
    "ZUSATZFELDER",
    "CsvStammdatenQuelle",
    "StammdatenFehler",
    "als_text",
    "feldnamen",
    "lies_csv_zeilen",
    "parse_belegart",
    "parse_bool",
    "parse_datum",
    "parse_dezimal",
    "parse_int",
    "zeile_zu_artikel",
    "zeile_zu_beleg",
    "zeile_zu_kunde",
    "zeile_zu_lieferant",
]

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class StammdatenFehler(Exception):
    """Stammdaten können nicht gelesen werden (Datei/Spalten fehlen, Verbindung, SQL, Inhalt).

    Hinweis: Diese Ausnahme gehört fachlich nach ``stammdaten.basis``; sie liegt hier, weil
    ``basis.py`` nicht Teil dieses Moduls ist. ``odbc_quelle`` und ``laden`` exportieren sie erneut.
    """


# ---------------------------------------------------------------------------
# Tabellen, Felder, Pflichtspalten
# ---------------------------------------------------------------------------

MODELLE: dict[str, type[BaseModel]] = {
    "artikel": Artikel,
    "kunden": Kunde,
    "lieferanten": Lieferant,
    "belege": Beleg,
}

PFLICHTFELDER: dict[str, tuple[str, ...]] = {
    "artikel": ("artikelnummer", "bezeichnung"),
    "kunden": ("kundennummer", "name"),
    "lieferanten": ("lieferantennummer", "name"),
    "belege": ("belegart", "belegnummer"),
}

# Optionale, negierte Felder aus dem Lexware-Export ("Inaktiv (J/N)", "Gesperrt (J/N)").
ZUSATZFELDER: dict[str, tuple[str, ...]] = {
    "artikel": ("inaktiv", "gesperrt"),
    "kunden": ("inaktiv",),
    "lieferanten": ("inaktiv",),
    "belege": (),
}

_SCHLUESSELFELD: dict[str, str] = {
    "artikel": "artikelnummer",
    "kunden": "kundennummer",
    "lieferanten": "lieferantennummer",
    "belege": "belegnummer",
}


def feldnamen(tabelle: str) -> tuple[str, ...]:
    """Unsere Feldnamen einer Tabelle: Modellfelder ohne ``firma_id`` plus Zusatzfelder."""
    modell = MODELLE[tabelle]
    felder = tuple(f for f in modell.model_fields if f != "firma_id")
    return felder + ZUSATZFELDER.get(tabelle, ())


# ---------------------------------------------------------------------------
# Werte-Parser (Strings aus CSV oder native Werte aus ODBC)
# ---------------------------------------------------------------------------

_WAHR = {"1", "ja", "j", "true", "wahr", "yes", "y", "x", "-1", "w"}
_FALSCH = {"0", "nein", "n", "false", "falsch", "no", "f"}
_DEZIMAL_STOERER = re.compile(r"(€|eur\b|\s)", re.IGNORECASE)
_DATUM_MUSTER: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})$"), "tmj"),
    (re.compile(r"^(\d{1,2})\.(\d{1,2})\.(\d{2})$"), "tmj2"),
    (re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$"), "jmt"),
    (re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$"), "tmj"),
    (re.compile(r"^(\d{4})(\d{2})(\d{2})$"), "jmt"),
)


def als_text(wert: object) -> str | None:
    """Beliebigen Wert in einen bereinigten String wandeln; leer -> ``None``.

    Ganze Zahlen aus ODBC (``3441``, ``Decimal("3441")``, ``3441.0``) werden ohne
    Nachkommastellen geschrieben, damit Nummern vergleichbar bleiben.
    """
    if wert is None:
        return None
    if isinstance(wert, bool):
        return "1" if wert else "0"
    if isinstance(wert, int):
        return str(wert)
    if isinstance(wert, float):
        if not math.isfinite(wert):
            return None
        return str(int(wert)) if wert.is_integer() else repr(wert)
    if isinstance(wert, Decimal):
        if wert.is_finite() and wert == wert.to_integral_value():
            return str(int(wert))
        return str(wert)
    if isinstance(wert, datetime):
        return wert.date().isoformat()
    if isinstance(wert, date):
        return wert.isoformat()
    if isinstance(wert, bytes | bytearray):
        wert = bytes(wert).decode("cp1252", errors="replace")
    text = str(wert).strip().strip("\x00").strip()
    return text or None


def _leer(wert: object) -> bool:
    if wert is None:
        return True
    if isinstance(wert, str):
        return not wert.strip()
    return False


def parse_dezimal(wert: object) -> Decimal | None:
    """Deutsche ("4,20", "1.234,50", "6,90 €") oder technische ("4.20") Dezimalzahl -> Decimal.

    Native Zahlen werden exakt übernommen (``float`` über ``repr``, nie binär). Leere
    Werte, "-" und ungültige Texte ergeben ``None`` (Warnung im Log bei ungültigen Texten).
    """
    if wert is None:
        return None
    if isinstance(wert, bool):
        return Decimal(int(wert))
    if isinstance(wert, Decimal):
        return wert if wert.is_finite() else None
    if isinstance(wert, int):
        return Decimal(wert)
    if isinstance(wert, float):
        return Decimal(repr(wert)) if math.isfinite(wert) else None
    text = _DEZIMAL_STOERER.sub("", str(wert)).strip()
    if not text or text in {"-", "--", "–"}:
        return None
    if "," in text:
        # deutsch: Punkt = Tausender, Komma = Dezimaltrenner
        text = text.replace(".", "").replace(",", ".")
    elif text.count(".") > 1:
        # nur Punkte, mehrere: Tausendertrenner
        text = text.replace(".", "")
    try:
        return Decimal(text)
    except InvalidOperation:
        logger.warning("Ungültige Dezimalzahl %r wird als leer behandelt", wert)
        return None


def parse_int(wert: object) -> int | None:
    """Ganze Zahl ("1", "2023", 1, Decimal("1"), "1,0") -> int; leer/ungültig -> ``None``."""
    if wert is None:
        return None
    if isinstance(wert, bool):
        return int(wert)
    if isinstance(wert, int):
        return wert
    dezimal = parse_dezimal(wert)
    if dezimal is None:
        return None
    if dezimal != dezimal.to_integral_value():
        logger.warning("Erwartete ganze Zahl, erhalten %r; Wert wird als leer behandelt", wert)
        return None
    return int(dezimal)


def parse_bool(wert: object, standard: bool = False) -> bool:
    """0/1, ja/nein, J/N, true/false, wahr/falsch, x, -1 -> bool; leer -> ``standard``."""
    if wert is None:
        return standard
    if isinstance(wert, bool):
        return wert
    if isinstance(wert, int | float | Decimal):
        return wert != 0
    text = str(wert).strip().lower()
    if not text:
        return standard
    if text in _WAHR:
        return True
    if text in _FALSCH:
        return False
    logger.warning("Unbekannter Wahrheitswert %r, verwende Standard %s", wert, standard)
    return standard


def parse_datum(wert: object) -> date | None:
    """Datum "15.09.2026", "15.9.26", "2026-09-15", "2026-09-15 00:00:00", date/datetime -> date."""
    if wert is None:
        return None
    if isinstance(wert, datetime):
        return wert.date()
    if isinstance(wert, date):
        return wert
    text = str(wert).strip()
    if not text:
        return None
    # Zeitanteil abschneiden ("2026-09-15 00:00:00", "2026-09-15T10:30", "15.09.2026 10:30")
    text = re.split(r"[ T]", text, maxsplit=1)[0]
    for muster, art in _DATUM_MUSTER:
        treffer = muster.match(text)
        if not treffer:
            continue
        a, b, c = (int(g) for g in treffer.groups())
        if art == "jmt":
            jahr, monat, tag = a, b, c
        elif art == "tmj2":
            tag, monat, jahr = a, b, (2000 + c if c < 80 else 1900 + c)
        else:
            tag, monat, jahr = a, b, c
        try:
            return date(jahr, monat, tag)
        except ValueError:
            logger.warning("Ungültiges Datum %r wird als leer behandelt", wert)
            return None
    logger.warning("Unbekanntes Datumsformat %r wird als leer behandelt", wert)
    return None


_BELEGARTEN: dict[str, Belegart] = {
    # Auftragsbestätigung
    "AB": Belegart.AUFTRAGSBESTAETIGUNG,
    "AUFTRAGSBESTAETIGUNG": Belegart.AUFTRAGSBESTAETIGUNG,
    "AUFTRAGSBEST": Belegart.AUFTRAGSBESTAETIGUNG,
    "AUFTRAG": Belegart.AUFTRAGSBESTAETIGUNG,
    "1": Belegart.AUFTRAGSBESTAETIGUNG,  # TODO-LEXWARE: FK_Auftrag.AuftragsKennung 1 = AB
    # Lieferschein
    "LS": Belegart.LIEFERSCHEIN,
    "LI": Belegart.LIEFERSCHEIN,
    "LIEFERSCHEIN": Belegart.LIEFERSCHEIN,
    "2": Belegart.LIEFERSCHEIN,  # TODO-LEXWARE: AuftragsKennung 2 = Lieferschein
    # Rechnung
    "RE": Belegart.RECHNUNG,
    "RG": Belegart.RECHNUNG,
    "RECHNUNG": Belegart.RECHNUNG,
    "3": Belegart.RECHNUNG,  # TODO-LEXWARE: AuftragsKennung 3 = Rechnung
    # Bestellung (Einkauf)
    "BE": Belegart.BESTELLUNG,
    "B": Belegart.BESTELLUNG,
    "BESTELLUNG": Belegart.BESTELLUNG,
    "5": Belegart.BESTELLUNG,  # TODO-LEXWARE: AuftragsKennung 5 = Bestellung
}
_BELEGART_PRAEFIXE: tuple[tuple[str, Belegart], ...] = (
    ("AUFTRAGSBEST", Belegart.AUFTRAGSBESTAETIGUNG),
    ("LIEFERSCH", Belegart.LIEFERSCHEIN),
    ("RECHN", Belegart.RECHNUNG),
    ("BESTELL", Belegart.BESTELLUNG),
)


def parse_belegart(wert: object) -> Belegart:
    """Belegart tolerant erkennen ("AB", "Auftragsbestätigung", "LS", "Rechnung", 5 ...) -> Belegart.

    Unbekannte oder leere Werte ergeben :attr:`Belegart.SONSTIGE`.
    """
    if isinstance(wert, Belegart):
        return wert
    text = als_text(wert)
    if text is None:
        return Belegart.SONSTIGE
    norm = text.upper()
    for alt, neu in (("Ä", "AE"), ("Ö", "OE"), ("Ü", "UE"), ("ß", "SS"), ("-", ""), (".", ""), (" ", "")):
        norm = norm.replace(alt, neu)
    try:
        return Belegart(text.upper())
    except ValueError:
        pass
    if norm in _BELEGARTEN:
        return _BELEGARTEN[norm]
    for praefix, belegart in _BELEGART_PRAEFIXE:
        if norm.startswith(praefix):
            return belegart
    logger.debug("Unbekannte Belegart %r -> SONSTIGE", wert)
    return Belegart.SONSTIGE


# ---------------------------------------------------------------------------
# Zeile (dict mit unseren Feldnamen) -> Modell
# ---------------------------------------------------------------------------


def _aktiv(zeile: Mapping[str, object]) -> bool:
    """``aktiv`` (Standard wahr), negiert durch ``inaktiv``/``gesperrt`` (Lexware-Export)."""
    aktiv_roh = zeile.get("aktiv")
    aktiv = True if _leer(aktiv_roh) else parse_bool(aktiv_roh, standard=True)
    for negativ in ("inaktiv", "gesperrt"):
        wert = zeile.get(negativ)
        if not _leer(wert) and parse_bool(wert, standard=False):
            aktiv = False
    return aktiv


def _pflicht(zeile: Mapping[str, object], feld: str, kontext: str) -> str:
    wert = als_text(zeile.get(feld))
    if wert is None:
        raise StammdatenFehler(f"{kontext}: Pflichtfeld '{feld}' ist leer")
    return wert


def _validiere(modell: type[T], kontext: str, **felder: object) -> T:
    try:
        return modell.model_validate(felder)
    except ValidationError as exc:
        fehler = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
        )
        raise StammdatenFehler(f"{kontext}: ungültige Werte ({fehler})") from exc


def zeile_zu_artikel(firma_id: str, zeile: Mapping[str, object]) -> Artikel:
    """Zeile mit unseren Feldnamen -> :class:`Artikel`; fehlende Volumen-/Holzkisten-/
    Jahrgangsangaben werden aus der Bezeichnung abgeleitet."""
    nummer = _pflicht(zeile, "artikelnummer", "Artikel")
    kontext = f"Artikel {nummer}"
    bezeichnung = _pflicht(zeile, "bezeichnung", kontext)

    volumen = parse_dezimal(zeile.get("volumen_cl"))
    if volumen is None:
        volumen = parse_volumen_cl(bezeichnung)
        if volumen is not None:
            logger.debug("%s: Volumen %s cl aus der Bezeichnung abgeleitet", kontext, volumen)
    holz_roh = zeile.get("holzkiste")
    holzkiste = ist_holzkiste(bezeichnung) if _leer(holz_roh) else parse_bool(holz_roh)
    jahrgang = parse_int(zeile.get("jahrgang"))
    if jahrgang is None:
        jahrgang = parse_jahrgang(bezeichnung)

    return _validiere(
        Artikel,
        kontext,
        firma_id=firma_id,
        artikelnummer=nummer,
        bezeichnung=bezeichnung,
        warengruppe=als_text(zeile.get("warengruppe")),
        lieferantennummer=als_text(zeile.get("lieferantennummer")),
        volumen_cl=volumen,
        holzkiste=holzkiste,
        jahrgang=jahrgang,
        ek_preis=parse_dezimal(zeile.get("ek_preis")),
        vk_preis_1=parse_dezimal(zeile.get("vk_preis_1")),
        einheit=als_text(zeile.get("einheit")),
        matchcode=als_text(zeile.get("matchcode")),
        aktiv=_aktiv(zeile),
    )


def _name_mit_ersatz(zeile: Mapping[str, object], kontext: str) -> tuple[str, str | None]:
    """``name`` ist Pflicht; ist er leer, aber ``name2`` gefüllt, rückt ``name2`` nach."""
    name = als_text(zeile.get("name"))
    name2 = als_text(zeile.get("name2"))
    if name is None and name2 is not None:
        logger.info("%s: 'name' leer, verwende 'name2' (%s)", kontext, name2)
        return name2, None
    if name is None:
        raise StammdatenFehler(f"{kontext}: Pflichtfeld 'name' ist leer")
    return name, name2


def zeile_zu_kunde(firma_id: str, zeile: Mapping[str, object]) -> Kunde:
    """Zeile mit unseren Feldnamen -> :class:`Kunde`."""
    nummer = _pflicht(zeile, "kundennummer", "Kunde")
    kontext = f"Kunde {nummer}"
    name, name2 = _name_mit_ersatz(zeile, kontext)
    return _validiere(
        Kunde,
        kontext,
        firma_id=firma_id,
        kundennummer=nummer,
        name=name,
        name2=name2,
        strasse=als_text(zeile.get("strasse")),
        plz=als_text(zeile.get("plz")),
        ort=als_text(zeile.get("ort")),
        land=als_text(zeile.get("land")),
        preisgruppe=parse_int(zeile.get("preisgruppe")),
        matchcode=als_text(zeile.get("matchcode")),
        aktiv=_aktiv(zeile),
    )


def zeile_zu_lieferant(firma_id: str, zeile: Mapping[str, object]) -> Lieferant:
    """Zeile mit unseren Feldnamen -> :class:`Lieferant`."""
    nummer = _pflicht(zeile, "lieferantennummer", "Lieferant")
    kontext = f"Lieferant {nummer}"
    name, name2 = _name_mit_ersatz(zeile, kontext)
    return _validiere(
        Lieferant,
        kontext,
        firma_id=firma_id,
        lieferantennummer=nummer,
        name=name,
        name2=name2,
        ort=als_text(zeile.get("ort")),
        matchcode=als_text(zeile.get("matchcode")),
        aktiv=_aktiv(zeile),
    )


def zeile_zu_beleg(firma_id: str, zeile: Mapping[str, object]) -> Beleg:
    """Zeile mit unseren Feldnamen -> :class:`Beleg` (für die Dublettenprüfung)."""
    nummer = _pflicht(zeile, "belegnummer", "Beleg")
    kontext = f"Beleg {nummer}"
    belegart_roh = zeile.get("belegart")
    if _leer(belegart_roh):
        logger.warning("%s: Belegart leer -> SONSTIGE", kontext)
    belegart = parse_belegart(belegart_roh)
    return _validiere(
        Beleg,
        kontext,
        firma_id=firma_id,
        belegart=belegart,
        belegnummer=nummer,
        kundenbestellnummer=als_text(zeile.get("kundenbestellnummer")),
        partnernummer=als_text(zeile.get("partnernummer")),
        datum=parse_datum(zeile.get("datum")),
        netto=parse_dezimal(zeile.get("netto")),
    )


ZEILENUMWANDLER: dict[str, Callable[[str, Mapping[str, object]], BaseModel]] = {
    "artikel": zeile_zu_artikel,
    "kunden": zeile_zu_kunde,
    "lieferanten": zeile_zu_lieferant,
    "belege": zeile_zu_beleg,
}


def baue_modelle(
    firma_id: str,
    tabelle: str,
    zeilen: list[tuple[int, Mapping[str, object]]],
    quelle: str,
) -> list:
    """Zeilen (Nummer, Werte mit unseren Feldnamen) -> Modelle.

    Fehlerhafte Zeilen werden mit Warnung übersprungen (nie stillschweigend), doppelte
    Schlüssel gemeldet. Wird von CSV- und ODBC-Quelle gemeinsam verwendet.
    """
    umwandler = ZEILENUMWANDLER[tabelle]
    schluesselfeld = _SCHLUESSELFELD[tabelle]
    ergebnis: list = []
    gesehen: set[str] = set()
    uebersprungen = 0
    for nr, zeile in zeilen:
        try:
            modell = umwandler(firma_id, zeile)
        except StammdatenFehler as exc:
            uebersprungen += 1
            logger.warning("%s, Zeile %s übersprungen: %s", quelle, nr, exc)
            continue
        schluessel = getattr(modell, schluesselfeld)
        if tabelle != "belege" and schluessel in gesehen:
            logger.warning(
                "%s, Zeile %s: %s '%s' ist doppelt; der spätere Eintrag überschreibt im Index den früheren",
                quelle,
                nr,
                schluesselfeld,
                schluessel,
            )
        gesehen.add(schluessel)
        ergebnis.append(modell)
    if uebersprungen:
        logger.warning("%s: %d von %d Zeilen übersprungen", quelle, uebersprungen, len(zeilen))
    logger.info("%s: %d %s gelesen (Firma %s)", quelle, len(ergebnis), tabelle, firma_id)
    return ergebnis


# ---------------------------------------------------------------------------
# CSV lesen
# ---------------------------------------------------------------------------


def _normalisiere_spaltenname(name: object) -> str:
    text = str(name or "").replace("﻿", "").strip().strip('"').strip("'").strip()
    return re.sub(r"\s+", " ", text).lower()


def _trennzeichen(wert: str) -> str:
    if wert.lower() in {"tab", "\\t", "tabulator"}:
        return "\t"
    if len(wert) != 1:
        raise StammdatenFehler(f"Ungültiges CSV-Trennzeichen {wert!r} (genau ein Zeichen erwartet)")
    return wert


def _lies_text(pfad: Path, kodierung: str) -> str:
    daten = pfad.read_bytes()
    if daten.startswith(codecs.BOM_UTF8):
        if kodierung.lower().replace("-", "").replace("_", "") not in {"utf8", "utf8sig"}:
            logger.warning(
                "%s beginnt mit einem UTF-8-BOM; lese als utf-8-sig statt %s", pfad.name, kodierung
            )
        return daten.decode("utf-8-sig")
    try:
        return daten.decode(kodierung)
    except LookupError as exc:
        raise StammdatenFehler(f"Unbekannte Kodierung '{kodierung}' für {pfad}") from exc
    except UnicodeDecodeError as exc:
        logger.warning(
            "%s lässt sich nicht als %s lesen (%s); versuche utf-8-sig", pfad.name, kodierung, exc
        )
        try:
            return daten.decode("utf-8-sig")
        except UnicodeDecodeError as exc2:
            raise StammdatenFehler(
                f"{pfad}: weder als {kodierung} noch als utf-8-sig lesbar ({exc2})"
            ) from exc2


def lies_csv_zeilen(pfad: Path, konfig: CsvQuellen, tabelle: str) -> list[tuple[int, dict[str, str | None]]]:
    """Liest eine Export-Datei und liefert (Zeilennummer, {unser Feldname: Text | None}).

    Spaltenzuordnung über ``konfig.spalten_<tabelle>`` (unser Feldname -> Exportspalte),
    Standard ist der Feldname selbst. Unbekannte Spalten werden ignoriert, fehlende
    Pflichtspalten lösen :class:`StammdatenFehler` aus.
    """
    if tabelle not in MODELLE:
        raise StammdatenFehler(f"Unbekannte Stammdatentabelle '{tabelle}'")
    if not pfad.is_file():
        raise StammdatenFehler(f"Stammdatendatei für '{tabelle}' fehlt: {pfad}")

    text = _lies_text(pfad, konfig.kodierung)
    leser = csv.reader(io.StringIO(text, newline=""), delimiter=_trennzeichen(konfig.trennzeichen), quotechar='"')
    alle_zeilen = list(leser)

    # führende Leerzeilen überspringen
    start = 0
    while start < len(alle_zeilen) and not any(z.strip() for z in alle_zeilen[start]):
        start += 1
    if start >= len(alle_zeilen):
        raise StammdatenFehler(f"{pfad}: Datei ist leer (keine Kopfzeile)")

    kopf_roh = alle_zeilen[start]
    kopf = [_normalisiere_spaltenname(s) for s in kopf_roh]
    zuordnung: dict[str, str] = getattr(konfig, f"spalten_{tabelle}", {}) or {}
    felder = feldnamen(tabelle)

    index: dict[str, int] = {}
    fehlend: list[str] = []
    for feld in felder:
        spalte = zuordnung.get(feld, feld)
        norm = _normalisiere_spaltenname(spalte)
        if kopf.count(norm) > 1:
            logger.warning("%s: Spalte '%s' kommt mehrfach vor; die erste wird verwendet", pfad.name, spalte)
        if norm in kopf:
            index[feld] = kopf.index(norm)
        elif feld in PFLICHTFELDER[tabelle]:
            fehlend.append(f"{feld} (erwartete Spalte '{spalte}')")
    if fehlend:
        erwartet = ", ".join(zuordnung.get(f, f) for f in PFLICHTFELDER[tabelle])
        raise StammdatenFehler(
            f"{pfad}: Pflichtspalten fehlen: {', '.join(fehlend)}. "
            f"Erwartete Pflichtspalten: {erwartet}. Gefundene Spalten: {', '.join(kopf_roh)}"
        )
    unbekannt = [kopf_roh[i] for i, s in enumerate(kopf) if i not in index.values()]
    if unbekannt:
        logger.debug("%s: ignoriere Spalten %s", pfad.name, ", ".join(repr(s) for s in unbekannt))
    nicht_gefunden = [f for f in felder if f not in index and f not in ZUSATZFELDER.get(tabelle, ())]
    if nicht_gefunden:
        logger.info("%s: optionale Spalten fehlen (bleiben leer): %s", pfad.name, ", ".join(nicht_gefunden))

    ergebnis: list[tuple[int, dict[str, str | None]]] = []
    zu_lang = 0
    for nr, zeile in enumerate(alle_zeilen[start + 1 :], start=start + 2):
        if not any(z.strip() for z in zeile):
            continue
        if len(zeile) > len(kopf):
            zu_lang += 1
        werte: dict[str, str | None] = {}
        for feld, i in index.items():
            roh = zeile[i] if i < len(zeile) else ""
            werte[feld] = roh.strip() or None
        ergebnis.append((nr, werte))
    if zu_lang:
        logger.warning("%s: %d Zeilen haben mehr Felder als die Kopfzeile; überzählige Felder ignoriert", pfad.name, zu_lang)
    logger.debug("%s: %d Datenzeilen gelesen", pfad.name, len(ergebnis))
    return ergebnis


class CsvStammdatenQuelle:
    """Stammdatenquelle aus Lexware-CSV-Exporten einer Firma (implementiert ``StammdatenQuelle``)."""

    def __init__(self, firma_id: str, konfig: CsvQuellen) -> None:
        if konfig is None:
            raise StammdatenFehler(f"Firma {firma_id}: keine CSV-Konfiguration (stammdaten.csv) vorhanden")
        self.firma_id = firma_id
        self.konfig = konfig

    def __repr__(self) -> str:
        return f"CsvStammdatenQuelle({self.firma_id}, {self.konfig.artikel.parent})"

    def _lade(self, tabelle: str, pfad: Path) -> list:
        zeilen = lies_csv_zeilen(pfad, self.konfig, tabelle)
        return baue_modelle(self.firma_id, tabelle, zeilen, pfad.name)

    def artikel(self) -> list[Artikel]:
        return self._lade("artikel", Path(self.konfig.artikel))

    def kunden(self) -> list[Kunde]:
        return self._lade("kunden", Path(self.konfig.kunden))

    def lieferanten(self) -> list[Lieferant]:
        return self._lade("lieferanten", Path(self.konfig.lieferanten))

    def belege(self) -> list[Beleg]:
        """Belegexport ist optional: fehlt die Datei, gibt es keine Dublettenprüfung (Warnung)."""
        if self.konfig.belege is None:
            logger.info("Firma %s: kein Belegexport konfiguriert; Dublettenprüfung ohne Belege", self.firma_id)
            return []
        pfad = Path(self.konfig.belege)
        if not pfad.is_file():
            logger.warning(
                "Firma %s: Belegexport %s fehlt; Dublettenprüfung läuft ohne vorhandene Belege",
                self.firma_id,
                pfad,
            )
            return []
        return self._lade("belege", pfad)
