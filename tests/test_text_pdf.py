"""Tests für die Text-PDF-Extraktion und das Beispiel-PDF-Skript.

Die PDFs unter tests/fixtures/pdfs werden von scripts/beispiel_pdfs_erzeugen.py
erzeugt und liegen im Repo; die Tests prüfen exakte Feldwerte.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import ModuleType

import pytest

from auftragsautomat.extraktion import text_pdf
from auftragsautomat.extraktion.text_pdf import (
    PdfInhalt,
    TextPdfExtraktor,
    TextPdfFehler,
    inhalt_aus_text,
    ist_text_pdf,
    lese_kopffelder,
    lese_pdf,
)
from auftragsautomat.models import (
    Eingangstyp,
    Extraktionsmethode,
    Pruefcode,
    RohBestellung,
    Schweregrad,
)

SKRIPT = Path(__file__).resolve().parents[1] / "scripts" / "beispiel_pdfs_erzeugen.py"


def _lade_skript() -> ModuleType:
    spec = importlib.util.spec_from_file_location("beispiel_pdfs_erzeugen", SKRIPT)
    assert spec is not None and spec.loader is not None
    modul = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = modul
    spec.loader.exec_module(modul)
    return modul


@pytest.fixture()
def extraktor(settings) -> TextPdfExtraktor:
    return TextPdfExtraktor(settings)


@pytest.fixture()
def pdfs(fixtures_ordner: Path) -> Path:
    ordner = fixtures_ordner / "pdfs"
    assert ordner.is_dir(), "Beispiel-PDFs fehlen: python scripts/beispiel_pdfs_erzeugen.py"
    return ordner


def _hinweise(roh: RohBestellung, code: Pruefcode) -> list[str]:
    return [h.text for h in roh.hinweise if h.code == code]


# ---------------------------------------------------------------------------
# ist_text_pdf / kann_verarbeiten
# ---------------------------------------------------------------------------


def test_ist_text_pdf(pdfs: Path, tmp_path: Path):
    assert ist_text_pdf(pdfs / "kundenbestellung_kirchhellen.pdf") is True
    assert ist_text_pdf(pdfs / "kundenbestellung_menrath.pdf") is True
    assert ist_text_pdf(pdfs / "eigene_bestellung_cide.pdf") is True
    assert ist_text_pdf(pdfs / "scan_preisliste.pdf") is False
    assert ist_text_pdf(tmp_path / "gibt_es_nicht.pdf") is False
    kaputt = tmp_path / "kaputt.pdf"
    kaputt.write_bytes(b"kein pdf")
    assert ist_text_pdf(kaputt) is False


def test_kann_verarbeiten(extraktor: TextPdfExtraktor, pdfs: Path, tmp_path: Path):
    assert extraktor.kann_verarbeiten(pdfs / "kundenbestellung_kirchhellen.pdf") is True
    assert extraktor.kann_verarbeiten(pdfs / "scan_preisliste.pdf") is False
    txt = tmp_path / "bestellung.txt"
    txt.write_text("36 x Gavi", encoding="utf-8")
    assert extraktor.kann_verarbeiten(txt) is False


def test_lese_pdf_kaputte_datei(tmp_path: Path):
    kaputt = tmp_path / "kaputt.pdf"
    kaputt.write_bytes(b"%PDF-1.4 kaputt")
    with pytest.raises(TextPdfFehler):
        lese_pdf(kaputt)


# ---------------------------------------------------------------------------
# (1) Kundenbestellung Kirchhellen – Tabelle mit Linien
# ---------------------------------------------------------------------------


def test_kirchhellen_kopf(extraktor: TextPdfExtraktor, pdfs: Path):
    roh = extraktor.extrahiere(pdfs / "kundenbestellung_kirchhellen.pdf")
    assert roh.eingangstyp == Eingangstyp.KUNDENBESTELLUNG_PDF
    assert roh.extraktionsmethode == Extraktionsmethode.PDFPLUMBER
    assert roh.quelldatei == pdfs / "kundenbestellung_kirchhellen.pdf"
    assert roh.kunde_text == "Weinmarkt Kirchhellen GmbH"
    assert roh.kunde_adresse == "Hauptstraße 1, 46244 Bottrop"
    assert roh.lieferant_text is None
    assert roh.bestellnummer == "B-4712"
    assert roh.belegnummer is None
    assert roh.datum == date(2026, 10, 15)
    assert roh.liefertag == date(2026, 10, 24)
    assert roh.lieferadresse is None
    assert roh.seiten == 1
    assert roh.rohtext and "Bestell-Nr.: B-4712" in roh.rohtext
    # Unsere eigene Firma (Empfänger) darf nicht als Kunde gelesen werden
    assert "Wein-Import-Kreis" not in roh.kunde_text


def test_kirchhellen_positionen(extraktor: TextPdfExtraktor, pdfs: Path):
    roh = extraktor.extrahiere(pdfs / "kundenbestellung_kirchhellen.pdf")
    assert [p.laufnr for p in roh.positionen] == [1, 2, 3, 4]
    p1, p2, p3, p4 = roh.positionen

    assert p1.bezeichnung == "Villa Armellina Gloria Secco Frizzante"
    assert p1.menge == Decimal("60")
    assert p1.einheit == "Fl"
    assert p1.preis == Decimal("6.90")
    assert p1.jahrgang is None
    assert p1.volumen_cl is None
    assert p1.hinweis is None
    assert p1.durchgestrichen is False

    assert p2.bezeichnung == "Villa Armellina Rosato Secco Frizzante"
    assert p2.menge == Decimal("24")
    assert p2.preis == Decimal("6.90")

    assert p3.bezeichnung == "Colosi Nero d'Avola Sicilia DOC"
    assert p3.menge == Decimal("12")
    assert p3.jahrgang == 2022
    assert p3.preis == Decimal("8.40")
    assert p3.hinweis == "11+1"

    assert p4.bezeichnung == "DAA Neptun"
    assert p4.menge == Decimal("1")
    assert p4.einheit == "Stk"
    assert p4.preis == Decimal("0.00")

    assert all(isinstance(p.menge, Decimal) for p in roh.positionen)
    assert all(isinstance(p.preis, Decimal) for p in roh.positionen)
    assert roh.gesamtmenge == Decimal("97")


def test_kirchhellen_summenvermerk_und_hinweise(extraktor: TextPdfExtraktor, pdfs: Path):
    roh = extraktor.extrahiere(pdfs / "kundenbestellung_kirchhellen.pdf")
    assert len(roh.summenvermerke) == 1
    vermerk = roh.summenvermerke[0]
    assert vermerk.wert == Decimal("97")
    assert vermerk.einheit == "Fl"
    assert vermerk.rohtext == "Gesamt: 97 Fl."
    assert _hinweise(roh, Pruefcode.SUMMENVERMERK) == ["Summenvermerk erkannt: Gesamt: 97 Fl."]
    # Alle Kopffelder vorhanden: keine FELD_FEHLT-Warnung
    assert _hinweise(roh, Pruefcode.FELD_FEHLT) == []
    assert all(h.schweregrad != Schweregrad.FEHLER for h in roh.hinweise)


# ---------------------------------------------------------------------------
# (2) Kundenbestellung Menrath – Fließtext ohne Tabellen
# ---------------------------------------------------------------------------


def test_menrath_kopf(extraktor: TextPdfExtraktor, pdfs: Path):
    roh = extraktor.extrahiere(pdfs / "kundenbestellung_menrath.pdf")
    assert roh.eingangstyp == Eingangstyp.KUNDENBESTELLUNG_PDF
    assert roh.kunde_text == "Weinhaus Menrath"
    assert roh.kunde_adresse == "Marktplatz 3, 41061 Mönchengladbach"
    assert roh.bestellnummer == "M-2026-88"
    assert roh.belegnummer is None
    assert roh.datum == date(2026, 10, 16)  # "Mönchengladbach, 16.10.2026"
    assert roh.liefertag is None  # "Liefertermin: KW 44" ist kein Datum
    assert roh.lieferadresse == "Lager Menrath, Industriestr. 5, 41066 Mönchengladbach"
    assert roh.lieferant_text is None


def test_menrath_positionen(extraktor: TextPdfExtraktor, pdfs: Path):
    roh = extraktor.extrahiere(pdfs / "kundenbestellung_menrath.pdf")
    assert len(roh.positionen) == 3
    p1, p2, p3 = roh.positionen

    assert p1.laufnr == 1
    assert p1.bezeichnung == "Bollina Gavi di Gavi DOCG 2023 0,75 l"
    assert p1.menge == Decimal("36")
    assert p1.einheit is None  # "36 x" nennt keine Einheit
    assert p1.jahrgang == 2023
    assert p1.volumen_cl == Decimal("75")
    assert p1.preis == Decimal("12.00")  # weicht bewusst vom Stamm (12,50) ab

    assert p2.bezeichnung == "Colosi Grillo Sicilia DOC 2023 0,75 l"
    assert p2.menge == Decimal("6")
    assert p2.jahrgang == 2023
    assert p2.preis == Decimal("8.60")

    assert p3.bezeichnung == "Villa Armellina Rosato Secco Frizzante Magnum 1,5 l"
    assert p3.menge == Decimal("6")
    assert p3.jahrgang is None
    assert p3.volumen_cl == Decimal("150")  # Sondervariante, Matching entscheidet
    assert p3.preis == Decimal("14.90")

    assert roh.gesamtmenge == Decimal("48")


def test_menrath_summenvermerk_und_liefertag_warnung(extraktor: TextPdfExtraktor, pdfs: Path):
    roh = extraktor.extrahiere(pdfs / "kundenbestellung_menrath.pdf")
    assert [(v.wert, v.einheit) for v in roh.summenvermerke] == [(Decimal("48"), "Fl")]
    warnungen = [h for h in roh.hinweise if h.code == Pruefcode.FELD_FEHLT]
    assert len(warnungen) == 1
    assert warnungen[0].schweregrad == Schweregrad.WARNUNG
    assert "KW 44" in warnungen[0].text
    assert warnungen[0].details == {"rohtext": "KW 44"}


# ---------------------------------------------------------------------------
# (3) Eigene Bestellung an Cide – Eingangstyp B als PDF
# ---------------------------------------------------------------------------


def test_cide_kopf(extraktor: TextPdfExtraktor, pdfs: Path):
    roh = extraktor.extrahiere(pdfs / "eigene_bestellung_cide.pdf")
    assert roh.eingangstyp == Eingangstyp.EIGENE_BESTELLUNG_EMAIL
    assert roh.belegnummer == "BE-2026-0077"
    assert roh.bestellnummer is None
    assert roh.kunde_text == "Menrath"
    assert roh.kunde_adresse is None
    assert roh.lieferant_text == "Cide srl, Treviso"
    assert roh.datum == date(2026, 10, 17)
    assert roh.liefertag == date(2026, 10, 28)
    assert roh.lieferadresse == "Spedition Weinlogistik, Hafenstraße 2, 47119 Duisburg"
    assert _hinweise(roh, Pruefcode.FELD_FEHLT) == []


def test_cide_positionen(extraktor: TextPdfExtraktor, pdfs: Path):
    roh = extraktor.extrahiere(pdfs / "eigene_bestellung_cide.pdf")
    assert len(roh.positionen) == 2
    p1, p2 = roh.positionen
    assert (p1.artikelnummer, p1.menge, p1.einheit, p1.preis) == ("1643", Decimal("60"), "Fl", Decimal("4.20"))
    assert (p2.artikelnummer, p2.menge, p2.einheit, p2.preis) == ("957", Decimal("24"), "Fl", Decimal("4.20"))
    assert p1.bezeichnung == "Villa Armellina Gloria Secco Frizzante 0,75 l"
    assert p2.bezeichnung == "Villa Armellina Rosato Secco Frizzante 0,75 l"
    assert p1.volumen_cl == Decimal("75")
    assert p2.volumen_cl == Decimal("75")
    assert roh.summenvermerke == []  # "Summe netto: 352,80 €" ist Geld, kein Mengenvermerk
    assert roh.gesamtmenge == Decimal("84")


# ---------------------------------------------------------------------------
# (4) Scan – keine Textebene
# ---------------------------------------------------------------------------


def test_scan_ohne_textebene(extraktor: TextPdfExtraktor, pdfs: Path):
    roh = extraktor.extrahiere(pdfs / "scan_preisliste.pdf")
    assert roh.positionen == []
    assert roh.seiten == 1
    fehler = [h for h in roh.hinweise if h.schweregrad == Schweregrad.FEHLER]
    assert len(fehler) == 1
    assert fehler[0].code == Pruefcode.FELD_FEHLT
    assert "Textebene" in fehler[0].text


# ---------------------------------------------------------------------------
# JSON-Roundtrip (Vertrag mit Freigabe/zustand)
# ---------------------------------------------------------------------------


def test_rohbestellung_json_roundtrip(extraktor: TextPdfExtraktor, pdfs: Path):
    roh = extraktor.extrahiere(pdfs / "kundenbestellung_kirchhellen.pdf")
    kopie = RohBestellung.model_validate_json(roh.model_dump_json())
    assert kopie.positionen[0].menge == Decimal("60")
    assert kopie.positionen[0].preis == Decimal("6.90")
    assert kopie.kunde_text == roh.kunde_text


# ---------------------------------------------------------------------------
# Heuristiken ohne PDF
# ---------------------------------------------------------------------------


def test_lese_kopffelder():
    zeilen = [
        "Weinmarkt Kirchhellen GmbH",
        "Bestell-Nr.: B-4712",
        "Kundennr.: 3441",
        "Datum: 15.10.2026",
        "Liefertermin: 24.10.2026",
        "Beleg-Nr. AB-2026-0101",
        "Ihre Bestellung vom 1.10.2026",  # kein Wert mit Ziffern -> ignoriert
        "Lieferadresse: Lager 2, Industriestr. 5, 41066 Mönchengladbach",
        "Weingut: Cantine Colosi",
        "Kunde: Hegmann",
    ]
    felder = lese_kopffelder(zeilen)
    assert felder["bestellnummer"] == "B-4712"
    assert felder["kundennummer"] == "3441"
    assert felder["datum"] == "15.10.2026"
    assert felder["liefertag"] == "24.10.2026"
    assert felder["belegnummer"] == "AB-2026-0101"
    assert "ihre_bestellnummer" not in felder
    assert felder["lieferadresse"] == "Lager 2, Industriestr. 5, 41066 Mönchengladbach"
    assert felder["lieferant"] == "Cantine Colosi"
    assert felder["kunde"] == "Hegmann"


def test_lese_kopffelder_unterscheidet_datum_und_lieferdatum():
    felder = lese_kopffelder(["Lieferdatum: 24.10.2026", "Belegdatum 15.10.2026"])
    assert felder["liefertag"] == "24.10.2026"
    assert felder["datum"] == "15.10.2026"


def test_lese_kopffelder_liefertag_bevorzugt_datum_vor_rohtext():
    felder = lese_kopffelder(["Anlieferung an: Lager 2", "Liefertermin: KW 44", "Liefertag: 30.10.2026"])
    assert felder["liefertag"] == "30.10.2026"
    assert felder["lieferadresse"] == "Lager 2"
    assert lese_kopffelder(["Liefertermin: KW 44"])["liefertag"] == "KW 44"


def test_datum_explizit_vor_vom(extraktor: TextPdfExtraktor, tmp_path: Path):
    text = "Ihre Anfrage vom 01.09.2026\nDatum: 15.10.2026\nBestell-Nr. 1\n6 Fl Grillo 2023 8,60 €\n"
    roh = extraktor.extrahiere_text(text, tmp_path / "d.pdf")
    assert roh.datum == date(2026, 10, 15)
    text2 = "Bestellung vom 01.09.2026\nBestell-Nr. 1\n6 Fl Grillo 2023 8,60 €\n"
    assert extraktor.extrahiere_text(text2, tmp_path / "d2.pdf").datum == date(2026, 9, 1)


def test_zeilenheuristik_liefert_dasselbe_wie_tabellen(extraktor: TextPdfExtraktor, pdfs: Path):
    """Ohne Tabellenerkennung (z. B. Tabelle ohne Linien) muss die Zeilenheuristik dieselben Positionen finden."""
    pfad = pdfs / "kundenbestellung_kirchhellen.pdf"
    mit_tabellen = extraktor.extrahiere(pfad)
    inhalt = lese_pdf(pfad)
    inhalt.tabellen = []
    ohne_tabellen = extraktor._baue_bestellung(pfad, inhalt)
    felder = ("bezeichnung", "menge", "einheit", "preis", "jahrgang", "hinweis", "artikelnummer", "volumen_cl")
    assert [p.model_dump(include=set(felder)) for p in ohne_tabellen.positionen] == [
        p.model_dump(include=set(felder)) for p in mit_tabellen.positionen
    ]
    assert ohne_tabellen.kunde_text == mit_tabellen.kunde_text
    assert ohne_tabellen.summenvermerke == mit_tabellen.summenvermerke


def test_zeilenheuristik_faellt_auf_standardmuster_zurueck():
    zeilen = [
        "Pos Art.-Nr. Menge Einheit Artikel Preis",  # Kopfzeile verspricht Artikelnummern ...
        "12 Fl Colosi Grillo Sicilia DOC 2023 8,60 €",  # ... die Zeilen haben aber keine
    ]
    positionen = text_pdf._positionen_aus_zeilen(zeilen)
    assert [(p.menge, p.bezeichnung) for p in positionen] == [(Decimal("12"), "Colosi Grillo Sicilia DOC 2023")]


def test_extrahiere_text_fliesstext(extraktor: TextPdfExtraktor, tmp_path: Path):
    text = "\n".join(
        [
            "Vinothek Schmidt GmbH",
            "Am Markt 7",
            "45127 Essen",
            "",
            "Bestellnummer 4711",
            "Bestelldatum: 2026-10-01",
            "Lieferung am 09.10.2026",
            "",
            "12 Fl Colosi Nero d'Avola Sicilia DOC 2022 8,40 € 100,80 €",
            "6 Fl. Bollina Gavi di Gavi DOCG 2023 12,50 EUR",
            "2 Kt Riesling trocken 2023 Weingut Beispiel",
            "1 Stk Werbeaufsteller gratis 0,00 €",
            "180 Fl",
        ]
    )
    roh = extraktor.extrahiere_text(text, tmp_path / "bestellung.pdf")
    assert roh.eingangstyp == Eingangstyp.KUNDENBESTELLUNG_PDF
    assert roh.kunde_text == "Vinothek Schmidt GmbH"
    assert roh.kunde_adresse == "Am Markt 7, 45127 Essen"
    assert roh.bestellnummer == "4711"
    assert roh.datum == date(2026, 10, 1)
    assert roh.liefertag == date(2026, 10, 9)
    bez = [(p.menge, p.einheit, p.bezeichnung, p.preis, p.hinweis) for p in roh.positionen]
    assert bez == [
        (Decimal("12"), "Fl", "Colosi Nero d'Avola Sicilia DOC 2022", Decimal("8.40"), None),
        (Decimal("6"), "Fl", "Bollina Gavi di Gavi DOCG 2023", Decimal("12.50"), None),
        (Decimal("2"), "Kt", "Riesling trocken 2023 Weingut Beispiel", None, None),
        (Decimal("1"), "Stk", "Werbeaufsteller", Decimal("0.00"), "gratis"),
    ]
    assert roh.positionen[0].jahrgang == 2022
    assert [(v.wert, v.einheit) for v in roh.summenvermerke] == [(Decimal("180"), "Fl")]


def test_extrahiere_text_adresszeilen_sind_keine_positionen(extraktor: TextPdfExtraktor, tmp_path: Path):
    text = "\n".join(
        [
            "Weinhandlung Müller",
            "Bahnhofstraße 12",
            "47051 Duisburg",
            "Tel. 0203 123456",
            "Bestell-Nr. 2026-10",
            "Datum 17.10.2026",
            "Liefertermin 24.10.2026",
            "24 Fl Colosi Grillo Sicilia DOC 2023 8,60",
            "Gesamt: 24 Flaschen",
            "Summe netto 206,40 €",
        ]
    )
    roh = extraktor.extrahiere_text(text, tmp_path / "x.pdf")
    assert [p.bezeichnung for p in roh.positionen] == ["Colosi Grillo Sicilia DOC 2023"]
    assert roh.kunde_text == "Weinhandlung Müller"
    assert roh.bestellnummer == "2026-10"
    assert [(v.wert, v.einheit) for v in roh.summenvermerke] == [(Decimal("24"), "Fl")]


def test_extrahiere_text_kopfzeile_mit_artikelnummer(extraktor: TextPdfExtraktor, tmp_path: Path):
    text = "\n".join(
        [
            "Bestellung Nr. BE-2026-0078 an Bollina srl, Gavi",
            "Kunde: Vinothek Schmidt",
            "Datum: 18.10.2026",
            "Liefertag: 30.10.2026",
            "Pos Art.-Nr. Menge Einheit Artikel EK-Preis Gesamt",
            "1 3001 36 Fl Bollina Gavi di Gavi DOCG 2023 0,75 l 7,80 € 280,80 €",
            "2 3002 12 Fl Bollina Gavi di Gavi DOCG 2023 0,375 l 4,50 € 54,00 €",
        ]
    )
    roh = extraktor.extrahiere_text(text, tmp_path / "be.pdf")
    assert roh.eingangstyp == Eingangstyp.EIGENE_BESTELLUNG_EMAIL
    assert roh.belegnummer == "BE-2026-0078"
    assert roh.lieferant_text == "Bollina srl, Gavi"
    assert roh.kunde_text == "Vinothek Schmidt"
    assert [(p.artikelnummer, p.menge, p.preis, p.volumen_cl) for p in roh.positionen] == [
        ("3001", Decimal("36"), Decimal("7.80"), Decimal("75")),
        ("3002", Decimal("12"), Decimal("4.50"), Decimal("37.5")),
    ]
    assert _hinweise(roh, Pruefcode.FELD_FEHLT) == []


def test_eingangstyp_b_durch_lieferant_und_kunde(extraktor: TextPdfExtraktor, tmp_path: Path):
    text = "Lieferant: Cantine Colosi\nKunde: Hegmann\nBeleg-Nr.: BE-2026-0080\n12 Fl Nero d'Avola 2022 5,10 €\n"
    roh = extraktor.extrahiere_text(text, tmp_path / "b.pdf")
    assert roh.eingangstyp == Eingangstyp.EIGENE_BESTELLUNG_EMAIL
    assert roh.lieferant_text == "Cantine Colosi"
    assert roh.kunde_text == "Hegmann"
    assert roh.belegnummer == "BE-2026-0080"


def test_eingangstyp_a_wenn_bestellung_an_eigene_firma(extraktor: TextPdfExtraktor, tmp_path: Path):
    text = "\n".join(
        [
            "Weinhaus Menrath",
            "Marktplatz 3",
            "41061 Mönchengladbach",
            "",
            "Bestellung an Wein-Import-Kreis LEH",
            "Bestell-Nr. M-99",
            "6 Fl Colosi Grillo 2023 8,60 €",
        ]
    )
    roh = extraktor.extrahiere_text(text, tmp_path / "a.pdf")
    assert roh.eingangstyp == Eingangstyp.KUNDENBESTELLUNG_PDF
    assert roh.kunde_text == "Weinhaus Menrath"
    assert roh.lieferant_text is None


def test_eingangstyp_b_durch_eigene_firma_als_absender(extraktor: TextPdfExtraktor, tmp_path: Path):
    text = "\n".join(
        [
            "Wein-Import-Kreis LEH · Musterweg 10 · 40210 Düsseldorf",
            "",
            "Cantine Colosi",
            "Via Messina 1",
            "98100 Messina",
            "",
            "Bestellung",
            "Beleg-Nr.: BE-2026-0081",
            "12 Fl Nero d'Avola 2022 5,10 €",
        ]
    )
    roh = extraktor.extrahiere_text(text, tmp_path / "b2.pdf")
    assert roh.eingangstyp == Eingangstyp.EIGENE_BESTELLUNG_EMAIL
    assert roh.lieferant_text == "Cantine Colosi"
    assert roh.kunde_text is None
    assert "Kunde nicht erkannt." in _hinweise(roh, Pruefcode.FELD_FEHLT)


def test_fehlende_kopffelder_erzeugen_warnungen(extraktor: TextPdfExtraktor, tmp_path: Path):
    roh = extraktor.extrahiere_text("Irgendein Text ohne Struktur\nnoch eine Zeile", tmp_path / "leer.pdf")
    codes = {(h.code, h.schweregrad) for h in roh.hinweise}
    assert (Pruefcode.FELD_FEHLT, Schweregrad.WARNUNG) in codes
    assert (Pruefcode.FELD_FEHLT, Schweregrad.FEHLER) in codes  # keine Positionen
    texte = _hinweise(roh, Pruefcode.FELD_FEHLT)
    assert "Kunde nicht erkannt." in texte
    assert "Bestellnummer nicht erkannt." in texte
    assert "Belegdatum nicht erkannt." in texte
    assert "Liefertag nicht erkannt." in texte
    assert "Keine Positionen im PDF erkannt." in texte
    assert roh.positionen == []


def test_tabelle_ohne_kopfzeile_heuristisch(extraktor: TextPdfExtraktor, tmp_path: Path):
    inhalt = inhalt_aus_text("Bestellung\nBestell-Nr. 77\n")
    inhalt.tabellen = [
        [
            ["1", "60", "Villa Armellina Gloria Secco Frizzante", "6,90 €", "414,00 €"],
            ["2", "24", "Villa Armellina Rosato Secco Frizzante", "6,90 €", "165,60 €"],
            ["", "", "Summe netto", "", "580,60 €"],
        ]
    ]
    roh = extraktor._baue_bestellung(tmp_path / "t.pdf", inhalt)
    assert [(p.menge, p.bezeichnung, p.preis) for p in roh.positionen] == [
        (Decimal("60"), "Villa Armellina Gloria Secco Frizzante", Decimal("6.90")),
        (Decimal("24"), "Villa Armellina Rosato Secco Frizzante", Decimal("6.90")),
    ]


def test_tabelle_mit_umgebrochener_bezeichnung_und_hinweisspalte(extraktor: TextPdfExtraktor, tmp_path: Path):
    inhalt = inhalt_aus_text("Bestellung\n")
    inhalt.tabellen = [
        [
            ["Menge", "ME", "Bezeichnung", "Jg.", "Preis", "Bemerkung"],
            ["12", "Fl", "Colosi Nero d'Avola", "2022", "8,40", "11+1"],
            [None, None, "Sicilia DOC", None, None, None],
            ["6", "Fl", "Grillo Sicilia DOC kostenlos", "2023", "0,00", ""],
            ["Summe", "", "", "", "", ""],
        ]
    ]
    roh = extraktor._baue_bestellung(tmp_path / "t.pdf", inhalt)
    assert len(roh.positionen) == 2
    p1, p2 = roh.positionen
    assert p1.bezeichnung == "Colosi Nero d'Avola Sicilia DOC"
    assert p1.hinweis == "11+1"
    assert p1.jahrgang == 2022
    assert p2.bezeichnung == "Grillo Sicilia DOC"
    assert p2.hinweis == "kostenlos"
    assert p2.preis == Decimal("0.00")


def test_positionen_aus_zeilen_direkt():
    zeilen = [
        "41061 Mönchengladbach",  # PLZ, keine Position
        "2026 Jahresbestellung",  # kein Preis, keine Einheit -> keine Position
        "36 x Bollina Gavi di Gavi DOCG 2023 0,75 l à 12,00 €",
        "1 60 Fl Villa Armellina Gloria Secco Frizzante 6,90 € 414,00 €",
        "6 Colosi Grillo Sicilia DOC 2023 8,60 EUR",
        "180 Fl",  # Summenvermerk, keine Position
    ]
    positionen = text_pdf._positionen_aus_zeilen(zeilen)
    assert [(p.menge, p.einheit, p.bezeichnung, p.preis) for p in positionen] == [
        (Decimal("36"), None, "Bollina Gavi di Gavi DOCG 2023 0,75 l", Decimal("12.00")),
        (Decimal("60"), "Fl", "Villa Armellina Gloria Secco Frizzante", Decimal("6.90")),
        (Decimal("6"), None, "Colosi Grillo Sicilia DOC 2023", Decimal("8.60")),
    ]


def test_summenvermerke_direkt():
    zeilen = [
        "Gesamt: 97 Fl.",
        "Summe 180 R",
        "180 Fl",
        "Gesamtmenge: 72",
        "Summe netto: 680,40 €",  # Geld
        "Gesamt 573,00",  # Geld ohne Währung
        "Gesamt: 48 Flaschen, netto 573,00 €",
    ]
    vermerke = text_pdf._summenvermerke(zeilen)
    assert [(v.wert, v.einheit, v.rohtext) for v in vermerke] == [
        (Decimal("97"), "Fl", "Gesamt: 97 Fl."),
        (Decimal("180"), "R", "Summe 180 R"),
        (Decimal("180"), "Fl", "180 Fl"),
        (Decimal("72"), None, "Gesamtmenge: 72"),
        (Decimal("48"), "Fl", "Gesamt: 48 Flaschen"),
    ]


def test_pdfinhalt_aus_text_segmentiert_spalten():
    layout = "Weinmarkt Kirchhellen GmbH            Bestell-Nr.: B-4712\nHauptstraße 1                         Datum: 15.10.2026\n46244 Bottrop\n"
    inhalt = inhalt_aus_text("Weinmarkt Kirchhellen GmbH Bestell-Nr.: B-4712\n", layout)
    assert isinstance(inhalt, PdfInhalt)
    assert inhalt.segmente[:2] == ["Weinmarkt Kirchhellen GmbH", "Bestell-Nr.: B-4712"]
    assert inhalt.layout_spalten_seite1[0][:3] == ["Weinmarkt Kirchhellen GmbH", "Hauptstraße 1", "46244 Bottrop"]
    assert inhalt.layout_spalten_seite1[1][:2] == ["Bestell-Nr.: B-4712", "Datum: 15.10.2026"]


# ---------------------------------------------------------------------------
# Beispiel-PDF-Skript
# ---------------------------------------------------------------------------


def test_beispiel_pdfs_erzeugen_idempotent(tmp_path: Path):
    skript = _lade_skript()
    ziel = tmp_path / "pdfs"
    pfade = skript.erzeuge_alle(ziel)
    assert [p.name for p in pfade] == [
        "kundenbestellung_kirchhellen.pdf",
        "kundenbestellung_menrath.pdf",
        "eigene_bestellung_cide.pdf",
        "scan_preisliste.pdf",
        "spar_sap_bestellung.pdf",
        "lagerbestellung_edeka.pdf",
        "shop_bestellung_fachhandel.pdf",
        "kundenbestellung_revised.pdf",
    ]
    assert all(p.is_file() and p.stat().st_size > 0 for p in pfade)
    inhalte = [p.read_bytes() for p in pfade]
    skript.erzeuge_alle(ziel)  # zweiter Lauf: identische Dateien
    assert [p.read_bytes() for p in pfade] == inhalte
    assert ist_text_pdf(pfade[0]) and ist_text_pdf(pfade[1]) and ist_text_pdf(pfade[2])
    assert ist_text_pdf(pfade[3]) is False


def test_beispiel_pdfs_entsprechen_fixtures(tmp_path: Path, pdfs: Path, settings):
    """Frisch erzeugte PDFs liefern dieselbe Extraktion wie die im Repo liegenden."""
    skript = _lade_skript()
    pfade = skript.erzeuge_alle(tmp_path)
    extraktor = TextPdfExtraktor(settings)
    for pfad in pfade[:3]:
        neu = extraktor.extrahiere(pfad).model_dump(exclude={"quelldatei"})
        alt = extraktor.extrahiere(pdfs / pfad.name).model_dump(exclude={"quelldatei"})
        assert neu == alt


def test_beispiel_pdfs_main(tmp_path: Path, capsys):
    skript = _lade_skript()
    assert skript.main([str(tmp_path)]) == 0
    ausgabe = capsys.readouterr().out
    assert "scan_preisliste.pdf" in ausgabe
    assert skript.STANDARD_ORDNER == Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "pdfs"
