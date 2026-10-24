"""Tests für termine.register (Fixtermin-Register auf der Fixterminüberwachung).

Grundlage ist das synthetische Fixture ``tests/fixtures/excel/fixterminueberwachung_beispiel.xlsx``
(erzeugt durch ``erzeuge_fixtermin_beispiel.py``): KW 20–23/2026, Kopfzeile in Zeile 4, fünf Slots je
Werktag, Feiertage als einzelne Zeile. Vorbelegt: 12.05. (Zeilen 10–11), 13.05. (Zeile 15, abgeholt),
20.05. (Zeilen 36–40, voll), 02.06. (Zeile 77). Alle Namen und Nummern sind synthetisch.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

import pytest

from auftragsautomat.config import DateiRefKonfig, Settings
from auftragsautomat.m365.basis import datum_zu_serienzahl
from auftragsautomat.m365.fake import FakeExcelBackend, Schreibzugriff
from auftragsautomat.models import (
    Belegart,
    Belegergebnis,
    Belegfelder,
    Belegpaket,
    BuchungsErgebnis,
    Eingangstyp,
    Extraktionsmethode,
    Fixtermin,
    Kunde,
    KundeZuordnung,
    Lieferant,
    LieferantZuordnung,
    MatchStatus,
    RohBestellung,
)
from auftragsautomat.termine import register as register_modul
from auftragsautomat.termine.kurznamen import Kurznamen
from auftragsautomat.termine.register import (
    FixterminRegister,
    RegisterFehler,
    RegisterVoll,
    bemerkung_teile,
    bemerkung_zusammenfuegen,
    fixtermin_aus_paket,
)

FIXTURE = Path(__file__).parent / "fixtures" / "excel" / "fixterminueberwachung_beispiel.xlsx"
BLATT = "Fixterminüberwachung"
DI = date(2026, 5, 12)  # Zeilen 10-14, zwei belegt
MI = date(2026, 5, 13)  # Zeilen 15-19, eine belegt (abgeholt)
HIMMELFAHRT = date(2026, 5, 14)  # Zeile 20 (Feiertag)
FR = date(2026, 5, 15)  # Zeilen 21-25, frei
VOLL = date(2026, 5, 20)  # Zeilen 36-40, alle belegt
SA = date(2026, 5, 16)


@pytest.fixture()
def excel() -> FakeExcelBackend:
    return FakeExcelBackend(aus_xlsx=FIXTURE, jetzt=lambda: datetime(2026, 5, 6, 9, 0))


@pytest.fixture()
def ref() -> DateiRefKonfig:
    return DateiRefKonfig(lokaler_pfad=FIXTURE, blatt=BLATT)


@pytest.fixture()
def register(excel: FakeExcelBackend, ref: DateiRefKonfig) -> FixterminRegister:
    return FixterminRegister(excel, ref)


def _fixtermin(lieferant: str = "Beispielgut", kunde: str = "Neukunde", liefertag: date = DI, **felder) -> Fixtermin:
    return Fixtermin(lieferant_kurz=lieferant, kunde_kurz=kunde, liefertag=liefertag, **felder)


def _zellen(zugriff: Schreibzugriff) -> dict[str, object]:
    return {zelle: wert for zelle, wert, _ in zugriff.zellen}


# ---------------------------------------------------------------- Laden


def test_laden_struktur(register: FixterminRegister, excel: FakeExcelBackend):
    register.lade()
    assert register.geladen and register.kopfzeile == 4 and register.blatt == BLATT
    assert register.spalten == {
        "kw": 1, "datum": 2, "tag": 3, "lieferant": 4, "kunde": 5, "auftragsdatum": 6, "verladedatum": 7,
        "lieferant_bestaetigt": 8, "spedition_bestaetigt": 9, "abgeholt": 10, "bemerkung": 11,
    }
    assert register.zeitraum() == (date(2026, 5, 11), date(2026, 6, 5))
    assert register.zeilen_fuer_datum(date(2026, 5, 11)) == [5, 6, 7, 8, 9]  # Formeltexte "=B9" aufgelöst
    assert register.zeilen_fuer_datum(DI) == [10, 11, 12, 13, 14]
    assert register.zeilen_fuer_datum(HIMMELFAHRT) == [20]  # Feiertag: eine Zeile
    assert register.zeilen_fuer_datum(FR) == [21, 22, 23, 24, 25]
    assert register.zeilen_fuer_datum(SA) == [] and register.zeilen_fuer_datum(date(2026, 7, 1)) == []
    assert register.zeilen_fuer_datum(datetime(2026, 5, 12, 8, 0)) == [10, 11, 12, 13, 14]
    assert register.datum_der_zeile(9) == date(2026, 5, 11) and register.datum_der_zeile(4) is None
    assert register.freie_slots(DI) == [12, 13, 14]
    assert register.freie_slots(HIMMELFAHRT) == [] and register.freie_slots(VOLL) == []
    assert excel.schreibprotokoll == []  # Laden schreibt nichts
    # Nur einmal gelesen: ein zweites lade() ohne erneut=True greift nicht auf das Backend zu
    excel.mappen.clear()
    register.lade()
    assert register.zeilen_fuer_datum(DI) == [10, 11, 12, 13, 14]


def test_eintraege_werte_und_typen(register: FixterminRegister):
    eintraege = register.eintraege()
    assert [e.zeile for e in eintraege] == [10, 11, 15, 36, 37, 38, 39, 40, 77]
    erster = eintraege[0]
    assert erster.lieferant_kurz == "Beispielgut" and erster.kunde_kurz == "Musterhandel"
    assert erster.liefertag == DI and erster.auftragsdatum == date(2026, 5, 4) and erster.verladedatum == date(2026, 5, 11)
    assert type(erster.auftragsdatum) is date  # openpyxl liefert datetime -> date
    assert (erster.lieferant_bestaetigt, erster.spedition_bestaetigt, erster.abgeholt) == (True, False, False)
    assert (erster.bestellnummer, erster.ab_nummer, erster.spedition) == ("20260001", "30260001", "Spedition Beispiel")
    assert erster.quelle == f"{BLATT}!10"
    zweiter = eintraege[1]
    assert zweiter.auftragsdatum == date(2026, 5, 5) and zweiter.verladedatum is None and zweiter.bemerkung is None
    assert zweiter.bestellnummer is None and zweiter.spedition is None
    abgeholt = eintraege[2]
    assert abgeholt.abgeholt is True and abgeholt.tracking_referenz == "DEMB00000"
    assert register.eintraege(ab=date(2026, 5, 20), bis=date(2026, 5, 20)) == eintraege[3:8]
    assert [e.zeile for e in register.eintraege(ab=date(2026, 6, 1))] == [77]
    assert register.eintrag(12) is None and register.eintrag(20) is None and register.eintrag(10) == erster


# ---------------------------------------------------------------- Eintragen


def test_trage_ein_erster_freier_slot(register: FixterminRegister, excel: FakeExcelBackend):
    fixtermin = _fixtermin(auftragsdatum=date(2026, 5, 6), spedition="Spedition Beispiel", bestellnummer="20260009", ab_nummer="30260009")
    ergebnis = register.trage_ein(fixtermin)
    assert ergebnis.zeile == 12 and ergebnis.quelle == f"{BLATT}!12"
    assert ergebnis.bemerkung == "Best.-Nr 20260009, AB 30260009, Spedition Beispiel"
    assert ergebnis.liefertag == DI and ergebnis.lieferant_kurz == "Beispielgut"
    assert len(excel.schreibprotokoll) == 1
    zugriff = excel.schreibprotokoll[0]
    assert (zugriff.blatt, zugriff.adresse) == (BLATT, "D12:K12")  # nur D..K
    assert _zellen(zugriff) == {
        "D12": "Beispielgut", "E12": "Neukunde", "F12": date(2026, 5, 6), "G12": "",
        "H12": False, "I12": False, "J12": False, "K12": "Best.-Nr 20260009, AB 30260009, Spedition Beispiel",
    }
    assert type(_zellen(zugriff)["F12"]) is date and type(_zellen(zugriff)["H12"]) is bool
    assert zugriff.zahlenformat is None  # Datumsformat setzt das Backend (Graph) selbst
    zeile = excel.werte(DateiRefKonfig(lokaler_pfad=FIXTURE), BLATT)[11]
    assert zeile[0:3] == ["=A14", "=B14", "=C14"]  # A–C unberührt
    assert register.freie_slots(DI) == [13, 14]
    assert register.eintrag(12) is not None and register.eintrag(12).kunde_kurz == "Neukunde"


def test_zweiter_eintrag_naechster_slot_und_sechster_register_voll(register: FixterminRegister, excel: FakeExcelBackend):
    zeilen = [register.trage_ein(_fixtermin(kunde=f"Kunde {i}")).zeile for i in range(3)]
    assert zeilen == [12, 13, 14]  # nach den zwei vorbelegten Slots
    with pytest.raises(RegisterVoll) as info:
        register.trage_ein(_fixtermin(kunde="Kunde 4"))
    assert info.value.datum == DI and info.value.zeilen == [10, 11, 12, 13, 14]
    assert "12.05.2026" in str(info.value) and "Zeilen 10–14" in str(info.value)
    assert isinstance(info.value, RegisterFehler)
    assert len(excel.schreibprotokoll) == 3  # der sechste schreibt nichts
    with pytest.raises(RegisterVoll):
        register.trage_ein(_fixtermin(liefertag=VOLL))  # Tag im Fixture bereits voll
    assert [z.adresse for z in excel.schreibprotokoll] == ["D12:K12", "D13:K13", "D14:K14"]


def test_update_statt_duplikat(register: FixterminRegister, excel: FakeExcelBackend):
    # gleicher Tag, Lieferant, Kunde (Groß-/Kleinschreibung egal) und gleiche Bestellnummer -> Zeile 10 aktualisieren
    neu = _fixtermin("beispielgut", "MUSTERHANDEL", DI, bestellnummer="20260001", verladedatum=date(2026, 5, 8), spedition_bestaetigt=True)
    ergebnis = register.trage_ein(neu)
    assert ergebnis.zeile == 10 and ergebnis.lieferant_kurz == "Beispielgut" and ergebnis.kunde_kurz == "Musterhandel"
    assert ergebnis.auftragsdatum == date(2026, 5, 4)  # bleibt aus dem Blatt
    assert ergebnis.lieferant_bestaetigt is True and ergebnis.spedition_bestaetigt is True
    assert ergebnis.ab_nummer == "30260001" and ergebnis.spedition == "Spedition Beispiel"
    assert ergebnis.bemerkung == "Best.-Nr 20260001, AB 30260001, Spedition Beispiel"  # keine Wiederholung
    assert len(excel.schreibprotokoll) == 1
    assert _zellen(excel.schreibprotokoll[0]) == {"G10": date(2026, 5, 8), "I10": True}  # nur geänderte Zellen
    assert register.freie_slots(DI) == [12, 13, 14]
    # noch einmal dasselbe: nichts zu schreiben
    register.trage_ein(neu)
    assert len(excel.schreibprotokoll) == 1
    # ohne Nummer im neuen Fixtermin -> trotzdem derselbe Eintrag (Zeile 10 ist der erste passende)
    assert register.finde(_fixtermin("Beispielgut", "Musterhandel", DI)).zeile == 10
    # andere Bestellnummer -> anderer Vorgang -> neuer Slot
    anderer = register.trage_ein(_fixtermin("Beispielgut", "Musterhandel", DI, bestellnummer="20260099"))
    assert anderer.zeile == 12
    assert register.finde(_fixtermin("Beispielgut", "Musterhandel", DI, bestellnummer="20260099")).zeile == 12
    assert register.finde(_fixtermin("Beispielgut", "Musterhandel", DI, ab_nummer="30260001")).zeile == 10
    assert register.finde(_fixtermin("Beispielgut", "Musterhandel", FR)) is None
    assert register.finde(_fixtermin("Beispielgut", "Musterhandel", DI, bestellnummer="7", ab_nummer="8")) is None


def test_update_haekchen_nie_zuruecknehmen_und_bemerkung_ergaenzen(register: FixterminRegister, excel: FakeExcelBackend):
    ergebnis = register.trage_ein(_fixtermin("Beispielgut", "Musterhandel", DI, lieferant_bestaetigt=False, tracking_referenz="DEMB00001", bemerkung="Vormittag"))
    assert ergebnis.lieferant_bestaetigt is True  # True im Blatt bleibt
    assert ergebnis.bemerkung == "Best.-Nr 20260001, AB 30260001, Spedition Beispiel | Vormittag | Ref. DEMB00001"
    assert _zellen(excel.schreibprotokoll[0]) == {"K10": ergebnis.bemerkung}


def test_trage_ein_ohne_namen(register: FixterminRegister):
    with pytest.raises(RegisterFehler, match="ohne Lieferant und Kunde"):
        register.trage_ein(_fixtermin(" ", ""))


@pytest.mark.parametrize(
    ("datum", "text"),
    [
        (HIMMELFAHRT, "gesperrt (Feiertag: Christi Himmelfahrt)"),
        (SA, "Samstag; die Fixterminüberwachung führt nur Werktage (nächster Werktag: 18.05.2026)"),
        (date(2026, 5, 17), "Sonntag"),
        (date(2026, 7, 1), "außerhalb des Zeitraums der Fixterminüberwachung (11.05.2026–05.06.2026)"),
        (date(2026, 5, 1), "außerhalb des Zeitraums"),
    ],
)
def test_tag_ohne_slot_klare_meldung(register: FixterminRegister, excel: FakeExcelBackend, datum: date, text: str):
    with pytest.raises(RegisterFehler) as info:
        register.trage_ein(_fixtermin(liefertag=datum))
    assert not isinstance(info.value, RegisterVoll)
    assert text in str(info.value) and datum.strftime("%d.%m.%Y") in str(info.value)
    assert excel.schreibprotokoll == []


def test_feiertag_ohne_zeile_im_blatt(ref: DateiRefKonfig):
    # Blatt ohne Feiertagszeile: die Meldung nennt den Feiertag aus dem Kalender
    kopf = ["KW", "Datum", "Tag", "Lieferant", "Kunde", "Auftragsdatum", "Verladedatum", "Bestätigung des Lieferanten", "Bestätigung der Spedition", "Abgeholt", "Bemerkung"]
    matrix = [[], [], [], kopf, [20, date(2026, 5, 13), "Mi"], [23, date(2026, 6, 3), "Mi"], [23, date(2026, 6, 5), "Fr"]]
    excel = FakeExcelBackend({"standard": {BLATT: matrix}})
    reg = FixterminRegister(excel, DateiRefKonfig(blatt=BLATT), slots_je_tag=1)
    with pytest.raises(RegisterFehler, match=r"Feiertag \(Christi Himmelfahrt\).*15\.05\.2026"):
        reg.trage_ein(_fixtermin(liefertag=HIMMELFAHRT))
    fronleichnam = date(2026, 6, 4)
    with pytest.raises(RegisterFehler, match=r"Feiertag \(Fronleichnam\)"):
        reg.trage_ein(_fixtermin(liefertag=fronleichnam))
    # In Berlin ist Fronleichnam ein Werktag: dann fehlt schlicht die Zeile im Blatt
    with pytest.raises(RegisterFehler, match="04.06.2026 gibt es keine Zeile"):
        FixterminRegister(excel, DateiRefKonfig(blatt=BLATT), slots_je_tag=1, bundesland="BE").trage_ein(_fixtermin(liefertag=fronleichnam))
    with pytest.raises(RegisterFehler, match="keine Tageszeilen"):
        FixterminRegister(FakeExcelBackend({"standard": {BLATT: [[], [], [], kopf]}}), DateiRefKonfig(blatt=BLATT)).trage_ein(_fixtermin())


# ---------------------------------------------------------------- Häkchen, Verladedatum


def test_bestaetigung_setzen(register: FixterminRegister, excel: FakeExcelBackend):
    eintrag = register.eintrag(11)
    assert eintrag is not None and eintrag.spedition_bestaetigt is False
    ergebnis = register.setze_bestaetigung(eintrag, "spedition")
    assert ergebnis.spedition_bestaetigt is True and ergebnis.zeile == 11
    assert excel.schreibprotokoll[-1].adresse == "D11:K11" and _zellen(excel.schreibprotokoll[-1]) == {"I11": True}
    assert register.eintrag(11).spedition_bestaetigt is True
    # über Tag/Lieferant/Kunde ohne Zeile; abgeholt; zurücknehmen
    ergebnis = register.setze_bestaetigung(_fixtermin("Musterweine", "Beispielmarkt", DI), "abgeholt")
    assert ergebnis.abgeholt is True and ergebnis.zeile == 11 and _zellen(excel.schreibprotokoll[-1]) == {"J11": True}
    ergebnis = register.setze_bestaetigung(ergebnis, "lieferant", wert=False)
    assert ergebnis.lieferant_bestaetigt is False and _zellen(excel.schreibprotokoll[-1]) == {"H11": False}
    with pytest.raises(ValueError, match="Unbekanntes Bestätigungsfeld"):
        register.setze_bestaetigung(eintrag, "kunde")  # type: ignore[arg-type]
    with pytest.raises(RegisterFehler, match="Kein Eintrag"):
        register.setze_bestaetigung(_fixtermin("Niemand", "Keiner", DI), "lieferant")


def test_bestaetigung_mit_falscher_zeile_sucht_neu(register: FixterminRegister, excel: FakeExcelBackend, caplog: pytest.LogCaptureFixture):
    falsch = _fixtermin("Beispielgut", "Musterhandel", DI, zeile=11)  # Zeile 11 gehört Musterweine/Beispielmarkt
    with caplog.at_level(logging.WARNING):
        ergebnis = register.setze_bestaetigung(falsch, "spedition")
    assert ergebnis.zeile == 10 and _zellen(excel.schreibprotokoll[-1]) == {"I10": True}
    assert "passt nicht mehr" in caplog.text


def test_setze_verladedatum(register: FixterminRegister, excel: FakeExcelBackend):
    eintrag = register.eintrag(11)
    ergebnis = register.setze_verladedatum(eintrag, datetime(2026, 5, 11, 7, 0))
    assert ergebnis.verladedatum == date(2026, 5, 11) and _zellen(excel.schreibprotokoll[-1]) == {"G11": date(2026, 5, 11)}
    ergebnis = register.setze_verladedatum(ergebnis, None)
    assert ergebnis.verladedatum is None and _zellen(excel.schreibprotokoll[-1]) == {"G11": ""}
    assert register.eintrag(11).verladedatum is None


# ---------------------------------------------------------------- Verschieben


def test_verschieben(register: FixterminRegister, excel: FakeExcelBackend):
    alt = register.eintrag(10)
    ergebnis = register.verschiebe(alt, FR, "Kunde wünscht später")
    assert ergebnis.zeile == 21 and ergebnis.liefertag == FR
    assert ergebnis.bemerkung == "verschoben von 12.05.2026: Kunde wünscht später | Best.-Nr 20260001, AB 30260001, Spedition Beispiel"
    assert ergebnis.lieferant_bestaetigt is True and ergebnis.auftragsdatum == date(2026, 5, 4)  # Werte bleiben
    assert ergebnis.bestellnummer == "20260001" and ergebnis.ab_nummer == "30260001"
    assert [z.adresse for z in excel.schreibprotokoll] == ["D10:K10", "D21:K21"]
    assert _zellen(excel.schreibprotokoll[0]) == {"D10": "", "E10": "", "F10": "", "G10": "", "H10": False, "I10": False, "J10": False, "K10": ""}
    neu = _zellen(excel.schreibprotokoll[1])
    assert neu["D21"] == "Beispielgut" and neu["F21"] == date(2026, 5, 4) and neu["G21"] == date(2026, 5, 11) and neu["H21"] is True
    assert register.eintrag(10) is None and register.freie_slots(DI) == [10, 12, 13, 14]
    assert [e.zeile for e in register.eintraege(ab=FR, bis=FR)] == [21]
    # erneut eintragen findet den verschobenen Eintrag am neuen Tag
    assert register.finde(_fixtermin("Beispielgut", "Musterhandel", FR, bestellnummer="20260001")).zeile == 21
    # ein zweites Verschieben derselben Zeile hängt den Vermerk vorne an
    weiter = register.verschiebe(ergebnis, date(2026, 5, 18), "Lieferant verspätet")
    assert weiter.bemerkung.startswith("verschoben von 15.05.2026: Lieferant verspätet | verschoben von 12.05.2026")


def test_verschieben_auf_vollen_tag_laesst_alte_zeile_unberuehrt(register: FixterminRegister, excel: FakeExcelBackend):
    alt = register.eintrag(10)
    with pytest.raises(RegisterVoll):
        register.verschiebe(alt, VOLL, "Test")
    with pytest.raises(RegisterFehler, match="Samstag"):
        register.verschiebe(alt, SA, "Test")
    with pytest.raises(RegisterFehler, match="bereits am 12.05.2026"):
        register.verschiebe(alt, DI, "Test")
    with pytest.raises(RegisterFehler, match="Kein Eintrag"):
        register.verschiebe(_fixtermin("Niemand", "Keiner", DI), FR, "Test")
    assert excel.schreibprotokoll == [] and register.eintrag(10) == alt


def test_verschieben_mit_sitzung(ref: DateiRefKonfig):
    """Mehrere Schreibzugriffe laufen in einer Excel-Sitzung, wenn das Backend eine anbietet."""

    class MitSitzung(FakeExcelBackend):
        def __init__(self) -> None:
            super().__init__(aus_xlsx=FIXTURE)
            self.sitzungen = 0
            self.offen = False
            self.in_sitzung: list[str] = []

        @contextmanager
        def sitzung(self, datei: DateiRefKonfig) -> Iterator[None]:
            self.sitzungen += 1
            self.offen = True
            try:
                yield
            finally:
                self.offen = False

        def schreibe_bereich(self, datei, blatt, adresse, werte, *, zahlenformat=None):
            self.in_sitzung.append(adresse if self.offen else f"ohne Sitzung: {adresse}")
            super().schreibe_bereich(datei, blatt, adresse, werte, zahlenformat=zahlenformat)

    excel = MitSitzung()
    reg = FixterminRegister(excel, ref)
    reg.verschiebe(reg.eintrag(10), FR, "Test")
    reg.trage_ein(_fixtermin(kunde="Neukunde"))
    reg.setze_bestaetigung(_fixtermin(kunde="Neukunde"), "lieferant")
    assert excel.sitzungen == 3 and excel.in_sitzung == ["D10:K10", "D21:K21", "D10:K10", "D10:K10"]


# ---------------------------------------------------------------- Offene


def test_offene(register: FixterminRegister):
    offene = register.offene(date(2026, 5, 31))
    assert [e.zeile for e in offene] == [10, 11, 36, 37, 38, 39, 40]  # Zeile 15 abgeholt, Zeile 77 nach dem 31.05.
    assert [e.zeile for e in register.offene(date(2026, 5, 31), ab=date(2026, 5, 20))] == [36, 37, 38, 39, 40]
    assert [e.zeile for e in register.offene()] == [10, 11, 36, 37, 38, 39, 40, 77]
    register.setze_bestaetigung(register.eintrag(11), "abgeholt")
    assert [e.zeile for e in register.offene(DI)] == [10]


# ---------------------------------------------------------------- Tolerante Struktur (Graph-Werte, andere Kopfzeile)


def _graph_matrix() -> list[list[object]]:
    """Wie Graph liefert: Kopfzeile abweichend beschriftet in Zeile 2, Formeln ausgewertet (jede Zeile
    trägt KW/Datum/Tag), Datumswerte als Serienzahl, Kontrollkästchen als WAHR/FALSCH-Text."""
    kopf = ["KW", "Datum", "Tag", "Lieferant", "Kunde", "Auftragsdatum", "Verladedatum", "Lieferant bestätigt", "Spedition bestätigt", "Abgeholt", "Bemerkung"]
    zeilen: list[list[object]] = [["Fixterminüberwachung"], kopf]
    for tag in (date(2026, 5, 11), date(2026, 5, 12)):
        for _slot in range(2):
            zeilen.append([20, datum_zu_serienzahl(tag), "Mo", "", "", "", "", "FALSCH", "FALSCH", "FALSCH", ""])
    zeilen[2][3:] = ["Beispielgut", "Musterhandel", datum_zu_serienzahl(date(2026, 5, 4)), "", "WAHR", "FALSCH", 0, "Best-Nr 4711, AB 12, Spedition Beispiel"]
    zeilen.append([20, "14.05.2026", "Do", "", "", "", "", "", "", "", "Feiertag: Christi Himmelfahrt"])
    return zeilen


def test_graph_werte_und_abweichende_kopfzeile(caplog: pytest.LogCaptureFixture):
    excel = FakeExcelBackend({"standard": {BLATT: _graph_matrix()}})
    reg = FixterminRegister(excel, DateiRefKonfig(blatt=BLATT), slots_je_tag=2)
    with caplog.at_level(logging.WARNING):
        reg.lade()
    assert reg.kopfzeile == 2 and "Zeile 2 statt 4" in caplog.text
    assert reg.spalten["lieferant_bestaetigt"] == 8 and reg.spalten["spedition_bestaetigt"] == 9
    assert reg.zeilen_fuer_datum(date(2026, 5, 11)) == [3, 4] and reg.zeilen_fuer_datum(date(2026, 5, 12)) == [5, 6]
    assert reg.zeilen_fuer_datum(HIMMELFAHRT) == [7] and reg.freie_slots(HIMMELFAHRT) == []
    [eintrag] = reg.eintraege()
    assert eintrag.auftragsdatum == date(2026, 5, 4) and eintrag.lieferant_bestaetigt is True
    assert eintrag.spedition_bestaetigt is False and eintrag.abgeholt is False
    assert (eintrag.bestellnummer, eintrag.ab_nummer, eintrag.spedition) == ("4711", "12", "Spedition Beispiel")
    neu = reg.trage_ein(_fixtermin(liefertag=date(2026, 5, 12)))
    assert neu.zeile == 5 and excel.schreibprotokoll[-1].adresse == "D5:K5"
    assert reg.trage_ein(_fixtermin(liefertag=date(2026, 5, 11), kunde="Zweiter")).zeile == 4
    with pytest.raises(RegisterVoll):
        reg.trage_ein(_fixtermin(liefertag=date(2026, 5, 11), kunde="Dritter"))
    with pytest.raises(RegisterFehler, match="gesperrt"):
        reg.trage_ein(_fixtermin(liefertag=HIMMELFAHRT))


def test_kopfzeile_fehlt_und_blatt_fallback(caplog: pytest.LogCaptureFixture):
    excel = FakeExcelBackend({"standard": {BLATT: [["nur", "Text"], ["ohne", "Kopf"]]}})
    with pytest.raises(RegisterFehler, match="Kopfzeile .* nicht gefunden .* datum, lieferant, kunde"):
        FixterminRegister(excel, DateiRefKonfig(blatt=BLATT)).lade()
    # Blattname ohne Umlaut konfiguriert -> das Blatt mit "fixtermin" im Namen wird genommen
    excel = FakeExcelBackend(aus_xlsx=FIXTURE)
    reg = FixterminRegister(excel, DateiRefKonfig(lokaler_pfad=FIXTURE, blatt="Fixterminueberwachung"))
    with caplog.at_level(logging.WARNING):
        reg.lade()
    assert reg.blatt == BLATT and "verwende" in caplog.text
    with pytest.raises(RegisterFehler, match="Blatt 'Termine' fehlt"):
        FixterminRegister(FakeExcelBackend({"standard": {"Daten": [["x"]]}}), DateiRefKonfig(blatt="Termine")).lade()


def test_fehlende_optionale_spalte_warnt_und_schreibt_rest(caplog: pytest.LogCaptureFixture):
    kopf = ["KW", "Datum", "Tag", "Lieferant", "Kunde", "Auftragsdatum", "Verladedatum", "Bestätigung des Lieferanten", "Bestätigung der Spedition", "Abgeholt"]
    excel = FakeExcelBackend({"standard": {BLATT: [[], [], [], kopf, [20, date(2026, 5, 11), "Mo"]]}})
    reg = FixterminRegister(excel, DateiRefKonfig(blatt=BLATT), slots_je_tag=1)
    with caplog.at_level(logging.WARNING):
        ergebnis = reg.trage_ein(_fixtermin(liefertag=date(2026, 5, 11), bestellnummer="1"))
    assert "ohne Spalte(n) bemerkung" in caplog.text and "nicht geschrieben" in caplog.text
    assert ergebnis.zeile == 5 and excel.schreibprotokoll[-1].adresse == "D5:J5"


def test_revised_order_aktualisiert_die_vorhandene_zeile(register: FixterminRegister):
    """Regression (Review Befund 11): Eine Revised Order (Cde und AB neu, gleiche Kundenbestellnummer) aktualisiert
    die vorhandene Zeile, statt einen zweiten Slot für dieselbe Lieferung zu belegen; die neuen Nummern stehen
    vorn, die ersetzten als Vermerk."""
    erst = register.trage_ein(_fixtermin("Cide", "Spar", FR, bestellnummer="WIK-100", ab_nummer="AB-200", kundenbestellnummer="K1"))
    assert erst.zeile == 21
    neu = register.trage_ein(_fixtermin("Cide", "Spar", FR, bestellnummer="WIK-101", ab_nummer="AB-201", kundenbestellnummer="K1"))
    assert neu.zeile == 21
    register.lade(erneut=True)
    assert [e.zeile for e in register.eintraege(FR, FR)] == [21]
    eintrag = register.eintrag(21)
    assert eintrag is not None and eintrag.bestellnummer == "WIK-101" and eintrag.kundenbestellnummer == "K1"
    assert "ersetzt Bestellung WIK-100" in (eintrag.bemerkung or "")
    # Revision mit eigener neuer Kundenbestellnummer: über revision_von die ersetzte Zeile
    ersatz = register.trage_ein(
        _fixtermin("Cide", "Spar", FR, bestellnummer="WIK-102", kundenbestellnummer="K2", revision_von="K1")
    )
    assert ersatz.zeile == 21
    register.lade(erneut=True)
    eintrag = register.eintrag(21)
    assert eintrag is not None and eintrag.bestellnummer == "WIK-102" and eintrag.kundenbestellnummer == "K2"
    # eine andere Bestellung desselben Kunden am selben Tag bleibt ein eigener Eintrag
    andere = register.trage_ein(_fixtermin("Cide", "Spar", FR, bestellnummer="WIK-300", kundenbestellnummer="K9"))
    assert andere.zeile == 22


# ---------------------------------------------------------------- Bemerkung


def test_bemerkung_teile():
    assert bemerkung_teile("Best.-Nr 20260001, AB 30260001, Spedition Beispiel") == {
        "bestellnummer": "20260001", "ab_nummer": "30260001", "spedition": "Spedition Beispiel", "tracking_referenz": None,
        "kundenbestellnummer": None,
    }
    teile = bemerkung_teile("verschoben von 12.05.2026: Abholung ab 10 Uhr | Best-Nr: 4711, Spedition Beispiel | Ref. DEMB00000")
    assert teile == {
        "bestellnummer": "4711", "ab_nummer": None, "spedition": "Spedition Beispiel", "tracking_referenz": "DEMB00000",
        "kundenbestellnummer": None,
    }
    assert bemerkung_teile("Best.-Nr 1, AB 2, Spedition Beispiel, Kd-Nr B-4711")["kundenbestellnummer"] == "B-4711"
    assert bemerkung_teile("Kunde wünscht Vormittag, Spedition Beispiel")["spedition"] is None  # ohne Nummer keine Spedition
    assert bemerkung_teile("AB 1, Best.-Nr 2 | AB 3")["ab_nummer"] == "1"  # erste Fundstelle gewinnt
    assert bemerkung_teile(None) == bemerkung_teile("") == {
        "bestellnummer": None, "ab_nummer": None, "spedition": None, "tracking_referenz": None, "kundenbestellnummer": None,
    }


def test_bemerkung_zusammenfuegen():
    fixtermin = _fixtermin(bestellnummer="1", ab_nummer="2", spedition="Spedition Beispiel", tracking_referenz="DEMB00000", bemerkung="Vormittag")
    assert bemerkung_zusammenfuegen("", fixtermin) == "Vormittag | Best.-Nr 1, AB 2, Spedition Beispiel, Ref. DEMB00000"
    assert bemerkung_zusammenfuegen("Best.-Nr 1, AB 2, Spedition Beispiel", fixtermin) == "Best.-Nr 1, AB 2, Spedition Beispiel | Vormittag | Ref. DEMB00000"
    assert bemerkung_zusammenfuegen("alt", _fixtermin()) == "alt"
    assert bemerkung_zusammenfuegen(None, _fixtermin(spedition="  ")) == ""
    assert bemerkung_zusammenfuegen("vormittag", _fixtermin(bemerkung="Vormittag")) == "vormittag"


# ---------------------------------------------------------------- Fixtermin aus dem Belegpaket


def _paket(settings: Settings, *, liefertag: date | None = date(2026, 5, 12), datum: date | None = date(2026, 5, 4), firma_id: str = "wik_fh_seh", spedition: str | None = "Spedition Beispiel") -> Belegpaket:
    roh = RohBestellung(
        eingangstyp=Eingangstyp.KUNDENBESTELLUNG_PDF,
        quelldatei=Path("eingang/bestellung-4711.pdf"),
        extraktionsmethode=Extraktionsmethode.PDFPLUMBER,
        kunde_text="Weinhandel Muster GmbH",
        lieferant_text="Weingut Beispiel",
        bestellnummer="B-4711",
        datum=datum,
        liefertag=liefertag,
    )
    paket = Belegpaket(roh=roh, firma_id=firma_id, belegfelder=Belegfelder(spedition=spedition))
    paket.kunde = KundeZuordnung(kunde=Kunde(firma_id=firma_id, kundennummer="10001", name="Weinhandel Muster GmbH", matchcode="Musterhandel"), status=MatchStatus.SICHER)
    paket.lieferant = LieferantZuordnung(lieferant=Lieferant(firma_id=firma_id, lieferantennummer="90001", name="Weingut Beispiel srl", matchcode="Beispielgut"), status=MatchStatus.SICHER)
    return paket


def _ergebnis(*, einkauf: str | None = "20260009", verkauf: str | None = "30260009", angebot: str | None = None, dry_run: bool = False) -> BuchungsErgebnis:
    def beleg(art: Belegart, nummer: str | None) -> Belegergebnis:
        return Belegergebnis(belegart=art, belegnummer=nummer, erfolgreich=True, dry_run=dry_run)

    return BuchungsErgebnis(
        firma_id="wik_fh_seh",
        quelldatei=Path("eingang/bestellung-4711.pdf"),
        dry_run=dry_run,
        zeitpunkt=datetime(2026, 5, 6, 9, 0),
        einkauf=beleg(Belegart.BESTELLUNG, einkauf) if einkauf is not None or dry_run else None,
        verkauf=beleg(Belegart.AUFTRAGSBESTAETIGUNG, verkauf) if verkauf is not None or dry_run else None,
        angebot=beleg(Belegart.ANGEBOT, angebot) if angebot is not None else None,
    )


def test_fixtermin_aus_paket(settings: Settings):
    fixtermin = fixtermin_aus_paket(_paket(settings), _ergebnis())
    assert fixtermin == Fixtermin(
        firma_id="wik_fh_seh", lieferant_kurz="Beispielgut", kunde_kurz="Musterhandel", liefertag=date(2026, 5, 12),
        auftragsdatum=date(2026, 5, 4), bestellnummer="20260009", kundenbestellnummer="B-4711", ab_nummer="30260009",
        spedition="Spedition Beispiel", quelle="bestellung-4711.pdf",
    )
    # Dry Run: keine Belegnummern; ohne Ergebnis ebenso
    dry = fixtermin_aus_paket(_paket(settings), _ergebnis(einkauf=None, verkauf=None, dry_run=True))
    assert dry.bestellnummer is None and dry.ab_nummer is None
    assert fixtermin_aus_paket(_paket(settings)).ab_nummer is None
    # Weinstrasse Agentur: keine Einkauf-Bestellung -> Nummer des Angebots (als "Order" gedruckt)
    ws = fixtermin_aus_paket(_paket(settings, firma_id="weinstrasse_agentur"), _ergebnis(einkauf=None, angebot="40260009"))
    assert ws.bestellnummer == "40260009" and ws.firma_id == "weinstrasse_agentur"
    # Auftragsdatum: Belegdatum, sonst heute
    heute = fixtermin_aus_paket(_paket(settings, datum=None), heute=date(2026, 5, 6))
    assert heute.auftragsdatum == date(2026, 5, 6)
    assert fixtermin_aus_paket(_paket(settings, datum=None)).auftragsdatum == date.today()
    # Spedition leer -> None
    assert fixtermin_aus_paket(_paket(settings, spedition="  ")).spedition is None


def test_fixtermin_aus_paket_heute_monkeypatch(settings: Settings, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(register_modul, "_heute", lambda: date(2026, 5, 7))
    assert fixtermin_aus_paket(_paket(settings, datum=None)).auftragsdatum == date(2026, 5, 7)


def test_fixtermin_aus_paket_mit_kurznamen_und_ohne_liefertag(settings: Settings):
    kurznamen = Kurznamen.lade(settings=settings, vorhandene=["Musterhandl"])
    fixtermin = fixtermin_aus_paket(_paket(settings), kurznamen=kurznamen)
    assert (fixtermin.lieferant_kurz, fixtermin.kunde_kurz) == ("Beispielgut", "Musterhandl")  # YAML + Dropdown-Angleichung
    with pytest.raises(RegisterFehler, match="bestellung-4711.pdf nennt keinen Liefertag"):
        fixtermin_aus_paket(_paket(settings, liefertag=None))


# ---------------------------------------------------------------- Dry Run / Vorschau


def test_dry_run_vorschau_csv_und_datei_unveraendert(settings: Settings, register: FixterminRegister, excel: FakeExcelBackend, tmp_path: Path):
    vorher = hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
    fixtermin = fixtermin_aus_paket(_paket(settings), _ergebnis())
    ergebnis = register.trage_ein(fixtermin)
    register.setze_bestaetigung(ergebnis, "lieferant")
    vorschau = excel.vorschau_csv(tmp_path / "logs" / "fixtermine_vorschau.csv")
    zeilen = vorschau.read_text(encoding="utf-8-sig").splitlines()
    assert zeilen[0] == "Zeitpunkt;Datei;Blatt;Zelle;Wert;Zahlenformat"
    assert any(";D12;Beispielgut;" in z for z in zeilen) and any(";F12;04.05.2026;" in z for z in zeilen)
    assert any(";K12;Best.-Nr 20260009, AB 30260009, Spedition Beispiel, Kd-Nr B-4711;" in z for z in zeilen)
    assert any(";H12;WAHR;" in z for z in zeilen)
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == vorher  # die .xlsx wird nie beschrieben


def test_nur_datenspalten_werden_geschrieben(register: FixterminRegister, excel: FakeExcelBackend):
    register.trage_ein(_fixtermin())
    register.setze_bestaetigung(_fixtermin(), "spedition")
    register.setze_verladedatum(_fixtermin(), date(2026, 5, 11))
    register.verschiebe(_fixtermin(), FR, "Test")
    assert excel.schreibprotokoll
    for zugriff in excel.schreibprotokoll:
        assert zugriff.adresse.startswith("D") and ":K" in zugriff.adresse
        assert all(zelle[0] in "DEFGHIJK" for zelle in _zellen(zugriff))
