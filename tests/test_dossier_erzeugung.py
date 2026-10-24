"""Tests für dossier/erzeugung.py (DossierVerwaltung) mit FakeDateiBackend. Nur synthetische Daten."""

from __future__ import annotations

import email
import email.policy
import json
from datetime import date, datetime
from decimal import Decimal
from email.message import EmailMessage
from email.utils import formatdate
from pathlib import Path

import pytest
from pypdf import PdfReader
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from auftragsautomat.config import DateiRefKonfig, Settings
from auftragsautomat.dossier import (
    DECKBLATT,
    FIXTERMIN_DATEI,
    GESAMT_PDF,
    INDEX_DATEI,
    MAIL_ORDNER,
    PAKET_DATEI,
    DossierVerwaltung,
    eml_aus_entwurf,
    fixtermin_text,
    mail_dateiname,
)
from auftragsautomat.m365.fake import FakeDateiBackend
from auftragsautomat.models import (
    Belegart,
    Belegergebnis,
    Belegfelder,
    Belegpaket,
    BuchungsErgebnis,
    Dossier,
    Eingangstyp,
    EmailEntwurf,
    EntwurfTyp,
    Extraktionsmethode,
    Fixtermin,
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
    Sendung,
    SendungStatus,
)

T1 = datetime(2026, 10, 24, 9, 30).astimezone()
T2 = datetime(2026, 10, 25, 8, 15).astimezone()
ZEITPUNKT = datetime(2026, 10, 24, 9, 0)
ORDNERNAME = "Beispielgut-WIK-Musterhandel-4711-24.10.2026"


# ---------------------------------------------------------------------------
# Hilfen
# ---------------------------------------------------------------------------


def _pdf(pfad: Path, text: str, seiten: int = 1) -> Path:
    pfad.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(pfad), pagesize=A4)
    for i in range(seiten):
        c.drawString(72, 720, f"{text} – Seite {i + 1}")
        c.showPage()
    c.save()
    return pfad


def _paket(tmp_path: Path, firma_id: str = "wik_fh_seh", *, quelldatei: Path | None = None, ist_revision: bool = False, liefertag: date | None = date(2026, 11, 6)) -> Belegpaket:
    quelle = quelldatei or _pdf(tmp_path / "eingang" / "bestellung.pdf", "Kundenbestellung 4711")
    roh = RohBestellung(
        eingangstyp=Eingangstyp.KUNDENBESTELLUNG_PDF,
        quelldatei=quelle,
        extraktionsmethode=Extraktionsmethode.PDFPLUMBER,
        kunde_text="Weinhandel Muster GmbH",
        lieferant_text="Weingut Beispiel srl",
        bestellnummer="4711",
        datum=date(2026, 10, 20),
        liefertag=liefertag,
        positionen=[RohPosition(laufnr=1, bezeichnung="Rosso Esempio 2022", menge=Decimal("60"))],
        ist_revision=ist_revision,
    )
    paket = Belegpaket(roh=roh, firma_id=firma_id, ist_revision=ist_revision, einkauf_erforderlich=firma_id != "weinstrasse_agentur")
    paket.kunde = KundeZuordnung(kunde=Kunde(firma_id=firma_id, kundennummer="10001", name="Weinhandel Muster GmbH", matchcode="Musterhandel"), status=MatchStatus.SICHER)
    paket.lieferant = LieferantZuordnung(lieferant=Lieferant(firma_id=firma_id, lieferantennummer="90001", name="Weingut Beispiel srl", matchcode="Beispielgut"), status=MatchStatus.SICHER)
    paket.positionen = [PositionsZuordnung(roh=roh.positionen[0], status=MatchStatus.KEIN_TREFFER, menge=Decimal("60"))]
    paket.belegfelder = Belegfelder(spedition="Spedition Muster", weingut="Weingut Beispiel srl", shipment="directly", kunde="Weinhandel Muster GmbH")
    return paket


def _ergebnis(paket: Belegpaket, *, dry_run: bool = False, einkauf: str | None = "BE-1001", verkauf: str | None = "AB-2002", angebot: str | None = None) -> BuchungsErgebnis:
    def beleg(art: Belegart, nummer: str | None) -> Belegergebnis | None:
        if nummer is None:
            return None
        return Belegergebnis(belegart=art, belegnummer=None if dry_run else nummer, erfolgreich=True, dry_run=dry_run, methode="dry_run" if dry_run else "ui_automation")

    return BuchungsErgebnis(
        firma_id=paket.firma_id or "wik_fh_seh",
        quelldatei=paket.roh.quelldatei,
        dry_run=dry_run,
        zeitpunkt=ZEITPUNKT,
        angebot=beleg(Belegart.ANGEBOT, angebot),
        einkauf=beleg(Belegart.BESTELLUNG, einkauf),
        verkauf=beleg(Belegart.AUFTRAGSBESTAETIGUNG, verkauf),
    )


def _entwurf(typ: EntwurfTyp, *, an: list[str] | None = None, erstellt_am: datetime | None = None, speicherort: str | None = None, anhaenge: list[Path] | None = None) -> EmailEntwurf:
    return EmailEntwurf(
        typ=typ,
        an=["frau.beispiel@example.org"] if an is None else an,
        betreff=f"Testbetreff {typ.value}",
        text_html="<p>Guten Tag Frau Beispiel,</p><p>anbei die Unterlagen.</p>",
        text_plain="Guten Tag Frau Beispiel,\n\nanbei die Unterlagen.",
        erstellt_am=erstellt_am or datetime(2026, 10, 24, 9, 5),
        speicherort=speicherort,
        anhaenge=anhaenge or [],
    )


def _lexware_pdfs(tmp_path: Path, *kuerzel: str, text: str = "Beleg") -> dict[str, Path]:
    return {k: _pdf(tmp_path / "pdf" / f"{k}.pdf", f"{text} {k}") for k in kuerzel}


def _fixtermin(**felder) -> Fixtermin:
    basis = dict(firma_id="wik_fh_seh", lieferant_kurz="Beispielgut", kunde_kurz="Musterhandel", liefertag=date(2026, 11, 6), auftragsdatum=date(2026, 10, 24), spedition="Spedition Muster", zeile=12)
    basis.update(felder)
    return Fixtermin(**basis)


def _eml(pfad: Path, *, typ: str | None = "spedition_bestaetigung", datum: datetime = datetime(2026, 10, 26, 14, 0)) -> Path:
    nachricht = EmailMessage(policy=email.policy.SMTP)
    nachricht["Subject"] = "Auftragsbestätigung; Your Ref: 4711; Our Ref: DEMB00000"
    nachricht["From"] = "spedition@example.org"
    nachricht["To"] = "logistik@example.org"
    nachricht["Date"] = formatdate(datum.timestamp(), localtime=True)
    if typ:
        nachricht["X-Auftragsautomat-Typ"] = typ
    nachricht.set_content("Wir bestätigen den Auftrag.")
    pfad.parent.mkdir(parents=True, exist_ok=True)
    pfad.write_bytes(nachricht.as_bytes())
    return pfad


@pytest.fixture()
def uhr() -> dict[str, datetime]:
    return {"jetzt": T1}


@pytest.fixture()
def wurzel(tmp_path: Path) -> Path:
    return tmp_path / "dossiers"


@pytest.fixture()
def verwaltung(settings: Settings, tmp_path: Path, wurzel: Path, uhr: dict[str, datetime]) -> DossierVerwaltung:
    settings.dossier.lokaler_ordner = wurzel
    backend = FakeDateiBackend(wurzel)
    return DossierVerwaltung(backend, DateiRefKonfig(lokaler_pfad=wurzel), settings, jetzt=lambda: uhr["jetzt"])


def _dateien(ordner: Path) -> list[str]:
    return sorted(str(p.relative_to(ordner)) for p in ordner.rglob("*") if p.is_file())


# ---------------------------------------------------------------------------
# Erzeugen
# ---------------------------------------------------------------------------


def test_erstellt_dossier_mit_allen_dateien(verwaltung: DossierVerwaltung, wurzel: Path, tmp_path: Path):
    paket = _paket(tmp_path)
    entwuerfe = [
        _entwurf(EntwurfTyp.BESTELLUNG_WEINGUT),
        _entwurf(EntwurfTyp.AUFTRAGSKOPIE_KUNDE, erstellt_am=datetime(2026, 10, 24, 9, 6)),
        _entwurf(EntwurfTyp.SPEDITION_AUFTRAG, erstellt_am=datetime(2026, 10, 24, 9, 7)),
    ]
    dossier = verwaltung.erstelle_oder_aktualisiere(
        paket,
        _ergebnis(paket),
        lexware_pdfs=_lexware_pdfs(tmp_path, "Cde", "Kopie", "Speditionsauftrag"),
        entwuerfe=entwuerfe,
        fixtermin=_fixtermin(),
    )
    ordner = wurzel / ORDNERNAME
    assert dossier.name == ORDNERNAME
    assert ordner.is_dir()
    assert Path(dossier.ordner) == ordner
    dateien = _dateien(ordner)
    assert DECKBLATT in dateien and INDEX_DATEI in dateien and PAKET_DATEI in dateien and FIXTERMIN_DATEI in dateien
    kd = [d for d in dateien if d.startswith("01_Kundenbestellung_")]
    assert len(kd) == 1 and " – Kd – 24.10.2026.pdf" in kd[0] and " – WIK – 4711 – " in kd[0]
    assert any(d.startswith("02_Bestellung_") and " – Cde – " in d for d in dateien)
    assert any(d.startswith("03_Auftragskopie_") and " – Kopie – " in d for d in dateien)
    assert any(d.startswith("04_Speditionsauftrag_Spedition Muster – Speditionsauftrag – Beispielgut – 4711 – ") for d in dateien)
    mails = [d for d in dateien if d.startswith(f"{MAIL_ORDNER}/")]
    assert mails == [
        f"{MAIL_ORDNER}/20261024_090500_bestellung_weingut.eml",
        f"{MAIL_ORDNER}/20261024_090600_auftragskopie_kunde.eml",
        f"{MAIL_ORDNER}/20261024_090700_spedition_auftrag.eml",
    ]
    assert not any(d.startswith("05_Lieferschein") for d in dateien)
    assert GESAMT_PDF not in dateien  # Standard: aus

    # Kopfdaten
    assert dossier.firma_id == "wik_fh_seh"
    assert dossier.kunde_kurz == "Musterhandel" and dossier.lieferant_kurz == "Beispielgut"
    assert dossier.kundenbestellnummer == "4711"
    assert dossier.bestellnummer == "BE-1001" and dossier.ab_nummer == "AB-2002"
    assert dossier.fixtermin == date(2026, 11, 6)
    assert dossier.spedition == "Spedition Muster"
    assert dossier.vorgeschlagener_zielordner == "Aufträge ohne Provision"  # wie die Ablage (ablage.struktur)
    assert dossier.erstellt_am == T1 and dossier.aktualisiert_am == T1

    # Checkliste und offene Punkte
    assert dossier.checkliste["bestellung_gesendet"] is True
    assert dossier.checkliste["auftragskopie_gesendet"] is True
    assert dossier.checkliste["spedition_beauftragt"] is True
    assert dossier.checkliste["lieferant_bestaetigt"] is False
    assert dossier.checkliste["zugestellt"] is False
    assert "Bestätigung des Lieferanten steht aus" in dossier.offene_punkte
    assert "Bestätigung der Spedition steht aus" in dossier.offene_punkte
    assert "Keine Referenz der Spedition bekannt (Tracking nicht möglich)" in dossier.offene_punkte
    assert not any("fehlt im Dossier" in p for p in dossier.offene_punkte)
    assert not any("E-Mail-Entwurf" in p for p in dossier.offene_punkte)

    # Index und Dateitypen
    typen = {d.name: d.typ for d in dossier.dateien}
    assert typen[DECKBLATT] == "deckblatt" and typen[FIXTERMIN_DATEI] == "fixtermin"
    assert typen[kd[0]] == "kundenbestellung"
    assert sorted(set(typen.values())) == ["auftragskopie", "bestellung", "deckblatt", "fixtermin", "kundenbestellung", "mail", "speditionsauftrag"]
    assert all(d.groesse and d.groesse > 0 for d in dossier.dateien)
    gespeichert = Dossier.model_validate_json((ordner / INDEX_DATEI).read_text(encoding="utf-8"))
    assert gespeichert == dossier
    assert Belegpaket.model_validate_json((ordner / PAKET_DATEI).read_text(encoding="utf-8")).roh.bestellnummer == "4711"
    assert (ordner / DECKBLATT).read_bytes().startswith(b"%PDF")
    text = (ordner / FIXTERMIN_DATEI).read_text(encoding="utf-8")
    assert "Zeile 12" in text and "06.11.2026" in text and "Beispielgut | Musterhandel" in text and "KW | Datum" in text

    # Entwurf-Kopie ist eine gültige .eml mit X-Unsent
    eml = email.message_from_bytes((ordner / mails[0]).read_bytes(), policy=email.policy.default)
    assert eml["X-Unsent"] == "1" and eml["To"] == "frau.beispiel@example.org"
    assert "Frau Beispiel" in eml.get_body(preferencelist=("plain",)).get_content()


def test_idempotent_zweiter_lauf_ohne_duplikate(verwaltung: DossierVerwaltung, wurzel: Path, tmp_path: Path, uhr: dict[str, datetime]):
    paket = _paket(tmp_path)
    pdfs = _lexware_pdfs(tmp_path, "Cde", "Kopie")
    entwuerfe = [_entwurf(EntwurfTyp.BESTELLUNG_WEINGUT)]
    erster = verwaltung.erstelle_oder_aktualisiere(paket, _ergebnis(paket), lexware_pdfs=pdfs, entwuerfe=entwuerfe, fixtermin=_fixtermin())
    ordner = wurzel / ORDNERNAME
    vorher = _dateien(ordner)
    deckblatt_vorher = (ordner / DECKBLATT).read_bytes()

    uhr["jetzt"] = T2
    zweiter = verwaltung.erstelle_oder_aktualisiere(paket, _ergebnis(paket), lexware_pdfs=pdfs, entwuerfe=entwuerfe, fixtermin=_fixtermin())
    nachher = _dateien(ordner)
    assert nachher == vorher
    assert not any("_2." in d for d in nachher)
    assert len(list(wurzel.iterdir())) == 1
    assert zweiter.name == erster.name
    assert zweiter.erstellt_am == T1 and zweiter.aktualisiert_am == T2
    assert len(zweiter.dateien) == len(erster.dateien)
    # Deckblatt wurde neu erzeugt (Erzeugungszeit im Inhalt), Belege blieben unverändert
    assert (ordner / DECKBLATT).read_bytes() != deckblatt_vorher
    deck = next(d for d in zweiter.dateien if d.name == DECKBLATT)
    assert deck.hinzugefuegt == T2
    cde = next(d for d in zweiter.dateien if d.typ == "bestellung")
    assert cde.hinzugefuegt == T1


def test_geaenderte_datei_gleichen_namens_bekommt_suffix(verwaltung: DossierVerwaltung, wurzel: Path, tmp_path: Path):
    paket = _paket(tmp_path)
    verwaltung.erstelle_oder_aktualisiere(paket, _ergebnis(paket), lexware_pdfs=_lexware_pdfs(tmp_path, "Cde"))
    anders = {"Cde": _pdf(tmp_path / "pdf2" / "Cde.pdf", "Beleg Cde mit deutlich längerem Inhalt, damit die Größe abweicht", seiten=2)}
    dossier = verwaltung.erstelle_oder_aktualisiere(paket, _ergebnis(paket), lexware_pdfs=anders)
    bestellungen = sorted(d for d in _dateien(wurzel / ORDNERNAME) if d.startswith("02_Bestellung_"))
    assert len(bestellungen) == 2
    assert bestellungen[1].endswith("_2.pdf")
    assert sorted(d.name for d in dossier.dateien if d.typ == "bestellung") == bestellungen
    # das Original ist unverändert (nie überschreiben)
    assert PdfReader(str(wurzel / ORDNERNAME / bestellungen[0])).pages.__len__() == 1


def test_vorhandenes_dossier_wird_ueber_nummern_wiedergefunden(verwaltung: DossierVerwaltung, wurzel: Path, tmp_path: Path):
    paket = _paket(tmp_path)
    erster = verwaltung.erstelle_oder_aktualisiere(paket, _ergebnis(paket))
    # zweite Buchung desselben Vorgangs an einem anderen Tag (anderer Ordnername) -> fortschreiben
    spaeter = _ergebnis(paket)
    spaeter.zeitpunkt = datetime(2026, 10, 27, 10, 0)
    zweiter = verwaltung.erstelle_oder_aktualisiere(paket, spaeter, lexware_pdfs=_lexware_pdfs(tmp_path, "Kopie"))
    assert zweiter.name == erster.name
    assert [p.name for p in wurzel.iterdir()] == [ORDNERNAME]
    assert any(d.typ == "auftragskopie" for d in zweiter.dateien)


def test_oc_nachtrag_ohne_ergebnis_behaelt_den_stichtag(verwaltung: DossierVerwaltung, wurzel: Path, tmp_path: Path):
    """Regression (Review Befund 10): Der Oc-Nachtrag (Überwachungslauf, ohne Buchungsergebnis) benennt mit dem
    Buchungsdatum des Dossiers (24.10.), nicht mit dem Belegdatum (20.10.) – keine zweite Kd-Datei, Oc passend.
    Ein vorgegebenes Dossier wird auch ohne Kundenbestellnummer fortgeschrieben."""
    paket = _paket(tmp_path)
    erstes = verwaltung.erstelle_oder_aktualisiere(paket, _ergebnis(paket), lexware_pdfs=_lexware_pdfs(tmp_path, "Cde"))
    assert erstes.stichtag == date(2026, 10, 24)
    oc = _pdf(tmp_path / "mail" / "conferma.pdf", "Bestaetigung Weingut")
    nachtrag = verwaltung.erstelle_oder_aktualisiere(paket, None, lexware_pdfs={"Oc": oc})
    assert nachtrag.name == erstes.name
    namen = [p.name for p in (wurzel / erstes.name).iterdir()]
    assert not [n for n in namen if "20.10.2026" in n]
    assert [n for n in namen if n.startswith("01_Kundenbestellung_")] == ["01_Kundenbestellung_Beispielgut – WIK – 4711 – Kd – 24.10.2026.pdf"]
    assert any(n.startswith("08_") and n.endswith("Oc – 24.10.2026.pdf") for n in namen)
    # ohne Kundenbestellnummer und ohne Ergebnis: das vorgegebene Dossier wird fortgeschrieben, kein neues angelegt
    ohne_nr = paket.model_copy(deep=True)
    ohne_nr.roh.bestellnummer = None
    zweites_oc = _pdf(tmp_path / "mail" / "conferma2.pdf", "Bestaetigung Weingut 2")
    weiter = verwaltung.erstelle_oder_aktualisiere(ohne_nr, None, lexware_pdfs={"Oc": zweites_oc}, dossier=nachtrag)
    assert weiter.name == erstes.name and len([p for p in wurzel.iterdir() if p.is_dir()]) == 1


def test_revision_benennt_neu_und_neu_kopie(verwaltung: DossierVerwaltung, wurzel: Path, tmp_path: Path):
    paket = _paket(tmp_path, ist_revision=True)
    dossier = verwaltung.erstelle_oder_aktualisiere(paket, _ergebnis(paket), lexware_pdfs=_lexware_pdfs(tmp_path, "Cde", "Kopie"))
    dateien = _dateien(wurzel / ORDNERNAME)
    assert any(d.startswith("02_Bestellung_") and " – Neu – " in d for d in dateien)
    assert any(d.startswith("03_Auftragskopie_") and " – Neu Kopie – " in d for d in dateien)
    assert {d.typ for d in dossier.dateien if d.name.startswith(("02_", "03_"))} == {"revision"}
    assert not any("(Cde) fehlt" in p or "(Kopie) fehlt" in p for p in dossier.offene_punkte)


def test_weinstrasse_agentur_mit_angebot_und_provision(verwaltung: DossierVerwaltung, wurzel: Path, tmp_path: Path):
    paket = _paket(tmp_path, "weinstrasse_agentur")
    dossier = verwaltung.erstelle_oder_aktualisiere(paket, _ergebnis(paket, einkauf=None, angebot="AN-3003"), lexware_pdfs=_lexware_pdfs(tmp_path, "Cde", "Kopie", "Speditionsauftrag"))
    assert dossier.name == "Beispielgut-WS-Musterhandel-4711-24.10.2026"
    assert dossier.bestellnummer == "AN-3003"
    assert dossier.vorgeschlagener_zielordner == "Aufträge mit Provision"  # wie die Ablage (ablage.struktur)
    dateien = _dateien(wurzel / dossier.name)
    assert any(d.startswith("02_Bestellung_Beispielgut – Musterhandel – Cde – ") for d in dateien)


# ---------------------------------------------------------------------------
# Checkliste, offene Punkte
# ---------------------------------------------------------------------------


def test_checkliste_aus_fixtermin_und_sendung(verwaltung: DossierVerwaltung, tmp_path: Path):
    paket = _paket(tmp_path)
    sendung = Sendung(spedition="Spedition Muster", referenz="DEMB00000", status=SendungStatus.ZUGESTELLT, quelle="mail")
    dossier = verwaltung.erstelle_oder_aktualisiere(
        paket, _ergebnis(paket), lexware_pdfs=_lexware_pdfs(tmp_path, "Cde", "Kopie", "Speditionsauftrag"),
        entwuerfe=[_entwurf(EntwurfTyp.BESTELLUNG_WEINGUT), _entwurf(EntwurfTyp.AUFTRAGSKOPIE_KUNDE), _entwurf(EntwurfTyp.SPEDITION_AUFTRAG)],
        fixtermin=_fixtermin(lieferant_bestaetigt=True), sendung=sendung,
    )
    assert all(dossier.checkliste[k] for k in ("bestellung_gesendet", "auftragskopie_gesendet", "spedition_beauftragt", "lieferant_bestaetigt", "spedition_bestaetigt", "abgeholt", "zugestellt"))
    assert dossier.tracking_referenz == "DEMB00000"
    assert dossier.offene_punkte == []


def test_checkliste_haekchen_bleiben_bei_spaeterem_lauf(verwaltung: DossierVerwaltung, tmp_path: Path):
    paket = _paket(tmp_path)
    verwaltung.erstelle_oder_aktualisiere(paket, _ergebnis(paket), fixtermin=_fixtermin(lieferant_bestaetigt=True))
    dossier = verwaltung.erstelle_oder_aktualisiere(paket, _ergebnis(paket))
    assert dossier.checkliste["lieferant_bestaetigt"] is True
    assert dossier.checkliste["spedition_bestaetigt"] is False


def test_sendung_bestaetigt_setzt_nur_passende_haekchen(verwaltung: DossierVerwaltung, tmp_path: Path):
    paket = _paket(tmp_path)
    sendung = Sendung(spedition="Spedition Muster", referenz="DEMB00000", status=SendungStatus.BESTAETIGT, quelle="mail", geplante_abholung=date(2026, 11, 3))
    dossier = verwaltung.erstelle_oder_aktualisiere(paket, _ergebnis(paket), sendung=sendung)
    assert dossier.checkliste["spedition_beauftragt"] is True
    assert dossier.checkliste["spedition_bestaetigt"] is True
    assert dossier.checkliste["abgeholt"] is False and dossier.checkliste["zugestellt"] is False
    assert dossier.spedition == "Spedition Muster"


def test_offene_punkte_fehlende_pdfs_kontakte_und_hinweise(verwaltung: DossierVerwaltung, tmp_path: Path):
    paket = _paket(tmp_path, liefertag=None)
    paket.lieferschein_erstellen = True
    paket.hinweise = [
        Pruefhinweis(code=Pruefcode.PREIS_ABWEICHUNG, schweregrad=Schweregrad.WARNUNG, text="Preis weicht ab", position_laufnr=1),
        Pruefhinweis(code=Pruefcode.BELEGFELD_FEHLT, schweregrad=Schweregrad.FEHLER, text="Shipment fehlt"),
        Pruefhinweis(code=Pruefcode.FIRMA_GELERNT, schweregrad=Schweregrad.INFO, text="gelernt"),
    ]
    entwurf_ohne = _entwurf(EntwurfTyp.BESTELLUNG_WEINGUT, an=[])
    entwurf_ohne.hinweise.append("Anhang fehlt und wurde nicht beigefügt: Cde.pdf")
    ergebnis = _ergebnis(paket, dry_run=True)
    ergebnis.warnungen.append("USt-IdNr.-Dialog übersprungen")
    dossier = verwaltung.erstelle_oder_aktualisiere(
        paket, ergebnis, lexware_pdfs={"Kopie": tmp_path / "nicht-da.pdf"}, entwuerfe=[entwurf_ohne],
    )
    punkte = dossier.offene_punkte
    assert "Bestellung ans Weingut (Cde) fehlt im Dossier (PDF nicht vorhanden)" in punkte
    assert "Auftragskopie an den Kunden (Kopie) fehlt im Dossier (PDF nicht vorhanden)" in punkte
    assert "Speditionsauftrag (Speditionsauftrag) fehlt im Dossier (PDF nicht vorhanden)" in punkte
    assert "Lieferschein (Ls) fehlt im Dossier (PDF nicht vorhanden)" in punkte
    assert any(p.startswith("Auftragskopie an den Kunden (Kopie): PDF") and "nicht gefunden" in p for p in punkte)
    assert "Kein Empfänger für den E-Mail-Entwurf 'bestellung_weingut' – Kontakt in der Kontaktliste fehlt" in punkte
    assert "E-Mail-Entwurf 'bestellung_weingut': Anhang fehlt und wurde nicht beigefügt: Cde.pdf" in punkte
    assert "E-Mail-Entwurf 'Auftragskopie an den Kunden' fehlt" in punkte
    assert "[FEHLER] BELEGFELD_FEHLT: Shipment fehlt" in punkte
    assert "[WARNUNG] PREIS_ABWEICHUNG (Pos. 1): Preis weicht ab" in punkte
    assert not any("FIRMA_GELERNT" in p for p in punkte)
    assert any(p.startswith("Kein Fixtermin bekannt") for p in punkte)
    assert "Dry Run: Belege wurden nicht in Lexware gespeichert (keine Belegnummern)" in punkte
    assert "USt-IdNr.-Dialog übersprungen" in punkte
    assert dossier.checkliste["bestellung_gesendet"] is False  # Entwurf ohne Empfänger zählt nicht
    assert dossier.bestellnummer is None and dossier.ab_nummer is None  # Dry Run: keine Nummern
    assert len(punkte) == len(set(punkte))


def test_quelldatei_fehlt_wird_gemeldet(verwaltung: DossierVerwaltung, tmp_path: Path):
    paket = _paket(tmp_path)
    paket.roh.quelldatei = tmp_path / "weg.pdf"
    dossier = verwaltung.erstelle_oder_aktualisiere(paket, _ergebnis(paket))
    assert any(p.startswith("Kundenbestellung (Kd): Quelldatei") for p in dossier.offene_punkte)
    assert "Kundenbestellung (Kd) fehlt im Dossier (PDF nicht vorhanden)" in dossier.offene_punkte


def test_quelldatei_als_eml_und_weitere_dateien(verwaltung: DossierVerwaltung, wurzel: Path, tmp_path: Path):
    eml = _eml(tmp_path / "eingang" / "bestellung.eml", typ=None)
    paket = _paket(tmp_path, quelldatei=eml)
    weitere = _pdf(tmp_path / "extra" / "Rabattliste_Herbst.pdf", "Rabatte")
    dossier = verwaltung.erstelle_oder_aktualisiere(paket, _ergebnis(paket), quelldatei=eml, weitere_dateien=[weitere, tmp_path / "fehlt.pdf"])
    dateien = _dateien(wurzel / ORDNERNAME)
    assert any(d.startswith("01_Kundenbestellung_") and d.endswith(" – Kd – 24.10.2026.eml") for d in dateien)
    assert "09_Weitere_Rabattliste_Herbst.pdf" in dateien
    assert next(d.typ for d in dossier.dateien if d.name == "09_Weitere_Rabattliste_Herbst.pdf") == "rabattliste"
    assert any(p.startswith("Weitere Datei") and "fehlt.pdf" in p for p in dossier.offene_punkte)


def test_entwurf_aus_eml_datei_wird_kopiert(verwaltung: DossierVerwaltung, wurzel: Path, tmp_path: Path):
    quelle = _eml(tmp_path / "entwuerfe" / "x.eml", typ="bestellung_weingut")
    entwurf = _entwurf(EntwurfTyp.BESTELLUNG_WEINGUT, speicherort=str(quelle))
    paket = _paket(tmp_path)
    dossier = verwaltung.erstelle_oder_aktualisiere(paket, _ergebnis(paket), entwuerfe=[entwurf])
    kopie = wurzel / ORDNERNAME / MAIL_ORDNER / "20261024_090500_bestellung_weingut.eml"
    assert kopie.read_bytes() == quelle.read_bytes()
    assert next(d for d in dossier.dateien if d.typ == "mail").quelle == "entwurf:bestellung_weingut:x.eml"


def test_zwei_entwuerfe_gleichen_typs_und_gleicher_groesse_bleiben_beide(verwaltung: DossierVerwaltung, wurzel: Path, tmp_path: Path, uhr: dict[str, datetime]):
    """Regression (Integration Stufe 2): Bestellungen an zwei Weingüter entstehen in derselben Sekunde und haben
    denselben technischen Namen ``06_Mails/<zeit>_bestellung_weingut.eml`` – bei gleicher Größe wurde die zweite
    früher als "bereits vorhanden" übersprungen. Jetzt entscheidet der Inhalt; ein zweiter Lauf bleibt idempotent."""
    erste = _eml(tmp_path / "entwuerfe" / "a.eml", typ="bestellung_weingut")
    zweite = _eml(tmp_path / "entwuerfe" / "b.eml", typ="bestellung_weingut")
    zweite.write_bytes(erste.read_bytes().replace(b"Your Ref: 4711", b"Your Ref: 4712"))
    assert erste.stat().st_size == zweite.stat().st_size and erste.read_bytes() != zweite.read_bytes()
    entwuerfe = [_entwurf(EntwurfTyp.BESTELLUNG_WEINGUT, speicherort=str(erste)), _entwurf(EntwurfTyp.BESTELLUNG_WEINGUT, speicherort=str(zweite))]
    paket = _paket(tmp_path)
    dossier = verwaltung.erstelle_oder_aktualisiere(paket, _ergebnis(paket), entwuerfe=entwuerfe)
    mails = sorted(d.name for d in dossier.dateien if d.typ == "mail")
    assert mails == [f"{MAIL_ORDNER}/20261024_090500_bestellung_weingut.eml", f"{MAIL_ORDNER}/20261024_090500_bestellung_weingut_2.eml"]
    assert (wurzel / ORDNERNAME / mails[1]).read_bytes() == zweite.read_bytes()
    uhr["jetzt"] = T2
    wieder = verwaltung.erstelle_oder_aktualisiere(paket, _ergebnis(paket), entwuerfe=entwuerfe)
    assert sorted(d.name for d in wieder.dateien if d.typ == "mail") == mails
    assert len(list((wurzel / ORDNERNAME / MAIL_ORDNER).iterdir())) == 2


def test_entwurf_mit_graph_id_wird_als_eml_gebaut(verwaltung: DossierVerwaltung, wurzel: Path, tmp_path: Path):
    anhang = _pdf(tmp_path / "pdf" / "Cde.pdf", "Cde")
    entwurf = _entwurf(EntwurfTyp.SPEDITION_AUFTRAG, speicherort="AAMkAGI2THVSAAA=", anhaenge=[anhang, tmp_path / "fehlt.pdf"], an=["dispo@example.org"])
    paket = _paket(tmp_path)
    verwaltung.erstelle_oder_aktualisiere(paket, _ergebnis(paket), entwuerfe=[entwurf])
    kopie = wurzel / ORDNERNAME / MAIL_ORDNER / "20261024_090500_spedition_auftrag.eml"
    nachricht = email.message_from_bytes(kopie.read_bytes(), policy=email.policy.default)
    assert nachricht["To"] == "dispo@example.org"
    assert nachricht["X-Auftragsautomat-Typ"] == "spedition_auftrag"
    assert [a.get_filename() for a in nachricht.iter_attachments()] == ["Cde.pdf"]
    # deterministisch: gleicher Entwurf -> gleiche Bytes
    assert eml_aus_entwurf(entwurf, entwurf.erstellt_am) == eml_aus_entwurf(entwurf, entwurf.erstellt_am)
    assert mail_dateiname(datetime(2026, 1, 2, 3, 4, 5), "a b/c") == f"{MAIL_ORDNER}/20260102_030405_a_b_c.eml"


# ---------------------------------------------------------------------------
# Suchen, Auflisten, Mails ergänzen
# ---------------------------------------------------------------------------


def test_finde_dossier(verwaltung: DossierVerwaltung, tmp_path: Path):
    assert verwaltung.finde_dossier() is None
    assert verwaltung.finde_dossier(kundenbestellnummer="4711") is None  # Wurzel existiert noch nicht
    paket = _paket(tmp_path)
    sendung = Sendung(spedition="Spedition Muster", referenz="DEMB00000", status=SendungStatus.BESTAETIGT, quelle="mail")
    verwaltung.erstelle_oder_aktualisiere(paket, _ergebnis(paket), sendung=sendung)
    assert verwaltung.finde_dossier(kundenbestellnummer="4711").name == ORDNERNAME
    assert verwaltung.finde_dossier(kundenbestellnummer=" 47 11 ").name == ORDNERNAME
    assert verwaltung.finde_dossier(bestellnummer="BE-1001").name == ORDNERNAME
    assert verwaltung.finde_dossier(bestellnummer="ab-2002").name == ORDNERNAME
    assert verwaltung.finde_dossier(referenz="demb00000").name == ORDNERNAME
    assert verwaltung.finde_dossier(kundenbestellnummer="4711", firma_id="wik_fh_seh").name == ORDNERNAME
    assert verwaltung.finde_dossier(kundenbestellnummer="4711", firma_id="woe_leh") is None
    assert verwaltung.finde_dossier(kundenbestellnummer="9999") is None
    assert verwaltung.finde_dossier(bestellnummer="9999", referenz="DEMB99999") is None


def test_liste_und_lade_ignorieren_kaputte_indexdateien(verwaltung: DossierVerwaltung, wurzel: Path, tmp_path: Path):
    paket_a = _paket(tmp_path)
    verwaltung.erstelle_oder_aktualisiere(paket_a, _ergebnis(paket_a))
    paket_b = _paket(tmp_path, "woe_leh")
    paket_b.roh.bestellnummer = "0815"
    verwaltung.erstelle_oder_aktualisiere(paket_b, _ergebnis(paket_b, einkauf="BE-5"))
    (wurzel / "Fremder Ordner").mkdir()
    (wurzel / "Kaputt").mkdir()
    (wurzel / "Kaputt" / INDEX_DATEI).write_text("{ kein json", encoding="utf-8")
    (wurzel / "datei.txt").write_text("x", encoding="utf-8")
    namen = [d.name for d in verwaltung.liste()]
    assert namen == sorted([ORDNERNAME, "Beispielgut-WOE-Musterhandel-0815-24.10.2026"], key=str.casefold)
    assert verwaltung.lade("Fremder Ordner") is None
    assert verwaltung.lade("Kaputt") is None
    assert verwaltung.finde_dossier(kundenbestellnummer="0815").firma_id == "woe_leh"


def test_ergaenze_mail(verwaltung: DossierVerwaltung, wurzel: Path, tmp_path: Path, uhr: dict[str, datetime]):
    paket = _paket(tmp_path)
    dossier = verwaltung.erstelle_oder_aktualisiere(paket, _ergebnis(paket), lexware_pdfs=_lexware_pdfs(tmp_path, "Cde"))
    deckblatt_vorher = (wurzel / ORDNERNAME / DECKBLATT).read_bytes()
    uhr["jetzt"] = T2
    mail = _eml(tmp_path / "posteingang" / "bestaetigung.eml")
    aktualisiert = verwaltung.ergaenze_mail(dossier, mail, checkliste={"spedition_bestaetigt": True})
    ziel = wurzel / ORDNERNAME / MAIL_ORDNER / "20261026_140000_spedition_bestaetigung.eml"
    assert ziel.read_bytes() == mail.read_bytes()
    assert aktualisiert.checkliste["spedition_bestaetigt"] is True
    assert aktualisiert.aktualisiert_am == T2
    gespeichert = verwaltung.lade(ORDNERNAME)
    assert gespeichert is not None
    assert gespeichert.checkliste["spedition_bestaetigt"] is True
    assert any(d.name.endswith("spedition_bestaetigung.eml") and d.typ == "mail" for d in gespeichert.dateien)
    # Deckblatt neu erzeugt (mit gespeichertem Belegpaket) und Beleg unverändert
    assert (wurzel / ORDNERNAME / DECKBLATT).read_bytes() != deckblatt_vorher
    assert any(d.typ == "bestellung" for d in gespeichert.dateien)
    # zweites Ergänzen derselben Mail: kein Duplikat
    verwaltung.ergaenze_mail(gespeichert, mail)
    assert len(list((wurzel / ORDNERNAME / MAIL_ORDNER).iterdir())) == 1
    # Typ-Parameter und fehlende Kopfzeile
    ohne_typ = _eml(tmp_path / "posteingang" / "ohne.eml", typ=None, datum=datetime(2026, 10, 27, 8, 0))
    verwaltung.ergaenze_mail(gespeichert, ohne_typ)
    verwaltung.ergaenze_mail(gespeichert, ohne_typ, typ="lieferant_bestaetigung")
    mails = sorted(p.name for p in (wurzel / ORDNERNAME / MAIL_ORDNER).iterdir())
    assert "20261027_080000_eingang.eml" in mails and "20261027_080000_lieferant_bestaetigung.eml" in mails
    with pytest.raises(FileNotFoundError):
        verwaltung.ergaenze_mail(gespeichert, tmp_path / "gibt-es-nicht.eml")


# ---------------------------------------------------------------------------
# Gesamt-PDF, Fixtermin-Text, Fabrik
# ---------------------------------------------------------------------------


def test_gesamt_pdf_fuehrt_alle_pdfs_zusammen(verwaltung: DossierVerwaltung, wurzel: Path, tmp_path: Path, settings: Settings):
    settings.dossier.gesamt_pdf = True
    paket = _paket(tmp_path, quelldatei=_pdf(tmp_path / "eingang" / "kd.pdf", "Kd", seiten=2))
    pdfs = {"Cde": _pdf(tmp_path / "pdf" / "Cde.pdf", "Cde", seiten=1), "Kopie": _pdf(tmp_path / "pdf" / "Kopie.pdf", "Kopie", seiten=3)}
    dossier = verwaltung.erstelle_oder_aktualisiere(paket, _ergebnis(paket), lexware_pdfs=pdfs, entwuerfe=[_entwurf(EntwurfTyp.BESTELLUNG_WEINGUT)])
    ordner = wurzel / ORDNERNAME
    gesamt = ordner / GESAMT_PDF
    assert gesamt.is_file()
    deckblatt_seiten = len(PdfReader(str(ordner / DECKBLATT)).pages)
    assert len(PdfReader(str(gesamt)).pages) == deckblatt_seiten + 2 + 1 + 3
    eintrag = next(d for d in dossier.dateien if d.name == GESAMT_PDF)
    assert eintrag.typ == "gesamt_pdf" and eintrag.groesse == gesamt.stat().st_size
    # zweiter Lauf: Gesamt-PDF wird ersetzt, nicht dupliziert
    verwaltung.erstelle_oder_aktualisiere(paket, _ergebnis(paket), lexware_pdfs=pdfs)
    assert [p.name for p in ordner.iterdir() if p.name.startswith("Dossier_komplett")] == [GESAMT_PDF]
    assert len(PdfReader(str(gesamt)).pages) == deckblatt_seiten + 2 + 1 + 3


def test_gesamt_pdf_ueberspringt_defekte_pdfs(verwaltung: DossierVerwaltung, wurzel: Path, tmp_path: Path, settings: Settings):
    settings.dossier.gesamt_pdf = True
    kaputt = tmp_path / "pdf" / "Cde.pdf"
    kaputt.parent.mkdir(parents=True, exist_ok=True)
    kaputt.write_bytes(b"%PDF-1.4 kaputt")
    paket = _paket(tmp_path)
    dossier = verwaltung.erstelle_oder_aktualisiere(paket, _ergebnis(paket), lexware_pdfs={"Cde": kaputt})
    ordner = wurzel / ORDNERNAME
    assert (ordner / GESAMT_PDF).is_file()
    assert len(PdfReader(str(ordner / GESAMT_PDF)).pages) == len(PdfReader(str(ordner / DECKBLATT)).pages) + 1
    assert any(d.name == GESAMT_PDF for d in dossier.dateien)


def test_fixtermin_text():
    text = fixtermin_text(_fixtermin(verladedatum=date(2026, 11, 4), bemerkung="Best.-Nr. 4711", bestellnummer="BE-1001", tracking_referenz="DEMB00000"), stand=T1)
    zeilen = text.splitlines()
    assert zeilen[0] == "Fixterminüberwachung – Zeile 12"
    assert zeilen[2].startswith("KW | Datum | Tag | Lieferant | Kunde")
    assert zeilen[3] == "45 | 06.11.2026 | Freitag | Beispielgut | Musterhandel | 24.10.2026 | 04.11.2026 | nein | nein | nein | Best.-Nr. 4711"
    assert "Bestellnummer (Lexware): BE-1001" in zeilen
    assert "Referenz der Spedition: DEMB00000" in zeilen
    assert zeilen[-1] == "Stand: 24.10.2026 09:30"
    assert "noch nicht eingetragen" in fixtermin_text(_fixtermin(zeile=None))


def test_aus_settings_dry_run_nutzt_lokalen_ordner(settings: Settings, tmp_path: Path):
    settings.dossier.lokaler_ordner = tmp_path / "lokal"
    settings.dry_run = True
    settings.m365.backend = "graph"
    v = DossierVerwaltung.aus_settings(settings)
    assert isinstance(v.backend, FakeDateiBackend)
    assert v.wurzel.lokaler_pfad == tmp_path / "lokal"


def test_aus_settings_scharf_mit_backend_und_wurzel(settings: Settings, tmp_path: Path):
    settings.dossier.lokaler_ordner = tmp_path / "lokal"
    settings.dry_run = False
    settings.m365.backend = "lokal"
    backend = FakeDateiBackend(tmp_path / "onedrive")
    # ohne konfigurierten Ordner: Rückfall auf den lokalen Ordner (mit Warnung)
    v = DossierVerwaltung.aus_settings(settings, datei_backend=backend)
    assert v.wurzel.lokaler_pfad == tmp_path / "lokal"
    settings.m365.dossier_ordner = DateiRefKonfig(lokaler_pfad=tmp_path / "onedrive" / "vorangefertigte Dossiers")
    v = DossierVerwaltung.aus_settings(settings, datei_backend=backend)
    assert v.backend is backend
    assert v.wurzel.lokaler_pfad == tmp_path / "onedrive" / "vorangefertigte Dossiers"
    paket = _paket(tmp_path)
    dossier = v.erstelle_oder_aktualisiere(paket, _ergebnis(paket))
    assert (tmp_path / "onedrive" / "vorangefertigte Dossiers" / dossier.name / INDEX_DATEI).is_file()
    assert json.loads((tmp_path / "onedrive" / "vorangefertigte Dossiers" / dossier.name / INDEX_DATEI).read_text(encoding="utf-8"))["name"] == dossier.name
