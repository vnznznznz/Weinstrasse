"""Tests für lexware.buchen: Lexware-Prozess laut Ablaufplan (docs/AUSBAUSTUFE_2.md, Abschnitt 5).

Geprüft werden die Reihenfolge je Firma (WS: kein Einkauf, Angebot mit WS_EN x2, AB mit WS_AB, kein
Lieferschein; WIK: Einkauf mit WIK EN x2 und Nachbemerkung, Angebot -> Weiterführen -> AB, WIK AB_LS,
Lieferschein), der unveränderte Weg ``direkt_ab``, Revised Order, Dry Run (nichts gespeichert, PDFs nur
geplant) und die Protokollspalten. Alle Daten sind synthetisch (Firma Beispiel, Weingut Muster).
"""

from __future__ import annotations

import csv
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from auftragsautomat import benennung
from auftragsautomat.config import Settings
from auftragsautomat.lexware import buchen
from auftragsautomat.lexware.basis import nachbemerkung_einkauf, pdf_zielpfad
from auftragsautomat.lexware.buchen import buche as _buche
from auftragsautomat.lexware.dry_run import VORSCHAU_ENDUNG
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
    RohBestellung,
    RohPosition,
    Schweregrad,
)
from auftragsautomat.protokoll import zusammenfassung
from auftragsautomat.stammdaten.basis import Stammdaten

WIK = "wik_leh"
WS = "weinstrasse_agentur"
#: Fester Buchungszeitpunkt (Review Befund 17): Dateinamen, Archivjahr und Protokoll hängen nicht an der Wanduhr.
JETZT = datetime(2026, 10, 21, 10, 0)
HEUTE = JETZT.date()


def buche(*args, **kwargs):
    """``buchen.buche`` mit fester Uhr (``jetzt``), damit die Tests über Mitternacht stabil bleiben."""
    kwargs.setdefault("jetzt", lambda: JETZT)
    return _buche(*args, **kwargs)


@pytest.fixture(autouse=True)
def _pdf_ordner_umleiten(settings: Settings, tmp_path: Path) -> None:
    settings.lexware.pdf_ausgabe_ordner = tmp_path / "logs" / "pdf"


def _artikel(firma_id: str, nummer: str, bezeichnung: str, ek: str, vk: str) -> Artikel:
    return Artikel(
        firma_id=firma_id, artikelnummer=nummer, bezeichnung=bezeichnung, warengruppe="Muster",
        lieferantennummer="80001", volumen_cl=Decimal("75"), ek_preis=Decimal(ek), vk_preis_1=Decimal(vk), einheit="Fl",
    )


def _kunde(firma_id: str) -> Kunde:
    return Kunde(firma_id=firma_id, kundennummer="90001", name="Weinhandel Beispiel GmbH", matchcode="Beispiel", ort="Musterstadt", land="DE")


def _lieferant(firma_id: str) -> Lieferant:
    return Lieferant(firma_id=firma_id, lieferantennummer="80001", name="Weingut Muster", matchcode="Muster", land="IT")


def _paket(
    settings: Settings,
    firma_id: str = WIK,
    *,
    bestellnummer: str | None = "B-0001",
    lieferschein: bool = True,
    rabatt: bool = True,
    revision: bool = False,
    revision_von: str | None = None,
    name: str = "bestellung_beispiel.pdf",
) -> Belegpaket:
    quelle = settings.eingang_ordner / firma_id / name
    quelle.parent.mkdir(parents=True, exist_ok=True)
    quelle.write_bytes(b"%PDF-1.4 testdatei")
    roh = RohBestellung(
        eingangstyp=Eingangstyp.KUNDENBESTELLUNG_PDF,
        quelldatei=quelle,
        extraktionsmethode=Extraktionsmethode.PDFPLUMBER,
        kunde_text="Weinhandel Beispiel",
        lieferant_text="Muster",
        belegnummer="DOK-77",
        bestellnummer=bestellnummer,
        datum=date(2026, 10, 1),
        liefertag=date(2026, 11, 5),
        positionen=[
            RohPosition(laufnr=1, bezeichnung="Muster Rotwein", menge=Decimal("12"), preis=Decimal("6.90")),
            RohPosition(laufnr=2, bezeichnung="Muster Weißwein", menge=Decimal("6"), preis=Decimal("5.90")),
        ],
        ist_revision=revision,
        revision_von=revision_von,
    )
    rot = _artikel(firma_id, "4001", "Muster Rotwein 0,75 l", "4.00", "6.90")
    weiss = _artikel(firma_id, "4002", "Muster Weißwein 0,75 l", "3.50", "5.90")
    firma = settings.firma(firma_id)
    return Belegpaket(
        roh=roh,
        firma_id=firma_id,
        kunde=KundeZuordnung(kunde=_kunde(firma_id), status=MatchStatus.SICHER, quelle="alias"),
        lieferant=LieferantZuordnung(lieferant=_lieferant(firma_id), status=MatchStatus.SICHER, quelle="alias"),
        positionen=[
            PositionsZuordnung(
                roh=roh.positionen[0], artikel=rot, status=MatchStatus.SICHER, menge=Decimal("12"),
                ek_preis=Decimal("4.00"), vk_preis=Decimal("6.90"), preis_dokument=Decimal("6.90"),
                rabatt_prozent=Decimal("10") if rabatt else None, rabatt_quelle="aktion:herbst" if rabatt else None,
            ),
            PositionsZuordnung(
                roh=roh.positionen[1], artikel=weiss, status=MatchStatus.SICHER, menge=Decimal("6"),
                ek_preis=Decimal("3.50"), vk_preis=Decimal("5.90"), preis_dokument=Decimal("5.90"),
            ),
        ],
        belegfelder=Belegfelder(spedition="Spedition Beispiel", weingut="Weingut Muster", shipment="05.11.2026", kunde="Weinhandel Beispiel"),
        einkauf_erforderlich=firma.einkauf_bestellung,
        lieferschein_erstellen=lieferschein,
        freigegeben=True,
    )


def _stammdaten(firma_id: str, belege: list[Beleg] | None = None) -> Stammdaten:
    return Stammdaten(
        firma_id,
        artikel=[_artikel(firma_id, "4001", "Muster Rotwein 0,75 l", "4.00", "6.90"), _artikel(firma_id, "4002", "Muster Weißwein 0,75 l", "3.50", "5.90")],
        kunden=[_kunde(firma_id)],
        lieferanten=[_lieferant(firma_id)],
        belege=belege or [],
    )


def _protokoll_zeilen(settings: Settings) -> list[dict[str, str]]:
    with (settings.log_ordner / "protokoll.csv").open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f, delimiter=";"))


def _datum(settings: Settings) -> str:
    return benennung.datum_text(HEUTE, settings)


class ProzessSchreiber:
    """Erfüllt das vollständige LexwareSchreiber-Protokoll; protokolliert jeden Aufruf.

    ``pdf_ausgeben`` legt scharf eine Datei im pdf_ausgabe_ordner an, im Dry Run liefert es nur den Pfad.
    """

    def __init__(self, settings: Settings, firma_id: str, *, angebot_ok: bool = True, ab_ok: bool = True, pdf_werfen: bool = False) -> None:
        self.settings = settings
        self.firma = settings.firma(firma_id)
        self.angebot_ok = angebot_ok
        self.ab_ok = ab_ok
        self.pdf_werfen = pdf_werfen
        self.aufrufe: list[tuple] = []
        self._nr = 0

    def _nummer(self, praefix: str) -> str:
        self._nr += 1
        return f"{praefix}-2026-{self._nr:04d}"

    def _ergebnis(self, belegart: Belegart, ok: bool, praefix: str, netto: Decimal, dry_run: bool, **extra) -> Belegergebnis:
        return Belegergebnis(
            belegart=belegart, belegnummer=None if (dry_run or not ok) else self._nummer(praefix), netto=netto,
            brutto=(netto * Decimal("1.19")).quantize(Decimal("0.01")), erfolgreich=ok, dry_run=dry_run,
            methode="ui_automation", fehler=None if ok else f"{belegart.value} fehlgeschlagen (Fake)", **extra,
        )

    def schreibe_einkauf_bestellung(self, paket, *, dry_run=True):
        self.aufrufe.append(("einkauf", dry_run))
        return self._ergebnis(Belegart.BESTELLUNG, True, "BE", paket.summe_netto_ek, dry_run, nachbemerkung=nachbemerkung_einkauf(paket, self.firma))

    def schreibe_angebot(self, paket, *, dry_run=True):
        self.aufrufe.append(("angebot", dry_run))
        return self._ergebnis(Belegart.ANGEBOT, self.angebot_ok, "AN", paket.summe_netto_vk, dry_run)

    def angebot_zu_ab(self, angebot, paket, *, weg="angebot_weiterfuehren", dry_run=True):
        self.aufrufe.append(("angebot_zu_ab", weg, dry_run, angebot.belegnummer))
        return self._ergebnis(Belegart.AUFTRAGSBESTAETIGUNG, self.ab_ok, "AB", paket.summe_netto_vk, dry_run)

    def schreibe_verkauf_ab(self, paket, *, dry_run=True):
        self.aufrufe.append(("verkauf", dry_run))
        return self._ergebnis(Belegart.AUFTRAGSBESTAETIGUNG, self.ab_ok, "AB", paket.summe_netto_vk, dry_run)

    def auftrag_weiterfuehren_lieferschein(self, paket, ab_belegnummer, *, dry_run=True):
        self.aufrufe.append(("lieferschein", ab_belegnummer, dry_run))
        return self._ergebnis(Belegart.LIEFERSCHEIN, True, "LS", paket.summe_netto_vk, dry_run)

    def pdf_ausgeben(self, beleg, *, formular, dateiname, dry_run=True):
        self.aufrufe.append(("pdf", beleg.belegart.value, formular, dateiname, dry_run))
        if self.pdf_werfen:
            raise RuntimeError("Druckdialog verschwunden (Fake)")
        ziel = pdf_zielpfad(self.settings, dateiname)
        if not dry_run:
            ziel.parent.mkdir(parents=True, exist_ok=True)
            ziel.write_bytes(b"%PDF-1.4 fake")
        return ziel


class AlterSchreiber:
    """Schreiber aus Ausbaustufe 1 (nur drei Methoden): kennt weder Angebot noch PDF."""

    def __init__(self) -> None:
        self.aufrufe: list[str] = []

    def _ok(self, belegart: Belegart, netto: Decimal, dry_run: bool, nummer: str) -> Belegergebnis:
        return Belegergebnis(belegart=belegart, belegnummer=None if dry_run else nummer, netto=netto, erfolgreich=True, dry_run=dry_run, methode="ui_automation")

    def schreibe_einkauf_bestellung(self, paket, *, dry_run=True):
        self.aufrufe.append("einkauf")
        return self._ok(Belegart.BESTELLUNG, paket.summe_netto_ek, dry_run, "BE-1")

    def schreibe_verkauf_ab(self, paket, *, dry_run=True):
        self.aufrufe.append("verkauf")
        return self._ok(Belegart.AUFTRAGSBESTAETIGUNG, paket.summe_netto_vk, dry_run, "AB-1")

    def auftrag_weiterfuehren_lieferschein(self, paket, ab_belegnummer, *, dry_run=True):
        self.aufrufe.append("lieferschein")
        return self._ok(Belegart.LIEFERSCHEIN, paket.summe_netto_vk, dry_run, "LS-1")


# ---------------------------------------------------------------------------
# Weinstrasse Agentur: Angebot (WS_EN x2) -> Weiterführen -> AB (WS_AB), kein Einkauf, kein Lieferschein
# ---------------------------------------------------------------------------


def test_ws_weg(settings: Settings):
    paket = _paket(settings, WS, lieferschein=True)
    assert paket.einkauf_erforderlich is False
    paket.lieferant = LieferantZuordnung()  # Agentur: kein Lieferant zugeordnet, Weingut nur laut Dokument
    schreiber = ProzessSchreiber(settings, WS)

    ergebnis = buche(paket, settings, dry_run=False, schreiber=schreiber, stammdaten=_stammdaten(WS))

    assert ergebnis.erfolgreich, ergebnis.warnungen
    assert ergebnis.dry_run is False
    arten = [a[0] for a in schreiber.aufrufe]
    assert "einkauf" not in arten and "lieferschein" not in arten and "verkauf" not in arten
    assert arten == ["angebot", "pdf", "pdf", "angebot_zu_ab", "pdf"]
    datum = _datum(settings)
    pdf_aufrufe = [a for a in schreiber.aufrufe if a[0] == "pdf"]
    assert pdf_aufrufe[0][1:4] == ("AN", "WS_EN", f"Muster – Beispiel – Cde – {datum}.pdf")
    assert pdf_aufrufe[1][1:4] == ("AN", "WS_EN", f"Spedition Beispiel – Speditionsauftrag – Muster – Beispiel – {datum}.pdf")
    assert pdf_aufrufe[2][1:4] == ("AB", "WS_AB", f"Muster – Beispiel – Kopie – {datum}.pdf")
    assert schreiber.aufrufe[3] == ("angebot_zu_ab", "angebot_weiterfuehren", False, "AN-2026-0001")
    assert ergebnis.angebot is not None and ergebnis.angebot.belegnummer == "AN-2026-0001"
    assert ergebnis.verkauf is not None and ergebnis.verkauf.belegnummer == "AB-2026-0002"
    assert ergebnis.einkauf is None and ergebnis.lieferschein is None
    assert set(ergebnis.pdfs) == {"Cde", "Speditionsauftrag", "Kopie"}
    assert all(p.exists() for p in ergebnis.pdfs.values())
    assert ergebnis.angebot.pdf == ergebnis.pdfs["Cde"] and ergebnis.verkauf.pdf == ergebnis.pdfs["Kopie"]
    assert any("Lieferschein für Weinstrasse Agentur nicht vorgesehen" in w for w in ergebnis.warnungen)
    assert any("Keine Einkauf-Bestellung" in w for w in ergebnis.warnungen)
    zeile = _protokoll_zeilen(settings)[0]
    assert zeile["Angebot-Nr"] == "AN-2026-0001"
    assert zeile["beleg_verkauf"] == "AB-2026-0002" and zeile["beleg_lieferschein"] == ""
    assert "Cde=" in zeile["Dateien"] and "Kopie=" in zeile["Dateien"] and "Speditionsauftrag=" in zeile["Dateien"]
    assert zeile["Revision"] == "nein"
    assert "Auftragsbestätigung" not in Path(ergebnis.pdfs["Kopie"]).name


# ---------------------------------------------------------------------------
# WIK: Einkauf (WIK EN x2, Nachbemerkung) -> Angebot -> Weiterführen -> AB (WIK AB_LS) -> Lieferschein
# ---------------------------------------------------------------------------


def test_wik_weg(settings: Settings):
    paket = _paket(settings, WIK, lieferschein=True)
    schreiber = ProzessSchreiber(settings, WIK)

    ergebnis = buche(paket, settings, dry_run=False, schreiber=schreiber, stammdaten=_stammdaten(WIK))

    assert ergebnis.erfolgreich, ergebnis.warnungen
    arten = [a[0] for a in schreiber.aufrufe]
    assert arten == ["einkauf", "pdf", "pdf", "angebot", "angebot_zu_ab", "pdf", "lieferschein", "pdf"]
    datum = _datum(settings)
    pdf_aufrufe = [a for a in schreiber.aufrufe if a[0] == "pdf"]
    assert pdf_aufrufe[0][1:4] == ("BE", "WIK EN", f"Muster – WIK – B-0001 – Cde – {datum}.pdf")
    assert pdf_aufrufe[1][1:4] == ("BE", "WIK EN", f"Spedition Beispiel – Speditionsauftrag – Muster – B-0001 – {datum}.pdf")
    assert pdf_aufrufe[2][1:4] == ("AB", "WIK AB_LS", f"Muster – WIK – B-0001 – Kopie – {datum}.pdf")
    assert pdf_aufrufe[3][1:4] == ("LS", None, f"Muster – WIK – B-0001 – Ls – {datum}.pdf")
    assert ("angebot_zu_ab", "angebot_weiterfuehren", False, "AN-2026-0002") in schreiber.aufrufe
    assert ("lieferschein", "AB-2026-0003", False) in schreiber.aufrufe
    assert ergebnis.einkauf is not None and ergebnis.einkauf.nachbemerkung == "B-0001"
    assert ergebnis.einkauf.netto == Decimal("69.00")  # EK ohne Rabatt: 12 x 4,00 + 6 x 3,50
    assert ergebnis.verkauf is not None and ergebnis.verkauf.netto == Decimal("109.92")  # VK mit 10 % auf Pos. 1
    assert set(ergebnis.pdfs) == {"Cde", "Speditionsauftrag", "Kopie", "Ls"}
    assert ergebnis.lieferschein is not None and ergebnis.lieferschein.pdf == ergebnis.pdfs["Ls"]
    zeile = _protokoll_zeilen(settings)[0]
    assert zeile["Angebot-Nr"] == "AN-2026-0002"
    assert zeile["Rabatte"] == "Pos. 1 (4001): 10 % (aktion:herbst)"
    assert zeile["Dateien"].startswith("Cde=Muster – WIK – B-0001 – Cde – ")
    assert zeile["beleg_lieferschein"] == "LS-2026-0004"
    text = zusammenfassung(paket, ergebnis, firma=settings.firma(WIK))
    assert "Verkaufsweg:       angebot_weiterfuehren" in text
    assert "Formulare:         Einkauf EN=WIK EN (x2)" in text and "AB=WIK AB_LS" in text
    assert "Nachbemerkung:     B-0001" in text
    assert "Rabatte je Position (nur im Verkauf, VK bleibt Preisgruppe 1): Pos. 1 (4001): 10 % (aktion:herbst)" in text
    assert "Dateien (PDF laut Titulierung)" in text and "Kopie:" in text
    assert "Angebot:             OK" in text


def test_wik_ohne_kundenbestellnummer_keine_nachbemerkung_und_ohne_nr(settings: Settings):
    paket = _paket(settings, WIK, bestellnummer=None, lieferschein=False)
    paket.roh.belegnummer = None
    schreiber = ProzessSchreiber(settings, WIK)
    ergebnis = buche(paket, settings, dry_run=False, schreiber=schreiber, stammdaten=_stammdaten(WIK))
    assert ergebnis.erfolgreich, ergebnis.warnungen
    assert ergebnis.einkauf is not None and ergebnis.einkauf.nachbemerkung is None
    assert Path(ergebnis.pdfs["Cde"]).name.startswith("Muster – WIK – ohne-Nr – Cde – ")
    assert Path(ergebnis.pdfs["Speditionsauftrag"]).name.startswith("Spedition Beispiel – Speditionsauftrag – Muster – Beispiel – ")


def test_speditionsauftrag_kopien_1_nur_cde(settings: Settings):
    settings.firma(WIK).formulare.speditionsauftrag_kopien = 1
    paket = _paket(settings, WIK, lieferschein=False)
    ergebnis = buche(paket, settings, dry_run=False, schreiber=ProzessSchreiber(settings, WIK), stammdaten=_stammdaten(WIK))
    assert ergebnis.erfolgreich
    assert set(ergebnis.pdfs) == {"Cde", "Kopie"}


def test_einkauf_ohne_en_formular_keine_cde_pdf(settings: Settings):
    settings.firma(WIK).formulare.einkauf_en = None
    paket = _paket(settings, WIK, lieferschein=False)
    ergebnis = buche(paket, settings, dry_run=False, schreiber=ProzessSchreiber(settings, WIK), stammdaten=_stammdaten(WIK))
    assert ergebnis.erfolgreich
    assert "Cde" not in ergebnis.pdfs and "Kopie" in ergebnis.pdfs
    assert any("formulare.einkauf_en" in w for w in ergebnis.warnungen)


# ---------------------------------------------------------------------------
# direkt_ab: bisheriges Verhalten
# ---------------------------------------------------------------------------


def test_direkt_ab_unveraendert(settings: Settings):
    settings.firma(WIK).verkauf_weg = "direkt_ab"
    paket = _paket(settings, WIK, lieferschein=True)
    schreiber = ProzessSchreiber(settings, WIK)
    ergebnis = buche(paket, settings, dry_run=False, schreiber=schreiber, stammdaten=_stammdaten(WIK))
    assert ergebnis.erfolgreich, ergebnis.warnungen
    arten = [a[0] for a in schreiber.aufrufe]
    assert arten == ["einkauf", "pdf", "pdf", "verkauf", "pdf", "lieferschein", "pdf"]
    assert ergebnis.angebot is None
    assert ergebnis.verkauf is not None and ergebnis.verkauf.belegnummer == "AB-2026-0002"
    assert _protokoll_zeilen(settings)[0]["Angebot-Nr"] == ""
    assert any("Verkaufsweg: Auftragsbestätigung direkt" in w for w in ergebnis.warnungen)


def test_angebot_wandeln_wird_durchgereicht(settings: Settings):
    settings.firma(WIK).verkauf_weg = "angebot_wandeln"
    paket = _paket(settings, WIK, lieferschein=False)
    schreiber = ProzessSchreiber(settings, WIK)
    ergebnis = buche(paket, settings, dry_run=False, schreiber=schreiber, stammdaten=_stammdaten(WIK))
    assert ergebnis.erfolgreich
    assert any(a[:2] == ("angebot_zu_ab", "angebot_wandeln") for a in schreiber.aufrufe)


def test_schreiber_ohne_angebot_legt_ab_direkt_an(settings: Settings):
    paket = _paket(settings, WIK, lieferschein=False)
    alt = AlterSchreiber()
    ergebnis = buche(paket, settings, dry_run=False, schreiber=alt, stammdaten=_stammdaten(WIK))
    assert ergebnis.erfolgreich, ergebnis.warnungen
    assert alt.aufrufe == ["einkauf", "verkauf"]
    assert ergebnis.angebot is None and ergebnis.pdfs == {}
    assert any("kennt kein Angebot" in w for w in ergebnis.warnungen)
    assert any("kennt keine PDF-Ausgabe" in w for w in ergebnis.warnungen)


# ---------------------------------------------------------------------------
# Abbrüche: Angebot scharf fehlgeschlagen, Lieferschein nicht erlaubt, PDF-Fehler nur Warnung
# ---------------------------------------------------------------------------


def test_angebot_fehlgeschlagen_scharf_keine_ab(settings: Settings):
    paket = _paket(settings, WIK, lieferschein=True)
    schreiber = ProzessSchreiber(settings, WIK, angebot_ok=False)
    ergebnis = buche(paket, settings, dry_run=False, schreiber=schreiber, stammdaten=_stammdaten(WIK))
    assert ergebnis.erfolgreich is False
    arten = [a[0] for a in schreiber.aufrufe]
    assert "angebot_zu_ab" not in arten and "lieferschein" not in arten
    assert ergebnis.angebot is not None and not ergebnis.angebot.erfolgreich
    assert ergebnis.verkauf is None
    assert any("Angebot fehlgeschlagen" in w and "keine halbe Buchung" in w for w in ergebnis.warnungen)
    assert paket.roh.quelldatei.exists()


def test_angebot_fehlgeschlagen_dry_run_ab_trotzdem(settings: Settings):
    paket = _paket(settings, WIK, lieferschein=False)
    schreiber = ProzessSchreiber(settings, WIK, angebot_ok=False)
    ergebnis = buche(paket, settings, dry_run=True, schreiber=schreiber, stammdaten=_stammdaten(WIK))
    assert ergebnis.erfolgreich is False
    assert ergebnis.verkauf is not None and ergebnis.verkauf.erfolgreich
    assert any("Angebot fehlgeschlagen, Auftragsbestätigung wird trotzdem simuliert" in w for w in ergebnis.warnungen)


def test_lieferschein_fuer_firma_nicht_erlaubt(settings: Settings):
    settings.firma(WIK).lieferschein_erlaubt = False
    paket = _paket(settings, WIK, lieferschein=True)
    schreiber = ProzessSchreiber(settings, WIK)
    ergebnis = buche(paket, settings, dry_run=False, schreiber=schreiber, stammdaten=_stammdaten(WIK))
    assert ergebnis.erfolgreich
    assert "lieferschein" not in [a[0] for a in schreiber.aufrufe]
    assert ergebnis.lieferschein is None and "Ls" not in ergebnis.pdfs
    assert any("nicht vorgesehen (lieferschein_erlaubt=False)" in w for w in ergebnis.warnungen)


def test_pdf_fehler_ist_nur_warnung(settings: Settings):
    paket = _paket(settings, WIK, lieferschein=False)
    schreiber = ProzessSchreiber(settings, WIK, pdf_werfen=True)
    ergebnis = buche(paket, settings, dry_run=False, schreiber=schreiber, stammdaten=_stammdaten(WIK))
    assert ergebnis.erfolgreich, ergebnis.warnungen
    assert ergebnis.pdfs == {}
    assert sum("fehlgeschlagen: RuntimeError" in w for w in ergebnis.warnungen) == 3  # Cde, Speditionsauftrag, Kopie
    assert not paket.roh.quelldatei.exists()  # scharf und erfolgreich -> archiviert


def test_lieferschein_nachziehen_mit_pdf_und_firmenfreigabe(settings: Settings):
    paket = _paket(settings, WIK, lieferschein=True)
    schreiber = ProzessSchreiber(settings, WIK)
    ergebnis = buchen.lieferschein_nachziehen(paket, "AB-2026-0099", settings, dry_run=False, schreiber=schreiber)
    assert ergebnis.erfolgreich and ergebnis.lieferschein is not None
    assert list(ergebnis.pdfs) == ["Ls"]
    assert schreiber.aufrufe[0] == ("lieferschein", "AB-2026-0099", False)
    assert schreiber.aufrufe[1][0:3] == ("pdf", "LS", None)

    settings.firma(WIK).lieferschein_erlaubt = False
    verweigert = buchen.lieferschein_nachziehen(paket, "AB-2026-0099", settings, dry_run=False, schreiber=schreiber)
    assert verweigert.lieferschein is None
    assert any("nicht vorgesehen" in w for w in verweigert.warnungen)


# ---------------------------------------------------------------------------
# Revised Order
# ---------------------------------------------------------------------------


def _alte_ab(firma_id: str, kundenbestellnummer: str, partner: str = "90001") -> Beleg:
    return Beleg(firma_id=firma_id, belegart=Belegart.AUFTRAGSBESTAETIGUNG, belegnummer="AB-2026-0010",
                 kundenbestellnummer=kundenbestellnummer, partnernummer=partner, datum=date(2026, 9, 20))


def test_revision_ersetzte_bestellung_ist_info(settings: Settings):
    paket = _paket(settings, WIK, lieferschein=False, revision=True, revision_von="B-0001")
    paket.ist_revision = True
    schreiber = ProzessSchreiber(settings, WIK)
    stammdaten = _stammdaten(WIK, belege=[_alte_ab(WIK, "B-0001")])

    ergebnis = buche(paket, settings, dry_run=False, schreiber=schreiber, stammdaten=stammdaten)

    assert ergebnis.erfolgreich, ergebnis.warnungen
    assert ergebnis.ist_revision is True
    dubletten = [h for h in paket.hinweise if h.code == Pruefcode.DUBLETTE]
    assert len(dubletten) == 1 and dubletten[0].schweregrad == Schweregrad.INFO
    assert dubletten[0].text.startswith("Revised Order: ersetzt AB-Beleg AB-2026-0010")
    assert not paket.blockiert
    datum = _datum(settings)
    assert Path(ergebnis.pdfs["Cde"]).name == f"Muster – WIK – B-0001 – Neu – {datum}.pdf"
    assert Path(ergebnis.pdfs["Kopie"]).name == f"Muster – WIK – B-0001 – Neu Kopie – {datum}.pdf"
    assert set(ergebnis.pdfs) == {"Cde", "Speditionsauftrag", "Kopie"}
    assert any("Revised Order: Bestellung (Cde) und AB werden neu angelegt" in w and "ersetzt B-0001" in w for w in ergebnis.warnungen)
    zeile = _protokoll_zeilen(settings)[0]
    assert zeile["Revision"] == "ja (ersetzt B-0001)"
    assert "Revision:" in zusammenfassung(paket, ergebnis)


def test_revision_ohne_revision_von_nutzt_eigene_bestellnummer(settings: Settings):
    paket = _paket(settings, WIK, lieferschein=False, revision=True)
    stammdaten = _stammdaten(WIK, belege=[_alte_ab(WIK, "B-0001")])
    ergebnis = buche(paket, settings, dry_run=False, schreiber=ProzessSchreiber(settings, WIK), stammdaten=stammdaten)
    assert ergebnis.erfolgreich, ergebnis.warnungen
    assert ergebnis.ist_revision is True
    assert _protokoll_zeilen(settings)[0]["Revision"] == "ja"


def test_revision_andere_dublette_bleibt_fehler(settings: Settings):
    paket = _paket(settings, WIK, lieferschein=False, revision=True, revision_von="B-9999")
    stammdaten = _stammdaten(WIK, belege=[_alte_ab(WIK, "B-0001")])  # trifft die eigene Nummer, nicht die ersetzte
    ergebnis = buche(paket, settings, dry_run=False, schreiber=ProzessSchreiber(settings, WIK), stammdaten=stammdaten)
    assert ergebnis.erfolgreich is False
    assert ergebnis.verkauf is None and ergebnis.einkauf is None
    assert any("Dublette" in w for w in ergebnis.warnungen)
    assert paket.blockiert


def test_revision_vorhandener_dubletten_hinweis_wird_abgestuft(settings: Settings):
    from auftragsautomat.matching.pruefung import pruefe_dubletten

    paket = _paket(settings, WIK, lieferschein=False, revision=True, revision_von="B-0001")
    stammdaten = _stammdaten(WIK, belege=[_alte_ab(WIK, "B-0001")])
    pruefe_dubletten(paket, stammdaten, eintragen=True)  # wie die Vorprüfung vor der Freigabe
    assert paket.blockiert
    ergebnis = buche(paket, settings, dry_run=False, schreiber=ProzessSchreiber(settings, WIK), stammdaten=stammdaten)
    assert ergebnis.erfolgreich, ergebnis.warnungen


def test_auftragskopie_pdf_name_ohne_verbotene_woerter(settings: Settings):
    """Regression (Review Befund 5): Enthält ein Kurzname ein verbotenes Wort, wird die Kopie-PDF unter einem
    zulässigen Namen ausgegeben (mit Hinweis), nicht unter dem unzulässigen."""
    from auftragsautomat.benennung import pruefe_auftragskopie_name

    paket = _paket(settings, WIK, lieferschein=False)
    paket.lieferant.lieferant.matchcode = "Muster Verbindlich"
    ergebnis = buche(paket, settings, dry_run=False, schreiber=ProzessSchreiber(settings, WIK), stammdaten=_stammdaten(WIK))
    assert ergebnis.erfolgreich, ergebnis.warnungen
    assert Path(ergebnis.pdfs["Kopie"]).name == f"Muster – WIK – B-0001 – Kopie – {_datum(settings)}.pdf"
    assert pruefe_auftragskopie_name(Path(ergebnis.pdfs["Kopie"]).name, settings) == []
    assert "Verbindlich" in Path(ergebnis.pdfs["Cde"]).name
    assert any("aus dem Namen der Auftragskopie entfernt" in w for w in ergebnis.warnungen)


@pytest.mark.parametrize(
    ("zeitpunkt", "jahr", "datum"),
    [(datetime(2026, 12, 31, 23, 59, 59), "2026", "31.12.2026"), (datetime(2027, 1, 1, 0, 0, 3), "2027", "01.01.2027")],
)
def test_buchung_am_jahreswechsel_mit_fester_uhr(settings: Settings, zeitpunkt: datetime, jahr: str, datum: str):
    """Regression (Review Befund 17): buche() liest die Uhr über ``jetzt`` – Archivjahr und Datum im Dateinamen
    folgen dem Buchungszeitpunkt (testbar über Jahreswechsel und Mitternacht)."""
    paket = _paket(settings, WIK, lieferschein=False)
    ergebnis = _buche(
        paket, settings, dry_run=False, schreiber=ProzessSchreiber(settings, WIK), stammdaten=_stammdaten(WIK), jetzt=lambda: zeitpunkt
    )
    assert ergebnis.erfolgreich, ergebnis.warnungen
    assert ergebnis.zeitpunkt == zeitpunkt
    assert ergebnis.archiv is not None and ergebnis.archiv.parent.name == jahr
    assert Path(ergebnis.pdfs["Cde"]).name.endswith(f" – {datum}.pdf")


def test_lieferschein_nachziehen_mit_fester_uhr(settings: Settings):
    zeitpunkt = datetime(2026, 12, 31, 23, 59, 59)
    paket = _paket(settings, WIK, lieferschein=True)
    ergebnis = buchen.lieferschein_nachziehen(
        paket, "AB-2026-0042", settings, dry_run=False, schreiber=ProzessSchreiber(settings, WIK), jetzt=lambda: zeitpunkt
    )
    assert ergebnis.zeitpunkt == zeitpunkt


def test_archiviere_jahr_aus_der_uhr(settings: Settings, tmp_path: Path):
    from auftragsautomat.protokoll import archiviere

    datei = tmp_path / "eingang" / "x.pdf"
    datei.parent.mkdir(parents=True, exist_ok=True)
    datei.write_bytes(b"%PDF")
    ziel = archiviere(datei, settings, WIK, jetzt=lambda: datetime(2027, 1, 1, 0, 0, 1))
    assert ziel.parent.name == "2027"


def test_keine_revision_dublette_bleibt_fehler(settings: Settings):
    paket = _paket(settings, WIK, lieferschein=False)
    stammdaten = _stammdaten(WIK, belege=[_alte_ab(WIK, "B-0001")])
    ergebnis = buche(paket, settings, dry_run=False, schreiber=ProzessSchreiber(settings, WIK), stammdaten=stammdaten)
    assert ergebnis.erfolgreich is False and ergebnis.ist_revision is False


# ---------------------------------------------------------------------------
# Dry Run mit DryRunSchreiber: nichts speichern, PDFs nur geplant
# ---------------------------------------------------------------------------


def test_dry_run_speichert_nichts_und_plant_pdfs(settings: Settings):
    paket = _paket(settings, WIK, lieferschein=True)
    ergebnis = buche(paket, settings, stammdaten=_stammdaten(WIK))

    assert ergebnis.erfolgreich, ergebnis.warnungen
    assert ergebnis.dry_run is True
    assert ergebnis.angebot is not None and ergebnis.angebot.methode == "dry_run" and ergebnis.angebot.belegnummer is None
    assert ergebnis.einkauf is not None and ergebnis.einkauf.nachbemerkung == "B-0001"
    assert ergebnis.verkauf is not None and ergebnis.verkauf.methode == "opentrans"
    assert ergebnis.verkauf.artefakt is not None and ergebnis.verkauf.artefakt.parent == settings.xml_ausgabe_ordner
    assert ergebnis.verkauf.belegnummer is None
    assert ergebnis.lieferschein is None  # AB-Nummer liegt im Dry Run nicht vor
    assert set(ergebnis.pdfs) == {"Cde", "Speditionsauftrag", "Kopie"}
    ordner = Path(settings.lexware.pdf_ausgabe_ordner)
    for pfad in ergebnis.pdfs.values():
        assert pfad.parent == ordner
        assert not pfad.exists(), "Dry Run erzeugt keine PDF"
        assert pfad.with_suffix(VORSCHAU_ENDUNG).exists(), "Textvorschau fehlt"
    vorschau = ergebnis.pdfs["Cde"].with_suffix(VORSCHAU_ENDUNG).read_text(encoding="utf-8")
    assert "Formular: WIK EN" in vorschau and "Nachbemerkung: B-0001" in vorschau
    assert not list(ordner.glob("*.pdf"))
    import_ordner = settings.firma(WIK).opentrans.import_ordner
    assert import_ordner is None or not import_ordner.exists()
    assert paket.roh.quelldatei.exists()
    assert any("Dry Run: geplant, nicht erzeugt" in w for w in ergebnis.warnungen)
    zeile = _protokoll_zeilen(settings)[0]
    assert zeile["dry_run"] == "ja" and zeile["Angebot-Nr"] == "(simuliert)"
    assert zeile["Rabatte"] == "Pos. 1 (4001): 10 % (aktion:herbst)"
    text = zusammenfassung(paket, ergebnis, firma=settings.firma(WIK))
    assert "(Dry Run: geplant, nicht erzeugt)" in text
    assert "Angebot:             OK (Dry Run, nicht gespeichert)" in text


def test_dry_run_ws_mit_dry_run_schreiber(settings: Settings):
    paket = _paket(settings, WS, lieferschein=False)
    paket.lieferant = LieferantZuordnung()
    ergebnis = buche(paket, settings, stammdaten=_stammdaten(WS))
    assert ergebnis.erfolgreich, ergebnis.warnungen
    assert ergebnis.einkauf is None and ergebnis.angebot is not None and ergebnis.verkauf is not None
    assert set(ergebnis.pdfs) == {"Cde", "Speditionsauftrag", "Kopie"}
    assert Path(ergebnis.pdfs["Cde"]).name.startswith("Muster – Beispiel – Cde – ")
    assert any("Angebot -> Weiterführen -> Auftragsbestätigung simuliert" in w for w in ergebnis.verkauf.warnungen)


def test_verkaufsweg_bei_opentrans_schreiber_direkt(settings: Settings, monkeypatch: pytest.MonkeyPatch):
    """Windows + lizenziert: AB per openTRANS -> Angebot entfällt (methode keine), AB direkt."""
    fake = ProzessSchreiber(settings, WIK)
    monkeypatch.setattr(buchen, "_ist_windows", lambda: True)
    monkeypatch.setattr(buchen, "_erzeuge_ui_schreiber", lambda s, f: fake)
    settings.dry_run = False
    firma = settings.firma(WIK)
    assert firma.opentrans.lizenziert is True and firma.opentrans.import_ordner is not None
    firma.opentrans.import_ordner.mkdir(parents=True)
    paket = _paket(settings, WIK, lieferschein=False)
    ergebnis = buche(paket, settings, dry_run=False, stammdaten=_stammdaten(WIK))
    assert ergebnis.erfolgreich, ergebnis.warnungen
    assert ergebnis.angebot is not None and ergebnis.angebot.methode == "keine"
    assert ergebnis.angebot.warnungen == ["Angebot per openTRANS nicht möglich; AB direkt"]
    assert ergebnis.verkauf is not None and ergebnis.verkauf.methode == "opentrans"
    assert "Kopie" not in ergebnis.pdfs  # per openTRANS keine PDF (Beleg entsteht erst beim Übernehmen)
    assert any("kann per openTRANS nicht erzeugt werden" in w for w in ergebnis.verkauf.warnungen)
    assert [a[0] for a in fake.aufrufe] == ["einkauf", "pdf", "pdf"]
    assert _protokoll_zeilen(settings)[0]["Angebot-Nr"] == "(entfällt)"
