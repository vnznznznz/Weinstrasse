"""Tests für kommunikation.entwuerfe: Entwürfe zu einer Buchung (WIK: 3, Agentur ohne Bestellung), fehlende
Anhänge/Kontakte, Verbotswörter in der Auftragskopie, Nachfragen/Erinnerung, Speichern als .eml (FakeMailBackend).

Kontaktliste: tests/fixtures/excel/ansprechpartner_beispiel.xlsx (synthetisch, Domain example.org).
"""

from __future__ import annotations

import email
import email.policy
import shutil
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from auftragsautomat.config import Settings
from auftragsautomat.kommunikation.entwuerfe import (
    ANHANG_ORDNER,
    Entwurfsfabrik,
    erstelle_entwuerfe,
    pruefe_auftragskopie,
)
from auftragsautomat.kommunikation.vorlagen import Vorlagen
from auftragsautomat.kontakte import Kontaktliste, Zuordner, lade_kontaktliste
from auftragsautomat.m365.basis import M365Fehler
from auftragsautomat.m365.fake import FakeMailBackend
from auftragsautomat.models import (
    Ampel,
    Artikel,
    Belegart,
    Belegergebnis,
    Belegfelder,
    Belegpaket,
    BuchungsErgebnis,
    Eingangstyp,
    EmailEntwurf,
    EntwurfTyp,
    Extraktionsmethode,
    Fixtermin,
    Kontakt,
    Kontaktgruppe,
    Kunde,
    KundeZuordnung,
    Lieferant,
    LieferantZuordnung,
    MatchStatus,
    PositionsZuordnung,
    RohBestellung,
    RohPosition,
    Sendung,
    SendungStatus,
    Ueberwachungsbericht,
    UeberwachungsPunkt,
)
from auftragsautomat.stammdaten.laden import lade_alle

REPO_CONFIG = Path(__file__).resolve().parents[1] / "config"
KONTAKTLISTE = Path(__file__).parent / "fixtures" / "excel" / "ansprechpartner_beispiel.xlsx"
ZEITPUNKT = datetime(2026, 10, 24, 10, 0)
T = " – "


@pytest.fixture()
def kom_settings(settings: Settings, tmp_path: Path) -> Settings:
    settings.kommunikation.vorlagen_ordner = REPO_CONFIG / "vorlagen"
    settings.kommunikation.signaturen_datei = REPO_CONFIG / "signaturen.yaml"
    settings.kommunikation.anrede_datei = REPO_CONFIG / "anrede.yaml"
    settings.kommunikation.entwuerfe_ordner = tmp_path / "entwuerfe"
    settings.kommunikation.absender_name = "Max Beispiel"
    settings.kommunikation.absender_email = "logistik@example.org"
    settings.kommunikation.cc_standard = ["kollegin@example.org"]
    return settings


@pytest.fixture()
def liste(kom_settings: Settings) -> Kontaktliste:
    return lade_kontaktliste(KONTAKTLISTE, settings=kom_settings)


@pytest.fixture()
def zuordner(liste: Kontaktliste, kom_settings: Settings) -> Zuordner:
    return Zuordner(liste, lade_alle(kom_settings), kom_settings)


def _roh(**felder) -> RohBestellung:
    basis = {
        "eingangstyp": Eingangstyp.KUNDENBESTELLUNG_PDF,
        "quelldatei": Path("eingang/bestellung.pdf"),
        "extraktionsmethode": Extraktionsmethode.PDFPLUMBER,
        "kunde_text": "Beispielmarkt Nord",
        "lieferant_text": "Weingut Beispiel",
        "bestellnummer": "4711",
        "datum": date(2026, 10, 20),
        "liefertag": date(2026, 11, 9),
        "positionen": [
            RohPosition(laufnr=1, bezeichnung="Beispielwein Rosso 0,75 l", menge=Decimal(120)),
            RohPosition(laufnr=2, bezeichnung="Beispielwein Bianco 0,75 l", menge=Decimal(60)),
        ],
    }
    basis.update(felder)
    return RohBestellung(**basis)


def _paket(firma_id: str = "wik_leh", *, spedition: str | None = "Hillebrand", **roh_felder) -> Belegpaket:
    roh = _roh(**roh_felder)
    kunde = Kunde(firma_id=firma_id, kundennummer="2500", name="Beispielmarkt Nord GmbH", matchcode="BEISPIELMARKT", strasse="Marktweg 1", plz="00000", ort="Beispielstadt")
    lieferant = Lieferant(firma_id=firma_id, lieferantennummer="150", name="Weingut Beispiel", matchcode="BEISPIEL", ort="Esempio", land="IT")
    artikel = Artikel(firma_id=firma_id, artikelnummer="1643", bezeichnung="Beispielwein Rosso 0,75 l", ek_preis=Decimal("4.20"), vk_preis_1=Decimal("6.90"))
    positionen = [
        PositionsZuordnung(roh=roh.positionen[0], artikel=artikel, status=MatchStatus.SICHER, menge=Decimal(120), ek_preis=Decimal("4.20"), vk_preis=Decimal("6.90")),
        PositionsZuordnung(roh=roh.positionen[1], status=MatchStatus.SICHER, menge=Decimal(60)),
    ]
    firma_ist_agentur = firma_id == "weinstrasse_agentur"
    return Belegpaket(
        roh=roh,
        firma_id=firma_id,
        kunde=KundeZuordnung(kunde=kunde, status=MatchStatus.SICHER, quelle="alias"),
        lieferant=LieferantZuordnung(lieferant=lieferant, status=MatchStatus.SICHER, quelle="alias"),
        positionen=positionen,
        belegfelder=Belegfelder(spedition=spedition, weingut="Weingut Beispiel", shipment="directly", kunde="Beispielmarkt Nord"),
        einkauf_erforderlich=not firma_ist_agentur,
    )


def _ergebnis(firma_id: str = "wik_leh", *, pdfs: dict[str, Path] | None = None, lieferschein: bool = False) -> BuchungsErgebnis:
    einkauf = None
    if firma_id != "weinstrasse_agentur":
        einkauf = Belegergebnis(belegart=Belegart.BESTELLUNG, belegnummer="BE-2026-0100", erfolgreich=True, dry_run=False, methode="ui_automation")
    return BuchungsErgebnis(
        firma_id=firma_id,
        quelldatei=Path("eingang/bestellung.pdf"),
        dry_run=False,
        zeitpunkt=ZEITPUNKT,
        einkauf=einkauf,
        verkauf=Belegergebnis(belegart=Belegart.AUFTRAGSBESTAETIGUNG, belegnummer="AB-2026-0100", erfolgreich=True, dry_run=False, methode="ui_automation"),
        lieferschein=Belegergebnis(belegart=Belegart.LIEFERSCHEIN, belegnummer="LS-2026-0100", erfolgreich=True, dry_run=False) if lieferschein else None,
        pdfs=pdfs or {},
    )


def _fixtermin(**felder) -> Fixtermin:
    basis = {
        "firma_id": "wik_leh",
        "lieferant_kurz": "Beispiel",
        "kunde_kurz": "Beispielmarkt",
        "liefertag": date(2026, 11, 9),
        "auftragsdatum": date(2026, 10, 20),
        "verladedatum": date(2026, 11, 5),
        "bestellnummer": "BE-2026-0100",
        "kundenbestellnummer": "4711",
        "spedition": "Hillebrand",
    }
    basis.update(felder)
    return Fixtermin(**basis)


def _pdf(ordner: Path, name: str) -> Path:
    pfad = ordner / name
    pfad.parent.mkdir(parents=True, exist_ok=True)
    pfad.write_bytes(b"%PDF-1.4\n%synthetisch\n")
    return pfad


# ---------------------------------------------------------------- fuer_buchung


def test_wik_drei_entwuerfe_mit_empfaengern_und_anhaengen(kom_settings: Settings, zuordner: Zuordner, tmp_path: Path):
    pdfs = {
        "Cde": _pdf(tmp_path, f"BEISPIEL{T}WIK{T}4711{T}Cde{T}24.10.2026.pdf"),
        "Kopie": _pdf(tmp_path, f"BEISPIEL{T}WIK{T}4711{T}Kopie{T}24.10.2026.pdf"),
        "Speditionsauftrag": _pdf(tmp_path, f"Hillebrand{T}Speditionsauftrag{T}BEISPIEL{T}4711{T}24.10.2026.pdf"),
    }
    fabrik = Entwurfsfabrik(kom_settings, zuordner, kom_settings.firma("wik_leh"))
    entwuerfe = fabrik.fuer_buchung(_paket(), _ergebnis(pdfs=pdfs), _fixtermin(), {})
    assert [e.typ for e in entwuerfe] == [EntwurfTyp.BESTELLUNG_WEINGUT, EntwurfTyp.AUFTRAGSKOPIE_KUNDE, EntwurfTyp.SPEDITION_AUFTRAG]
    assert fabrik.hinweise == [] and all(e.hinweise == [] for e in entwuerfe)
    bestellung, kopie, sped = entwuerfe

    # Bestellung ans Weingut: Orders-Kontakt, englisch, Anhang Cde
    assert bestellung.an == ["orders@weingut-beispiel.example.org"] and bestellung.cc == ["kollegin@example.org"]
    assert bestellung.betreff == "New order BE-2026-0100 – Beispielmarkt Nord GmbH"
    assert bestellung.text_plain.startswith("Dear Ms Esempio,")
    assert "our new order BE-2026-0100 for Beispielmarkt Nord GmbH (customer order no. 4711): 180 bottles in 2 items." in bestellung.text_plain
    assert "due at the customer on 09.11.2026 (fixed delivery date); transport will be arranged by Hillebrand." in bestellung.text_plain
    assert [a.name for a in bestellung.anhaenge] == [pdfs["Cde"].name]
    assert bestellung.bezug == {"bestellnummer": "BE-2026-0100", "kundenbestellnummer": "4711", "kunde": "Beispielmarkt Nord GmbH", "lieferant": "Weingut Beispiel", "firma_id": "wik_leh", "kuerzel": "Cde"}
    assert bestellung.absender == "logistik@example.org"
    assert "<p>Max Beispiel<br>" in bestellung.text_html and "Wein-Import-Kreis LEH" in bestellung.text_plain

    # Auftragskopie: Kontakt des Kunden (Produktzeile des Weinguts zuerst), deutsch, Anhang Kopie
    assert kopie.an == ["einkauf@beispielmarkt.example.org"] and kopie.cc == ["kollegin@example.org"]
    assert kopie.betreff == "Ihre Bestellung 4711 – Auftragskopie zur Information"
    assert kopie.text_plain.startswith("Hallo Herr Beispiel!")
    assert "Kopie Ihres Auftrags, so wie wir ihn erfasst haben (unsere Nr. AB-2026-0100)." in kopie.text_plain
    assert "für den 09.11.2026 vorgesehen (Spedition Hillebrand)" in kopie.text_plain
    assert [a.name for a in kopie.anhaenge] == [pdfs["Kopie"].name]
    assert pruefe_auftragskopie(kopie, kom_settings) == []

    # Speditionsauftrag: Postfach "Neue Aufträge" aus der Kontaktliste, Anhang Speditionsauftrag
    assert sped.an == ["neue-auftraege@hillebrand.example.org"]
    assert sped.betreff == "Neuer Auftrag Beispielmarkt Nord GmbH – Abholung Weingut Beispiel"
    assert sped.text_plain.startswith("Guten Tag!")
    assert "- Abholung bei Weingut Beispiel, Esempio, IT – Verladung am 05.11.2026" in sped.text_plain
    assert "- Lieferung an Beispielmarkt Nord GmbH, Marktweg 1, 00000 Beispielstadt" in sped.text_plain
    assert "- Fixtermin beim Empfänger: 09.11.2026" in sped.text_plain
    assert "- Menge: 180 Flaschen in 2 Positionen" in sped.text_plain
    assert "Den Speditionsauftrag finden Sie im Anhang." in sped.text_plain  # kein Lieferschein
    assert [a.name for a in sped.anhaenge] == [pdfs["Speditionsauftrag"].name]
    assert sped.bezug["spedition"] == "Hillebrand" and sped.bezug["kuerzel"] == "Speditionsauftrag"


def test_agentur_ohne_bestellung_ans_weingut(kom_settings: Settings, zuordner: Zuordner):
    fabrik = Entwurfsfabrik(kom_settings, zuordner, kom_settings.firma("weinstrasse_agentur"))
    paket = _paket("weinstrasse_agentur", spedition="Hillebrand")
    entwuerfe = fabrik.fuer_buchung(paket, _ergebnis("weinstrasse_agentur"), None, {})
    assert [e.typ for e in entwuerfe] == [EntwurfTyp.AUFTRAGSKOPIE_KUNDE, EntwurfTyp.SPEDITION_AUFTRAG]
    kopie, sped = entwuerfe
    # WS-Schema: "Lieferant – Kunde – Kürzel – Datum"
    assert kopie.hinweise == [f"Anhang fehlt: BEISPIEL{T}BEISPIELMARKT{T}Kopie{T}24.10.2026.pdf"]
    assert sped.hinweise == [f"Anhang fehlt: Hillebrand{T}Speditionsauftrag{T}BEISPIEL{T}BEISPIELMARKT{T}24.10.2026.pdf"]
    assert "Weinstrasse Agentur" in kopie.text_plain
    # Schalter: WS_EN-Formular als Cde ans Weingut (Abschnitt 5.2) -> Bestellung zusätzlich
    mit = Entwurfsfabrik(kom_settings, zuordner, kom_settings.firma("weinstrasse_agentur"), bestellung_ohne_einkauf=True)
    typen = [e.typ for e in mit.fuer_buchung(paket, _ergebnis("weinstrasse_agentur"), None, {})]
    assert typen[0] == EntwurfTyp.BESTELLUNG_WEINGUT and len(typen) == 3


def test_agentur_selbstabholung_ohne_speditionsauftrag(kom_settings: Settings, zuordner: Zuordner):
    fabrik = Entwurfsfabrik(kom_settings, zuordner, kom_settings.firma("weinstrasse_agentur"))
    paket = _paket("weinstrasse_agentur", spedition="Selbstabholung")
    entwuerfe = fabrik.fuer_buchung(paket, _ergebnis("weinstrasse_agentur"), None, {})
    assert [e.typ for e in entwuerfe] == [EntwurfTyp.AUFTRAGSKOPIE_KUNDE]
    # ohne Belegfeld: Kontaktliste liefert die Standard-Spedition des Kunden (Beispiel-Logistik -> Dispo "Zentrale")
    paket.belegfelder.spedition = None
    entwuerfe = fabrik.fuer_buchung(paket, _ergebnis("weinstrasse_agentur"), None, {})
    sped = entwuerfe[-1]
    assert sped.typ == EntwurfTyp.SPEDITION_AUFTRAG and sped.an == ["dispo@beispiel-logistik.example.org"]
    assert sped.bezug["spedition"] == "Beispiel-Logistik"


def test_fehlende_anhaenge_und_fehlende_kontakte(kom_settings: Settings, tmp_path: Path):
    fabrik = Entwurfsfabrik(kom_settings, None, kom_settings.firma("wik_leh"))  # ohne Kontaktliste
    entwuerfe = fabrik.fuer_buchung(_paket(), _ergebnis(), _fixtermin(), {"Cde": tmp_path / "gibt_es_nicht.pdf"})
    assert len(entwuerfe) == 3
    bestellung, kopie, sped = entwuerfe
    assert bestellung.an == [] and bestellung.anhaenge == []
    assert bestellung.hinweise == [
        "Kein Empfänger gefunden (Orders-Kontakt Weingut Beispiel) – bitte Adresse ergänzen.",
        f"Anhang fehlt: BEISPIEL{T}WIK{T}4711{T}Cde{T}24.10.2026.pdf",
    ]
    assert bestellung.text_plain.startswith("Hello,")  # neutrale Anrede ohne Kontakt
    assert kopie.an == [] and kopie.cc == ["kollegin@example.org"] and kopie.text_plain.startswith("Guten Tag!")
    assert sped.an == [] and "Postfach 'Neue Aufträge' der Spedition Hillebrand" in sped.hinweise[0]
    assert sped.hinweise[1].startswith("Anhang fehlt: Hillebrand")


def test_anhang_wird_unter_titulierungsname_bereitgestellt(kom_settings: Settings, zuordner: Zuordner, tmp_path: Path):
    quelle = _pdf(tmp_path / "lexware", "Bestellung_irgendwas.pdf")
    anderes_datum = _pdf(tmp_path / "lexware", f"BEISPIEL{T}WIK{T}4711{T}Kopie{T}01.10.2026.pdf")
    fabrik = Entwurfsfabrik(kom_settings, zuordner, kom_settings.firma("wik_leh"))
    entwuerfe = fabrik.fuer_buchung(_paket(), _ergebnis(), None, {"Cde": quelle, "Kopie": anderes_datum})
    bestellung, kopie, _ = entwuerfe
    kopiert = bestellung.anhaenge[0]
    assert kopiert.name == f"BEISPIEL{T}WIK{T}4711{T}Cde{T}24.10.2026.pdf"
    assert kopiert.parent == kom_settings.kommunikation.entwuerfe_ordner / ANHANG_ORDNER
    assert kopiert.read_bytes() == quelle.read_bytes() and quelle.exists()
    # gleicher Name mit anderem Datum (Lexware-Ausgabedatum) bleibt unverändert
    assert kopie.anhaenge == [anderes_datum]


def test_revision_nutzt_neu_und_neu_kopie(kom_settings: Settings, zuordner: Zuordner, tmp_path: Path):
    pdfs = {"Neu": _pdf(tmp_path, f"BEISPIEL{T}WIK{T}4711{T}Neu{T}24.10.2026.pdf")}
    fabrik = Entwurfsfabrik(kom_settings, zuordner, kom_settings.firma("wik_leh"))
    paket = _paket(ist_revision=True, revision_von="4700")
    paket.ist_revision = True
    bestellung, kopie, _ = fabrik.fuer_buchung(paket, _ergebnis(), None, pdfs)
    assert bestellung.betreff.startswith("Revised order BE-2026-0100")
    assert "replaces our previous order 4700." in bestellung.text_plain
    assert [a.name for a in bestellung.anhaenge] == [pdfs["Neu"].name] and bestellung.bezug["kuerzel"] == "Neu"
    assert kopie.hinweise == [f"Anhang fehlt: BEISPIEL{T}WIK{T}4711{T}Neu Kopie{T}24.10.2026.pdf"]
    assert "ersetzt die frühere Fassung" in kopie.text_plain and kopie.bezug["kuerzel"] == "Neu Kopie"


def test_lieferschein_entwurf_nur_wenn_erlaubt(kom_settings: Settings, zuordner: Zuordner, tmp_path: Path):
    ls = _pdf(tmp_path, f"BEISPIEL{T}WIK{T}4711{T}Ls{T}24.10.2026.pdf")
    fabrik = Entwurfsfabrik(kom_settings, zuordner, kom_settings.firma("wik_leh"))
    paket = _paket()
    paket.lieferschein_erstellen = True
    entwuerfe = fabrik.fuer_buchung(paket, _ergebnis(lieferschein=True), _fixtermin(), {"Ls": ls})
    assert [e.typ for e in entwuerfe][-2:] == [EntwurfTyp.SPEDITION_AUFTRAG, EntwurfTyp.LIEFERSCHEIN]
    sped, lieferschein = entwuerfe[-2:]
    assert [a.name for a in sped.anhaenge] == [ls.name] and "und den Lieferschein finden Sie im Anhang" in sped.text_plain
    assert lieferschein.an == ["orders@weingut-beispiel.example.org"]
    assert lieferschein.betreff == "Delivery note LS-2026-0100 – Beispielmarkt Nord GmbH"
    assert "hand it over to the carrier" in lieferschein.text_plain and lieferschein.anhaenge == [ls]
    # Agentur: Lieferschein nicht erlaubt -> kein Entwurf, Hinweis
    agentur = Entwurfsfabrik(kom_settings, zuordner, kom_settings.firma("weinstrasse_agentur"))
    paket_ws = _paket("weinstrasse_agentur")
    paket_ws.lieferschein_erstellen = True
    typen = [e.typ for e in agentur.fuer_buchung(paket_ws, _ergebnis("weinstrasse_agentur"), None, {"Ls": ls})]
    assert EntwurfTyp.LIEFERSCHEIN not in typen and any("Lieferschein nicht erlaubt" in h for h in agentur.hinweise)
    # Lieferschein an den Kunden (deutsch)
    an_kunde = fabrik.lieferschein(paket, _ergebnis(lieferschein=True), None, {"Ls": ls}, an_kunde=True)
    assert an_kunde.an == ["einkauf@beispielmarkt.example.org"] and an_kunde.betreff.startswith("Lieferschein LS-2026-0100")
    assert "beim Eingang" in an_kunde.text_plain


# ---------------------------------------------------------------- Verbotswörter


def test_pruefe_auftragskopie_schlaegt_an(kom_settings: Settings):
    basis = dict(typ=EntwurfTyp.AUFTRAGSKOPIE_KUNDE, an=["kunde@example.org"], text_html="<p>Kopie</p>", text_plain="Kopie")
    assert pruefe_auftragskopie(EmailEntwurf(betreff="Ihre Bestellung 4711 – Auftragskopie", **basis), kom_settings) == []
    assert pruefe_auftragskopie(EmailEntwurf(betreff="Ihre Auftragsbestätigung 4711", **basis), kom_settings) == ["Auftragsbestätigung"]
    # ohne Umlaut und in anderer Schreibweise, nur einmal gemeldet
    assert pruefe_auftragskopie(EmailEntwurf(betreff="AUFTRAGSBESTAETIGUNG", **basis), kom_settings) == ["Auftragsbestätigung"]
    e = EmailEntwurf(betreff="Ihre Bestellung", **{**basis, "text_plain": "Bitte bestaetigen Sie verbindlich.", "text_html": "<p>Bitte best&auml;tigen Sie verbindlich.</p>"})
    assert pruefe_auftragskopie(e, kom_settings) == ["bestätigen", "verbindlich"]
    e = EmailEntwurf(betreff="Ihre Bestellung", anhaenge=[Path("Weingut – Kunde – Auftragsbestätigung – 24.10.2026.pdf")], **basis)
    assert pruefe_auftragskopie(e, kom_settings) == ["Auftragsbestätigung"]


def test_fuer_buchung_erzeugt_keine_auftragskopie_mit_verbotenen_woertern_in_der_vorlage(kom_settings: Settings, zuordner: Zuordner, tmp_path: Path, caplog):
    # jemand hat die Vorlage geändert und schreibt wieder "Auftragsbestätigung" (Haftung) -> kein Entwurf
    ordner = tmp_path / "vorlagen"
    shutil.copytree(REPO_CONFIG / "vorlagen", ordner)
    for endung in (".html.j2", ".txt.j2"):
        datei = ordner / f"auftragskopie_kunde{endung}"
        datei.write_text(datei.read_text(encoding="utf-8").replace("eine Kopie Ihres Auftrags", "die Auftragsbestaetigung"), encoding="utf-8")
    fabrik = Entwurfsfabrik(kom_settings, zuordner, kom_settings.firma("wik_leh"), vorlagen=Vorlagen(ordner))
    ergebnis = _ergebnis()
    with caplog.at_level("ERROR"):
        entwuerfe = fabrik.fuer_buchung(_paket(), ergebnis, None, {})
    assert [e.typ for e in entwuerfe] == [EntwurfTyp.BESTELLUNG_WEINGUT, EntwurfTyp.SPEDITION_AUFTRAG]
    assert len(fabrik.hinweise) == 1 and "Auftragsbestätigung" in fabrik.hinweise[0] and "nicht erzeugt" in fabrik.hinweise[0]
    assert ergebnis.warnungen == fabrik.hinweise
    assert "verbotene Wörter" in caplog.text
    # die unveränderte Vorlage erzeugt die Kopie
    assert len(Entwurfsfabrik(kom_settings, zuordner, kom_settings.firma("wik_leh")).fuer_buchung(_paket(), _ergebnis(), None, {})) == 3


def test_fuer_buchung_lehnt_auftragskopie_mit_verbotenem_anhangsnamen_ab(kom_settings: Settings, zuordner: Zuordner, tmp_path: Path):
    # WS-Schema trägt den Kundenkurznamen im Dateinamen; ein Kurzname mit verbotenem Wort blockiert die Kopie
    paket = _paket("weinstrasse_agentur", spedition="Selbstabholung")
    paket.kunde.kunde.matchcode = "VERBINDLICH"
    kopie = _pdf(tmp_path, f"BEISPIEL{T}VERBINDLICH{T}Kopie{T}24.10.2026.pdf")
    fabrik = Entwurfsfabrik(kom_settings, zuordner, kom_settings.firma("weinstrasse_agentur"))
    entwuerfe = fabrik.fuer_buchung(paket, _ergebnis("weinstrasse_agentur"), None, {"Kopie": kopie})
    assert entwuerfe == [] and fabrik.hinweise and "verbindlich" in fabrik.hinweise[0]


# ---------------------------------------------------------------- Nachfragen / Erinnerung


def test_nachfrage_lieferant(kom_settings: Settings, zuordner: Zuordner, liste: Kontaktliste):
    fabrik = Entwurfsfabrik(kom_settings, zuordner, kom_settings.firma("wik_leh"))
    kontakt = liste.kontakt_fuer_email("orders@weingut-beispiel.example.org")
    e = fabrik.nachfrage_lieferant(_fixtermin(), kontakt)
    assert e.typ == EntwurfTyp.NACHFRAGE_LIEFERANT and e.an == ["orders@weingut-beispiel.example.org"]
    assert e.betreff == "Order BE-2026-0100 – Beispielmarkt: confirmation and loading date"
    assert e.text_plain.startswith("Dear Ms Esempio,")
    assert "confirmation of our order BE-2026-0100 for Beispielmarkt dated 20.10.2026" in e.text_plain
    assert "fixed date for 09.11.2026, loading planned for 05.11.2026." in e.text_plain
    assert e.bezug["liefertag"] == "2026-11-09" and e.anhaenge == []
    ohne = fabrik.nachfrage_lieferant(_fixtermin(bestellnummer=None, kundenbestellnummer=None), None)
    assert ohne.an == [] and ohne.hinweise and ohne.betreff == "Our order for Beispielmarkt: confirmation and loading date"


def test_nachfrage_spedition(kom_settings: Settings, zuordner: Zuordner):
    fabrik = Entwurfsfabrik(kom_settings, zuordner, kom_settings.firma("wik_leh"))
    kontakt = zuordner.postfach_spedition("Hillebrand", "status")
    sendung = Sendung(spedition="Hillebrand", referenz="DEMB00000", kundenreferenz="BE-2026-0100", status=SendungStatus.BESTAETIGT)
    e = fabrik.nachfrage_spedition(_fixtermin(), sendung, kontakt)
    assert e.typ == EntwurfTyp.NACHFRAGE_SPEDITION and e.an == ["info@hillebrand.example.org"]
    assert e.betreff == "Statusanfrage Our Ref DEMB00000 – Beispiel / Beispielmarkt"
    assert e.text_plain.startswith("Guten Tag!")
    assert "(Our Ref DEMB00000), unsere Referenz BE-2026-0100, mit Fixtermin 09.11.2026 (Verladung 05.11.2026)" in e.text_plain
    assert "(letzter Stand bei uns: bestaetigt)" in e.text_plain and e.bezug["referenz"] == "DEMB00000"
    ohne_ref = fabrik.nachfrage_spedition(_fixtermin(), None, kontakt)
    assert ohne_ref.hinweise == ["Keine Referenz der Spedition (Our Ref) bekannt – bitte im Entwurf ergänzen."]
    assert ohne_ref.betreff == "Statusanfrage – Beispiel / Beispielmarkt" and ohne_ref.bezug["kundenreferenz"] == "BE-2026-0100"


def test_erinnerung_intern(kom_settings: Settings, zuordner: Zuordner):
    fabrik = Entwurfsfabrik(kom_settings, zuordner, kom_settings.firma("wik_leh"))
    punkte = [
        UeberwachungsPunkt(fixtermin=_fixtermin(), ampel="rot", grund="Lieferant hat nicht bestätigt", aktionen=["Nachfrage Lieferant"]),
        UeberwachungsPunkt(fixtermin=_fixtermin(kunde_kurz="Weinhandlung", liefertag=date(2026, 11, 10)), ampel="gelb", grund="Spedition offen"),
        UeberwachungsPunkt(fixtermin=_fixtermin(kunde_kurz="Dritter"), ampel="gruen", grund="alles bestätigt"),
    ]
    bericht = Ueberwachungsbericht(zeitpunkt=datetime(2026, 10, 24, 7, 0), zeitraum_bis=date(2026, 11, 14), punkte=punkte, fehler=["Excel gesperrt"])
    e = fabrik.erinnerung_intern(bericht)
    assert e.typ == EntwurfTyp.ERINNERUNG_INTERN and e.an == ["logistik@example.org"] and e.cc == []
    assert e.betreff == "Erinnerung 24.10.2026: 2 fällige Punkte"
    assert e.text_plain.startswith("Hallo Herr Beispiel!")
    assert "2 Fixtermine brauchen Aufmerksamkeit." in e.text_plain
    assert "- 09.11.2026 – Beispiel an Beispielmarkt (rot): Lieferant hat nicht bestätigt – Nachfrage Lieferant" in e.text_plain
    assert "- 10.11.2026 – Beispiel an Weinhandlung (gelb): Spedition offen" in e.text_plain
    assert "Dritter" not in e.text_plain and "Fehler im Lauf: Excel gesperrt" in e.text_plain
    kom_settings.kommunikation.absender_email = None
    ohne = fabrik.erinnerung_intern(bericht)
    assert ohne.an == [] and ohne.hinweise and "absender_email" in ohne.hinweise[0]
    ampel: Ampel = "gruen"
    assert ampel == "gruen"


# ---------------------------------------------------------------- speichern (.eml)


def test_speichere_eml_mit_x_unsent_anhang_html_und_text(kom_settings: Settings, zuordner: Zuordner, tmp_path: Path):
    pdfs = {"Cde": _pdf(tmp_path, f"BEISPIEL{T}WIK{T}4711{T}Cde{T}24.10.2026.pdf")}
    fabrik = Entwurfsfabrik(kom_settings, zuordner, kom_settings.firma("wik_leh"))
    entwuerfe = fabrik.fuer_buchung(_paket(), _ergebnis(), _fixtermin(), pdfs)
    backend = FakeMailBackend(kom_settings.kommunikation.entwuerfe_ordner, jetzt=lambda: datetime(2026, 10, 24, 10, 0).astimezone())
    gespeichert = fabrik.speichere(entwuerfe, backend)
    assert len(gespeichert) == 3 and all(e.speicherort and e.erstellt_am for e in gespeichert)
    assert len(backend.entwuerfe) == 3  # nie gesendet, nur Entwürfe
    eml = Path(gespeichert[0].speicherort)
    assert eml.suffix == ".eml" and eml.parent == kom_settings.kommunikation.entwuerfe_ordner
    nachricht = email.message_from_bytes(eml.read_bytes(), policy=email.policy.default)
    assert nachricht["X-Unsent"] == "1" and nachricht["Message-ID"]
    assert nachricht["To"] == "orders@weingut-beispiel.example.org" and nachricht["Cc"] == "kollegin@example.org"
    assert nachricht["Subject"] == "New order BE-2026-0100 – Beispielmarkt Nord GmbH"
    html = nachricht.get_body(preferencelist=("html",))
    text = nachricht.get_body(preferencelist=("plain",))
    assert html is not None and "<p>Dear Ms Esempio,</p>" in html.get_content()
    assert text is not None and text.get_content().startswith("Dear Ms Esempio,")
    anhaenge = [teil.get_filename() for teil in nachricht.iter_attachments()]
    assert anhaenge == [f"BEISPIEL{T}WIK{T}4711{T}Cde{T}24.10.2026.pdf"]
    # Entwurf ohne Anhang (Kopie fehlt): .eml ohne Anhang, Hinweis bleibt am Entwurf
    kopie = email.message_from_bytes(Path(gespeichert[1].speicherort).read_bytes(), policy=email.policy.default)
    assert list(kopie.iter_attachments()) == [] and gespeichert[1].hinweise[0].startswith("Anhang fehlt")


def test_speichere_faengt_backend_fehler_ab(kom_settings: Settings, zuordner: Zuordner):
    class KaputtesBackend:
        def erstelle_entwurf(self, entwurf: EmailEntwurf) -> str:
            raise M365Fehler("Postfach nicht erreichbar", status=503)

    fabrik = Entwurfsfabrik(kom_settings, zuordner, kom_settings.firma("wik_leh"))
    e = fabrik.nachfrage_lieferant(_fixtermin(), None)
    ergebnis = fabrik.speichere([e], KaputtesBackend())  # type: ignore[arg-type]
    assert ergebnis == [e] and e.speicherort is None
    assert any("Postfach nicht erreichbar" in h for h in e.hinweise)


def test_signatur_nur_wenn_das_backend_keine_einfuegt(kom_settings: Settings, zuordner: Zuordner):
    fixtermin = _fixtermin()
    kontakt = zuordner.kontakt_fuer_lieferant("Weingut Beispiel")
    mit = Entwurfsfabrik(kom_settings, zuordner, kom_settings.firma("wik_leh")).nachfrage_lieferant(fixtermin, kontakt)
    assert mit.text_plain.rstrip().endswith("Tel. +49 000 0000000 · logistik@example.org") and "<p>Max Beispiel<br>" in mit.text_html
    # Backend "lokal": Outlook fügt die eigene Signatur ein -> Text endet mit der Grußformel
    kom_settings.m365.backend = "lokal"
    ohne = Entwurfsfabrik(kom_settings, zuordner, kom_settings.firma("wik_leh")).nachfrage_lieferant(fixtermin, kontakt)
    assert ohne.text_plain.rstrip().endswith("Thank you and best regards") and "Max Beispiel" not in ohne.text_html
    # ausdrücklicher Schalter schlägt das Backend
    erzwungen = Entwurfsfabrik(kom_settings, zuordner, kom_settings.firma("wik_leh"), signatur_einfuegen=True).nachfrage_lieferant(fixtermin, kontakt)
    assert "Max Beispiel" in erzwungen.text_plain


def test_erstelle_entwuerfe_kurzform_und_ohne_firma(kom_settings: Settings, zuordner: Zuordner):
    entwuerfe = erstelle_entwuerfe(_paket(), _ergebnis(), kom_settings, kontakte=zuordner)
    assert [e.typ for e in entwuerfe] == [EntwurfTyp.BESTELLUNG_WEINGUT, EntwurfTyp.AUFTRAGSKOPIE_KUNDE, EntwurfTyp.SPEDITION_AUFTRAG]
    paket = _paket()
    paket.firma_id = None
    with pytest.raises(ValueError, match="ohne Firma"):
        erstelle_entwuerfe(paket, None, kom_settings)
    # Firma aus dem Ergebnis
    assert len(erstelle_entwuerfe(paket, _ergebnis(), kom_settings)) == 3


def test_anrede_regeln_aus_yaml_wandern_in_den_zuordner(kom_settings: Settings, liste: Kontaktliste, tmp_path: Path):
    regeln = tmp_path / "anrede.yaml"
    regeln.write_text(
        "regeln:\n  orders@weingut-beispiel.example.org:\n    anrede: 'Ciao Giulia'\n    sprache: en\n",
        encoding="utf-8",
    )
    kom_settings.kommunikation.anrede_datei = regeln
    zuordner = Zuordner(liste, None, kom_settings)
    fabrik = Entwurfsfabrik(kom_settings, zuordner, kom_settings.firma("wik_leh"))
    assert zuordner.anrede_regeln == {"orders@weingut-beispiel.example.org": {"anrede": "Ciao Giulia", "sprache": "en"}}
    e = fabrik.nachfrage_lieferant(_fixtermin(), liste.kontakt_fuer_email("orders@weingut-beispiel.example.org"))
    assert e.text_plain.startswith("Ciao Giulia,")
    kontakt = Kontakt(gruppe=Kontaktgruppe.LIEFERANT, firma="X", name="Frau Beispiel", email="x@example.org", land="Deutschland")
    assert fabrik._sprache(kontakt, "en") == "de"  # Land schlägt den Vorlagen-Standard
