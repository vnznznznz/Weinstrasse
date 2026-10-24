"""Tests für dossier/deckblatt.py (reportlab-Deckblatt). Nur synthetische Daten."""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from pypdf import PdfReader

from auftragsautomat.config import Settings
from auftragsautomat.dossier.deckblatt import (
    CHECKLISTE,
    CHECKLISTE_REIHENFOLGE,
    datum_text,
    erzeuge_deckblatt,
    schriften,
    zahl_text,
    zeit_text,
)
from auftragsautomat.models import (
    Artikel,
    Belegfelder,
    Belegpaket,
    Dossier,
    DossierDatei,
    Eingangstyp,
    Extraktionsmethode,
    Kunde,
    KundeZuordnung,
    Lieferant,
    LieferantZuordnung,
    MatchStatus,
    PositionsZuordnung,
    Pruefcode,
    Pruefhinweis,
    RohBestellung,
    RohPosition,
    Schweregrad,
)

JETZT = datetime(2026, 10, 24, 9, 30)


def _paket(firma_id: str = "wik_fh_seh", *, kunde_name: str = "Weinhandel Muster GmbH", lieferant_name: str = "Weingut Beispiel srl") -> Belegpaket:
    roh = RohBestellung(
        eingangstyp=Eingangstyp.KUNDENBESTELLUNG_PDF,
        quelldatei=Path("eingang/bestellung.pdf"),
        extraktionsmethode=Extraktionsmethode.PDFPLUMBER,
        kunde_text=kunde_name,
        lieferant_text=lieferant_name,
        bestellnummer="4711",
        datum=date(2026, 10, 20),
        liefertag=date(2026, 11, 6),
        lieferadresse="Musterstraße 1, 12345 Musterstadt",
        positionen=[
            RohPosition(laufnr=1, bezeichnung="Rosso Esempio 2022", jahrgang=2022, menge=Decimal("1234"), einheit="Fl"),
            RohPosition(laufnr=2, bezeichnung="DAA Neptun", menge=Decimal("1"), preis=Decimal("0")),
        ],
    )
    paket = Belegpaket(roh=roh, firma_id=firma_id)
    paket.kunde = KundeZuordnung(
        kunde=Kunde(firma_id=firma_id, kundennummer="10001", name=kunde_name, matchcode="Musterhandel"),
        status=MatchStatus.SICHER,
    )
    paket.lieferant = LieferantZuordnung(
        lieferant=Lieferant(firma_id=firma_id, lieferantennummer="90001", name=lieferant_name, matchcode="Beispielgut"),
        status=MatchStatus.SICHER,
    )
    artikel = Artikel(firma_id=firma_id, artikelnummer="2001", bezeichnung="Rosso Esempio 0,75 l", jahrgang=2022, ek_preis=Decimal("5"), vk_preis_1=Decimal("8"))
    paket.positionen = [
        PositionsZuordnung(roh=roh.positionen[0], artikel=artikel, status=MatchStatus.SICHER, menge=Decimal("1234"), ek_preis=Decimal("5"), vk_preis=Decimal("8")),
        PositionsZuordnung(
            roh=roh.positionen[1],
            status=MatchStatus.UEBERSPRUNGEN,
            menge=Decimal("0"),
            hinweise=[Pruefhinweis(code=Pruefcode.NULLPREIS_OHNE_ARTIKEL, schweregrad=Schweregrad.WARNUNG, text="0,00 € ohne Stammartikel", position_laufnr=2)],
        ),
    ]
    paket.belegfelder = Belegfelder(spedition="Spedition Muster", weingut=lieferant_name, shipment="directly", kunde=kunde_name)
    paket.hinweise = [Pruefhinweis(code=Pruefcode.PREIS_ABWEICHUNG, schweregrad=Schweregrad.FEHLER, text="Preis weicht ab")]
    return paket


def _dossier(**felder) -> Dossier:
    basis = dict(
        name="Beispielgut-WIK-Musterhandel-4711-24.10.2026",
        ordner="/tmp/dossiers/Beispielgut-WIK-Musterhandel-4711-24.10.2026",
        firma_id="wik_fh_seh",
        kunde_kurz="Musterhandel",
        lieferant_kurz="Beispielgut",
        kundenbestellnummer="4711",
        bestellnummer="BE-1001",
        ab_nummer="AB-2002",
        fixtermin=date(2026, 11, 6),
        spedition="Spedition Muster",
        tracking_referenz="DEMB00000",
        checkliste={"bestellung_gesendet": True, "abgeholt": False},
        offene_punkte=["Bestätigung des Lieferanten steht aus"],
        vorgeschlagener_zielordner="Auftragsablage (ohne Provision)/Musterhandel/2026",
        dateien=[DossierDatei(name="01_Kundenbestellung_x.pdf", typ="kundenbestellung", groesse=2048, hinzugefuegt=JETZT)],
        erstellt_am=JETZT,
    )
    basis.update(felder)
    return Dossier(**basis)


def _text(pfad: Path) -> str:
    leser = PdfReader(str(pfad))
    text = "\n".join(seite.extract_text() or "" for seite in leser.pages)
    return re.sub(r"\s+", " ", text)


def test_deckblatt_erzeugt_pdf_mit_allen_abschnitten(settings: Settings, tmp_path: Path):
    ziel = erzeuge_deckblatt(_dossier(), _paket(), tmp_path / "00_Deckblatt.pdf", settings=settings, jetzt=JETZT)
    assert ziel.is_file()
    assert ziel.read_bytes().startswith(b"%PDF")
    text = _text(ziel)
    for erwartet in (
        "Kopfdaten",
        "Positionen",
        "Belegfelder",
        "Checkliste",
        "Offene Punkte",
        "Einsortieren",
        "Musterhandel",
        "Beispielgut",
        "4711",
        "BE-1001",
        "AB-2002",
        "DEMB00000",
        "06.11.2026",
        "1.234",
        "2001",
        "Auftragsablage (ohne Provision)/Musterhandel/2026",
        "Erzeugt am 24.10.2026 09:30",
        "Wein-Import-Kreis FH & SEH (WIK)",
    ):
        assert erwartet in text, erwartet
    for label in CHECKLISTE.values():
        assert label.split(" (")[0] in text
    assert "Preis weicht ab" in text
    assert "übersprungen" in text


def test_deckblatt_ohne_paket(settings: Settings, tmp_path: Path):
    ziel = erzeuge_deckblatt(_dossier(), None, tmp_path / "deck.pdf", settings=settings, jetzt=JETZT)
    text = _text(ziel)
    assert "Checkliste" in text
    assert "Prüfhinweise nicht verfügbar" in text
    assert "Positionen" not in text.split("Checkliste")[0] or "Kopfdaten" in text


def test_deckblatt_ohne_settings_und_leerem_dossier(tmp_path: Path):
    dossier = Dossier(name="Test", ordner="x")
    ziel = erzeuge_deckblatt(dossier, None, tmp_path / "unter" / "deck.pdf")
    assert ziel.is_file()
    text = _text(ziel)
    assert "Keine offenen Punkte" in text
    assert "Noch keine Dateien abgelegt" in text


def test_deckblatt_mit_umlauten_und_sonderzeichen(settings: Settings, tmp_path: Path):
    paket = _paket(kunde_name="Weinhandel Söhne & Töchter – Zażółć gęślą", lieferant_name="Château Exemple <Test>")
    dossier = _dossier(kunde_kurz="Söhne & Töchter", lieferant_kurz="Château")
    ziel = erzeuge_deckblatt(dossier, paket, tmp_path / "deck.pdf", settings=settings, jetzt=JETZT)
    text = _text(ziel)
    assert "Söhne" in text
    assert "Château" in text


def test_deckblatt_zusaetzliche_checklistenpunkte_und_viele_positionen(settings: Settings, tmp_path: Path):
    paket = _paket()
    roh_pos = [RohPosition(laufnr=i, bezeichnung=f"Wein {i}", menge=Decimal(i)) for i in range(3, 80)]
    paket.roh.positionen.extend(roh_pos)
    paket.positionen.extend(PositionsZuordnung(roh=p, status=MatchStatus.KEIN_TREFFER, menge=p.menge or Decimal(0)) for p in roh_pos)
    dossier = _dossier(checkliste={"bestellung_gesendet": True, "rechnung_geprueft": True})
    ziel = erzeuge_deckblatt(dossier, paket, tmp_path / "deck.pdf", settings=settings, jetzt=JETZT)
    leser = PdfReader(str(ziel))
    assert len(leser.pages) >= 2
    assert "rechnung geprueft" in _text(ziel)


def test_zahl_und_datumsformate():
    assert zahl_text(Decimal("1234.5")) == "1.234,5"
    assert zahl_text(Decimal("12.50")) == "12,5"
    assert zahl_text(Decimal("12.5"), 2) == "12,50"
    assert zahl_text(Decimal("3420")) == "3.420"
    assert zahl_text(Decimal("0")) == "0"
    assert zahl_text(None) == "–"
    assert zahl_text(7) == "7"
    assert datum_text(date(2026, 11, 6)) == "06.11.2026"
    assert datum_text(None) == "–"
    assert zeit_text(JETZT) == "24.10.2026 09:30"


def test_schriften_liefert_paar():
    normal, fett = schriften()
    assert normal and fett
    assert tuple(CHECKLISTE_REIHENFOLGE) == tuple(CHECKLISTE)
