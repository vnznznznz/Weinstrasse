"""Stammdaten aus Lexware-CSV-Exporten (``Datei -> Export -> ASCII``), NUR LESEND.

Format (siehe ``tests/fixtures/stammdaten/<firma>/*.csv``):

* Trennzeichen ``;`` (konfigurierbar), Textkennzeichen ``"`` (Werte dürfen beidseitig in
  Anführungszeichen stehen oder nicht), Kodierung cp1252 (Lexware "ANSI (Windows)").
  **Kodierungs-Erkennung**: Beginnt die Datei mit einem UTF-8-BOM, wird sie als ``utf-8-sig``
  gelesen (Artikel-Export). Scheitert die Dekodierung mit ``konfig.kodierung``, werden
  nacheinander ``utf-8-sig``, ``cp1252`` und ``cp850`` versucht – jeweils mit Warnung im Log.
* Kopfzeile mit Spaltennamen. Standard: Spaltenname = Feldname des Modells (:class:`Artikel`,
  :class:`Kunde`, :class:`Lieferant`, :class:`Beleg`). Abweichende Exportspalten werden über
  ``CsvQuellen.spalten_artikel`` usw. zugeordnet (unser Feldname -> Spaltenname im Export).
  **Tolerantes Kopfzeilen-Matching** (:func:`spaltenschluessel`): NFKC-Normalisierung,
  Anführungszeichen und BOM entfernen, Kleinschreibung, Mehrfach-Leerzeichen zusammenfassen;
  verglichen wird zuerst die umschriebene Form (ß -> ss, ä -> ae) und dann die Form ohne alle
  Nicht-ASCII-Zeichen. Damit passt die Konfiguration "Straße" sowohl auf "Straße" als auch auf
  die DOS-Umlaut-Mojibake "Straáe" des Kunden-Exports ("steuerbare Ums„tze", "F„llig zum", "õ13b").
  Unbekannte Spalten werden ignoriert, fehlende Pflichtspalten ergeben einen
  :class:`StammdatenFehler` mit Dateiname und erwarteten Spalten.
* **Invertierte Spalten**: Ein ``!`` vor dem Spaltennamen negiert einen Wahrheitswert, z. B.
  ``spalten_artikel: {aktiv: "!Gesperrt"}`` (Gesperrt = J -> aktiv = nein) oder
  ``spalten_kunden: {aktiv: "!Inaktiv"}``. Leere Zellen bleiben leer (Standard: aktiv).
* Deutsche Dezimalzahlen ("4,20", "1.234,50"), Wahrheitswerte 0/1, ja/nein, J/N, true/false,
  wahr/falsch, leere Werte -> ``None``. Führende Tabulatoren/Leerzeichen in Werten (Lexware
  exportiert Postleitzahlen teils als ``"\\t46244"``) werden entfernt.
* ``volumen_cl`` leer -> aus ``einheit`` ("75 cl") oder der Bezeichnung abgeleitet;
  ``holzkiste``/``jahrgang`` leer -> aus der Bezeichnung (``matching.volumen``). ``jahrgang``
  aus einer Spalte (z. B. "Benutzerdefiniert 2") zählt nur, wenn er vierstellig (1900–2099) ist.
  ``ek_preis`` 0,00 bedeutet "kein EK gepflegt" -> ``None``.
* Zusätzlich zu den Modellfeldern werden verstanden: die negierten Lexware-Exportfelder
  ``inaktiv``/``gesperrt`` (z. B. ``spalten_kunden: {inaktiv: "Inaktiv"}``), bei Kunden und
  Lieferanten ``nachname``/``vorname`` (Lexware "Name"/"Vorname": Fallback, wenn "Firma" leer ist)
  und bei Kunden ``hausnr`` (wird an ``strasse`` angehängt).

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
import unicodedata
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
    "spaltenschluessel",
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

# Optionale Zusatzfelder aus dem Lexware-Export: negierte Flags ("Inaktiv (J/N)", "Gesperrt (J/N)"),
# Personenname ("Name"/"Vorname" als Fallback für "Firma") und Hausnummer (an die Straße angehängt).
ZUSATZFELDER: dict[str, tuple[str, ...]] = {
    "artikel": ("inaktiv", "gesperrt"),
    "kunden": ("inaktiv", "nachname", "vorname", "hausnr"),
    "lieferanten": ("inaktiv", "nachname", "vorname"),
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


_JAHRGANG_SPALTE = re.compile(r"^\s*((?:19|20)\d{2})\s*$")


def _jahrgang_aus_spalte(wert: object, kontext: str) -> int | None:
    """Jahrgang aus einer Spalte (z. B. "Benutzerdefiniert 2"): nur vierstellig 1900–2099, sonst ``None``."""
    text = als_text(wert)
    if text is None:
        return None
    treffer = _JAHRGANG_SPALTE.match(text)
    if treffer:
        return int(treffer.group(1))
    logger.debug("%s: Jahrgangsspalte %r ist nicht vierstellig, wird ignoriert", kontext, text)
    return None


def zeile_zu_artikel(firma_id: str, zeile: Mapping[str, object]) -> Artikel:
    """Zeile mit unseren Feldnamen -> :class:`Artikel`; fehlende Volumen-/Holzkisten-/
    Jahrgangsangaben werden aus ``einheit`` ("75 cl") bzw. der Bezeichnung abgeleitet.
    EK-Preis 0,00 gilt als nicht gepflegt (``None``)."""
    nummer = _pflicht(zeile, "artikelnummer", "Artikel")
    kontext = f"Artikel {nummer}"
    bezeichnung = _pflicht(zeile, "bezeichnung", kontext)
    einheit = als_text(zeile.get("einheit"))

    volumen = parse_dezimal(zeile.get("volumen_cl"))
    if volumen is None and einheit:
        volumen = parse_volumen_cl(einheit)
        if volumen is not None:
            logger.debug("%s: Volumen %s cl aus der Einheit %r abgeleitet", kontext, volumen, einheit)
    if volumen is None:
        volumen = parse_volumen_cl(bezeichnung)
        if volumen is not None:
            logger.debug("%s: Volumen %s cl aus der Bezeichnung abgeleitet", kontext, volumen)
    holz_roh = zeile.get("holzkiste")
    holzkiste = ist_holzkiste(bezeichnung) if _leer(holz_roh) else parse_bool(holz_roh)
    jahrgang = _jahrgang_aus_spalte(zeile.get("jahrgang"), kontext)
    if jahrgang is None:
        jahrgang = parse_jahrgang(bezeichnung)
    ek_preis = parse_dezimal(zeile.get("ek_preis"))
    if ek_preis is not None and ek_preis == 0:
        logger.debug("%s: EK-Preis 0,00 gilt als nicht gepflegt", kontext)
        ek_preis = None

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
        ek_preis=ek_preis,
        vk_preis_1=parse_dezimal(zeile.get("vk_preis_1")),
        einheit=einheit,
        matchcode=als_text(zeile.get("matchcode")),
        aktiv=_aktiv(zeile),
    )


def _name_mit_ersatz(zeile: Mapping[str, object], kontext: str) -> tuple[str, str | None]:
    """``name`` (Lexware "Firma") ist Pflicht; ist er leer, rückt der Personenname
    (``vorname`` + ``nachname``, Lexware "Vorname"/"Name") nach, danach ``name2`` (Zusatz)."""
    name = als_text(zeile.get("name"))
    name2 = als_text(zeile.get("name2"))
    if name is None:
        person = " ".join(t for t in (als_text(zeile.get("vorname")), als_text(zeile.get("nachname"))) if t)
        if person:
            logger.debug("%s: 'name' (Firma) leer, verwende Personenname (%s)", kontext, person)
            return person, name2
    if name is None and name2 is not None:
        logger.info("%s: 'name' leer, verwende 'name2' (%s)", kontext, name2)
        return name2, None
    if name is None:
        raise StammdatenFehler(f"{kontext}: Pflichtfeld 'name' ist leer")
    return name, name2


def _strasse_mit_hausnr(zeile: Mapping[str, object]) -> str | None:
    strasse = als_text(zeile.get("strasse"))
    hausnr = als_text(zeile.get("hausnr"))
    if strasse and hausnr and not strasse.endswith(hausnr):
        return f"{strasse} {hausnr}"
    return strasse or hausnr


def zeile_zu_kunde(firma_id: str, zeile: Mapping[str, object]) -> Kunde:
    """Zeile mit unseren Feldnamen -> :class:`Kunde` (Straße + HausNr, PLZ bereinigt, E-Mail, Bemerkung, USt-ID)."""
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
        strasse=_strasse_mit_hausnr(zeile),
        plz=als_text(zeile.get("plz")),
        ort=als_text(zeile.get("ort")),
        land=als_text(zeile.get("land")),
        preisgruppe=parse_int(zeile.get("preisgruppe")),
        matchcode=als_text(zeile.get("matchcode")),
        aktiv=_aktiv(zeile),
        email=als_text(zeile.get("email")),
        bemerkung=als_text(zeile.get("bemerkung")),
        ust_id=als_text(zeile.get("ust_id")),
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
        land=als_text(zeile.get("land")),
        matchcode=als_text(zeile.get("matchcode")),
        aktiv=_aktiv(zeile),
        email=als_text(zeile.get("email")),
        bemerkung=als_text(zeile.get("bemerkung")),
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


_UMSCHRIFT = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss", "Ä": "ae", "Ö": "oe", "Ü": "ue"})
NEGATIONS_PRAEFIX = "!"


def _spaltenname_roh(name: object) -> str:
    text = str(name or "").replace("﻿", "").strip().strip('"').strip("'").strip()
    text = unicodedata.normalize("NFKC", text)
    return re.sub(r"\s+", " ", text).strip()


def spaltenschluessel(name: object) -> tuple[str, str]:
    """Zwei Vergleichsschlüssel für einen Spaltennamen (tolerantes Kopfzeilen-Matching).

    1. umschrieben: Umlaute/ß als ae/oe/ue/ss, Kleinschreibung ("Straße" -> "strasse"),
    2. ASCII: alle Nicht-ASCII-Zeichen entfernt ("Straße" -> "strae", "Straáe" -> "strae",
       "steuerbare Ums„tze" -> "steuerbare umstze", "õ13b" -> "13b").

    Zwei Namen passen, wenn einer der beiden Schlüssel übereinstimmt.
    """
    roh = _spaltenname_roh(name)
    umschrieben = roh.translate(_UMSCHRIFT).lower()
    ascii_form = "".join(c for c in roh if ord(c) < 128)
    ascii_form = re.sub(r"\s+", " ", ascii_form).strip().lower()
    return umschrieben, ascii_form


def _normalisiere_spaltenname(name: object) -> str:
    """Umschriebener Schlüssel (für Meldungen und einfache Vergleiche)."""
    return spaltenschluessel(name)[0]


def _spalten_index(kopf: list[tuple[str, str]], gesucht: tuple[str, str]) -> list[int]:
    """Alle Kopfpositionen, die zum gesuchten Schlüsselpaar passen (umschrieben vor ASCII)."""
    treffer = [i for i, k in enumerate(kopf) if k[0] and k[0] == gesucht[0]]
    if not treffer and gesucht[1]:
        treffer = [i for i, k in enumerate(kopf) if k[1] and k[1] == gesucht[1]]
    return treffer


def _trennzeichen(wert: str) -> str:
    if wert.lower() in {"tab", "\\t", "tabulator"}:
        return "\t"
    if len(wert) != 1:
        raise StammdatenFehler(f"Ungültiges CSV-Trennzeichen {wert!r} (genau ein Zeichen erwartet)")
    return wert


_KODIERUNG_KANDIDATEN = ("utf-8-sig", "cp1252", "cp850")


def _kodierung_norm(kodierung: str) -> str:
    return kodierung.lower().replace("-", "").replace("_", "")


# Lexware schreibt die Feldnamen des Kunden-Exports in der DOS-Codepage, die Datenzeilen in cp1252.
# TODO-PRUEFEN: Am echten Kunden-Export prüfen, ob die Kopfzeile tatsächlich cp850 ist (Mojibake "Straáe",
# "Ums„tze", "õ13b" beim Lesen als cp1252) und ob "Länderkürzel" (ü = 0x81) darin vorkommt; ggf. die
# Reihenfolge der Rückfall-Kodierungen anpassen.
_DOS_KODIERUNG = "cp850"


def _kopfzeile_getrennt(daten: bytes) -> tuple[bytes, bytes, bytes] | None:
    """(Kopfzeile, Zeilenende, Datenzeilen) oder ``None``, wenn es nur eine Zeile gibt."""
    index = daten.find(b"\n")
    if index < 0:
        return None
    kopf = daten[:index]
    ende = b"\n"
    if kopf.endswith(b"\r"):
        kopf, ende = kopf[:-1], b"\r\n"
    return kopf, ende, daten[index + 1 :]


def _lies_text(pfad: Path, kodierung: str) -> str:
    """Dekodiert die Datei: BOM -> utf-8-sig; sonst ``kodierung``.

    Scheitert das, wird zuerst der Lexware-Sonderfall geprüft (Kopfzeile in cp850, Datenzeilen in
    ``kodierung``), danach werden utf-8-sig, cp1252 und cp850 für die ganze Datei versucht –
    jeweils mit Warnung im Log.
    """
    daten = pfad.read_bytes()
    if daten.startswith(codecs.BOM_UTF8):
        if _kodierung_norm(kodierung) not in {"utf8", "utf8sig"}:
            logger.warning(
                "%s beginnt mit einem UTF-8-BOM; lese als utf-8-sig statt %s", pfad.name, kodierung
            )
        return daten.decode("utf-8-sig")
    try:
        return daten.decode(kodierung)
    except LookupError as exc:
        raise StammdatenFehler(f"Unbekannte Kodierung '{kodierung}' für {pfad}") from exc
    except UnicodeDecodeError as exc:
        fehler = str(exc)
    geteilt = _kopfzeile_getrennt(daten)
    if geteilt is not None:
        kopf, ende, rest = geteilt
        try:
            rest_text = rest.decode(kodierung)
        except UnicodeDecodeError:
            rest_text = None
        if rest_text is not None:
            try:
                kopf_text = kopf.decode(_DOS_KODIERUNG)
            except UnicodeDecodeError:
                kopf_text = kopf.decode(kodierung, errors="replace")
            logger.warning(
                "%s: Kopfzeile nicht als %s lesbar (%s); Kopfzeile als %s, Datenzeilen als %s gelesen",
                pfad.name,
                kodierung,
                fehler,
                _DOS_KODIERUNG,
                kodierung,
            )
            return kopf_text + ende.decode("ascii") + rest_text
    versucht = [kodierung]
    for kandidat in _KODIERUNG_KANDIDATEN:
        if _kodierung_norm(kandidat) == _kodierung_norm(kodierung):
            continue
        try:
            text = daten.decode(kandidat)
        except UnicodeDecodeError:
            versucht.append(kandidat)
            continue
        logger.warning(
            "%s lässt sich nicht als %s lesen (%s); lese als %s", pfad.name, kodierung, fehler, kandidat
        )
        return text
    raise StammdatenFehler(f"{pfad}: mit keiner Kodierung lesbar (versucht: {', '.join(versucht)}; {fehler})")


def lies_csv_zeilen(pfad: Path, konfig: CsvQuellen, tabelle: str) -> list[tuple[int, dict[str, str | None]]]:
    """Liest eine Export-Datei und liefert (Zeilennummer, {unser Feldname: Text | None}).

    Spaltenzuordnung über ``konfig.spalten_<tabelle>`` (unser Feldname -> Exportspalte),
    Standard ist der Feldname selbst; ein ``!`` vor der Exportspalte negiert einen
    Wahrheitswert (``aktiv: "!Gesperrt"`` -> "J" wird "0", "N" wird "1", leer bleibt leer).
    Kopfzeilen werden tolerant verglichen (:func:`spaltenschluessel`). Unbekannte Spalten werden
    ignoriert, fehlende Pflichtspalten lösen :class:`StammdatenFehler` aus.
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
    kopf = [spaltenschluessel(s) for s in kopf_roh]
    zuordnung: dict[str, str] = getattr(konfig, f"spalten_{tabelle}", {}) or {}
    felder = feldnamen(tabelle)

    index: dict[str, int] = {}
    negiert: set[str] = set()
    fehlend: list[str] = []
    for feld in felder:
        spalte = str(zuordnung.get(feld, feld)).strip()
        if spalte.startswith(NEGATIONS_PRAEFIX):
            spalte = spalte[len(NEGATIONS_PRAEFIX) :].strip()
            negiert.add(feld)
        treffer = _spalten_index(kopf, spaltenschluessel(spalte))
        if len(treffer) > 1:
            logger.warning("%s: Spalte '%s' kommt mehrfach vor; die erste wird verwendet", pfad.name, spalte)
        if treffer:
            index[feld] = treffer[0]
            if kopf_roh[treffer[0]].strip().strip('"') != spalte:
                logger.debug("%s: Spalte %r tolerant auf %r zugeordnet", pfad.name, spalte, kopf_roh[treffer[0]])
        elif feld in PFLICHTFELDER[tabelle]:
            fehlend.append(f"{feld} (erwartete Spalte '{spalte}')")
    if fehlend:
        erwartet = ", ".join(str(zuordnung.get(f, f)).lstrip(NEGATIONS_PRAEFIX) for f in PFLICHTFELDER[tabelle])
        raise StammdatenFehler(
            f"{pfad}: Pflichtspalten fehlen: {', '.join(fehlend)}. "
            f"Erwartete Pflichtspalten: {erwartet}. Gefundene Spalten: {', '.join(kopf_roh)}"
        )
    unbekannt = [kopf_roh[i] for i in range(len(kopf)) if i not in index.values()]
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
            wert = roh.strip().strip('"').strip() or None
            if wert is not None and feld in negiert:
                wert = "0" if parse_bool(wert, standard=False) else "1"
            werte[feld] = wert
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
