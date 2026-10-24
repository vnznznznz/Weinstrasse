"""Tests für lexware/nachbereitung.py und die Einbindung in lexware.buchen.

Alles synthetisch: Kunde "Beispielmarkt Nord GmbH", Lieferant "Weingut Beispiel Srl", Spedition "Hillebrand"
(Kontaktliste tests/fixtures/excel/ansprechpartner_beispiel.xlsx, Domain example.org), Fixterminüberwachung
tests/fixtures/excel/fixterminueberwachung_beispiel.xlsx (KW 20–23/2026). Es wird nie etwas in die Fixtures
geschrieben (Hash-Prüfung), nichts gesendet; alle Ausgaben landen in tmp_path.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from auftragsautomat.benennung import name_fuer_paket, normalisiere, pruefe_auftragskopie_name
from auftragsautomat.config import DateiRefKonfig, Settings
from auftragsautomat.lexware import buchen as buchen_modul
from auftragsautomat.lexware import nachbereitung as nbm
from auftragsautomat.lexware.buchen import buche
from auftragsautomat.m365.basis import M365Fehler
from auftragsautomat.m365.fabrik import Backends
from auftragsautomat.m365.fake import FakeDateiBackend, FakeExcelBackend, FakeMailBackend
from auftragsautomat.models import (
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
    Kunde,
    KundeZuordnung,
    Lieferant,
    LieferantZuordnung,
    MatchStatus,
    PositionsZuordnung,
    RohBestellung,
    RohPosition,
)

REPO = Path(__file__).resolve().parents[1]
REPO_CONFIG = REPO / "config"
FIXTURES = Path(__file__).parent / "fixtures"
FIXTERMIN_XLSX = FIXTURES / "excel" / "fixterminueberwachung_beispiel.xlsx"
KONTAKTLISTE_XLSX = FIXTURES / "excel" / "ansprechpartner_beispiel.xlsx"
FIRMA = "wik_leh"
LIEFERTAG = date(2026, 5, 15)  # Freitag, KW 20, alle Slots frei (Zeilen 21-25)
ZEITPUNKT = datetime(2026, 5, 6, 9, 30)
STICHTAG = ZEITPUNKT.date()
PDF_BYTES = b"%PDF-1.4\n%synthetischer Beleg\n"


# ---------------------------------------------------------------------------
# Fixtures und Hilfen
# ---------------------------------------------------------------------------


def _sha(pfad: Path) -> str:
    return hashlib.sha256(pfad.read_bytes()).hexdigest()


@pytest.fixture()
def nb_settings(settings: Settings, tmp_path: Path) -> Settings:
    settings.lexware.pdf_ausgabe_ordner = tmp_path / "logs" / "pdf"
    settings.kommunikation.vorlagen_ordner = REPO_CONFIG / "vorlagen"
    settings.kommunikation.signaturen_datei = REPO_CONFIG / "signaturen.yaml"
    settings.kommunikation.anrede_datei = REPO_CONFIG / "anrede.yaml"
    settings.kommunikation.entwuerfe_ordner = tmp_path / "logs" / "entwuerfe"
    settings.kommunikation.absender_name = "Max Beispiel"
    settings.kommunikation.absender_email = "logistik@example.org"
    settings.kommunikation.cc_standard = ["kollegin@example.org"]
    settings.dossier.lokaler_ordner = tmp_path / "logs" / "dossiers"
    settings.ablage.lokaler_ordner = tmp_path / "logs" / "ablage"
    settings.tracking.hillebrand_konfig = REPO_CONFIG / "tracking_hillebrand.yaml"
    settings.m365.fixtermin_datei = DateiRefKonfig(lokaler_pfad=FIXTERMIN_XLSX, blatt="Fixterminüberwachung")
    settings.m365.kontaktliste_datei = DateiRefKonfig(lokaler_pfad=KONTAKTLISTE_XLSX)
    return settings


def _quelldatei(settings: Settings, name: str = "bestellung_beispielmarkt.pdf") -> Path:
    pfad = settings.eingang_ordner / FIRMA / name
    pfad.parent.mkdir(parents=True, exist_ok=True)
    pfad.write_bytes(PDF_BYTES)
    return pfad


def _paket(settings: Settings, *, liefertag: date | None = LIEFERTAG, freigegeben: bool = True) -> Belegpaket:
    roh = RohBestellung(
        eingangstyp=Eingangstyp.KUNDENBESTELLUNG_PDF,
        quelldatei=_quelldatei(settings),
        extraktionsmethode=Extraktionsmethode.PDFPLUMBER,
        kunde_text="Beispielmarkt Nord",
        lieferant_text="Weingut Beispiel",
        belegnummer="BM-2026-0005",
        bestellnummer="4711",
        datum=date(2026, 5, 4),
        liefertag=liefertag,
        positionen=[
            RohPosition(laufnr=1, bezeichnung="Beispielwein Rosso 0,75 l", menge=Decimal(120), preis=Decimal("6.90")),
            RohPosition(laufnr=2, bezeichnung="Beispielwein Bianco 0,75 l", menge=Decimal(60), preis=Decimal("5.90")),
        ],
    )
    kunde = Kunde(firma_id=FIRMA, kundennummer="2500", name="Beispielmarkt Nord GmbH", matchcode="Beispielmarkt", ort="Beispielstadt")
    lieferant = Lieferant(firma_id=FIRMA, lieferantennummer="150", name="Weingut Beispiel Srl", matchcode="Beispiel", land="IT")
    rosso = Artikel(firma_id=FIRMA, artikelnummer="9001", bezeichnung="Beispielwein Rosso 0,75 l", warengruppe="Beispiel", lieferantennummer="150", ek_preis=Decimal("4.20"), vk_preis_1=Decimal("6.90"))
    bianco = Artikel(firma_id=FIRMA, artikelnummer="9002", bezeichnung="Beispielwein Bianco 0,75 l", warengruppe="Beispiel", lieferantennummer="150", ek_preis=Decimal("3.80"), vk_preis_1=Decimal("5.90"))
    return Belegpaket(
        roh=roh,
        firma_id=FIRMA,
        firma_quelle="alias",
        kunde=KundeZuordnung(kunde=kunde, status=MatchStatus.SICHER, quelle="alias"),
        lieferant=LieferantZuordnung(lieferant=lieferant, status=MatchStatus.SICHER, quelle="alias"),
        positionen=[
            PositionsZuordnung(roh=roh.positionen[0], artikel=rosso, status=MatchStatus.SICHER, menge=Decimal(120), ek_preis=Decimal("4.20"), vk_preis=Decimal("6.90"), preis_dokument=Decimal("6.90")),
            PositionsZuordnung(roh=roh.positionen[1], artikel=bianco, status=MatchStatus.SICHER, menge=Decimal(60), ek_preis=Decimal("3.80"), vk_preis=Decimal("5.90"), preis_dokument=Decimal("5.90")),
        ],
        belegfelder=Belegfelder(spedition="Hillebrand", weingut="Weingut Beispiel Srl", shipment="15.05.2026", kunde="Beispielmarkt Nord GmbH"),
        einkauf_erforderlich=True,
        freigegeben=freigegeben,
    )


def _ergebnis(paket: Belegpaket, settings: Settings, *, dry_run: bool = True, pdfs: dict[str, Path] | None = None) -> BuchungsErgebnis:
    def beleg(art: Belegart, nummer: str) -> Belegergebnis:
        return Belegergebnis(
            belegart=art, belegnummer=None if dry_run else nummer, erfolgreich=True, dry_run=dry_run,
            methode="dry_run" if dry_run else "ui_automation", netto=paket.summe_netto_vk,
        )

    if pdfs is None:
        pdfs = {k: Path(settings.lexware.pdf_ausgabe_ordner) / name_fuer_paket(k, paket, settings, datum=ZEITPUNKT.date()) for k in ("Cde", "Speditionsauftrag", "Kopie")}
    return BuchungsErgebnis(
        firma_id=FIRMA,
        quelldatei=paket.roh.quelldatei,
        dry_run=dry_run,
        zeitpunkt=ZEITPUNKT,
        angebot=beleg(Belegart.ANGEBOT, "AN-2026-0100"),
        einkauf=beleg(Belegart.BESTELLUNG, "BE-2026-0100"),
        verkauf=beleg(Belegart.AUFTRAGSBESTAETIGUNG, "AB-2026-0100"),
        pdfs=pdfs,
    )


def _echte_pdfs(paket: Belegpaket, settings: Settings, ordner: Path, *, datum: date = STICHTAG) -> dict[str, Path]:
    ordner.mkdir(parents=True, exist_ok=True)
    pdfs: dict[str, Path] = {}
    for k in ("Cde", "Speditionsauftrag", "Kopie"):
        pfad = ordner / name_fuer_paket(k, paket, settings, datum=datum)
        pfad.write_bytes(PDF_BYTES + k.encode("utf-8"))
        pdfs[k] = pfad
    return pdfs


def _eml_text(entwurf: EmailEntwurf) -> str:
    assert entwurf.speicherort
    return Path(entwurf.speicherort).read_bytes().decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Gesamtlauf im Dry Run
# ---------------------------------------------------------------------------


def test_nachbereiten_dry_run_komplett(nb_settings: Settings, tmp_path: Path):
    hash_vorher = _sha(FIXTERMIN_XLSX)
    paket = _paket(nb_settings)
    ergebnis = _ergebnis(paket, nb_settings)

    nb = nbm.nachbereiten(paket, ergebnis, nb_settings)

    assert nb.dry_run is True and nb.zeitpunkt == ZEITPUNKT
    # Fixtermin: erster freier Slot des 15.05.2026 (Zeile 21), nur Vorschau – Datei unverändert
    assert nb.fixtermin is not None
    assert nb.fixtermin.liefertag == LIEFERTAG and nb.fixtermin.zeile == 21
    assert (nb.fixtermin.lieferant_kurz, nb.fixtermin.kunde_kurz) == ("Beispiel", "Beispielmarkt")
    assert nb.fixtermin.spedition == "Hillebrand" and nb.fixtermin.kundenbestellnummer == "4711"
    assert nb.vorschau_csv is not None and nb.vorschau_csv == nb_settings.log_ordner / nbm.VORSCHAU_CSV
    csv_text = nb.vorschau_csv.read_text(encoding="utf-8-sig")
    assert "D21" in csv_text and "Beispielmarkt" in csv_text
    assert _sha(FIXTERMIN_XLSX) == hash_vorher
    assert any("Dry Run: Fixtermin 15.05.2026" in h for h in nb.hinweise)

    # Entwürfe: drei Stück, als .eml gespeichert, Empfänger aus der Kontaktliste, nie gesendet
    assert [e.typ for e in nb.entwuerfe] == [EntwurfTyp.BESTELLUNG_WEINGUT, EntwurfTyp.AUFTRAGSKOPIE_KUNDE, EntwurfTyp.SPEDITION_AUFTRAG]
    bestellung, kopie, sped = nb.entwuerfe
    assert bestellung.an == ["orders@weingut-beispiel.example.org"]
    assert kopie.an and all(a.endswith("@beispielmarkt.example.org") for a in kopie.an)
    assert sped.an == ["neue-auftraege@hillebrand.example.org"]
    for e in nb.entwuerfe:
        assert e.speicherort and Path(e.speicherort).is_file() and Path(e.speicherort).parent == nb_settings.kommunikation.entwuerfe_ordner
        assert "X-Unsent: 1" in _eml_text(e)
        assert any("Anhang fehlt" in h for h in e.hinweise)  # Dry Run: PDFs nur geplant
    # Auftragskopie ohne verbotene Wörter (Betreff, Text, Anhangsname)
    assert pruefe_auftragskopie_name(kopie.betreff, nb_settings) == []
    assert "auftragsbestaetigung" not in normalisiere(_eml_text(kopie))
    assert "verbindlich" not in normalisiere(kopie.text_plain)

    # Dossier lokal mit Deckblatt, Kundenbestellung, Mails, Fixtermin-Text, Index
    assert nb.dossier is not None
    ordner = Path(nb.dossier.ordner)
    assert ordner.parent == nb_settings.dossier.lokaler_ordner
    assert nb.dossier.name.startswith("Beispiel-WIK-Beispielmarkt-4711-")
    namen = sorted(p.relative_to(ordner).as_posix() for p in ordner.rglob("*") if p.is_file())
    assert "00_Deckblatt.pdf" in namen and "07_Fixtermin.txt" in namen and "dossier.json" in namen and "paket.json" in namen
    assert any(n.startswith("01_Kundenbestellung_") and n.endswith(" – Kd – 06.05.2026.pdf") for n in namen)
    assert sum(1 for n in namen if n.startswith("06_Mails/") and n.endswith(".eml")) == 3
    assert (ordner / "00_Deckblatt.pdf").read_bytes().startswith(b"%PDF")
    assert "15.05.2026" in (ordner / "07_Fixtermin.txt").read_text(encoding="utf-8")

    # Ablage: nur protokolliert, Kd zuerst, Zielordner "Aufträge ohne Provision" (WIK)
    assert nb.ablage and all(a.dry_run and not a.abgelegt for a in nb.ablage)
    assert [a.kuerzel for a in nb.ablage] == ["Kd", "Cde", "Speditionsauftrag", "Kopie"]
    assert {a.zielordner for a in nb.ablage} == {"Aufträge ohne Provision"}
    assert nb.ablage[0].zielname.endswith(" – Kd – 06.05.2026.pdf")
    assert ergebnis.ablage == nb.ablage

    # JSON neben dem Paket, wieder ladbar
    assert nb.speicherort == nbm.nachbereitung_pfad(paket, nb_settings)
    assert nb.speicherort.name == "bestellung_beispielmarkt.nachbereitung.json"
    geladen = nbm.lade_nachbereitung(paket, nb_settings)
    assert geladen is not None
    assert geladen.fixtermin == nb.fixtermin and geladen.dossier is not None and geladen.dossier.name == nb.dossier.name
    assert [e.typ for e in geladen.entwuerfe] == [e.typ for e in nb.entwuerfe]
    assert geladen.ergebnis is not None and geladen.ergebnis.pdfs == ergebnis.pdfs
    daten = json.loads(nb.speicherort.read_text(encoding="utf-8"))
    assert daten["dry_run"] is True and daten["fixtermin"]["zeile"] == 21

    text = nbm.zusammenfassung_nachbereitung(nb)
    assert "Nachbereitung (Dry Run)" in text and "Fixtermin: 15.05.2026" in text and "Dossier: Beispiel-WIK" in text
    assert "nur protokolliert" in text and "Vorschau-CSV" in text
    assert "Fixtermin 15.05.2026 (Zeile 21, Vorschau)" in nb.kurzfassung and "3 Entwürfe (3 gespeichert)" in nb.kurzfassung


def test_nachbereiten_ohne_liefertag_und_ohne_kontaktliste(nb_settings: Settings):
    nb_settings.m365.kontaktliste_datei = DateiRefKonfig()
    paket = _paket(nb_settings, liefertag=None)
    nb = nbm.nachbereiten(paket, _ergebnis(paket, nb_settings), nb_settings)
    assert nb.fixtermin is None and nb.vorschau_csv is None
    assert any("Kein Liefertag" in h for h in nb.hinweise)
    assert any("Kontaktliste nicht konfiguriert" in h for h in nb.hinweise)
    assert len(nb.entwuerfe) == 3 and all(e.an == [] for e in nb.entwuerfe)
    assert all(any("Kein Empfänger" in h for h in e.hinweise) for e in nb.entwuerfe)
    assert nb.dossier is not None and not (Path(nb.dossier.ordner) / "07_Fixtermin.txt").exists()
    assert "kein Fixtermin" in nb.kurzfassung


def test_nachbereiten_findet_pdfs_nach_hausmuster(nb_settings: Settings, tmp_path: Path):
    paket = _paket(nb_settings)
    # PDFs liegen mit anderem Datum im PDF-Ausgabeordner (Lexware hat sie an einem anderen Tag erzeugt)
    pdfs = _echte_pdfs(paket, nb_settings, Path(nb_settings.lexware.pdf_ausgabe_ordner), datum=date(2026, 5, 5))
    gefunden = nbm.finde_lexware_pdfs(paket, nb_settings)
    assert gefunden == pdfs  # Ls fehlt, die drei anderen werden gefunden
    assert nbm.finde_lexware_pdfs(paket, nb_settings, kuerzel=("Ls",)) == {}

    ergebnis = _ergebnis(paket, nb_settings)  # geplante (nicht vorhandene) Pfade
    nb = nbm.nachbereiten(paket, ergebnis, nb_settings)
    assert sum(1 for h in nb.hinweise if "nach Hausmuster gefunden" in h) == 3
    bestellung, kopie, sped = nb.entwuerfe
    assert [a.name for a in bestellung.anhaenge] == [pdfs["Cde"].name]
    assert [a.name for a in kopie.anhaenge] == [pdfs["Kopie"].name]
    assert pdfs["Speditionsauftrag"].name in [a.name for a in sped.anhaenge]
    assert not any("Anhang fehlt" in h for e in nb.entwuerfe for h in e.hinweise)
    assert nb.dossier is not None
    namen = [p.name for p in Path(nb.dossier.ordner).iterdir()]
    assert any(n.startswith("02_Bestellung_") for n in namen)
    assert any(n.startswith("03_Auftragskopie_") for n in namen)
    assert any(n.startswith("04_Speditionsauftrag_") for n in namen)
    assert not any("fehlt im Dossier" in p for p in nb.dossier.offene_punkte if "(Cde)" in p or "(Kopie)" in p)


def test_nachbereiten_ohne_firma_und_fixtermin_ausserhalb(nb_settings: Settings):
    paket = _paket(nb_settings)
    ergebnis_ohne = _ergebnis(paket, nb_settings)
    paket.firma_id = None
    nb = nbm.nachbereiten(paket, ergebnis_ohne, nb_settings, speichern=False)
    assert nb.fixtermin is None and nb.dossier is None and nb.entwuerfe == [] and nb.speicherort is None
    assert any("keine bekannte Firma" in h for h in nb.hinweise)

    # Liefertag außerhalb des Zeitraums der Excel: Fixtermin bleibt ohne Zeile, Grund im Hinweis, Rest läuft
    paket = _paket(nb_settings, liefertag=date(2026, 9, 15))
    nb = nbm.nachbereiten(paket, _ergebnis(paket, nb_settings), nb_settings)
    assert nb.fixtermin is not None and nb.fixtermin.zeile is None and nb.vorschau_csv is None
    assert any("nicht eingetragen" in h for h in nb.hinweise)
    assert nb.dossier is not None and len(nb.entwuerfe) == 3
    assert "Fixtermin 15.09.2026 (nicht eingetragen)" in nb.kurzfassung


# ---------------------------------------------------------------------------
# Scharf mit injizierten Backends
# ---------------------------------------------------------------------------


def test_nachbereiten_scharf_schreibt_register_und_legt_ab(nb_settings: Settings, tmp_path: Path):
    hash_vorher = _sha(FIXTERMIN_XLSX)
    auftraege = tmp_path / "OneDrive" / "Aufträge"
    auftraege.mkdir(parents=True)
    nb_settings.ablage.aktiv = True
    nb_settings.ablage.auftraege_ordner = DateiRefKonfig(lokaler_pfad=auftraege)
    excel = FakeExcelBackend(aus_xlsx=FIXTERMIN_XLSX, jetzt=lambda: ZEITPUNKT)  # "echtes" Backend im Test
    mail = FakeMailBackend(nb_settings.kommunikation.entwuerfe_ordner, jetzt=lambda: ZEITPUNKT.astimezone())
    backends = Backends(excel=excel, mail=mail, datei=FakeDateiBackend(tmp_path / "wurzel"), backend="graph")
    paket = _paket(nb_settings)
    pdfs = _echte_pdfs(paket, nb_settings, Path(nb_settings.lexware.pdf_ausgabe_ordner))
    ergebnis = _ergebnis(paket, nb_settings, dry_run=False, pdfs=pdfs)

    nb = nbm.nachbereiten(paket, ergebnis, nb_settings, backends=backends)

    assert nb.dry_run is False and nb.vorschau_csv is None
    assert nb.fixtermin is not None and nb.fixtermin.zeile == 21
    assert nb.fixtermin.bestellnummer == "BE-2026-0100" and nb.fixtermin.ab_nummer == "AB-2026-0100"
    assert excel.schreibprotokoll and excel.schreibprotokoll[0].adresse.startswith("D21")
    assert excel.werte(nb_settings.m365.fixtermin_datei, "Fixterminüberwachung")[20][3] == "Beispiel"  # im Speicher …
    assert _sha(FIXTERMIN_XLSX) == hash_vorher  # … nie in der Datei
    assert any("in Zeile 21 der Fixterminüberwachung eingetragen" in h for h in nb.hinweise)
    assert len(mail.entwuerfe) == 3 and all(Path(e.speicherort).is_file() for e in mail.entwuerfe)  # type: ignore[arg-type]
    # Ablage scharf: Kd + drei PDFs liegen im Provisionsordner
    assert nb.ablage and all(a.abgelegt and not a.dry_run and a.fehler is None for a in nb.ablage)
    ziel = auftraege / "Aufträge ohne Provision"
    abgelegt = sorted(p.name for p in ziel.iterdir())
    assert len(abgelegt) == 4 and any(" – Kd – " in n for n in abgelegt) and pdfs["Cde"].name in abgelegt
    assert "Ablage 4/4 Dateien" in nb.kurzfassung
    # Dossier: scharf, aber Datei-Backend ist der injizierte Fake -> lokal in dossier.lokaler_ordner (nie OneDrive im Test)
    assert nb.dossier is not None and Path(nb.dossier.ordner).parent == nb_settings.dossier.lokaler_ordner


# ---------------------------------------------------------------------------
# Einbindung in buche()
# ---------------------------------------------------------------------------


def test_buche_ruft_nachbereitung_auf(nb_settings: Settings):
    paket = _paket(nb_settings)
    ergebnis = buche(paket, nb_settings, dry_run=True)
    assert ergebnis.erfolgreich and ergebnis.dry_run
    assert any(w.startswith("Nachbereitung: Fixtermin 15.05.2026 (Zeile 21, Vorschau)") for w in ergebnis.warnungen)
    assert any(w.startswith("Nachbereitung gespeichert:") for w in ergebnis.warnungen)
    nb = nbm.lade_nachbereitung(paket, nb_settings)
    assert nb is not None and nb.dossier is not None and len(nb.entwuerfe) == 3
    assert ergebnis.ablage and all(a.dry_run for a in ergebnis.ablage)  # lege_ab trägt ins Ergebnis ein
    assert (nb_settings.log_ordner / nbm.VORSCHAU_CSV).is_file()
    assert (nb_settings.log_ordner / "protokoll.csv").is_file()


def test_buche_fehler_der_nachbereitung_kippt_buchung_nicht(nb_settings: Settings, monkeypatch: pytest.MonkeyPatch):
    def kaputt(*_a, **_k):
        raise RuntimeError("Graph nicht erreichbar")

    monkeypatch.setattr(nbm, "nachbereiten", kaputt)
    paket = _paket(nb_settings)
    ergebnis = buche(paket, nb_settings, dry_run=True)
    assert ergebnis.erfolgreich is True
    assert any("Nachbereitung nicht ausgeführt (RuntimeError: Graph nicht erreichbar)" in w for w in ergebnis.warnungen)
    assert ergebnis.protokoll is not None and ergebnis.protokoll.is_file()
    assert nbm.lade_nachbereitung(paket, nb_settings) is None


def test_buche_ohne_erfolg_keine_nachbereitung(nb_settings: Settings, monkeypatch: pytest.MonkeyPatch):
    aufrufe: list[str] = []
    monkeypatch.setattr(nbm, "nachbereiten", lambda *a, **k: aufrufe.append("x"))
    paket = _paket(nb_settings, freigegeben=False)
    ergebnis = buche(paket, nb_settings, dry_run=True)
    assert not ergebnis.erfolgreich and aufrufe == []
    assert buchen_modul._nachbereiten is not None  # Hook existiert


# ---------------------------------------------------------------------------
# Backends, Vorschau-Excel, gemischtes Mail-Backend
# ---------------------------------------------------------------------------


def test_erzeuge_backends_fuer_fake_und_dry_run_graph(nb_settings: Settings, monkeypatch: pytest.MonkeyPatch):
    fake = nbm.erzeuge_backends_fuer(nb_settings, dry_run=False)
    assert fake.backend == "fake" and isinstance(fake.excel, FakeExcelBackend) and isinstance(fake.mail, FakeMailBackend)
    assert fake.mail.entwuerfe_ordner == nb_settings.kommunikation.entwuerfe_ordner  # type: ignore[union-attr]
    assert fake.datei.wurzel == nb_settings.dossier.lokaler_ordner  # type: ignore[union-attr]

    echt_excel = FakeExcelBackend(aus_xlsx=FIXTERMIN_XLSX)
    nb_settings.m365.backend = "graph"
    monkeypatch.setattr(nbm, "erzeuge_backends", lambda s: Backends(excel=echt_excel, mail=FakeMailBackend(nb_settings.kommunikation.entwuerfe_ordner), datei=FakeDateiBackend(nb_settings.dossier.lokaler_ordner), backend="graph"))
    hinweise: list[str] = []
    vorschau = nbm.erzeuge_backends_fuer(nb_settings, dry_run=True, hinweise=hinweise)
    assert vorschau.backend == nbm.BACKEND_VORSCHAU and isinstance(vorschau.excel, nbm.VorschauExcelBackend) and hinweise == []
    ref = nb_settings.m365.fixtermin_datei
    assert vorschau.excel.blattnamen(ref) == echt_excel.blattnamen(ref)
    vorher = echt_excel.werte(ref, "Fixterminüberwachung")[20][3]
    vorschau.excel.schreibe_bereich(ref, "Fixterminüberwachung", "D21:E21", [["Testgut", None]])
    assert echt_excel.werte(ref, "Fixterminüberwachung")[20][3] == vorher  # nichts geschrieben
    assert len(vorschau.excel.schreibprotokoll) == 1 and vorschau.excel.schreibprotokoll[0].zellen == [("D21", "Testgut", None)]
    csv_pfad = vorschau.excel.vorschau_csv(nb_settings.log_ordner / "vorschau_test.csv")
    assert "D21;Testgut" in csv_pfad.read_text(encoding="utf-8-sig")

    def kaputt(_s):
        raise M365Fehler("keine Anmeldung")

    monkeypatch.setattr(nbm, "erzeuge_backends", kaputt)
    hinweise = []
    rueckfall = nbm.erzeuge_backends_fuer(nb_settings, dry_run=True, hinweise=hinweise)
    assert rueckfall.backend == "fake" and any("Fake-Backends" in h for h in hinweise)
    with pytest.raises(M365Fehler):
        nbm.erzeuge_backends_fuer(nb_settings, dry_run=False)


def test_lese_schreib_mail_backend_und_ueberwachungsbackends(nb_settings: Settings, tmp_path: Path):
    from auftragsautomat.m365.basis import Mail, MailKopf

    posteingang = [Mail(kopf=MailKopf(id="m1", betreff="Test", absender="a@example.org", empfangen=datetime(2026, 5, 6, 8, 0).astimezone(), hat_anhaenge=False), text="Hallo")]
    lesen = FakeMailBackend(tmp_path / "lesen", posteingang)
    schreiben = FakeMailBackend(tmp_path / "schreiben")
    gemischt = nbm.LeseSchreibMailBackend(lesen, schreiben)
    koepfe = gemischt.suche_mails(seit=datetime(2026, 5, 1).astimezone())
    assert [k.id for k in koepfe] == ["m1"] and gemischt.lese_mail("m1").text == "Hallo"
    entwurf = EmailEntwurf(typ=EntwurfTyp.ERINNERUNG_INTERN, an=["logistik@example.org"], betreff="Erinnerung", text_html="<p>x</p>", text_plain="x")
    ort = gemischt.erstelle_entwurf(entwurf)
    assert Path(ort).parent == tmp_path / "schreiben" and gemischt.entwuerfe == [entwurf] and lesen.entwuerfe == []

    nur_lesen = nbm.backends_fuer_ueberwachung(nb_settings, nur_lesen=True)
    assert nur_lesen.backend == "fake" and isinstance(nur_lesen.mail, FakeMailBackend)
    standard = nbm.backends_fuer_ueberwachung(nb_settings)
    assert standard.backend == "fake"  # settings.m365.backend == "fake" -> immer Fake


def test_nachbereitung_pfad_und_laden_defekt(nb_settings: Settings):
    paket = _paket(nb_settings)
    pfad = nbm.nachbereitung_pfad(paket, nb_settings)
    assert pfad == nb_settings.log_ordner / "pakete" / "bestellung_beispielmarkt.nachbereitung.json"
    assert nbm.nachbereitung_pfad(Path("x/bestellung_beispielmarkt.paket.json"), nb_settings) == pfad
    assert nbm.lade_nachbereitung(paket, nb_settings) is None
    pfad.parent.mkdir(parents=True, exist_ok=True)
    pfad.write_text("{kaputt", encoding="utf-8")
    assert nbm.lade_nachbereitung(paket, nb_settings) is None
    gespeichert = nbm.speichere_nachbereitung(nbm.Nachbereitung(hinweise=["a"]), paket, nb_settings)
    assert gespeichert == pfad
    geladen = nbm.lade_nachbereitung(paket, nb_settings)
    assert geladen is not None and geladen.hinweise == ["a"] and geladen.speicherort == pfad
    assert not list(pfad.parent.glob("*.tmp"))
