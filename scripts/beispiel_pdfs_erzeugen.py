"""Erzeugt die Beispiel-PDFs für Tests und Demos (tests/fixtures/pdfs).

Vier Dokumente in unterschiedlichen Layouts, wie sie im Alltag eingehen:

1. ``kundenbestellung_kirchhellen.pdf`` – klassische Kundenbestellung mit
   Tabellenlinien (Pos/Menge/Einheit/Artikel/Jahrgang/Einzelpreis/Gesamt),
   Hinweis "11+1", einer 0,00-€-Position ("DAA Neptun") und Summenvermerk.
2. ``kundenbestellung_menrath.pdf`` – Bestellung als Fließtext ohne Tabellen,
   abweichende Lieferadresse, Sondervariante (Magnum 1,5 l) und einem Preis,
   der vom Stamm abweicht (Gavi 12,00 statt 12,50).
3. ``eigene_bestellung_cide.pdf`` – unsere eigene Bestellung an das Weingut
   (Eingangstyp B als PDF) mit Artikelnummern und EK-Preisen.
4. ``scan_preisliste.pdf`` – Seite nur als Bild (ohne Textebene), simuliert eine
   eingescannte Preisliste mit handschriftlichen Mengen.

Das Skript ist idempotent und deterministisch (``invariant=1``): mehrfacher
Aufruf erzeugt byte-identische Dateien. Aufruf::

    python scripts/beispiel_pdfs_erzeugen.py [ordner]
    auftragsautomat beispiel-pdfs [ordner]      (CLI ruft ``erzeuge_alle``)
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

logger = logging.getLogger(__name__)

STANDARD_ORDNER = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "pdfs"

DATEI_KIRCHHELLEN = "kundenbestellung_kirchhellen.pdf"
DATEI_MENRATH = "kundenbestellung_menrath.pdf"
DATEI_CIDE = "eigene_bestellung_cide.pdf"
DATEI_SCAN = "scan_preisliste.pdf"

_UNSERE_FIRMA = ("Wein-Import-Kreis LEH", "Musterweg 10", "40210 Düsseldorf")


def _stile() -> dict[str, ParagraphStyle]:
    basis = getSampleStyleSheet()
    normal = ParagraphStyle("normal", parent=basis["Normal"], fontName="Helvetica", fontSize=10, leading=13)
    return {
        "normal": normal,
        "klein": ParagraphStyle("klein", parent=normal, fontSize=8, leading=10, textColor=colors.grey),
        "fett": ParagraphStyle("fett", parent=normal, fontName="Helvetica-Bold"),
        "titel": ParagraphStyle("titel", parent=normal, fontName="Helvetica-Bold", fontSize=16, leading=20),
        "rechts": ParagraphStyle("rechts", parent=normal, alignment=TA_RIGHT),
        "rechts_fett": ParagraphStyle(
            "rechts_fett", parent=normal, alignment=TA_RIGHT, fontName="Helvetica-Bold"
        ),
        "zelle": ParagraphStyle("zelle", parent=normal, fontSize=9, leading=11),
    }


def _dokument(ziel: Path, titel: str) -> SimpleDocTemplate:
    return SimpleDocTemplate(
        str(ziel),
        pagesize=A4,
        leftMargin=20 * mm,
        rightMargin=20 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
        title=titel,
        author="Beispiel",
        invariant=1,  # deterministische Ausgabe (keine Zeitstempel/IDs)
    )


def _absaetze(zeilen: list[str], stil: ParagraphStyle) -> list[Paragraph]:
    return [Paragraph(z, stil) for z in zeilen]


def _gittertabelle(daten: list[list], breiten: list[float], ausrichtung_rechts: tuple[int, ...]) -> Table:
    tabelle = Table(daten, colWidths=breiten, repeatRows=1)
    stil = [
        ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e6e6e6")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]
    for spalte in ausrichtung_rechts:
        stil.append(("ALIGN", (spalte, 1), (spalte, -1), "RIGHT"))
    tabelle.setStyle(TableStyle(stil))
    return tabelle


# ---------------------------------------------------------------------------
# (1) Kundenbestellung Weinmarkt Kirchhellen – Tabelle mit Linien
# ---------------------------------------------------------------------------


def erzeuge_kundenbestellung_kirchhellen(ziel: Path) -> Path:
    st = _stile()
    doc = _dokument(ziel, "Bestellung B-4712")
    inhalt: list = []

    absender = _absaetze(
        ["<b>Weinmarkt Kirchhellen GmbH</b>", "Hauptstraße 1", "46244 Bottrop", "Tel. 02045 123456"],
        st["normal"],
    )
    infos = _absaetze(
        ["Bestell-Nr.: B-4712", "Datum: 15.10.2026", "Liefertermin: 24.10.2026", "Kundennr.: 3441"],
        st["rechts"],
    )
    kopf = Table([[absender, infos]], colWidths=[95 * mm, 75 * mm])
    kopf.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    inhalt.append(kopf)
    inhalt.append(Spacer(1, 10 * mm))

    inhalt.extend(_absaetze(["An", *_UNSERE_FIRMA], st["normal"]))
    inhalt.append(Spacer(1, 10 * mm))
    inhalt.append(Paragraph("Bestellung", st["titel"]))
    inhalt.append(Spacer(1, 3 * mm))
    inhalt.append(
        Paragraph("Sehr geehrte Damen und Herren, hiermit bestellen wir folgende Artikel:", st["normal"])
    )
    inhalt.append(Spacer(1, 4 * mm))

    kopfzeile = ["Pos", "Menge", "Einheit", "Artikel", "Jahrgang", "Einzelpreis", "Gesamt"]
    zeilen = [
        ("1", "60", "Fl", "Villa Armellina Gloria Secco Frizzante", "", "6,90 €", "414,00 €"),
        ("2", "24", "Fl", "Villa Armellina Rosato Secco Frizzante", "", "6,90 €", "165,60 €"),
        ("3", "12", "Fl", "Colosi Nero d'Avola Sicilia DOC<br/>11+1", "2022", "8,40 €", "100,80 €"),
        ("4", "1", "Stk", "DAA Neptun", "", "0,00 €", "0,00 €"),
    ]
    daten = [kopfzeile]
    for zeile in zeilen:
        daten.append([Paragraph(z, st["zelle"]) for z in zeile])
    inhalt.append(
        _gittertabelle(daten, [12 * mm, 14 * mm, 16 * mm, 68 * mm, 18 * mm, 21 * mm, 21 * mm], (1, 5, 6))
    )
    inhalt.append(Spacer(1, 3 * mm))
    inhalt.append(Paragraph("Summe netto: 680,40 €", st["rechts_fett"]))
    inhalt.append(Paragraph("zzgl. 19 % MwSt.", st["rechts"]))
    inhalt.append(Spacer(1, 8 * mm))
    inhalt.append(Paragraph("Gesamt: 97 Fl.", st["fett"]))
    inhalt.append(Spacer(1, 8 * mm))
    inhalt.extend(
        _absaetze(
            [
                "Bitte liefern Sie frei Haus an die oben genannte Adresse.",
                "Mit freundlichen Grüßen",
                "Weinmarkt Kirchhellen GmbH",
            ],
            st["normal"],
        )
    )
    doc.build(inhalt)
    logger.info("Erzeugt: %s", ziel)
    return ziel


# ---------------------------------------------------------------------------
# (2) Kundenbestellung Weinhaus Menrath – Fließtext ohne Tabellenlinien
# ---------------------------------------------------------------------------


def erzeuge_kundenbestellung_menrath(ziel: Path) -> Path:
    st = _stile()
    doc = _dokument(ziel, "Bestellung M-2026-88")
    inhalt: list = []

    inhalt.append(Paragraph("Weinhaus Menrath · Marktplatz 3 · 41061 Mönchengladbach", st["klein"]))
    inhalt.append(Spacer(1, 8 * mm))
    inhalt.extend(_absaetze(list(_UNSERE_FIRMA), st["normal"]))
    inhalt.append(Spacer(1, 12 * mm))
    inhalt.append(Paragraph("Mönchengladbach, 16.10.2026", st["rechts"]))
    inhalt.append(Spacer(1, 6 * mm))
    inhalt.append(Paragraph("Ihre Bestellung Nr. M-2026-88", st["titel"]))
    inhalt.append(Spacer(1, 4 * mm))
    inhalt.extend(
        _absaetze(
            [
                "Lieferung an: Lager Menrath, Industriestr. 5, 41066 Mönchengladbach",
                "Liefertermin: KW 44",
            ],
            st["normal"],
        )
    )
    inhalt.append(Spacer(1, 6 * mm))
    inhalt.append(Paragraph("Sehr geehrte Frau Adolph,", st["normal"]))
    inhalt.append(Paragraph("wir bestellen hiermit zu den vereinbarten Konditionen:", st["normal"]))
    inhalt.append(Spacer(1, 4 * mm))
    inhalt.extend(
        _absaetze(
            [
                "36 x Bollina Gavi di Gavi DOCG 2023 0,75 l à 12,00 €",
                "6 x Colosi Grillo Sicilia DOC 2023 0,75 l à 8,60 €",
                "6 x Villa Armellina Rosato Secco Frizzante Magnum 1,5 l à 14,90 €",
            ],
            st["normal"],
        )
    )
    inhalt.append(Spacer(1, 4 * mm))
    inhalt.append(Paragraph("Gesamt 48 Flaschen, netto 573,00 €", st["fett"]))
    inhalt.append(Spacer(1, 8 * mm))
    inhalt.extend(
        _absaetze(
            [
                "Bitte bestätigen Sie uns den Auftrag kurz per E-Mail.",
                "Mit freundlichen Grüßen",
                "Thomas Menrath",
            ],
            st["normal"],
        )
    )
    doc.build(inhalt)
    logger.info("Erzeugt: %s", ziel)
    return ziel


# ---------------------------------------------------------------------------
# (3) Eigene Bestellung an Cide srl – Eingangstyp B als PDF
# ---------------------------------------------------------------------------


def erzeuge_eigene_bestellung_cide(ziel: Path) -> Path:
    st = _stile()
    doc = _dokument(ziel, "Bestellung BE-2026-0077")
    inhalt: list = []

    inhalt.append(Paragraph(" · ".join(_UNSERE_FIRMA), st["klein"]))
    inhalt.append(Spacer(1, 8 * mm))
    inhalt.extend(_absaetze(["Cide srl", "Via Roma 12", "31100 Treviso", "Italien"], st["normal"]))
    inhalt.append(Spacer(1, 10 * mm))
    inhalt.append(Paragraph("Bestellung Nr. BE-2026-0077 an Cide srl, Treviso", st["titel"]))
    inhalt.append(Spacer(1, 4 * mm))
    inhalt.extend(
        _absaetze(["Datum: 17.10.2026", "Kunde: Menrath", "Liefertag: 28.10.2026"], st["normal"])
    )
    inhalt.append(Spacer(1, 6 * mm))
    inhalt.append(Paragraph("Wir bestellen hiermit zu den vereinbarten Einkaufspreisen:", st["normal"]))
    inhalt.append(Spacer(1, 4 * mm))

    kopfzeile = ["Pos", "Art.-Nr.", "Menge", "Einheit", "Artikel", "EK-Preis", "Gesamt"]
    zeilen = [
        ("1", "1643", "60", "Fl", "Villa Armellina Gloria Secco Frizzante 0,75 l", "4,20 €", "252,00 €"),
        ("2", "957", "24", "Fl", "Villa Armellina Rosato Secco Frizzante 0,75 l", "4,20 €", "100,80 €"),
    ]
    daten = [kopfzeile]
    for zeile in zeilen:
        daten.append([Paragraph(z, st["zelle"]) for z in zeile])
    inhalt.append(
        _gittertabelle(daten, [12 * mm, 18 * mm, 14 * mm, 14 * mm, 70 * mm, 21 * mm, 21 * mm], (2, 5, 6))
    )
    inhalt.append(Spacer(1, 3 * mm))
    inhalt.append(Paragraph("Summe netto: 352,80 €", st["rechts_fett"]))
    inhalt.append(Spacer(1, 8 * mm))
    inhalt.extend(
        _absaetze(
            [
                "Bitte liefern an: Spedition Weinlogistik, Hafenstraße 2, 47119 Duisburg",
                "Mit freundlichen Grüßen",
                "Wein-Import-Kreis LEH",
            ],
            st["normal"],
        )
    )
    doc.build(inhalt)
    logger.info("Erzeugt: %s", ziel)
    return ziel


# ---------------------------------------------------------------------------
# (4) Scan einer Preisliste – nur Bild, keine Textebene
# ---------------------------------------------------------------------------

_SCAN_ZEILEN: list[tuple[str, str, str, str]] = [
    # (Artikel, Preis, handschriftliche Menge, durchgestrichen?)
    ("Nero d'Avola Sicilia DOC 2022  0,75 l", "8,40", "24", ""),
    ("Grillo Sicilia DOC 2023  0,75 l", "8,60", "12", ""),
    ("Salina Bianco IGT 2023  0,75 l", "9,80", "6", "x"),
    ("Nero d'Avola 2022  6er Holzkiste", "58,00", "", ""),
    ("Cannonau di Sardegna 2021  0,75 l", "7,90", "60", ""),
    ("Grillo Sicilia DOC 2023  0,375 l", "4,60", "", ""),
    ("Nerello Mascalese 2022  0,75 l", "11,20", "18", ""),
    ("Malvasia delle Lipari Passito 2020  0,5 l", "19,50", "6", ""),
]


def _schriftart(name: str, groesse: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Nutzt die mit reportlab ausgelieferten Vera-Schriften, sonst die PIL-Standardschrift."""
    try:
        import reportlab

        pfad = Path(reportlab.__file__).resolve().parent / "fonts" / name
        if pfad.exists():
            return ImageFont.truetype(str(pfad), groesse)
    except Exception as exc:  # pragma: no cover - nur bei kaputter Installation
        logger.debug("Vera-Schrift nicht verfügbar (%s), nutze PIL-Standardschrift", exc)
    try:
        return ImageFont.load_default(size=groesse)
    except TypeError:  # pragma: no cover - ältere Pillow-Version
        return ImageFont.load_default()


def _rendere_preisliste_bild() -> Image.Image:
    breite, hoehe = 1240, 1754  # A4 bei 150 dpi
    bild = Image.new("RGB", (breite, hoehe), (247, 245, 240))
    zeichnen = ImageDraw.Draw(bild)
    titel = _schriftart("VeraBd.ttf", 44)
    normal = _schriftart("Vera.ttf", 26)
    klein = _schriftart("Vera.ttf", 20)
    hand = _schriftart("VeraBI.ttf", 40)

    zeichnen.text((100, 90), "Cantine Colosi – Preisliste Herbst 2026", font=titel, fill=(30, 30, 30))
    zeichnen.text((100, 150), "Preise netto in EUR je Flasche, ab Lager Düsseldorf", font=klein, fill=(80, 80, 80))
    zeichnen.text((100, 230), "Artikel", font=normal, fill=(30, 30, 30))
    zeichnen.text((820, 230), "Preis", font=normal, fill=(30, 30, 30))
    zeichnen.text((980, 230), "Menge", font=normal, fill=(30, 30, 30))
    zeichnen.line((100, 270, 1140, 270), fill=(30, 30, 30), width=3)

    y = 310
    tinte = (28, 52, 140)
    for artikel, preis, menge, gestrichen in _SCAN_ZEILEN:
        zeichnen.text((100, y), artikel, font=normal, fill=(30, 30, 30))
        zeichnen.text((820, y), preis, font=normal, fill=(30, 30, 30))
        zeichnen.line((100, y + 48, 1140, y + 48), fill=(200, 200, 200), width=1)
        if menge:
            zeichnen.text((1000, y - 8), menge, font=hand, fill=tinte)
            if gestrichen:
                zeichnen.line((985, y + 22, 1075, y + 10), fill=tinte, width=4)
        y += 80

    zeichnen.text((1000, y + 20), "180 R", font=hand, fill=tinte)
    zeichnen.line((990, y + 70, 1130, y + 70), fill=tinte, width=3)
    zeichnen.text((100, y + 120), "für Hegmann, Lieferung KW 44", font=hand, fill=tinte)
    zeichnen.text((100, hoehe - 120), "Gescanntes Dokument – nur zu Testzwecken", font=klein, fill=(120, 120, 120))

    # leichte Schieflage wie beim Scannen
    bild = bild.rotate(0.6, resample=Image.BICUBIC, fillcolor=(247, 245, 240))
    return bild.convert("L")


def erzeuge_scan_preisliste(ziel: Path) -> Path:
    bild = _rendere_preisliste_bild()
    c = canvas.Canvas(str(ziel), pagesize=A4, invariant=1)
    c.setTitle("Scan Preisliste")
    c.setAuthor("Beispiel")
    breite, hoehe = A4
    c.drawImage(ImageReader(bild), 0, 0, width=breite, height=hoehe)
    c.showPage()
    c.save()
    logger.info("Erzeugt: %s", ziel)
    return ziel


# ---------------------------------------------------------------------------
# Alle
# ---------------------------------------------------------------------------


def erzeuge_alle(ordner: Path | None = None) -> list[Path]:
    """Erzeugt alle Beispiel-PDFs im Ordner (Standard: tests/fixtures/pdfs) und liefert die Pfade."""
    ordner = Path(ordner) if ordner is not None else STANDARD_ORDNER
    ordner.mkdir(parents=True, exist_ok=True)
    return [
        erzeuge_kundenbestellung_kirchhellen(ordner / DATEI_KIRCHHELLEN),
        erzeuge_kundenbestellung_menrath(ordner / DATEI_MENRATH),
        erzeuge_eigene_bestellung_cide(ordner / DATEI_CIDE),
        erzeuge_scan_preisliste(ordner / DATEI_SCAN),
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Erzeugt die Beispiel-PDFs für Tests und Demos.")
    parser.add_argument("ordner", nargs="?", type=Path, default=STANDARD_ORDNER, help="Zielordner")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    for pfad in erzeuge_alle(args.ordner):
        print(pfad)
    return 0


if __name__ == "__main__":
    sys.exit(main())
