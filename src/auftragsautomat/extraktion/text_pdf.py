"""Extraktion von Bestellungen aus Text-PDFs mit pdfplumber.

Strategie (robust gegen unterschiedliche Layouts):

1. **Tabellen zuerst**: ``page.extract_tables()`` liefert bei Tabellen mit Linien
   saubere Zellen. Die Kopfzeile wird über Schlüsselwörter (Pos, Menge, Einheit,
   Artikel/Bezeichnung, Jahrgang, Einzelpreis/EK, Gesamt, Art.-Nr., Hinweis) den
   Feldern zugeordnet; ohne erkennbare Kopfzeile greift eine Spaltenheuristik
   (Zahlen-, Preis- und Textspalten).
2. **Zeilenheuristik**: Finden sich keine Tabellenpositionen, werden die Textzeilen
   (``extract_text`` und ``extract_text(layout=True)``) mit einem Positions-Regex
   gelesen: ``[Pos] Menge [Einheit] Bezeichnung [à] [Einzelpreis] [Gesamt]``. Eine
   Kopfzeile im Text ("Pos Art.-Nr. Menge ...") steuert die Spaltenreihenfolge.
3. **Kopffelder** per Schlüsselwort-Regex (Kunde, Kundennummer, Bestell-Nr.,
   Ihre Bestellung, Beleg-Nr., Datum, Liefertermin/Liefertag, Lieferadresse,
   Lieferant/Weingut, "Bestellung ... an"). Der Absenderblock (Name, Straße,
   PLZ Ort) wird als Kunde gelesen, sofern er nicht eine unserer eigenen Firmen ist.
4. **Summenvermerke** ("Gesamt: 97 Fl.", "Summe 180 R", "180 Fl") per Regex.

Eingangstyp: Standard ist A (Kundenbestellung). B (eigene Bestellung an das
Weingut) wird nur bei eindeutigen Hinweisen gesetzt: "Bestellung ... an <Weingut>",
"Lieferant:" zusammen mit "Kunde:", oder unsere eigene Firma als Absenderblock.

Erweiterungen für die echten Belegformate (Ausbaustufe 2, ``docs/AUSBAUSTUFE_2.md`` Abschnitt 4):

* **Verpackungseinheiten**: Spalten "Bestellmenge in Bestell-Einheit" × "Einheiten pro
  Bestell-Einheit" (SAP-Bestellung) bzw. "Menge" in KOA/Kolli × "Inhalt" (Lagerbestellung)
  werden in Flaschen umgerechnet: ``menge = menge_ve * ve_inhalt``; die VE-Angabe bleibt in
  ``hinweis`` und ``rohtext`` erhalten. Eine "Gesamt"-Spalte mit Flaschenzahl wird gegengeprüft
  (``SUMME_ABWEICHUNG``).
* **Artikelnummern**: "Lief. ANr." / "Lieferanten-Artikelnummer" ist unsere Artikelnummer
  (``artikelnummer``); steht daneben eine allgemeine "Artikelnummer" – oder eine GTIN-Spalte –
  ist diese die Nummer des Kunden (``kundenartikelnummer``). "ArtNr: 11081" als Folgezeile einer
  Position (Shop-Bestellung) ist ``artikelnummer``. GTIN/EAN -> ``gtin``.
* **Adressblöcke mit Überschrift** ("Rechnungsempfänger", "Warenempfänger", "Bestellempfänger",
  "Ladeadresse", "Anlieferadresse", "Rechnungs-/Lieferanschrift") -> Kunde, Lieferadresse,
  Ladeadresse, eigene Firma (``empfaenger_firma_text``). "Lieferanten-Nr." in einer
  Kundenbestellung ist UNSERE Nummer beim Kunden -> nur Hinweis (INFO), nie ``kundennummer_dokument``.
* **Lieferbedingung** ("Lieferbedingungen: FH / Frei Haus", "frei Haus", "ex cellar") und
  **Liefertag** aus "Lieferdatum"/"Liefertermin" (= Fixtermin bei LEH-Bestellungen).
* **Belegnummer vs. Bestellnummer**: Bei Kundenbestellungen (Typ A) ist die Bestellnummer des
  Kunden die Kundenbestellnummer; nennt das Dokument nur eine "Belegnummer", wird sie zusätzlich
  als Kundenbestellnummer übernommen (Hinweis INFO).
* **Speditionszeilen**: "DAA …" und Zeilen mit Verbrauchsteuernummer (DE + 11 Ziffern) zu 0,00
  bleiben Positionen mit ``hinweis`` "Speditionszeile" (das Matching überspringt sie).
* **Rabatte** je Position ("Rabatt 10 %", "-10%", "Aktion -10 %", Spalte "Rabatt") ->
  ``rabatt_prozent``/``rabatt_text``; **Aktionshinweis** auf Belegebene ("Aktion KW 45", "Promo",
  "Aktionszeitraum 01.11.-30.11.") -> ``aktionshinweis``; **Revised Order** ("Revised order",
  "Änderung zur Bestellung", "ersetzt Bestellung Nr. 123", "Storno + Neu") -> ``ist_revision``,
  ``revision_von`` und ``Pruefhinweis(REVISION, WARNUNG)``.

Grenzen: Durchgestrichenen Text kann pdfplumber nicht erkennen; ``durchgestrichen``
bleibt daher immer ``False`` (das leistet nur ``extraktion.vision``). Fehlende
Kopffelder ergeben ``Pruefhinweis(FELD_FEHLT, WARNUNG)``, keine Exception.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import pdfplumber

from auftragsautomat.config import Settings
from auftragsautomat.extraktion import zahlen
from auftragsautomat.matching import volumen
from auftragsautomat.models import (
    Eingangstyp,
    Extraktionsmethode,
    Pruefcode,
    Pruefhinweis,
    RohBestellung,
    RohPosition,
    Schweregrad,
    Summenvermerk,
)

__all__ = [
    "MIN_TEXT_ZEICHEN",
    "PdfInhalt",
    "RevisionTreffer",
    "TextPdfExtraktor",
    "TextPdfFehler",
    "finde_aktionshinweis",
    "finde_lieferbedingung",
    "finde_revision",
    "inhalt_aus_text",
    "ist_speditionszeile",
    "ist_text_pdf",
    "lese_kopffelder",
    "lese_pdf",
    "revisions_hinweis",
]

logger = logging.getLogger(__name__)

MIN_TEXT_ZEICHEN = 40  # alphanumerische Zeichen auf Seite 1, ab denen ein PDF als Text-PDF gilt


class TextPdfFehler(RuntimeError):
    """Die PDF-Datei kann nicht gelesen werden (defekt, verschlüsselt, kein PDF)."""


# ---------------------------------------------------------------------------
# PDF lesen
# ---------------------------------------------------------------------------


@dataclass
class PdfInhalt:
    """Rohinhalt eines PDFs: Text in zwei Varianten, Tabellen, Zeilen."""

    seiten: int
    text: str  # extract_text() aller Seiten, mit "\n" verbunden
    layout_text: str  # extract_text(layout=True) aller Seiten (oder leer)
    tabellen: list[list[list[str | None]]] = field(default_factory=list)
    zeilen: list[str] = field(default_factory=list)  # nicht-leere Zeilen aus ``text``
    segmente: list[str] = field(default_factory=list)  # Layout-Zeilen, an >=3 Leerzeichen geteilt
    segmente_seite1: list[str] = field(default_factory=list)
    # Seite 1 nach horizontaler Position in Spalten gruppiert (Absenderblock links, Kopffelder rechts)
    layout_spalten_seite1: list[list[str]] = field(default_factory=list)


def _layout_zeilen(layout_text: str) -> list[list[tuple[int, str]]]:
    """Zerlegt Layout-Text in Zeilen aus (Spaltenversatz, Segment); Segmente enden an >=3 Leerzeichen."""
    zeilen: list[list[tuple[int, str]]] = []
    for zeile in layout_text.splitlines():
        segmente: list[tuple[int, str]] = []
        for treffer in re.finditer(r"\S+(?:\s{1,2}\S+)*", zeile):
            text = re.sub(r"\s+", " ", treffer.group(0)).strip()
            if text:
                segmente.append((treffer.start(), text))
        zeilen.append(segmente)
    return zeilen


def _segmentiere(layout_text: str) -> list[str]:
    """Teilt Layout-Zeilen an Lücken von drei und mehr Leerzeichen (nebeneinander stehende Blöcke)."""
    segmente: list[str] = []
    for zeile in _layout_zeilen(layout_text):
        if not zeile:
            segmente.append("")
            continue
        segmente.extend(text for _, text in zeile)
    return segmente


def _layout_spalten(layout_text: str, toleranz: int = 4) -> list[list[str]]:
    """Gruppiert Segmente nach ihrem horizontalen Versatz zu Spalten (links nach rechts).

    Leere Zeilen bleiben als "" erhalten, damit Adressblöcke getrennt bleiben.
    """
    spalten: list[tuple[int, list[str]]] = []
    for zeile in _layout_zeilen(layout_text):
        getroffen: set[int] = set()
        for versatz, text in zeile:
            index = next((i for i, (v, _) in enumerate(spalten) if abs(v - versatz) <= toleranz), None)
            if index is None:
                spalten.append((versatz, []))
                index = len(spalten) - 1
            spalten[index][1].append(text)
            getroffen.add(index)
        for i, (_, zeilen) in enumerate(spalten):
            if i not in getroffen:
                zeilen.append("")
    return [zeilen for _, zeilen in sorted(spalten, key=lambda s: s[0])]


def lese_pdf(datei: Path) -> PdfInhalt:
    """Liest Text, Layout-Text und Tabellen aller Seiten. Wirft :class:`TextPdfFehler` bei defekter Datei."""
    datei = Path(datei)
    texte: list[str] = []
    layout_texte: list[str] = []
    tabellen: list[list[list[str | None]]] = []
    segmente_seite1: list[str] = []
    layout_spalten_seite1: list[list[str]] = []
    try:
        with pdfplumber.open(datei) as pdf:
            seiten = len(pdf.pages)
            for nr, seite in enumerate(pdf.pages, start=1):
                text = seite.extract_text() or ""
                texte.append(text)
                try:
                    layout = seite.extract_text(layout=True) or ""
                except Exception as exc:  # pragma: no cover - pdfminer-Sonderfälle
                    logger.debug("Layout-Text Seite %d nicht lesbar (%s), nutze Fließtext", nr, exc)
                    layout = text
                layout_texte.append(layout)
                if nr == 1:
                    segmente_seite1 = _segmentiere(layout)
                    layout_spalten_seite1 = _layout_spalten(layout)
                try:
                    for tabelle in seite.extract_tables() or []:
                        if tabelle and any(any(z for z in zeile if z) for zeile in tabelle):
                            tabellen.append(tabelle)
                except Exception as exc:  # pragma: no cover
                    logger.warning("Tabellenerkennung Seite %d fehlgeschlagen: %s", nr, exc)
    except TextPdfFehler:
        raise
    except Exception as exc:
        raise TextPdfFehler(f"PDF kann nicht gelesen werden: {datei} ({exc})") from exc

    text = "\n".join(texte)
    layout_text = "\n".join(layout_texte)
    zeilen = [z.strip() for z in text.splitlines() if z.strip()]
    return PdfInhalt(
        seiten=seiten,
        text=text,
        layout_text=layout_text,
        tabellen=tabellen,
        zeilen=zeilen,
        segmente=_segmentiere(layout_text),
        segmente_seite1=segmente_seite1,
        layout_spalten_seite1=layout_spalten_seite1,
    )


def inhalt_aus_text(text: str, layout_text: str | None = None) -> PdfInhalt:
    """Baut einen :class:`PdfInhalt` aus reinem Text (z. B. E-Mail-Text oder Tests ohne PDF)."""
    layout = layout_text if layout_text is not None else text
    return PdfInhalt(
        seiten=1,
        text=text,
        layout_text=layout,
        tabellen=[],
        zeilen=[z.strip() for z in text.splitlines() if z.strip()],
        segmente=_segmentiere(layout),
        segmente_seite1=_segmentiere(layout),
        layout_spalten_seite1=_layout_spalten(layout),
    )


def ist_text_pdf(datei: Path, *, min_zeichen: int = MIN_TEXT_ZEICHEN) -> bool:
    """True, wenn Seite 1 nennenswerten Text enthält (Textebene vorhanden).

    Reine Bild-PDFs (Scans) und nicht lesbare Dateien ergeben ``False``.
    """
    datei = Path(datei)
    if not datei.is_file():
        return False
    try:
        with pdfplumber.open(datei) as pdf:
            if not pdf.pages:
                return False
            text = pdf.pages[0].extract_text() or ""
    except Exception as exc:
        logger.warning("PDF nicht lesbar, behandle als Nicht-Text-PDF: %s (%s)", datei, exc)
        return False
    zeichen = sum(1 for c in text if c.isalnum())
    logger.debug("%s: %d alphanumerische Zeichen auf Seite 1", datei.name, zeichen)
    return zeichen >= min_zeichen


# ---------------------------------------------------------------------------
# Normalisierung
# ---------------------------------------------------------------------------


def _normalisiere(text: str | None) -> str:
    if not text:
        return ""
    t = text.replace("ß", "ss").replace("ä", "ae").replace("ö", "oe").replace("ü", "ue")
    t = t.replace("Ä", "ae").replace("Ö", "oe").replace("Ü", "ue")
    t = "".join(c for c in unicodedata.normalize("NFKD", t) if not unicodedata.combining(c))
    t = re.sub(r"[^a-z0-9]+", " ", t.casefold())
    return re.sub(r"\s+", " ", t).strip()


def _kopfzelle(text: str | None) -> str:
    """Normalisiert eine Kopfzelle: Umbrüche, Punkte, Striche, Währung entfernen ("Art.-Nr." -> "artnr")."""
    if not text:
        return ""
    t = re.sub(r"(€|\beur(?:o)?\b|\bin\b|\bnetto\b|\bbrutto\b)", " ", text, flags=re.I)
    t = _normalisiere(t)
    return t.replace(" ", "")


# ---------------------------------------------------------------------------
# Kopffelder
# ---------------------------------------------------------------------------

_WERT = r"(?P<wert>[A-Za-z0-9][A-Za-z0-9\-/_.]*)"
_KOPF_MUSTER: dict[str, re.Pattern[str]] = {
    "kundennummer": re.compile(
        r"\b(?:Kunden-?\s?(?:Nr|Nummer)|Kd\.?-?\s?Nr|Kundennr|Debitoren-?\s?(?:Nr|Nummer))\.?\s*:?\s*" + _WERT,
        re.I,
    ),
    "ihre_bestellnummer": re.compile(
        r"\b(?:Ihre\s+Bestellung(?:\s*-?\s*(?:Nr|Nummer)\.?)?|Ihre\s+Bestell-?\s?(?:Nr|Nummer)\.?"
        r"|Kunden-?\s?bestell-?\s?(?:Nr|Nummer)\.?|Ihre\s+Auftrags-?\s?(?:Nr|Nummer)\.?|Ihr\s+Zeichen)"
        r"\s*:?\s*" + _WERT,
        re.I,
    ),
    "bestellnummer": re.compile(
        r"\b(?:Bestell-?\s?(?:Nr|Nummer)|Bestellung\s*-?\s*(?:Nr|Nummer)|Auftrags-?\s?(?:Nr|Nummer)"
        r"|Auftrag\s+Nr|Order\s*-?\s*(?:No|Nr|Number))\.?\s*:?\s*" + _WERT,
        re.I,
    ),
    "belegnummer": re.compile(
        r"\b(?:Beleg-?\s?(?:Nr|Nummer)|Dokument(?:en)?-?\s?(?:Nr|Nummer)|Vorgangs-?\s?(?:Nr|Nummer))"
        r"\.?\s*:?\s*" + _WERT,
        re.I,
    ),
    "datum": re.compile(
        r"(?<![A-Za-zäöüÄÖÜ])(?:Datum|Bestelldatum|Belegdatum|Auftragsdatum)\s*:?\s*(?P<wert>[^\n]{6,40})",
        re.I,
    ),
    "datum_vom": re.compile(r"\bvom\s+(?P<wert>[^\n]{6,40})", re.I),  # schwach: "Bestellung vom 1.10.2026"
    "datum_ort": re.compile(
        r"^(?P<ort>[A-ZÄÖÜ][A-Za-zÄÖÜäöüß.\- ]{1,40}),\s*(?:den\s+)?"
        r"(?P<wert>\d{1,2}\.\s?\d{1,2}\.\s?\d{2,4}|\d{1,2}\.?\s*[A-Za-zäöüÄÖÜ]+\.?\s+\d{2,4})\s*$"
    ),
    "liefertag": re.compile(
        r"\b(?:Liefertermin|Liefertag|Lieferdatum|Lieferung\s+am|Anlieferung\s+am|Zustellung\s+am"
        r"|Wunschtermin|Wunschlieferdatum|Wunschliefertermin|Lieferwoche|Liefer-?\s?KW|Anlieferung\s*:|Termin\s*:)"
        r"\s*:?\s*(?P<wert>[^\n]{1,40})",
        re.I,
    ),
    "lieferadresse": re.compile(
        r"\b(?:Lieferadresse|Lieferanschrift|Lieferung\s+an|Anlieferung\s+an|Liefern\s+an|Versandadresse"
        r"|Versandanschrift|Warenempfänger|Abweichende\s+Lieferadresse|Anlieferadresse|Anlieferanschrift"
        r"|Anlieferort|Anlieferstelle|Lieferort|Ship-?\s?to|Delivery\s+address)\s*:?\s*(?P<wert>[^\n]*)",
        re.I,
    ),
    # Abholadresse beim Weingut (Lagerbestellungen der Kellereien: "Ladeadresse:")
    "ladeadresse": re.compile(
        r"\b(?:Ladeadresse|Abholadresse|Ladestelle|Abholort|Abholung\s+bei|Abholung\s+ab|Ladeort|Verladeort"
        r"|Verladestelle|Abholanschrift|Pick-?\s?up(?:\s+address)?)\s*:?\s*(?P<wert>[^\n]*)",
        re.I,
    ),
    "lieferbedingung": re.compile(
        r"\b(?:Lieferbedingung(?:en)?|Incoterms?|Lieferkondition(?:en)?|Frachtbedingung(?:en)?"
        r"|Versandbedingung(?:en)?|Terms\s+of\s+delivery|Delivery\s+terms)\s*:?\s*(?P<wert>[^\n]{2,60})",
        re.I,
    ),
    # Aus Sicht des Kunden: UNSERE Lieferantennummer beim Kunden (SAP: "Lieferanten-Nr."), nie die Kundennummer.
    "lieferantennummer": re.compile(
        r"\b(?:Lieferanten-?\s?(?:Nr|Nummer)|Lief\.?-?\s?Nr|Kreditoren-?\s?(?:Nr|Nummer)|Supplier\s*-?\s*(?:No|Nr|Number)"
        r"|Vendor\s*-?\s*(?:No|Nr|Number))\.?\s*:?\s*" + _WERT,
        re.I,
    ),
    "lieferant": re.compile(
        r"\b(?:Lieferant|Weingut|Erzeuger|Hersteller|Produzent|Cantina|Azienda)\s*:\s*(?P<wert>[^\n]+)", re.I
    ),
    "kunde": re.compile(
        r"\b(?:Kunde|Kundenname|Besteller|Auftraggeber|Endkunde|Kommission|Für\s+Kunde|Customer)"
        r"\s*:\s*(?P<wert>[^\n]+)",
        re.I,
    ),
    # Groß-/Kleinschreibung bewusst beachten: "Bestellung ... an Cide srl"
    "bestellung_an": re.compile(r"\bBestellung\b[^\n]{0,40}?\ban\s*:?\s+(?P<wert>[A-ZÄÖÜ][^\n]+)"),
}
_NUMMERNFELDER = {"kundennummer", "ihre_bestellnummer", "bestellnummer", "belegnummer", "lieferantennummer"}
_DATUMSFELDER = {"datum", "datum_ort", "datum_vom", "liefertag"}
_ADRESSFELDER = {"lieferadresse", "ladeadresse"}

# Lieferbedingung als Freitext ohne Schlüsselwort ("Bitte liefern Sie frei Haus", "ex cellar")
_LIEFERBEDINGUNG_FREI = re.compile(
    r"\b(?P<wert>frei\s+Haus|frei\s+Lager|frei\s+Rampe|ab\s+Werk|ab\s+Keller|ab\s+Hof|ex\s+cellar|ex\s+works"
    r"|franco\s+cantina|franco\s+destino|unfrei|EXW|FCA|DAP|DDP|CIF|FOB)\b",
    re.I,
)

# --- Revised Order ----------------------------------------------------------------------------
_REVISION_MUSTER = re.compile(
    r"\b(?:revised\s+order|revised|revisione\s+ordine|änderung(?:en)?\s+(?:zur|der)\s+bestellung|bestelländerung"
    r"|bestellungsänderung|geänderte\s+bestellung|korrektur\s+(?:der|zur)\s+bestellung|korrigierte\s+bestellung"
    r"|ersetzt\s+(?:die\s+|unsere\s+|ihre\s+)?bestellung|ersatz\s+für\s+(?:die\s+)?bestellung"
    r"|storno\s*(?:\+|und|&|/)\s*neu|order\s+change|change\s+order|amended\s+order|replaces\s+(?:our\s+|the\s+)?order"
    r"|neufassung\s+der\s+bestellung)\b",
    re.I,
)
_REVISION_VON = re.compile(
    r"\b(?:ersetzt\s+(?:die\s+|unsere\s+|ihre\s+)?Bestellung|Änderung(?:en)?\s+(?:zur|der)\s+Bestellung"
    r"|Korrektur\s+(?:der|zur)\s+Bestellung|ursprüngliche\s+Bestellung|alte\s+Bestell(?:ung|-?\s?Nr\.?|nummer)"
    r"|bisherige\s+Bestellung|vorherige\s+Bestellung|replaces\s+(?:our\s+|the\s+)?order|original\s+order"
    r"|previous\s+order|Storno\s+(?:der\s+)?Bestellung|Ersatz\s+für\s+(?:die\s+)?Bestellung)"
    r"\s*(?:-?\s*(?:Nr|Nummer|No)\.?\s*:?\s*)?(?P<nummer>[A-Za-z0-9][A-Za-z0-9\-/_.]*\d[A-Za-z0-9\-/_.]*)",
    re.I,
)

# --- Aktionshinweis auf Belegebene -------------------------------------------------------------
_AKTION_STARK = (
    r"Aktionszeitraum|Aktionszeitr\.|Aktionswoche|Aktionszeit|Aktions-?\s?KW|Aktionskalenderwoche|Werbewoche"
    r"|Angebotswoche|Sonderaktion|Aktionsware|Promotionszeitraum|Promo-?\s?zeitraum|Promotion|Promo"
)
_AKTION_MUSTER = re.compile(
    rf"(?P<text>\b(?P<schluessel>{_AKTION_STARK}|Aktion)\b(?:\s*:)?"
    r"(?:\s*(?P<zusatz>(?:KW|Kalenderwoche|Woche)\s*\.?\s*\d{1,2}(?:\s*(?:[-–/]|bis)\s*(?:KW\s*)?\d{1,2})?(?:\s*/\s*\d{2,4})?"
    r"|(?:vom\s+|ab\s+)?\d{1,2}\.\s?\d{1,2}\.?(?:\s?\d{2,4})?\s*(?:[-–]|bis)\s*\d{1,2}\.\s?\d{1,2}\.?(?:\s?\d{2,4})?"
    r"|(?:vom|ab)\s+\d{1,2}\.\s?\d{1,2}\.?(?:\s?\d{2,4})?"
    r"|[A-Za-zäöüÄÖÜß][A-Za-zäöüÄÖÜß\-]{1,30}(?:\s+\d{2,4})?(?:\s+(?-i:[A-ZÄÖÜ])[A-Za-zäöüÄÖÜß\-]{1,30}){0,2}))?)",
    re.I,
)
_AKTION_SCHWACH = {"aktion"}
_AKTION_ZUSATZ_STOPP = re.compile(r"^(?:rabatt|nachlass|preis|netto|brutto|gesamt|summe|menge|je|pro|per|à|a)\b", re.I)
_AKTION_MAX_LAENGE = 80


def _bereinige_wert(feld: str, wert: str) -> str | None:
    wert = wert.strip().rstrip(".,:;").strip()
    if not wert:
        return None
    if feld in _NUMMERNFELDER and not re.search(r"\d", wert):
        return None  # "Ihre Bestellung vom ..." -> kein Wert
    if feld in _DATUMSFELDER:
        return wert if zahlen.finde_datum(wert) or feld == "liefertag" else None
    if feld in _ADRESSFELDER and _block_rolle(wert):
        return None  # "Ladeadresse: Anlieferadresse:" (zwei Überschriften nebeneinander) -> kein Wert
    return wert


def _ist_kopffeldzeile(zeile: str) -> bool:
    return any(m.search(zeile) for feld, m in _KOPF_MUSTER.items() if feld != "datum_ort") or bool(
        _KOPF_MUSTER["datum_ort"].match(zeile)
    )


def lese_kopffelder(zeilen: list[str]) -> dict[str, str]:
    """Liest Kopffelder aus Textzeilen (erster Treffer je Feld gewinnt).

    Felder: kundennummer, ihre_bestellnummer, bestellnummer, belegnummer, datum,
    datum_ort, datum_vom, liefertag (Rohtext, z. B. "KW 44"), lieferadresse, lieferant,
    kunde, bestellung_an. Werte sind unbereinigter Text; Datumsfelder werden erst vom
    Aufrufer mit :func:`zahlen.parse_datum` gelesen. Beim Liefertag gewinnt ein Wert
    mit lesbarem Datum vor einem Rohtext ohne Datum.
    """
    felder: dict[str, str] = {}
    liefertag_roh: str | None = None
    for zeile in zeilen:
        if not zeile:
            continue
        # "ersetzt Bestellung Nr. 123": die Nummer der ERSETZTEN Bestellung ist keine Kopfnummer
        revisionszeile = bool(_REVISION_VON.search(zeile))
        for feld, muster in _KOPF_MUSTER.items():
            if feld in felder:
                continue
            if revisionszeile and feld in _NUMMERNFELDER:
                continue
            treffer = muster.search(zeile)
            if not treffer:
                continue
            wert = _bereinige_wert(feld, treffer.group("wert"))
            if wert is None:
                continue
            if feld == "liefertag" and not zahlen.finde_datum(wert):
                liefertag_roh = liefertag_roh or wert
                continue
            felder[feld] = wert
    if "liefertag" not in felder and liefertag_roh:
        felder["liefertag"] = liefertag_roh
    return felder


@dataclass(frozen=True)
class RevisionTreffer:
    """Ergebnis von :func:`finde_revision`: Fundtext und (falls genannt) die ersetzte Bestellnummer."""

    rohtext: str
    revision_von: str | None = None


def finde_revision(zeilen: list[str | None]) -> RevisionTreffer | None:
    """Erkennt eine "Revised Order" (Änderung/Ersatz einer früheren Bestellung).

    Fundstellen: "Revised order", "REVISED", "Änderung zur Bestellung", "ersetzt Bestellung
    Nr. 123", "Storno + Neu", "replaces order 123". Die ersetzte Nummer wird geliefert, wenn
    sie hinter einem dieser Schlüssel steht; sonst ``revision_von = None``.
    """
    treffer: RevisionTreffer | None = None
    for zeile in zeilen:
        if not zeile:
            continue
        s = re.sub(r"\s+", " ", zeile).strip()
        von = _REVISION_VON.search(s)
        if von:
            nummer = von.group("nummer").rstrip(".,;:")
            return RevisionTreffer(rohtext=s[: von.end()][-120:], revision_von=nummer or None)
        if treffer is None:
            fund = _REVISION_MUSTER.search(s)
            if fund:
                treffer = RevisionTreffer(rohtext=fund.group(0), revision_von=None)
    return treffer


def revisions_hinweis(revision: RevisionTreffer) -> Pruefhinweis:
    """Pruefhinweis REVISION (WARNUNG) für eine erkannte Revised Order; der Text nennt die ersetzte Nummer."""
    if revision.revision_von:
        text = (
            f"Revised Order: Diese Bestellung ersetzt Bestellung Nr. {revision.revision_von} – "
            "Cde und AB werden neu angelegt (Kürzel 'Neu'); bitte die ersetzte Bestellung prüfen."
        )
    else:
        text = (
            f"Revised Order erkannt ('{revision.rohtext}'): ersetzt eine frühere Bestellung, deren Nummer "
            "im Dokument nicht genannt ist – Cde und AB werden neu angelegt (Kürzel 'Neu')."
        )
    return Pruefhinweis(
        code=Pruefcode.REVISION,
        schweregrad=Schweregrad.WARNUNG,
        text=text,
        details={"revision_von": revision.revision_von or "", "rohtext": revision.rohtext},
    )


def finde_aktionshinweis(zeilen: list[str | None]) -> str | None:
    """Aktionshinweis auf Belegebene ("Aktion KW 45", "Promo", "Aktionszeitraum 01.11.-30.11.").

    Ein nacktes "Aktion" zählt nur mit Zusatz (KW, Zeitraum, Bezeichnung); "Aktion -10 %" ist ein
    Positionsrabatt und kein Aktionshinweis. Geliefert wird der erste Fund (max. 80 Zeichen).
    """
    for zeile in zeilen:
        if not zeile:
            continue
        s = re.sub(r"\s+", " ", zeile).strip()
        for treffer in _AKTION_MUSTER.finditer(s):
            schluessel = treffer.group("schluessel").casefold()
            zusatz = (treffer.group("zusatz") or "").strip()
            if zusatz and _AKTION_ZUSATZ_STOPP.match(zusatz):
                zusatz = ""
            if schluessel in _AKTION_SCHWACH and not zusatz:
                continue
            text = treffer.group("text").strip().rstrip(" :;,-–")
            if not re.search(r"\d\.$", text):
                text = text.rstrip(".")  # "30.11." behält den Datumspunkt, "Promo." nicht
            if not zusatz:
                text = treffer.group("schluessel")
            return text[:_AKTION_MAX_LAENGE].strip()
    return None


def finde_lieferbedingung(zeilen: list[str | None]) -> str | None:
    """Lieferbedingung als Freitext ("frei Haus", "ex cellar", "EXW"), wenn kein Kopffeld vorhanden ist."""
    for zeile in zeilen:
        if not zeile:
            continue
        treffer = _LIEFERBEDINGUNG_FREI.search(zeile)
        if treffer:
            return re.sub(r"\s+", " ", treffer.group("wert")).strip()
    return None


_VERBRAUCHSTEUERNUMMER = re.compile(r"\bDE\s?\d{11}\b")
_DAA_ZEILE = re.compile(r"^\s*DAA\b", re.I)
SPEDITIONSZEILE_HINWEIS = "Speditionszeile"


def ist_speditionszeile(bezeichnung: str | None, preis: Decimal | None) -> bool:
    """"DAA …"-Zeilen und Zeilen mit Verbrauchsteuernummer (DE + 11 Ziffern) zu 0,00 € sind Speditionszeilen.

    Sie bleiben als Position erhalten (``hinweis`` "Speditionszeile"); das Matching überspringt sie.
    """
    if not bezeichnung or preis is None or preis != 0:
        return False
    return bool(_DAA_ZEILE.match(bezeichnung) or _VERBRAUCHSTEUERNUMMER.search(bezeichnung))


_SCHLUESSELZEILE = re.compile(r"^[\wäöüÄÖÜß.\-/ ]{2,30}:\s*\S")
_UEBERSCHRIFT = re.compile(
    r"^(?:Bestellung|Auftrag|Auftragsbestätigung|Order|Angebot|Rechnung|Lieferschein|Pos\b|Position|Sehr geehrte)",
    re.I,
)
_EMPFAENGER_MARKER = re.compile(r"^(?:an|firma|herrn?|frau|z\.?\s?hd\.?.*|zu\s+händen.*)\s*:?$", re.I)
_PLZ_ZEILE = re.compile(r"^(?:[A-Z]{1,2}[- ])?\d{4,5}\s+[A-Za-zÄÖÜ]")
# Überschriften von Adressblöcken (allein auf einer Zeile) und ihre Rolle
_BLOCK_ROLLEN: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"^(?:Rechnungsempfänger|Rechnungsanschrift|Rechnungsadresse|Besteller|Auftraggeber|Kunde|Käufer"
            r"|Bill-?\s?to|Invoice\s+to|Sold-?\s?to)\s*:?$",
            re.I,
        ),
        "kunde",
    ),
    (
        re.compile(
            r"^(?:Warenempfänger|Lieferadresse|Lieferanschrift|Anlieferadresse|Anlieferanschrift|Anlieferort"
            r"|Rechnungs-?\s*/\s*Lieferanschrift|Rechnungs-?\s*(?:und|&)\s*Lieferanschrift|Versandadresse"
            r"|Versandanschrift|Ship-?\s?to|Deliver(?:y)?\s+(?:to|address))\s*:?$",
            re.I,
        ),
        "lieferadresse",
    ),
    (
        re.compile(
            r"^(?:Ladeadresse|Abholadresse|Ladestelle|Abholort|Verladeort|Verladestelle|Abholanschrift"
            r"|Pick-?\s?up(?:\s+address)?)\s*:?$",
            re.I,
        ),
        "ladeadresse",
    ),
    (
        re.compile(r"^(?:Bestellempfänger|Lieferant|Supplier|Vendor|Empfänger|Auftragnehmer)\s*:?$", re.I),
        "empfaenger",
    ),
)
# Zeilen innerhalb eines Adressblocks, die kein Name/keine Anschrift sind (GLN, Telefon, Nummern)
_ADRESSZUSATZ = re.compile(
    r"^(?:GLN|ILN|UID|USt-?\s?Id(?:Nr)?|UST-?ID|VAT|Tel\.?|Telefon|Fax|Telefax|E-?Mail|Mobil|Kd-?\s?Nr|Kunden-?\s?Nr"
    r"|Lieferanten-?\s?Nr|Lief\.?-?\s?Nr|Debitoren-?\s?Nr|Kreditoren-?\s?Nr)\b",
    re.I,
)


def _block_rolle(zeile: str) -> str | None:
    for muster, rolle in _BLOCK_ROLLEN:
        if muster.match(zeile):
            return rolle
    return None
_LAENDER = re.compile(
    r"^(?:Deutschland|Germany|Italien|Italia|Italy|Österreich|Austria|Schweiz|Switzerland|Frankreich|France"
    r"|Spanien|España|Spain|Portugal|Niederlande|Nederland|Luxemburg|Belgien)$",
    re.I,
)


@dataclass
class _Adressblock:
    zeilen: list[str]
    als_empfaenger_markiert: bool = False
    rolle: str | None = None  # "kunde" | "lieferadresse" | "ladeadresse" | "empfaenger" | None

    @property
    def name(self) -> str:
        return self.zeilen[0]

    @property
    def adresse(self) -> str:
        return ", ".join(self.zeilen[1:])

    @property
    def volltext(self) -> str:
        return ", ".join(self.zeilen)


def _teile_adresszeile(zeile: str) -> list[str]:
    """Zerlegt einzeilige Absenderzeilen ("Firma · Straße 1 · 12345 Ort")."""
    for trenner in (r"\s*[·•|]\s*", r"\s+[–-]\s+", r",\s*"):
        teile = [t.strip() for t in re.split(trenner, zeile) if t.strip()]
        if len(teile) >= 2 and _PLZ_ZEILE.match(teile[-1]):
            return teile
    return [zeile]


def _adressbloecke(zeilen: list[str], maximal: int = 400) -> list[_Adressblock]:
    """Findet Adressblöcke (Name, [Zusatz], Straße, PLZ Ort) im Kopfbereich.

    Eine Überschrift allein auf einer Zeile ("Rechnungsempfänger", "Warenempfänger", "Ladeadresse",
    "Bestellempfänger") gibt dem folgenden Block seine Rolle; Zusatzzeilen (GLN, Telefon,
    "Lieferanten-Nr.") gehören zum Block, werden aber nie zum Namen.
    """
    bloecke: list[_Adressblock] = []
    aktuell: list[str] = []
    markiert = False
    rolle: str | None = None

    def zuruecksetzen(*, markiert_neu: bool = False, rolle_neu: str | None = None) -> None:
        nonlocal aktuell, markiert, rolle
        aktuell, markiert, rolle = [], markiert_neu, rolle_neu

    for zeile in zeilen[:maximal]:
        s = zeile.strip()
        if not s:
            zuruecksetzen()
            continue
        rolle_neu = _block_rolle(s)
        if rolle_neu is not None:
            zuruecksetzen(markiert_neu=True, rolle_neu=rolle_neu)
            continue
        if _EMPFAENGER_MARKER.match(s):
            zuruecksetzen(markiert_neu=True)
            continue
        if _ADRESSZUSATZ.match(s):
            continue  # GLN, Telefon, Lieferanten-Nr.: Teil des Blocks, aber kein Name/keine Anschrift
        if _SCHLUESSELZEILE.match(s) or _UEBERSCHRIFT.match(s) or _ist_kopffeldzeile(s):
            zuruecksetzen()
            continue
        if _LAENDER.match(s):
            aktuell = []
            continue
        teile = _teile_adresszeile(s)
        if len(teile) >= 2:
            bloecke.append(_Adressblock(teile, markiert, rolle))
            zuruecksetzen()
            continue
        aktuell.append(s)
        if _PLZ_ZEILE.match(s):
            if len(aktuell) >= 2:
                bloecke.append(_Adressblock(aktuell[-5:], markiert, rolle))
            zuruecksetzen()
        elif len(aktuell) > 6:
            aktuell = aktuell[-6:]
    return bloecke


# ---------------------------------------------------------------------------
# Positionen
# ---------------------------------------------------------------------------

# TODO-LEXWARE: Spaltenbeschriftungen des Lexware-Bestelldrucks (vermutlich "Pos.", "Artikel-Nr.",
# "Bezeichnung", "Menge", "Einheit", "Einzelpreis", "Gesamtpreis") an einem echten Ausdruck prüfen
# und fehlende Schreibweisen hier ergänzen.
_SPALTEN_SCHLUESSEL: dict[str, frozenset[str]] = {
    "pos": frozenset({"pos", "position", "nr", "lfdnr", "lfd", "posnr"}),
    "artikelnummer": frozenset(
        {"artnr", "artikelnr", "artikelnummer", "art", "artno", "sku", "itemno", "artikelno", "nummer"}
    ),
    # Unsere Artikelnummer aus Sicht des Kunden ("Lief. ANr.", "Ihre Artikelnummer")
    "artikelnummer_lieferant": frozenset(
        {
            "liefanr",
            "liefartnr",
            "liefartikelnr",
            "liefartikelnummer",
            "lieferantenartikelnummer",
            "lieferantenartnr",
            "lieferantenartikelnr",
            "lieferantartikelnummer",
            "lieferantartnr",
            "ihreartnr",
            "ihreartikelnummer",
            "ihreartikelnr",
            "artnrlieferant",
            "artikelnrlieferant",
            "artikelnummerlieferant",
            "lfartnr",
            "lieferantensku",
        }
    ),
    # Artikelnummer des Kunden (SPAR "Artikelnummer", Rheinberg "Artikelnr." neben der GTIN)
    "kundenartikelnummer": frozenset(
        {
            "kundenartikelnummer",
            "kundenartnr",
            "kdartnr",
            "kdartikelnr",
            "kundenartikelnr",
            "unsereartnr",
            "unsereartikelnummer",
            "unsereartikelnr",
            "interneartnr",
            "interneartikelnummer",
        }
    ),
    "gtin": frozenset(
        {"gtin", "ean", "eancode", "kolligtin", "gtinkolli", "eankolli", "barcode", "gtinean", "eangtin", "flaschengtin", "gtinflasche"}
    ),
    "menge": frozenset({"menge", "anzahl", "qty", "quantity", "stueck", "stk", "bestellmenge", "mge"}),
    # Menge in Verpackungseinheiten (SAP: "Bestellmenge in Bestell-Einheit"; "Anzahl Kolli", "VE")
    "menge_ve": frozenset(
        {
            "bestellmengebestelleinheit",
            "mengebestelleinheit",
            "bestellmengeve",
            "mengeve",
            "anzahlve",
            "ve",
            "kolli",
            "anzahlkolli",
            "mengekolli",
            "kollianzahl",
            "bestelleinheiten",
            "bestelleinheit",
            "anzahlgebinde",
            "kartons",
            "anzahlkartons",
            "cases",
            "qtycases",
            "cartons",
        }
    ),
    # Flaschen je Verpackungseinheit (SAP: "Einheiten pro Bestell-Einheit"; "Fl./VE", "Inhalt" bei Kolli)
    "ve_inhalt": frozenset(
        {
            "einheitenprobestelleinheit",
            "einheitenjebestelleinheit",
            "inhaltve",
            "veinhalt",
            "inhaltjeve",
            "inhaltprove",
            "flaschenjeve",
            "flaschenprove",
            "stueckjeve",
            "stueckprove",
            "flaschenjekarton",
            "flaschenprokarton",
            "flkt",
            "flve",
            "stkve",
            "inhaltkolli",
            "kolliinhalt",
            "flaschenjekolli",
            "stueckjekolli",
            "einheitenprove",
            "einheitenjeve",
            "einheitenprokolli",
            "einheitenjekolli",
            "packungsinhalt",
            "gebindeinhalt",
        }
    ),
    "einheit": frozenset({"einheit", "me", "einh", "mengeneinheit", "unit", "vpe"}),
    "mwst": frozenset({"mwst", "ust", "steuer", "mwstsatz", "ustsatz", "steuersatz", "vat", "tax", "mwstprozent"}),
    "rabatt": frozenset({"rabatt", "rabattprozent", "rab", "nachlass", "discount", "aktionsrabatt", "rabattsatz"}),
    "bezeichnung": frozenset(
        {
            "artikel",
            "bezeichnung",
            "beschreibung",
            "artikelbezeichnung",
            "wein",
            "produkt",
            "text",
            "artikeltext",
            "description",
            "ware",
            "warenbezeichnung",
            "artikelbeschreibung",
            "artikelbez",
            "artbez",
            "bez",
            "artikelname",
            "produktbezeichnung",
        }
    ),
    "jahrgang": frozenset({"jahrgang", "jg", "vintage", "jahr"}),
    "volumen": frozenset({"inhalt", "volumen", "fuellmenge", "gebinde", "groesse", "flgroesse", "size", "fuellung"}),
    "preis": frozenset(
        {
            "einzelpreis",
            "preis",
            "ep",
            "ek",
            "ekpreis",
            "vk",
            "vkpreis",
            "stueckpreis",
            "preisfl",
            "epreis",
            "price",
            "unitprice",
            "nettopreis",
            "einzelpreisnetto",
            "preisnetto",
            "einkaufspreis",
            "verkaufspreis",
            "preisjefl",
            "preisjeflasche",
        }
    ),
    "gesamt": frozenset(
        {"gesamt", "gesamtpreis", "betrag", "summe", "total", "gesamtnetto", "nettobetrag", "zeilensumme", "amount"}
    ),
    "hinweis": frozenset({"hinweis", "bemerkung", "info", "anmerkung", "kommentar", "notiz", "remark", "aktion"}),
}
_KOPF_ZU_FELD: dict[str, str] = {
    wort: feld for feld, woerter in _SPALTEN_SCHLUESSEL.items() for wort in woerter
}

_HINWEIS_MUSTER = re.compile(
    r"(?<![\d,.])(\d{1,2}\s*\+\s*\d{1,2})(?![\d,.])"
    r"|\b(gratis|kostenlos|kostenfrei|unentgeltlich|Naturalrabatt|Gratisflaschen?|Freiware|ohne\s+Berechnung"
    r"|o\.\s?B\.|Muster|Probeflaschen?|Dreingabe)\b",
    re.I,
)
_SUMMENZEILE = re.compile(
    r"^(?:Summe|Gesamt|Zwischensumme|Netto|Brutto|MwSt|USt|Total|Endbetrag|Übertrag|zzgl)", re.I
)
_PREIS = r"-?\d{1,3}(?:\.\d{3})*,\d{2}|-?\d{1,6}\.\d{2}"
_WAEHRUNG = r"(?:€|EUR|Euro)?"
_EINHEIT_ODER_X = zahlen.EINHEIT_MUSTER[:-1] + r"|x|×)"
_BUCHSTABEN = re.compile(r"[A-Za-zÄÖÜäöüß]{2}")
_UNERWUENSCHTER_ANFANG = re.compile(
    r"^(?:Datum|Liefer|Bestell|Kunde|Tel|Fax|E-?Mail|Seite|Uhr|Stunden|Tage|Jahre|Prozent|%)", re.I
)


def _hinweis_trennen(text: str, zusatz: str | None = None) -> tuple[str, str | None]:
    """Trennt Hinweise ("11+1", "Gratis") von einer Bezeichnung ab."""
    hinweise: list[str] = []
    for roh in filter(None, [zusatz]):
        for treffer in _HINWEIS_MUSTER.finditer(roh):
            hinweise.append(treffer.group(0).strip())
        if not hinweise and roh.strip():
            hinweise.append(roh.strip())

    def _merken(treffer: re.Match[str]) -> str:
        wert = treffer.group(0).strip()
        if wert.casefold() not in {h.casefold() for h in hinweise}:
            hinweise.append(wert)
        return " "

    bereinigt = _HINWEIS_MUSTER.sub(_merken, text)
    bereinigt = re.sub(r"\(\s*\)|\[\s*\]", " ", bereinigt)
    bereinigt = re.sub(r"\s+", " ", bereinigt).strip(" -–,;:/")
    return bereinigt, ("; ".join(hinweise) if hinweise else None)


_ZAHL_KLEIN = re.compile(r"\d{1,3}")
_GTIN = re.compile(r"\d{8,14}")
_FLASCHEN_EINHEITEN = frozenset({"Fl", "Stk", "Pal"})  # Mengen in diesen Einheiten sind keine VE-Anzahl


def _volumen_aus_text(text: str | None) -> Decimal | None:
    """Volumen aus einem Text; "htr.0,75l" (Punkt direkt vor der Zahl) wird ebenfalls gelesen."""
    if not text:
        return None
    wert = volumen.parse_volumen_cl(text)
    if wert is None and re.search(r"[A-Za-z]\.\d", text):
        wert = volumen.parse_volumen_cl(re.sub(r"(?<=[A-Za-z])\.(?=\d)", ". ", text))
    return wert


def _nummer(text: str | None) -> str | None:
    wert = (text or "").strip().rstrip(".,")
    return wert if wert and re.search(r"\d", wert) else None


def _hinweis_anhaengen(hinweis: str | None, zusatz: str | None) -> str | None:
    if not zusatz:
        return hinweis
    if not hinweis:
        return zusatz
    if zusatz.casefold() in {h.strip().casefold() for h in hinweis.split(";")}:
        return hinweis
    return f"{hinweis}; {zusatz}"


def _baue_position(
    *,
    bezeichnung: str | None,
    menge: str | None,
    einheit: str | None = None,
    preis: str | None = None,
    jahrgang: str | None = None,
    volumen_text: str | None = None,
    artikelnummer: str | None = None,
    hinweis: str | None = None,
    rohtext: str,
    menge_ve: str | None = None,
    ve_inhalt: str | None = None,
    gtin: str | None = None,
    kundenartikelnummer: str | None = None,
    rabatt: str | None = None,
) -> RohPosition | None:
    """Baut eine :class:`RohPosition` aus Zellen-/Regex-Werten (alles Text, ``None`` = nicht vorhanden).

    Verpackungseinheiten: ``menge_ve`` × ``ve_inhalt`` -> ``menge`` in Flaschen; fehlt ``ve_inhalt``,
    wird er aus "Inhalt"-/"VPE"-Zellen mit reiner Zahl oder aus der Bezeichnung ("VE 6", "6er Karton")
    gelesen. Steht die Menge in einer Gebinde-Einheit (Kt, Kolli, KOA, VE), gilt sie als VE-Anzahl.
    Rabatt: Spalte ``rabatt`` oder Text in der Bezeichnung ("Aktion -10 %").
    """
    bez_roh = re.sub(r"\s+", " ", (bezeichnung or "").replace("\n", " ")).strip()

    # Rabatt laut Dokument (Spalte vor Text)
    rabatt_prozent = zahlen.parse_prozent(rabatt) if rabatt else None
    rabatt_text = rabatt.strip() if rabatt_prozent is not None and rabatt else None
    if rabatt_prozent is None:
        fund = zahlen.parse_rabatt(bez_roh)
        if fund is not None:
            rabatt_prozent, rabatt_text = fund.prozent, fund.rohtext
            bez_roh = re.sub(r"\s+", " ", bez_roh[: fund.start] + " " + bez_roh[fund.ende :]).strip()
    if rabatt_prozent is None and hinweis:
        fund = zahlen.parse_rabatt(hinweis)
        if fund is not None:
            rabatt_prozent, rabatt_text = fund.prozent, fund.rohtext
    bez, hinweis_text = _hinweis_trennen(bez_roh, hinweis)

    # Schmale Tabellenzellen umbrechen kurze Einheiten ("KO\nA" -> "KOA")
    if einheit and len(einheit.strip()) <= 10:
        einheit = re.sub(r"\s+", "", einheit)
    # "VPE"/"Einheit"-Zelle mit reiner Zahl = Flaschen je VE; "Inhalt"-Zelle: Volumen oder Flaschen je Kolli
    if einheit and _ZAHL_KLEIN.fullmatch(einheit.strip()):
        ve_inhalt = ve_inhalt or einheit.strip()
        einheit = None
    if volumen_text and _ZAHL_KLEIN.fullmatch(volumen_text.strip()) and _volumen_aus_text(volumen_text) is None:
        ve_inhalt = ve_inhalt or volumen_text.strip()
        volumen_text = None

    menge_obj = zahlen.parse_menge(menge)
    einheit_norm = zahlen.normalisiere_einheit(einheit) or (menge_obj.einheit if menge_obj else None)
    ve_anzahl = zahlen.glatt(zahlen.parse_dezimal(menge_ve)) if menge_ve else None
    inhalt = zahlen.glatt(zahlen.parse_dezimal(ve_inhalt)) if ve_inhalt else None
    if inhalt is not None and inhalt <= 0:
        inhalt = None
    inhalt_aus_spalte = inhalt is not None
    if not bez and menge_obj is None and ve_anzahl is None:
        return None

    menge_wert = zahlen.glatt(menge_obj.wert) if menge_obj else None
    ve_hinweis: str | None = None
    if menge_obj is not None and menge_obj.gebinde is not None:
        # "12 x 6" / "2 Kt à 6": parse_menge hat bereits in Flaschen umgerechnet
        ve_anzahl = ve_anzahl or zahlen.glatt(menge_obj.gebinde)
        inhalt = inhalt or zahlen.glatt(menge_obj.je_gebinde)
        ve_hinweis = f"{ve_anzahl} VE à {inhalt} Fl"
    elif ve_anzahl is not None:
        # eigene VE-Spalte(n), z. B. SAP "Bestellmenge in Bestell-Einheit" × "Einheiten pro Bestell-Einheit"
        if inhalt is None:
            inhalt = zahlen.parse_ve_inhalt(bez)
        gebinde = einheit_norm if zahlen.ist_gebinde_einheit(einheit_norm) else "VE"
        if inhalt is not None:
            menge_wert = zahlen.glatt(ve_anzahl * inhalt)
            einheit_norm = "Fl"
            ve_hinweis = f"{ve_anzahl} {gebinde} à {inhalt} Fl"
        else:
            menge_wert = ve_anzahl
            einheit_norm = einheit_norm or "VE"
            ve_hinweis = f"{ve_anzahl} {gebinde}, Inhalt je VE unbekannt"
    elif menge_obj is not None and (
        zahlen.ist_gebinde_einheit(einheit_norm) or (inhalt_aus_spalte and einheit_norm not in _FLASCHEN_EINHEITEN)
    ):
        # Menge in Kolli/KOA/Kt (Lagerbestellung) bzw. Menge neben einer "Inhalt"-Spalte (Einheit unbekannt/leer)
        if inhalt is None:
            inhalt = zahlen.parse_ve_inhalt(bez)
        if inhalt is not None:
            ve_anzahl = menge_wert
            gebinde = einheit_norm or "VE"
            menge_wert = zahlen.glatt(ve_anzahl * inhalt)
            einheit_norm = "Fl"
            ve_hinweis = f"{ve_anzahl} {gebinde} à {inhalt} Fl"
    if ve_anzahl is None:
        inhalt = None  # ohne VE-Anzahl ist ein Inhalt bedeutungslos
    hinweis_text = _hinweis_anhaengen(hinweis_text, ve_hinweis)

    preis_wert = zahlen.parse_dezimal(preis)
    if ist_speditionszeile(bez, preis_wert):
        hinweis_text = _hinweis_anhaengen(hinweis_text, SPEDITIONSZEILE_HINWEIS)

    jahrgang_wert: int | None = None
    if jahrgang and re.fullmatch(r"\s*(?:19|20)\d{2}\s*", jahrgang):
        jahrgang_wert = int(jahrgang)
    if jahrgang_wert is None:
        jahrgang_wert = volumen.parse_jahrgang(bez)

    volumen_cl = _volumen_aus_text(volumen_text) if volumen_text else None
    if volumen_cl is None:
        volumen_cl = _volumen_aus_text(bez)

    gtin_wert = re.sub(r"\s+", "", gtin or "")  # umgebrochene Tabellenzellen ("409999900001\n8")
    if gtin_wert and not _GTIN.fullmatch(gtin_wert):
        logger.debug("GTIN %r hat kein GTIN-Format, wird ignoriert", gtin)
        gtin_wert = ""

    return RohPosition(
        laufnr=0,
        bezeichnung=bez,
        jahrgang=jahrgang_wert,
        menge=menge_wert,
        einheit=einheit_norm,
        preis=preis_wert,
        volumen_cl=volumen_cl,
        artikelnummer=_nummer(artikelnummer),
        hinweis=hinweis_text,
        durchgestrichen=False,  # pdfplumber erkennt Streichungen nicht
        rohtext=rohtext,
        menge_ve=ve_anzahl,
        ve_inhalt=inhalt,
        kundenartikelnummer=_nummer(kundenartikelnummer),
        gtin=gtin_wert or None,
        rabatt_prozent=rabatt_prozent,
        rabatt_text=rabatt_text,
    )


# -- Tabellen ---------------------------------------------------------------


def _artikelnummern_zuordnen(spalten: dict[str, int]) -> dict[str, int]:
    """Entscheidet, welche Nummernspalte unsere Artikelnummer ist und welche die des Kunden.

    * Gibt es eine ausdrückliche Lieferanten-Artikelnummer ("Lief. ANr."), ist sie ``artikelnummer``;
      eine allgemeine "Artikelnummer" daneben ist dann die Nummer des Kunden.
    * Steht eine allgemeine "Artikelnummer" neben einer GTIN-Spalte (LEH-Layouts), ist sie die
      Nummer des Kunden (``kundenartikelnummer``).
    * Sonst bleibt "Artikelnummer" unsere Artikelnummer (Fachhandel, eigene Bestellungen).

    TODO-PRUEFEN: An echten SAP-Bestellungen (Handelskette) und Lagerbestellungen (Kellerei) prüfen, ob
    "Lief. ANr." wirklich unsere Lexware-Artikelnummer enthält und die "Artikelnummer"/"Artikelnr." neben
    der GTIN die Nummer des Kunden ist (Alias-Quelle für config/aliase/artikel.yaml).
    """
    if "artikelnummer_lieferant" in spalten:
        allgemein = spalten.pop("artikelnummer", None)
        spalten["artikelnummer"] = spalten.pop("artikelnummer_lieferant")
        if allgemein is not None and "kundenartikelnummer" not in spalten:
            spalten["kundenartikelnummer"] = allgemein
    elif "gtin" in spalten and "artikelnummer" in spalten and "kundenartikelnummer" not in spalten:
        spalten["kundenartikelnummer"] = spalten.pop("artikelnummer")
        logger.debug("Artikelnummer neben GTIN als Kundenartikelnummer gelesen")
    return spalten


def _spalten_aus_kopfzeile(zeile: list[str | None]) -> dict[str, int] | None:
    spalten: dict[str, int] = {}
    for index, zelle in enumerate(zeile):
        feld = _KOPF_ZU_FELD.get(_kopfzelle(zelle))
        if feld and feld not in spalten:
            spalten[feld] = index
    if len(spalten) >= 2 and ("bezeichnung" in spalten or "menge" in spalten or "menge_ve" in spalten):
        return _artikelnummern_zuordnen(spalten)
    return None


def _spalten_heuristisch(tabelle: list[list[str | None]]) -> dict[str, int] | None:
    """Spaltenzuordnung ohne Kopfzeile: Zahlen-, Preis- und Textspalten erkennen."""
    zeilen = [z for z in tabelle if any((c or "").strip() for c in z)]
    if len(zeilen) < 1:
        return None
    anzahl = max(len(z) for z in zeilen)
    if anzahl < 3:
        return None

    def spalte(i: int) -> list[str]:
        return [(z[i] or "").strip() if i < len(z) else "" for z in zeilen]

    ganzzahl = re.compile(r"^\d{1,6}$")
    preis = re.compile(rf"^(?:{_PREIS})\s*{_WAEHRUNG}$")
    quoten_int: dict[int, float] = {}
    quoten_preis: dict[int, float] = {}
    textlaenge: dict[int, float] = {}
    for i in range(anzahl):
        werte = [w for w in spalte(i) if w]
        if not werte:
            continue
        quoten_int[i] = sum(bool(ganzzahl.match(w)) for w in werte) / len(werte)
        quoten_preis[i] = sum(bool(preis.match(w)) for w in werte) / len(werte)
        textlaenge[i] = sum(len(w) for w in werte if _BUCHSTABEN.search(w)) / len(werte)

    spalten: dict[str, int] = {}
    preisspalten = [i for i, q in quoten_preis.items() if q >= 0.5]
    if preisspalten:
        spalten["preis"] = preisspalten[0]
        if len(preisspalten) > 1:
            spalten["gesamt"] = preisspalten[-1]
    intspalten = [i for i, q in quoten_int.items() if q >= 0.5 and i not in preisspalten]
    for i in list(intspalten):
        werte = [w for w in spalte(i) if w]
        if len(werte) >= 2 and [int(w) for w in werte if ganzzahl.match(w)] == list(range(1, len(werte) + 1)):
            spalten["pos"] = i
            intspalten.remove(i)
            break
    if len(intspalten) == 1:
        spalten["menge"] = intspalten[0]
    elif len(intspalten) >= 2:
        # Artikelnummern sind im Mittel größer als Mengen
        mittel = {
            i: sum(int(w) for w in spalte(i) if ganzzahl.match(w)) / max(1, len([w for w in spalte(i) if w]))
            for i in intspalten
        }
        artikel_i = max(mittel, key=mittel.get)  # type: ignore[arg-type]
        spalten["artikelnummer"] = artikel_i
        rest = [i for i in intspalten if i != artikel_i]
        spalten["menge"] = rest[0]
    textspalten = {i: laenge for i, laenge in textlaenge.items() if i not in spalten.values() and laenge > 0}
    if textspalten:
        spalten["bezeichnung"] = max(textspalten, key=textspalten.get)  # type: ignore[arg-type]
    if "menge" in spalten and "bezeichnung" in spalten:
        return spalten
    return None


_GELDBETRAG = re.compile(r"[,.]\d{2}\s*(?:€|EUR|Euro)?\s*$", re.I)


def _gesamtmenge_pruefen(position: RohPosition, gesamt_text: str | None) -> str | None:
    """Gegenprobe VE × Inhalt gegen eine "Gesamt"-Spalte mit Flaschenzahl (keine Geldbeträge)."""
    if position.ve_inhalt is None or position.menge_ve is None or not gesamt_text:
        return None
    if _GELDBETRAG.search(gesamt_text):
        return None
    gesamt = zahlen.parse_dezimal(gesamt_text)
    if gesamt is None or gesamt != gesamt.to_integral_value() or gesamt == position.menge:
        return None
    return (
        f"Gesamtmenge laut Dokument ({gesamt_text.strip()}) weicht von "
        f"{position.menge_ve} × {position.ve_inhalt} = {position.menge} Flaschen ab."
    )


def _positionen_aus_tabelle(
    tabelle: list[list[str | None]], kontrollen: list[tuple[RohPosition, str]] | None = None
) -> list[RohPosition]:
    """Positionen einer pdfplumber-Tabelle; ``kontrollen`` sammelt Abweichungen (Position, Text)."""
    positionen: list[RohPosition] = []
    spalten: dict[str, int] | None = None
    start = 0
    for index, zeile in enumerate(tabelle):
        spalten = _spalten_aus_kopfzeile(zeile)
        if spalten:
            start = index + 1
            break
    if spalten is None:
        spalten = _spalten_heuristisch(tabelle)
        if spalten is None:
            logger.debug("Tabelle ohne erkennbare Spalten übersprungen (%d Zeilen)", len(tabelle))
            return []
        logger.debug("Tabellenspalten heuristisch zugeordnet: %s", spalten)
    else:
        logger.debug("Tabellenspalten aus Kopfzeile: %s", spalten)

    def zelle(zeile: list[str | None], feld: str) -> str | None:
        i = spalten.get(feld)
        if i is None or i >= len(zeile):
            return None
        wert = zeile[i]
        return wert.strip() if wert and wert.strip() else None

    for zeile in tabelle[start:]:
        werte = [(c or "").strip() for c in zeile]
        if not any(werte):
            continue
        if _spalten_aus_kopfzeile(zeile):
            continue  # wiederholte Kopfzeile (Folgeseite)
        bez = zelle(zeile, "bezeichnung")
        menge = zelle(zeile, "menge")
        menge_ve = zelle(zeile, "menge_ve")
        mengenzelle = menge or menge_ve
        gefuellt = [w for w in werte if w]
        if mengenzelle is None and any(_SUMMENZEILE.match(w) for w in gefuellt):
            logger.debug("Summenzeile übersprungen: %s", gefuellt)
            continue
        if mengenzelle is None and bez and len(gefuellt) == 1 and positionen:
            # Fortsetzungszeile einer umgebrochenen Bezeichnung
            vorher = positionen[-1]
            bez_neu, hinweis_neu = _hinweis_trennen(f"{vorher.bezeichnung} {bez}", vorher.hinweis)
            positionen[-1] = vorher.model_copy(update={"bezeichnung": bez_neu, "hinweis": hinweis_neu})
            continue
        position = _baue_position(
            bezeichnung=bez,
            menge=menge,
            einheit=zelle(zeile, "einheit"),
            preis=zelle(zeile, "preis"),
            jahrgang=zelle(zeile, "jahrgang"),
            volumen_text=zelle(zeile, "volumen"),
            artikelnummer=zelle(zeile, "artikelnummer"),
            hinweis=zelle(zeile, "hinweis"),
            rohtext=" | ".join(gefuellt),
            menge_ve=menge_ve,
            ve_inhalt=zelle(zeile, "ve_inhalt"),
            gtin=zelle(zeile, "gtin"),
            kundenartikelnummer=zelle(zeile, "kundenartikelnummer"),
            rabatt=zelle(zeile, "rabatt"),
        )
        if position is None:
            continue
        positionen.append(position)
        if kontrollen is not None:
            abweichung = _gesamtmenge_pruefen(position, zelle(zeile, "gesamt"))
            if abweichung:
                kontrollen.append((position, abweichung))
    return positionen


def _positionen_aus_tabellen(
    tabellen: list[list[list[str | None]]], kontrollen: list[tuple[RohPosition, str]] | None = None
) -> list[RohPosition]:
    positionen: list[RohPosition] = []
    for tabelle in tabellen:
        positionen.extend(_positionen_aus_tabelle(tabelle, kontrollen))
    return positionen


# -- Zeilen -----------------------------------------------------------------

_STANDARD_SPALTEN = ["pos", "menge", "einheit", "bezeichnung", "preis", "gesamt"]
_ART_NR_TOKEN = re.compile(r"Art(?:ikel)?\.?\s*-?\s*(?:Nr|No)\.?", re.I)


def _erkenne_textkopfzeile(zeile: str) -> list[str] | None:
    """Erkennt eine Tabellenkopfzeile im Fließtext ("Pos Art.-Nr. Menge Einheit Artikel EK-Preis Gesamt")."""
    vorbereitet = _ART_NR_TOKEN.sub("ArtNr", zeile)
    spalten: dict[str, int] = {}
    for index, token in enumerate(vorbereitet.split()):
        feld = _KOPF_ZU_FELD.get(_kopfzelle(token))
        if feld and feld not in spalten:
            spalten[feld] = index
    felder = [feld for feld, _ in sorted(_artikelnummern_zuordnen(spalten).items(), key=lambda kv: kv[1])]
    if len(felder) >= 3 and "bezeichnung" in felder and ("menge" in felder or "menge_ve" in felder):
        if "bezeichnung" not in felder[:-1]:
            return None  # Bezeichnung als letzte Spalte: Preis-Spalten davor sind nicht sicher trennbar
        return felder
    return None


def _zeilenmuster(spalten: list[str]) -> re.Pattern[str]:
    """Baut den Positions-Regex aus der Spaltenreihenfolge."""
    bez_index = spalten.index("bezeichnung")
    vorne = spalten[:bez_index]
    hinten = spalten[bez_index + 1 :]
    menge_hinten = "menge" in hinten  # LEH-Layouts: "Artikelbez. Menge ME Inhalt Preis"
    teile = [r"^\s*"]
    leerraum_sicher = True  # endet der bisherige Ausdruck garantiert mit Leerraum (oder ist leer)?
    for feld in vorne:
        if feld == "pos":
            teile.append(r"(?:(?P<pos>\d{1,3})[.)]?\s+)?")
        elif feld == "artikelnummer":
            teile.append(r"(?P<artikelnummer>[A-Za-z0-9][A-Za-z0-9\-/.]{1,15})\s+")
            leerraum_sicher = True
        elif feld == "kundenartikelnummer":
            teile.append(r"(?P<kundenartikelnummer>[A-Za-z0-9][A-Za-z0-9\-/.]{1,15})\s+")
            leerraum_sicher = True
        elif feld == "gtin":
            teile.append(r"(?P<gtin>\d{8,14})\s+")
            leerraum_sicher = True
        elif feld == "menge":
            teile.append(r"(?P<menge>\d{1,4}(?:[.,]\d{1,2})?)\s*")
            leerraum_sicher = False
        elif feld == "menge_ve":
            teile.append(r"(?P<menge_ve>\d{1,5}(?:[.,]\d{1,3})?)\s*")
            leerraum_sicher = False
        elif feld == "ve_inhalt":
            teile.append(r"(?P<ve_inhalt>\d{1,3})\s+")
            leerraum_sicher = True
        elif feld == "einheit":
            teile.append(rf"(?:(?P<einheit>{_EINHEIT_ODER_X})\.?)?")
            leerraum_sicher = False
        # andere Felder vor der Bezeichnung (Jahrgang, Volumen) sind unüblich und werden ignoriert
    if "menge" not in vorne and "menge_ve" not in vorne and not menge_hinten:
        teile.append(r"(?P<menge>\d{1,4}(?:[.,]\d{1,2})?)\s*")
        leerraum_sicher = False
    if "einheit" not in vorne and not menge_hinten:
        teile.append(rf"(?:(?P<einheit>{_EINHEIT_ODER_X})\.?)?")
        leerraum_sicher = False
    teile.append((r"" if leerraum_sicher else r"\s+") + r"(?P<bez>.+?)")
    if menge_hinten:
        teile.append(r"\s+(?P<menge>\d{1,5}(?:[.,]\d{1,3})?)")
        teile.append(rf"(?:\s*(?P<einheit>{_EINHEIT_ODER_X})\.?)?")
    teile.append(r"(?:[,;]?\s+(?:à|á|je|@|zu|a|EP|Einzelpreis)\s*)?")
    for feld in hinten:
        if feld == "jahrgang":
            teile.append(r"(?:\s+(?P<jahrgang>(?:19|20)\d{2}))?")
        elif feld == "volumen":
            # "0,75 l" oder – Spalte "Inhalt" – eine nackte Flaschenzahl je Kolli ("6")
            teile.append(r"(?:\s+(?P<volumen>\d+(?:[.,]\d+)?\s*(?:l|cl|ml)|\d{1,3}(?![\d,.])))?")
        elif feld == "ve_inhalt" and "ve_inhalt" not in vorne:
            teile.append(r"(?:\s+(?P<ve_inhalt>\d{1,3}))?")
        elif feld == "mwst":
            teile.append(r"(?:\s+(?P<mwst>\d{1,2}(?:[.,]\d{1,2})?\s*%))?")
        elif feld == "rabatt":
            teile.append(r"(?:\s+(?P<rabatt>[-−–]?\s*\d{1,2}(?:[.,]\d{1,2})?\s*%))?")
    teile.append(rf"(?:\s*(?P<preis>{_PREIS})\s*{_WAEHRUNG}(?:\s*/\s*(?:Fl\.?|Stk\.?|Flasche|St\.?))?)?")
    teile.append(rf"(?:\s+=?\s*(?P<gesamt>{_PREIS})\s*{_WAEHRUNG})?")
    teile.append(r"\s*$")
    return re.compile("".join(teile), re.I)


# Folgezeilen unter einer Position (Shop-Bestellungen): "ArtNr: 11081", "Hinweis: 11+1", "Rabatt 10 %"
_ZUSATZ_ARTNR = re.compile(
    r"^(?:Art(?:ikel)?\.?\s*-?\s*(?:Nr|No|Nummer)\.?|SKU)\s*:?\s*(?P<nr>[A-Za-z0-9][A-Za-z0-9\-/.]{1,15})\s*$", re.I
)
_ZUSATZ_GTIN = re.compile(r"^(?:GTIN|EAN)\s*:?\s*(?P<nr>\d{8,14})\s*$", re.I)
_ZUSATZ_HINWEIS = re.compile(r"^(?:Hinweis|Bemerkung|Anmerkung|Info|Notiz|Kommentar)\s*:\s*(?P<text>.+)$", re.I)
_ZUSATZ_MAX_LAENGE = 40


def _zusatzzeile_anwenden(position: RohPosition, zeile: str) -> RohPosition | None:
    """Wendet eine Folgezeile auf die vorige Position an; ``None`` = keine Zusatzzeile."""
    s = zeile.strip()
    if _HINWEIS_MUSTER.fullmatch(s):
        return position.model_copy(update={"hinweis": _hinweis_anhaengen(position.hinweis, s)})
    treffer = _ZUSATZ_ARTNR.match(s)
    if treffer:
        if position.artikelnummer:
            return position
        return position.model_copy(update={"artikelnummer": treffer.group("nr")})
    treffer = _ZUSATZ_GTIN.match(s)
    if treffer:
        return position.model_copy(update={"gtin": treffer.group("nr")})
    treffer = _ZUSATZ_HINWEIS.match(s)
    if treffer:
        text = treffer.group("text").strip()
        update: dict[str, object] = {"hinweis": _hinweis_anhaengen(position.hinweis, text)}
        rabatt = zahlen.parse_rabatt(text)
        if rabatt is not None and position.rabatt_prozent is None:
            update.update(rabatt_prozent=rabatt.prozent, rabatt_text=rabatt.rohtext)
        return position.model_copy(update=update)
    rabatt = zahlen.parse_rabatt(s)
    if rabatt is not None and len(s) <= _ZUSATZ_MAX_LAENGE and position.rabatt_prozent is None:
        return position.model_copy(update={"rabatt_prozent": rabatt.prozent, "rabatt_text": rabatt.rohtext})
    return None


_STANDARD_MUSTER = _zeilenmuster(_STANDARD_SPALTEN)


def _positionen_aus_zeilen(zeilen: list[str], *, kopfzeilen_beachten: bool = True) -> list[RohPosition]:
    """Liest Positionen aus Textzeilen; eine erkannte Kopfzeile steuert die Spaltenreihenfolge.

    Liefert die kopfzeilengesteuerte Variante nichts, wird ohne Kopfzeile erneut versucht.
    """
    positionen: list[RohPosition] = []
    muster = _STANDARD_MUSTER
    kopf_erkannt = False
    letzte_war_position = False
    for zeile in zeilen:
        s = zeile.strip()
        if not s:
            letzte_war_position = False
            continue
        kopf = _erkenne_textkopfzeile(s) if kopfzeilen_beachten else None
        if kopf:
            muster = _zeilenmuster(kopf)
            kopf_erkannt = True
            logger.debug("Textkopfzeile erkannt, Spalten: %s", kopf)
            letzte_war_position = False
            continue
        treffer = muster.match(s)
        if not treffer:
            ergaenzt = _zusatzzeile_anwenden(positionen[-1], s) if letzte_war_position and positionen else None
            if ergaenzt is not None:
                positionen[-1] = ergaenzt
            else:
                letzte_war_position = False
            continue
        g = treffer.groupdict()
        bez = g.get("bez") or ""
        if (
            not _BUCHSTABEN.search(bez)
            or _UNERWUENSCHTER_ANFANG.match(bez)
            or (
                g.get("preis") is None
                and g.get("einheit") is None
                and g.get("artikelnummer") is None
                and g.get("gtin") is None
                and g.get("menge_ve") is None
            )
        ):
            letzte_war_position = False
            continue
        position = _baue_position(
            bezeichnung=bez,
            menge=g.get("menge"),
            einheit=g.get("einheit"),
            preis=g.get("preis"),
            jahrgang=g.get("jahrgang"),
            volumen_text=g.get("volumen"),
            artikelnummer=g.get("artikelnummer"),
            rohtext=s,
            menge_ve=g.get("menge_ve"),
            ve_inhalt=g.get("ve_inhalt"),
            gtin=g.get("gtin"),
            kundenartikelnummer=g.get("kundenartikelnummer"),
            rabatt=g.get("rabatt"),
        )
        if position is None or position.menge is None:
            letzte_war_position = False
            continue
        positionen.append(position)
        letzte_war_position = True
    if not positionen and kopf_erkannt:
        logger.debug("Kopfzeilengesteuerte Zeilenheuristik ohne Treffer, versuche Standardmuster")
        return _positionen_aus_zeilen(zeilen, kopfzeilen_beachten=False)
    return positionen


# ---------------------------------------------------------------------------
# Summenvermerke
# ---------------------------------------------------------------------------

_SUMMEN_EINHEIT = r"(?P<einheit>Flaschen|Flasche|Fl\.?|Stück|Stk\.?|Kartons?|Kt\.?|R|Btl\.?|Bottles?)"
_SUMME_MIT_SCHLUESSEL = re.compile(
    r"\b(?P<schluessel>Gesamtmenge|Gesamtanzahl|Flaschen\s+gesamt|Menge\s+gesamt|Gesamt|Summe|Insgesamt|Total)"
    rf"\s*:?\s*(?P<wert>\d{{1,5}}(?:[.,]\d{{1,2}})?)\s*{_SUMMEN_EINHEIT}?(?!\w)",
    re.I,
)
_SUMME_ALLEIN = re.compile(rf"^\s*(?:ca\.\s*)?(?P<wert>\d{{1,5}})\s*{_SUMMEN_EINHEIT}\s*\.?\s*$", re.I)
_MENGEN_SCHLUESSEL = {"gesamtmenge", "gesamtanzahl", "flaschen gesamt", "menge gesamt"}


def _summenvermerke(zeilen: list[str]) -> list[Summenvermerk]:
    vermerke: list[Summenvermerk] = []
    gesehen: set[tuple[Decimal, str | None, str]] = set()

    def merken(wert_text: str, einheit_text: str | None, rohtext: str) -> None:
        wert = zahlen.parse_dezimal(wert_text)
        if wert is None or wert <= 0:
            return
        einheit = zahlen.normalisiere_einheit(einheit_text) if einheit_text else None
        schluessel = (wert, einheit, rohtext)
        if schluessel in gesehen:
            return
        gesehen.add(schluessel)
        vermerke.append(Summenvermerk(wert=wert, einheit=einheit, rohtext=rohtext))

    for zeile in zeilen:
        s = zeile.strip()
        if not s:
            continue
        treffer = _SUMME_MIT_SCHLUESSEL.search(s)
        if treffer:
            wert_text = treffer.group("wert")
            einheit_text = treffer.group("einheit")
            schluessel = re.sub(r"\s+", " ", treffer.group("schluessel").casefold())
            rest = s[treffer.end() :].lstrip(" .,")
            ist_geld = ("," in wert_text or "." in wert_text) or re.match(r"(?:€|EUR|Euro)", rest, re.I)
            # Ohne Einheit nur bei eindeutigen Mengen-Schlüsseln ("Gesamtmenge: 97"); Geldbeträge nie.
            if einheit_text or (schluessel in _MENGEN_SCHLUESSEL and not ist_geld):
                merken(wert_text, einheit_text, treffer.group(0).strip())
                continue
        treffer = _SUMME_ALLEIN.match(s)
        if treffer:
            merken(treffer.group("wert"), treffer.group("einheit"), s)
    return vermerke


# ---------------------------------------------------------------------------
# Extraktor
# ---------------------------------------------------------------------------


def _hinweis(code: Pruefcode, grad: Schweregrad, text: str, **details: str) -> Pruefhinweis:
    return Pruefhinweis(code=code, schweregrad=grad, text=text, details=details)


class TextPdfExtraktor:
    """Extraktor für Text-PDFs (Eingangstyp A, bei eindeutigen Hinweisen auch B)."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._eigene_namen: list[str] = []
        self._eigene_anzeige: dict[str, str] = {}  # normalisierter Name -> Firmenname laut Konfiguration
        for firma in settings.firmen.values():
            for name in (firma.name, firma.lexware_firmenname):
                norm = _normalisiere(name)
                if len(norm) >= 4 and norm not in self._eigene_namen:
                    self._eigene_namen.append(norm)
                    self._eigene_anzeige[norm] = firma.name

    # -- Schnittstelle ------------------------------------------------------

    def kann_verarbeiten(self, datei: Path) -> bool:
        datei = Path(datei)
        return datei.suffix.lower() == ".pdf" and ist_text_pdf(datei)

    def extrahiere(self, datei: Path) -> RohBestellung:
        datei = Path(datei)
        inhalt = lese_pdf(datei)
        if not inhalt.text.strip():
            logger.warning("%s: keine Textebene gefunden", datei.name)
            return RohBestellung(
                eingangstyp=Eingangstyp.KUNDENBESTELLUNG_PDF,
                quelldatei=datei,
                extraktionsmethode=Extraktionsmethode.PDFPLUMBER,
                hinweise=[
                    _hinweis(
                        Pruefcode.FELD_FEHLT,
                        Schweregrad.FEHLER,
                        "PDF enthält keine Textebene – bitte als Scan (Vision) verarbeiten.",
                    )
                ],
                rohtext=inhalt.text,
                seiten=inhalt.seiten,
            )
        return self._baue_bestellung(datei, inhalt)

    def extrahiere_text(self, text: str, quelldatei: Path, *, layout_text: str | None = None) -> RohBestellung:
        """Wendet dieselben Heuristiken auf reinen Text an (z. B. den Text einer E-Mail).

        ``layout_text`` kann eine Variante mit erhaltenen Spaltenabständen sein; ohne
        Angabe wird ``text`` verwendet.
        """
        return self._baue_bestellung(Path(quelldatei), inhalt_aus_text(text, layout_text))

    # -- Aufbau -------------------------------------------------------------

    def _ist_eigene_firma(self, text: str | None) -> bool:
        norm = _normalisiere(text)
        return bool(norm) and any(name in norm for name in self._eigene_namen)

    def _eigene_firma_im_text(self, text: str | None) -> str | None:
        """Name der eigenen Firma, die im Text genannt wird (längster Treffer), sonst ``None``."""
        norm = _normalisiere(text)
        if not norm:
            return None
        treffer = [name for name in self._eigene_namen if name in norm]
        if not treffer:
            return None
        return self._eigene_anzeige[max(treffer, key=len)]

    @staticmethod
    def _block_mit_rolle(bloecke: list[_Adressblock], *rollen: str) -> _Adressblock | None:
        for rolle in rollen:
            for block in bloecke:
                if block.rolle == rolle:
                    return block
        return None

    def _baue_bestellung(self, datei: Path, inhalt: PdfInhalt) -> RohBestellung:
        # Segmente (Layout) zuerst, damit nebeneinander stehende Blöcke nicht verschmelzen.
        kopfzeilen = [*inhalt.segmente, *inhalt.zeilen]
        kopf = lese_kopffelder(kopfzeilen)
        bloecke = _adressbloecke(self._zeilen_fuer_adressbloecke(inhalt))
        eigene = [b for b in bloecke if any(self._ist_eigene_firma(z) for z in b.zeilen)]
        fremde = [b for b in bloecke if b not in eigene]
        typ = self._erkenne_eingangstyp(kopf, bloecke, eigene)
        hinweise: list[Pruefhinweis] = []

        # Kunde / Lieferant
        kunde_text: str | None = kopf.get("kunde")
        kunde_adresse: str | None = None
        lieferant_text: str | None = kopf.get("lieferant")
        if typ == Eingangstyp.KUNDENBESTELLUNG_PDF:
            if kunde_text is None and fremde:
                # Block mit Rolle "Kunde/Rechnungsempfänger" vor unbenannten Blöcken vor Lieferadressen
                kunde_block = (
                    self._block_mit_rolle(fremde, "kunde")
                    or next((b for b in fremde if b.rolle is None), None)
                    or self._block_mit_rolle(fremde, "lieferadresse")
                    or fremde[0]
                )
                kunde_text = kunde_block.name
                kunde_adresse = kunde_block.adresse or None
        else:
            if lieferant_text is None:
                an = kopf.get("bestellung_an")
                if an and not self._ist_eigene_firma(an):
                    lieferant_text = an
                elif fremde:
                    lieferant_block = self._block_mit_rolle(fremde, "empfaenger", "ladeadresse") or fremde[0]
                    lieferant_text = lieferant_block.name
        if kunde_text is not None:
            kunde_text = kunde_text.strip().rstrip(",;")

        # Nummern: bei Kundenbestellungen ist die Bestellnummer des Kunden die Kundenbestellnummer
        if typ == Eingangstyp.KUNDENBESTELLUNG_PDF:
            bestellnummer = kopf.get("ihre_bestellnummer") or kopf.get("bestellnummer")
            belegnummer = kopf.get("belegnummer")
            if bestellnummer is None and belegnummer:
                bestellnummer = belegnummer
                hinweise.append(
                    _hinweis(
                        Pruefcode.FELD_FEHLT,
                        Schweregrad.INFO,
                        f"Keine Bestellnummer genannt – Belegnummer {belegnummer} des Kunden wird als "
                        "Kundenbestellnummer übernommen.",
                        belegnummer=belegnummer,
                    )
                )
        else:
            belegnummer = kopf.get("belegnummer") or kopf.get("bestellnummer")
            bestellnummer = kopf.get("ihre_bestellnummer")

        # Unsere Lieferantennummer beim Kunden ("Lieferanten-Nr." aus Sicht des Kunden) -> nur Hinweis
        lieferantennummer = kopf.get("lieferantennummer")
        if lieferantennummer:
            if typ == Eingangstyp.KUNDENBESTELLUNG_PDF:
                text = f"Unsere Lieferantennummer beim Kunden laut Dokument: {lieferantennummer}."
            else:
                text = f"Lieferantennummer laut Dokument: {lieferantennummer}."
            hinweise.append(
                _hinweis(Pruefcode.FELD_FEHLT, Schweregrad.INFO, text, lieferantennummer=lieferantennummer)
            )

        # Daten
        datum = (
            zahlen.parse_datum(kopf.get("datum"))
            or zahlen.parse_datum(kopf.get("datum_ort"))
            or zahlen.parse_datum(kopf.get("datum_vom"))
        )
        liefertag_roh = kopf.get("liefertag")
        liefertag = zahlen.parse_datum(liefertag_roh)
        lieferadresse = self._adressfeld(kopf, inhalt, "lieferadresse", self._block_mit_rolle(bloecke, "lieferadresse"))
        ladeadresse = self._adressfeld(kopf, inhalt, "ladeadresse", self._block_mit_rolle(bloecke, "ladeadresse"))
        lieferbedingung = kopf.get("lieferbedingung") or finde_lieferbedingung(kopfzeilen)

        # Revised Order / Aktionshinweis (Belegebene)
        revision = finde_revision(kopfzeilen)
        if revision is not None:
            hinweise.append(revisions_hinweis(revision))
        aktionshinweis = finde_aktionshinweis(kopfzeilen)

        # Positionen
        kontrollen: list[tuple[RohPosition, str]] = []
        positionen = _positionen_aus_tabellen(inhalt.tabellen, kontrollen)
        strategie = "tabellen"
        if not positionen:
            for name, zeilen in (("zeilen", inhalt.zeilen), ("layout", inhalt.segmente)):
                positionen = _positionen_aus_zeilen(zeilen)
                if positionen:
                    strategie = name
                    break
        for nr, position in enumerate(positionen, start=1):
            position.laufnr = nr
            if position.menge is None:
                hinweise.append(
                    Pruefhinweis(
                        code=Pruefcode.FELD_FEHLT,
                        schweregrad=Schweregrad.WARNUNG,
                        text=f"Position {nr}: Menge nicht erkannt ({position.bezeichnung}).",
                        position_laufnr=nr,
                    )
                )
            if position.rabatt_prozent is not None:
                logger.info(
                    "%s: Position %d: Rabatt laut Dokument %s %% (%s)",
                    datei.name,
                    nr,
                    position.rabatt_prozent,
                    position.rabatt_text,
                )
        for position, text in kontrollen:
            hinweise.append(
                Pruefhinweis(
                    code=Pruefcode.SUMME_ABWEICHUNG,
                    schweregrad=Schweregrad.WARNUNG,
                    text=f"Position {position.laufnr}: {text}",
                    position_laufnr=position.laufnr,
                    details={"menge": str(position.menge), "menge_ve": str(position.menge_ve), "ve_inhalt": str(position.ve_inhalt)},
                )
            )

        summenvermerke = _summenvermerke([*inhalt.zeilen, *inhalt.segmente])
        for vermerk in summenvermerke:
            hinweise.append(
                _hinweis(
                    Pruefcode.SUMMENVERMERK,
                    Schweregrad.INFO,
                    f"Summenvermerk erkannt: {vermerk.rohtext}",
                    wert=str(vermerk.wert),
                    einheit=vermerk.einheit or "",
                )
            )

        # Fehlende Kopffelder
        if not kunde_text:
            hinweise.append(_hinweis(Pruefcode.FELD_FEHLT, Schweregrad.WARNUNG, "Kunde nicht erkannt."))
        if typ == Eingangstyp.KUNDENBESTELLUNG_PDF and not bestellnummer:
            hinweise.append(_hinweis(Pruefcode.FELD_FEHLT, Schweregrad.WARNUNG, "Bestellnummer nicht erkannt."))
        if typ == Eingangstyp.EIGENE_BESTELLUNG_EMAIL:
            if not belegnummer:
                hinweise.append(_hinweis(Pruefcode.FELD_FEHLT, Schweregrad.WARNUNG, "Belegnummer nicht erkannt."))
            if not lieferant_text:
                hinweise.append(
                    _hinweis(Pruefcode.FELD_FEHLT, Schweregrad.WARNUNG, "Lieferant/Weingut nicht erkannt.")
                )
        if datum is None:
            hinweise.append(_hinweis(Pruefcode.FELD_FEHLT, Schweregrad.WARNUNG, "Belegdatum nicht erkannt."))
        if liefertag is None:
            text = "Liefertag nicht erkannt."
            if liefertag_roh:
                text = f"Liefertag nicht als Datum lesbar: '{liefertag_roh}'."
            hinweise.append(
                _hinweis(Pruefcode.FELD_FEHLT, Schweregrad.WARNUNG, text, rohtext=liefertag_roh or "")
            )
        if not positionen:
            hinweise.append(
                _hinweis(Pruefcode.FELD_FEHLT, Schweregrad.FEHLER, "Keine Positionen im PDF erkannt.")
            )

        # Welche unserer Firmen im Dokument steht (Adressblock, sonst Nennung im Text)
        empfaenger_firma_text = eigene[0].name if eigene else self._eigene_firma_im_text(inhalt.text)

        logger.info(
            "%s: Typ %s, %d Positionen (%s), Kunde %r, Lieferant %r, Bestellnr %r, Belegnr %r, Revision %s",
            datei.name,
            typ.value,
            len(positionen),
            strategie,
            kunde_text,
            lieferant_text,
            bestellnummer,
            belegnummer,
            revision is not None,
        )
        return RohBestellung(
            eingangstyp=typ,
            quelldatei=datei,
            extraktionsmethode=Extraktionsmethode.PDFPLUMBER,
            kunde_text=kunde_text or None,
            kunde_adresse=kunde_adresse,
            lieferant_text=lieferant_text or None,
            belegnummer=belegnummer,
            bestellnummer=bestellnummer,
            datum=datum,
            liefertag=liefertag,
            lieferadresse=lieferadresse,
            positionen=positionen,
            summenvermerke=summenvermerke,
            hinweise=hinweise,
            rohtext=inhalt.text,
            seiten=inhalt.seiten,
            kundennummer_dokument=(kopf.get("kundennummer") or None),
            empfaenger_firma_text=empfaenger_firma_text,
            ladeadresse=ladeadresse,
            lieferbedingung=lieferbedingung,
            ist_revision=revision is not None,
            revision_von=revision.revision_von if revision else None,
            aktionshinweis=aktionshinweis,
        )

    def _erkenne_eingangstyp(
        self, kopf: dict[str, str], bloecke: list[_Adressblock], eigene: list[_Adressblock]
    ) -> Eingangstyp:
        # TODO-LEXWARE: Prüfen, welche Merkmale ein als PDF gedruckter Lexware-Bestellbeleg trägt
        # (Überschrift "Bestellung", Belegnummernformat, ob ein Kundenfeld gedruckt wird), damit
        # eigene Bestellungen (Typ B) zuverlässig von Kundenbestellungen (Typ A) unterschieden werden.
        an = kopf.get("bestellung_an")
        if an and not self._ist_eigene_firma(an):
            logger.debug("Eingangstyp B: 'Bestellung ... an %s'", an)
            return Eingangstyp.EIGENE_BESTELLUNG_EMAIL
        if "lieferant" in kopf and "kunde" in kopf:
            logger.debug("Eingangstyp B: Felder 'Lieferant:' und 'Kunde:' vorhanden")
            return Eingangstyp.EIGENE_BESTELLUNG_EMAIL
        if bloecke and eigene and bloecke[0] is eigene[0] and not bloecke[0].als_empfaenger_markiert:
            if any(b not in eigene for b in bloecke):
                logger.debug("Eingangstyp B: eigene Firma als Absenderblock")
                return Eingangstyp.EIGENE_BESTELLUNG_EMAIL
        return Eingangstyp.KUNDENBESTELLUNG_PDF

    @staticmethod
    def _zeilen_fuer_adressbloecke(inhalt: PdfInhalt) -> list[str]:
        """Spaltenweise Layout-Zeilen von Seite 1 (durch Leerzeilen getrennt), sonst Fließtextzeilen.

        Aufeinanderfolgende Leerzeilen einer Spalte werden zu einer zusammengefasst, damit auch
        rechte Spalten (Rechnungs-/Warenempfänger) innerhalb des Suchfensters liegen.
        """
        if not inhalt.layout_spalten_seite1:
            return inhalt.segmente_seite1 or inhalt.zeilen
        zeilen: list[str] = []
        for spalte in inhalt.layout_spalten_seite1:
            for zeile in spalte:
                if zeile or (zeilen and zeilen[-1]):
                    zeilen.append(zeile)
            if zeilen and zeilen[-1]:
                zeilen.append("")
        return zeilen

    @staticmethod
    def _adressfeld(
        kopf: dict[str, str], inhalt: PdfInhalt, feld: str, block: _Adressblock | None = None
    ) -> str | None:
        """Adressfeld (lieferadresse/ladeadresse): Wert in der Schlüsselzeile, sonst der Adressblock unter der
        Überschrift (spaltenweise gelesen), sonst die Folgezeilen bis zur PLZ-Zeile."""
        wert = kopf.get(feld)
        if wert:
            return wert
        if block is not None:
            return block.volltext
        # Schlüsselwort ohne Wert in derselben Zeile: Folgezeilen bis zur PLZ-Zeile einsammeln.
        muster = _KOPF_MUSTER[feld]
        for quelle in (inhalt.segmente, inhalt.zeilen):
            for index, zeile in enumerate(quelle):
                treffer = muster.search(zeile)
                if not treffer or treffer.group("wert").strip():
                    continue
                gesammelt: list[str] = []
                for folge in quelle[index + 1 : index + 5]:
                    s = folge.strip()
                    if not s or _SCHLUESSELZEILE.match(s) or _ist_kopffeldzeile(s):
                        break
                    gesammelt.append(s)
                    if _PLZ_ZEILE.match(s):
                        break
                if gesammelt:
                    return ", ".join(gesammelt)
        return None
