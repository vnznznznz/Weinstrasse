"""Tests für protokoll (protokoll.csv, Bericht, Zusammenfassung, Archiv)."""

from __future__ import annotations

import csv
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from auftragsautomat import protokoll
from auftragsautomat.config import Settings
from auftragsautomat.models import (
    Artikel,
    Belegart,
    Belegergebnis,
    Belegfelder,
    Belegpaket,
    BuchungsErgebnis,
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
    Summenvermerk,
)
from auftragsautomat.protokoll import (
    PROTOKOLL_SPALTEN,
    archiviere,
    formatiere_betrag,
    formatiere_datum,
    formatiere_menge,
    protokoll_zeile,
    schreibe_protokoll,
    zusammenfassung,
)

FIRMA_ID = "wik_leh"
ZEIT = datetime(2026, 10, 18, 9, 15, 30)


def _paket(quelldatei: Path = Path("eingang/wik_leh/kundenbestellung_kirchhellen.pdf")) -> Belegpaket:
    roh = RohBestellung(
        eingangstyp=Eingangstyp.KUNDENBESTELLUNG_PDF,
        quelldatei=quelldatei,
        extraktionsmethode=Extraktionsmethode.PDFPLUMBER,
        kunde_text="Weinmarkt Kirchhellen",
        belegnummer="2026-0917",
        bestellnummer="B-4712",
        datum=date(2026, 9, 10),
        liefertag=date(2026, 9, 15),
        lieferadresse="Weinmarkt Kirchhellen Lager\nIndustriestraße 5\n46244 Bottrop",
        positionen=[
            RohPosition(laufnr=1, bezeichnung="Gloria Secco Frizzante", menge=Decimal("60"), preis=Decimal("6.50")),
            RohPosition(laufnr=2, bezeichnung="Rosato Secco Frizzante", menge=Decimal("24"), preis=Decimal("6.90")),
            RohPosition(laufnr=3, bezeichnung="DAA Neptun Werbemittel", menge=Decimal("1"), preis=Decimal("0")),
        ],
        summenvermerke=[Summenvermerk(wert=Decimal("84"), einheit="Fl", rohtext="84 Fl.")],
    )
    gloria = Artikel(firma_id=FIRMA_ID, artikelnummer="1643", bezeichnung="Villa Armellina Gloria Secco Frizzante 0,75 l",
                     ek_preis=Decimal("4.20"), vk_preis_1=Decimal("6.90"))
    rosato = Artikel(firma_id=FIRMA_ID, artikelnummer="957", bezeichnung="Villa Armellina Rosato Secco Frizzante 0,75 l",
                     ek_preis=Decimal("4.20"), vk_preis_1=Decimal("6.90"))
    return Belegpaket(
        roh=roh,
        firma_id=FIRMA_ID,
        kunde=KundeZuordnung(kunde=Kunde(firma_id=FIRMA_ID, kundennummer="3441", name="Hegmann", name2="Weinmarkt Bottrop-Kirchhellen"), status=MatchStatus.SICHER),
        lieferant=LieferantZuordnung(lieferant=Lieferant(firma_id=FIRMA_ID, lieferantennummer="72", name="Cide srl"), status=MatchStatus.SICHER),
        positionen=[
            PositionsZuordnung(roh=roh.positionen[0], artikel=gloria, status=MatchStatus.SICHER, menge=Decimal("60"),
                               ek_preis=Decimal("4.20"), vk_preis=Decimal("6.90"), preis_dokument=Decimal("6.50"),
                               hinweise=[Pruefhinweis(code=Pruefcode.PREIS_ABWEICHUNG, schweregrad=Schweregrad.WARNUNG,
                                                      text="VK 6,90 weicht vom Dokumentpreis 6,50 ab", position_laufnr=1)]),
            PositionsZuordnung(roh=roh.positionen[1], artikel=rosato, status=MatchStatus.SICHER, menge=Decimal("24"),
                               ek_preis=Decimal("4.20"), vk_preis=Decimal("6.90"), preis_dokument=Decimal("6.90")),
            PositionsZuordnung(roh=roh.positionen[2], status=MatchStatus.UEBERSPRUNGEN, menge=Decimal("0"), preis_dokument=Decimal("0"),
                               hinweise=[Pruefhinweis(code=Pruefcode.NULLPREIS_OHNE_ARTIKEL, schweregrad=Schweregrad.WARNUNG,
                                                      text="0,00 € ohne Stammartikel, übersprungen", position_laufnr=3)]),
        ],
        belegfelder=Belegfelder(spedition="Spedition Weinlogistik", weingut="Cide srl", shipment="15.09.2026", kunde="Hegmann"),
        hinweise=[Pruefhinweis(code=Pruefcode.SUMMENVERMERK, schweregrad=Schweregrad.INFO, text="Summenvermerk 84 Fl. passt")],
        freigegeben=True,
        freigegeben_von="JD",
    )


def _ergebnis(paket: Belegpaket, *, dry_run: bool = True, mit_lieferschein: bool = False, zeit: datetime = ZEIT) -> BuchungsErgebnis:
    einkauf = Belegergebnis(
        belegart=Belegart.BESTELLUNG, belegnummer=None if dry_run else "BE-2026-0001",
        netto=Decimal("352.80"), brutto=Decimal("419.83"), erfolgreich=True, dry_run=dry_run,
        methode="dry_run" if dry_run else "ui_automation",
        warnungen=["Artikel 1643 x 60 @ 4,20", "Ablauf: Strg+M gedrückt"],
    )
    verkauf = Belegergebnis(
        belegart=Belegart.AUFTRAGSBESTAETIGUNG, belegnummer=None if dry_run else "AB-2026-0001",
        netto=Decimal("579.60"), brutto=Decimal("689.72"), erfolgreich=True, dry_run=dry_run,
        methode="opentrans" if dry_run else "ui_automation",
        warnungen=["Belegnummer wird erst beim Import in Lexware vergeben"],
        artefakt=Path("logs/opentrans/20260910_3441_B-4712_abcdef12.xml") if dry_run else None,
    )
    lieferschein = None
    if mit_lieferschein:
        lieferschein = Belegergebnis(belegart=Belegart.LIEFERSCHEIN, belegnummer="LS-2026-0001", netto=Decimal("579.60"),
                                     erfolgreich=True, dry_run=False, methode="ui_automation")
    return BuchungsErgebnis(
        firma_id=FIRMA_ID, quelldatei=paket.roh.quelldatei, dry_run=dry_run, zeitpunkt=zeit,
        einkauf=einkauf, verkauf=verkauf, lieferschein=lieferschein,
        warnungen=["Hinweis auf Belegebene\nmit Zeilenumbruch"],
    )


def _zeilen(pfad: Path) -> list[dict[str, str]]:
    with pfad.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f, delimiter=";"))


# ---------------------------------------------------------------------------
# Formatierung
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("wert", "erwartet"),
    [
        (Decimal("1234.5"), "1.234,50"),
        (Decimal("0"), "0,00"),
        (Decimal("-0.4"), "-0,40"),
        (Decimal("1234567.895"), "1.234.567,90"),
        (Decimal("6.9"), "6,90"),
        (None, ""),
    ],
)
def test_formatiere_betrag(wert, erwartet):
    assert formatiere_betrag(wert) == erwartet


@pytest.mark.parametrize(
    ("wert", "erwartet"),
    [
        (Decimal("60"), "60"),
        (Decimal("6E+1"), "60"),
        (Decimal("0.5"), "0,5"),
        (Decimal("3420"), "3.420"),
        (Decimal("1234.25"), "1.234,25"),
        (Decimal("-3"), "-3"),
        (None, ""),
    ],
)
def test_formatiere_menge(wert, erwartet):
    assert formatiere_menge(wert) == erwartet


def test_formatiere_datum():
    assert formatiere_datum(date(2026, 9, 15)) == "15.09.2026"
    assert formatiere_datum(ZEIT, mit_zeit=True) == "18.10.2026 09:15:30"
    assert formatiere_datum(ZEIT) == "18.10.2026"
    assert formatiere_datum(None) == "-"


# ---------------------------------------------------------------------------
# protokoll.csv
# ---------------------------------------------------------------------------


def test_schreibe_protokoll_kopf_bom_und_anhaengen(settings: Settings):
    paket = _paket()
    pfad = schreibe_protokoll(paket, _ergebnis(paket), settings.log_ordner)
    assert pfad == settings.log_ordner / "protokoll.csv"
    roh = pfad.read_bytes()
    assert roh.startswith(b"\xef\xbb\xbf"), "UTF-8 mit BOM für Excel"
    assert roh.count(b"\xef\xbb\xbf") == 1
    kopf = roh.decode("utf-8-sig").splitlines()[0]
    assert kopf == ";".join(PROTOKOLL_SPALTEN)

    schreibe_protokoll(paket, _ergebnis(paket, dry_run=False, mit_lieferschein=True, zeit=datetime(2026, 10, 18, 9, 16)), settings.log_ordner)
    text = pfad.read_bytes()
    assert text.count(b"\xef\xbb\xbf") == 1, "BOM nur einmal am Anfang"
    assert text.count(b"zeitpunkt;firma;") == 1, "Kopfzeile nur einmal"
    zeilen = _zeilen(pfad)
    assert len(zeilen) == 2
    assert zeilen[0]["dry_run"] == "ja" and zeilen[1]["dry_run"] == "nein"
    assert zeilen[1]["beleg_einkauf"] == "BE-2026-0001"
    assert zeilen[1]["beleg_verkauf"] == "AB-2026-0001"
    assert zeilen[1]["beleg_lieferschein"] == "LS-2026-0001"
    assert zeilen[0]["beleg_lieferschein"] == ""
    assert zeilen[0]["beleg_verkauf"] == "(simuliert)"


def test_protokoll_zeile_inhalt():
    paket = _paket()
    zeile = protokoll_zeile(paket, _ergebnis(paket))
    assert list(zeile) == list(PROTOKOLL_SPALTEN)
    assert zeile["zeitpunkt"] == "18.10.2026 09:15:30"
    assert zeile["firma"] == FIRMA_ID
    assert zeile["quelldatei"].endswith("kundenbestellung_kirchhellen.pdf")
    assert zeile["kunde_nr"] == "3441" and zeile["kunde_name"] == "Hegmann"
    assert zeile["lieferant_nr"] == "72" and zeile["lieferant_name"] == "Cide srl"
    assert zeile["bestellnummer"] == "B-4712"
    assert zeile["netto_ek"] == "352,80" and zeile["brutto_ek"] == "419,83"
    assert zeile["netto_vk"] == "579,60" and zeile["brutto_vk"] == "689,72"
    assert zeile["positionen"] == "2" and zeile["uebersprungen"] == "1"
    assert zeile["erfolgreich"] == "ja"
    warnungen = zeile["warnungen"].split(" | ")
    assert "Hinweis auf Belegebene / mit Zeilenumbruch" in warnungen
    assert "Einkauf-Bestellung: Artikel 1643 x 60 @ 4,20" in warnungen
    assert "Verkauf-AB: Belegnummer wird erst beim Import in Lexware vergeben" in warnungen
    assert "Prüfung [PREIS_ABWEICHUNG] Pos. 1: VK 6,90 weicht vom Dokumentpreis 6,50 ab" in warnungen
    assert not any(w.startswith("Einkauf-Bestellung: Ablauf:") for w in warnungen), "Ablaufprotokoll nicht in die CSV"
    assert not any("SUMMENVERMERK" in w for w in warnungen), "INFO-Hinweise nicht in die CSV"
    assert "\n" not in zeile["warnungen"]


def test_protokoll_zeile_fehlgeschlagen_und_ohne_partner():
    paket = _paket()
    paket.kunde = KundeZuordnung()
    paket.lieferant = LieferantZuordnung()
    ergebnis = BuchungsErgebnis(
        firma_id=FIRMA_ID, quelldatei=paket.roh.quelldatei, dry_run=False, zeitpunkt=ZEIT,
        einkauf=Belegergebnis(belegart=Belegart.BESTELLUNG, erfolgreich=False, dry_run=False, methode="ui_automation", fehler="Summenabweichung"),
        warnungen=["Einkauf fehlgeschlagen"],
    )
    zeile = protokoll_zeile(paket, ergebnis)
    assert zeile["kunde_nr"] == "" and zeile["kunde_name"] == "Weinmarkt Kirchhellen"
    assert zeile["lieferant_nr"] == "" and zeile["lieferant_name"] == ""
    assert zeile["beleg_einkauf"] == "FEHLER" and zeile["beleg_verkauf"] == ""
    assert zeile["netto_ek"] == "352,80", "ohne Netto im Ergebnis zählt die Paketsumme"
    assert zeile["netto_vk"] == ""
    assert zeile["erfolgreich"] == "nein"
    assert "Einkauf-Bestellung FEHLER: Summenabweichung" in zeile["warnungen"]


def test_protokoll_mit_excel_trennzeichen_lesbar(settings: Settings):
    paket = _paket()
    paket.roh.lieferadresse = "Straße; mit Semikolon"
    pfad = schreibe_protokoll(paket, _ergebnis(paket), settings.log_ordner)
    zeilen = _zeilen(pfad)
    assert len(zeilen) == 1
    assert len(zeilen[0]) == len(PROTOKOLL_SPALTEN)


def test_fremde_kopfzeile_wird_toleriert(settings: Settings, caplog: pytest.LogCaptureFixture):
    pfad = settings.log_ordner / "protokoll.csv"
    pfad.write_text("﻿alt;format\r\n1;2\r\n", encoding="utf-8")
    paket = _paket()
    with caplog.at_level("WARNING", logger="auftragsautomat.protokoll"):
        schreibe_protokoll(paket, _ergebnis(paket), settings.log_ordner)
    assert "Kopfzeile" in caplog.text
    assert len(pfad.read_text(encoding="utf-8-sig").splitlines()) == 3


# ---------------------------------------------------------------------------
# Bericht-Datei
# ---------------------------------------------------------------------------


def test_bericht_datei_mit_zeitstempel_und_suffix(settings: Settings):
    paket = _paket()
    schreibe_protokoll(paket, _ergebnis(paket), settings.log_ordner)
    berichte = settings.log_ordner / "berichte"
    erster = berichte / "kundenbestellung_kirchhellen_20261018-091530.txt"
    assert erster.exists()
    inhalt = erster.read_text(encoding="utf-8")
    assert inhalt == zusammenfassung(paket, _ergebnis(paket))

    schreibe_protokoll(paket, _ergebnis(paket), settings.log_ordner)
    assert (berichte / "kundenbestellung_kirchhellen_20261018-091530_2.txt").exists()


def test_bericht_dateiname_wird_bereinigt(settings: Settings):
    paket = _paket(Path("eingang/wik_leh/Bestellung Müller & Co (Kopie).pdf"))
    schreibe_protokoll(paket, _ergebnis(paket), settings.log_ordner)
    namen = [p.name for p in (settings.log_ordner / "berichte").iterdir()]
    assert len(namen) == 1
    assert namen[0].endswith("_20261018-091530.txt")
    assert " " not in namen[0] and "&" not in namen[0] and "(" not in namen[0]


def test_schreibe_protokoll_legt_ordner_an(settings: Settings):
    ordner = settings.log_ordner / "neu" / "tiefer"
    paket = _paket()
    pfad = schreibe_protokoll(paket, _ergebnis(paket), ordner)
    assert pfad.exists()
    assert any((ordner / "berichte").iterdir())


# ---------------------------------------------------------------------------
# Zusammenfassung
# ---------------------------------------------------------------------------


def test_zusammenfassung_dry_run():
    paket = _paket()
    text = zusammenfassung(paket, _ergebnis(paket))
    assert "Buchungsbericht Auftrags-Automat – 18.10.2026 09:15:30" in text
    assert "DRY RUN" in text
    assert "wik_leh" in text
    assert "3441 Hegmann (Weinmarkt Bottrop-Kirchhellen)" in text
    assert "72 Cide srl" in text
    assert "Bestellnummer:     B-4712" in text
    assert "Liefertag:         15.09.2026" in text
    assert "Lieferadresse:     Weinmarkt Kirchhellen Lager / Industriestraße 5 / 46244 Bottrop" in text
    assert "Spedition=Spedition Weinlogistik; Weingut=Cide srl; Shipment=15.09.2026; Kunde=Hegmann" in text
    # Positionstabelle
    assert "Pos | Artikelnr | Bezeichnung" in text
    zeile_1 = next(z for z in text.splitlines() if z.lstrip().startswith("1 | 1643 "))
    assert "60" in zeile_1 and "4,20" in zeile_1 and "6,90" in zeile_1 and "6,50" in zeile_1
    assert "!! VK +0,40 zum Dokument" in zeile_1
    zeile_3 = next(z for z in text.splitlines() if "DAA Neptun" in z)
    assert "übersprungen" in zeile_3
    assert "Gesamtmenge: 84 in 2 aktiven Positionen, 1 übersprungen" in text
    assert "Summenvermerk im Dokument: '84 Fl.'" in text
    assert "1 Position(en) mit Preisabweichung" in text
    # Summen und Belege
    assert "352,80" in text and "419,83" in text
    assert "579,60" in text and "689,72" in text
    assert "Einkauf-Bestellung:  OK (Dry Run, nicht gespeichert)" in text
    assert "Verkauf-AB:          OK (Dry Run, nicht gespeichert)" in text
    assert "Datei: logs/opentrans/20260910_3441_B-4712_abcdef12.xml" in text
    assert "Lieferschein:        nicht angefordert" in text
    # Prüfhinweise nach Schweregrad
    assert "WARNUNG (2)" in text and "INFO (1)" in text and "FEHLER" not in text.split("Prüfhinweise")[1].split("Hinweise der Buchung")[0]
    assert "[PREIS_ABWEICHUNG] Pos. 1: VK 6,90 weicht vom Dokumentpreis 6,50 ab" in text
    assert "[SUMMENVERMERK] Summenvermerk 84 Fl. passt" in text
    # Hinweise der Buchung inkl. Ablaufprotokoll
    assert "- Hinweis auf Belegebene / mit Zeilenumbruch" in text
    assert "- Einkauf-Bestellung: Ablauf: Strg+M gedrückt" in text
    assert "Ergebnis: ERFOLGREICH" in text
    assert text.rstrip().endswith("die Quelldatei bleibt im Eingang.")
    assert "Freigabe:          ja (JD)" in text


def test_zusammenfassung_scharf_mit_fehler():
    paket = _paket()
    paket.einkauf_erforderlich = False
    paket.lieferschein_erstellen = True
    ergebnis = BuchungsErgebnis(
        firma_id=FIRMA_ID, quelldatei=paket.roh.quelldatei, dry_run=False, zeitpunkt=ZEIT,
        verkauf=Belegergebnis(belegart=Belegart.AUFTRAGSBESTAETIGUNG, erfolgreich=False, dry_run=False,
                              methode="ui_automation", fehler="SummenAbweichung: Lexware 500,00 != 579,60", netto=Decimal("579.60")),
        warnungen=["Lieferschein nicht erstellt: Verkauf-AB wurde nicht (erfolgreich) angelegt"],
    )
    text = zusammenfassung(paket, ergebnis)
    assert "Modus:             SCHARF" in text
    assert "Einkauf-Bestellung:  nicht erforderlich (Firma ohne Einkauf-Bestellung)" in text
    assert "Verkauf-AB:          FEHLGESCHLAGEN" in text
    assert "Fehler: SummenAbweichung: Lexware 500,00 != 579,60" in text
    assert "Lieferschein:        nicht angelegt" in text
    assert "Ergebnis: FEHLGESCHLAGEN / NICHT GEBUCHT" in text
    assert "SCHARF: Belege wurden" in text
    assert "DRY RUN" not in text


def test_zusammenfassung_ohne_positionen_und_partner():
    paket = _paket()
    paket.positionen = []
    paket.roh.positionen = []
    paket.roh.summenvermerke = []
    paket.kunde = KundeZuordnung()
    paket.lieferant = LieferantZuordnung()
    paket.hinweise = [Pruefhinweis(code=Pruefcode.KEIN_KUNDE, schweregrad=Schweregrad.FEHLER, text="Kein Kunde zugeordnet")]
    ergebnis = BuchungsErgebnis(firma_id=FIRMA_ID, quelldatei=paket.roh.quelldatei, dry_run=True, zeitpunkt=ZEIT,
                                warnungen=["Buchung verweigert: nichts geschrieben"])
    text = zusammenfassung(paket, ergebnis)
    assert "(keine Positionen)" in text
    assert "Kunde:             - (Dokument: 'Weinmarkt Kirchhellen')" in text
    assert "Lieferant:         - (Dokument: '-')" in text
    assert "FEHLER (1)" in text and "[KEIN_KUNDE] Kein Kunde zugeordnet" in text
    assert "Ergebnis: FEHLGESCHLAGEN / NICHT GEBUCHT" in text


def test_zusammenfassung_markiert_unsichere_position():
    paket = _paket()
    paket.positionen[1].artikel = None
    paket.positionen[1].status = MatchStatus.UNSICHER
    text = zusammenfassung(paket, _ergebnis(paket))
    zeile = next(z for z in text.splitlines() if z.lstrip().startswith("2 | ?"))
    assert "Rosato Secco Frizzante" in zeile
    assert "? nicht zugeordnet" in zeile


# ---------------------------------------------------------------------------
# Archiv
# ---------------------------------------------------------------------------


def test_archiviere_verschiebt_in_firma_und_jahr(settings: Settings):
    quelle = settings.eingang_ordner / "wik_leh" / "bestellung.pdf"
    quelle.parent.mkdir(parents=True)
    quelle.write_bytes(b"%PDF")
    ziel = archiviere(quelle, settings, FIRMA_ID, jahr=2026)
    assert ziel == settings.archiv_ordner / FIRMA_ID / "2026" / "bestellung.pdf"
    assert ziel.read_bytes() == b"%PDF"
    assert not quelle.exists()


def test_archiviere_standardjahr_ist_aktuelles_jahr(settings: Settings):
    quelle = settings.eingang_ordner / "x.pdf"
    quelle.write_bytes(b"1")
    ziel = archiviere(quelle, settings, FIRMA_ID)
    assert ziel.parent.name == f"{date.today().year}"


def test_archiviere_ueberschreibt_nie(settings: Settings):
    quelle = settings.eingang_ordner / "bestellung.pdf"
    quelle.write_bytes(b"neu")
    vorhanden = settings.archiv_ordner / FIRMA_ID / "2026" / "bestellung.pdf"
    vorhanden.parent.mkdir(parents=True)
    vorhanden.write_bytes(b"alt")
    ziel = archiviere(quelle, settings, FIRMA_ID, jahr=2026)
    assert ziel.name == "bestellung_2.pdf"
    assert vorhanden.read_bytes() == b"alt"
    assert ziel.read_bytes() == b"neu"
    quelle.write_bytes(b"noch neuer")
    assert archiviere(quelle, settings, FIRMA_ID, jahr=2026).name == "bestellung_3.pdf"


def test_archiviere_fehlende_datei(settings: Settings):
    with pytest.raises(FileNotFoundError):
        archiviere(settings.eingang_ordner / "fehlt.pdf", settings, FIRMA_ID)


def test_archiviere_bereinigt_firma_im_pfad(settings: Settings):
    quelle = settings.eingang_ordner / "a.pdf"
    quelle.write_bytes(b"1")
    ziel = archiviere(quelle, settings, "../böse firma", jahr=2026)
    assert ziel.is_relative_to(settings.archiv_ordner)
    assert ziel.parent.parent.name == "b_se_firma"


def test_archiviere_datei_bereits_im_archiv(settings: Settings):
    ziel_ordner = settings.archiv_ordner / FIRMA_ID / "2026"
    ziel_ordner.mkdir(parents=True)
    datei = ziel_ordner / "schon_da.pdf"
    datei.write_bytes(b"1")
    assert archiviere(datei, settings, FIRMA_ID, jahr=2026) == datei
    assert datei.exists()


def test_freier_pfad_faellt_auf_zeitstempel_zurueck(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(protokoll, "_MAX_SUFFIX_VERSUCHE", 2)
    basis = tmp_path / "datei.txt"
    for name in ("datei.txt", "datei_2.txt"):
        (tmp_path / name).write_text("x")
    frei = protokoll._freier_pfad(basis)
    assert not frei.exists()
    assert frei.name.startswith("datei_") and frei.suffix == ".txt"
