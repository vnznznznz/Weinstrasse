"""Tests für lexware.ui_automation mit dem FakeLexwareBackend (kein Windows, kein pywinauto)."""

from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from auftragsautomat.config import Settings
from auftragsautomat.lexware.fake_backend import FakeArtikel, FakeBeleg, FakeDialog, FakeLexwareBackend
from auftragsautomat.lexware.ui_automation import (
    AssistentSchritt,
    ControlSpec,
    LexwareBackend,
    LexwareControls,
    NichtAufWindows,
    PywinautoBackend,
    UiSchreiber,
    erkenne_schritt,
    formatiere_menge,
    lade_controls,
    parse_betrag,
)
from auftragsautomat.models import (
    Artikel,
    Belegart,
    Belegfelder,
    Belegpaket,
    Eingangstyp,
    Extraktionsmethode,
    Kunde,
    KundeZuordnung,
    Lieferant,
    LieferantZuordnung,
    MatchStatus,
    PositionsZuordnung,
    RohBestellung,
    RohPosition,
)

FIRMA_ID = "wik_leh"
FIRMENNAME = "Wein-Import-Kreis LEH"
HEUTE = date(2026, 10, 18)

GLORIA = Artikel(firma_id=FIRMA_ID, artikelnummer="1643", bezeichnung="Villa Armellina Gloria Secco Frizzante 0,75 l",
                 lieferantennummer="72", ek_preis=Decimal("4.20"), vk_preis_1=Decimal("6.90"))
ROSATO = Artikel(firma_id=FIRMA_ID, artikelnummer="957", bezeichnung="Villa Armellina Rosato Secco Frizzante 0,75 l",
                 lieferantennummer="72", ek_preis=Decimal("4.20"), vk_preis_1=Decimal("6.90"))
NEPTUN = Artikel(firma_id=FIRMA_ID, artikelnummer="8888", bezeichnung="DAA Neptun", ek_preis=Decimal("0"), vk_preis_1=Decimal("0"))
CIDE = Lieferant(firma_id=FIRMA_ID, lieferantennummer="72", name="Cide srl", name2="Villa Armellina")
HEGMANN = Kunde(firma_id=FIRMA_ID, kundennummer="3441", name="Hegmann", name2="Weinmarkt Bottrop-Kirchhellen")


def _position(laufnr: int, artikel: Artikel, menge: str, *, status: MatchStatus = MatchStatus.SICHER) -> PositionsZuordnung:
    return PositionsZuordnung(
        roh=RohPosition(laufnr=laufnr, bezeichnung=artikel.bezeichnung, menge=Decimal(menge), preis=artikel.vk_preis_1),
        artikel=artikel,
        status=status,
        menge=Decimal(menge),
        ek_preis=artikel.ek_preis,
        vk_preis=artikel.vk_preis_1,
        preis_dokument=artikel.vk_preis_1,
    )


def _paket(*, freigegeben: bool = False, mit_uebersprungen: bool = True) -> Belegpaket:
    positionen = [_position(1, GLORIA, "12"), _position(2, ROSATO, "6")]
    if mit_uebersprungen:
        positionen.append(_position(3, NEPTUN, "1", status=MatchStatus.UEBERSPRUNGEN))
    return Belegpaket(
        roh=RohBestellung(
            eingangstyp=Eingangstyp.KUNDENBESTELLUNG_PDF,
            quelldatei=Path("kundenbestellung_kirchhellen.pdf"),
            extraktionsmethode=Extraktionsmethode.PDFPLUMBER,
            kunde_text="Weinmarkt Kirchhellen",
            bestellnummer="B-4712",
            liefertag=date(2026, 11, 5),
            lieferadresse="Weinmarkt Kirchhellen, Hauptstraße 1, 46244 Bottrop",
        ),
        firma_id=FIRMA_ID,
        kunde=KundeZuordnung(kunde=HEGMANN, status=MatchStatus.SICHER, quelle="alias"),
        lieferant=LieferantZuordnung(lieferant=CIDE, status=MatchStatus.SICHER, quelle="alias"),
        positionen=positionen,
        belegfelder=Belegfelder(spedition="Spedition Weinlogistik", weingut="Cide srl", shipment="05.11.2026", kunde="Hegmann"),
        freigegeben=freigegeben,
    )


def _fake(**kw) -> FakeLexwareBackend:
    kw.setdefault("firmenname", FIRMENNAME)
    kw.setdefault("artikel", [
        FakeArtikel("1643", GLORIA.bezeichnung, Decimal("4.20"), Decimal("6.90")),
        FakeArtikel("957", ROSATO.bezeichnung, Decimal("4.20"), Decimal("6.90")),
    ])
    kw.setdefault("partner", {"72": "Cide srl", "3441": "Hegmann"})
    kw.setdefault("heute", lambda: HEUTE)
    return FakeLexwareBackend(**kw)


def _schreiber(settings: Settings, fake: FakeLexwareBackend, **kw) -> UiSchreiber:
    kw.setdefault("heute", lambda: HEUTE)
    return UiSchreiber(settings, settings.firma(FIRMA_ID), fake, **kw)


def _index(aktionen: list[str], eintrag: str, *, ab: int = 0) -> int:
    for i in range(ab, len(aktionen)):
        if aktionen[i] == eintrag:
            return i
    raise AssertionError(f"{eintrag!r} nicht in Aktionen ab {ab}: {aktionen[ab:]}")


# ---------------------------------------------------------------------------
# Einkauf-Bestellung
# ---------------------------------------------------------------------------


def test_fake_backend_erfuellt_protokoll():
    assert isinstance(_fake(), LexwareBackend)


def test_einkauf_dry_run_kompletter_ablauf(settings: Settings):
    fake = _fake()
    ergebnis = _schreiber(settings, fake).schreibe_einkauf_bestellung(_paket(), dry_run=True)

    assert ergebnis.erfolgreich, ergebnis.fehler
    assert ergebnis.dry_run is True
    assert ergebnis.belegnummer is None
    assert ergebnis.belegart is Belegart.BESTELLUNG
    assert ergebnis.methode == "ui_automation"
    assert ergebnis.netto == Decimal("75.60")  # 12 * 4,20 + 6 * 4,20
    assert ergebnis.brutto == Decimal("89.96")
    a = fake.aktionen
    # Reihenfolge: Assistent per Strg+M, Auftragsart, Lieferant, Lieferdatum, Freifelder, dann Weiter
    i_m = _index(a, "tasten:^m")
    i_art = _index(a, "waehle:AUFTRAGSART:Bestellung", ab=i_m)
    i_lief = _index(a, "tippe:LIEFERANTENNUMMER:7", ab=i_art)
    i_datum = _index(a, "tippe:LIEFERDATUM:0", ab=i_lief)
    i_kunde = _index(a, "setze:FREIFELD_KUNDE:Hegmann", ab=i_datum)
    i_weiter1 = _index(a, "klicke:WEITER", ab=i_kunde)
    assert "setze:FREIFELD_SPEDITION:Spedition Weinlogistik" in a[i_art:i_weiter1]
    assert "setze:FREIFELD_WEINGUT:Cide srl" in a[i_art:i_weiter1]
    assert "setze:FREIFELD_SHIPMENT:05.11.2026" in a[i_art:i_weiter1]
    # Positionen: Artikelnummer und Menge einzeln getippt, grüner Haken
    i_pos1 = _index(a, "tippe:ARTIKELNUMMER:1", ab=i_weiter1)
    assert a[i_pos1:i_pos1 + 4] == ["tippe:ARTIKELNUMMER:1", "tippe:ARTIKELNUMMER:6", "tippe:ARTIKELNUMMER:4", "tippe:ARTIKELNUMMER:3"]
    i_menge1 = _index(a, "tippe:MENGE:1", ab=i_pos1)
    assert a[i_menge1 - 1] == "klicke:MENGE"
    assert a[i_menge1:i_menge1 + 2] == ["tippe:MENGE:1", "tippe:MENGE:2"]
    i_haken1 = _index(a, "klicke:POSITION_UEBERNEHMEN", ab=i_menge1)
    i_menge2 = _index(a, "tippe:MENGE:6", ab=i_haken1)
    i_haken2 = _index(a, "klicke:POSITION_UEBERNEHMEN", ab=i_menge2)
    assert "waehle:POSITIONSART:Stammartikel" in a[i_weiter1:i_pos1]
    assert "waehle:SUCHKRITERIUM:Nummer" in a[i_weiter1:i_pos1]
    # Übersprungene Position wird nicht erfasst
    assert not any(x.startswith("tippe:ARTIKELNUMMER:8") for x in a)
    # Weiter nur zweimal (Kopf -> Positionen, Positionen -> Summen), kein Doppelklick
    assert fake.anzahl("klicke:WEITER") == 2
    i_weiter2 = _index(a, "klicke:WEITER", ab=i_haken2)
    # Dry Run: Abbrechen statt Speichern, Rückfrage mit Ja bestätigt
    i_abbr = _index(a, "klicke:ABBRECHEN", ab=i_weiter2)
    assert _index(a, "klicke:DIALOG_JA", ab=i_abbr) > i_abbr
    assert "klicke:SPEICHERN" not in a
    assert not any(x.startswith("gespeichert:") for x in a)
    assert fake.gespeicherte_belege == []
    assert fake.assistent is not None and fake.assistent.geschlossen
    assert fake.dialoge == []
    assert any("Summenvergleich bestanden" in w for w in ergebnis.warnungen)
    assert not any("Preisabweichung" in w or "weicht von Stamm" in w for w in ergebnis.warnungen)


def test_weiter_verzoegert_wird_nach_timeout_erneut_geklickt(settings: Settings):
    fake = _fake(weiter_erster_klick_ignorieren=True)
    ergebnis = _schreiber(settings, fake).schreibe_einkauf_bestellung(_paket(), dry_run=True)
    assert ergebnis.erfolgreich, ergebnis.fehler
    assert fake.anzahl("klicke:WEITER") == 3
    assert any("keine Wirkung (Versuch 1)" in w for w in ergebnis.warnungen)


def test_weiter_mit_verzoegertem_seitenwechsel_nur_einmal_geklickt(settings: Settings):
    fake = _fake(weiter_verzoegerung_abfragen=2, max_abfragen=6)
    ergebnis = _schreiber(settings, fake).schreibe_einkauf_bestellung(_paket(), dry_run=True)
    assert ergebnis.erfolgreich, ergebnis.fehler
    assert fake.anzahl("klicke:WEITER") == 2


def test_summenabweichung_bricht_ab_ohne_speichern(settings: Settings):
    fake = _fake(artikel=[
        FakeArtikel("1643", GLORIA.bezeichnung, Decimal("4.50"), Decimal("6.90")),  # EK im Stamm weicht ab
        FakeArtikel("957", ROSATO.bezeichnung, Decimal("4.20"), Decimal("6.90")),
    ])
    ergebnis = _schreiber(settings, fake).schreibe_einkauf_bestellung(_paket(freigegeben=True), dry_run=False)
    assert ergebnis.erfolgreich is False
    assert "SummenAbweichung" in (ergebnis.fehler or "")
    assert "79,20" in ergebnis.fehler and "75,60" in ergebnis.fehler
    assert "klicke:SPEICHERN" not in fake.aktionen
    assert fake.gespeicherte_belege == []
    # Preisabweichung wurde als Warnung gemeldet, nicht überschrieben
    assert any("weicht von Stamm" in w and "1643" in w for w in ergebnis.warnungen)
    # Assistent wurde sauber abgebrochen, Screenshot liegt vor
    assert "klicke:ABBRECHEN" in fake.aktionen
    assert fake.assistent is not None and fake.assistent.geschlossen
    assert fake.screenshots and fake.screenshots[0].exists()
    assert any(w.startswith("Screenshot:") for w in ergebnis.warnungen)


def test_falsche_aktive_firma_bricht_ab(settings: Settings):
    fake = _fake(firmenname="Weinstrasse Agentur", firmen=["Weinstrasse Agentur", FIRMENNAME])
    ergebnis = _schreiber(settings, fake).schreibe_einkauf_bestellung(_paket(), dry_run=True)
    assert ergebnis.erfolgreich is False
    assert "FalscheFirma" in ergebnis.fehler
    assert FIRMENNAME in ergebnis.fehler and "Weinstrasse Agentur" in ergebnis.fehler
    assert "tasten:^m" not in fake.aktionen
    assert not any(x.startswith("menue:") for x in fake.aktionen)


def test_firmenwechsel_wenn_erlaubt(settings: Settings):
    fake = _fake(firmenname="Weinstrasse Agentur", firmen=["Weinstrasse Agentur", FIRMENNAME])
    schreiber = _schreiber(settings, fake, firmenwechsel_erlaubt=True)
    ergebnis = schreiber.schreibe_einkauf_bestellung(_paket(), dry_run=True)
    assert ergebnis.erfolgreich, ergebnis.fehler
    assert fake.firmenname == FIRMENNAME
    assert "menue:Datei -> Wechseln" in fake.aktionen
    assert _index(fake.aktionen, "firma:" + FIRMENNAME) < _index(fake.aktionen, "tasten:^m")


def test_scharfer_lauf_speichert_und_beantwortet_dialoge(settings: Settings):
    fake = _fake(dialoge_nach_speichern=[
        FakeDialog("Bestellung drucken", "Möchten Sie die Bestellung jetzt ausdrucken oder per E-Mail versenden?",
                   buttons=("DIALOG_DRUCKEN", "DIALOG_EMAIL", "DIALOG_UEBERSPRINGEN")),
        FakeDialog("USt-IdNr. Prüfung", "Soll die USt-IdNr. des Lieferanten jetzt geprüft werden?",
                   buttons=("DIALOG_JA", "DIALOG_NEIN")),
    ])
    ergebnis = _schreiber(settings, fake).schreibe_einkauf_bestellung(_paket(freigegeben=True), dry_run=False)
    assert ergebnis.erfolgreich, ergebnis.fehler
    assert ergebnis.dry_run is False
    assert ergebnis.belegnummer == "BE-2026-0001"
    assert ergebnis.netto == Decimal("75.60")
    a = fake.aktionen
    i_sp = _index(a, "klicke:SPEICHERN")
    assert "klicke:ABBRECHEN" not in a
    assert _index(a, "klicke:DIALOG_UEBERSPRINGEN", ab=i_sp) > i_sp
    assert _index(a, "klicke:DIALOG_NEIN", ab=i_sp) > i_sp
    assert "klicke:DIALOG_JA" not in a
    assert "klicke:DIALOG_DRUCKEN" not in a and "klicke:DIALOG_EMAIL" not in a
    assert fake.dialoge == []
    assert len(fake.gespeicherte_belege) == 1
    beleg = fake.gespeicherte_belege[0]
    assert beleg.auftragsart == "Bestellung"
    assert beleg.partnernummer == "72"
    assert beleg.positionen == [("1643", Decimal("12"), Decimal("4.20")), ("957", Decimal("6"), Decimal("4.20"))]
    assert beleg.felder["FREIFELD_KUNDE"] == "Hegmann"
    assert beleg.felder["LIEFERDATUM"] == "05.11.2026"
    assert beleg.felder["BESTELLNUMMER"] == "B-4712"
    assert any("Belegnummer gelesen: BE-2026-0001" in w for w in ergebnis.warnungen)


def test_scharf_ohne_freigabe_wird_verweigert(settings: Settings):
    fake = _fake()
    ergebnis = _schreiber(settings, fake).schreibe_einkauf_bestellung(_paket(freigegeben=False), dry_run=False)
    assert ergebnis.erfolgreich is False
    assert "Freigabe" in ergebnis.fehler
    assert fake.aktionen == [] or not any(x.startswith("tasten:") for x in fake.aktionen)


def test_blockiertes_paket_wird_verweigert(settings: Settings):
    from auftragsautomat.models import Pruefcode, Pruefhinweis, Schweregrad

    paket = _paket()
    paket.hinweise.append(Pruefhinweis(code=Pruefcode.DUBLETTE, schweregrad=Schweregrad.FEHLER, text="Dublette"))
    fake = _fake()
    ergebnis = _schreiber(settings, fake).schreibe_einkauf_bestellung(paket, dry_run=True)
    assert ergebnis.erfolgreich is False
    assert "blockiert" in ergebnis.fehler
    assert "tasten:^m" not in fake.aktionen


def test_unbekannter_dialog_fuehrt_zu_fehler_und_screenshot(settings: Settings):
    fake = _fake(dialoge_bei_schritt={"Positionen": [
        FakeDialog("Hinweis", "Der verfügbare Lagerbestand reicht nicht aus. Trotzdem fortfahren?", buttons=("DIALOG_JA", "DIALOG_NEIN")),
    ]})
    ergebnis = _schreiber(settings, fake).schreibe_einkauf_bestellung(_paket(), dry_run=True)
    assert ergebnis.erfolgreich is False
    assert "UnbekannterDialog" in ergebnis.fehler
    assert "Lagerbestand" in ergebnis.fehler
    assert "klicke:DIALOG_JA" not in fake.aktionen and "klicke:DIALOG_NEIN" not in fake.aktionen
    assert "klicke:SPEICHERN" not in fake.aktionen
    assert fake.screenshots, "Screenshot fehlt"
    assert fake.screenshots[0].exists()
    assert fake.screenshots[0].parent == settings.lexware.screenshots_ordner
    assert any(str(fake.screenshots[0]) in w for w in ergebnis.warnungen)
    assert any("manuell prüfen" in w for w in ergebnis.warnungen)


def test_menge_wird_bei_fehlender_uebernahme_gemeldet(settings: Settings):
    fake = _fake(menge_nur_einzeln=True)
    # Das Fake-Mengenfeld ignoriert set_text; der Schreiber tippt einzeln -> funktioniert
    ergebnis = _schreiber(settings, fake).schreibe_einkauf_bestellung(_paket(), dry_run=True)
    assert ergebnis.erfolgreich, ergebnis.fehler
    assert not any(x.startswith("setze:MENGE") for x in fake.aktionen)


def test_fehlendes_freifeld_ist_im_dry_run_nur_warnung(settings: Settings):
    fake = _fake(felder_fehlend={"FREIFELD_SHIPMENT"})
    ergebnis = _schreiber(settings, fake).schreibe_einkauf_bestellung(_paket(), dry_run=True)
    assert ergebnis.erfolgreich, ergebnis.fehler
    assert any("Shipment" in w and "nicht gefunden" in w for w in ergebnis.warnungen)


def test_leeres_belegfeld_scharf_verweigert(settings: Settings):
    paket = _paket(freigegeben=True)
    paket.belegfelder.spedition = None
    settings.firma(FIRMA_ID).belegfelder_standard.clear()
    fake = _fake()
    ergebnis = _schreiber(settings, fake).schreibe_einkauf_bestellung(paket, dry_run=False)
    assert ergebnis.erfolgreich is False
    assert "Spedition" in ergebnis.fehler
    assert "tasten:^m" not in fake.aktionen


def test_unbekannter_artikel_bricht_ab(settings: Settings):
    fake = _fake(artikel=[FakeArtikel("1643", GLORIA.bezeichnung, Decimal("4.20"), Decimal("6.90"))])
    ergebnis = _schreiber(settings, fake).schreibe_einkauf_bestellung(_paket(), dry_run=True)
    assert ergebnis.erfolgreich is False
    assert "957" in ergebnis.fehler
    assert "klicke:SPEICHERN" not in fake.aktionen
    assert fake.assistent is not None and fake.assistent.geschlossen


# ---------------------------------------------------------------------------
# Verkauf-AB und Lieferschein
# ---------------------------------------------------------------------------


def test_verkauf_ab_dry_run_ablauf(settings: Settings):
    fake = _fake()
    ergebnis = _schreiber(settings, fake).schreibe_verkauf_ab(_paket(), dry_run=True)
    assert ergebnis.erfolgreich, ergebnis.fehler
    assert ergebnis.belegart is Belegart.AUFTRAGSBESTAETIGUNG
    assert ergebnis.netto == Decimal("124.20")  # 18 * 6,90 (VK Preisgruppe 1)
    assert ergebnis.belegnummer is None
    a = fake.aktionen
    i_n = _index(a, "tasten:^n")
    assert "tasten:^m" not in a
    i_art = _index(a, "waehle:AUFTRAGSART:Auftragsbestätigung", ab=i_n)
    i_kd = _index(a, "tippe:KUNDENNUMMER:3", ab=i_art)
    i_weiter = _index(a, "klicke:WEITER", ab=i_kd)
    assert "setze:BESTELLNUMMER:B-4712" in a[i_kd:i_weiter]
    assert "tippe:LIEFERDATUM:0" in a[i_kd:i_weiter]
    assert any(x.startswith("setze:LIEFERADRESSE:") for x in a[i_kd:i_weiter])
    assert "setze:FREIFELD_KUNDE:Hegmann" in a[i_kd:i_weiter]
    assert fake.anzahl("klicke:WEITER") == 2
    assert "klicke:ABBRECHEN" in a and "klicke:SPEICHERN" not in a


def test_verkauf_ab_scharf_liefert_belegnummer(settings: Settings):
    fake = _fake(dialoge_nach_speichern=[
        FakeDialog("Auftragsbestätigung drucken", "Auftragsbestätigung jetzt drucken oder per E-Mail versenden?",
                   buttons=("DIALOG_DRUCKEN", "DIALOG_UEBERSPRINGEN")),
    ])
    ergebnis = _schreiber(settings, fake).schreibe_verkauf_ab(_paket(freigegeben=True), dry_run=False)
    assert ergebnis.erfolgreich, ergebnis.fehler
    assert ergebnis.belegnummer == "AB-2026-0001"
    assert fake.gespeicherte_belege[0].partnernummer == "3441"
    assert "klicke:DIALOG_UEBERSPRINGEN" in fake.aktionen


def test_verkauf_summenabweichung_bricht_ab(settings: Settings):
    fake = _fake(artikel=[
        FakeArtikel("1643", GLORIA.bezeichnung, Decimal("4.20"), Decimal("7.10")),
        FakeArtikel("957", ROSATO.bezeichnung, Decimal("4.20"), Decimal("6.90")),
    ])
    ergebnis = _schreiber(settings, fake).schreibe_verkauf_ab(_paket(freigegeben=True), dry_run=False)
    assert ergebnis.erfolgreich is False
    assert "SummenAbweichung" in ergebnis.fehler
    assert fake.gespeicherte_belege == []


def _ab_beleg() -> FakeBeleg:
    return FakeBeleg(
        belegnummer="AB-2026-0101", auftragsart="Auftragsbestätigung", partnernummer="3441",
        datum=date(2026, 9, 15), netto=Decimal("124.20"),
        positionen=[("1643", Decimal("12"), Decimal("6.90")), ("957", Decimal("6"), Decimal("6.90"))],
        felder={"BESTELLNUMMER": "B-4712", "FREIFELD_KUNDE": "Hegmann"},
    )


def test_lieferschein_dry_run_ablauf(settings: Settings):
    fake = _fake(belege=[_ab_beleg()])
    paket = _paket()
    paket.lieferschein_erstellen = True
    ergebnis = _schreiber(settings, fake).auftrag_weiterfuehren_lieferschein(paket, "AB-2026-0101", dry_run=True)
    assert ergebnis.erfolgreich, ergebnis.fehler
    assert ergebnis.belegart is Belegart.LIEFERSCHEIN
    assert ergebnis.belegnummer is None
    assert ergebnis.netto == Decimal("124.20")
    a = fake.aktionen
    i_liste = _index(a, "menue:Verkauf -> Aufträge")
    i_mark = _index(a, "markiere:AUFTRAGSLISTE_VERKAUF:AB-2026-0101", ab=i_liste)
    i_wf = _index(a, "menue:Bearbeiten -> Aufträge -> Weiterführen", ab=i_mark)
    i_art = _index(a, "waehle:AUFTRAGSART:Lieferschein", ab=i_wf)
    assert fake.anzahl("klicke:WEITER") == 2
    assert _index(a, "klicke:ABBRECHEN", ab=i_art) > i_art
    assert "klicke:SPEICHERN" not in a
    assert not any(x.startswith("tippe:ARTIKELNUMMER") for x in a)  # Positionen kommen aus der AB
    assert fake.belege[0].status == ""  # AB unverändert


def test_lieferschein_scharf_speichert_und_markiert_ab(settings: Settings):
    fake = _fake(belege=[_ab_beleg()])
    paket = _paket(freigegeben=True)
    paket.lieferschein_erstellen = True
    ergebnis = _schreiber(settings, fake).auftrag_weiterfuehren_lieferschein(paket, "AB-2026-0101", dry_run=False)
    assert ergebnis.erfolgreich, ergebnis.fehler
    assert ergebnis.belegnummer == "LS-2026-0001"
    assert fake.belege[0].status == "W"
    assert fake.gespeicherte_belege[0].auftragsart == "Lieferschein"
    assert fake.gespeicherte_belege[0].positionen == [("1643", Decimal("12"), Decimal("6.90")), ("957", Decimal("6"), Decimal("6.90"))]


def test_lieferschein_ohne_freigabe_scharf_verweigert(settings: Settings):
    fake = _fake(belege=[_ab_beleg()])
    ergebnis = _schreiber(settings, fake).auftrag_weiterfuehren_lieferschein(_paket(), "AB-2026-0101", dry_run=False)
    assert ergebnis.erfolgreich is False
    assert "Freigabe" in ergebnis.fehler
    assert fake.gespeicherte_belege == []


def test_lieferschein_ab_nicht_gefunden(settings: Settings):
    fake = _fake(belege=[_ab_beleg()])
    ergebnis = _schreiber(settings, fake).auftrag_weiterfuehren_lieferschein(_paket(), "AB-2026-0999", dry_run=True)
    assert ergebnis.erfolgreich is False
    assert "AB-2026-0999" in ergebnis.fehler
    assert not any(x.startswith("menue:Bearbeiten") for x in fake.aktionen)


def test_lieferschein_bereits_weitergefuehrt(settings: Settings):
    ab = _ab_beleg()
    ab.status = "W"
    fake = _fake(belege=[ab])
    ergebnis = _schreiber(settings, fake).auftrag_weiterfuehren_lieferschein(_paket(), "AB-2026-0101", dry_run=True)
    assert ergebnis.erfolgreich is False
    assert "bereits weitergeführt" in ergebnis.fehler


# ---------------------------------------------------------------------------
# Backend / Controls / Hilfsfunktionen
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="nur außerhalb von Windows")
def test_pywinauto_backend_auf_linux_wirft_nicht_auf_windows(settings: Settings):
    with pytest.raises(NichtAufWindows):
        PywinautoBackend(settings)
    with pytest.raises(NichtAufWindows):
        UiSchreiber(settings, settings.firma(FIRMA_ID))


def test_lade_controls_mit_yaml_override(tmp_path: Path):
    datei = tmp_path / "lexware_controls.yaml"
    datei.write_text(
        "controls:\n"
        "  WEITER: {auto_id: \"btnWeiter\", control_type: \"Button\"}\n"
        "  MENGE: \"Anzahl\"\n"
        "  PROZESSNAME: \"LxFramework.exe\"\n"
        "  MENUE_AUFTRAG_WEITERFUEHREN: [\"Bearbeiten\", \"Weiterführen\"]\n"
        "  SEITENLISTE: {control_type: \"Tree\", found_index: 1}\n"
        "  UNBEKANNT: \"x\"\n",
        encoding="utf-8",
    )
    c = lade_controls(datei)
    assert c.WEITER == ControlSpec(auto_id="btnWeiter", control_type="Button")
    assert c.MENGE == ControlSpec(title="Anzahl")
    assert c.PROZESSNAME == "LxFramework.exe"
    assert c.MENUE_AUFTRAG_WEITERFUEHREN == ("Bearbeiten", "Weiterführen")
    assert c.SEITENLISTE == ControlSpec(control_type="Tree", found_index=1)
    assert c.SPEICHERN == LexwareControls().SPEICHERN  # unverändert
    assert c.spec("WEITER").pywinauto_kwargs() == {"auto_id": "btnWeiter", "control_type": "Button"}


def test_lade_controls_fehlende_datei_liefert_defaults(tmp_path: Path):
    assert lade_controls(tmp_path / "gibt_es_nicht.yaml") == LexwareControls()
    assert lade_controls(None) == LexwareControls()


def test_lade_controls_ungueltiger_schluessel(tmp_path: Path):
    datei = tmp_path / "c.yaml"
    datei.write_text("WEITER: {titel: \"Weiter\"}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unbekannte Schlüssel"):
        lade_controls(datei)


def test_standard_controls_datei_entspricht_defaults():
    datei = Path(__file__).resolve().parents[1] / "config" / "lexware_controls.yaml"
    assert datei.exists()
    assert lade_controls(datei) == LexwareControls()


def test_controls_spec_unbekannt():
    from auftragsautomat.lexware.ui_automation import ControlNichtGefunden

    with pytest.raises(ControlNichtGefunden):
        LexwareControls().spec("PROZESSNAME")
    assert LexwareControls().titel("WEITER") == "Weiter"


@pytest.mark.parametrize(
    "text, erwartet",
    [
        ("Lieferant", AssistentSchritt.KOPF),
        ("1. Kunde", AssistentSchritt.KOPF),
        ("Positionen", AssistentSchritt.POSITIONEN),
        ("2 - Positionen", AssistentSchritt.POSITIONEN),
        ("Summen", AssistentSchritt.SUMMEN),
        ("3. Summe", AssistentSchritt.SUMMEN),
        ("", AssistentSchritt.UNBEKANNT),
        (None, AssistentSchritt.UNBEKANNT),
        ("Info", AssistentSchritt.UNBEKANNT),
    ],
)
def test_erkenne_schritt(text, erwartet):
    assert erkenne_schritt(text) is erwartet


@pytest.mark.parametrize(
    "text, erwartet",
    [
        ("1.234,50 €", Decimal("1234.50")),
        ("4,20", Decimal("4.20")),
        ("75,60 EUR", Decimal("75.60")),
        ("-12,00", Decimal("-12.00")),
        ("1234.50", Decimal("1234.50")),
        ("12", Decimal("12")),
        ("1.234", Decimal("1234")),
        ("", None),
        (None, None),
        ("abc", None),
    ],
)
def test_parse_betrag(text, erwartet):
    assert parse_betrag(text) == erwartet


@pytest.mark.parametrize("menge, erwartet", [(Decimal("12"), "12"), (Decimal("12.0"), "12"), (Decimal("0.5"), "0,5"), (Decimal("3420"), "3420")])
def test_formatiere_menge(menge, erwartet):
    assert formatiere_menge(menge) == erwartet
