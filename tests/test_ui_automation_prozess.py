"""Tests für lexware.ui_automation (Fake-Backend): Angebot, Positionsrabatt, Weiterführen/Wandeln, Lieferdatum und
AB-Text leeren, Nachbemerkung, PDF-Ausgabe (drucker/export/keine) – ohne Windows, ohne pywinauto.

Alle Daten sind synthetisch (Weinhandel Beispiel, Weingut Muster).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from auftragsautomat.config import Settings
from auftragsautomat.lexware.fake_backend import FakeArtikel, FakeLexwareBackend
from auftragsautomat.lexware.ui_automation import ControlNichtGefunden, UiSchreiber
from auftragsautomat.models import (
    Artikel,
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
    RohBestellung,
    RohPosition,
)

FIRMA_ID = "wik_leh"
FIRMENNAME = "Wein-Import-Kreis LEH"
HEUTE = date(2026, 10, 24)
AB_STANDARDTEXT = "Wir bedanken uns für Ihren Auftrag."

ROT = Artikel(firma_id=FIRMA_ID, artikelnummer="4001", bezeichnung="Muster Rotwein 0,75 l", lieferantennummer="80001",
              ek_preis=Decimal("4.00"), vk_preis_1=Decimal("6.90"))
WEISS = Artikel(firma_id=FIRMA_ID, artikelnummer="4002", bezeichnung="Muster Weißwein 0,75 l", lieferantennummer="80001",
                ek_preis=Decimal("3.50"), vk_preis_1=Decimal("5.90"))
LIEFERANT = Lieferant(firma_id=FIRMA_ID, lieferantennummer="80001", name="Weingut Muster", matchcode="Muster")
KUNDE = Kunde(firma_id=FIRMA_ID, kundennummer="90001", name="Weinhandel Beispiel GmbH", matchcode="Beispiel")


@pytest.fixture(autouse=True)
def _pdf_ordner(settings: Settings, tmp_path: Path) -> None:
    settings.lexware.pdf_ausgabe_ordner = tmp_path / "pdf"


def _position(laufnr: int, artikel: Artikel, menge: str, rabatt: str | None = None) -> PositionsZuordnung:
    return PositionsZuordnung(
        roh=RohPosition(laufnr=laufnr, bezeichnung=artikel.bezeichnung, menge=Decimal(menge), preis=artikel.vk_preis_1),
        artikel=artikel, status=MatchStatus.SICHER, menge=Decimal(menge), ek_preis=artikel.ek_preis,
        vk_preis=artikel.vk_preis_1, preis_dokument=artikel.vk_preis_1,
        rabatt_prozent=Decimal(rabatt) if rabatt else None, rabatt_quelle="aktion:herbst" if rabatt else None,
    )


def _paket(*, freigegeben: bool = True, rabatt: str | None = "10") -> Belegpaket:
    return Belegpaket(
        roh=RohBestellung(
            eingangstyp=Eingangstyp.KUNDENBESTELLUNG_PDF, quelldatei=Path("bestellung_beispiel.pdf"),
            extraktionsmethode=Extraktionsmethode.PDFPLUMBER, kunde_text="Weinhandel Beispiel",
            bestellnummer="B-0001", liefertag=date(2026, 11, 5),
        ),
        firma_id=FIRMA_ID,
        kunde=KundeZuordnung(kunde=KUNDE, status=MatchStatus.SICHER, quelle="alias"),
        lieferant=LieferantZuordnung(lieferant=LIEFERANT, status=MatchStatus.SICHER, quelle="alias"),
        positionen=[_position(1, ROT, "12", rabatt), _position(2, WEISS, "6")],
        belegfelder=Belegfelder(spedition="Spedition Beispiel", weingut="Weingut Muster", shipment="05.11.2026", kunde="Weinhandel Beispiel"),
        freigegeben=freigegeben,
        lieferschein_erstellen=True,
    )


def _fake(**kw) -> FakeLexwareBackend:
    kw.setdefault("firmenname", FIRMENNAME)
    kw.setdefault("artikel", [
        FakeArtikel("4001", ROT.bezeichnung, Decimal("4.00"), Decimal("6.90")),
        FakeArtikel("4002", WEISS.bezeichnung, Decimal("3.50"), Decimal("5.90")),
    ])
    kw.setdefault("partner", {"80001": "Weingut Muster", "90001": "Weinhandel Beispiel GmbH"})
    kw.setdefault("heute", lambda: HEUTE)
    kw.setdefault("standardtexte", {"Auftragsbestätigung": AB_STANDARDTEXT})
    return FakeLexwareBackend(**kw)


def _schreiber(settings: Settings, fake: FakeLexwareBackend, **kw) -> UiSchreiber:
    kw.setdefault("heute", lambda: HEUTE)
    return UiSchreiber(settings, settings.firma(FIRMA_ID), fake, **kw)


def _index(aktionen: list[str], eintrag: str, *, ab: int = 0) -> int:
    for i in range(ab, len(aktionen)):
        if aktionen[i] == eintrag:
            return i
    raise AssertionError(f"{eintrag!r} nicht in Aktionen ab {ab}: {aktionen[ab:]}")


NETTO_VK = Decimal("109.92")  # 12 x 6,90 x 0,9 = 74,52 + 6 x 5,90 = 35,40
NETTO_EK = Decimal("69.00")  # 12 x 4,00 + 6 x 3,50


# ---------------------------------------------------------------------------
# Einkauf: Nachbemerkung, nie Rabatt
# ---------------------------------------------------------------------------


def test_einkauf_scharf_mit_nachbemerkung(settings: Settings):
    fake = _fake()
    ergebnis = _schreiber(settings, fake).schreibe_einkauf_bestellung(_paket(), dry_run=False)
    assert ergebnis.erfolgreich, ergebnis.fehler
    assert ergebnis.belegnummer == "BE-2026-0001"
    assert ergebnis.nachbemerkung == "B-0001"
    assert ergebnis.netto == NETTO_EK
    beleg = fake.gespeicherte_belege[0]
    assert beleg.felder["NACHBEMERKUNG"] == "B-0001"
    assert beleg.rabatte == []  # Geschäftsregel 16: nie im Einkauf
    a = fake.aktionen
    i_weiter2 = _index(a, "klicke:WEITER", ab=_index(a, "klicke:WEITER") + 1)
    i_reg = _index(a, "klicke:REGISTER_NACHBEMERKUNG", ab=i_weiter2)
    assert _index(a, "setze:NACHBEMERKUNG:B-0001", ab=i_reg) < _index(a, "klicke:SPEICHERN", ab=i_reg)
    assert not any(x.startswith("tippe:RABATT") or x.startswith("setze:RABATT") for x in a)
    assert any("Register 'Nachbemerkung' geöffnet" in w for w in ergebnis.warnungen)


def test_einkauf_dry_run_nachbemerkung_feld_fehlt_nur_warnung(settings: Settings):
    fake = _fake(felder_fehlend={"NACHBEMERKUNG"})
    ergebnis = _schreiber(settings, fake).schreibe_einkauf_bestellung(_paket(), dry_run=True)
    assert ergebnis.erfolgreich, ergebnis.fehler
    assert ergebnis.nachbemerkung is None
    assert any("Nachbemerkung" in w and "nicht gefunden" in w for w in ergebnis.warnungen)
    assert "klicke:SPEICHERN" not in fake.aktionen


def test_einkauf_scharf_nachbemerkung_feld_fehlt_bricht_ab(settings: Settings):
    fake = _fake(felder_fehlend={"NACHBEMERKUNG"})
    ergebnis = _schreiber(settings, fake).schreibe_einkauf_bestellung(_paket(), dry_run=False)
    assert ergebnis.erfolgreich is False
    assert "NACHBEMERKUNG" in (ergebnis.fehler or "")
    assert fake.gespeicherte_belege == []


def test_einkauf_ohne_nachbemerkung_wenn_firma_es_nicht_vorsieht(settings: Settings):
    settings.firma(FIRMA_ID).bestellnummer_in_nachbemerkung = False
    fake = _fake()
    ergebnis = _schreiber(settings, fake).schreibe_einkauf_bestellung(_paket(), dry_run=False)
    assert ergebnis.erfolgreich and ergebnis.nachbemerkung is None
    assert "klicke:REGISTER_NACHBEMERKUNG" not in fake.aktionen
    assert "NACHBEMERKUNG" not in fake.gespeicherte_belege[0].felder


# ---------------------------------------------------------------------------
# Angebot mit Positionsrabatt
# ---------------------------------------------------------------------------


def test_angebot_dry_run_mit_rabatt_bricht_vor_speichern_ab(settings: Settings):
    fake = _fake()
    ergebnis = _schreiber(settings, fake).schreibe_angebot(_paket(), dry_run=True)
    assert ergebnis.erfolgreich, ergebnis.fehler
    assert ergebnis.belegart is Belegart.ANGEBOT and ergebnis.dry_run and ergebnis.belegnummer is None
    assert ergebnis.netto == NETTO_VK
    a = fake.aktionen
    i_n = _index(a, "tasten:^n")
    i_art = _index(a, "waehle:AUFTRAGSART:Angebot", ab=i_n)
    i_kd = _index(a, "tippe:KUNDENNUMMER:9", ab=i_art)
    i_pos1 = _index(a, "tippe:ARTIKELNUMMER:4", ab=i_kd)
    i_rab = _index(a, "tippe:RABATT:1", ab=i_pos1)
    assert a[i_rab:i_rab + 2] == ["tippe:RABATT:1", "tippe:RABATT:0"]
    assert i_rab < _index(a, "klicke:POSITION_UEBERNEHMEN", ab=i_pos1)
    # zweite Position ohne Rabatt -> kein Rabattfeld
    i_pos2 = _index(a, "tippe:ARTIKELNUMMER:4", ab=_index(a, "klicke:POSITION_UEBERNEHMEN", ab=i_pos1))
    i_haken2 = _index(a, "klicke:POSITION_UEBERNEHMEN", ab=i_pos2)
    assert not any(x.startswith("tippe:RABATT") for x in a[i_pos2:i_haken2])
    assert "klicke:ABBRECHEN" in a and "klicke:SPEICHERN" not in a
    assert fake.gespeicherte_belege == []
    assert any("Positionsrabatt 10 % (aktion:herbst)" in w for w in ergebnis.warnungen)
    assert any("Summenvergleich bestanden" in w for w in ergebnis.warnungen)


def test_angebot_rabattfeld_fehlt_bricht_ab(settings: Settings):
    fake = _fake(felder_fehlend={"RABATT"})
    ergebnis = _schreiber(settings, fake).schreibe_angebot(_paket(freigegeben=True), dry_run=False)
    assert ergebnis.erfolgreich is False
    assert "RABATT" in (ergebnis.fehler or "")
    assert fake.gespeicherte_belege == []
    assert fake.assistent is not None and fake.assistent.geschlossen


def test_angebot_scharf_speichert_rabatt(settings: Settings):
    fake = _fake()
    ergebnis = _schreiber(settings, fake).schreibe_angebot(_paket(), dry_run=False)
    assert ergebnis.erfolgreich, ergebnis.fehler
    assert ergebnis.belegnummer == "AN-2026-0001"
    beleg = fake.gespeicherte_belege[0]
    assert beleg.auftragsart == "Angebot" and beleg.partnernummer == "90001"
    assert beleg.rabatte == [("4001", Decimal("10"))]
    assert beleg.positionen[0] == ("4001", Decimal("12"), Decimal("6.2100"))
    assert beleg.netto == NETTO_VK


def test_verkauf_ab_direkt_mit_rabatt(settings: Settings):
    fake = _fake()
    ergebnis = _schreiber(settings, fake).schreibe_verkauf_ab(_paket(), dry_run=False)
    assert ergebnis.erfolgreich, ergebnis.fehler
    assert ergebnis.belegnummer == "AB-2026-0001" and ergebnis.netto == NETTO_VK
    assert fake.gespeicherte_belege[0].rabatte == [("4001", Decimal("10"))]
    # direkt_ab: Lieferdatum bleibt gesetzt (bisheriges Verhalten)
    assert fake.gespeicherte_belege[0].felder["LIEFERDATUM"] == "05.11.2026"


# ---------------------------------------------------------------------------
# Angebot -> AB: Weiterführen / Wandeln, Lieferdatum und AB-Text leeren
# ---------------------------------------------------------------------------


def _angebot_scharf(settings: Settings, fake: FakeLexwareBackend) -> tuple[UiSchreiber, Belegergebnis]:
    schreiber = _schreiber(settings, fake)
    angebot = schreiber.schreibe_angebot(_paket(), dry_run=False)
    assert angebot.erfolgreich, angebot.fehler
    return schreiber, angebot


def test_weiterfuehren_zu_ab_scharf_leert_lieferdatum_und_ab_text(settings: Settings):
    fake = _fake()
    schreiber, angebot = _angebot_scharf(settings, fake)
    start = len(fake.aktionen)

    ab = schreiber.angebot_zu_ab(angebot, _paket(), weg="angebot_weiterfuehren", dry_run=False)

    assert ab.erfolgreich, ab.fehler
    assert ab.belegart is Belegart.AUFTRAGSBESTAETIGUNG and ab.belegnummer == "AB-2026-0001"
    assert ab.netto == NETTO_VK
    a = fake.aktionen
    i_mark = _index(a, "markiere:AUFTRAGSLISTE_VERKAUF:AN-2026-0001", ab=start)
    i_wf = _index(a, "menue:Bearbeiten -> Aufträge -> Weiterführen", ab=i_mark)
    i_art = _index(a, "waehle:AUFTRAGSART:Auftragsbestätigung", ab=i_wf)
    i_ld = _index(a, "setze:LIEFERDATUM:", ab=i_art)
    i_txt = _index(a, "setze:AB_TEXT:", ab=i_ld)
    assert i_txt < _index(a, "klicke:WEITER", ab=i_art)
    assert not any(x.startswith("tippe:ARTIKELNUMMER") for x in a[start:])  # Positionen kommen aus dem Angebot
    assert "klicke:SPEICHERN" in a[start:]
    angebot_beleg = fake.beleg("AN-2026-0001")
    assert angebot_beleg is not None and angebot_beleg.status == "W"
    ab_beleg = fake.beleg("AB-2026-0001")
    assert ab_beleg is not None
    assert ab_beleg.felder["LIEFERDATUM"] == "" and ab_beleg.felder["AB_TEXT"] == ""
    assert ab_beleg.felder["BESTELLNUMMER"] == "B-0001"
    assert ab_beleg.rabatte == [("4001", Decimal("10"))]
    assert any("Lieferdatum (Fixtermin steht in der Fixterminüberwachung) geleert" in w for w in ab.warnungen)
    assert any("AB-Text geleert" in w for w in ab.warnungen)


def test_weiterfuehren_ueber_schaltflaeche_wenn_menue_fehlt(settings: Settings, monkeypatch: pytest.MonkeyPatch):
    fake = _fake()
    original = fake.oeffne_menue

    def ohne_menue(pfad):
        if tuple(pfad) == tuple(fake.controls.MENUE_AUFTRAG_WEITERFUEHREN):
            fake.aktionen.append("menue:" + " -> ".join(pfad))
            raise ControlNichtGefunden("Menüeintrag 'Weiterführen' nicht gefunden (Fake)")
        return original(pfad)

    monkeypatch.setattr(fake, "oeffne_menue", ohne_menue)
    schreiber, angebot = _angebot_scharf(settings, fake)
    ab = schreiber.angebot_zu_ab(angebot, _paket(), weg="angebot_weiterfuehren", dry_run=False)
    assert ab.erfolgreich, ab.fehler
    assert "klicke:WEITERFUEHREN_BUTTON" in fake.aktionen
    assert any("Schaltfläche 'Weiterführen'" in w for w in ab.warnungen)


def test_wandeln_zu_ab_scharf(settings: Settings):
    fake = _fake()
    schreiber, angebot = _angebot_scharf(settings, fake)
    ab = schreiber.angebot_zu_ab(angebot, _paket(), weg="angebot_wandeln", dry_run=False)
    assert ab.erfolgreich, ab.fehler
    a = fake.aktionen
    i_w = _index(a, "menue:Bearbeiten -> Aufträge -> Wandeln")
    i_z = _index(a, "waehle:WANDELN_ZIELART:Auftragsbestätigung", ab=i_w)
    i_ok = _index(a, "klicke:DIALOG_OK", ab=i_z)
    assert a[i_ok + 1] == "gewandelt:AN-2026-0001:Auftragsbestätigung"
    assert "menue:Bearbeiten -> Aufträge -> Weiterführen" not in a
    assert fake.dialoge == []
    ab_beleg = fake.beleg("AB-2026-0001")
    assert ab_beleg is not None and ab_beleg.felder["LIEFERDATUM"] == "" and ab_beleg.felder["AB_TEXT"] == ""


def test_angebot_zu_ab_ohne_leeren_laut_firma(settings: Settings):
    firma = settings.firma(FIRMA_ID)
    firma.lieferdatum_in_ab_loeschen = False
    firma.ab_text_loeschen = False
    fake = _fake()
    schreiber, angebot = _angebot_scharf(settings, fake)
    ab = schreiber.angebot_zu_ab(angebot, _paket(), dry_run=False)
    assert ab.erfolgreich, ab.fehler
    ab_beleg = fake.beleg("AB-2026-0001")
    assert ab_beleg is not None
    assert ab_beleg.felder["LIEFERDATUM"] == "05.11.2026" and ab_beleg.felder["AB_TEXT"] == AB_STANDARDTEXT
    assert "setze:LIEFERDATUM:" not in fake.aktionen and "setze:AB_TEXT:" not in fake.aktionen


def test_angebot_zu_ab_dry_run_simuliert_ab_direkt_mit_geleerten_feldern(settings: Settings):
    fake = _fake()
    schreiber = _schreiber(settings, fake)
    angebot = schreiber.schreibe_angebot(_paket(), dry_run=True)
    assert angebot.erfolgreich and angebot.belegnummer is None
    start = len(fake.aktionen)

    ab = schreiber.angebot_zu_ab(angebot, _paket(), dry_run=True)

    assert ab.erfolgreich, ab.fehler
    assert ab.dry_run and ab.belegnummer is None and ab.netto == NETTO_VK
    a = fake.aktionen[start:]
    assert "tasten:^n" in a and not any(x.startswith("menue:Bearbeiten") for x in a)
    assert "waehle:AUFTRAGSART:Auftragsbestätigung" in a
    assert "setze:LIEFERDATUM:" in a and not any(x.startswith("tippe:LIEFERDATUM") for x in a)
    assert "setze:AB_TEXT:" in a
    assert "klicke:ABBRECHEN" in a and "klicke:SPEICHERN" not in a
    assert fake.gespeicherte_belege == []
    assert fake.assistent is not None and fake.assistent.geschlossen
    assert fake.assistent.felder["LIEFERDATUM"] == "" and fake.assistent.felder["AB_TEXT"] == ""
    assert any("AB-Assistent wird direkt simuliert" in w for w in ab.warnungen)


def test_angebot_zu_ab_scharf_ohne_angebotsnummer_verweigert(settings: Settings):
    fake = _fake()
    schreiber = _schreiber(settings, fake)
    angebot = schreiber.schreibe_angebot(_paket(), dry_run=True)
    ab = schreiber.angebot_zu_ab(angebot, _paket(), dry_run=False)
    assert ab.erfolgreich is False
    assert "Belegnummer" in (ab.fehler or "")
    assert fake.gespeicherte_belege == []


def test_angebot_zu_ab_unbekannter_weg(settings: Settings):
    fake = _fake()
    schreiber, angebot = _angebot_scharf(settings, fake)
    ab = schreiber.angebot_zu_ab(angebot, _paket(), weg="irgendwas", dry_run=False)  # type: ignore[arg-type]
    assert ab.erfolgreich is False and "Verkaufsweg" in (ab.fehler or "")
    assert len(fake.gespeicherte_belege) == 1


def test_angebot_zu_ab_positionen_weichen_ab_bricht_ab(settings: Settings):
    fake = _fake()
    schreiber, angebot = _angebot_scharf(settings, fake)
    anderes = _paket()
    anderes.positionen = anderes.positionen[:1]
    ab = schreiber.angebot_zu_ab(angebot, anderes, dry_run=False)
    assert ab.erfolgreich is False
    assert "weichen vom Angebot" in (ab.fehler or "")
    assert len(fake.gespeicherte_belege) == 1  # keine AB gespeichert
    assert fake.assistent is not None and fake.assistent.geschlossen


# ---------------------------------------------------------------------------
# PDF-Ausgabe
# ---------------------------------------------------------------------------

DATEINAME = "Muster – Beispiel – Cde – 24.10.2026.pdf"


def _pdf_einstellungen(settings: Settings, modus: str, drucker: str = "Microsoft Print to PDF") -> None:
    settings.lexware.pdf_ausgabe = modus  # type: ignore[assignment]
    settings.lexware.pdf_drucker = drucker


def test_pdf_drucker_scharf(settings: Settings):
    _pdf_einstellungen(settings, "drucker")
    fake = _fake()
    schreiber, angebot = _angebot_scharf(settings, fake)
    start = len(fake.aktionen)

    pfad = schreiber.pdf_ausgeben(angebot, formular="WS_EN", dateiname=DATEINAME, dry_run=False)

    assert pfad == Path(settings.lexware.pdf_ausgabe_ordner) / DATEINAME
    assert pfad.exists() and pfad.read_bytes().startswith(b"%PDF")
    assert angebot.pdf == pfad
    assert fake.papier == [], "nie auf Papier drucken"
    assert len(fake.gedruckt) == 1
    druck = fake.gedruckt[0]
    assert druck["beleg"] == "AN-2026-0001" and druck["formular"] == "WS_EN" and druck["drucker"] == "Microsoft Print to PDF"
    a = fake.aktionen
    i_mark = _index(a, "markiere:AUFTRAGSLISTE_VERKAUF:AN-2026-0001", ab=start)
    i_menu = _index(a, "menue:Datei -> Drucken", ab=i_mark)
    i_form = _index(a, "waehle:DRUCK_FORMULAR:WS_EN", ab=i_menu)
    i_dr = _index(a, "waehle:DRUCK_DRUCKER:Microsoft Print to PDF", ab=i_form)
    i_start = _index(a, "klicke:DRUCK_STARTEN", ab=i_dr)
    i_name = _index(a, f"setze:DATEIDIALOG_NAME:{pfad}", ab=i_start)
    assert _index(a, "klicke:DATEIDIALOG_SPEICHERN", ab=i_name) > i_name
    assert fake.dialoge == []
    assert any(w.startswith("Ablauf: PDF erzeugt:") for w in angebot.warnungen)
    beleg = fake.beleg("AN-2026-0001")
    assert beleg is not None and "D" in beleg.status.split()


def test_pdf_drucker_nie_ueberschreiben(settings: Settings):
    _pdf_einstellungen(settings, "drucker")
    fake = _fake()
    schreiber, angebot = _angebot_scharf(settings, fake)
    vorhanden = Path(settings.lexware.pdf_ausgabe_ordner) / DATEINAME
    vorhanden.parent.mkdir(parents=True)
    vorhanden.write_bytes(b"alt")
    pfad = schreiber.pdf_ausgeben(angebot, formular="WS_EN", dateiname=DATEINAME, dry_run=False)
    assert pfad is not None and pfad.name == "Muster – Beispiel – Cde – 24.10.2026 (2).pdf"
    assert vorhanden.read_bytes() == b"alt"
    assert any("existiert bereits" in w for w in angebot.warnungen)


def test_pdf_drucker_formular_fehlt_nur_warnung_und_dialoge_geschlossen(settings: Settings):
    _pdf_einstellungen(settings, "drucker")
    fake = _fake(formulare=["WIK EN", "WIK AB_LS"])
    schreiber, angebot = _angebot_scharf(settings, fake)
    pfad = schreiber.pdf_ausgeben(angebot, formular="WS_EN", dateiname=DATEINAME, dry_run=False)
    assert pfad is None
    assert not (Path(settings.lexware.pdf_ausgabe_ordner) / DATEINAME).exists()
    assert fake.gedruckt == [] and fake.papier == []
    assert fake.dialoge == [], "Druckdialog muss abgebrochen sein"
    assert "klicke:DIALOG_ABBRECHEN" in fake.aktionen
    assert any("PDF-Ausgabe" in w and "fehlgeschlagen" in w and "WS_EN" in w for w in angebot.warnungen)
    assert any(w.startswith("Screenshot:") for w in angebot.warnungen)


def test_pdf_drucker_ohne_pdf_drucker_verweigert(settings: Settings):
    _pdf_einstellungen(settings, "drucker", drucker="Standarddrucker (Papier)")
    fake = _fake()
    schreiber, angebot = _angebot_scharf(settings, fake)
    start = len(fake.aktionen)
    pfad = schreiber.pdf_ausgeben(angebot, formular="WS_EN", dateiname=DATEINAME, dry_run=False)
    assert pfad is None
    assert fake.papier == [] and fake.gedruckt == []
    assert not any(x.startswith("menue:Datei -> Drucken") for x in fake.aktionen[start:])
    assert any("Papierdruck ist ausgeschlossen" in w for w in angebot.warnungen)


def test_pdf_export_scharf(settings: Settings):
    _pdf_einstellungen(settings, "export")
    fake = _fake()
    schreiber, angebot = _angebot_scharf(settings, fake)
    pfad = schreiber.pdf_ausgeben(angebot, formular="WS_EN", dateiname=DATEINAME, dry_run=False)
    assert pfad is not None and pfad.exists()
    assert len(fake.exportiert) == 1 and fake.exportiert[0]["formular"] == "WS_EN"
    assert fake.gedruckt == [] and fake.papier == []
    a = fake.aktionen
    i_menu = _index(a, "menue:Datei -> Export -> PDF")
    i_form = _index(a, "waehle:EXPORT_FORMULAR:WS_EN", ab=i_menu)
    i_name = _index(a, f"setze:EXPORT_DATEINAME:{pfad}", ab=i_form)
    assert _index(a, "klicke:EXPORT_OK", ab=i_name) > i_name


def test_pdf_keine_nur_protokoll(settings: Settings):
    _pdf_einstellungen(settings, "keine")
    fake = _fake()
    schreiber, angebot = _angebot_scharf(settings, fake)
    start = len(fake.aktionen)
    pfad = schreiber.pdf_ausgeben(angebot, formular="WS_EN", dateiname=DATEINAME, dry_run=False)
    assert pfad is None
    assert fake.aktionen[start:] == []
    assert any("abgeschaltet (pdf_ausgabe=keine)" in w and DATEINAME in w for w in angebot.warnungen)
    assert angebot.pdf is None


def test_pdf_dry_run_liefert_nur_geplanten_pfad(settings: Settings):
    _pdf_einstellungen(settings, "drucker")
    fake = _fake()
    schreiber = _schreiber(settings, fake)
    angebot = schreiber.schreibe_angebot(_paket(), dry_run=True)
    start = len(fake.aktionen)
    pfad = schreiber.pdf_ausgeben(angebot, formular="WS_EN", dateiname=DATEINAME, dry_run=True)
    assert pfad == Path(settings.lexware.pdf_ausgabe_ordner) / DATEINAME
    assert not pfad.exists()
    assert fake.aktionen[start:] == []
    assert any("Dry Run: PDF" in w and "geplant, nicht erzeugt" in w for w in angebot.warnungen)


def test_pdf_scharf_ohne_belegnummer_nur_warnung(settings: Settings):
    _pdf_einstellungen(settings, "drucker")
    fake = _fake()
    schreiber = _schreiber(settings, fake)
    angebot = schreiber.schreibe_angebot(_paket(), dry_run=True)  # nicht gespeichert
    pfad = schreiber.pdf_ausgeben(angebot, formular="WS_EN", dateiname=DATEINAME, dry_run=False)
    assert pfad is None
    assert any("ohne Belegnummer" in w for w in angebot.warnungen)
    assert fake.gedruckt == []


def test_pdf_einkauf_aus_einkaufsliste(settings: Settings):
    _pdf_einstellungen(settings, "drucker")
    fake = _fake()
    schreiber = _schreiber(settings, fake)
    einkauf = schreiber.schreibe_einkauf_bestellung(_paket(), dry_run=False)
    assert einkauf.erfolgreich
    pfad = schreiber.pdf_ausgeben(einkauf, formular="WIK EN", dateiname="Muster – WIK – B-0001 – Cde – 24.10.2026.pdf", dry_run=False)
    assert pfad is not None and pfad.exists()
    assert "markiere:AUFTRAGSLISTE_EINKAUF:BE-2026-0001" in fake.aktionen
    assert fake.gedruckt[0]["beleg"] == "BE-2026-0001" and fake.gedruckt[0]["formular"] == "WIK EN"


def test_pdf_datei_erscheint_nicht_ist_fehler(settings: Settings, monkeypatch: pytest.MonkeyPatch):
    _pdf_einstellungen(settings, "drucker")
    fake = _fake()
    schreiber, angebot = _angebot_scharf(settings, fake)
    monkeypatch.setattr(FakeLexwareBackend, "_pdf_schreiben", lambda self, d, pfad_text, liste: None)
    pfad = schreiber.pdf_ausgeben(angebot, formular="WS_EN", dateiname=DATEINAME, dry_run=False)
    assert pfad is None
    assert any("wurde nicht erzeugt" in w for w in angebot.warnungen)
