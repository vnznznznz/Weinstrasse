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
    "TextPdfExtraktor",
    "TextPdfFehler",
    "inhalt_aus_text",
    "ist_text_pdf",
    "lese_kopffelder",
    "lese_pdf",
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
        r"|Versandanschrift|Warenempfänger|Abweichende\s+Lieferadresse)\s*:?\s*(?P<wert>[^\n]*)",
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
_NUMMERNFELDER = {"kundennummer", "ihre_bestellnummer", "bestellnummer", "belegnummer"}
_DATUMSFELDER = {"datum", "datum_ort", "datum_vom", "liefertag"}


def _bereinige_wert(feld: str, wert: str) -> str | None:
    wert = wert.strip().rstrip(".,:;").strip()
    if not wert:
        return None
    if feld in _NUMMERNFELDER and not re.search(r"\d", wert):
        return None  # "Ihre Bestellung vom ..." -> kein Wert
    if feld in _DATUMSFELDER:
        return wert if zahlen.finde_datum(wert) or feld == "liefertag" else None
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
        for feld, muster in _KOPF_MUSTER.items():
            if feld in felder:
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


_SCHLUESSELZEILE = re.compile(r"^[\wäöüÄÖÜß.\-/ ]{2,30}:\s*\S")
_UEBERSCHRIFT = re.compile(
    r"^(?:Bestellung|Auftrag|Auftragsbestätigung|Order|Angebot|Rechnung|Lieferschein|Pos\b|Position|Sehr geehrte)",
    re.I,
)
_EMPFAENGER_MARKER = re.compile(r"^(?:an|firma|herrn?|frau|z\.?\s?hd\.?.*|zu\s+händen.*)\s*:?$", re.I)
_PLZ_ZEILE = re.compile(r"^(?:[A-Z]{1,2}[- ])?\d{4,5}\s+[A-Za-zÄÖÜ]")
_LAENDER = re.compile(
    r"^(?:Deutschland|Germany|Italien|Italia|Italy|Österreich|Austria|Schweiz|Switzerland|Frankreich|France"
    r"|Spanien|España|Spain|Portugal|Niederlande|Nederland|Luxemburg|Belgien)$",
    re.I,
)


@dataclass
class _Adressblock:
    zeilen: list[str]
    als_empfaenger_markiert: bool = False

    @property
    def name(self) -> str:
        return self.zeilen[0]

    @property
    def adresse(self) -> str:
        return ", ".join(self.zeilen[1:])


def _teile_adresszeile(zeile: str) -> list[str]:
    """Zerlegt einzeilige Absenderzeilen ("Firma · Straße 1 · 12345 Ort")."""
    for trenner in (r"\s*[·•|]\s*", r"\s+[–-]\s+", r",\s*"):
        teile = [t.strip() for t in re.split(trenner, zeile) if t.strip()]
        if len(teile) >= 2 and _PLZ_ZEILE.match(teile[-1]):
            return teile
    return [zeile]


def _adressbloecke(zeilen: list[str], maximal: int = 80) -> list[_Adressblock]:
    """Findet Adressblöcke (Name, [Zusatz], Straße, PLZ Ort) im Kopfbereich."""
    bloecke: list[_Adressblock] = []
    aktuell: list[str] = []
    markiert = False
    for zeile in zeilen[:maximal]:
        s = zeile.strip()
        if not s:
            aktuell, markiert = [], False
            continue
        if _EMPFAENGER_MARKER.match(s):
            aktuell, markiert = [], True
            continue
        if _SCHLUESSELZEILE.match(s) or _UEBERSCHRIFT.match(s) or _ist_kopffeldzeile(s):
            aktuell, markiert = [], False
            continue
        if _LAENDER.match(s):
            aktuell = []
            continue
        teile = _teile_adresszeile(s)
        if len(teile) >= 2:
            bloecke.append(_Adressblock(teile, markiert))
            aktuell, markiert = [], False
            continue
        aktuell.append(s)
        if _PLZ_ZEILE.match(s):
            if len(aktuell) >= 2:
                bloecke.append(_Adressblock(aktuell[-5:], markiert))
            aktuell, markiert = [], False
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
        {"artnr", "artikelnr", "artikelnummer", "art", "artno", "sku", "ean", "itemno", "artikelno", "nummer"}
    ),
    "menge": frozenset({"menge", "anzahl", "qty", "quantity", "stueck", "stk", "bestellmenge", "mge"}),
    "einheit": frozenset({"einheit", "me", "einh", "mengeneinheit", "unit", "vpe"}),
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
) -> RohPosition | None:
    bez_roh = re.sub(r"\s+", " ", (bezeichnung or "").replace("\n", " ")).strip()
    bez, hinweis_text = _hinweis_trennen(bez_roh, hinweis)

    menge_obj = zahlen.parse_menge(menge)
    einheit_norm = zahlen.normalisiere_einheit(einheit) or (menge_obj.einheit if menge_obj else None)
    if not bez and menge_obj is None:
        return None

    jahrgang_wert: int | None = None
    if jahrgang and re.fullmatch(r"\s*(?:19|20)\d{2}\s*", jahrgang):
        jahrgang_wert = int(jahrgang)
    if jahrgang_wert is None:
        jahrgang_wert = volumen.parse_jahrgang(bez)

    volumen_cl = volumen.parse_volumen_cl(volumen_text) if volumen_text else None
    if volumen_cl is None:
        volumen_cl = volumen.parse_volumen_cl(bez)

    artnr = (artikelnummer or "").strip().rstrip(".,")
    if not artnr or not re.search(r"\d", artnr):
        artnr = ""

    return RohPosition(
        laufnr=0,
        bezeichnung=bez,
        jahrgang=jahrgang_wert,
        menge=menge_obj.wert if menge_obj else None,
        einheit=einheit_norm,
        preis=zahlen.parse_dezimal(preis),
        volumen_cl=volumen_cl,
        artikelnummer=artnr or None,
        hinweis=hinweis_text,
        durchgestrichen=False,  # pdfplumber erkennt Streichungen nicht
        rohtext=rohtext,
    )


# -- Tabellen ---------------------------------------------------------------


def _spalten_aus_kopfzeile(zeile: list[str | None]) -> dict[str, int] | None:
    spalten: dict[str, int] = {}
    for index, zelle in enumerate(zeile):
        feld = _KOPF_ZU_FELD.get(_kopfzelle(zelle))
        if feld and feld not in spalten:
            spalten[feld] = index
    if len(spalten) >= 2 and ("bezeichnung" in spalten or "menge" in spalten):
        return spalten
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


def _positionen_aus_tabelle(tabelle: list[list[str | None]]) -> list[RohPosition]:
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
        gefuellt = [w for w in werte if w]
        if menge is None and any(_SUMMENZEILE.match(w) for w in gefuellt):
            logger.debug("Summenzeile übersprungen: %s", gefuellt)
            continue
        if menge is None and bez and len(gefuellt) == 1 and positionen:
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
        )
        if position is not None:
            positionen.append(position)
    return positionen


def _positionen_aus_tabellen(tabellen: list[list[list[str | None]]]) -> list[RohPosition]:
    positionen: list[RohPosition] = []
    for tabelle in tabellen:
        positionen.extend(_positionen_aus_tabelle(tabelle))
    return positionen


# -- Zeilen -----------------------------------------------------------------

_STANDARD_SPALTEN = ["pos", "menge", "einheit", "bezeichnung", "preis", "gesamt"]
_ART_NR_TOKEN = re.compile(r"Art(?:ikel)?\.?\s*-?\s*(?:Nr|No)\.?", re.I)


def _erkenne_textkopfzeile(zeile: str) -> list[str] | None:
    """Erkennt eine Tabellenkopfzeile im Fließtext ("Pos Art.-Nr. Menge Einheit Artikel EK-Preis Gesamt")."""
    vorbereitet = _ART_NR_TOKEN.sub("ArtNr", zeile)
    felder: list[str] = []
    for token in vorbereitet.split():
        feld = _KOPF_ZU_FELD.get(_kopfzelle(token))
        if feld and feld not in felder:
            felder.append(feld)
    if len(felder) >= 3 and "bezeichnung" in felder and "menge" in felder:
        if "bezeichnung" not in felder[:-1]:
            return None  # Bezeichnung als letzte Spalte: Preis-Spalten davor sind nicht sicher trennbar
        return felder
    return None


def _zeilenmuster(spalten: list[str]) -> re.Pattern[str]:
    """Baut den Positions-Regex aus der Spaltenreihenfolge."""
    bez_index = spalten.index("bezeichnung")
    vorne = spalten[:bez_index]
    hinten = spalten[bez_index + 1 :]
    teile = [r"^\s*"]
    for feld in vorne:
        if feld == "pos":
            teile.append(r"(?:(?P<pos>\d{1,3})[.)]?\s+)?")
        elif feld == "artikelnummer":
            teile.append(r"(?P<artikelnummer>[A-Za-z0-9][A-Za-z0-9\-/.]{1,15})\s+")
        elif feld == "menge":
            teile.append(r"(?P<menge>\d{1,4}(?:[.,]\d{1,2})?)\s*")
        elif feld == "einheit":
            teile.append(rf"(?:(?P<einheit>{_EINHEIT_ODER_X})\.?)?")
        # andere Felder vor der Bezeichnung (Jahrgang, Volumen) sind unüblich und werden ignoriert
    if "menge" not in vorne:
        teile.append(r"(?P<menge>\d{1,4}(?:[.,]\d{1,2})?)\s*")
    if "einheit" not in vorne:
        teile.append(rf"(?:(?P<einheit>{_EINHEIT_ODER_X})\.?)?")
    teile.append(r"\s+(?P<bez>.+?)")
    teile.append(r"(?:[,;]?\s+(?:à|á|je|@|zu|a|EP|Einzelpreis)\s*)?")
    for feld in hinten:
        if feld == "jahrgang":
            teile.append(r"(?:\s+(?P<jahrgang>(?:19|20)\d{2}))?")
        elif feld == "volumen":
            teile.append(r"(?:\s+(?P<volumen>\d+(?:[.,]\d+)?\s*(?:l|cl|ml)))?")
    teile.append(rf"(?:\s*(?P<preis>{_PREIS})\s*{_WAEHRUNG}(?:\s*/\s*(?:Fl\.?|Stk\.?|Flasche|St\.?))?)?")
    teile.append(rf"(?:\s+=?\s*(?P<gesamt>{_PREIS})\s*{_WAEHRUNG})?")
    teile.append(r"\s*$")
    return re.compile("".join(teile), re.I)


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
            if letzte_war_position and positionen and _HINWEIS_MUSTER.fullmatch(s):
                vorher = positionen[-1]
                hinweis = f"{vorher.hinweis}; {s}" if vorher.hinweis else s
                positionen[-1] = vorher.model_copy(update={"hinweis": hinweis})
            else:
                letzte_war_position = False
            continue
        g = treffer.groupdict()
        bez = g.get("bez") or ""
        if (
            not _BUCHSTABEN.search(bez)
            or _UNERWUENSCHTER_ANFANG.match(bez)
            or (g.get("preis") is None and g.get("einheit") is None and g.get("artikelnummer") is None)
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
        for firma in settings.firmen.values():
            for name in (firma.name, firma.lexware_firmenname):
                norm = _normalisiere(name)
                if len(norm) >= 4 and norm not in self._eigene_namen:
                    self._eigene_namen.append(norm)

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

    def _baue_bestellung(self, datei: Path, inhalt: PdfInhalt) -> RohBestellung:
        # Segmente (Layout) zuerst, damit nebeneinander stehende Blöcke nicht verschmelzen.
        kopf = lese_kopffelder([*inhalt.segmente, *inhalt.zeilen])
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
                kunde_text = fremde[0].name
                kunde_adresse = fremde[0].adresse or None
        else:
            if lieferant_text is None:
                an = kopf.get("bestellung_an")
                if an and not self._ist_eigene_firma(an):
                    lieferant_text = an
                elif fremde:
                    lieferant_text = fremde[0].name
        if kunde_text is not None:
            kunde_text = kunde_text.strip().rstrip(",;")

        # Nummern
        if typ == Eingangstyp.KUNDENBESTELLUNG_PDF:
            bestellnummer = kopf.get("ihre_bestellnummer") or kopf.get("bestellnummer")
            belegnummer = kopf.get("belegnummer")
        else:
            belegnummer = kopf.get("belegnummer") or kopf.get("bestellnummer")
            bestellnummer = kopf.get("ihre_bestellnummer")

        # Daten
        datum = (
            zahlen.parse_datum(kopf.get("datum"))
            or zahlen.parse_datum(kopf.get("datum_ort"))
            or zahlen.parse_datum(kopf.get("datum_vom"))
        )
        liefertag_roh = kopf.get("liefertag")
        liefertag = zahlen.parse_datum(liefertag_roh)
        lieferadresse = self._lieferadresse(kopf, inhalt)

        # Positionen
        positionen = _positionen_aus_tabellen(inhalt.tabellen)
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

        logger.info(
            "%s: Typ %s, %d Positionen (%s), Kunde %r, Lieferant %r, Bestellnr %r, Belegnr %r",
            datei.name,
            typ.value,
            len(positionen),
            strategie,
            kunde_text,
            lieferant_text,
            bestellnummer,
            belegnummer,
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
            empfaenger_firma_text=(eigene[0].name if eigene else None),
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
        """Spaltenweise Layout-Zeilen von Seite 1 (durch Leerzeilen getrennt), sonst Fließtextzeilen."""
        if not inhalt.layout_spalten_seite1:
            return inhalt.segmente_seite1 or inhalt.zeilen
        zeilen: list[str] = []
        for spalte in inhalt.layout_spalten_seite1:
            zeilen.extend(spalte)
            zeilen.append("")
        return zeilen

    @staticmethod
    def _lieferadresse(kopf: dict[str, str], inhalt: PdfInhalt) -> str | None:
        wert = kopf.get("lieferadresse")
        if wert:
            return wert
        # Schlüsselwort ohne Wert in derselben Zeile: Folgezeilen bis zur PLZ-Zeile einsammeln.
        muster = _KOPF_MUSTER["lieferadresse"]
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
