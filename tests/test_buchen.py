"""Tests für lexware.buchen (Orchestrierung) und lexware.dry_run (DryRunSchreiber) – ohne Lexware."""

from __future__ import annotations

import csv
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from auftragsautomat.config import Settings
from auftragsautomat.lexware import buchen
from auftragsautomat.lexware.buchen import buche, vorbedingungen
from auftragsautomat.lexware.dry_run import DryRunSchreiber, positionszeilen
from auftragsautomat.models import (
    Artikel,
    Beleg,
    Belegart,
    Belegergebnis,
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
    Pruefcode,
    Pruefhinweis,
    RohBestellung,
    RohPosition,
    Schweregrad,
)
from auftragsautomat.protokoll import zusammenfassung
from auftragsautomat.stammdaten.basis import Stammdaten

FIRMA_ID = "wik_leh"


@pytest.fixture(autouse=True)
def _pdf_ordner_umleiten(settings: Settings, tmp_path: Path) -> None:
    """Dry-Run-PDF-Vorschauen (DryRunSchreiber.pdf_ausgeben) nie in tests/fixtures schreiben."""
    settings.lexware.pdf_ausgabe_ordner = tmp_path / "logs" / "pdf"


GLORIA = Artikel(
    firma_id=FIRMA_ID, artikelnummer="1643", bezeichnung="Villa Armellina Gloria Secco Frizzante 0,75 l",
    warengruppe="Cide", lieferantennummer="72", volumen_cl=Decimal("75"),
    ek_preis=Decimal("4.20"), vk_preis_1=Decimal("6.90"), einheit="Fl",
)
ROSATO = Artikel(
    firma_id=FIRMA_ID, artikelnummer="957", bezeichnung="Villa Armellina Rosato Secco Frizzante 0,75 l",
    warengruppe="Cide", lieferantennummer="72", volumen_cl=Decimal("75"),
    ek_preis=Decimal("4.20"), vk_preis_1=Decimal("6.90"), einheit="Fl",
)
HEGMANN = Kunde(
    firma_id=FIRMA_ID, kundennummer="3441", name="Hegmann", name2="Weinmarkt Bottrop-Kirchhellen",
    strasse="Hauptstraße 1", plz="46244", ort="Bottrop", land="DE", preisgruppe=1,
)
CIDE = Lieferant(firma_id=FIRMA_ID, lieferantennummer="72", name="Cide srl", name2="Villa Armellina", ort="Treviso")


def _paket(
    quelldatei: Path,
    *,
    freigegeben: bool = True,
    bestellnummer: str = "B-4712",
    lieferschein: bool = False,
) -> Belegpaket:
    """Kundenbestellung Weinmarkt Kirchhellen: 60 x 1643, 24 x 957, 'DAA Neptun' übersprungen."""
    roh = RohBestellung(
        eingangstyp=Eingangstyp.KUNDENBESTELLUNG_PDF,
        quelldatei=quelldatei,
        extraktionsmethode=Extraktionsmethode.PDFPLUMBER,
        kunde_text="Weinmarkt Kirchhellen",
        lieferant_text="Cide",
        belegnummer="2026-0917",
        bestellnummer=bestellnummer,
        datum=date(2026, 9, 10),
        liefertag=date(2026, 9, 15),
        positionen=[
            RohPosition(laufnr=1, bezeichnung="Gloria Secco Frizzante", menge=Decimal("60"), preis=Decimal("6.50"), hinweis="11+1"),
            RohPosition(laufnr=2, bezeichnung="Rosato Secco Frizzante", menge=Decimal("24"), preis=Decimal("6.90")),
            RohPosition(laufnr=3, bezeichnung="DAA Neptun", menge=Decimal("1"), preis=Decimal("0")),
        ],
    )
    return Belegpaket(
        roh=roh,
        firma_id=FIRMA_ID,
        kunde=KundeZuordnung(kunde=HEGMANN, status=MatchStatus.SICHER, quelle="alias"),
        lieferant=LieferantZuordnung(lieferant=CIDE, status=MatchStatus.SICHER, quelle="alias"),
        positionen=[
            PositionsZuordnung(
                roh=roh.positionen[0], artikel=GLORIA, status=MatchStatus.SICHER, menge=Decimal("60"),
                ek_preis=Decimal("4.20"), vk_preis=Decimal("6.90"), preis_dokument=Decimal("6.50"),
                hinweise=[Pruefhinweis(code=Pruefcode.PREIS_ABWEICHUNG, schweregrad=Schweregrad.WARNUNG,
                                       text="VK 6,90 weicht vom Dokumentpreis 6,50 ab", position_laufnr=1)],
            ),
            PositionsZuordnung(
                roh=roh.positionen[1], artikel=ROSATO, status=MatchStatus.SICHER, menge=Decimal("24"),
                ek_preis=Decimal("4.20"), vk_preis=Decimal("6.90"), preis_dokument=Decimal("6.90"),
            ),
            PositionsZuordnung(
                roh=roh.positionen[2], status=MatchStatus.UEBERSPRUNGEN, menge=Decimal("0"), preis_dokument=Decimal("0"),
                hinweise=[Pruefhinweis(code=Pruefcode.NULLPREIS_OHNE_ARTIKEL, schweregrad=Schweregrad.WARNUNG,
                                       text="0,00 € ohne Stammartikel, übersprungen", position_laufnr=3)],
            ),
        ],
        belegfelder=Belegfelder(spedition="Spedition Weinlogistik", weingut="Cide srl", shipment="15.09.2026", kunde="Hegmann"),
        einkauf_erforderlich=True,
        lieferschein_erstellen=lieferschein,
        freigegeben=freigegeben,
    )


def _quelldatei(settings: Settings, name: str = "kundenbestellung_kirchhellen.pdf") -> Path:
    pfad = settings.eingang_ordner / FIRMA_ID / name
    pfad.parent.mkdir(parents=True, exist_ok=True)
    pfad.write_bytes(b"%PDF-1.4 testdatei")
    return pfad


def _leere_stammdaten() -> Stammdaten:
    return Stammdaten(FIRMA_ID, artikel=[GLORIA, ROSATO], kunden=[HEGMANN], lieferanten=[CIDE])


def _protokoll_zeilen(settings: Settings) -> list[dict[str, str]]:
    pfad = settings.log_ordner / "protokoll.csv"
    with pfad.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f, delimiter=";"))


def _xml_dateien(settings: Settings) -> list[Path]:
    return sorted(settings.xml_ausgabe_ordner.glob("*.xml")) if settings.xml_ausgabe_ordner.exists() else []


class FakeSchreiber:
    """Erfüllt LexwareSchreiber; liefert konfigurierbare Ergebnisse und protokolliert die Aufrufe."""

    def __init__(
        self,
        *,
        einkauf_ok: bool = True,
        ab_ok: bool = True,
        ls_ok: bool = True,
        ab_nummer: str = "AB-2026-0001",
        werfen: bool = False,
    ) -> None:
        self.einkauf_ok = einkauf_ok
        self.ab_ok = ab_ok
        self.ls_ok = ls_ok
        self.ab_nummer = ab_nummer
        self.werfen = werfen
        self.aufrufe: list[tuple[str, bool, str | None]] = []

    @staticmethod
    def _ergebnis(belegart: Belegart, ok: bool, nummer: str, netto: Decimal, dry_run: bool) -> Belegergebnis:
        return Belegergebnis(
            belegart=belegart,
            belegnummer=None if dry_run or not ok else nummer,
            netto=netto,
            brutto=(netto * Decimal("1.19")).quantize(Decimal("0.01")),
            erfolgreich=ok,
            dry_run=dry_run,
            methode="ui_automation",
            fehler=None if ok else f"{belegart.value}: Summenabweichung (Fake)",
            warnungen=["Ablauf: Fake-Assistent durchlaufen"],
        )

    def schreibe_einkauf_bestellung(self, paket: Belegpaket, *, dry_run: bool = True) -> Belegergebnis:
        self.aufrufe.append(("einkauf", dry_run, None))
        if self.werfen:
            raise RuntimeError("Lexware-Fenster verschwunden")
        return self._ergebnis(Belegart.BESTELLUNG, self.einkauf_ok, "BE-2026-0001", paket.summe_netto_ek, dry_run)

    def schreibe_verkauf_ab(self, paket: Belegpaket, *, dry_run: bool = True) -> Belegergebnis:
        self.aufrufe.append(("verkauf", dry_run, None))
        return self._ergebnis(Belegart.AUFTRAGSBESTAETIGUNG, self.ab_ok, self.ab_nummer, paket.summe_netto_vk, dry_run)

    def auftrag_weiterfuehren_lieferschein(self, paket: Belegpaket, ab_belegnummer: str, *, dry_run: bool = True) -> Belegergebnis:
        self.aufrufe.append(("lieferschein", dry_run, ab_belegnummer))
        return self._ergebnis(Belegart.LIEFERSCHEIN, self.ls_ok, "LS-2026-0001", paket.summe_netto_vk, dry_run)


# ---------------------------------------------------------------------------
# Dry Run mit DryRunSchreiber (Standardweg unter Linux)
# ---------------------------------------------------------------------------


def test_dry_run_komplett(settings: Settings):
    quelle = _quelldatei(settings)
    paket = _paket(quelle)

    ergebnis = buche(paket, settings)

    assert ergebnis.erfolgreich, ergebnis.warnungen
    assert ergebnis.dry_run is True
    assert ergebnis.firma_id == FIRMA_ID
    assert ergebnis.einkauf is not None and ergebnis.einkauf.erfolgreich
    assert ergebnis.einkauf.methode == "dry_run"
    assert ergebnis.einkauf.belegnummer is None
    assert ergebnis.einkauf.netto == Decimal("352.80")
    assert ergebnis.einkauf.brutto == Decimal("419.83")
    assert "Artikel 1643 x 60 @ 4,20" in ergebnis.einkauf.warnungen
    assert "Artikel 957 x 24 @ 4,20" in ergebnis.einkauf.warnungen
    assert ergebnis.verkauf is not None and ergebnis.verkauf.erfolgreich
    assert ergebnis.verkauf.methode == "opentrans"
    assert ergebnis.verkauf.netto == Decimal("579.60")
    assert ergebnis.verkauf.artefakt is not None and ergebnis.verkauf.artefakt.exists()
    assert ergebnis.verkauf.artefakt.parent == settings.xml_ausgabe_ordner
    assert b"<SUPPLIER_AID>1643</SUPPLIER_AID>" in ergebnis.verkauf.artefakt.read_bytes()
    assert ergebnis.lieferschein is None

    zeilen = _protokoll_zeilen(settings)
    assert len(zeilen) == 1
    zeile = zeilen[0]
    assert zeile["firma"] == FIRMA_ID
    assert zeile["dry_run"] == "ja"
    assert zeile["kunde_nr"] == "3441"
    assert zeile["lieferant_nr"] == "72"
    assert zeile["bestellnummer"] == "B-4712"
    assert zeile["netto_ek"] == "352,80"
    assert zeile["netto_vk"] == "579,60"
    assert zeile["positionen"] == "2"
    assert zeile["uebersprungen"] == "1"
    assert zeile["erfolgreich"] == "ja"
    assert (settings.log_ordner / "protokoll.csv").read_bytes().startswith(b"\xef\xbb\xbf")
    berichte = list((settings.log_ordner / "berichte").glob("kundenbestellung_kirchhellen_*.txt"))
    assert len(berichte) == 1
    assert "Buchungsbericht" in berichte[0].read_text(encoding="utf-8")

    assert quelle.exists(), "Dry Run darf die Quelldatei nicht verschieben"
    assert not any(settings.archiv_ordner.rglob("*.pdf"))
    assert any(w.startswith("Protokoll: ") for w in ergebnis.warnungen)


def test_dry_run_ist_wiederholbar(settings: Settings):
    quelle = _quelldatei(settings)
    erstes = buche(_paket(quelle), settings)
    zweites = buche(_paket(quelle), settings)
    assert erstes.erfolgreich and zweites.erfolgreich, zweites.verkauf
    assert zweites.verkauf is not None and zweites.verkauf.artefakt is not None
    assert zweites.verkauf.artefakt.exists()
    assert any("umbenannt" in w for w in zweites.verkauf.warnungen)
    assert len(_xml_dateien(settings)) == 2
    assert len(_protokoll_zeilen(settings)) == 2


def test_lieferschein_flag_ohne_ab_belegnummer_nur_warnung(settings: Settings):
    paket = _paket(_quelldatei(settings), lieferschein=True)
    ergebnis = buche(paket, settings)
    assert ergebnis.erfolgreich
    assert ergebnis.lieferschein is None
    assert any("Lieferschein erst nach Import möglich" in w for w in ergebnis.warnungen)


def test_scharf_ohne_schreiber_auf_linux_wird_dry_run(settings: Settings, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(buchen, "_ist_windows", lambda: False)
    settings.dry_run = False
    quelle = _quelldatei(settings)
    ergebnis = buche(_paket(quelle), settings, dry_run=False, stammdaten=_leere_stammdaten())
    assert ergebnis.dry_run is True
    assert ergebnis.erfolgreich
    assert any("als Dry Run ausgeführt" in w for w in ergebnis.warnungen)
    assert quelle.exists()


def test_settings_dry_run_schlaegt_parameter_ohne_schreiber(settings: Settings, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(buchen, "_ist_windows", lambda: True)
    monkeypatch.setattr(buchen, "_erzeuge_ui_schreiber", lambda s, f: pytest.fail("UiSchreiber darf bei settings.dry_run nicht erzeugt werden"))
    settings.dry_run = True
    ergebnis = buche(_paket(_quelldatei(settings)), settings, dry_run=False, stammdaten=_leere_stammdaten())
    assert ergebnis.dry_run is True
    assert ergebnis.einkauf is not None and ergebnis.einkauf.methode == "dry_run"
    assert any("settings.dry_run" in w for w in ergebnis.warnungen)


# ---------------------------------------------------------------------------
# Vorbedingungen
# ---------------------------------------------------------------------------


def _nichts_geschrieben(settings: Settings, ergebnis, quelle: Path) -> None:
    assert ergebnis.einkauf is None and ergebnis.verkauf is None and ergebnis.lieferschein is None
    assert ergebnis.erfolgreich is False
    assert not (settings.log_ordner / "protokoll.csv").exists()
    assert _xml_dateien(settings) == []
    assert quelle.exists()


def test_nicht_freigegeben_nichts_geschrieben(settings: Settings):
    quelle = _quelldatei(settings)
    ergebnis = buche(_paket(quelle, freigegeben=False), settings)
    _nichts_geschrieben(settings, ergebnis, quelle)
    assert any("nicht freigegeben" in w for w in ergebnis.warnungen)


def test_blockiert_nichts_geschrieben(settings: Settings):
    quelle = _quelldatei(settings)
    paket = _paket(quelle)
    paket.hinweise.append(Pruefhinweis(code=Pruefcode.BELEGFELD_FEHLT, schweregrad=Schweregrad.FEHLER, text="Spedition fehlt"))
    assert paket.blockiert
    ergebnis = buche(paket, settings, dry_run=False, schreiber=FakeSchreiber())
    _nichts_geschrieben(settings, ergebnis, quelle)
    assert any("blockiert" in w and "BELEGFELD_FEHLT" in w for w in ergebnis.warnungen)


def test_ohne_firma_verweigert(settings: Settings):
    quelle = _quelldatei(settings)
    paket = _paket(quelle)
    paket.firma_id = None
    ergebnis = buche(paket, settings)
    _nichts_geschrieben(settings, ergebnis, quelle)
    assert ergebnis.firma_id == "unbekannt"
    assert any("Keine Firma" in w for w in ergebnis.warnungen)


def test_unbekannte_firma_verweigert(settings: Settings):
    paket = _paket(_quelldatei(settings))
    paket.firma_id = "gibt_es_nicht"
    ergebnis = buche(paket, settings)
    assert ergebnis.verkauf is None
    assert any("Unbekannte Firma" in w for w in ergebnis.warnungen)


def test_mandantenschutz_kunde_anderer_firma(settings: Settings):
    quelle = _quelldatei(settings)
    paket = _paket(quelle)
    paket.kunde.kunde = HEGMANN.model_copy(update={"firma_id": "woe_leh"})
    gruende = vorbedingungen(paket, settings)
    assert any("Mandantenschutz" in g for g in gruende)
    ergebnis = buche(paket, settings, dry_run=False, schreiber=FakeSchreiber())
    _nichts_geschrieben(settings, ergebnis, quelle)


def test_ohne_kunde_verweigert(settings: Settings):
    paket = _paket(_quelldatei(settings))
    paket.kunde = KundeZuordnung()
    assert any("Kein Kunde" in g for g in vorbedingungen(paket, settings))


# ---------------------------------------------------------------------------
# Weinstrasse Agentur: kein Einkauf
# ---------------------------------------------------------------------------


def test_weinstrasse_agentur_ohne_einkauf(settings: Settings):
    quelle = settings.eingang_ordner / "weinstrasse_agentur" / "provision.pdf"
    quelle.parent.mkdir(parents=True)
    quelle.write_bytes(b"%PDF")
    roh = RohBestellung(
        eingangstyp=Eingangstyp.KUNDENBESTELLUNG_PDF,
        quelldatei=quelle,
        extraktionsmethode=Extraktionsmethode.PDFPLUMBER,
        bestellnummer="PROV-77",
        datum=date(2026, 10, 1),
        positionen=[RohPosition(laufnr=1, bezeichnung="Provision", menge=Decimal("1"), preis=Decimal("150"))],
    )
    kunde = Kunde(firma_id="weinstrasse_agentur", kundennummer="9001", name="Weingut Sonnenhang", ort="Bad Dürkheim", land="DE")
    artikel = Artikel(firma_id="weinstrasse_agentur", artikelnummer="8001", bezeichnung="Agentur Provision Weinvermittlung", vk_preis_1=Decimal("150"), einheit="Stk")
    paket = Belegpaket(
        roh=roh,
        firma_id="weinstrasse_agentur",
        kunde=KundeZuordnung(kunde=kunde, status=MatchStatus.SICHER),
        positionen=[PositionsZuordnung(roh=roh.positionen[0], artikel=artikel, status=MatchStatus.SICHER, menge=Decimal("1"), vk_preis=Decimal("150"), preis_dokument=Decimal("150"))],
        belegfelder=Belegfelder(spedition="Selbstabholung", weingut="-", shipment="-", kunde="Weingut Sonnenhang"),
        einkauf_erforderlich=False,
        freigegeben=True,
    )
    ergebnis = buche(paket, settings)
    assert ergebnis.erfolgreich, ergebnis.warnungen
    assert ergebnis.einkauf is None
    assert ergebnis.verkauf is not None and ergebnis.verkauf.erfolgreich
    assert ergebnis.verkauf.netto == Decimal("150")
    assert any("Keine Einkauf-Bestellung" in w for w in ergebnis.warnungen)
    zeile = _protokoll_zeilen(settings)[0]
    assert zeile["firma"] == "weinstrasse_agentur"
    assert zeile["beleg_einkauf"] == ""
    assert zeile["netto_ek"] == ""
    assert zeile["lieferant_nr"] == ""


# ---------------------------------------------------------------------------
# Dubletten
# ---------------------------------------------------------------------------


def test_dublette_bricht_ab(settings: Settings):
    quelle = _quelldatei(settings)
    paket = _paket(quelle, bestellnummer="B-4712")
    stammdaten = Stammdaten(
        FIRMA_ID,
        belege=[Beleg(firma_id=FIRMA_ID, belegart=Belegart.AUFTRAGSBESTAETIGUNG, belegnummer="AB-2026-0199",
                      kundenbestellnummer="B-4712", partnernummer="3441", datum=date(2026, 9, 11))],
    )
    ergebnis = buche(paket, settings, dry_run=False, schreiber=FakeSchreiber(), stammdaten=stammdaten)
    assert ergebnis.erfolgreich is False
    assert ergebnis.einkauf is None and ergebnis.verkauf is None and ergebnis.lieferschein is None
    assert any("Dublette" in w for w in ergebnis.warnungen)
    assert paket.blockiert, "Dubletten-Treffer muss ins Paket eingetragen werden"
    assert any(h.code == Pruefcode.DUBLETTE for h in paket.hinweise)
    assert quelle.exists()
    assert _xml_dateien(settings) == []
    zeilen = _protokoll_zeilen(settings)
    assert len(zeilen) == 1 and zeilen[0]["erfolgreich"] == "nein"
    assert "Dublette" in zeilen[0]["warnungen"]


def test_dublette_aus_frisch_geladenen_stammdaten(settings: Settings):
    # B-4711 / Kunde 3441 ist im Fixture-Belegexport (AB-2026-0101) vorhanden -> Dublette ohne übergebene Stammdaten
    paket = _paket(_quelldatei(settings), bestellnummer="B-4711")
    ergebnis = buche(paket, settings)
    assert ergebnis.erfolgreich is False
    assert ergebnis.verkauf is None
    assert any("AB-2026-0101" in w for w in ergebnis.warnungen)


def test_dublette_eines_anderen_partners_ist_keine(settings: Settings):
    paket = _paket(_quelldatei(settings), bestellnummer="B-4712")
    stammdaten = Stammdaten(
        FIRMA_ID,
        belege=[Beleg(firma_id=FIRMA_ID, belegart=Belegart.AUFTRAGSBESTAETIGUNG, belegnummer="AB-2026-0150",
                      kundenbestellnummer="B-4712", partnernummer="1392")],
    )
    ergebnis = buche(paket, settings, stammdaten=stammdaten)
    assert ergebnis.erfolgreich, ergebnis.warnungen


def test_stammdaten_nicht_ladbar_scharf_verweigert_dry_run_warnt(settings: Settings, monkeypatch: pytest.MonkeyPatch):
    def _kaputt(firma):
        raise OSError("CSV nicht lesbar")

    monkeypatch.setattr(buchen, "_lade_stammdaten", _kaputt)
    quelle = _quelldatei(settings)
    scharf = buche(_paket(quelle), settings, dry_run=False, schreiber=FakeSchreiber())
    assert scharf.erfolgreich is False and scharf.verkauf is None
    assert any("Dublettenprüfung nicht möglich" in w for w in scharf.warnungen)
    assert quelle.exists()

    trocken = buche(_paket(quelle), settings, dry_run=True, schreiber=FakeSchreiber())
    assert trocken.erfolgreich
    assert any("Dublettenprüfung nicht möglich" in w for w in trocken.warnungen)


def test_stammdaten_anderer_firma_werden_abgelehnt(settings: Settings):
    ergebnis = buche(_paket(_quelldatei(settings)), settings, dry_run=False, schreiber=FakeSchreiber(), stammdaten=Stammdaten("woe_leh"))
    assert ergebnis.erfolgreich is False
    assert any("gehören zu Firma 'woe_leh'" in w for w in ergebnis.warnungen)


# ---------------------------------------------------------------------------
# Reihenfolge und Abbruch mit Fake-Schreiber
# ---------------------------------------------------------------------------


def test_scharf_einkauf_fehlgeschlagen_keine_ab(settings: Settings):
    quelle = _quelldatei(settings)
    fake = FakeSchreiber(einkauf_ok=False)
    ergebnis = buche(_paket(quelle, lieferschein=True), settings, dry_run=False, schreiber=fake, stammdaten=_leere_stammdaten())
    assert ergebnis.erfolgreich is False
    assert ergebnis.einkauf is not None and ergebnis.einkauf.erfolgreich is False
    assert ergebnis.verkauf is None and ergebnis.lieferschein is None
    assert [a[0] for a in fake.aufrufe] == ["einkauf"]
    assert any("keine halbe Buchung" in w for w in ergebnis.warnungen)
    assert quelle.exists(), "bei Fehlschlag nicht archivieren"
    zeile = _protokoll_zeilen(settings)[0]
    assert zeile["dry_run"] == "nein" and zeile["erfolgreich"] == "nein"
    assert zeile["beleg_einkauf"] == "FEHLER" and zeile["beleg_verkauf"] == ""


def test_dry_run_einkauf_fehlgeschlagen_ab_trotzdem(settings: Settings):
    fake = FakeSchreiber(einkauf_ok=False)
    ergebnis = buche(_paket(_quelldatei(settings)), settings, dry_run=True, schreiber=fake, stammdaten=_leere_stammdaten())
    assert ergebnis.erfolgreich is False
    assert ergebnis.verkauf is not None and ergebnis.verkauf.erfolgreich
    assert [a[0] for a in fake.aufrufe] == ["einkauf", "verkauf"]
    assert all(a[1] is True for a in fake.aufrufe)


def test_lieferschein_mit_ab_belegnummer(settings: Settings):
    fake = FakeSchreiber(ab_nummer="AB-2026-0042")
    ergebnis = buche(_paket(_quelldatei(settings), lieferschein=True), settings, dry_run=False, schreiber=fake, stammdaten=_leere_stammdaten())
    assert ergebnis.erfolgreich, ergebnis.warnungen
    assert ergebnis.lieferschein is not None and ergebnis.lieferschein.erfolgreich
    assert ergebnis.lieferschein.belegnummer == "LS-2026-0001"
    assert fake.aufrufe == [("einkauf", False, None), ("verkauf", False, None), ("lieferschein", False, "AB-2026-0042")]
    zeile = _protokoll_zeilen(settings)[0]
    assert zeile["beleg_einkauf"] == "BE-2026-0001"
    assert zeile["beleg_verkauf"] == "AB-2026-0042"
    assert zeile["beleg_lieferschein"] == "LS-2026-0001"


def test_lieferschein_nicht_ohne_flag(settings: Settings):
    fake = FakeSchreiber()
    ergebnis = buche(_paket(_quelldatei(settings), lieferschein=False), settings, dry_run=False, schreiber=fake, stammdaten=_leere_stammdaten())
    assert ergebnis.erfolgreich
    assert ergebnis.lieferschein is None
    assert [a[0] for a in fake.aufrufe] == ["einkauf", "verkauf"]


def test_lieferschein_bei_fehlgeschlagener_ab_nicht(settings: Settings):
    fake = FakeSchreiber(ab_ok=False)
    ergebnis = buche(_paket(_quelldatei(settings), lieferschein=True), settings, dry_run=False, schreiber=fake, stammdaten=_leere_stammdaten())
    assert ergebnis.erfolgreich is False
    assert ergebnis.lieferschein is None
    assert any("Lieferschein nicht erstellt" in w for w in ergebnis.warnungen)


def test_scharf_archiviert_quelldatei(settings: Settings):
    quelle = _quelldatei(settings)
    ergebnis = buche(_paket(quelle), settings, dry_run=False, schreiber=FakeSchreiber(), stammdaten=_leere_stammdaten())
    assert ergebnis.erfolgreich, ergebnis.warnungen
    assert ergebnis.dry_run is False
    assert not quelle.exists()
    archiviert = list(settings.archiv_ordner.rglob("kundenbestellung_kirchhellen.pdf"))
    assert len(archiviert) == 1
    assert archiviert[0].parent.parent == settings.archiv_ordner / FIRMA_ID
    assert archiviert[0].parent.name == f"{date.today().year}"
    assert any(w.startswith("Quelldatei archiviert: ") for w in ergebnis.warnungen)
    zeile = _protokoll_zeilen(settings)[0]
    assert zeile["dry_run"] == "nein" and zeile["erfolgreich"] == "ja"
    assert zeile["quelldatei"] == str(quelle)


def test_scharf_fehlende_quelldatei_nur_warnung(settings: Settings):
    quelle = settings.eingang_ordner / "fehlt.pdf"
    ergebnis = buche(_paket(quelle), settings, dry_run=False, schreiber=FakeSchreiber(), stammdaten=_leere_stammdaten())
    assert ergebnis.erfolgreich
    assert any("nicht gefunden, nichts archiviert" in w for w in ergebnis.warnungen)


def test_schreiber_exception_wird_belegergebnis(settings: Settings):
    quelle = _quelldatei(settings)
    ergebnis = buche(_paket(quelle), settings, dry_run=False, schreiber=FakeSchreiber(werfen=True), stammdaten=_leere_stammdaten())
    assert ergebnis.erfolgreich is False
    assert ergebnis.einkauf is not None
    assert ergebnis.einkauf.methode == "keine"
    assert ergebnis.einkauf.fehler is not None and "RuntimeError" in ergebnis.einkauf.fehler
    assert ergebnis.verkauf is None
    assert quelle.exists()


def test_explizit_dry_run_schreiber_scharf_wird_nicht_archiviert(settings: Settings):
    quelle = _quelldatei(settings)
    schreiber = DryRunSchreiber(settings, settings.firma(FIRMA_ID))
    ergebnis = buche(_paket(quelle), settings, dry_run=False, schreiber=schreiber, stammdaten=_leere_stammdaten())
    assert ergebnis.erfolgreich
    assert ergebnis.dry_run is True, "DryRunSchreiber simuliert immer -> Lauf gilt als Dry Run"
    assert quelle.exists()
    assert any("gilt als Dry Run" in w for w in ergebnis.warnungen)


# ---------------------------------------------------------------------------
# Schreiberwahl unter Windows (simuliert)
# ---------------------------------------------------------------------------


def test_windows_scharf_opentrans_fuer_ab_und_ui_fuer_einkauf(settings: Settings, monkeypatch: pytest.MonkeyPatch):
    fake = FakeSchreiber()
    monkeypatch.setattr(buchen, "_ist_windows", lambda: True)
    monkeypatch.setattr(buchen, "_erzeuge_ui_schreiber", lambda s, f: fake)
    settings.dry_run = False
    firma = settings.firma(FIRMA_ID)
    assert firma.opentrans.lizenziert is True
    assert firma.opentrans.import_ordner is not None
    firma.opentrans.import_ordner.mkdir(parents=True)
    quelle = _quelldatei(settings)

    ergebnis = buche(_paket(quelle, lieferschein=True), settings, dry_run=False, stammdaten=_leere_stammdaten())

    assert ergebnis.erfolgreich, ergebnis.warnungen
    assert ergebnis.dry_run is False
    assert ergebnis.einkauf is not None and ergebnis.einkauf.methode == "ui_automation"
    assert ergebnis.verkauf is not None and ergebnis.verkauf.methode == "opentrans"
    assert ergebnis.verkauf.artefakt is not None and ergebnis.verkauf.artefakt.parent == firma.opentrans.import_ordner
    assert ergebnis.lieferschein is None
    assert any("Lieferschein erst nach Import möglich" in w for w in ergebnis.warnungen)
    assert [a[0] for a in fake.aufrufe] == ["einkauf"]
    assert not quelle.exists()


def test_windows_nicht_lizenziert_ab_per_ui(settings: Settings, monkeypatch: pytest.MonkeyPatch):
    fake = FakeSchreiber()
    monkeypatch.setattr(buchen, "_ist_windows", lambda: True)
    monkeypatch.setattr(buchen, "_erzeuge_ui_schreiber", lambda s, f: fake)
    settings.dry_run = False
    settings.firma(FIRMA_ID).opentrans.lizenziert = False
    ergebnis = buche(_paket(_quelldatei(settings), lieferschein=True), settings, dry_run=False, stammdaten=_leere_stammdaten())
    assert ergebnis.erfolgreich, ergebnis.warnungen
    assert ergebnis.verkauf is not None and ergebnis.verkauf.methode == "ui_automation"
    assert ergebnis.lieferschein is not None and ergebnis.lieferschein.belegnummer == "LS-2026-0001"
    assert [a[0] for a in fake.aufrufe] == ["einkauf", "verkauf", "lieferschein"]


def test_windows_lizenz_ungeprueft_warnt_und_nutzt_ui(settings: Settings, monkeypatch: pytest.MonkeyPatch):
    fake = FakeSchreiber()
    monkeypatch.setattr(buchen, "_ist_windows", lambda: True)
    monkeypatch.setattr(buchen, "_erzeuge_ui_schreiber", lambda s, f: fake)
    settings.dry_run = False
    settings.firma(FIRMA_ID).opentrans.lizenziert = None
    ergebnis = buche(_paket(_quelldatei(settings)), settings, dry_run=True, stammdaten=_leere_stammdaten())
    assert ergebnis.dry_run is True
    assert ergebnis.verkauf is not None and ergebnis.verkauf.methode == "ui_automation"
    assert any("noch nicht geprüft" in w for w in ergebnis.warnungen)
    assert all(a[1] is True for a in fake.aufrufe)


def test_windows_ui_schreiber_nicht_verfuegbar_verweigert(settings: Settings, monkeypatch: pytest.MonkeyPatch):
    from auftragsautomat.lexware.ui_automation import BackendNichtVerfuegbar

    def _fehlt(s, f):
        raise BackendNichtVerfuegbar("pywinauto nicht installiert")

    monkeypatch.setattr(buchen, "_ist_windows", lambda: True)
    monkeypatch.setattr(buchen, "_erzeuge_ui_schreiber", _fehlt)
    settings.dry_run = False
    quelle = _quelldatei(settings)
    ergebnis = buche(_paket(quelle), settings, dry_run=False)
    assert ergebnis.erfolgreich is False and ergebnis.einkauf is None
    assert any("BackendNichtVerfuegbar" in w for w in ergebnis.warnungen)
    assert quelle.exists()


def test_windows_opentrans_vorpruefung_verhindert_halbe_buchung(settings: Settings, monkeypatch: pytest.MonkeyPatch):
    fake = FakeSchreiber()
    monkeypatch.setattr(buchen, "_ist_windows", lambda: True)
    monkeypatch.setattr(buchen, "_erzeuge_ui_schreiber", lambda s, f: fake)
    settings.dry_run = False
    paket = _paket(_quelldatei(settings))
    paket.positionen[1].vk_preis = None  # AB nicht erzeugbar -> Einkauf darf gar nicht erst angelegt werden
    ergebnis = buche(paket, settings, dry_run=False, stammdaten=_leere_stammdaten())
    assert ergebnis.erfolgreich is False
    assert fake.aufrufe == []
    assert any("Verkauf-AB per openTRANS nicht möglich" in w for w in ergebnis.warnungen)


# ---------------------------------------------------------------------------
# DryRunSchreiber direkt
# ---------------------------------------------------------------------------


def test_dry_run_schreiber_einkauf(settings: Settings):
    schreiber = DryRunSchreiber(settings, settings.firma(FIRMA_ID))
    ergebnis = schreiber.schreibe_einkauf_bestellung(_paket(_quelldatei(settings)))
    assert ergebnis.erfolgreich and ergebnis.dry_run and ergebnis.belegnummer is None
    assert ergebnis.belegart == Belegart.BESTELLUNG
    assert ergebnis.netto == Decimal("352.80") and ergebnis.brutto == Decimal("419.83")
    assert "Artikel 1643 x 60 @ 4,20" in ergebnis.warnungen
    assert any(w.startswith("Belegfelder: Spedition=Spedition Weinlogistik") for w in ergebnis.warnungen)
    assert any("Customer" in w and "3441 Hegmann" in w for w in ergebnis.warnungen)


def test_dry_run_schreiber_einkauf_ohne_lieferant(settings: Settings):
    schreiber = DryRunSchreiber(settings, settings.firma(FIRMA_ID))
    paket = _paket(_quelldatei(settings))
    paket.lieferant = LieferantZuordnung()
    ergebnis = schreiber.schreibe_einkauf_bestellung(paket)
    assert ergebnis.erfolgreich is False
    assert ergebnis.fehler is not None and "Kein Lieferant" in ergebnis.fehler
    assert ergebnis.methode == "dry_run"


def test_dry_run_schreiber_einkauf_ohne_ek_preis(settings: Settings):
    schreiber = DryRunSchreiber(settings, settings.firma(FIRMA_ID))
    paket = _paket(_quelldatei(settings))
    paket.positionen[0].ek_preis = None
    ergebnis = schreiber.schreibe_einkauf_bestellung(paket)
    assert ergebnis.erfolgreich is False
    assert ergebnis.fehler is not None and "kein EK-Preis" in ergebnis.fehler


def test_dry_run_schreiber_scharf_wird_ignoriert(settings: Settings):
    schreiber = DryRunSchreiber(settings, settings.firma(FIRMA_ID))
    paket = _paket(_quelldatei(settings))
    einkauf = schreiber.schreibe_einkauf_bestellung(paket, dry_run=False)
    verkauf = schreiber.schreibe_verkauf_ab(paket, dry_run=False)
    assert einkauf.dry_run is True and verkauf.dry_run is True
    assert any("trotzdem nur simuliert" in w for w in einkauf.warnungen)
    assert any("trotzdem nur simuliert" in w for w in verkauf.warnungen)
    assert verkauf.artefakt is not None and verkauf.artefakt.parent == settings.xml_ausgabe_ordner
    assert not (settings.firma(FIRMA_ID).opentrans.import_ordner or Path("/nicht/da")).exists()


def test_dry_run_schreiber_verkauf_unvollstaendig(settings: Settings):
    schreiber = DryRunSchreiber(settings, settings.firma(FIRMA_ID))
    paket = _paket(_quelldatei(settings))
    paket.kunde = KundeZuordnung()
    ergebnis = schreiber.schreibe_verkauf_ab(paket)
    assert ergebnis.erfolgreich is False
    assert ergebnis.methode == "dry_run"
    assert ergebnis.fehler is not None and "Kein Kunde" in ergebnis.fehler
    assert _xml_dateien(settings) == []


def test_dry_run_schreiber_verkauf_warnungen(settings: Settings):
    schreiber = DryRunSchreiber(settings, settings.firma(FIRMA_ID))
    ergebnis = schreiber.schreibe_verkauf_ab(_paket(_quelldatei(settings)))
    assert ergebnis.erfolgreich
    assert ergebnis.warnungen[0] == "Belegnummer wird erst beim Import in Lexware vergeben"
    assert "Artikel 1643 x 60 @ 6,90" in ergebnis.warnungen
    assert "Artikel 957 x 24 @ 6,90" in ergebnis.warnungen


def test_dry_run_schreiber_lieferschein(settings: Settings):
    schreiber = DryRunSchreiber(settings, settings.firma(FIRMA_ID))
    paket = _paket(_quelldatei(settings), lieferschein=True)
    ergebnis = schreiber.auftrag_weiterfuehren_lieferschein(paket, "AB-2026-0001")
    assert ergebnis.erfolgreich and ergebnis.dry_run
    assert ergebnis.belegart == Belegart.LIEFERSCHEIN
    assert ergebnis.netto == Decimal("579.60") and ergebnis.brutto == Decimal("689.72")
    assert any("AB-2026-0001" in w for w in ergebnis.warnungen)

    ohne_nummer = schreiber.auftrag_weiterfuehren_lieferschein(paket, "")
    assert ohne_nummer.erfolgreich is False and "AB-Belegnummer fehlt" in (ohne_nummer.fehler or "")
    paket.lieferschein_erstellen = False
    ohne_flag = schreiber.auftrag_weiterfuehren_lieferschein(paket, "AB-2026-0001")
    assert ohne_flag.erfolgreich is False and "nicht angefordert" in (ohne_flag.fehler or "")


def test_positionszeilen(settings: Settings):
    paket = _paket(_quelldatei(settings))
    assert positionszeilen(paket, "ek") == ["Artikel 1643 x 60 @ 4,20", "Artikel 957 x 24 @ 4,20"]
    assert positionszeilen(paket, "vk") == ["Artikel 1643 x 60 @ 6,90", "Artikel 957 x 24 @ 6,90"]


# ---------------------------------------------------------------------------
# Mehrere Weingüter: je Weingut eine eigene Einkauf-Bestellung
# ---------------------------------------------------------------------------

NERO = Artikel(
    firma_id=FIRMA_ID, artikelnummer="2001", bezeichnung="Colosi Nero d'Avola Sicilia DOC 2022 0,75 l",
    warengruppe="Colosi", lieferantennummer="87", volumen_cl=Decimal("75"), jahrgang=2022,
    ek_preis=Decimal("5.10"), vk_preis_1=Decimal("8.40"), einheit="Fl",
)
COLOSI = Lieferant(firma_id=FIRMA_ID, lieferantennummer="87", name="Cantine Colosi", name2="Colosi", ort="Messina")


def _paket_zwei_weingueter(settings: Settings) -> tuple[Belegpaket, Stammdaten]:
    paket = _paket(_quelldatei(settings))
    nero_roh = RohPosition(laufnr=4, bezeichnung="Colosi Nero d'Avola 2022", menge=Decimal("12"), preis=Decimal("8.40"))
    paket.roh.positionen.append(nero_roh)
    paket.positionen.append(
        PositionsZuordnung(
            roh=nero_roh, artikel=NERO, status=MatchStatus.SICHER, menge=Decimal("12"),
            ek_preis=Decimal("5.10"), vk_preis=Decimal("8.40"), preis_dokument=Decimal("8.40"),
        )
    )
    paket.belegfelder.weingut = "Cide srl / Cantine Colosi"
    stammdaten = Stammdaten(FIRMA_ID, artikel=[GLORIA, ROSATO, NERO], kunden=[HEGMANN], lieferanten=[CIDE, COLOSI])
    return paket, stammdaten


def test_einkaufs_teilpakete(settings: Settings):
    paket, stammdaten = _paket_zwei_weingueter(settings)
    warnungen: list[str] = []
    teile = buchen.einkaufs_teilpakete(paket, stammdaten, warnungen)
    assert [(b, [p.roh.laufnr for p in t.aktive_positionen()]) for t, b in teile] == [
        ("72 Cide srl", [1, 2]),
        ("87 Cantine Colosi", [4]),
    ]
    cide, colosi = teile[0][0], teile[1][0]
    assert cide is not paket and paket.positionen[3].artikel is NERO  # Original unverändert
    assert cide.lieferant.lieferant is CIDE or cide.lieferant.lieferant.lieferantennummer == "72"
    assert cide.belegfelder.weingut == "Cide srl" and colosi.belegfelder.weingut == "Cantine Colosi"
    assert colosi.lieferant.lieferant.lieferantennummer == "87" and colosi.lieferant.quelle == "aufteilung"
    assert cide.summe_netto_ek == Decimal("352.80") and colosi.summe_netto_ek == Decimal("61.20")
    assert cide.kunde.kunde is not None and cide.kunde.kunde.kundennummer == "3441"  # Customer bleibt
    assert any("2 Einkauf-Bestellungen" in w for w in warnungen)
    # Ohne Stammdaten keine Aufteilung (Warnung), alles an den Hauptlieferanten
    ohne: list[str] = []
    assert buchen.einkaufs_teilpakete(paket, None, ohne) == [(paket, "72 Cide srl")]
    assert any("nicht geprüft" in w for w in ohne)
    # Nur ein Weingut -> Originalpaket
    einfach = _paket(_quelldatei(settings, "einfach.pdf"))
    assert buchen.einkaufs_teilpakete(einfach, stammdaten) == [(einfach, "72 Cide srl")]


def test_dry_run_zwei_weingueter_zwei_bestellungen(settings: Settings):
    paket, stammdaten = _paket_zwei_weingueter(settings)
    ergebnis = buche(paket, settings, stammdaten=stammdaten)
    assert ergebnis.erfolgreich and ergebnis.dry_run, ergebnis.warnungen
    assert ergebnis.einkauf is not None and ergebnis.einkauf.partner == "72 Cide srl"
    assert ergebnis.einkauf.netto == Decimal("352.80")
    assert [t.partner for t in ergebnis.einkauf_weitere] == ["87 Cantine Colosi"]
    assert ergebnis.einkauf_weitere[0].netto == Decimal("61.20")
    assert len(ergebnis.alle_einkaeufe()) == 2
    assert any("Einkauf-Bestellung an Lieferant 87 Cantine Colosi" in w for w in ergebnis.einkauf_weitere[0].warnungen)
    assert any("Weingut=Cantine Colosi" in w for w in ergebnis.einkauf_weitere[0].warnungen)
    assert ergebnis.verkauf is not None and ergebnis.verkauf.netto == Decimal("680.40")  # AB bleibt ein Beleg
    assert ergebnis.protokoll == settings.log_ordner / "protokoll.csv" and ergebnis.archiv is None
    zeile = _protokoll_zeilen(settings)[0]
    assert zeile["beleg_einkauf"] == "(simuliert) + (simuliert)"
    assert zeile["netto_ek"] == "414,00" and zeile["lieferant_nr"] == "72"
    assert "87 Cantine Colosi" in zeile["warnungen"]
    text = zusammenfassung(paket, ergebnis)
    assert "Einkauf-Bestellung (87 Cantine Colosi)" in text and "aufgeteilt auf 2 Weingüter" in text


def test_scharf_zweite_bestellung_fehlgeschlagen_keine_ab(settings: Settings):
    paket, stammdaten = _paket_zwei_weingueter(settings)

    class ZweiteSchlaegtFehl(FakeSchreiber):
        def schreibe_einkauf_bestellung(self, paket: Belegpaket, *, dry_run: bool = True) -> Belegergebnis:
            self.einkauf_ok = paket.lieferant.lieferant.lieferantennummer != "87"
            return super().schreibe_einkauf_bestellung(paket, dry_run=dry_run)

    fake = ZweiteSchlaegtFehl()
    ergebnis = buche(paket, settings, dry_run=False, schreiber=fake, stammdaten=stammdaten)
    assert not ergebnis.erfolgreich
    assert ergebnis.einkauf is not None and ergebnis.einkauf.erfolgreich
    assert ergebnis.einkauf_weitere and not ergebnis.einkauf_weitere[0].erfolgreich
    assert ergebnis.verkauf is None  # keine halbe Buchung
    assert [a[0] for a in fake.aufrufe] == ["einkauf", "einkauf"]
    assert any("keine halbe Buchung" in w for w in ergebnis.warnungen)


def test_lieferschein_nachziehen(settings: Settings):
    paket = _paket(_quelldatei(settings), lieferschein=True)
    fake = FakeSchreiber()
    ergebnis = buchen.lieferschein_nachziehen(paket, "AB-2026-0101", settings, dry_run=False, schreiber=fake)
    assert ergebnis.erfolgreich and not ergebnis.dry_run
    assert ergebnis.einkauf is None and ergebnis.verkauf is None
    assert ergebnis.lieferschein is not None and ergebnis.lieferschein.belegnummer == "LS-2026-0001"
    assert fake.aufrufe == [("lieferschein", False, "AB-2026-0101")]
    assert ergebnis.protokoll is not None and ergebnis.archiv is None  # nie archivieren
    zeile = _protokoll_zeilen(settings)[-1]
    assert zeile["beleg_lieferschein"] == "LS-2026-0001" and zeile["beleg_verkauf"] == ""

    # Dry Run ohne Schreiber: DryRunSchreiber simuliert
    simuliert = buchen.lieferschein_nachziehen(paket, "AB-2026-0101", settings)
    assert simuliert.erfolgreich and simuliert.dry_run and simuliert.lieferschein is not None
    assert simuliert.lieferschein.methode == "dry_run"

    # Verweigert: ohne AB-Nummer, ohne Anforderung, ohne Freigabe
    ohne_ab = buchen.lieferschein_nachziehen(paket, " ", settings, schreiber=fake)
    assert ohne_ab.lieferschein is None and any("AB-Belegnummer fehlt" in w for w in ohne_ab.warnungen)
    paket.lieferschein_erstellen = False
    nicht_angefordert = buchen.lieferschein_nachziehen(paket, "AB-1", settings, schreiber=fake)
    assert any("nicht angefordert" in w for w in nicht_angefordert.warnungen)
    paket.lieferschein_erstellen = True
    paket.freigegeben = False
    nicht_frei = buchen.lieferschein_nachziehen(paket, "AB-1", settings, schreiber=fake)
    assert any("nicht freigegeben" in w for w in nicht_frei.warnungen)
    assert len(fake.aufrufe) == 1  # nichts weiter geschrieben
