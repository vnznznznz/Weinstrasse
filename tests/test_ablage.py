"""Tests für ablage/ (Ablage fertiger Dateien in OneDrive "Aufträge").

Alle Namen, Nummern und Pfade sind synthetisch ("Weingut Beispiel", "Weinhandel Beispiel", "4711").
Das Backend ist der FakeDateiBackend auf tmp_path; nichts geht nach OneDrive.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from pathlib import Path

import pytest

from auftragsautomat.ablage import (
    KUERZEL_KUNDENBESTELLUNG,
    AblageFehler,
    ablage_ref,
    freier_name,
    lege_ab,
    quellen,
    zielordner,
)
from auftragsautomat.ablage import ablage as ablage_modul
from auftragsautomat.benennung import name_fuer_paket
from auftragsautomat.config import DateiRefKonfig, Settings
from auftragsautomat.m365.basis import M365Fehler
from auftragsautomat.m365.fake import FakeDateiBackend
from auftragsautomat.models import (
    Belegart,
    Belegergebnis,
    Belegpaket,
    BuchungsErgebnis,
    Eingangstyp,
    Extraktionsmethode,
    Kunde,
    KundeZuordnung,
    Lieferant,
    LieferantZuordnung,
    MatchStatus,
    RohBestellung,
)

STICHTAG = date(2026, 10, 24)
ZEITPUNKT = datetime(2026, 10, 24, 10, 30)


# ---------------------------------------------------------------------------
# Fixtures und Hilfen
# ---------------------------------------------------------------------------


@pytest.fixture()
def auftraege(tmp_path: Path, settings: Settings) -> Path:
    """OneDrive-Ordner "Aufträge" (lokal synchronisiert) als Ziel der Ablage; Ablage scharf geschaltet."""
    ordner = tmp_path / "OneDrive" / "Aufträge"
    ordner.mkdir(parents=True)
    settings.ablage.auftraege_ordner = DateiRefKonfig(lokaler_pfad=ordner)
    settings.ablage.aktiv = True
    settings.ablage.lokaler_ordner = tmp_path / "logs" / "ablage"
    return ordner


@pytest.fixture()
def backend(tmp_path: Path) -> FakeDateiBackend:
    return FakeDateiBackend(tmp_path / "fake_wurzel")


def _kunde(firma_id: str, name: str = "Weinhandel Beispiel", matchcode: str | None = "Beispiel") -> Kunde:
    return Kunde(firma_id=firma_id, kundennummer="10001", name=name, matchcode=matchcode)


def _lieferant(firma_id: str, name: str = "Weingut Beispiel", matchcode: str | None = "Weingut Beispiel") -> Lieferant:
    return Lieferant(firma_id=firma_id, lieferantennummer="70001", name=name, matchcode=matchcode)


def _paket(
    tmp_path: Path,
    firma_id: str = "wik_fh_seh",
    *,
    quelle_endung: str = ".pdf",
    bestellnummer: str | None = "4711",
    kunde: Kunde | None = None,
    lieferant: Lieferant | None = None,
    freigegeben: bool = True,
) -> Belegpaket:
    quelle = tmp_path / "eingang" / f"bestellung{quelle_endung}"
    quelle.parent.mkdir(parents=True, exist_ok=True)
    quelle.write_bytes(b"%PDF-1.4 Kundenbestellung" if quelle_endung == ".pdf" else b"Subject: Bestellung\n\nText")
    roh = RohBestellung(
        eingangstyp=Eingangstyp.KUNDENBESTELLUNG_PDF,
        quelldatei=quelle,
        extraktionsmethode=Extraktionsmethode.PDFPLUMBER,
        kunde_text="Weinhandel Beispiel GmbH",
        lieferant_text="Weingut Beispiel",
        bestellnummer=bestellnummer,
        datum=date(2026, 10, 20),
    )
    paket = Belegpaket(roh=roh, firma_id=firma_id, freigegeben=freigegeben)
    paket.kunde = KundeZuordnung(kunde=kunde or _kunde(firma_id), status=MatchStatus.SICHER, quelle="alias")
    paket.lieferant = LieferantZuordnung(lieferant=lieferant or _lieferant(firma_id), status=MatchStatus.SICHER, quelle="alias")
    return paket


def _beleg(belegart: Belegart, *, erfolgreich: bool = True, dry_run: bool = False) -> Belegergebnis:
    return Belegergebnis(belegart=belegart, belegnummer=None if dry_run else "AB-1", erfolgreich=erfolgreich, dry_run=dry_run)


def _ergebnis(
    paket: Belegpaket,
    settings: Settings,
    pdf_ordner: Path,
    *,
    kuerzel: tuple[str, ...] = ("Cde", "Kopie"),
    dry_run: bool = False,
    erfolgreich: bool = True,
) -> BuchungsErgebnis:
    """BuchungsErgebnis mit PDFs, die (wie in lexware/) nach Titulierung benannt sind."""
    pdf_ordner.mkdir(parents=True, exist_ok=True)
    pdfs: dict[str, Path] = {}
    for k in kuerzel:
        pfad = pdf_ordner / name_fuer_paket(k, paket, settings, datum=STICHTAG)
        pfad.write_bytes(f"%PDF-1.4 {k}".encode())
        pdfs[k] = pfad
    return BuchungsErgebnis(
        firma_id=paket.firma_id or "",
        quelldatei=paket.roh.quelldatei,
        dry_run=dry_run,
        zeitpunkt=ZEITPUNKT,
        einkauf=_beleg(Belegart.BESTELLUNG, erfolgreich=erfolgreich, dry_run=dry_run),
        verkauf=_beleg(Belegart.AUFTRAGSBESTAETIGUNG, erfolgreich=erfolgreich, dry_run=dry_run),
        pdfs=pdfs,
    )


# ---------------------------------------------------------------------------
# Zielordner
# ---------------------------------------------------------------------------


def test_zielordner_fester_text(settings: Settings, tmp_path: Path):
    """ablage.struktur darf auch fester Text ohne Platzhalter sein; Schrägstriche am Rand fallen weg."""
    settings.ablage.struktur = "Sammelordner"
    assert zielordner(_paket(tmp_path), settings) == "Sammelordner"
    settings.ablage.struktur = "/Sammelordner/"
    assert zielordner(_paket(tmp_path), settings) == "Sammelordner"


def test_zielordner_provision_je_firma(settings: Settings, tmp_path: Path):
    assert settings.ablage.struktur == "{provision}"
    assert zielordner(_paket(tmp_path, "weinstrasse_agentur"), settings) == "Aufträge mit Provision"
    for firma_id in ("wik_fh_seh", "wik_leh", "woe_leh"):
        assert zielordner(_paket(tmp_path, firma_id), settings) == "Aufträge ohne Provision"
    # ohne Firma: "ohne Provision"
    paket = _paket(tmp_path)
    paket.firma_id = None
    assert zielordner(paket, settings) == "Aufträge ohne Provision"


def test_zielordner_alle_platzhalter(settings: Settings, tmp_path: Path):
    settings.ablage.struktur = "{provision}/{firma}/{jahr}/{monat}/{lieferant}/{kunde}"
    paket = _paket(tmp_path)  # wik_fh_seh: ablage_ordner "WIK FH"
    assert zielordner(paket, settings, datum=STICHTAG) == "Aufträge ohne Provision/WIK FH/2026/10/Weingut Beispiel/Beispiel"
    # Firma ohne ablage_ordner -> Kürzel; Kunde ohne Matchcode -> Name; verbotene Zeichen -> Ersatzzeichen
    settings.firma("wik_fh_seh").ablage_ordner = None
    paket.kunde.kunde = _kunde("wik_fh_seh", name="Wein/Haus: Beispiel", matchcode=None)
    assert zielordner(paket, settings, datum=STICHTAG) == "Aufträge ohne Provision/WIK/2026/10/Weingut Beispiel/Wein-Haus- Beispiel"


def test_zielordner_ohne_firma_und_heute(settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    settings.ablage.struktur = "{firma}/{jahr}-{monat}"
    monkeypatch.setattr(ablage_modul, "_heute", lambda: date(2027, 1, 5))
    paket = _paket(tmp_path)
    paket.firma_id = None
    assert zielordner(paket, settings) == "Unbekannt/2027-01"


def test_zielordner_unbekannter_platzhalter(settings: Settings, tmp_path: Path):
    settings.ablage.struktur = "{provision}/{kundengruppe}"
    with pytest.raises(AblageFehler, match="kundengruppe"):
        zielordner(_paket(tmp_path), settings)
    settings.ablage.struktur = "{provision"
    with pytest.raises(AblageFehler):
        zielordner(_paket(tmp_path), settings)


def test_zielordner_leer_bedeutet_wurzel(settings: Settings, tmp_path: Path):
    settings.ablage.struktur = ""
    assert zielordner(_paket(tmp_path), settings) == ""


# ---------------------------------------------------------------------------
# Freier Name
# ---------------------------------------------------------------------------


def test_freier_name_zaehlt_hoch(backend: FakeDateiBackend, tmp_path: Path):
    ref = DateiRefKonfig(lokaler_pfad=tmp_path / "ziel")
    name = "Weingut Beispiel – WIK – 4711 – Cde – 24.10.2026.pdf"
    assert freier_name(backend, ref, "Aufträge ohne Provision", name) == name
    backend.ordner_anlegen(ref, "Aufträge ohne Provision")
    (tmp_path / "ziel" / "Aufträge ohne Provision" / name).write_bytes(b"1")
    assert freier_name(backend, ref, "Aufträge ohne Provision", name) == "Weingut Beispiel – WIK – 4711 – Cde – 24.10.2026 (2).pdf"
    (tmp_path / "ziel" / "Aufträge ohne Provision" / "Weingut Beispiel – WIK – 4711 – Cde – 24.10.2026 (2).pdf").write_bytes(b"2")
    assert freier_name(backend, ref, "Aufträge ohne Provision", name) == "Weingut Beispiel – WIK – 4711 – Cde – 24.10.2026 (3).pdf"


def test_freier_name_ohne_endung_und_in_wurzel(backend: FakeDateiBackend, tmp_path: Path):
    ref = DateiRefKonfig(lokaler_pfad=tmp_path / "ziel")
    (tmp_path / "ziel").mkdir()
    name = "Weingut Beispiel – Beispiel – Kd – 24.10.2026"  # Datum am Ende ist keine Endung
    (tmp_path / "ziel" / name).write_bytes(b"1")
    assert freier_name(backend, ref, "", name) == "Weingut Beispiel – Beispiel – Kd – 24.10.2026 (2)"


def test_freier_name_gibt_auf(backend: FakeDateiBackend, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(ablage_modul, "MAX_KOLLISIONEN", 3)
    ref = DateiRefKonfig(lokaler_pfad=tmp_path / "ziel")
    (tmp_path / "ziel").mkdir()
    for n in ("", " (2)", " (3)"):
        (tmp_path / "ziel" / f"x{n}.pdf").write_bytes(b"1")
    with pytest.raises(AblageFehler, match="Kein freier Dateiname"):
        freier_name(backend, ref, "", "x.pdf")


# ---------------------------------------------------------------------------
# Quellen und Namen
# ---------------------------------------------------------------------------


def test_quellen_kd_zuerst_mit_originalendung(settings: Settings, tmp_path: Path):
    paket = _paket(tmp_path, quelle_endung=".eml")
    ergebnis = _ergebnis(paket, settings, tmp_path / "pdf")
    liste = quellen(paket, ergebnis, settings)
    assert [q.kuerzel for q in liste] == ["Kd", "Cde", "Kopie"]
    assert liste[0].pfad == paket.roh.quelldatei
    assert liste[0].zielname == "Weingut Beispiel – WIK – 4711 – Kd – 24.10.2026.eml"
    assert liste[1].zielname == "Weingut Beispiel – WIK – 4711 – Cde – 24.10.2026.pdf"
    assert liste[2].zielname == "Weingut Beispiel – WIK – 4711 – Kopie – 24.10.2026.pdf"
    assert all("Auftragsbestätigung" not in q.zielname for q in liste)


def test_quellen_ws_schema_und_revision(settings: Settings, tmp_path: Path):
    paket = _paket(tmp_path, "weinstrasse_agentur")
    paket.ist_revision = True
    ergebnis = _ergebnis(paket, settings, tmp_path / "pdf", kuerzel=("Cde", "Kopie", "Speditionsauftrag"))
    namen = {q.kuerzel: q.zielname for q in quellen(paket, ergebnis, settings)}
    assert namen["Kd"] == "Weingut Beispiel – Beispiel – Kd – 24.10.2026.pdf"
    assert namen["Cde"] == "Weingut Beispiel – Beispiel – Neu – 24.10.2026.pdf"
    assert namen["Kopie"] == "Weingut Beispiel – Beispiel – Neu Kopie – 24.10.2026.pdf"
    assert namen["Speditionsauftrag"] == "Selbstabholung – Speditionsauftrag – Weingut Beispiel – Beispiel – 24.10.2026.pdf"


def test_quellen_ohne_kd_wenn_abgeschaltet_oder_schon_vorhanden(settings: Settings, tmp_path: Path):
    paket = _paket(tmp_path)
    ergebnis = _ergebnis(paket, settings, tmp_path / "pdf")
    settings.eingang.als_kundenbestellung_ablegen = False
    assert [q.kuerzel for q in quellen(paket, ergebnis, settings)] == ["Cde", "Kopie"]
    settings.eingang.als_kundenbestellung_ablegen = True
    settings.ablage.kundenbestellung_ablegen = False
    assert [q.kuerzel for q in quellen(paket, ergebnis, settings)] == ["Cde", "Kopie"]
    settings.ablage.kundenbestellung_ablegen = True
    ergebnis2 = _ergebnis(paket, settings, tmp_path / "pdf2", kuerzel=("Kd", "Cde"))
    liste = quellen(paket, ergebnis2, settings)
    assert [q.kuerzel for q in liste] == ["Kd", "Cde"] and liste[0].pfad == ergebnis2.pdfs["Kd"]


def test_quellen_nimmt_archivierte_quelldatei(settings: Settings, tmp_path: Path):
    paket = _paket(tmp_path)
    ergebnis = _ergebnis(paket, settings, tmp_path / "pdf")
    archiv = tmp_path / "archiv" / "bestellung.pdf"
    archiv.parent.mkdir(exist_ok=True)
    paket.roh.quelldatei.rename(archiv)  # wie buchen() nach dem scharfen Lauf
    ergebnis.archiv = archiv
    kd = quellen(paket, ergebnis, settings)[0]
    assert kd.kuerzel == KUERZEL_KUNDENBESTELLUNG and kd.pfad == archiv


def test_quellen_unbekanntes_kuerzel_behaelt_quellnamen(settings: Settings, tmp_path: Path, caplog: pytest.LogCaptureFixture):
    paket = _paket(tmp_path)
    ergebnis = _ergebnis(paket, settings, tmp_path / "pdf", kuerzel=("Cde",))
    sonst = tmp_path / "pdf" / "Anlage: Etikett*.pdf"
    sonst.write_bytes(b"%PDF")
    ergebnis.pdfs["Anlage"] = sonst
    with caplog.at_level(logging.WARNING, logger="auftragsautomat.ablage.ablage"):
        liste = quellen(paket, ergebnis, settings)
    assert liste[-1].zielname == "Anlage- Etikett-.pdf"
    assert "Anlage" in caplog.text


# ---------------------------------------------------------------------------
# lege_ab: scharf
# ---------------------------------------------------------------------------


def test_lege_ab_standard_ohne_provision(settings: Settings, tmp_path: Path, auftraege: Path, backend: FakeDateiBackend):
    paket = _paket(tmp_path)
    ergebnis = _ergebnis(paket, settings, tmp_path / "pdf")
    ergebnisse = lege_ab(paket, ergebnis, settings, datei_backend=backend, dry_run=False)

    assert [e.kuerzel for e in ergebnisse] == ["Kd", "Cde", "Kopie"]
    assert all(e.abgelegt and not e.dry_run and e.fehler is None for e in ergebnisse)
    assert all(e.zielordner == "Aufträge ohne Provision" for e in ergebnisse)
    ziel = auftraege / "Aufträge ohne Provision"
    assert sorted(p.name for p in ziel.iterdir()) == sorted(e.zielname for e in ergebnisse)
    assert (ziel / "Weingut Beispiel – WIK – 4711 – Kd – 24.10.2026.pdf").read_bytes() == b"%PDF-1.4 Kundenbestellung"
    assert (ziel / "Weingut Beispiel – WIK – 4711 – Cde – 24.10.2026.pdf").read_bytes() == b"%PDF-1.4 Cde"
    assert all(Path(e.speicherort).is_file() for e in ergebnisse)
    # nichts verschoben oder gelöscht
    assert paket.roh.quelldatei.is_file() and all(p.is_file() for p in ergebnis.pdfs.values())
    assert ergebnis.ablage == ergebnisse
    # nicht im Rückfallordner gelandet
    assert not (tmp_path / "fake_wurzel").exists()


def test_lege_ab_provisionsordner_und_struktur(settings: Settings, tmp_path: Path, auftraege: Path, backend: FakeDateiBackend):
    settings.ablage.struktur = "{provision}/{jahr}/{kunde}"
    ws = _paket(tmp_path, "weinstrasse_agentur")
    e_ws = lege_ab(ws, _ergebnis(ws, settings, tmp_path / "pdf_ws", kuerzel=("Cde", "Kopie")), settings, datei_backend=backend, dry_run=False)
    assert {e.zielordner for e in e_ws} == {"Aufträge mit Provision/2026/Beispiel"}
    assert (auftraege / "Aufträge mit Provision" / "2026" / "Beispiel" / "Weingut Beispiel – Beispiel – Kopie – 24.10.2026.pdf").is_file()

    wik = _paket(tmp_path, "wik_leh")
    e_wik = lege_ab(wik, _ergebnis(wik, settings, tmp_path / "pdf_wik"), settings, datei_backend=backend, dry_run=False)
    assert {e.zielordner for e in e_wik} == {"Aufträge ohne Provision/2026/Beispiel"}
    assert (auftraege / "Aufträge ohne Provision" / "2026" / "Beispiel" / "Weingut Beispiel – WIK – 4711 – Cde – 24.10.2026.pdf").is_file()


def test_lege_ab_kollision_haengt_nummer_an(settings: Settings, tmp_path: Path, auftraege: Path, backend: FakeDateiBackend):
    paket = _paket(tmp_path)
    ergebnis = _ergebnis(paket, settings, tmp_path / "pdf", kuerzel=("Cde",))
    erste = lege_ab(paket, ergebnis, settings, datei_backend=backend, dry_run=False)
    ergebnis.pdfs["Cde"].write_bytes(b"%PDF-1.4 Cde neu")
    zweite = lege_ab(paket, ergebnis, settings, datei_backend=backend, dry_run=False)
    dritte = lege_ab(paket, ergebnis, settings, datei_backend=backend, dry_run=False)

    assert [e.zielname for e in erste] == [
        "Weingut Beispiel – WIK – 4711 – Kd – 24.10.2026.pdf",
        "Weingut Beispiel – WIK – 4711 – Cde – 24.10.2026.pdf",
    ]
    assert [e.zielname for e in zweite] == [
        "Weingut Beispiel – WIK – 4711 – Kd – 24.10.2026 (2).pdf",
        "Weingut Beispiel – WIK – 4711 – Cde – 24.10.2026 (2).pdf",
    ]
    assert dritte[1].zielname == "Weingut Beispiel – WIK – 4711 – Cde – 24.10.2026 (3).pdf"
    ziel = auftraege / "Aufträge ohne Provision"
    assert (ziel / erste[1].zielname).read_bytes() == b"%PDF-1.4 Cde"  # Original unangetastet
    assert (ziel / zweite[1].zielname).read_bytes() == b"%PDF-1.4 Cde neu"
    assert len(list(ziel.iterdir())) == 6


def test_lege_ab_ueberschreiben_nur_mit_schalter(settings: Settings, tmp_path: Path, auftraege: Path, backend: FakeDateiBackend):
    settings.ablage.vorhandene_ueberschreiben = True
    paket = _paket(tmp_path)
    ergebnis = _ergebnis(paket, settings, tmp_path / "pdf", kuerzel=("Cde",))
    lege_ab(paket, ergebnis, settings, datei_backend=backend, dry_run=False)
    ergebnis.pdfs["Cde"].write_bytes(b"%PDF-1.4 Cde neu")
    zweite = lege_ab(paket, ergebnis, settings, datei_backend=backend, dry_run=False)
    assert zweite[1].zielname == "Weingut Beispiel – WIK – 4711 – Cde – 24.10.2026.pdf"
    ziel = auftraege / "Aufträge ohne Provision"
    assert len(list(ziel.iterdir())) == 2
    assert (ziel / zweite[1].zielname).read_bytes() == b"%PDF-1.4 Cde neu"


def test_lege_ab_fehler_je_datei(settings: Settings, tmp_path: Path, auftraege: Path, backend: FakeDateiBackend):
    paket = _paket(tmp_path)
    ergebnis = _ergebnis(paket, settings, tmp_path / "pdf", kuerzel=("Cde", "Kopie", "Ls"))
    ergebnis.pdfs["Ls"].unlink()  # Quelldatei fehlt

    class StoerendesBackend(FakeDateiBackend):
        def hochladen(self, lokal: Path, ref: DateiRefKonfig, relativ: str) -> str:
            if "Kopie" in relativ:
                raise M365Fehler("Upload abgelehnt", status=507, code="quotaLimitReached")
            return super().hochladen(lokal, ref, relativ)

    ergebnisse = lege_ab(paket, ergebnis, settings, datei_backend=StoerendesBackend(tmp_path / "w"), dry_run=False)
    nach_kuerzel = {e.kuerzel: e for e in ergebnisse}
    assert nach_kuerzel["Kd"].abgelegt and nach_kuerzel["Cde"].abgelegt
    assert not nach_kuerzel["Kopie"].abgelegt and "quotaLimitReached" in (nach_kuerzel["Kopie"].fehler or "")
    assert not nach_kuerzel["Ls"].abgelegt and "Quelldatei fehlt" in (nach_kuerzel["Ls"].fehler or "")
    assert all(not e.dry_run for e in ergebnisse)
    assert sorted(p.name for p in (auftraege / "Aufträge ohne Provision").iterdir()) == sorted(
        [nach_kuerzel["Kd"].zielname, nach_kuerzel["Cde"].zielname]
    )


def test_lege_ab_zielordner_nicht_anlegbar(settings: Settings, tmp_path: Path, auftraege: Path):
    paket = _paket(tmp_path)
    ergebnis = _ergebnis(paket, settings, tmp_path / "pdf", kuerzel=("Cde",))

    class OhneOrdner(FakeDateiBackend):
        def ordner_anlegen(self, ref: DateiRefKonfig, relativ: str) -> str:
            raise M365Fehler("Zugriff verweigert", status=403, code="accessDenied")

    ergebnisse = lege_ab(paket, ergebnis, settings, datei_backend=OhneOrdner(tmp_path / "w"), dry_run=False)
    assert all(not e.abgelegt and "accessDenied" in (e.fehler or "") for e in ergebnisse)
    assert not (auftraege / "Aufträge ohne Provision").exists()


def test_lege_ab_nur_nach_freigabe_und_erfolg(settings: Settings, tmp_path: Path, auftraege: Path, backend: FakeDateiBackend):
    paket = _paket(tmp_path, freigegeben=False)
    ergebnis = _ergebnis(paket, settings, tmp_path / "pdf")
    ergebnisse = lege_ab(paket, ergebnis, settings, datei_backend=backend, dry_run=False)
    assert ergebnisse and all(not e.abgelegt and not e.dry_run and "nicht freigegeben" in (e.fehler or "") for e in ergebnisse)

    paket.freigegeben = True
    ergebnis.verkauf = _beleg(Belegart.AUFTRAGSBESTAETIGUNG, erfolgreich=False)
    ergebnisse = lege_ab(paket, ergebnis, settings, datei_backend=backend, dry_run=False)
    assert all(not e.abgelegt and "nicht erfolgreich" in (e.fehler or "") for e in ergebnisse)
    assert not (auftraege / "Aufträge ohne Provision").exists()


def test_lege_ab_rueckfall_auf_lokalen_ordner(settings: Settings, tmp_path: Path, backend: FakeDateiBackend, caplog: pytest.LogCaptureFixture):
    settings.ablage.aktiv = True
    settings.ablage.lokaler_ordner = tmp_path / "logs" / "ablage"
    assert settings.ablage.auftraege_ordner.lokaler_pfad is None and settings.ablage.auftraege_ordner.web_url is None
    with caplog.at_level(logging.WARNING, logger="auftragsautomat.ablage.ablage"):
        assert ablage_ref(settings).lokaler_pfad == tmp_path / "logs" / "ablage"
    assert "nicht konfiguriert" in caplog.text
    paket = _paket(tmp_path)
    ergebnisse = lege_ab(paket, _ergebnis(paket, settings, tmp_path / "pdf", kuerzel=("Cde",)), settings, datei_backend=backend, dry_run=False)
    assert all(e.abgelegt for e in ergebnisse)
    assert (tmp_path / "logs" / "ablage" / "Aufträge ohne Provision" / ergebnisse[1].zielname).is_file()


def test_ablage_ref_konfiguriert(settings: Settings):
    settings.ablage.auftraege_ordner = DateiRefKonfig(web_url="https://example.org/personal/x/Documents/Aufträge")
    assert ablage_ref(settings) is settings.ablage.auftraege_ordner
    settings.ablage.auftraege_ordner = DateiRefKonfig(drive_id="b!x", item_id="01ABC")
    assert ablage_ref(settings) is settings.ablage.auftraege_ordner


# ---------------------------------------------------------------------------
# lege_ab: nur Protokoll
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fall", ["dry_run", "inaktiv", "nur_scharf"])
def test_lege_ab_nur_protokoll(settings: Settings, tmp_path: Path, auftraege: Path, backend: FakeDateiBackend, fall: str):
    paket = _paket(tmp_path)
    dry_run = fall == "dry_run"
    if fall == "inaktiv":
        settings.ablage.aktiv = False
    ergebnis = _ergebnis(paket, settings, tmp_path / "pdf", dry_run=(fall == "nur_scharf"))
    ergebnisse = lege_ab(paket, ergebnis, settings, datei_backend=backend, dry_run=dry_run)
    assert [e.kuerzel for e in ergebnisse] == ["Kd", "Cde", "Kopie"]
    assert all(e.dry_run and not e.abgelegt and e.speicherort is None and e.fehler is None for e in ergebnisse)
    assert ergebnisse[1].zielordner == "Aufträge ohne Provision"
    assert ergebnisse[1].zielname == "Weingut Beispiel – WIK – 4711 – Cde – 24.10.2026.pdf"
    assert not (auftraege / "Aufträge ohne Provision").exists() and list(auftraege.iterdir()) == []
    assert ergebnis.ablage == ergebnisse


def test_lege_ab_dry_run_buchung_mit_nur_scharf_aus(settings: Settings, tmp_path: Path, auftraege: Path, backend: FakeDateiBackend):
    """ablage.nur_scharf = false: auch das Ergebnis einer Dry-Run-Buchung wird abgelegt (Kd vorhanden,
    die im Dry Run nicht erzeugte PDF fehlt und wird als Fehler gemeldet)."""
    settings.ablage.nur_scharf = False
    paket = _paket(tmp_path)
    ergebnis = _ergebnis(paket, settings, tmp_path / "pdf", kuerzel=("Cde",), dry_run=True)
    ergebnis.pdfs["Cde"].unlink()
    ergebnisse = lege_ab(paket, ergebnis, settings, datei_backend=backend, dry_run=False)
    assert ergebnisse[0].abgelegt and (auftraege / "Aufträge ohne Provision" / ergebnisse[0].zielname).is_file()
    assert not ergebnisse[1].abgelegt and "Quelldatei fehlt" in (ergebnisse[1].fehler or "")


def test_lege_ab_protokoll_meldet_fehlende_quelle(settings: Settings, tmp_path: Path, auftraege: Path, backend: FakeDateiBackend):
    paket = _paket(tmp_path)
    ergebnis = _ergebnis(paket, settings, tmp_path / "pdf", kuerzel=("Cde",))
    ergebnis.pdfs["Cde"].unlink()
    ergebnisse = lege_ab(paket, ergebnis, settings, datei_backend=backend, dry_run=True)
    assert ergebnisse[0].fehler is None
    assert ergebnisse[1].dry_run and "Quelldatei fehlt" in (ergebnisse[1].fehler or "")


def test_lege_ab_standard_ist_dry_run(settings: Settings, tmp_path: Path, auftraege: Path, backend: FakeDateiBackend):
    paket = _paket(tmp_path)
    ergebnisse = lege_ab(paket, _ergebnis(paket, settings, tmp_path / "pdf"), settings, datei_backend=backend)
    assert all(e.dry_run for e in ergebnisse) and list(auftraege.iterdir()) == []


def test_lege_ab_kaputte_struktur_wirft(settings: Settings, tmp_path: Path, auftraege: Path, backend: FakeDateiBackend):
    settings.ablage.struktur = "{unbekannt}"
    paket = _paket(tmp_path)
    with pytest.raises(AblageFehler):
        lege_ab(paket, _ergebnis(paket, settings, tmp_path / "pdf"), settings, datei_backend=backend)
