"""Deckblatt eines Dossiers als PDF (reportlab, platypus).

:func:`erzeuge_deckblatt` schreibt ``00_Deckblatt.pdf`` mit Titel, Kopfdaten (Firma, Kunde,
Lieferant, Nummern, Fixtermin, Spedition, Referenz), Positionstabelle (Artikelnr., Bezeichnung,
Menge, Einheit, Jahrgang), Belegfeldern, Checkliste mit Kästchen, Prüfhinweisen, offenen Punkten,
Dateiliste, vorgeschlagenem Zielordner und Erzeugungszeit. Zahlen und Datumswerte erscheinen in
deutscher Schreibweise.

Schrift: Standard ist Helvetica (WinAnsi, reicht für Umlaute und westeuropäische Namen). Liegt eine
DejaVu-Sans- bzw. Arial-TrueType-Schrift vor (Linux-Systemschrift, Windows-Fonts), wird sie
registriert, damit auch Zeichen außerhalb von Latin-1 (z. B. polnische oder tschechische Namen)
korrekt gedruckt werden. Für Helvetica werden nicht darstellbare Zeichen transliteriert, damit die
Erzeugung nie an einem Namen scheitert.

Die Kästchen der Checkliste sind kleine umrandete Tabellenzellen (schriftunabhängig); ein "X"
markiert erledigte Punkte. "gesendet" bedeutet dabei: Der Entwurf wurde in Outlook angelegt – der
Versand selbst erfolgt immer manuell (das Tool sendet nie).
"""

from __future__ import annotations

import logging
import unicodedata
from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from auftragsautomat.config import Settings
from auftragsautomat.models import Belegpaket, Dossier, Pruefhinweis, Schweregrad

__all__ = [
    "CHECKLISTE",
    "CHECKLISTE_REIHENFOLGE",
    "DATUMSFORMAT",
    "ZEITFORMAT",
    "datum_text",
    "erzeuge_deckblatt",
    "schriften",
    "zahl_text",
    "zeit_text",
]

logger = logging.getLogger(__name__)

DATUMSFORMAT = "%d.%m.%Y"
ZEITFORMAT = "%d.%m.%Y %H:%M"
LEER = "–"

#: Schlüssel der Checkliste (``Dossier.checkliste``) -> Beschriftung im Deckblatt, in Druckreihenfolge.
CHECKLISTE: dict[str, str] = {
    "bestellung_gesendet": "Bestellung ans Weingut gesendet (Entwurf in Outlook angelegt)",
    "auftragskopie_gesendet": "Auftragskopie an den Kunden gesendet (Entwurf in Outlook angelegt)",
    "spedition_beauftragt": "Spedition beauftragt",
    "lieferant_bestaetigt": "Lieferant hat bestätigt",
    "spedition_bestaetigt": "Spedition hat bestätigt",
    "abgeholt": "Abgeholt",
    "zugestellt": "Zugestellt",
}
CHECKLISTE_REIHENFOLGE: tuple[str, ...] = tuple(CHECKLISTE)

#: Kandidaten für eine Unicode-TrueType-Schrift: (normal, fett). Erste vorhandene gewinnt.
_TTF_KANDIDATEN: tuple[tuple[str, str, str], ...] = (
    ("DejaVuSans", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ("DejaVuSans", "/usr/share/fonts/dejavu/DejaVuSans.ttf", "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf"),
    ("DejaVuSans", "/usr/share/fonts/TTF/DejaVuSans.ttf", "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf"),
    ("DejaVuSans", "C:/Windows/Fonts/DejaVuSans.ttf", "C:/Windows/Fonts/DejaVuSans-Bold.ttf"),
    ("Arial", "C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/arialbd.ttf"),
)
_schrift_cache: tuple[str, str] | None = None

_SCHWEREGRAD_FARBE = {
    Schweregrad.FEHLER: colors.HexColor("#b00020"),
    Schweregrad.WARNUNG: colors.HexColor("#b35c00"),
    Schweregrad.INFO: colors.HexColor("#555555"),
}
_SCHWEREGRAD_TEXT = {Schweregrad.FEHLER: "FEHLER", Schweregrad.WARNUNG: "WARNUNG", Schweregrad.INFO: "Info"}


# ---------------------------------------------------------------------------
# Formatierung
# ---------------------------------------------------------------------------


def zahl_text(wert: Decimal | int | float | None, nachkomma: int | None = None) -> str:
    """Zahl in deutscher Schreibweise (Tausenderpunkt, Dezimalkomma). ``nachkomma`` = feste
    Nachkommastellen, sonst so viele wie nötig (``Decimal("12.50")`` -> ``"12,5"``). ``None`` -> "–"."""
    if wert is None:
        return LEER
    dezimal = wert if isinstance(wert, Decimal) else Decimal(str(wert))
    if nachkomma is None:
        dezimal = dezimal.normalize()
        stellen = max(0, -dezimal.as_tuple().exponent) if dezimal.is_finite() else 0
    else:
        stellen = max(0, nachkomma)
        dezimal = dezimal.quantize(Decimal(1).scaleb(-stellen))
    text = f"{dezimal:,.{stellen}f}"
    return text.replace(",", "\x00").replace(".", ",").replace("\x00", ".")


def datum_text(wert: date | datetime | None) -> str:
    """Datum als ``TT.MM.JJJJ``; ``None`` -> "–"."""
    if wert is None:
        return LEER
    return wert.strftime(DATUMSFORMAT)


def zeit_text(wert: datetime | None) -> str:
    """Zeitpunkt als ``TT.MM.JJJJ HH:MM``; ``None`` -> "–"."""
    if wert is None:
        return LEER
    return wert.strftime(ZEITFORMAT)


def _text(wert: Any) -> str:
    """Beliebiger Wert als Anzeigetext; leer/None -> "–"."""
    if wert is None:
        return LEER
    if isinstance(wert, bool):
        return "ja" if wert else "nein"
    if isinstance(wert, Decimal):
        return zahl_text(wert)
    if isinstance(wert, datetime):
        return zeit_text(wert)
    if isinstance(wert, date):
        return datum_text(wert)
    text = str(wert).strip()
    return text or LEER


# ---------------------------------------------------------------------------
# Schrift
# ---------------------------------------------------------------------------


def schriften() -> tuple[str, str]:
    """``(normal, fett)``: registrierte Unicode-TrueType-Schrift (DejaVu Sans / Arial), sonst Helvetica.
    Das Ergebnis wird je Prozess gemerkt."""
    global _schrift_cache
    if _schrift_cache is not None:
        return _schrift_cache
    for name, normal_pfad, fett_pfad in _TTF_KANDIDATEN:
        if Path(normal_pfad).is_file():
            try:
                pdfmetrics.registerFont(TTFont(name, normal_pfad))
                fett = f"{name}-Bold"
                if Path(fett_pfad).is_file():
                    pdfmetrics.registerFont(TTFont(fett, fett_pfad))
                else:
                    fett = name
                pdfmetrics.registerFontFamily(name, normal=name, bold=fett, italic=name, boldItalic=fett)
                _schrift_cache = (name, fett)
                logger.debug("Deckblatt-Schrift: %s (%s)", name, normal_pfad)
                return _schrift_cache
            except Exception as exc:  # pragma: no cover - defekte Schriftdatei
                logger.warning("TrueType-Schrift %s nicht nutzbar (%s); Helvetica wird verwendet.", normal_pfad, exc)
    _schrift_cache = ("Helvetica", "Helvetica-Bold")
    return _schrift_cache


def _darstellbar(text: str, schrift: str) -> str:
    """Für Helvetica (WinAnsi): Akzente außerhalb von cp1252 abstreifen, Rest ersetzen; TrueType: unverändert."""
    if schrift != "Helvetica":
        return text
    try:
        text.encode("cp1252")
        return text
    except UnicodeEncodeError:
        pass
    zerlegt = unicodedata.normalize("NFKD", text)
    ohne_akzente = "".join(z for z in zerlegt if not unicodedata.combining(z))
    return ohne_akzente.encode("cp1252", errors="replace").decode("cp1252")


# ---------------------------------------------------------------------------
# Bausteine
# ---------------------------------------------------------------------------


class _Bausteine:
    """Stile und Hilfsfunktionen für ein Deckblatt (eine Instanz je Aufruf)."""

    def __init__(self) -> None:
        self.normal, self.fett = schriften()
        self.breite = A4[0] - 40 * mm
        self.st_titel = ParagraphStyle("titel", fontName=self.fett, fontSize=16, leading=20, spaceAfter=2 * mm)
        self.st_untertitel = ParagraphStyle("untertitel", fontName=self.normal, fontSize=9.5, leading=12, textColor=colors.HexColor("#444444"))
        self.st_h2 = ParagraphStyle("h2", fontName=self.fett, fontSize=11.5, leading=14, spaceBefore=5 * mm, spaceAfter=2 * mm)
        self.st_text = ParagraphStyle("text", fontName=self.normal, fontSize=9, leading=11.5, alignment=TA_LEFT)
        self.st_klein = ParagraphStyle("klein", fontName=self.normal, fontSize=8, leading=10, textColor=colors.HexColor("#555555"))
        self.st_zelle = ParagraphStyle("zelle", fontName=self.normal, fontSize=8.5, leading=10.5)
        self.st_zelle_fett = ParagraphStyle("zelle_fett", fontName=self.fett, fontSize=8.5, leading=10.5)
        self.st_mono = ParagraphStyle("mono", fontName=self.normal, fontSize=8, leading=10)

    def p(self, text: Any, stil: ParagraphStyle | None = None) -> Paragraph:
        return Paragraph(escape(_darstellbar(_text(text), self.normal)), stil or self.st_zelle)

    def tabelle(self, zeilen: Sequence[Sequence[Any]], breiten: Sequence[float], *, kopf: bool = True) -> Table:
        daten = []
        for i, zeile in enumerate(zeilen):
            stil = self.st_zelle_fett if kopf and i == 0 else self.st_zelle
            daten.append([z if isinstance(z, Table | Paragraph) else self.p(z, stil) for z in zeile])
        tabelle = Table(daten, colWidths=list(breiten), repeatRows=1 if kopf else 0)
        stile = [
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#999999")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 3),
            ("RIGHTPADDING", (0, 0), (-1, -1), 3),
            ("TOPPADDING", (0, 0), (-1, -1), 2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ]
        if kopf:
            stile.append(("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8e8e8")))
        tabelle.setStyle(TableStyle(stile))
        return tabelle

    def kaestchen(self, erledigt: bool) -> Table:
        """Schriftunabhängiges Kontrollkästchen: umrandete Zelle, bei ``erledigt`` mit "X"."""
        inhalt = Paragraph("X", ParagraphStyle("x", fontName=self.fett, fontSize=8.5, leading=9, alignment=1)) if erledigt else ""
        box = Table([[inhalt]], colWidths=[4.2 * mm], rowHeights=[4.2 * mm])
        box.setStyle(
            TableStyle(
                [
                    ("BOX", (0, 0), (-1, -1), 0.8, colors.black),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                    ("TOPPADDING", (0, 0), (-1, -1), 0),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                ]
            )
        )
        return box

    def hinweis_paragraph(self, hinweis: Pruefhinweis) -> Paragraph:
        farbe = _SCHWEREGRAD_FARBE.get(hinweis.schweregrad, colors.black)
        stil = ParagraphStyle("hinweis", parent=self.st_zelle, textColor=farbe)
        return Paragraph(escape(_darstellbar(hinweis.text, self.normal)), stil)


def _firma_anzeige(dossier: Dossier, settings: Settings | None) -> str:
    if not dossier.firma_id:
        return LEER
    if settings is not None and dossier.firma_id in settings.firmen:
        firma = settings.firmen[dossier.firma_id]
        return f"{firma.name} ({firma.kurz})"
    return dossier.firma_id


def _kopfdaten(dossier: Dossier, paket: Belegpaket | None, settings: Settings | None) -> list[tuple[str, str]]:
    roh = paket.roh if paket is not None else None
    kunde_lang = ""
    lieferant_lang = ""
    if paket is not None:
        if paket.kunde.kunde is not None:
            kunde_lang = f"{paket.kunde.kunde.name} (Kd-Nr. {paket.kunde.kunde.kundennummer})"
        elif roh is not None and roh.kunde_text:
            kunde_lang = f"{roh.kunde_text} (laut Dokument)"
        if paket.lieferant.lieferant is not None:
            lieferant_lang = f"{paket.lieferant.lieferant.name} (Lf-Nr. {paket.lieferant.lieferant.lieferantennummer})"
        elif roh is not None and roh.lieferant_text:
            lieferant_lang = f"{roh.lieferant_text} (laut Dokument)"
    kunde = dossier.kunde_kurz or LEER
    if kunde_lang:
        kunde = f"{kunde} – {kunde_lang}"
    lieferant = dossier.lieferant_kurz or LEER
    if lieferant_lang:
        lieferant = f"{lieferant} – {lieferant_lang}"
    zeilen: list[tuple[str, str]] = [
        ("Firma", _firma_anzeige(dossier, settings)),
        ("Kunde", kunde),
        ("Lieferant / Weingut", lieferant),
        ("Kundenbestellnummer", _text(dossier.kundenbestellnummer)),
        ("Bestellnummer (Cde / Lexware)", _text(dossier.bestellnummer)),
        ("Auftragsnummer (AB / Lexware)", _text(dossier.ab_nummer)),
        ("Lieferschein-Nr.", _text(dossier.lieferschein_nummer)),
        ("Fixtermin", datum_text(dossier.fixtermin)),
        ("Spedition", _text(dossier.spedition)),
        ("Referenz der Spedition", _text(dossier.tracking_referenz)),
    ]
    if roh is not None:
        zeilen.extend(
            [
                ("Belegdatum laut Dokument", datum_text(roh.datum)),
                ("Liefertag laut Dokument", datum_text(roh.liefertag)),
                ("Lieferadresse", _text(roh.lieferadresse)),
                ("Quelldatei", roh.quelldatei.name),
            ]
        )
        if paket is not None and (paket.ist_revision or roh.ist_revision):
            zeilen.append(("Revised Order", f"ersetzt {roh.revision_von or paket.revision_von or 'eine frühere Bestellung'}"))
    return zeilen


def _positionen(b: _Bausteine, paket: Belegpaket) -> list:
    if not paket.positionen:
        return [b.p("Keine Positionen im Belegpaket.", b.st_text)]
    zeilen: list[list[Any]] = [["Pos.", "Artikelnr.", "Bezeichnung", "Menge", "Einheit", "Jahrgang"]]
    for pos in paket.positionen:
        artikel = pos.artikel
        bezeichnung = artikel.bezeichnung if artikel is not None else pos.roh.bezeichnung
        if pos.uebersprungen:
            bezeichnung = f"{bezeichnung} (übersprungen)"
        elif artikel is None:
            bezeichnung = f"{bezeichnung} (kein Stammartikel)"
        if pos.roh.durchgestrichen:
            bezeichnung = f"{bezeichnung} (durchgestrichen)"
        einheit = pos.roh.einheit or (artikel.einheit if artikel is not None else None) or "Fl."
        jahrgang = (artikel.jahrgang if artikel is not None else None) or pos.roh.jahrgang
        menge = zahl_text(pos.menge) if not pos.uebersprungen else zahl_text(pos.roh.menge)
        zeilen.append(
            [
                str(pos.roh.laufnr),
                artikel.artikelnummer if artikel is not None else LEER,
                bezeichnung,
                menge,
                einheit,
                str(jahrgang) if jahrgang else LEER,
            ]
        )
    zeilen.append(["", "", "Gesamtmenge (aktive Positionen)", zahl_text(paket.gesamtmenge), "Fl.", ""])
    breiten = [10 * mm, 24 * mm, 82 * mm, 20 * mm, 16 * mm, 18 * mm]
    tabelle = b.tabelle(zeilen, breiten)
    tabelle.setStyle(TableStyle([("ALIGN", (3, 1), (3, -1), "RIGHT"), ("FONTNAME", (2, -1), (3, -1), b.fett)]))
    return [tabelle]


def _checkliste(b: _Bausteine, dossier: Dossier) -> Table:
    schluessel = list(CHECKLISTE_REIHENFOLGE) + [k for k in dossier.checkliste if k not in CHECKLISTE]
    zeilen = []
    for key in schluessel:
        erledigt = bool(dossier.checkliste.get(key, False))
        zeilen.append([b.kaestchen(erledigt), b.p(CHECKLISTE.get(key, key.replace("_", " ")))])
    tabelle = Table(zeilen, colWidths=[8 * mm, b.breite - 8 * mm])
    tabelle.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 2),
                ("TOPPADDING", (0, 0), (-1, -1), 1.5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5),
            ]
        )
    )
    return tabelle


def _hinweise(b: _Bausteine, paket: Belegpaket | None) -> list:
    if paket is None:
        return [b.p("Kein Belegpaket übergeben – Prüfhinweise nicht verfügbar.", b.st_text)]
    hinweise = paket.alle_hinweise
    if not hinweise:
        return [b.p("Keine Prüfhinweise.", b.st_text)]
    rang = {Schweregrad.FEHLER: 0, Schweregrad.WARNUNG: 1, Schweregrad.INFO: 2}
    sortiert = sorted(hinweise, key=lambda h: (rang.get(h.schweregrad, 3), h.position_laufnr or 0))
    zeilen: list[list[Any]] = [["Grad", "Code", "Pos.", "Hinweis"]]
    for h in sortiert:
        grad = Paragraph(
            _SCHWEREGRAD_TEXT.get(h.schweregrad, str(h.schweregrad)),
            ParagraphStyle("grad", parent=b.st_zelle_fett, textColor=_SCHWEREGRAD_FARBE.get(h.schweregrad, colors.black)),
        )
        zeilen.append([grad, str(h.code), str(h.position_laufnr) if h.position_laufnr else LEER, b.hinweis_paragraph(h)])
    return [b.tabelle(zeilen, [20 * mm, 40 * mm, 12 * mm, b.breite - 72 * mm])]


def _liste(b: _Bausteine, eintraege: Sequence[str], leer_text: str) -> list:
    if not eintraege:
        return [b.p(leer_text, b.st_text)]
    return [b.p(f"• {eintrag}", b.st_text) for eintrag in eintraege]


def _dateien(b: _Bausteine, dossier: Dossier) -> list:
    if not dossier.dateien:
        return [b.p("Noch keine Dateien abgelegt.", b.st_text)]
    zeilen: list[list[Any]] = [["Datei", "Typ", "Hinzugefügt", "Größe"]]
    for d in sorted(dossier.dateien, key=lambda x: x.name.casefold()):
        groesse = f"{zahl_text(Decimal(d.groesse) / Decimal(1024), 1)} KB" if d.groesse is not None else LEER
        zeilen.append([d.name, d.typ, zeit_text(d.hinzugefuegt), groesse])
    return [b.tabelle(zeilen, [b.breite - 92 * mm, 36 * mm, 32 * mm, 24 * mm])]


def _fusszeile(name: str, schrift: str):
    text = _darstellbar(f"Deckblatt Dossier {name}", schrift)

    def zeichne(canvas, doc) -> None:  # noqa: ANN001 - reportlab-Callback
        canvas.saveState()
        canvas.setFont(schrift, 7.5)
        canvas.setFillColor(colors.HexColor("#666666"))
        canvas.drawString(20 * mm, 10 * mm, text)
        canvas.drawRightString(A4[0] - 20 * mm, 10 * mm, f"Seite {doc.page}")
        canvas.restoreState()

    return zeichne


# ---------------------------------------------------------------------------
# Öffentliche API
# ---------------------------------------------------------------------------


def erzeuge_deckblatt(
    dossier: Dossier,
    paket: Belegpaket | None,
    ziel: Path,
    *,
    settings: Settings | None = None,
    jetzt: datetime | None = None,
) -> Path:
    """Schreibt das Deckblatt des Dossiers als PDF nach ``ziel`` und liefert den Pfad.

    ``paket`` liefert Positionen, Belegfelder und Prüfhinweise; ohne Paket (z. B. Aktualisierung
    im Überwachungslauf) enthält das Deckblatt die Angaben aus ``dossier``. ``settings`` löst den
    Firmennamen auf. ``jetzt`` = Erzeugungszeit (Standard: aktuelle Zeit).
    """
    ziel = Path(ziel)
    ziel.parent.mkdir(parents=True, exist_ok=True)
    b = _Bausteine()
    erzeugt = jetzt or datetime.now().astimezone()
    story: list = []

    story.append(b.p(f"Dossier {dossier.name}", b.st_titel))
    story.append(
        b.p(
            "Vorangefertigtes Dossier des Auftrags-Automaten – bitte prüfen und in die Auftragsablage einsortieren.",
            b.st_untertitel,
        )
    )
    story.append(Spacer(1, 3 * mm))

    story.append(b.p("Kopfdaten", b.st_h2))
    kopf = [[b.p(bez, b.st_zelle_fett), b.p(wert)] for bez, wert in _kopfdaten(dossier, paket, settings)]
    kopf_tabelle = Table(kopf, colWidths=[55 * mm, b.breite - 55 * mm])
    kopf_tabelle.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#999999")),
                ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f2f2f2")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ]
        )
    )
    story.append(kopf_tabelle)

    if paket is not None:
        story.append(b.p("Positionen", b.st_h2))
        story.extend(_positionen(b, paket))

        story.append(b.p("Belegfelder (Lexware)", b.st_h2))
        felder = paket.belegfelder
        beleg_zeilen = [
            ["Spedition (Transport by)", _text(felder.spedition)],
            ["Weingut (Supplier)", _text(felder.weingut)],
            ["Shipment", _text(felder.shipment)],
            ["Kunde (Customer)", _text(felder.kunde)],
        ]
        story.append(b.tabelle(beleg_zeilen, [55 * mm, b.breite - 55 * mm], kopf=False))

    story.append(b.p("Checkliste", b.st_h2))
    story.append(KeepTogether([_checkliste(b, dossier)]))
    story.append(
        b.p(
            "„gesendet“ = der E-Mail-Entwurf wurde angelegt; der Versand erfolgt manuell aus Outlook. "
            "Häkchen zu Bestätigungen stammen aus der Fixterminüberwachung bzw. dem Tracking.",
            b.st_klein,
        )
    )

    story.append(b.p("Prüfhinweise", b.st_h2))
    story.extend(_hinweise(b, paket))

    story.append(b.p("Offene Punkte", b.st_h2))
    story.extend(_liste(b, dossier.offene_punkte, "Keine offenen Punkte."))

    story.append(b.p("Dateien im Dossier", b.st_h2))
    story.extend(_dateien(b, dossier))

    story.append(b.p("Einsortieren", b.st_h2))
    story.append(b.p(f"Vorgeschlagener Zielordner: {_text(dossier.vorgeschlagener_zielordner)}", b.st_text))
    story.append(
        b.p(
            "Das Tool verschiebt nichts aus „vorangefertigte Dossiers“ heraus; das Einsortieren übernimmt der Mitarbeiter.",
            b.st_klein,
        )
    )
    story.append(Spacer(1, 4 * mm))
    story.append(b.p(f"Erzeugt am {zeit_text(erzeugt)} durch den Auftrags-Automaten.", b.st_klein))
    if dossier.erstellt_am is not None:
        story.append(b.p(f"Dossier angelegt am {zeit_text(dossier.erstellt_am)}.", b.st_klein))

    dokument = SimpleDocTemplate(
        str(ziel),
        pagesize=A4,
        leftMargin=20 * mm,
        rightMargin=20 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
        title=_darstellbar(f"Dossier {dossier.name}", b.normal),
        author="Auftrags-Automat",
        subject="Deckblatt",
    )
    fuss = _fusszeile(dossier.name, b.normal)
    dokument.build(story, onFirstPage=fuss, onLaterPages=fuss)
    logger.info("Deckblatt geschrieben: %s", ziel)
    return ziel
