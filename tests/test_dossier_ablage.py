"""Tests für dossier/ablage.py: Ordnername, Zielordner-Vorschlag, Dateityp aus dem Namen.

Alle Namen und Nummern sind synthetisch ("Weingut Beispiel", "Musterhandel", "4711").
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest

from auftragsautomat.config import Settings
from auftragsautomat.dossier.ablage import (
    OHNE_NUMMER,
    dateityp_aus_name,
    dossier_name,
    stichtag,
    zielordner_vorschlag,
)
from auftragsautomat.models import (
    Belegart,
    Belegergebnis,
    Belegpaket,
    BuchungsErgebnis,
    Eingangstyp,
    Extraktionsmethode,
    Kunde,
    KundeZuordnung,
    Lieferant,
    LieferantZuordnung,
    MatchStatus,
    RohBestellung,
)

ZEITPUNKT = datetime(2026, 10, 24, 9, 0)


def _paket(
    firma_id: str | None = "wik_fh_seh",
    *,
    kunde_kurz: str | None = "Musterhandel",
    lieferant_kurz: str | None = "Beispielgut",
    bestellnummer: str | None = "4711",
    belegnummer: str | None = None,
    datum: date | None = date(2026, 10, 20),
) -> Belegpaket:
    roh = RohBestellung(
        eingangstyp=Eingangstyp.KUNDENBESTELLUNG_PDF,
        quelldatei=Path("eingang/bestellung.pdf"),
        extraktionsmethode=Extraktionsmethode.PDFPLUMBER,
        kunde_text="Weinhandel Muster GmbH",
        lieferant_text="Weingut Beispiel srl",
        bestellnummer=bestellnummer,
        belegnummer=belegnummer,
        datum=datum,
        liefertag=date(2026, 11, 6),
    )
    paket = Belegpaket(roh=roh, firma_id=firma_id)
    if kunde_kurz is not None:
        kunde = Kunde(firma_id=firma_id or "x", kundennummer="10001", name="Weinhandel Muster GmbH", matchcode=kunde_kurz)
        paket.kunde = KundeZuordnung(kunde=kunde, status=MatchStatus.SICHER, quelle="alias")
    if lieferant_kurz is not None:
        lieferant = Lieferant(firma_id=firma_id or "x", lieferantennummer="90001", name="Weingut Beispiel srl", matchcode=lieferant_kurz)
        paket.lieferant = LieferantZuordnung(lieferant=lieferant, status=MatchStatus.SICHER, quelle="alias")
    return paket


def _ergebnis(paket: Belegpaket, *, einkauf: str | None = "BE-1001", angebot: str | None = None, verkauf: str | None = "AB-2002") -> BuchungsErgebnis:
    def beleg(art: Belegart, nummer: str | None) -> Belegergebnis | None:
        if nummer is None:
            return None
        return Belegergebnis(belegart=art, belegnummer=nummer, erfolgreich=True, dry_run=False, methode="ui_automation")

    return BuchungsErgebnis(
        firma_id=paket.firma_id or "wik_fh_seh",
        quelldatei=paket.roh.quelldatei,
        dry_run=False,
        zeitpunkt=ZEITPUNKT,
        angebot=beleg(Belegart.ANGEBOT, angebot),
        einkauf=beleg(Belegart.BESTELLUNG, einkauf),
        verkauf=beleg(Belegart.AUFTRAGSBESTAETIGUNG, verkauf),
    )


# ---------------------------------------------------------------------------
# dossier_name
# ---------------------------------------------------------------------------


def test_dossier_name_wik(settings: Settings):
    paket = _paket()
    assert dossier_name(paket, _ergebnis(paket), settings=settings) == "Beispielgut-WIK-Musterhandel-4711-24.10.2026"


def test_dossier_name_ws(settings: Settings):
    paket = _paket("weinstrasse_agentur")
    assert dossier_name(paket, _ergebnis(paket), settings=settings) == "Beispielgut-WS-Musterhandel-4711-24.10.2026"


def test_dossier_name_ohne_settings_leitet_firma_aus_id_ab():
    paket = _paket("woe_leh")
    assert dossier_name(paket, _ergebnis(paket)) == "Beispielgut-WOE-Musterhandel-4711-24.10.2026"


def test_dossier_name_ohne_ergebnis_nutzt_belegdatum(settings: Settings):
    paket = _paket()
    assert dossier_name(paket, None, settings=settings) == "Beispielgut-WIK-Musterhandel-4711-20.10.2026"


def test_dossier_name_nummer_aus_lexware_oder_platzhalter(settings: Settings):
    paket = _paket(bestellnummer=None)
    assert dossier_name(paket, _ergebnis(paket), settings=settings) == "Beispielgut-WIK-Musterhandel-BE-1001-24.10.2026"
    assert dossier_name(paket, _ergebnis(paket, einkauf=None, angebot="AN-7"), settings=settings).split("-")[3:5] == ["AN", "7"]
    assert dossier_name(paket, None, settings=settings) == f"Beispielgut-WIK-Musterhandel-{OHNE_NUMMER}-20.10.2026"
    paket = _paket(bestellnummer=None, belegnummer="B-0815")
    assert "B-0815" in dossier_name(paket, None, settings=settings)


def test_dossier_name_bereinigt_verbotene_zeichen_und_bindestriche(settings: Settings):
    paket = _paket(kunde_kurz='Muster/Handel: "Nord"', lieferant_kurz="Bottrop-Kirchhellen")
    name = dossier_name(paket, _ergebnis(paket), settings=settings)
    for zeichen in '/\\:*?"<>|':
        assert zeichen not in name
    assert name.startswith("Bottrop Kirchhellen-WIK-")
    assert name.count("-") == 4


def test_dossier_name_ohne_stammdaten_nutzt_dokumenttext(settings: Settings):
    paket = _paket(kunde_kurz=None, lieferant_kurz=None)
    assert dossier_name(paket, None, settings=settings).startswith("Weingut Beispiel srl-WIK-Weinhandel Muster GmbH-")


def test_dossier_name_ohne_firma(settings: Settings):
    paket = _paket(None)
    assert dossier_name(paket, None, settings=settings) == "Beispielgut-Unbekannt-Musterhandel-4711-20.10.2026"


def test_stichtag_reihenfolge():
    paket = _paket()
    assert stichtag(paket, _ergebnis(paket)) == date(2026, 10, 24)
    assert stichtag(paket, None) == date(2026, 10, 20)
    assert stichtag(paket, None, datum=date(2026, 1, 1)) == date(2026, 1, 1)
    assert stichtag(_paket(datum=None), None) == date.today()


# ---------------------------------------------------------------------------
# zielordner_vorschlag
# ---------------------------------------------------------------------------


def test_zielordner_mit_provision_fuer_agentur(settings: Settings):
    paket = _paket("weinstrasse_agentur")
    assert zielordner_vorschlag(paket, settings) == "Aufträge mit Provision"


def test_zielordner_ohne_provision_sonst(settings: Settings):
    assert zielordner_vorschlag(_paket("wik_fh_seh"), settings) == "Aufträge ohne Provision"
    assert zielordner_vorschlag(_paket("wik_leh"), settings) == "Aufträge ohne Provision"


def test_zielordner_folgt_der_ablage_konfiguration_und_jahr(settings: Settings):
    """Regression (Review Befund 6): Der Vorschlag im Deckblatt folgt AblageKonfig (firmen_mit_provision,
    Ordnernamen, struktur) – nicht mehr den eigenen Feldern im Abschnitt dossier."""
    from auftragsautomat.ablage import zielordner

    settings.ablage.firmen_mit_provision = ["weinstrasse_agentur", "wik_fh_seh"]
    settings.ablage.ordner_mit_provision = "Ablage A"
    settings.dossier.firmen_mit_provision = []  # veraltet, ohne Wirkung
    paket = _paket("wik_fh_seh", datum=None)
    assert zielordner_vorschlag(paket, settings) == "Ablage A" == zielordner(paket, settings)
    settings.ablage.struktur = "{provision}/{kunde}/{jahr}"
    # ohne Belegdatum zählt der Liefertag (06.11.2026)
    assert zielordner_vorschlag(paket, settings) == "Ablage A/Musterhandel/2026"
    assert zielordner_vorschlag(paket, settings, datum=date(2027, 2, 1)) == "Ablage A/Musterhandel/2027"
    settings.ablage.struktur = "{provision}/{gibtsnicht}"
    assert zielordner_vorschlag(paket, settings) == "Ablage A"


# ---------------------------------------------------------------------------
# dateityp_aus_name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "typ"),
    [
        ("00_Deckblatt.pdf", "deckblatt"),
        ("01_Kundenbestellung_Beispielgut – WIK – 4711 – Kd – 24.10.2026.pdf", "kundenbestellung"),
        ("02_Bestellung_Beispielgut – WIK – 4711 – Cde – 24.10.2026.pdf", "bestellung"),
        ("02_Bestellung_Beispielgut – WIK – 4711 – Neu – 24.10.2026.pdf", "revision"),
        ("03_Auftragskopie_Beispielgut – Musterhandel – Kopie – 24.10.2026.pdf", "auftragskopie"),
        ("03_Auftragskopie_Beispielgut – Musterhandel – Neu Kopie – 24.10.2026.pdf", "revision"),
        ("04_Speditionsauftrag_Spedition Muster – Speditionsauftrag – Beispielgut – 4711 – 24.10.2026.pdf", "speditionsauftrag"),
        ("Spedition Muster – Speditionsbestätigung – Beispielgut – 4711 – 24.10.2026.pdf", "speditionsbestaetigung"),
        ("05_Lieferschein_Beispielgut – WIK – 4711 – Ls – 24.10.2026.pdf", "lieferschein"),
        ("Beispielgut – WIK – 4711 – Oc – 24.10.2026.pdf", "auftragsbestaetigung_lieferant"),
        ("Beispielgut – WIK – 4711 – Rekl – 24.10.2026.pdf", "reklamation"),
        # bisherige Hausmuster
        ("Beispielgut-WIK-Musterhandel-4711-Kd-24.10.2026.pdf", "kundenbestellung"),
        ("Beispielgut-WIK-Musterhandel-4711-Cde-24.10.2026.pdf", "bestellung"),
        ("Auftragskopie-Beispielgut-Musterhandel_4711-24.10.2026.pdf", "auftragskopie"),
        ("AB-Beispielgut-Musterhandel_4711-24.10.2026.pdf", "auftragskopie"),
        ("Beispielgut-WIK-Musterhandel-4711-LS-24.10.2026.pdf", "lieferschein"),
        ("Hillebrand-Beispielgut-WIK-4711-24.10.2026.pdf", "speditionsauftrag"),
        ("Spedition_Auftrag.pdf", "speditionsauftrag"),
        ("Beispielgut-WIK-Musterhandel-4711-BestätigungWG-24.10.2026.pdf", "auftragsbestaetigung_lieferant"),
        # Technik
        ("06_Mails/20261024_090500_bestellung_weingut.eml", "mail"),
        ("nachricht.msg", "mail"),
        ("07_Fixtermin.txt", "fixtermin"),
        ("dossier.json", "index"),
        ("Dossier_komplett.pdf", "gesamt_pdf"),
        ("Rabattliste_Herbst.pdf", "rabattliste"),
        ("Angebot_4711.pdf", "angebot"),
        ("irgendwas.docx", "sonstiges"),
        ("", "sonstiges"),
    ],
)
def test_dateityp_aus_name(name: str, typ: str):
    assert dateityp_aus_name(name) == typ


def test_dateityp_kuerzel_schlaegt_hausmuster():
    # "AB" im Kundennamen darf das Kürzel "Kd" nicht überstimmen
    assert dateityp_aus_name("Beispielgut – WIK – AB-Getränke – Kd – 24.10.2026.pdf") == "kundenbestellung"
