"""Tests für ueberwachung/lauf.py – der tägliche Überwachungslauf, komplett mit Fakes.

Stichtag ist fest Montag, 26.10.2026. Fixterminüberwachung als In-Memory-Arbeitsmappe (FakeExcelBackend),
Posteingang aus tests/fixtures/mails/*.eml (FakeMailBackend), Kontaktliste
tests/fixtures/excel/ansprechpartner_beispiel.xlsx, Dossiers in tmp_path (FakeDateiBackend),
Tracking nur über die Mail-Quelle (kein Browser, keine API). Alle Daten sind synthetisch.

Szenarien laut Auftrag:
  A  Fixtermin in 12 Werktagen ohne Bestätigungen -> Nachfrage Lieferant als .eml, gelb
  B  Hillebrand-Bestätigung mit Referenz + Kundenreferenz = Bestellnummer -> Häkchen Spedition, Referenz in
     Bemerkung, Mail im Dossier
  C  Terminänderungsmail -> Vorschlag, Excel unverändert (automatisch aus)
  D  Drossel verhindert zweite Nachfrage am Folgetag
  E  Exception im Tracking landet in bericht.fehler, Lauf läuft weiter
"""

from __future__ import annotations

import email
import email.policy
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from auftragsautomat.config import DateiRefKonfig, Settings
from auftragsautomat.dossier import INDEX_DATEI, MAIL_ORDNER, DossierVerwaltung
from auftragsautomat.kommunikation.entwuerfe import Entwurfsfabrik
from auftragsautomat.kontakte import Kontaktliste, Zuordner, lade_kontaktliste
from auftragsautomat.m365.basis import Mail, MailKopf
from auftragsautomat.m365.fabrik import Backends
from auftragsautomat.m365.fake import FakeDateiBackend, FakeExcelBackend, FakeMailBackend, mail_aus_eml
from auftragsautomat.models import (
    Dossier,
    EntwurfTyp,
    Fixtermin,
    Sendung,
    SendungStatus,
    Ueberwachungsbericht,
)
from auftragsautomat.termine import kalender
from auftragsautomat.termine.register import FixterminRegister, RegisterFehler
from auftragsautomat.tracking.basis import lade_hillebrand_konfig
from auftragsautomat.tracking.hillebrand_mail import MailQuelle
from auftragsautomat.tracking.kette import TrackingKette
from auftragsautomat.ueberwachung import Drossel, Regelwerk, Ueberwachungslauf, eml_aus_mail, ueberwachen
from auftragsautomat.ueberwachung.bericht import lade_letzten_bericht

REPO_CONFIG = Path(__file__).resolve().parents[1] / "config"
MAILS = Path(__file__).parent / "fixtures" / "mails"
KONTAKTLISTE = Path(__file__).parent / "fixtures" / "excel" / "ansprechpartner_beispiel.xlsx"
BLATT = "Fixterminüberwachung"
KOPF = ["KW", "Datum", "Tag", "Lieferant", "Kunde", "Auftragsdatum", "Verladedatum", "Bestätigung des Lieferanten", "Bestätigung der Spedition", "Abgeholt", "Bemerkung"]
HEUTE = date(2026, 10, 26)  # Montag
JETZT = datetime(2026, 10, 26, 7, 5).astimezone()
VON, BIS = date(2026, 10, 19), date(2026, 11, 27)
LIEFERANT, KUNDE = "Weingut Beispiel", "Beispielmarkt"
BESTELLNUMMER = "WIK-2026-0100"

#: (Lieferant, Kunde, Auftragsdatum, Verladedatum, Lief. best., Sped. best., Abgeholt, Bemerkung)
Eintrag = tuple[str, str, date | None, date | None, bool, bool, bool, str]


# ---------------------------------------------------------------------------
# Hilfen
# ---------------------------------------------------------------------------


def _mappe(eintraege: dict[date, list[Eintrag]], *, von: date = VON, bis: date = BIS) -> dict[str, list[list[Any]]]:
    """Arbeitsmappe wie die echte Fixterminüberwachung: Kopfzeile 4, je Werktag fünf Slots."""
    zeilen: list[list[Any]] = [["Fixterminüberwachung – Test"], ["Legende"], [], KOPF]
    tag = von
    while tag <= bis:
        if kalender.ist_werktag(tag):
            liste = eintraege.get(tag, [])
            for slot in range(5):
                zeile: list[Any] = [kalender.kw(tag), tag, kalender.wochentag_kurz(tag), "", "", "", "", False, False, False, ""]
                if slot < len(liste):
                    lieferant, kunde, auftrag, verladung, bl, bs, ab, bemerkung = liste[slot]
                    zeile[3:11] = [lieferant, kunde, auftrag or "", verladung or "", bl, bs, ab, bemerkung]
                zeilen.append(zeile)
        tag += timedelta(days=1)
    return {BLATT: zeilen, "Ressourcen für Auto DropDown": [["Lieferanten", "Kunden"]]}


def _eintrag(liefertag: date, *, auftrag: date = date(2026, 10, 20), verladung: date | None = None, bl: bool = False, bs: bool = False, ab: bool = False, bemerkung: str = f"Best.-Nr {BESTELLNUMMER}, Hillebrand", lieferant: str = LIEFERANT, kunde: str = KUNDE) -> tuple[date, Eintrag]:
    return liefertag, (lieferant, kunde, auftrag, verladung, bl, bs, ab, bemerkung)


def _tage(*paare: tuple[date, Eintrag]) -> dict[date, list[Eintrag]]:
    """Einträge je Liefertag gruppieren (Slots in Reihenfolge der Angabe)."""
    tage: dict[date, list[Eintrag]] = {}
    for tag, eintrag in paare:
        tage.setdefault(tag, []).append(eintrag)
    return tage


def _mail(name: str) -> Mail:
    return mail_aus_eml(MAILS / f"{name}.eml")


@dataclass
class Umgebung:
    settings: Settings
    excel: FakeExcelBackend
    register: FixterminRegister
    mail: FakeMailBackend
    zuordner: Zuordner
    fabrik: Entwurfsfabrik
    dossiers: DossierVerwaltung
    dossier_ordner: Path
    konfig: Any

    @property
    def backends(self) -> Backends:
        return Backends(excel=self.excel, mail=self.mail, datei=FakeDateiBackend(self.dossier_ordner), backend="fake")

    def kette_fabrik(self, ereignisse: list) -> TrackingKette:
        return TrackingKette([MailQuelle(ereignisse, self.konfig)])

    def lauf(self, *, heute: date = HEUTE, kette: Any = None, drossel: Drossel | None = None, **kw: Any) -> Ueberwachungslauf:
        return Ueberwachungslauf(
            self.settings, self.backends, self.register, self.zuordner, self.fabrik,
            kette if kette is not None else self.kette_fabrik, self.dossiers,
            heute=heute, drossel=drossel, jetzt=kw.pop("jetzt", lambda: datetime.combine(heute, JETZT.timetz())), **kw,
        )

    def fixtermin(self, zeile: int) -> Fixtermin:
        self.register.lade(erneut=True)
        ft = self.register.eintrag(zeile)
        assert ft is not None
        return ft

    def entwuerfe(self, typ: EntwurfTyp) -> list:
        return [e for e in self.mail.entwuerfe if e.typ == typ]


@pytest.fixture()
def ue_settings(settings: Settings, tmp_path: Path) -> Settings:
    settings.kommunikation.vorlagen_ordner = REPO_CONFIG / "vorlagen"
    settings.kommunikation.signaturen_datei = REPO_CONFIG / "signaturen.yaml"
    settings.kommunikation.anrede_datei = REPO_CONFIG / "anrede.yaml"  # -> Regelwerk aus config/ueberwachung.yaml
    settings.kommunikation.entwuerfe_ordner = tmp_path / "entwuerfe"
    settings.kommunikation.absender_name = "Max Beispiel"
    settings.kommunikation.absender_email = "logistik@example.org"
    settings.kommunikation.cc_standard = ["kollegin@example.org"]
    settings.tracking.hillebrand_konfig = REPO_CONFIG / "tracking_hillebrand.yaml"
    settings.tracking.quellen = ["mail"]
    settings.ueberwachung.merkdatei = tmp_path / "logs" / "nachfragen.json"
    settings.ueberwachung.berichte_ordner = tmp_path / "logs" / "ueberwachung"
    settings.dossier.lokaler_ordner = tmp_path / "dossiers"
    settings.m365.fixtermin_datei = DateiRefKonfig(blatt=BLATT)
    return settings


@pytest.fixture()
def liste(ue_settings: Settings) -> Kontaktliste:
    return lade_kontaktliste(KONTAKTLISTE, settings=ue_settings)


def _umgebung(ue_settings: Settings, liste: Kontaktliste, tmp_path: Path, eintraege: dict[date, list[Eintrag]], posteingang: list[Mail] = ()) -> Umgebung:  # type: ignore[assignment]
    excel = FakeExcelBackend({"standard": _mappe(eintraege)}, jetzt=lambda: JETZT)
    register = FixterminRegister(excel, ue_settings.m365.fixtermin_datei, bundesland=ue_settings.ueberwachung.bundesland)
    mail = FakeMailBackend(ue_settings.kommunikation.entwuerfe_ordner, list(posteingang), jetzt=lambda: JETZT)
    zuordner = Zuordner(liste, None, ue_settings)
    fabrik = Entwurfsfabrik(ue_settings, zuordner, ue_settings.firma("wik_leh"))
    dossier_ordner = ue_settings.dossier.lokaler_ordner
    dossier_ordner.mkdir(parents=True, exist_ok=True)
    dossiers = DossierVerwaltung(FakeDateiBackend(dossier_ordner), DateiRefKonfig(lokaler_pfad=dossier_ordner), ue_settings, jetzt=lambda: JETZT)
    return Umgebung(ue_settings, excel, register, mail, zuordner, fabrik, dossiers, dossier_ordner, lade_hillebrand_konfig(ue_settings.tracking.hillebrand_konfig))


def _dossier_anlegen(umg: Umgebung, **felder: Any) -> Dossier:
    """Dossier-Ordner mit dossier.json (wie nach dem Buchen), ohne paket.json."""
    basis: dict[str, Any] = {
        "name": f"{LIEFERANT}-WIK-{KUNDE}-{BESTELLNUMMER}-20.10.2026",
        "firma_id": "wik_leh",
        "lieferant_kurz": LIEFERANT,
        "kunde_kurz": KUNDE,
        "bestellnummer": BESTELLNUMMER,
        "fixtermin": date(2026, 11, 9),
        "spedition": "Hillebrand",
        "erstellt_am": datetime(2026, 10, 20, 12, 0),
    }
    basis.update(felder)
    ordner = umg.dossier_ordner / basis["name"]
    ordner.mkdir(parents=True, exist_ok=True)
    dossier = Dossier(ordner=str(ordner), **basis)
    (ordner / INDEX_DATEI).write_text(dossier.model_dump_json(indent=2), encoding="utf-8")
    return dossier


def _eml_dateien(umg: Umgebung, dossier: Dossier) -> list[Path]:
    ordner = umg.dossier_ordner / dossier.name / MAIL_ORDNER
    return sorted(ordner.glob("*.eml")) if ordner.is_dir() else []


class KaputteKette:
    name = "kaputt"

    def __init__(self) -> None:
        self.aufrufe = 0

    def verfuegbar(self) -> bool:
        return True

    def aktualisiere(self, sendung: Sendung) -> Sendung:
        self.aufrufe += 1
        raise RuntimeError("Portal nicht erreichbar")


# ---------------------------------------------------------------- Szenario A


def test_szenario_a_nachfrage_lieferant_als_eml_gelb(ue_settings: Settings, liste: Kontaktliste, tmp_path: Path):
    umg = _umgebung(ue_settings, liste, tmp_path, _tage(_eintrag(date(2026, 11, 11))))  # 12 Werktage
    lauf = umg.lauf()
    bericht = lauf.ausfuehren(dry_run=True)

    assert isinstance(bericht, Ueberwachungsbericht) and bericht.zeitraum_bis == HEUTE + timedelta(days=21)
    assert bericht.zeitpunkt.date() == HEUTE and bericht.fehler == []
    assert len(bericht.punkte) == 1
    punkt = bericht.punkte[0]
    assert punkt.ampel == "gelb" and punkt.fixtermin.liefertag == date(2026, 11, 11) and punkt.fixtermin.bestellnummer == BESTELLNUMMER
    assert "Bestätigung des Lieferanten fehlt (Frist 28.10.2026" in punkt.grund
    assert any(a.startswith("Nachfrage Lieferant: Entwurf 'Order WIK-2026-0100 – Beispielmarkt: confirmation and loading date' an orders@weingut-beispiel.example.org") for a in punkt.aktionen)
    assert any(a.startswith("Nachfrage Spedition: Entwurf 'Statusanfrage – Weingut Beispiel / Beispielmarkt' an info@hillebrand.example.org") for a in punkt.aktionen)

    nachfragen = umg.entwuerfe(EntwurfTyp.NACHFRAGE_LIEFERANT)
    assert len(nachfragen) == 1 and nachfragen[0].an == ["orders@weingut-beispiel.example.org"] and nachfragen[0].cc == ["kollegin@example.org"]
    assert nachfragen[0] in bericht.entwuerfe and nachfragen[0].speicherort and nachfragen[0].speicherort.endswith(".eml")
    eml = Path(nachfragen[0].speicherort)
    assert eml.is_file() and eml.parent == ue_settings.kommunikation.entwuerfe_ordner
    nachricht = email.message_from_bytes(eml.read_bytes(), policy=email.policy.default)
    assert nachricht["X-Unsent"] == "1" and nachricht["To"] == "orders@weingut-beispiel.example.org"
    assert nachfragen[0].text_plain.startswith("Dear Ms Esempio,") and "WIK-2026-0100" in nachfragen[0].text_plain
    assert "11.11.2026" in nachfragen[0].text_plain

    assert len(umg.entwuerfe(EntwurfTyp.NACHFRAGE_SPEDITION)) == 1
    erinnerung = umg.entwuerfe(EntwurfTyp.ERINNERUNG_INTERN)
    assert len(erinnerung) == 1 and erinnerung[0].an == ["logistik@example.org"] and bericht.entwuerfe[-1] is erinnerung[0]
    assert "1 Fixtermin braucht Aufmerksamkeit" in erinnerung[0].text_plain and "2 Entwurf/Entwürfe" in erinnerung[0].text_plain
    assert [e.typ for e in bericht.entwuerfe] == [EntwurfTyp.NACHFRAGE_LIEFERANT, EntwurfTyp.NACHFRAGE_SPEDITION, EntwurfTyp.ERINNERUNG_INTERN]
    # Dry Run: Excel unverändert, keine Merkdatei, keine Dossier-Dateien
    assert umg.excel.schreibprotokoll == [] and not ue_settings.ueberwachung.merkdatei.exists()
    assert lauf.protokoll == [] and lauf.hinweise == []


def test_szenario_a_scharf_schreibt_merkdatei_und_bericht(ue_settings: Settings, liste: Kontaktliste, tmp_path: Path):
    umg = _umgebung(ue_settings, liste, tmp_path, _tage(_eintrag(date(2026, 11, 11))))
    bericht = umg.lauf().ausfuehren(dry_run=False)
    merk = json.loads(ue_settings.ueberwachung.merkdatei.read_text(encoding="utf-8"))
    assert merk == {f"lieferant|{BESTELLNUMMER}": "2026-10-26", f"spedition|{BESTELLNUMMER}": "2026-10-26"}
    assert umg.excel.schreibprotokoll == []  # keine Bestätigungsmails -> nichts in der Excel geändert
    assert len(bericht.entwuerfe) == 3


# ---------------------------------------------------------------- Szenario B


def test_szenario_b_speditionsbestaetigung_setzt_haekchen_referenz_und_dossier(ue_settings: Settings, liste: Kontaktliste, tmp_path: Path):
    umg = _umgebung(ue_settings, liste, tmp_path, _tage(_eintrag(date(2026, 11, 9), bl=True)), [_mail("spedition_bestaetigung")])
    dossier = _dossier_anlegen(umg)
    lauf = umg.lauf()
    bericht = lauf.ausfuehren(dry_run=False)

    assert bericht.fehler == [] and len(bericht.punkte) == 1
    punkt = bericht.punkte[0]
    ft = punkt.fixtermin
    assert ft.spedition_bestaetigt and ft.lieferant_bestaetigt and ft.tracking_referenz == "DEMB00000"
    assert ft.bemerkung and "Ref. DEMB00000" in ft.bemerkung and f"Best.-Nr {BESTELLNUMMER}" in ft.bemerkung
    # In der Excel: Häkchen in Spalte I und Bemerkung in Spalte K der Zeile
    zellen = {z: w for zugriff in umg.excel.schreibprotokoll for z, w, _ in zugriff.zellen}
    assert zellen[f"I{ft.zeile}"] is True and "Ref. DEMB00000" in str(zellen[f"K{ft.zeile}"])
    assert umg.fixtermin(ft.zeile).spedition_bestaetigt  # type: ignore[arg-type]
    assert any("Häkchen 'Spedition bestätigt' gesetzt" in a for a in punkt.aktionen)
    assert any("Referenz DEMB00000 in die Bemerkung eingetragen" in a for a in punkt.aktionen)
    # Tracking über die Mail-Quelle: bestätigt, Referenz, geplante Zustellung laut Transporeon
    assert punkt.sendung is not None and punkt.sendung.status == SendungStatus.BESTAETIGT and punkt.sendung.quelle == "mail"
    assert punkt.sendung.referenz == "DEMB00000" and punkt.sendung.geplante_zustellung == date(2026, 11, 9)
    # Anlieferung laut Transporeon = Fixtermin -> keine Terminänderung
    assert bericht.terminaenderungen == []
    # alles bestätigt -> grün, keine Nachfragen, keine Erinnerung
    assert punkt.ampel == "gruen" and bericht.entwuerfe == [] and umg.mail.entwuerfe == []
    # Mail im Dossier
    emls = _eml_dateien(umg, dossier)
    assert len(emls) == 1 and emls[0].name == "20261020_112000_spedition_bestaetigung.eml"
    nachricht = email.message_from_bytes(emls[0].read_bytes(), policy=email.policy.default)
    assert nachricht["Message-ID"] == "<spedition-bestaetigung@fixture.example.org>" and "DEMB00000" in nachricht["Subject"]
    index = Dossier.model_validate_json((umg.dossier_ordner / dossier.name / INDEX_DATEI).read_text(encoding="utf-8"))
    assert index.checkliste.get("spedition_bestaetigt") is True and any(d.typ == "mail" for d in index.dateien)
    assert (umg.dossier_ordner / dossier.name / "00_Deckblatt.pdf").is_file()
    assert any("Dossier" in p and "abgelegt" in p for p in lauf.protokoll)

    # Zweiter Lauf: idempotent (keine zweite .eml, keine weiteren Excel-Schreibzugriffe, kein Fehler)
    schreibzugriffe = len(umg.excel.schreibprotokoll)
    bericht2 = umg.lauf().ausfuehren(dry_run=False)
    assert bericht2.fehler == [] and len(_eml_dateien(umg, dossier)) == 1
    assert len(umg.excel.schreibprotokoll) == schreibzugriffe
    assert any("war bereits gesetzt" in a for a in bericht2.punkte[0].aktionen)
    assert any("liegt bereits im Dossier" in a for a in bericht2.punkte[0].aktionen)


def test_szenario_b_dry_run_schlaegt_nur_vor(ue_settings: Settings, liste: Kontaktliste, tmp_path: Path):
    umg = _umgebung(ue_settings, liste, tmp_path, _tage(_eintrag(date(2026, 11, 9))), [_mail("spedition_bestaetigung")])
    dossier = _dossier_anlegen(umg)
    bericht = umg.lauf().ausfuehren(dry_run=True)
    punkt = bericht.punkte[0]
    assert not punkt.fixtermin.spedition_bestaetigt and umg.excel.schreibprotokoll == []
    assert any(a.startswith("Dry Run: Häkchen 'Spedition bestätigt' würde gesetzt") for a in punkt.aktionen)
    assert any(a.startswith("Dry Run: Referenz DEMB00000 würde") for a in punkt.aktionen)
    assert any(a.startswith("Dry Run: Mail würde im Dossier") for a in punkt.aktionen)
    assert _eml_dateien(umg, dossier) == []
    # Tracking kennt die Bestätigung trotzdem, Lieferant fehlt -> gelb mit Nachfrage Lieferant
    assert punkt.sendung is not None and punkt.sendung.status == SendungStatus.BESTAETIGT
    assert punkt.ampel == "gelb" and len(umg.entwuerfe(EntwurfTyp.NACHFRAGE_LIEFERANT)) == 1


def test_szenario_b_ohne_automatik_nur_vorschlag(ue_settings: Settings, liste: Kontaktliste, tmp_path: Path):
    ue_settings.ueberwachung.bestaetigungen_automatisch_setzen = False
    umg = _umgebung(ue_settings, liste, tmp_path, _tage(_eintrag(date(2026, 11, 9))), [_mail("spedition_bestaetigung")])
    lauf = umg.lauf()
    bericht = lauf.ausfuehren(dry_run=False)
    assert umg.excel.schreibprotokoll == [] and not bericht.punkte[0].fixtermin.spedition_bestaetigt
    assert any(a.startswith("Vorschlag: Häkchen 'Spedition bestätigt' setzen") for a in bericht.punkte[0].aktionen)
    assert any(h.startswith("Vorschlag für Weingut Beispiel / Beispielmarkt am 09.11.2026: Häkchen 'Spedition bestätigt'") for h in lauf.hinweise)


def test_lieferantenbestaetigung_setzt_haekchen_und_verladedatum(ue_settings: Settings, liste: Kontaktliste, tmp_path: Path):
    umg = _umgebung(ue_settings, liste, tmp_path, _tage(_eintrag(date(2026, 11, 9), bs=True)), [_mail("lieferant_bestaetigung_en")])
    bericht = umg.lauf().ausfuehren(dry_run=False)
    ft = bericht.punkte[0].fixtermin
    assert ft.lieferant_bestaetigt and ft.verladedatum == date(2026, 11, 5) and bericht.fehler == []
    zellen = {z: w for zugriff in umg.excel.schreibprotokoll for z, w, _ in zugriff.zellen}
    assert zellen[f"H{ft.zeile}"] is True and zellen[f"G{ft.zeile}"] == date(2026, 11, 5)
    assert bericht.punkte[0].ampel == "gruen" and "Verladung am 05.11.2026" in bericht.punkte[0].grund
    assert umg.mail.entwuerfe == []


# ---------------------------------------------------------------- Szenario C


def test_szenario_c_terminaenderung_nur_vorschlag_excel_unveraendert(ue_settings: Settings, liste: Kontaktliste, tmp_path: Path):
    assert ue_settings.ueberwachung.termine_automatisch_anpassen is False
    umg = _umgebung(ue_settings, liste, tmp_path, _tage(_eintrag(date(2026, 11, 9), bl=True, bs=True, bemerkung="Best.-Nr 4712, Hillebrand")), [_mail("terminaenderung_lieferant")])
    lauf = umg.lauf()
    bericht = lauf.ausfuehren(dry_run=False)
    assert bericht.fehler == [] and umg.excel.schreibprotokoll == []
    assert len(bericht.terminaenderungen) == 1
    t = bericht.terminaenderungen[0]
    assert t.neues_datum == date(2026, 11, 3) and not t.uebernommen and t.fixtermin.liefertag == date(2026, 11, 9)
    assert t.quelle is not None and t.quelle.kundenreferenz == "4712" and "neuer Liefertermin" in t.grund
    assert bericht.punkte[0].fixtermin.liefertag == date(2026, 11, 9)
    assert any(a.startswith("Vorschlag: Terminänderung 09.11.2026 -> 03.11.2026") for a in bericht.punkte[0].aktionen)
    # Die Erinnerung nennt den Vorschlag
    erinnerung = umg.entwuerfe(EntwurfTyp.ERINNERUNG_INTERN)
    assert len(erinnerung) == 1 and "1 Terminänderung(en) warten auf Freigabe" in erinnerung[0].text_plain


def test_szenario_c_automatisch_verschiebt_den_eintrag(ue_settings: Settings, liste: Kontaktliste, tmp_path: Path):
    ue_settings.ueberwachung.termine_automatisch_anpassen = True
    umg = _umgebung(ue_settings, liste, tmp_path, _tage(_eintrag(date(2026, 11, 9), bl=True, bs=True, bemerkung="Best.-Nr 4712, Hillebrand")), [_mail("terminaenderung_lieferant")])
    bericht = umg.lauf().ausfuehren(dry_run=False)
    assert bericht.fehler == [] and len(bericht.terminaenderungen) == 1 and bericht.terminaenderungen[0].uebernommen
    ft = bericht.punkte[0].fixtermin
    assert ft.liefertag == date(2026, 11, 3) and ft.bemerkung and ft.bemerkung.startswith("verschoben von 09.11.2026:")
    assert umg.register.eintraege(date(2026, 11, 3), date(2026, 11, 3))[0].zeile == ft.zeile
    assert umg.register.eintraege(date(2026, 11, 9), date(2026, 11, 9)) == []
    assert any("verschoben" in a for a in bericht.punkte[0].aktionen)
    # Dry Run verschiebt nicht
    umg2 = _umgebung(ue_settings, liste, tmp_path, _tage(_eintrag(date(2026, 11, 9), bemerkung="Best.-Nr 4712, Hillebrand")), [_mail("terminaenderung_lieferant")])
    b2 = umg2.lauf().ausfuehren(dry_run=True)
    assert not b2.terminaenderungen[0].uebernommen and umg2.excel.schreibprotokoll == []
    assert any(a.startswith("Dry Run: Terminänderung") for a in b2.punkte[0].aktionen)


# ---------------------------------------------------------------- Szenario D


def test_szenario_d_drossel_verhindert_zweite_nachfrage_am_folgetag(ue_settings: Settings, liste: Kontaktliste, tmp_path: Path):
    umg = _umgebung(ue_settings, liste, tmp_path, _tage(_eintrag(date(2026, 11, 11))))
    erster = umg.lauf(heute=HEUTE).ausfuehren(dry_run=False)
    assert len(umg.entwuerfe(EntwurfTyp.NACHFRAGE_LIEFERANT)) == 1 and len(umg.entwuerfe(EntwurfTyp.NACHFRAGE_SPEDITION)) == 1
    assert [e.typ for e in erster.entwuerfe] == [EntwurfTyp.NACHFRAGE_LIEFERANT, EntwurfTyp.NACHFRAGE_SPEDITION, EntwurfTyp.ERINNERUNG_INTERN]

    zweiter = umg.lauf(heute=HEUTE + timedelta(days=1)).ausfuehren(dry_run=False)  # Dienstag: 1 Werktag
    assert len(umg.entwuerfe(EntwurfTyp.NACHFRAGE_LIEFERANT)) == 1  # keine zweite Nachfrage
    assert len(umg.entwuerfe(EntwurfTyp.NACHFRAGE_SPEDITION)) == 1
    assert [e.typ for e in zweiter.entwuerfe] == [EntwurfTyp.ERINNERUNG_INTERN]
    assert zweiter.punkte[0].ampel == "gelb"
    assert any(a == "Nachfrage Lieferant gedrosselt (zuletzt 26.10.2026, erneut nach 2 Werktagen)" for a in zweiter.punkte[0].aktionen)
    assert any(a.startswith("Nachfrage Spedition gedrosselt") for a in zweiter.punkte[0].aktionen)

    dritter = umg.lauf(heute=HEUTE + timedelta(days=2)).ausfuehren(dry_run=False)  # Mittwoch: 2 Werktage
    assert len(umg.entwuerfe(EntwurfTyp.NACHFRAGE_LIEFERANT)) == 2 and len(dritter.entwuerfe) == 3
    merk = json.loads(ue_settings.ueberwachung.merkdatei.read_text(encoding="utf-8"))
    assert merk[f"lieferant|{BESTELLNUMMER}"] == "2026-10-28"


def test_drossel_im_dry_run_nicht_gespeichert_aber_beachtet(ue_settings: Settings, liste: Kontaktliste, tmp_path: Path):
    umg = _umgebung(ue_settings, liste, tmp_path, _tage(_eintrag(date(2026, 11, 11))))
    umg.lauf().ausfuehren(dry_run=True)
    assert not ue_settings.ueberwachung.merkdatei.exists()
    drossel = Drossel(ue_settings.ueberwachung.merkdatei)
    drossel.merke("nachfrage_lieferant", umg.register.eintraege()[0], HEUTE)
    drossel.speichere()
    bericht = umg.lauf(heute=HEUTE + timedelta(days=1)).ausfuehren(dry_run=True)
    assert [e.typ for e in bericht.entwuerfe] == [EntwurfTyp.NACHFRAGE_SPEDITION, EntwurfTyp.ERINNERUNG_INTERN]


# ---------------------------------------------------------------- Szenario E


def test_szenario_e_tracking_exception_landet_in_fehler_lauf_geht_weiter(ue_settings: Settings, liste: Kontaktliste, tmp_path: Path):
    umg = _umgebung(ue_settings, liste, tmp_path, _tage(_eintrag(date(2026, 11, 11)), _eintrag(date(2026, 11, 12), bl=True, bs=True, kunde="Weinhandlung Beispiel", bemerkung="Best.-Nr 4712, Hillebrand | Ref. DEMB00001")))
    kette = KaputteKette()
    lauf = umg.lauf(kette=kette)
    bericht = lauf.ausfuehren(dry_run=True)
    assert kette.aufrufe == 2
    assert len(bericht.fehler) == 2
    assert bericht.fehler[0].startswith("Tracking Weingut Beispiel / Beispielmarkt am 11.11.2026 (WIK-2026-0100): RuntimeError: Portal nicht erreichbar")
    assert bericht.fehler[1].startswith("Tracking Weingut Beispiel / Weinhandlung Beispiel am 12.11.2026 (DEMB00001): RuntimeError")
    assert len(bericht.punkte) == 2 and bericht.punkte[0].ampel == "gelb" and bericht.punkte[1].ampel == "gruen"
    assert all(p.sendung is not None and p.sendung.quelle == "keine" and "Tracking fehlgeschlagen" in p.sendung.hinweise[-1] for p in bericht.punkte)
    assert len(umg.entwuerfe(EntwurfTyp.NACHFRAGE_LIEFERANT)) == 1  # Nachfragen laufen trotzdem
    erinnerung = umg.entwuerfe(EntwurfTyp.ERINNERUNG_INTERN)
    assert len(erinnerung) == 1 and "Fehler im Lauf: Tracking" in erinnerung[0].text_plain


def test_fehler_in_fabrik_und_register_landen_im_bericht(ue_settings: Settings, liste: Kontaktliste, tmp_path: Path):
    umg = _umgebung(ue_settings, liste, tmp_path, {})
    umg.excel.mappen["standard"] = {BLATT: [["Nur ein Titel"], [], [], ["Spalte A", "Spalte B"]]}

    def kaputte_fabrik(ereignisse: list) -> TrackingKette:
        raise ValueError("keine Quellen")

    bericht = umg.lauf(kette=kaputte_fabrik).ausfuehren(dry_run=True)
    assert bericht.punkte == [] and any(f.startswith("Fixterminüberwachung lesen: RegisterFehler") for f in bericht.fehler)
    assert any(f.startswith("Tracking-Kette aufbauen: ValueError: keine Quellen") for f in bericht.fehler)
    assert [e.typ for e in bericht.entwuerfe] == [EntwurfTyp.ERINNERUNG_INTERN]  # Fehler werden gemeldet


def test_posteingang_fehler_und_kaputte_mail_stoppen_nicht(ue_settings: Settings, liste: Kontaktliste, tmp_path: Path):
    umg = _umgebung(ue_settings, liste, tmp_path, _tage(_eintrag(date(2026, 11, 11), bl=True, bs=True)), [_mail("spedition_bestaetigung")])

    def kaputt(**kw: Any) -> list:
        raise OSError("Postfach offline")

    umg.mail.suche_mails = kaputt  # type: ignore[method-assign]
    bericht = umg.lauf().ausfuehren(dry_run=True)
    assert bericht.punkte and bericht.punkte[0].ampel == "gruen" and bericht.fehler == []  # sammle_ereignisse fängt selbst ab


# ---------------------------------------------------------------- Weitere Ereignisse


def test_kontaktaenderung_aus_signatur_als_vorschlag(ue_settings: Settings, liste: Kontaktliste, tmp_path: Path):
    umg = _umgebung(ue_settings, liste, tmp_path, _tage(_eintrag(date(2026, 11, 11), bl=True, bs=True)), [_mail("kontaktaenderung")])
    lauf = umg.lauf()
    bericht = lauf.ausfuehren(dry_run=True)
    assert bericht.fehler == []
    assert len(bericht.kontaktaenderungen) == 1
    a = bericht.kontaktaenderungen[0]
    assert a.feld == "telefon" and "0000099" in a.neu and not a.uebernommen and a.kontakt is not None and a.kontakt.name == "Marco Esempio"
    assert umg.excel.schreibprotokoll == []  # Kontaktliste wird nie automatisch geändert
    erinnerung = umg.entwuerfe(EntwurfTyp.ERINNERUNG_INTERN)
    assert len(erinnerung) == 1 and "1 Kontaktänderung(en) warten auf Freigabe" in erinnerung[0].text_plain


def test_nicht_zuordenbare_mail_wird_als_hinweis_gemeldet(ue_settings: Settings, liste: Kontaktliste, tmp_path: Path):
    umg = _umgebung(ue_settings, liste, tmp_path, _tage(_eintrag(date(2026, 11, 11), bl=True, bs=True, bemerkung="Best.-Nr 9999")), [_mail("spedition_bestaetigung")])
    lauf = umg.lauf()
    bericht = lauf.ausfuehren(dry_run=False)
    assert bericht.fehler == [] and umg.excel.schreibprotokoll == []
    assert len(lauf.hinweise) == 1 and lauf.hinweise[0].startswith("Nicht zugeordnet: spedition_bestaetigung 'Auftragsbestätigung; Your Ref: WIK-2026-0100; Our Ref: DEMB00000[jobDEM00000000]' von dispo.beispiel@hillebrandgori.com (20.10.2026)")
    assert bericht.punkte[0].fixtermin.spedition_bestaetigt is True  # unverändert aus der Excel


def test_mehrdeutige_zuordnung_setzt_nichts(ue_settings: Settings, liste: Kontaktliste, tmp_path: Path):
    eintraege = _tage(_eintrag(date(2026, 11, 9), bl=True), _eintrag(date(2026, 11, 9), bl=True, kunde="Beispielmarkt Süd"))
    umg = _umgebung(ue_settings, liste, tmp_path, eintraege, [_mail("spedition_bestaetigung")])
    lauf = umg.lauf()
    bericht = lauf.ausfuehren(dry_run=False)
    assert umg.excel.schreibprotokoll == [] and not any(p.fixtermin.spedition_bestaetigt for p in bericht.punkte)
    assert lauf.hinweise and lauf.hinweise[0].startswith("Mehrdeutig, nichts geändert:") and "Beispielmarkt Süd" in lauf.hinweise[0]


def test_abholung_laut_tracking_setzt_haekchen(ue_settings: Settings, liste: Kontaktliste, tmp_path: Path):
    umg = _umgebung(ue_settings, liste, tmp_path, _tage(_eintrag(date(2026, 11, 9), bl=True, bs=True, verladung=date(2026, 10, 23), bemerkung=f"Best.-Nr {BESTELLNUMMER}, Hillebrand, Ref. DEMB00000")))

    class Abgeholt:
        name = "api"

        def verfuegbar(self) -> bool:
            return True

        def aktualisiere(self, sendung: Sendung) -> Sendung:
            assert sendung.referenz == "DEMB00000" and sendung.kundenreferenz == BESTELLNUMMER and sendung.spedition == "Hillebrand"
            return sendung.model_copy(update={"status": SendungStatus.ABGEHOLT, "quelle": "api"})

    bericht = umg.lauf(kette=lambda ereignisse: TrackingKette([Abgeholt()])).ausfuehren(dry_run=False)
    ft = bericht.punkte[0].fixtermin
    assert ft.abgeholt and bericht.punkte[0].ampel == "gruen" and bericht.punkte[0].grund == "abgeholt"
    zellen = {z: w for zugriff in umg.excel.schreibprotokoll for z, w, _ in zugriff.zellen}
    assert zellen[f"J{ft.zeile}"] is True
    assert any(a.startswith("Häkchen 'Abgeholt' gesetzt (Tracking api: abgeholt, Referenz DEMB00000") for a in bericht.punkte[0].aktionen)


def test_sendung_nur_mit_referenz_oder_nummer(ue_settings: Settings, liste: Kontaktliste, tmp_path: Path):
    umg = _umgebung(ue_settings, liste, tmp_path, _tage(_eintrag(date(2026, 11, 11), bl=True, bs=True, bemerkung="")))
    kette = KaputteKette()
    bericht = umg.lauf(kette=kette).ausfuehren(dry_run=True)
    assert kette.aufrufe == 0 and bericht.punkte[0].sendung is None and bericht.fehler == []


# ---------------------------------------------------------------- Bausteine


def test_eml_aus_mail_deterministisch_mit_html_und_anhangshinweis():
    mail = Mail(
        kopf=MailKopf(id="AAMk-1", betreff="Test", absender="dispo@hillebrand.example.org", absender_name="Dispo", empfangen=datetime(2026, 10, 20, 11, 20).astimezone(), hat_anhaenge=True),
        text="Hallo",
        html="<p>Hallo</p>",
        an=["logistik@example.org"],
        anhaenge=[{"id": "a1", "name": "Bestätigung.pdf", "content_type": "application/pdf"}],  # type: ignore[list-item]
    )
    a, b = eml_aus_mail(mail), eml_aus_mail(mail)
    assert a == b
    nachricht = email.message_from_bytes(a, policy=email.policy.default)
    assert nachricht["Message-ID"] == "<AAMk-1@posteingang.auftragsautomat.invalid>" and nachricht["From"] == "Dispo <dispo@hillebrand.example.org>"
    assert nachricht["X-Auftragsautomat-Anhang"] == "Bestätigung.pdf" and nachricht["Date"].startswith("Tue, 20 Oct 2026 11:20:00")
    assert nachricht.get_body(preferencelist=("html",)).get_content().strip() == "<p>Hallo</p>"  # type: ignore[union-attr]
    assert nachricht.get_body(preferencelist=("plain",)).get_content().strip() == "Hallo"  # type: ignore[union-attr]


def test_lauf_mit_mail_backend_direkt_und_fabrik_funktion(ue_settings: Settings, liste: Kontaktliste, tmp_path: Path):
    umg = _umgebung(ue_settings, liste, tmp_path, _tage(_eintrag(date(2026, 11, 11))))
    aufrufe: list[str | None] = []

    def fabrik(firma_id: str | None) -> Entwurfsfabrik:
        aufrufe.append(firma_id)
        return umg.fabrik

    lauf = Ueberwachungslauf(ue_settings, umg.mail, umg.register, umg.zuordner, fabrik, None, None, heute=HEUTE, regelwerk=Regelwerk(mindest_alter_werktage=0))
    bericht = lauf.ausfuehren(dry_run=True)
    assert lauf.mail is umg.mail and bericht.fehler == [] and aufrufe == [None]
    assert bericht.punkte[0].sendung is not None and bericht.punkte[0].sendung.quelle == "keine"  # keine Kette
    assert len(bericht.entwuerfe) == 3 and lauf.bericht is bericht


def test_ueberwachen_aus_settings_schreibt_bericht(ue_settings: Settings, liste: Kontaktliste, tmp_path: Path):
    umg = _umgebung(ue_settings, liste, tmp_path, _tage(_eintrag(date(2026, 11, 11))))
    bericht, pfad = ueberwachen(ue_settings, dry_run=True, heute=HEUTE, backends=umg.backends, kontaktliste=liste, stammdaten={}, firma_id="wik_leh")
    assert pfad == ue_settings.ueberwachung.berichte_ordner / "ueberwachung_2026-10-26.md" and pfad.is_file()
    assert bericht.fehler == [] and bericht.punkte[0].ampel == "gelb" and len(bericht.entwuerfe) == 3
    assert lade_letzten_bericht(ue_settings.ueberwachung.berichte_ordner) == bericht
    text = pfad.read_text(encoding="utf-8")
    assert "| GELB | 11.11.2026 | Weingut Beispiel | Beispielmarkt |" in text and "nachfrage_lieferant" in text


def test_aus_settings_ohne_firmen_meldet_klar(ue_settings: Settings, liste: Kontaktliste, tmp_path: Path):
    umg = _umgebung(ue_settings, liste, tmp_path, {})
    ue_settings.firmen = {}
    with pytest.raises(ValueError, match="Keine Firma konfiguriert"):
        Ueberwachungslauf.aus_settings(ue_settings, backends=umg.backends, kontaktliste=liste, stammdaten={})


def test_register_fehler_bei_leerer_mappe_ist_kein_absturz(ue_settings: Settings, liste: Kontaktliste, tmp_path: Path):
    umg = _umgebung(ue_settings, liste, tmp_path, {})
    umg.excel.mappen.clear()
    with pytest.raises(RegisterFehler):
        umg.register.eintraege()
    bericht = umg.lauf().ausfuehren(dry_run=True)
    assert bericht.punkte == [] and len(bericht.fehler) == 1 and "Fixterminüberwachung lesen" in bericht.fehler[0]
