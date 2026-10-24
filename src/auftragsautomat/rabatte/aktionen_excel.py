"""Aktionszeiträume aus der Excel in OneDrive lesen (``rabatte.aktionen_datei``, Blatt "Aktionen").

Quelle 1 der Aktionen laut ``docs/AUSBAUSTUFE_2.md`` Abschnitt 7. Die Excel wird über ein
:class:`~auftragsautomat.m365.basis.ExcelBackend` gelesen (Graph, COM oder Fake), nie geschrieben.

Ablauf von :func:`lese_aktionen`:

1. Benutzten Bereich des Blatts lesen (``lese_benutzten_bereich``; Graph liefert Datumswerte als
   Excel-Serienzahl, COM/Fake als ``date``/``datetime`` – beides wird verstanden).
2. Kopfzeile tolerant suchen: die erste Zeile, in der mindestens :data:`MINDEST_SPALTEN` Zellen auf
   eine konfigurierte Überschrift (``rabatte.spalten``, Vergleich casefold ohne Sonderzeichen) oder
   ein eingebautes Synonym (:data:`SYNONYME`) passen. Titelzeilen oberhalb und zusätzliche Spalten
   stören nicht.
3. Jede weitere nicht leere Zeile wird zu einer :class:`~auftragsautomat.models.Aktion`
   (``aktion_id`` und ``quelle`` = ``excel:<Blatt>:<Zeile>``). Datum aus ``date``/``datetime``,
   Serienzahl (``int``/``float``), "TT.MM.JJJJ" oder "JJJJ-MM-TT"; Prozent aus "10 %", "10", "0,1"
   bzw. ``0.1`` (Werte unter 1 gelten als Anteil); Artikelnummern kommagetrennt; Firma per ID,
   Kürzel, Name oder Lexware-Firmenname (``firmen``), sonst bleibt der Text stehen.
4. Zeilen ohne Von/Bis oder ohne Rabattangabe (Prozent, EUR oder Aktionspreis) ergeben einen
   :class:`~auftragsautomat.models.Pruefhinweis` (``FELD_FEHLT``, WARNUNG) und werden übersprungen –
   kein Abbruch. Fehlende Pflichtspalten (Von, Bis, Rabatt) ergeben einen FEHLER-Hinweis und eine
   leere Liste.

:func:`aktion_aus_werten` ist der gemeinsame Zeilenparser, den auch ``rabattliste_pdf`` benutzt.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, Field

from auftragsautomat.config import DateiRefKonfig, Firma, RabatteKonfig
from auftragsautomat.extraktion.zahlen import glatt, parse_datum, parse_dezimal
from auftragsautomat.m365.basis import (
    ExcelBackend,
    M365Fehler,
    M365NichtGefunden,
    parse_adresse,
    serienzahl_zu_datum,
    spaltenbuchstabe,
)
from auftragsautomat.matching.aliase import normalisiere_name
from auftragsautomat.models import Aktion, Pruefcode, Pruefhinweis, Schweregrad

__all__ = [
    "FELDER",
    "MINDEST_SPALTEN",
    "PFLICHTFELDER",
    "RABATTFELDER",
    "STANDARD_BLATT",
    "SYNONYME",
    "AktionenLadeErgebnis",
    "aktion_aus_werten",
    "aktion_aus_zeile",
    "betrag_aus",
    "datum_aus",
    "erkenne_spalten",
    "finde_kopfzeile",
    "firma_id_aus",
    "lese_aktionen",
    "normalisiere_kopf",
    "nummern_aus",
    "prozent_aus",
    "text_aus",
]

logger = logging.getLogger(__name__)

STANDARD_BLATT = "Aktionen"
#: So viele erkannte Überschriften braucht eine Zeile, um als Kopfzeile zu gelten.
MINDEST_SPALTEN = 3
# TODO-PRUEFEN: Aufbau der echten Aktionszeiträume-Excel in OneDrive (Blattname, Überschriften laut
# rabatte.spalten, Datumszellen als echte Datumswerte – Graph liefert dann Serienzahlen – und ob
# Prozentzellen als 0,1 (Anteil, Prozentformat) oder als 10 geführt werden) am echten Tenant prüfen.

#: Alle Felder, die eine Spalte liefern kann (Schlüssel wie in ``RabatteKonfig.spalten``).
FELDER: tuple[str, ...] = (
    "kunde_kurz",
    "kundennummern",
    "lieferant_kurz",
    "artikel_text",
    "artikelnummern",
    "von",
    "bis",
    "rabatt_prozent",
    "rabatt_eur",
    "aktionspreis",
    "bezeichnung",
    "bemerkung",
    "firma_id",
    "aktiv",
)
#: Ohne diese Spalten ist das Blatt nicht lesbar.
PFLICHTFELDER: tuple[str, ...] = ("von", "bis")
#: Mindestens eine dieser Spalten muss vorhanden sein.
RABATTFELDER: tuple[str, ...] = ("rabatt_prozent", "rabatt_eur", "aktionspreis")

#: Eingebaute Synonyme je Feld (bereits normalisiert, siehe :func:`normalisiere_kopf`). Die
#: konfigurierten Überschriften aus ``rabatte.spalten`` haben Vorrang.
SYNONYME: dict[str, tuple[str, ...]] = {
    "kunde_kurz": ("kunde", "kunden", "kundengruppe", "kundenkurz", "region", "kette"),
    "kundennummern": ("kundennummer", "kundennummern", "kundennr", "kdnr", "kundenno"),
    "lieferant_kurz": ("lieferant", "lieferanten", "weingut", "produzent", "erzeuger", "lieferantkurz"),
    "artikel_text": ("artikel", "artikelbezeichnung", "bezeichnung", "wein", "produkt", "artikeltext"),
    "artikelnummern": ("artikelnummer", "artikelnummern", "artnr", "artikelnr", "artno", "nummer"),
    "von": ("von", "ab", "beginn", "start", "gueltigab", "gueltigvon", "zeitraumvon", "aktionsbeginn"),
    "bis": ("bis", "ende", "gueltigbis", "zeitraumbis", "aktionsende"),
    "rabatt_prozent": ("rabattprozent", "rabatt", "prozent", "rabattin", "nachlass", "nachlassprozent"),
    "rabatt_eur": ("rabatteur", "rabatteuro", "rabattbetrag", "betrag", "nachlasseur", "rabattjeflasche"),
    "aktionspreis": ("aktionspreis", "aktionsvk", "sonderpreis", "aktionspreiseur", "preis"),
    "bezeichnung": ("aktion", "aktionsname", "name", "titel", "aktionsbezeichnung"),
    "bemerkung": ("bemerkung", "bemerkungen", "hinweis", "notiz", "kommentar", "info"),
    "firma_id": ("firma", "mandant", "gesellschaft", "firmaid"),
    "aktiv": ("aktiv", "gueltig", "status"),
}

_INAKTIV_WERTE = frozenset({"nein", "0", "false", "falsch", "inaktiv", "aus", "no", "n"})
_UMLAUTE = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})
_NUMMERN_TRENNER = re.compile(r"[,;|\n]+")


class AktionenLadeErgebnis(BaseModel):
    """Ergebnis von :func:`lese_aktionen`: Aktionen plus Hinweise zu übersprungenen Zeilen."""

    aktionen: list[Aktion] = Field(default_factory=list)
    hinweise: list[Pruefhinweis] = Field(default_factory=list)
    blatt: str | None = None
    kopfzeile: int | None = None  # Excel-Zeile (1-basiert) der erkannten Kopfzeile
    spalten: dict[str, str] = Field(default_factory=dict)  # Feld -> Spaltenbuchstabe

    @property
    def fehler(self) -> list[Pruefhinweis]:
        return [h for h in self.hinweise if h.schweregrad == Schweregrad.FEHLER]


# ---------------------------------------------------------------------------
# Zellwerte lesen
# ---------------------------------------------------------------------------


def normalisiere_kopf(text: Any) -> str:
    """Vergleichsform einer Überschrift: casefold, Umlaute aufgelöst, ``%`` -> "prozent", ``€`` -> "eur",
    alle anderen Nicht-Alphanumerika entfernt ("Rabatt %" -> "rabattprozent", "Art.-Nr." -> "artnr")."""
    if text is None:
        return ""
    t = str(text).casefold().translate(_UMLAUTE)
    t = t.replace("%", " prozent ").replace("€", " eur ")
    t = unicodedata.normalize("NFKD", t)
    t = "".join(z for z in t if not unicodedata.combining(z))
    return re.sub(r"[^a-z0-9]+", "", t)


def text_aus(wert: Any) -> str | None:
    """Zellwert als bereinigter Text; leer -> ``None``. Ganzzahlige Zahlen ohne ".0"."""
    if wert is None or isinstance(wert, bool):
        return None
    if isinstance(wert, float) and wert.is_integer():
        wert = int(wert)
    elif isinstance(wert, Decimal) and wert == wert.to_integral_value():
        wert = int(wert)
    elif isinstance(wert, datetime):
        wert = wert.strftime("%d.%m.%Y %H:%M") if (wert.hour or wert.minute) else wert.strftime("%d.%m.%Y")
    elif isinstance(wert, date):
        wert = wert.strftime("%d.%m.%Y")
    text = " ".join(str(wert).split())
    return text or None


def datum_aus(wert: Any) -> date | None:
    """Datum aus ``date``/``datetime``, Excel-Serienzahl (``int``/``float``/``Decimal``), "TT.MM.JJJJ",
    "JJJJ-MM-TT" oder einer Serienzahl als Text; sonst ``None``."""
    if wert is None or isinstance(wert, bool):
        return None
    if isinstance(wert, datetime):
        return wert.date()
    if isinstance(wert, date):
        return wert
    if isinstance(wert, int | float | Decimal):
        serie = serienzahl_zu_datum(wert)
        if isinstance(serie, datetime):
            return serie.date()
        return serie
    text = str(wert).strip()
    if not text:
        return None
    datum = parse_datum(text)
    if datum is not None:
        return datum
    if re.fullmatch(r"\d{4,6}(?:[.,]\d+)?", text):
        serie = serienzahl_zu_datum(text)
        if isinstance(serie, datetime):
            return serie.date()
        return serie
    return None


def _decimal_aus(wert: Any) -> Decimal | None:
    if wert is None or isinstance(wert, bool):
        return None
    if isinstance(wert, Decimal):
        return wert
    if isinstance(wert, int | float):
        try:
            return Decimal(str(wert))
        except InvalidOperation:
            return None
    return parse_dezimal(str(wert))


def prozent_aus(wert: Any) -> Decimal | None:
    """Prozentsatz aus "10 %", "10", "10,5", "0,1" oder ``0.1``; Werte unter 1 (ohne ``%``) gelten als
    Anteil (0,1 -> 10 %). Negative Werte werden positiv gelesen; über 100 oder 0 -> ``None``."""
    if wert is None or isinstance(wert, bool):
        return None
    mit_prozentzeichen = isinstance(wert, str) and "%" in wert
    zahl = _decimal_aus(wert)
    if zahl is None or not zahl.is_finite():
        return None
    zahl = abs(zahl)
    if zahl == 0:
        return None
    if zahl < 1 and not mit_prozentzeichen:
        zahl = zahl * 100
    if zahl > 100:
        return None
    return glatt(zahl.quantize(Decimal("0.01")))


def betrag_aus(wert: Any) -> Decimal | None:
    """Geldbetrag (EUR je Flasche) aus Zahl oder Text ("1,00 €"); 0 oder negativ -> ``None``."""
    zahl = _decimal_aus(wert)
    if zahl is None or not zahl.is_finite():
        return None
    zahl = abs(zahl).quantize(Decimal("0.01"))
    return zahl if zahl > 0 else None


def nummern_aus(wert: Any) -> list[str]:
    """Kommagetrennte Nummern ("1643, 957"; auch ";" oder Zeilenumbruch); Zahlen ohne ".0"."""
    if wert is None or isinstance(wert, bool):
        return []
    if isinstance(wert, int | float | Decimal):
        text = text_aus(wert) or ""
        return [text] if text else []
    nummern: list[str] = []
    for teil in _NUMMERN_TRENNER.split(str(wert)):
        teil = teil.strip().strip("'\"")
        if re.fullmatch(r"\d+\.0+", teil):
            teil = teil.split(".")[0]
        if teil and teil not in nummern:
            nummern.append(teil)
    return nummern


def _aktiv_aus(wert: Any) -> bool:
    if wert is None:
        return True
    if isinstance(wert, bool):
        return wert
    if isinstance(wert, int | float | Decimal):
        return wert != 0
    return str(wert).strip().casefold() not in _INAKTIV_WERTE


def firma_id_aus(text: Any, firmen: Mapping[str, Firma] | None) -> tuple[str | None, str | None]:
    """Firmen-ID zu einem Zelltext (ID, Kürzel, Name oder Lexware-Firmenname).

    Liefert ``(firma_id, hinweis)``: ohne Text ``(None, None)`` (= alle Firmen); eindeutig gefunden
    ``(id, None)``; mehrdeutiges Kürzel (z. B. "WIK" für WIK FH und WIK LEH) oder unbekannter Text
    ``(text, hinweis)`` – der Text bleibt erhalten und wird beim Anwenden gegen das Kürzel verglichen.
    """
    roh = text_aus(text)
    if roh is None:
        return None, None
    if not firmen:
        return roh, None
    schluessel = roh.casefold()
    for firma_id in firmen:
        if firma_id.casefold() == schluessel:
            return firma_id, None
    norm = normalisiere_name(roh)
    treffer_kurz = [fid for fid, f in firmen.items() if normalisiere_name(f.kurz) == norm]
    if len(treffer_kurz) == 1:
        return treffer_kurz[0], None
    treffer_name = [
        fid
        for fid, f in firmen.items()
        if norm and norm in {normalisiere_name(f.name), normalisiere_name(f.lexware_firmenname)}
    ]
    if len(treffer_name) == 1:
        return treffer_name[0], None
    if treffer_kurz or treffer_name:
        kandidaten = ", ".join(treffer_kurz or treffer_name)
        return roh, f"Firma '{roh}' ist nicht eindeutig ({kandidaten}); die Aktion gilt für alle davon"
    return roh, f"Firma '{roh}' ist nicht konfiguriert; die Aktion gilt nur, wenn das Kürzel passt"


# ---------------------------------------------------------------------------
# Kopfzeile / Spalten
# ---------------------------------------------------------------------------


def _zuordnungen(konfig: RabatteKonfig) -> list[tuple[str, str, bool]]:
    """(normalisierte Überschrift, Feld, aus Konfiguration?) – konfigurierte Namen zuerst."""
    paare: list[tuple[str, str, bool]] = []
    for feld, ueberschrift in konfig.spalten.items():
        if feld not in FELDER:
            logger.warning("rabatte.spalten: unbekanntes Feld %r wird ignoriert", feld)
            continue
        norm = normalisiere_kopf(ueberschrift)
        if norm:
            paare.append((norm, feld, True))
    for feld, synonyme in SYNONYME.items():
        for synonym in synonyme:
            paare.append((synonym, feld, False))
    return paare


def erkenne_spalten(zeile: list[Any], konfig: RabatteKonfig) -> dict[str, int]:
    """Ordnet die Zellen einer (Kopf-)Zeile den Feldern zu: ``{feld: spaltenindex (0-basiert)}``.
    Jede Spalte höchstens einem Feld, jedes Feld höchstens einer Spalte (konfigurierte Namen vor
    Synonymen, links vor rechts)."""
    paare = _zuordnungen(konfig)
    koepfe = [normalisiere_kopf(z) for z in zeile]
    spalten: dict[str, int] = {}
    belegt: set[int] = set()
    for konfiguriert in (True, False):
        for norm, feld, aus_konfig in paare:
            if aus_konfig != konfiguriert or feld in spalten:
                continue
            for index, kopf in enumerate(koepfe):
                if index in belegt or not kopf:
                    continue
                if kopf == norm:
                    spalten[feld] = index
                    belegt.add(index)
                    break
    return spalten


def finde_kopfzeile(
    werte: list[list[Any]], konfig: RabatteKonfig, *, mindestens: int = MINDEST_SPALTEN
) -> tuple[int, dict[str, int]] | None:
    """Erste Zeile (0-basierter Index) mit mindestens ``mindestens`` erkannten Überschriften."""
    for index, zeile in enumerate(werte):
        spalten = erkenne_spalten(list(zeile), konfig)
        if len(spalten) >= mindestens:
            return index, spalten
    return None


# ---------------------------------------------------------------------------
# Zeilen -> Aktion
# ---------------------------------------------------------------------------


def _hinweis(
    grad: Schweregrad, text: str, *, quelle: str, feld: str | None = None, code: Pruefcode = Pruefcode.FELD_FEHLT
) -> Pruefhinweis:
    details = {"quelle": quelle}
    if feld:
        details["feld"] = feld
    return Pruefhinweis(code=code, schweregrad=grad, text=text, details=details)


def aktion_aus_werten(
    werte: Mapping[str, Any],
    *,
    aktion_id: str,
    quelle: str,
    konfig: RabatteKonfig,
    firmen: Mapping[str, Firma] | None = None,
    vorgaben: Mapping[str, Any] | None = None,
    konfidenz: float | None = None,
) -> tuple[Aktion | None, list[Pruefhinweis]]:
    """Baut aus Feldwerten (Schlüssel wie :data:`FELDER`) eine Aktion.

    ``vorgaben`` füllen leere Felder (z. B. Kunde/Lieferant/Zeitraum vom Blatt- oder Seitenkopf).
    Liefert ``(Aktion | None, Hinweise)``: ohne Von/Bis oder ohne Rabattangabe ``None`` plus
    WARNUNG; eine nicht eindeutige Firma ergibt nur einen INFO-Hinweis.
    """
    hinweise: list[Pruefhinweis] = []
    daten: dict[str, Any] = dict(vorgaben or {})
    for feld, wert in werte.items():
        if wert is not None and not (isinstance(wert, str) and not wert.strip()):
            daten[feld] = wert

    von = datum_aus(daten.get("von"))
    bis = datum_aus(daten.get("bis"))
    if von is None or bis is None:
        fehlend = [f for f, d in (("Von", von), ("Bis", bis)) if d is None]
        hinweise.append(
            _hinweis(
                Schweregrad.WARNUNG,
                f"{quelle}: {' und '.join(fehlend)} fehlt oder ist kein Datum – Zeile übersprungen",
                quelle=quelle,
                feld="/".join(f.lower() for f in fehlend),
            )
        )
        return None, hinweise
    if bis < von:
        hinweise.append(
            _hinweis(
                Schweregrad.WARNUNG,
                f"{quelle}: Bis ({bis:%d.%m.%Y}) liegt vor Von ({von:%d.%m.%Y}) – Zeitraum wurde umgedreht",
                quelle=quelle,
                feld="von/bis",
            )
        )
        von, bis = bis, von

    rabatt_prozent = prozent_aus(daten.get("rabatt_prozent"))
    rabatt_eur = betrag_aus(daten.get("rabatt_eur"))
    aktionspreis = betrag_aus(daten.get("aktionspreis"))
    if rabatt_prozent is None and rabatt_eur is None and aktionspreis is None:
        gefuellt = [f for f in RABATTFELDER if text_aus(daten.get(f))]
        grund = (
            f"Rabattangabe {', '.join(text_aus(daten.get(f)) or '' for f in gefuellt)!r} nicht lesbar"
            if gefuellt
            else "keine Rabattangabe (Rabatt %, Rabatt EUR oder Aktionspreis)"
        )
        hinweise.append(
            _hinweis(Schweregrad.WARNUNG, f"{quelle}: {grund} – Zeile übersprungen", quelle=quelle, feld="rabatt")
        )
        return None, hinweise

    firma_id, firma_hinweis = firma_id_aus(daten.get("firma_id"), firmen)
    if firma_hinweis:
        hinweise.append(_hinweis(Schweregrad.INFO, f"{quelle}: {firma_hinweis}", quelle=quelle, feld="firma"))

    kundennummern = nummern_aus(daten.get("kundennummern"))
    kunde_kurz = text_aus(daten.get("kunde_kurz"))
    if kunde_kurz and not kundennummern and re.fullmatch(r"[\d ,;]+", kunde_kurz):
        # Reine Nummern in der Kundenspalte sind Lexware-Kundennummern
        kundennummern = nummern_aus(kunde_kurz)
        kunde_kurz = None

    aktion = Aktion(
        aktion_id=aktion_id,
        bezeichnung=text_aus(daten.get("bezeichnung")),
        firma_id=firma_id,
        kunde_kurz=kunde_kurz,
        kundennummern=kundennummern,
        lieferant_kurz=text_aus(daten.get("lieferant_kurz")),
        artikelnummern=nummern_aus(daten.get("artikelnummern")),
        artikel_text=text_aus(daten.get("artikel_text")),
        von=von,
        bis=bis,
        rabatt_prozent=rabatt_prozent,
        rabatt_eur=rabatt_eur,
        aktionspreis=aktionspreis,
        bezug=konfig.bezug,
        quelle=quelle,
        konfidenz=konfidenz,
        bemerkung=text_aus(daten.get("bemerkung")),
        aktiv=_aktiv_aus(daten.get("aktiv")),
    )
    return aktion, hinweise


def aktion_aus_zeile(
    zeile: list[Any],
    spalten: Mapping[str, int],
    *,
    aktion_id: str,
    quelle: str,
    konfig: RabatteKonfig,
    firmen: Mapping[str, Firma] | None = None,
    vorgaben: Mapping[str, Any] | None = None,
    konfidenz: float | None = None,
) -> tuple[Aktion | None, list[Pruefhinweis]]:
    """Wie :func:`aktion_aus_werten`, liest die Werte aber über die Spaltenzuordnung aus einer Zeile."""
    werte = {feld: (zeile[index] if index < len(zeile) else None) for feld, index in spalten.items()}
    return aktion_aus_werten(
        werte,
        aktion_id=aktion_id,
        quelle=quelle,
        konfig=konfig,
        firmen=firmen,
        vorgaben=vorgaben,
        konfidenz=konfidenz,
    )


def _zeile_leer(zeile: list[Any]) -> bool:
    return all(text_aus(z) is None for z in zeile)


# ---------------------------------------------------------------------------
# Öffentlich
# ---------------------------------------------------------------------------


def lese_aktionen(
    excel: ExcelBackend,
    ref: DateiRefKonfig,
    konfig: RabatteKonfig,
    *,
    firmen: Mapping[str, Firma] | None = None,
) -> AktionenLadeErgebnis:
    """Liest alle Aktionen aus dem Blatt ``ref.blatt`` (Standard "Aktionen") der Aktionszeiträume-Excel.

    Fehlt das Blatt und hat die Mappe genau ein Blatt, wird dieses genommen (INFO-Hinweis). Lesefehler
    des Backends (``M365Fehler``) werden als FEHLER-Hinweis geliefert, nicht geworfen; der Aufrufer
    (:func:`~auftragsautomat.rabatte.anwenden.alle_aktionen`) protokolliert sie.
    """
    ergebnis = AktionenLadeErgebnis(blatt=ref.blatt or STANDARD_BLATT)
    blatt = ergebnis.blatt or STANDARD_BLATT
    try:
        namen = excel.blattnamen(ref)
        if blatt not in namen:
            if len(namen) == 1:
                ergebnis.hinweise.append(
                    _hinweis(
                        Schweregrad.INFO,
                        f"Aktionen-Excel: Blatt '{blatt}' fehlt, verwende das einzige Blatt '{namen[0]}'",
                        quelle=f"excel:{blatt}",
                        feld="blatt",
                    )
                )
                blatt = namen[0]
                ergebnis.blatt = blatt
            else:
                ergebnis.hinweise.append(
                    _hinweis(
                        Schweregrad.FEHLER,
                        f"Aktionen-Excel: Blatt '{blatt}' nicht vorhanden (vorhanden: {', '.join(namen) or '-'})",
                        quelle=f"excel:{blatt}",
                        feld="blatt",
                    )
                )
                return ergebnis
        adresse, werte = excel.lese_benutzten_bereich(ref, blatt)
    except M365NichtGefunden as exc:
        ergebnis.hinweise.append(
            _hinweis(Schweregrad.FEHLER, f"Aktionen-Excel nicht gefunden: {exc}", quelle=f"excel:{blatt}")
        )
        return ergebnis
    except M365Fehler as exc:
        ergebnis.hinweise.append(
            _hinweis(Schweregrad.FEHLER, f"Aktionen-Excel nicht lesbar: {exc}", quelle=f"excel:{blatt}")
        )
        return ergebnis

    zeile_offset = 1
    try:
        if adresse:
            zeile_offset = parse_adresse(adresse).zeile_von
    except ValueError:
        logger.debug("Adresse des benutzten Bereichs %r nicht lesbar; nehme Zeile 1 an", adresse)

    werte = [list(z) for z in werte]
    kopf = finde_kopfzeile(werte, konfig)
    if kopf is None:
        erwartet = ", ".join(konfig.spalten.values())
        ergebnis.hinweise.append(
            _hinweis(
                Schweregrad.FEHLER,
                f"Aktionen-Excel, Blatt '{blatt}': keine Kopfzeile mit mindestens {MINDEST_SPALTEN} der "
                f"Überschriften gefunden (erwartet z. B. {erwartet})",
                quelle=f"excel:{blatt}",
                feld="kopfzeile",
            )
        )
        return ergebnis
    kopf_index, spalten = kopf
    ergebnis.kopfzeile = zeile_offset + kopf_index
    ergebnis.spalten = {feld: spaltenbuchstabe(index + 1) for feld, index in spalten.items()}

    fehlende = [f for f in PFLICHTFELDER if f not in spalten]
    if not any(f in spalten for f in RABATTFELDER):
        fehlende.append("rabatt")
    if fehlende:
        namen_fehlend = ", ".join(konfig.spalten.get(f, f) for f in fehlende)
        ergebnis.hinweise.append(
            _hinweis(
                Schweregrad.FEHLER,
                f"Aktionen-Excel, Blatt '{blatt}', Kopfzeile {ergebnis.kopfzeile}: Pflichtspalten fehlen "
                f"({namen_fehlend}); keine Aktion gelesen",
                quelle=f"excel:{blatt}:{ergebnis.kopfzeile}",
                feld=",".join(fehlende),
            )
        )
        return ergebnis

    for index in range(kopf_index + 1, len(werte)):
        zeile = werte[index]
        if _zeile_leer(zeile):
            continue
        zeilen_nr = zeile_offset + index
        quelle = f"excel:{blatt}:{zeilen_nr}"
        aktion, hinweise = aktion_aus_zeile(
            zeile, spalten, aktion_id=quelle, quelle=quelle, konfig=konfig, firmen=firmen
        )
        ergebnis.hinweise.extend(hinweise)
        if aktion is not None:
            ergebnis.aktionen.append(aktion)

    logger.info(
        "Aktionen-Excel, Blatt '%s': %d Aktionen gelesen, %d Hinweise (Kopfzeile %s, Spalten %s)",
        blatt,
        len(ergebnis.aktionen),
        len(ergebnis.hinweise),
        ergebnis.kopfzeile,
        ergebnis.spalten,
    )
    for hinweis in ergebnis.hinweise:
        if hinweis.schweregrad != Schweregrad.INFO:
            logger.warning("%s", hinweis.text)
    return ergebnis
