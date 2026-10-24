"""Tests für lexware.dry_run: Angebot, Angebot -> AB, Nachbemerkung, Lieferschein-Freigabe und PDF-Vorschau."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from auftragsautomat.config import Settings
from auftragsautomat.lexware.dry_run import VORSCHAU_ENDUNG, DryRunSchreiber, positionszeilen, rabattzeilen
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


@pytest.fixture(autouse=True)
def _pdf_ordner(settings: Settings, tmp_path: Path) -> None:
    settings.lexware.pdf_ausgabe_ordner = tmp_path / "pdf"


def _paket(settings: Settings, *, bestellnummer: str | None = "B-0001") -> Belegpaket:
    quelle = settings.eingang_ordner / "bestellung_beispiel.pdf"
    quelle.write_bytes(b"%PDF")
    roh = RohBestellung(
        eingangstyp=Eingangstyp.KUNDENBESTELLUNG_PDF, quelldatei=quelle, extraktionsmethode=Extraktionsmethode.PDFPLUMBER,
        kunde_text="Weinhandel Beispiel", bestellnummer=bestellnummer, datum=date(2026, 10, 1), liefertag=date(2026, 11, 5),
        positionen=[
            RohPosition(laufnr=1, bezeichnung="Muster Rotwein", menge=Decimal("12"), preis=Decimal("6.90")),
            RohPosition(laufnr=2, bezeichnung="Muster Weißwein", menge=Decimal("6"), preis=Decimal("5.90")),
        ],
    )
    rot = Artikel(firma_id=FIRMA_ID, artikelnummer="4001", bezeichnung="Muster Rotwein 0,75 l", ek_preis=Decimal("4.00"), vk_preis_1=Decimal("6.90"))
    weiss = Artikel(firma_id=FIRMA_ID, artikelnummer="4002", bezeichnung="Muster Weißwein 0,75 l", ek_preis=Decimal("3.50"), vk_preis_1=Decimal("5.90"))
    return Belegpaket(
        roh=roh,
        firma_id=FIRMA_ID,
        kunde=KundeZuordnung(kunde=Kunde(firma_id=FIRMA_ID, kundennummer="90001", name="Weinhandel Beispiel GmbH", matchcode="Beispiel"), status=MatchStatus.SICHER),
        lieferant=LieferantZuordnung(lieferant=Lieferant(firma_id=FIRMA_ID, lieferantennummer="80001", name="Weingut Muster", matchcode="Muster"), status=MatchStatus.SICHER),
        positionen=[
            PositionsZuordnung(roh=roh.positionen[0], artikel=rot, status=MatchStatus.SICHER, menge=Decimal("12"), ek_preis=Decimal("4.00"),
                               vk_preis=Decimal("6.90"), preis_dokument=Decimal("6.90"), rabatt_prozent=Decimal("10"), rabatt_quelle="aktion:herbst"),
            PositionsZuordnung(roh=roh.positionen[1], artikel=weiss, status=MatchStatus.SICHER, menge=Decimal("6"), ek_preis=Decimal("3.50"),
                               vk_preis=Decimal("5.90"), preis_dokument=Decimal("5.90")),
        ],
        belegfelder=Belegfelder(spedition="Spedition Beispiel", weingut="Weingut Muster", shipment="05.11.2026", kunde="Weinhandel Beispiel"),
        freigegeben=True,
        lieferschein_erstellen=True,
    )


def test_positionszeilen_und_rabattzeilen(settings: Settings):
    paket = _paket(settings)
    assert positionszeilen(paket, "ek") == ["Artikel 4001 x 12 @ 4,00", "Artikel 4002 x 6 @ 3,50"]
    assert positionszeilen(paket, "vk") == ["Artikel 4001 x 12 @ 6,90 - 10 % = 6,21", "Artikel 4002 x 6 @ 5,90"]
    assert rabattzeilen(paket) == ["Pos. 1 (4001): 10 % (aktion:herbst)"]


def test_angebot_simuliert(settings: Settings):
    schreiber = DryRunSchreiber(settings, settings.firma(FIRMA_ID))
    ergebnis = schreiber.schreibe_angebot(_paket(settings))
    assert ergebnis.erfolgreich and ergebnis.dry_run and ergebnis.belegnummer is None
    assert ergebnis.belegart is Belegart.ANGEBOT and ergebnis.methode == "dry_run"
    assert ergebnis.netto == Decimal("109.92") and ergebnis.brutto == Decimal("130.80")
    assert "Artikel 4001 x 12 @ 6,90 - 10 % = 6,21" in ergebnis.warnungen
    assert "Rabatte je Position: Pos. 1 (4001): 10 % (aktion:herbst)" in ergebnis.warnungen
    assert any("Angebot -> Weiterführen -> Auftragsbestätigung" in w for w in ergebnis.warnungen)
    assert "Lieferdatum: 05.11.2026" in ergebnis.warnungen


def test_angebot_unvollstaendig(settings: Settings):
    schreiber = DryRunSchreiber(settings, settings.firma(FIRMA_ID))
    paket = _paket(settings)
    paket.kunde = KundeZuordnung()
    ergebnis = schreiber.schreibe_angebot(paket)
    assert ergebnis.erfolgreich is False and "Kein Kunde" in (ergebnis.fehler or "")


def test_angebot_zu_ab_simuliert_mit_xml(settings: Settings):
    schreiber = DryRunSchreiber(settings, settings.firma(FIRMA_ID))
    paket = _paket(settings)
    angebot = schreiber.schreibe_angebot(paket)
    ab = schreiber.angebot_zu_ab(angebot, paket, weg="angebot_weiterfuehren", dry_run=False)
    assert ab.erfolgreich and ab.dry_run and ab.belegart is Belegart.AUFTRAGSBESTAETIGUNG
    assert ab.methode == "opentrans" and ab.artefakt is not None and ab.artefakt.exists()
    assert ab.warnungen[0] == "Dry Run: Angebot -> Weiterführen -> Auftragsbestätigung simuliert"
    assert "Lieferdatum in der AB geleert: ja; AB-Text geleert: ja" in ab.warnungen
    assert any("trotzdem nur simuliert" in w for w in ab.warnungen)
    assert b"<PRICE_AMOUNT>6.21</PRICE_AMOUNT>" in ab.artefakt.read_bytes()

    wandeln = schreiber.angebot_zu_ab(angebot, paket, weg="angebot_wandeln")
    assert wandeln.warnungen[0] == "Dry Run: Angebot -> Auftrag wandeln -> Auftragsbestätigung simuliert"


def test_angebot_zu_ab_unbekannter_weg(settings: Settings):
    schreiber = DryRunSchreiber(settings, settings.firma(FIRMA_ID))
    paket = _paket(settings)
    angebot = schreiber.schreibe_angebot(paket)
    ab = schreiber.angebot_zu_ab(angebot, paket, weg="direkt_ab")  # type: ignore[arg-type]
    assert ab.erfolgreich is False and "Verkaufsweg" in (ab.fehler or "")


def test_einkauf_nachbemerkung(settings: Settings):
    schreiber = DryRunSchreiber(settings, settings.firma(FIRMA_ID))
    ergebnis = schreiber.schreibe_einkauf_bestellung(_paket(settings))
    assert ergebnis.erfolgreich and ergebnis.nachbemerkung == "B-0001"
    assert "Nachbemerkung: B-0001 (Kundenbestellnummer)" in ergebnis.warnungen
    assert "Artikel 4001 x 12 @ 4,00" in ergebnis.warnungen  # EK ohne Rabatt

    ohne_nummer = schreiber.schreibe_einkauf_bestellung(_paket(settings, bestellnummer=None))
    assert ohne_nummer.nachbemerkung is None
    assert any(w.startswith("Nachbemerkung: leer") for w in ohne_nummer.warnungen)

    settings.firma(FIRMA_ID).bestellnummer_in_nachbemerkung = False
    ohne = DryRunSchreiber(settings, settings.firma(FIRMA_ID)).schreibe_einkauf_bestellung(_paket(settings))
    assert ohne.nachbemerkung is None
    assert "Nachbemerkung: keine (bestellnummer_in_nachbemerkung=False)" in ohne.warnungen


def test_lieferschein_nicht_erlaubt(settings: Settings):
    settings.firma(FIRMA_ID).lieferschein_erlaubt = False
    schreiber = DryRunSchreiber(settings, settings.firma(FIRMA_ID))
    ergebnis = schreiber.auftrag_weiterfuehren_lieferschein(_paket(settings), "AB-2026-0001")
    assert ergebnis.erfolgreich is False and "nicht vorgesehen" in (ergebnis.fehler or "")


def test_pdf_vorschau(settings: Settings):
    schreiber = DryRunSchreiber(settings, settings.firma(FIRMA_ID))
    einkauf = schreiber.schreibe_einkauf_bestellung(_paket(settings))
    name = "Muster – WIK – B-0001 – Cde – 24.10.2026.pdf"
    pfad = schreiber.pdf_ausgeben(einkauf, formular="WIK EN", dateiname=name, dry_run=True)
    assert pfad == Path(settings.lexware.pdf_ausgabe_ordner) / name
    assert not pfad.exists(), "Dry Run erzeugt keine PDF"
    vorschau = pfad.with_suffix(VORSCHAU_ENDUNG)
    assert vorschau.exists()
    text = vorschau.read_text(encoding="utf-8")
    assert "Formular: WIK EN" in text and "Belegart: BE" in text and "Nachbemerkung: B-0001" in text
    assert "Artikel 4001 x 12 @ 4,00" in text
    assert einkauf.pdf == pfad
    assert any("nur geplant; Vorschau:" in w for w in einkauf.warnungen)
    assert not any(w.startswith("Ablauf:") for w in einkauf.warnungen)

    # scharf angefordert -> trotzdem nur Vorschau, mit Hinweis
    beleg = Belegergebnis(belegart=Belegart.AUFTRAGSBESTAETIGUNG, erfolgreich=True, dry_run=True, methode="dry_run")
    pfad2 = schreiber.pdf_ausgeben(beleg, formular=None, dateiname="Muster – WIK – B-0001 – Kopie – 24.10.2026.pdf", dry_run=False)
    assert pfad2 is not None and not pfad2.exists()
    assert any("trotzdem nur simuliert" in w for w in beleg.warnungen)
    assert "Lexware-Standardformular" in pfad2.with_suffix(VORSCHAU_ENDUNG).read_text(encoding="utf-8")


def test_pdf_vorschau_nicht_schreibbar_nur_warnung(settings: Settings, tmp_path: Path):
    datei = tmp_path / "keine-datei"
    datei.write_text("x")
    settings.lexware.pdf_ausgabe_ordner = datei / "pdf"  # Elternpfad ist eine Datei -> mkdir scheitert
    schreiber = DryRunSchreiber(settings, settings.firma(FIRMA_ID))
    beleg = Belegergebnis(belegart=Belegart.BESTELLUNG, erfolgreich=True, dry_run=True, methode="dry_run")
    pfad = schreiber.pdf_ausgeben(beleg, formular="WIK EN", dateiname="x.pdf", dry_run=True)
    assert pfad is not None
    assert any("Vorschau nicht schreibbar" in w for w in beleg.warnungen)
