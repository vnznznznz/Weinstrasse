"""Tests für die Erweiterung der Extraktion auf die echten Belegformate (Ausbaustufe 2, Abschnitt 4)
sowie für Rabatt-, Aktions- und Revised-Order-Erkennung in text_pdf, email_bestellung und vision.

Die PDFs unter tests/fixtures/pdfs werden von scripts/beispiel_pdfs_erzeugen.py erzeugt (synthetische
Daten, Domain example.org). Kein Netzwerk, kein Claude-Aufruf: die Vision-Tests nutzen nur
``wandle_in_rohbestellung`` auf einem fertigen ``VisionErgebnis``.
"""

from __future__ import annotations

import email.message
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from auftragsautomat.extraktion import text_pdf, zahlen
from auftragsautomat.extraktion.email_bestellung import EmailExtraktor, lese_positionen
from auftragsautomat.extraktion.text_pdf import (
    SPEDITIONSZEILE_HINWEIS,
    RevisionTreffer,
    TextPdfExtraktor,
    finde_aktionshinweis,
    finde_lieferbedingung,
    finde_revision,
    inhalt_aus_text,
    ist_speditionszeile,
    lese_kopffelder,
    revisions_hinweis,
)
from auftragsautomat.extraktion.vision import (
    VisionErgebnis,
    VisionPosition,
    VisionSummenvermerk,
    wandle_in_rohbestellung,
)
from auftragsautomat.models import Eingangstyp, Pruefcode, RohBestellung, Schweregrad


@pytest.fixture()
def extraktor(settings) -> TextPdfExtraktor:
    return TextPdfExtraktor(settings)


@pytest.fixture()
def pdfs(fixtures_ordner: Path) -> Path:
    ordner = fixtures_ordner / "pdfs"
    assert (ordner / "spar_sap_bestellung.pdf").is_file(), "Beispiel-PDFs fehlen: python scripts/beispiel_pdfs_erzeugen.py"
    return ordner


def _hinweise(roh: RohBestellung, code: Pruefcode) -> list[str]:
    return [h.text for h in roh.hinweise if h.code == code]


# ---------------------------------------------------------------------------
# (5) SAP-Bestellung einer Handelskette: VE × Inhalt, GTIN, Kunden-/Lieferanten-Artikelnummer
# ---------------------------------------------------------------------------


def test_spar_kopf(extraktor: TextPdfExtraktor, pdfs: Path):
    roh = extraktor.extrahiere(pdfs / "spar_sap_bestellung.pdf")
    assert roh.eingangstyp == Eingangstyp.KUNDENBESTELLUNG_PDF
    assert roh.bestellnummer == "4500000001"  # Bestellnummer des Kunden = Kundenbestellnummer
    assert roh.belegnummer is None
    assert roh.kunde_text == "Beispiel Handels AG"  # Rechnungsempfänger, nicht der Warenempfänger
    assert roh.kunde_adresse == "Handelsstraße 1, 5020 Salzburg"  # GLN-Zeile gehört nicht zur Anschrift
    assert roh.datum == date(2026, 10, 20)
    assert roh.liefertag == date(2026, 11, 3)  # "Lieferdatum" = Fixtermin
    assert roh.lieferadresse == "Beispiel Handels AG Lager Nord, Lagerstraße 5, 4600 Wels"  # Warenempfänger
    assert roh.ladeadresse is None
    assert roh.lieferbedingung == "FH / Frei Haus"
    assert roh.empfaenger_firma_text == "Wein-Import-Kreis LEH"  # Bestellempfänger = unsere Firma
    assert roh.kundennummer_dokument is None  # "Lieferanten-Nr." ist NICHT unsere Kundennummer
    assert roh.ist_revision is False and roh.aktionshinweis is None
    infos = [h for h in roh.hinweise if h.schweregrad == Schweregrad.INFO]
    assert any("Lieferantennummer beim Kunden" in h.text and "100001" in h.text for h in infos)
    assert all(h.schweregrad != Schweregrad.FEHLER for h in roh.hinweise)
    assert "Kunde nicht erkannt." not in _hinweise(roh, Pruefcode.FELD_FEHLT)


def test_spar_positionen_in_flaschen(extraktor: TextPdfExtraktor, pdfs: Path):
    roh = extraktor.extrahiere(pdfs / "spar_sap_bestellung.pdf")
    assert [p.laufnr for p in roh.positionen] == [1, 2]
    p1, p2 = roh.positionen
    assert p1.menge_ve == Decimal("570") and p1.ve_inhalt == Decimal("6")
    assert p1.menge == Decimal("3420")  # 570 VE × 6 Flaschen
    assert p1.einheit == "Fl"
    assert p1.artikelnummer == "1643"  # "Lief. ANr." = unsere Artikelnummer
    assert p1.kundenartikelnummer == "700123"  # "Artikelnummer" der Kette
    assert p1.gtin == "4099999000018"
    assert p1.volumen_cl == Decimal("75")
    assert p1.hinweis == "570 VE à 6 Fl"  # VE-Angabe bleibt sichtbar
    assert p1.rohtext and "570.0" in p1.rohtext and "6.00" in p1.rohtext
    assert "VE 6" in p1.bezeichnung
    assert p2.menge == Decimal("720") and p2.menge_ve == Decimal("120")
    assert roh.gesamtmenge == Decimal("4140")
    assert all(isinstance(p.menge, Decimal) for p in roh.positionen)


# ---------------------------------------------------------------------------
# (6) Lagerbestellung einer Kellerei: Kolli × Inhalt, Belegnummer, Lade-/Anlieferadresse
# ---------------------------------------------------------------------------


def test_lagerbestellung_kopf(extraktor: TextPdfExtraktor, pdfs: Path):
    roh = extraktor.extrahiere(pdfs / "lagerbestellung_edeka.pdf")
    assert roh.eingangstyp == Eingangstyp.KUNDENBESTELLUNG_PDF
    assert roh.kunde_text == "Musterkellerei Nord GmbH"
    assert roh.belegnummer == "3450000001"
    assert roh.bestellnummer == "3450000001"  # nur Belegnummer genannt -> auch Kundenbestellnummer (INFO)
    assert any("Belegnummer 3450000001" in t for t in _hinweise(roh, Pruefcode.FELD_FEHLT))
    assert roh.datum == date(2026, 10, 21)
    assert roh.liefertag == date(2026, 11, 5)
    assert roh.ladeadresse == "Quinta Beispiel Lda., Rua do Vinho 10, 4900 Viana do Castelo"
    assert roh.lieferadresse == "Lagerhaus Beispiel Nord, Hafenstraße 8, 47119 Duisburg"
    assert roh.empfaenger_firma_text == "Wine-of-Excellence LEH"
    assert "Bestellnummer nicht erkannt." not in _hinweise(roh, Pruefcode.FELD_FEHLT)
    assert all(h.schweregrad != Schweregrad.FEHLER for h in roh.hinweise)


def test_lagerbestellung_positionen_kolli(extraktor: TextPdfExtraktor, pdfs: Path):
    roh = extraktor.extrahiere(pdfs / "lagerbestellung_edeka.pdf")
    assert len(roh.positionen) == 2
    p1, p2 = roh.positionen
    assert p1.menge_ve == Decimal("3250") and p1.ve_inhalt == Decimal("6")
    assert p1.menge == Decimal("19500")  # 3.250 KOA × 6 = Gesamt 19.500
    assert p1.einheit == "Fl"
    assert p1.hinweis == "3250 KOA à 6 Fl"
    assert p1.preis == Decimal("1.1410")
    assert p1.volumen_cl == Decimal("75")  # "htr.0,75l"
    assert p1.kundenartikelnummer == "123456"  # Artikelnr. neben der GTIN ist die des Kunden
    assert p1.artikelnummer is None
    assert p1.gtin == "4099999000100"
    assert p2.menge == Decimal("3000")
    # Gesamt-Spalte stimmt mit VE × Inhalt überein -> keine Abweichung
    assert _hinweise(roh, Pruefcode.SUMME_ABWEICHUNG) == []


def test_gesamtspalte_weicht_von_ve_ab(extraktor: TextPdfExtraktor, tmp_path: Path):
    inhalt = inhalt_aus_text("Belegnummer: 1\n")
    inhalt.tabellen = [
        [
            ["Pos", "Artikelbez.", "Menge", "ME", "Inhalt", "Gesamt", "Preis"],
            ["1", "Vinho Verde 0,75l", "100", "KOA", "6", "500", "1,10"],
        ]
    ]
    roh = extraktor._baue_bestellung(tmp_path / "t.pdf", inhalt)
    assert roh.positionen[0].menge == Decimal("600")
    warnungen = [h for h in roh.hinweise if h.code == Pruefcode.SUMME_ABWEICHUNG]
    assert len(warnungen) == 1
    assert warnungen[0].position_laufnr == 1 and "500" in warnungen[0].text


# ---------------------------------------------------------------------------
# (7) Shop-Bestellung Fachhandel: Zeilenheuristik, ArtNr/Hinweis-Folgezeilen, Speditionszeile
# ---------------------------------------------------------------------------


def test_shop_kopf(extraktor: TextPdfExtraktor, pdfs: Path):
    roh = extraktor.extrahiere(pdfs / "shop_bestellung_fachhandel.pdf")
    assert roh.eingangstyp == Eingangstyp.KUNDENBESTELLUNG_PDF
    assert roh.kunde_text == "Vinothek Beispiel GmbH"
    assert roh.kunde_adresse == "Marktplatz 2, 45127 Essen"
    assert roh.bestellnummer == "1559"  # "Auftragsnummer 1559"
    assert roh.datum == date(2026, 10, 22)  # "Auftragsdatum"
    assert roh.kundennummer_dokument == "2100"
    assert roh.lieferadresse == "Vinothek Beispiel GmbH, Marktplatz 2, 45127 Essen"  # "Rechnungs-/Lieferanschrift"
    assert roh.lieferbedingung == "frei Haus"
    assert roh.empfaenger_firma_text == "Weinstrasse Agentur"


def test_shop_positionen(extraktor: TextPdfExtraktor, pdfs: Path):
    roh = extraktor.extrahiere(pdfs / "shop_bestellung_fachhandel.pdf")
    assert len(roh.positionen) == 3
    daa, p2, p3 = roh.positionen
    assert daa.bezeichnung == "DAA Neptun DE00000000000"
    assert daa.menge == Decimal("1") and daa.preis == Decimal("0.00")
    assert daa.hinweis == SPEDITIONSZEILE_HINWEIS  # bleibt Position, Matching überspringt sie
    assert p2.bezeichnung == "Villa Armellina Gloria Secco Frizzante 0,75l"
    assert p2.menge == Decimal("60") and p2.preis == Decimal("6.90")
    assert p2.artikelnummer == "11081"  # Folgezeile "ArtNr: 11081"
    assert p2.hinweis == "11+1"  # Folgezeile "Hinweis: 11+1"
    assert p2.volumen_cl == Decimal("75")
    assert p2.rabatt_prozent is None  # "19 %" MwSt ist kein Rabatt
    assert p3.artikelnummer == "2001" and p3.jahrgang == 2022 and p3.menge == Decimal("12")
    assert roh.summenvermerke == []  # "Gesamt 612,61 €" ist Geld
    assert roh.gesamtmenge == Decimal("73")


# ---------------------------------------------------------------------------
# (8) Revised Order mit Rabatten und Aktionszeitraum
# ---------------------------------------------------------------------------


def test_revised_order_pdf(extraktor: TextPdfExtraktor, pdfs: Path):
    roh = extraktor.extrahiere(pdfs / "kundenbestellung_revised.pdf")
    assert roh.ist_revision is True
    assert roh.revision_von == "B-5001"
    assert roh.bestellnummer == "B-5002"  # die ersetzte Nummer ist nicht die Bestellnummer
    assert roh.kundennummer_dokument == "2100"
    assert roh.empfaenger_firma_text == "Wein-Import-Kreis FH & SEH"
    revision = [h for h in roh.hinweise if h.code == Pruefcode.REVISION]
    assert len(revision) == 1
    assert revision[0].schweregrad == Schweregrad.WARNUNG
    assert "B-5001" in revision[0].text and revision[0].details["revision_von"] == "B-5001"
    assert roh.aktionshinweis == "Aktionszeitraum 01.11.-30.11.2026"
    p1, p2, p3 = roh.positionen
    assert (p1.rabatt_prozent, p1.rabatt_text) == (Decimal("10"), "10 %")  # Rabatt-Spalte
    assert (p2.rabatt_prozent, p2.rabatt_text) == (Decimal("5"), "Aktion -5 %")  # Rabatt im Text
    assert p2.bezeichnung == "Colosi Nero d'Avola Sicilia DOC"  # Rabatttext entfernt
    assert p2.jahrgang == 2022
    assert p3.rabatt_prozent is None
    assert [(v.wert, v.einheit) for v in roh.summenvermerke] == [(Decimal("90"), "Fl")]


# ---------------------------------------------------------------------------
# Skript: alle acht PDFs deterministisch, Fixtures aktuell
# ---------------------------------------------------------------------------


def test_neue_beispiel_pdfs_entsprechen_fixtures(tmp_path: Path, pdfs: Path, settings):
    import importlib.util
    import sys

    skript = Path(__file__).resolve().parents[1] / "scripts" / "beispiel_pdfs_erzeugen.py"
    spec = importlib.util.spec_from_file_location("beispiel_pdfs_erzeugen_leh", skript)
    assert spec is not None and spec.loader is not None
    modul = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = modul
    spec.loader.exec_module(modul)
    pfade = modul.erzeuge_alle(tmp_path)
    neue = [p for p in pfade if p.name in {modul.DATEI_SPAR, modul.DATEI_LAGER, modul.DATEI_SHOP, modul.DATEI_REVISED}]
    assert len(neue) == 4
    extraktor = TextPdfExtraktor(settings)
    for pfad in neue:
        assert pfad.read_bytes() == (pdfs / pfad.name).read_bytes(), f"{pfad.name} im Repo veraltet"
        neu = extraktor.extrahiere(pfad).model_dump(exclude={"quelldatei"})
        alt = extraktor.extrahiere(pdfs / pfad.name).model_dump(exclude={"quelldatei"})
        assert neu == alt


# ---------------------------------------------------------------------------
# Heuristiken ohne PDF: Kopffelder, VE, Rabatt, Aktion, Revision, Speditionszeile
# ---------------------------------------------------------------------------


def test_kopffelder_leh():
    felder = lese_kopffelder(
        [
            "Lieferanten-Nr.: 100001",
            "Lieferbedingungen: FH / Frei Haus",
            "Ladeadresse: Quinta Beispiel, Rua do Vinho 10, 4900 Viana do Castelo",
            "Anlieferadresse: Lagerhaus Nord, Hafenstraße 8, 47119 Duisburg",
            "Lieferdatum: 05.11.2026",
            "Belegnummer: 3450000001 Lagerbestellung",
            "ersetzt Bestellung Nr. 4500000099",  # Revisionszeile liefert keine Kopfnummer
            "Bestellung - Nr.: 4500000001",
        ]
    )
    assert felder["lieferantennummer"] == "100001"
    assert felder["lieferbedingung"] == "FH / Frei Haus"
    assert felder["ladeadresse"].startswith("Quinta Beispiel")
    assert felder["lieferadresse"].startswith("Lagerhaus Nord")
    assert felder["liefertag"] == "05.11.2026"
    assert felder["belegnummer"] == "3450000001"
    assert felder["bestellnummer"] == "4500000001"
    assert "kundennummer" not in felder
    # Zwei Überschriften auf einer Zeile ergeben keinen Wert
    assert "ladeadresse" not in lese_kopffelder(["Ladeadresse: Anlieferadresse:"])


def test_lieferantennummer_ist_keine_kundennummer(extraktor: TextPdfExtraktor, tmp_path: Path):
    text = "\n".join(
        [
            "Beispiel Handels AG",
            "Handelsstraße 1",
            "5020 Salzburg",
            "",
            "Bestellung - Nr.: 4500000002",
            "Lieferanten-Nr.: 100001",
            "Lieferdatum: 03.11.2026",
            "12 Fl Colosi Grillo Sicilia DOC 2023 8,60 €",
        ]
    )
    roh = extraktor.extrahiere_text(text, tmp_path / "sap.pdf")
    assert roh.kundennummer_dokument is None
    assert roh.bestellnummer == "4500000002"
    assert any("100001" in h.text and h.schweregrad == Schweregrad.INFO for h in roh.hinweise)


@pytest.mark.parametrize(
    ("zeilen", "erwartet"),
    [
        (["Lieferung frei Haus bis Rampe"], "frei Haus"),
        (["Preise ex cellar, netto"], "ex cellar"),
        (["Incoterms: EXW Treviso"], "EXW"),  # Freitext-Finder liest auch Incoterm-Kürzel
        (["Vielen Dank für Ihre Bestellung"], None),
    ],
)
def test_finde_lieferbedingung(zeilen: list[str], erwartet: str | None):
    assert finde_lieferbedingung(zeilen) == erwartet


def test_ve_aus_tabelle_mit_vpe_und_gebinde(extraktor: TextPdfExtraktor, tmp_path: Path):
    inhalt = inhalt_aus_text("Bestellung\nBestell-Nr. 7\n")
    inhalt.tabellen = [
        [
            ["Pos", "Menge", "VPE", "Artikel", "Preis"],
            ["1", "12", "6", "Colosi Grillo Sicilia DOC 2023 0,75 l", "8,60"],  # VPE = Inhalt, Menge = VE
            ["2", "24 Fl", "", "Bollina Gavi 2023 0,75 l", "12,50"],  # Flaschen bleiben Flaschen
            ["3", "4 Kt", "", "Villa Armellina Gloria 6er Karton", "6,90"],  # Inhalt aus der Bezeichnung
            ["4", "2 Kt", "", "Villa Armellina Rosato", "6,90"],  # Inhalt unbekannt -> bleibt 2 Kt
        ]
    ]
    roh = extraktor._baue_bestellung(tmp_path / "ve.pdf", inhalt)
    p1, p2, p3, p4 = roh.positionen
    assert (p1.menge, p1.menge_ve, p1.ve_inhalt, p1.einheit) == (Decimal("72"), Decimal("12"), Decimal("6"), "Fl")
    assert (p2.menge, p2.menge_ve, p2.einheit) == (Decimal("24"), None, "Fl")
    assert (p3.menge, p3.menge_ve, p3.ve_inhalt, p3.einheit) == (Decimal("24"), Decimal("4"), Decimal("6"), "Fl")
    assert (p4.menge, p4.menge_ve, p4.ve_inhalt, p4.einheit) == (Decimal("2"), None, None, "Kt")
    assert p3.hinweis == "4 Kt à 6 Fl"


def test_kolli_ohne_inhalt_wird_nicht_freigabefaehig(extraktor: TextPdfExtraktor, tmp_path: Path, settings):
    """Regression (Review Befund 8): '10 Kolli' ohne Inhalt je VE wird nicht als 10 Flaschen freigegeben."""
    from auftragsautomat.matching.pipeline import erstelle_belegpaket

    position = text_pdf._baue_position(bezeichnung="Gavi di Gavi 2023 0,75 l", menge="10", einheit="KOA", rohtext="r")
    assert (position.menge, position.einheit, position.ve_inhalt) == (Decimal("10"), "KOA", None)
    text = "Bestellung\nBestell-Nr. 4711\nPos Art.-Nr. Bezeichnung Menge Preis\n1 1643 Gavi di Gavi DOCG 2023 0,75 l 10 Kolli 6,90\n"
    roh = extraktor.extrahiere_text(text, tmp_path / "kolli.pdf")
    kolli = [p for p in roh.positionen if p.menge == Decimal("10")]
    assert kolli and kolli[0].einheit == "Kolli" and kolli[0].ve_inhalt is None and kolli[0].menge_ve is None
    paket = erstelle_belegpaket(roh, settings, firma_id="wik_leh")
    assert paket.blockiert
    assert any(h.code == Pruefcode.MENGE_FEHLT and "Inhalt je VE unbekannt" in h.text for h in paket.alle_hinweise)


def test_gebinde_in_mengenzelle_bleibt_kompatibel():
    position = text_pdf._baue_position(bezeichnung="Gavi 2023", menge="12 x 6", preis="12,50", rohtext="r")
    assert position is not None
    assert (position.menge, position.menge_ve, position.ve_inhalt) == (Decimal("72"), Decimal("12"), Decimal("6"))
    assert position.hinweis == "12 VE à 6 Fl"


def test_artikelnummern_zuordnung_kopfzeile():
    # Lief. ANr. neben Artikelnummer: Lief. ANr. ist unsere Nummer, Artikelnummer die des Kunden
    spalten = text_pdf._spalten_aus_kopfzeile(["Pos", "GTIN", "Artikelnummer", "Lief. ANr.", "Artikelbezeichnung", "Menge"])
    assert spalten == {"pos": 0, "gtin": 1, "kundenartikelnummer": 2, "artikelnummer": 3, "bezeichnung": 4, "menge": 5}
    # Artikelnr. neben GTIN ohne Lieferanten-Spalte: Nummer des Kunden
    spalten = text_pdf._spalten_aus_kopfzeile(["Pos", "Kolli-GTIN", "Artikelnr.", "Artikelbez.", "Menge", "ME", "Inhalt"])
    assert spalten["kundenartikelnummer"] == 2 and "artikelnummer" not in spalten and spalten["gtin"] == 1
    # Fachhandel/eigene Bestellung: Art.-Nr. bleibt unsere Artikelnummer
    spalten = text_pdf._spalten_aus_kopfzeile(["Pos", "Art.-Nr.", "Menge", "Einheit", "Artikel", "EK-Preis"])
    assert spalten["artikelnummer"] == 1 and "kundenartikelnummer" not in spalten


def test_zeilenheuristik_leh_kopfzeile():
    zeilen = [
        "Pos GTIN Artikelnr. Artikelbez. Menge ME Inhalt Preis",
        "1 4099999000100 123456 Vinho Verde DOC 0,75l 100 KOA 6 1,14 €",
    ]
    positionen = text_pdf._positionen_aus_zeilen(zeilen)
    assert len(positionen) == 1
    p = positionen[0]
    assert (p.gtin, p.kundenartikelnummer, p.menge, p.menge_ve, p.ve_inhalt, p.einheit) == (
        "4099999000100",
        "123456",
        Decimal("600"),
        Decimal("100"),
        Decimal("6"),
        "Fl",
    )
    assert p.preis == Decimal("1.14")


@pytest.mark.parametrize(
    ("bezeichnung", "preis", "erwartet"),
    [
        ("DAA Neptun", Decimal("0"), True),
        ("DAA Neptun DE00000000000", Decimal("0.00"), True),
        ("Verbrauchsteuer-Nr. DE00000000000", Decimal("0"), True),
        ("DAA Neptun", Decimal("5.00"), False),
        ("Colosi Grillo", Decimal("0"), False),
        ("DAA Neptun", None, False),
    ],
)
def test_ist_speditionszeile(bezeichnung: str, preis: Decimal | None, erwartet: bool):
    assert ist_speditionszeile(bezeichnung, preis) is erwartet


@pytest.mark.parametrize(
    ("text", "prozent", "rohtext"),
    [
        ("Rabatt 10 %", Decimal("10"), "Rabatt 10 %"),
        ("Gavi 2023 -10%", Decimal("10"), "-10%"),
        ("Aktion -10 %", Decimal("10"), "Aktion -10 %"),
        ("Aktionsrabatt 10%", Decimal("10"), "Aktionsrabatt 10%"),
        ("12,5 % Nachlass", Decimal("12.5"), "12,5 % Nachlass"),
        ("Gloria 0,75 l 19 %", None, None),  # MwSt ist kein Rabatt
        ("19 % MwSt", None, None),
        ("Skonto 2 %", None, None),
    ],
)
def test_parse_rabatt(text: str, prozent: Decimal | None, rohtext: str | None):
    rabatt = zahlen.parse_rabatt(text)
    if prozent is None:
        assert rabatt is None
    else:
        assert rabatt is not None and (rabatt.prozent, rabatt.rohtext) == (prozent, rohtext)


def test_parse_prozent_und_ve_hilfen():
    assert zahlen.parse_prozent("10 %") == Decimal("10")
    assert zahlen.parse_prozent("-10,5 %") == Decimal("10.5")
    assert zahlen.parse_prozent("0") is None and zahlen.parse_prozent("abc") is None
    assert zahlen.parse_ve_inhalt("Villa Armellina Gloria 0,75l G VE 6") == Decimal("6")
    assert zahlen.parse_ve_inhalt("Gavi 6 x 0,75 l") == Decimal("6")
    assert zahlen.parse_ve_inhalt("Gavi 2023 0,75 l") is None
    assert zahlen.ist_gebinde_einheit("KOA") and zahlen.ist_gebinde_einheit("Kolli") and not zahlen.ist_gebinde_einheit("Fl")
    assert zahlen.glatt(Decimal("570.0")) == Decimal("570") and str(zahlen.glatt(Decimal("570.0"))) == "570"
    assert zahlen.parse_menge("36 Verao Vinho Verde").einheit is None  # "VE" nicht aus "Verao" lesen


@pytest.mark.parametrize(
    ("zeilen", "erwartet"),
    [
        (["Aktion KW 45"], "Aktion KW 45"),
        (["Lieferung in der Promo"], "Promo"),
        (["Aktionszeitraum 01.11.-30.11."], "Aktionszeitraum 01.11.-30.11."),
        (["Aktion Herbst 2026 für alle Filialen"], "Aktion Herbst 2026"),
        (["2 Fl Gavi Aktion -10 % 8,60 €"], None),  # Positionsrabatt, kein Aktionshinweis
        (["Aktionsrabatt 10 %"], None),
        (["Normale Bestellung"], None),
    ],
)
def test_finde_aktionshinweis(zeilen: list[str], erwartet: str | None):
    assert finde_aktionshinweis(zeilen) == erwartet


@pytest.mark.parametrize(
    ("zeilen", "revision_von", "ist"),
    [
        (["Revised order"], None, True),
        (["REVISED"], None, True),
        (["Änderung zur Bestellung Nr. 4500000001"], "4500000001", True),
        (["Diese Bestellung ersetzt Bestellung Nr. B-123 vom 1.10."], "B-123", True),
        (["Storno + Neu"], None, True),
        (["replaces order 4711"], "4711", True),
        (["Bestellung B-5002"], None, False),
        (["Revisionsstand 3"], None, False),
        # Regression (Review Befund 1): Alltagsfloskeln sind keine Revised Order
        (["Please do not change order quantities"], None, False),
        (["revised prices apply from 1 November"], None, False),
        (["Purchase Order (REVISED)"], None, True),
        (["PO 4500001 - REVISED"], None, True),
        (["Change Order No. 3"], None, True),
    ],
)
def test_finde_revision(zeilen: list[str], revision_von: str | None, ist: bool):
    treffer = finde_revision(zeilen)
    assert (treffer is not None) is ist
    if treffer is not None:
        assert treffer.revision_von == revision_von


def test_revisions_hinweis_text():
    hinweis = revisions_hinweis(RevisionTreffer(rohtext="ersetzt Bestellung Nr. 123", revision_von="123"))
    assert hinweis.code == Pruefcode.REVISION and hinweis.schweregrad == Schweregrad.WARNUNG
    assert "123" in hinweis.text and "Neu" in hinweis.text
    ohne = revisions_hinweis(RevisionTreffer(rohtext="REVISED"))
    assert "nicht genannt" in ohne.text and ohne.details["revision_von"] == ""


def test_extrahiere_text_rabatt_aktion_revision(extraktor: TextPdfExtraktor, tmp_path: Path):
    text = "\n".join(
        [
            "Vinothek Beispiel GmbH",
            "Marktplatz 2",
            "45127 Essen",
            "",
            "Bestell-Nr. B-77",
            "Datum: 20.10.2026",
            "Revised order – ersetzt Bestellung Nr. B-76",
            "Aktion KW 45",
            "12 Fl Colosi Grillo Sicilia DOC 2023 Aktion -10 % 8,60 €",
            "6 Fl Bollina Gavi di Gavi DOCG 2023 12,50 €",
            "Rabatt 5 %",
            "1 Stk DAA Neptun 0,00 €",
        ]
    )
    roh = extraktor.extrahiere_text(text, tmp_path / "r.pdf")
    assert roh.bestellnummer == "B-77"
    assert roh.ist_revision and roh.revision_von == "B-76"
    assert roh.aktionshinweis == "Aktion KW 45"
    p1, p2, p3 = roh.positionen
    assert p1.bezeichnung == "Colosi Grillo Sicilia DOC 2023"
    assert (p1.rabatt_prozent, p1.rabatt_text) == (Decimal("10"), "Aktion -10 %")
    assert (p2.rabatt_prozent, p2.rabatt_text) == (Decimal("5"), "Rabatt 5 %")  # Folgezeile
    assert p3.hinweis == SPEDITIONSZEILE_HINWEIS
    assert len([h for h in roh.hinweise if h.code == Pruefcode.REVISION]) == 1


# ---------------------------------------------------------------------------
# E-Mail (eigene Bestellung): Rabatt, Aktionshinweis, Revised Order
# ---------------------------------------------------------------------------


def _eml(pfad: Path, text: str, betreff: str) -> Path:
    msg = email.message.EmailMessage()
    msg["From"] = "Wein-Import-Kreis LEH <bestellung@example.org>"
    msg["To"] = "Cide srl <ordini@example.org>"
    msg["Subject"] = betreff
    msg["Date"] = "Tue, 20 Oct 2026 10:00:00 +0200"
    msg.set_content(text)
    pfad.write_bytes(msg.as_bytes())
    return pfad


def test_email_rabatt_aktion_revision(settings, tmp_path: Path):
    text = (
        "Sehr geehrte Damen und Herren der Cide srl,\n\n"
        "Revised order: diese Bestellung ersetzt Bestellung Nr. BE-2026-0070.\n"
        "Aktionszeitraum 01.11.-30.11.2026\n"
        "Kunde: Vinothek Beispiel\nLiefertermin: 06.11.2026\n\n"
        "- 60 Fl Villa Armellina Gloria Secco Frizzante Aktion -10 % à 4,20 €\n"
        "- 24 Fl Villa Armellina Rosato Secco Frizzante à 4,20 €\n"
        "  Rabatt 5 %\n"
        "- 1 Stk DAA Neptun DE00000000000 à 0,00 €\n\n"
        "Mit freundlichen Grüßen\nWein-Import-Kreis LEH\n"
    )
    roh = EmailExtraktor(settings).extrahiere(_eml(tmp_path / "rev.eml", text, "Bestellung BE-2026-0071 für Vinothek Beispiel"))
    assert roh.ist_revision and roh.revision_von == "BE-2026-0070"
    assert roh.belegnummer == "BE-2026-0071"
    assert roh.aktionshinweis == "Aktionszeitraum 01.11.-30.11.2026"
    assert len([h for h in roh.hinweise if h.code == Pruefcode.REVISION]) == 1
    p1, p2, p3 = roh.positionen
    assert p1.bezeichnung == "Villa Armellina Gloria Secco Frizzante"
    assert (p1.rabatt_prozent, p1.rabatt_text) == (Decimal("10"), "Aktion -10 %")
    assert (p2.rabatt_prozent, p2.rabatt_text) == (Decimal("5"), "Rabatt 5 %")
    assert p3.hinweis == SPEDITIONSZEILE_HINWEIS and p3.preis == Decimal("0.00")


def test_email_ohne_revision(settings, tmp_path: Path):
    text = "Hallo Cide-Team,\n\nKunde: Menrath\n- 12 Fl Villa Armellina Gloria 0,75 l à 4,20 €\n\nViele Grüße\n"
    roh = EmailExtraktor(settings).extrahiere(_eml(tmp_path / "normal.eml", text, "Bestellung BE-2026-0072"))
    assert roh.ist_revision is False and roh.revision_von is None and roh.aktionshinweis is None
    assert roh.positionen[0].rabatt_prozent is None
    assert not [h for h in roh.hinweise if h.code == Pruefcode.REVISION]


def test_lese_positionen_rabatt_folgezeile():
    positionen = lese_positionen(["- 12 Fl Gavi 2023 à 12,50 €", "Rabatt 10 %", "- 6 Fl Grillo 2023 -5% à 8,60 €"])
    assert [(p.rabatt_prozent, p.rabatt_text) for p in positionen] == [(Decimal("10"), "Rabatt 10 %"), (Decimal("5"), "-5%")]
    assert positionen[1].bezeichnung == "Grillo 2023"


# ---------------------------------------------------------------------------
# Vision (ohne API-Aufruf): Schema-Felder und deterministische Rückfallerkennung
# ---------------------------------------------------------------------------


def _vision_position(**werte) -> VisionPosition:
    basis = {
        "laufnr": 1,
        "bezeichnung": "Colosi Grillo Sicilia DOC",
        "jahrgang": 2023,
        "menge_text": "12",
        "menge": 12.0,
        "einheit": "Fl",
        "preis": 8.6,
        "volumen_text": "0,75 l",
        "artikelnummer": None,
        "hinweis": None,
        "durchgestrichen": False,
        "konfidenz_menge": 0.95,
        "konfidenz_preis": 0.95,
        "rohtext": "Grillo Sicilia DOC 2023 0,75 l 8,60 12",
    }
    basis.update(werte)
    return VisionPosition(**basis)


def _vision_ergebnis(positionen: list[VisionPosition], **werte) -> VisionErgebnis:
    basis = {
        "kunde_text": "Weinhaus Beispiel",
        "lieferant_text": "Cantine Colosi",
        "belegnummer": None,
        "bestellnummer": "B-9",
        "datum": "20.10.2026",
        "liefertag": None,
        "lieferadresse": None,
        "positionen": positionen,
        "summenvermerke": [VisionSummenvermerk(wert=12.0, einheit="Fl", rohtext="12 Fl", konfidenz=0.9)],
        "seitenzahl": 1,
        "anmerkungen": [],
    }
    basis.update(werte)
    return VisionErgebnis(**basis)


def test_vision_schema_felder_sind_optional():
    ergebnis = _vision_ergebnis([_vision_position()])
    assert ergebnis.ist_revision is False and ergebnis.revision_von is None and ergebnis.aktionshinweis is None
    assert ergebnis.positionen[0].rabatt_prozent is None
    schema = VisionErgebnis.model_json_schema()
    assert "rabatt_prozent" in schema["$defs"]["VisionPosition"]["properties"]
    assert {"aktionshinweis", "ist_revision", "revision_von"} <= set(schema["properties"])


def test_vision_rabatt_aktion_revision_aus_antwort(tmp_path: Path):
    ergebnis = _vision_ergebnis(
        [_vision_position(rabatt_prozent=10.0, rabatt_text="Aktion -10 %")],
        aktionshinweis="Aktion KW 45",
        ist_revision=True,
        revision_von="B-8",
    )
    roh = wandle_in_rohbestellung(ergebnis, tmp_path / "scan.pdf", 0.8)
    assert roh.positionen[0].rabatt_prozent == Decimal("10")
    assert roh.positionen[0].rabatt_text == "Aktion -10 %"
    assert roh.aktionshinweis == "Aktion KW 45"
    assert roh.ist_revision and roh.revision_von == "B-8"
    revision = [h for h in roh.hinweise if h.code == Pruefcode.REVISION]
    assert len(revision) == 1 and "B-8" in revision[0].text


def test_vision_rueckfall_aus_rohtext_und_anmerkungen(tmp_path: Path):
    ergebnis = _vision_ergebnis(
        [
            _vision_position(hinweis="Rabatt 5 %"),
            _vision_position(laufnr=2, bezeichnung="DAA Neptun", preis=0.0, menge=1.0, menge_text="1", rohtext="DAA Neptun 0,00 1"),
        ],
        anmerkungen=["Handschriftlich: Revised order, ersetzt Bestellung Nr. 4711", "Aktionszeitraum 01.11.-30.11."],
    )
    roh = wandle_in_rohbestellung(ergebnis, tmp_path / "scan.pdf", 0.8)
    assert (roh.positionen[0].rabatt_prozent, roh.positionen[0].rabatt_text) == (Decimal("5"), "Rabatt 5 %")
    assert roh.positionen[1].hinweis == SPEDITIONSZEILE_HINWEIS
    assert roh.ist_revision and roh.revision_von == "4711"
    assert roh.aktionshinweis == "Aktionszeitraum 01.11.-30.11."


def test_vision_unplausibler_rabatt_wird_verworfen(tmp_path: Path):
    ergebnis = _vision_ergebnis([_vision_position(rabatt_prozent=250.0, rabatt_text="250 %")])
    roh = wandle_in_rohbestellung(ergebnis, tmp_path / "scan.pdf", 0.8)
    assert roh.positionen[0].rabatt_prozent is None
