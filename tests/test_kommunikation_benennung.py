"""Tests für kommunikation.benennung: Anhangsnamen laufen ausschließlich über die Titulierung (benennung.py)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from auftragsautomat import benennung as titulierung
from auftragsautomat.config import Settings
from auftragsautomat.kommunikation.benennung import (
    anhangsname,
    auftragskopie_pdf,
    bereinige,
    bestellung_pdf,
    enthaelt_verbotene_woerter,
    firma_fuer,
    firmenkuerzel,
    lieferschein_pdf,
    speditionsauftrag_pdf,
)
from auftragsautomat.models import (
    Belegfelder,
    Belegpaket,
    Eingangstyp,
    Extraktionsmethode,
    Kunde,
    KundeZuordnung,
    Lieferant,
    LieferantZuordnung,
    MatchStatus,
    RohBestellung,
)

DATUM = date(2026, 10, 24)
T = " – "


def test_firmenkuerzel_und_firma_fuer(settings: Settings):
    assert firmenkuerzel("wik_leh", settings) == "WIK" and firmenkuerzel("woe_leh", settings) == "WOE"
    assert firmenkuerzel("weinstrasse_agentur", settings) == "WS"
    assert firmenkuerzel(settings.firma("wik_fh_seh")) == "WIK"
    assert firmenkuerzel("wik_leh") == "WIK" and firmenkuerzel("WOE") == "WOE" and firmenkuerzel("") == ""
    assert firmenkuerzel("unbekannt_firma", settings) == "UNBEKANNT"
    assert firma_fuer("WS", settings).id == "weinstrasse_agentur"
    assert firma_fuer("wik", settings).id == "wik_fh_seh"  # erstes WIK in der Reihenfolge der Konfiguration
    assert firma_fuer(settings.firma("woe_leh"), settings).id == "woe_leh"
    with pytest.raises(KeyError, match="Unbekannte Firma"):
        firma_fuer("xyz", settings)


def test_bestellung_und_lieferschein_wik(settings: Settings):
    assert bestellung_pdf("Beispielgut", "wik_leh", "4711", DATUM, settings=settings) == f"Beispielgut{T}WIK{T}4711{T}Cde{T}24.10.2026.pdf"
    assert bestellung_pdf("Beispielgut", "WOE", "4711", DATUM, settings=settings, revision=True) == f"Beispielgut{T}WOE{T}4711{T}Neu{T}24.10.2026.pdf"
    assert bestellung_pdf("Beispielgut", "wik_leh", None, DATUM, settings=settings) == f"Beispielgut{T}WIK{T}ohne-Nr{T}Cde{T}24.10.2026.pdf"
    assert lieferschein_pdf("Beispielgut", "wik_leh", "4711", DATUM, settings=settings) == f"Beispielgut{T}WIK{T}4711{T}Ls{T}24.10.2026.pdf"
    assert lieferschein_pdf("Beispielgut", "wik_leh", "4711", DATUM, settings=settings, endung="") == f"Beispielgut{T}WIK{T}4711{T}Ls{T}24.10.2026"


def test_bestellung_und_auftragskopie_ws(settings: Settings):
    ws = settings.firma("weinstrasse_agentur")
    assert bestellung_pdf("Beispielgut", ws, "4711", DATUM, settings=settings, kunde="Musterhandel") == f"Beispielgut{T}Musterhandel{T}Cde{T}24.10.2026.pdf"
    assert bestellung_pdf("Beispielgut", "WS", "4711", DATUM, settings=settings, kunde="SPAR Beispielmarkt") == f"Beispielgut{T}SPAR Beispielmarkt{T}4711{T}Cde{T}24.10.2026.pdf"
    assert auftragskopie_pdf("Beispielgut", "Musterhandel", "4711", DATUM, firma=ws, settings=settings) == f"Beispielgut{T}Musterhandel{T}Kopie{T}24.10.2026.pdf"
    assert auftragskopie_pdf("Beispielgut", "Musterhandel", "4711", DATUM, firma="wik_leh", settings=settings, revision=True) == f"Beispielgut{T}WIK{T}4711{T}Neu Kopie{T}24.10.2026.pdf"


def test_speditionsauftrag(settings: Settings):
    assert speditionsauftrag_pdf("Hillebrand", "Beispielgut", "wik_leh", "4711", DATUM, settings=settings, kunde="Musterhandel") == f"Hillebrand{T}Speditionsauftrag{T}Beispielgut{T}4711{T}24.10.2026.pdf"
    assert speditionsauftrag_pdf("Hillebrand", "Beispielgut", "weinstrasse_agentur", "4711", DATUM, settings=settings, kunde="Musterhandel") == f"Hillebrand{T}Speditionsauftrag{T}Beispielgut{T}Musterhandel{T}24.10.2026.pdf"
    assert speditionsauftrag_pdf(None, "Beispielgut", "wik_leh", None, DATUM, settings=settings, kunde="Musterhandel") == f"Unbekannt{T}Speditionsauftrag{T}Beispielgut{T}Musterhandel{T}24.10.2026.pdf"


def test_anhangsname_entspricht_titulierung(settings: Settings):
    roh = RohBestellung(eingangstyp=Eingangstyp.KUNDENBESTELLUNG_PDF, quelldatei=Path("x.pdf"), extraktionsmethode=Extraktionsmethode.PDFPLUMBER, bestellnummer="4711")
    paket = Belegpaket(
        roh=roh,
        firma_id="wik_leh",
        kunde=KundeZuordnung(kunde=Kunde(firma_id="wik_leh", kundennummer="1", name="Weinhandel Muster", matchcode="Musterhandel"), status=MatchStatus.SICHER),
        lieferant=LieferantZuordnung(lieferant=Lieferant(firma_id="wik_leh", lieferantennummer="9", name="Weingut Beispiel", matchcode="Beispielgut"), status=MatchStatus.SICHER),
        belegfelder=Belegfelder(spedition="Hillebrand"),
    )
    for kuerzel in ("Cde", "Kopie", "Ls", "Speditionsauftrag"):
        assert anhangsname(kuerzel, paket, settings, datum=DATUM) == titulierung.name_fuer_paket(kuerzel, paket, settings, datum=DATUM)
    assert anhangsname("Kopie", paket, settings, datum=DATUM) == f"Beispielgut{T}WIK{T}4711{T}Kopie{T}24.10.2026.pdf"


def test_bereinige_und_verbotene_woerter(settings: Settings):
    # Folgen verbotener Zeichen ('":') werden zu einem Ersatzzeichen, Umlaute bleiben
    assert bereinige('Weingut Müller / "Süd":  Test?', settings) == "Weingut Müller - -Süd- Test-"
    assert bereinige("a/b") == "a-b"  # ohne settings: Standardwerte
    assert enthaelt_verbotene_woerter("Auftragsbestaetigung 4711", settings) == ["Auftragsbestätigung"]
    assert enthaelt_verbotene_woerter("Bitte BESTÄTIGEN", settings) == ["bestätigen"]
    assert enthaelt_verbotene_woerter("Auftragskopie zur Information", settings) == []
    assert enthaelt_verbotene_woerter(None, settings) == []
