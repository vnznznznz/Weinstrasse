"""Tests für ueberwachung/regeln.py: Ampel/Maßnahmen je Fixtermin, Regelwerk (YAML), Drossel (Merkdatei).

Stichtag ist fest: Montag, 26.10.2026 (NW; keine Feiertage im Zeitraum außer Allerheiligen am Sonntag).
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import pytest

from auftragsautomat.config import Settings, UeberwachungKonfig
from auftragsautomat.models import Fixtermin, Sendung, SendungStatus
from auftragsautomat.ueberwachung import regeln
from auftragsautomat.ueberwachung.regeln import (
    Bewertung,
    Drossel,
    Massnahme,
    Regelwerk,
    RegelwerkFehler,
    bewerte,
    hoechste_ampel,
    regelwerk_pfad,
)

REPO_CONFIG = Path(__file__).resolve().parents[1] / "config"
HEUTE = date(2026, 10, 26)  # Montag
KONFIG = UeberwachungKonfig()  # 10 / 5 / 2 Werktage, NW


def _ft(**felder) -> Fixtermin:
    basis = {
        "lieferant_kurz": "Weingut Beispiel",
        "kunde_kurz": "Beispielmarkt",
        "liefertag": date(2026, 11, 11),  # 12 Werktage nach HEUTE
        "auftragsdatum": date(2026, 10, 20),
        "bestellnummer": "WIK-2026-0100",
        "spedition": "Hillebrand",
        "zeile": 12,
    }
    basis.update(felder)
    return Fixtermin(**basis)


def _sendung(status: SendungStatus, quelle: str = "mail", **felder) -> Sendung:
    return Sendung(spedition="Hillebrand", referenz="DEMB00000", status=status, quelle=quelle, **felder)  # type: ignore[arg-type]


# ---------------------------------------------------------------- bewerte


def test_szenario_a_zwoelf_werktage_ohne_bestaetigungen_gelb_noch_ohne_nachfragen():
    """Regression (Review Befund 13): 12 Werktage vor dem Fixtermin sind beide Schwellen (10/5) noch nicht erreicht:
    gelb mit Frist, aber noch kein Entwurf."""
    ampel, grund, massnahmen = bewerte(_ft(), None, HEUTE, konfig=KONFIG)
    assert ampel == "gelb" and massnahmen == []
    assert "Frist 28.10.2026, 10 Werktage" in grund and "Frist 04.11.2026, 5 Werktage" in grund


@pytest.mark.parametrize(
    ("liefertag", "werktage", "erwartet"),
    [
        (date(2026, 11, 10), 11, []),  # Schwelle Lieferant + 1
        (date(2026, 11, 9), 10, ["nachfrage_lieferant"]),  # Schwelle Lieferant
        (date(2026, 11, 6), 9, ["nachfrage_lieferant"]),  # Schwelle Lieferant - 1
        (date(2026, 11, 3), 6, ["nachfrage_lieferant"]),  # Schwelle Spedition + 1
        (date(2026, 11, 2), 5, ["nachfrage_lieferant", "nachfrage_spedition"]),  # Schwelle Spedition
        (date(2026, 10, 30), 4, ["nachfrage_lieferant", "nachfrage_spedition"]),  # Schwelle Spedition - 1
    ],
)
def test_nachfragen_erst_ab_der_schwelle(liefertag: date, werktage: int, erwartet: list[str]):
    """Regression (Review Befund 13): nachfrage_lieferant_werktage / nachfrage_spedition_werktage wirken auf die
    Maßnahmen (AUSBAUSTUFE_2 2.6: ab 10 bzw. 5 Werktagen vor dem Fixtermin), nicht nur auf den Fristtext."""
    b = bewerte(_ft(liefertag=liefertag), None, HEUTE, konfig=KONFIG)
    assert b.werktage_bis == werktage and b.typen == erwartet
    if not erwartet:
        assert b.ampel == "gelb"
    # andere Schwellen ändern die Maßnahmen, nicht nur den Text
    spaet = UeberwachungKonfig(nachfrage_lieferant_werktage=3, nachfrage_spedition_werktage=2)
    assert bewerte(_ft(liefertag=date(2026, 11, 2)), None, HEUTE, konfig=spaet).typen == []


def test_bewertung_ist_objekt_mit_werktagen_und_befunden():
    b = bewerte(_ft(liefertag=date(2026, 11, 2)), None, HEUTE, konfig=KONFIG)
    assert isinstance(b, Bewertung) and b.werktage_bis == 5 and b.hat("nachfrage_lieferant")
    assert b.typen == ["nachfrage_lieferant", "nachfrage_spedition"] and len(b.befunde) == 2
    assert all(isinstance(m, Massnahme) and m.begruendung for m in b.massnahmen)
    assert tuple(b) == (b.ampel, b.grund, b.massnahmen)


def test_frist_ueberschritten_bleibt_gelb_ueber_fuenf_werktagen():
    ampel, grund, massnahmen = bewerte(_ft(liefertag=date(2026, 11, 3)), None, HEUTE, konfig=KONFIG)  # 6 Werktage
    assert ampel == "gelb" and "überfällig" in grund and "Frist 20.10.2026" in grund
    assert "nachfrage_lieferant" in [m.typ for m in massnahmen]


def test_weniger_als_fuenf_werktage_ohne_bestaetigung_rot():
    ampel, grund, massnahmen = bewerte(_ft(liefertag=date(2026, 10, 30)), None, HEUTE, konfig=KONFIG)  # 4 Werktage
    assert ampel == "rot" and "überfällig" in grund
    assert [m.typ for m in massnahmen] == ["nachfrage_lieferant", "nachfrage_spedition"]


def test_nur_spedition_offen_wenn_lieferant_bestaetigt():
    ampel, _, massnahmen = bewerte(_ft(lieferant_bestaetigt=True, liefertag=date(2026, 11, 2)), None, HEUTE, konfig=KONFIG)
    assert ampel == "gelb" and [m.typ for m in massnahmen] == ["nachfrage_spedition"]


def test_auftrag_von_heute_noch_keine_nachfrage():
    faellig = date(2026, 11, 2)  # beide Schwellen erreicht
    ampel, grund, massnahmen = bewerte(_ft(auftragsdatum=HEUTE, liefertag=faellig), None, HEUTE, konfig=KONFIG)
    assert ampel == "gelb" and massnahmen == []
    assert "Nachfrage frühestens nach 1 Werktag" in grund
    # ohne Auftragsdatum: sofort
    assert bewerte(_ft(auftragsdatum=None, liefertag=faellig), None, HEUTE, konfig=KONFIG).typen == ["nachfrage_lieferant", "nachfrage_spedition"]
    # mindest_alter 0: sofort
    assert bewerte(_ft(auftragsdatum=HEUTE, liefertag=faellig), None, HEUTE, konfig=KONFIG, regelwerk=Regelwerk(mindest_alter_werktage=0)).hat("nachfrage_lieferant")


def test_selbstabholung_erwartet_keine_speditionsbestaetigung():
    b = bewerte(_ft(spedition="Selbstabholung", lieferant_bestaetigt=True), None, HEUTE, konfig=KONFIG)
    assert b.ampel == "gruen" and b.massnahmen == [] and "haben bestätigt" in b.grund


def test_alles_bestaetigt_gruen_mit_verladedatum():
    ft = _ft(lieferant_bestaetigt=True, spedition_bestaetigt=True, verladedatum=date(2026, 11, 6))
    ampel, grund, massnahmen = bewerte(ft, None, HEUTE, konfig=KONFIG)
    assert ampel == "gruen" and massnahmen == [] and "Verladung am 06.11.2026" in grund


def test_verladedatum_erreicht_nicht_abgeholt_statusanfrage_und_tracking():
    ft = _ft(lieferant_bestaetigt=True, spedition_bestaetigt=True, verladedatum=date(2026, 10, 26), liefertag=date(2026, 11, 3))
    ampel, grund, massnahmen = bewerte(ft, None, HEUTE, konfig=KONFIG)
    assert ampel == "gelb" and "Verladedatum 26.10.2026 erreicht" in grund
    assert [m.typ for m in massnahmen] == ["status_spedition", "tracking"]
    # Spedition unbestätigt dazu: die Statusanfrage deckt die Nachfrage mit ab (keine zweite Mail)
    b = bewerte(ft.model_copy(update={"spedition_bestaetigt": False}), None, HEUTE, konfig=KONFIG)
    assert b.typen == ["status_spedition", "tracking"] and "Bestätigung der Spedition" in b.grund


def test_fixtermin_morgen_erinnerung():
    ft = _ft(liefertag=date(2026, 10, 27), lieferant_bestaetigt=True, spedition_bestaetigt=True)
    b = bewerte(ft, None, HEUTE, konfig=KONFIG)
    assert b.ampel == "gelb" and b.typen == ["erinnerung"] and "nächsten Werktag (27.10.2026)" in b.grund
    heute = bewerte(ft.model_copy(update={"liefertag": HEUTE, "abgeholt": True}), None, HEUTE, konfig=KONFIG)
    assert heute.ampel == "gruen" and heute.typen == ["erinnerung"] and "heute" in heute.grund
    # Abgeholt und bestätigt, ohne Erinnerungsfenster: grün "abgeholt"
    assert bewerte(ft.model_copy(update={"abgeholt": True, "liefertag": date(2026, 11, 11)}), None, HEUTE, konfig=KONFIG) == Bewertung("gruen", "abgeholt", [], 12, [("gruen", "abgeholt")])


def test_ueberschritten_nicht_abgeholt_rot():
    ft = _ft(liefertag=date(2026, 10, 22), lieferant_bestaetigt=True, spedition_bestaetigt=True)
    ampel, grund, massnahmen = bewerte(ft, None, HEUTE, konfig=KONFIG)
    assert ampel == "rot" and "überschritten, nicht abgeholt" in grund
    assert [m.typ for m in massnahmen] == ["status_spedition", "tracking"]


def test_ueberschritten_abgeholt_ohne_tracking_grau():
    ft = _ft(liefertag=date(2026, 10, 22), lieferant_bestaetigt=True, spedition_bestaetigt=True, abgeholt=True)
    assert bewerte(ft, None, HEUTE, konfig=KONFIG).ampel == "grau"
    # mit Tracking, das noch "unterwegs" meldet: rot
    b = bewerte(ft, _sendung(SendungStatus.UNTERWEGS), HEUTE, konfig=KONFIG)
    assert b.ampel == "rot" and "noch nicht zugestellt (unterwegs)" in b.grund and b.typen == ["status_spedition", "tracking"]


def test_tracking_zugestellt_grau_und_problem_rot():
    ft = _ft(lieferant_bestaetigt=True, spedition_bestaetigt=True, abgeholt=True)
    assert bewerte(ft, _sendung(SendungStatus.ZUGESTELLT, "api"), HEUTE, konfig=KONFIG) == Bewertung("grau", "zugestellt laut Tracking (api)", [], 12, [("grau", "zugestellt laut Tracking (api)")])
    b = bewerte(ft, _sendung(SendungStatus.VERSPAETET), HEUTE, konfig=KONFIG)
    assert b.ampel == "rot" and b.typen == ["status_spedition", "tracking"] and "verspaetet" in b.grund


def test_tracking_abgeholt_ohne_haekchen_gilt_als_abgeholt():
    ft = _ft(lieferant_bestaetigt=True, spedition_bestaetigt=True, verladedatum=date(2026, 10, 23))
    b = bewerte(ft, _sendung(SendungStatus.ABGEHOLT), HEUTE, konfig=KONFIG)
    assert b.ampel == "gruen" and b.massnahmen == [] and "Häkchen 'Abgeholt' fehlt" in b.grund
    # Sendung ohne echte Quelle zählt nicht
    b2 = bewerte(ft, Sendung(spedition="Hillebrand", status=SendungStatus.ABGEHOLT, quelle="keine"), HEUTE, konfig=KONFIG)
    assert b2.typen == ["status_spedition", "tracking"]


def test_bewerte_mit_settings_und_datetime(settings: Settings):
    settings.ueberwachung.nachfrage_lieferant_werktage = 3
    b = bewerte(_ft(), None, datetime(2026, 10, 26, 9, 30), konfig=settings)
    assert b.werktage_bis == 12 and "Frist 06.11.2026, 3 Werktage" in b.grund


def test_texte_aus_regelwerk_werden_verwendet():
    rw = Regelwerk(texte={"lieferant_offen": "LIEFERANT FEHLT bis {frist}"})  # type: ignore[arg-type]
    b = bewerte(_ft(spedition_bestaetigt=True), None, HEUTE, konfig=KONFIG, regelwerk=rw)
    assert b.grund == "LIEFERANT FEHLT bis 28.10.2026"


def test_hoechste_ampel():
    assert hoechste_ampel(["grau", "gelb", "gruen"]) == "gelb"
    assert hoechste_ampel(["rot", "gelb"]) == "rot" and hoechste_ampel([]) == "gruen"


# ---------------------------------------------------------------- Regelwerk


def test_regelwerk_fehlende_datei_standardwerte(tmp_path: Path):
    rw = Regelwerk.lade(tmp_path / "gibt_es_nicht.yaml")
    assert rw.quelle is None and rw.mindest_alter_werktage == 1 and rw.rot_unter_werktagen == 5
    assert Regelwerk.lade(None).texte.abgeholt == "abgeholt"


def test_regelwerk_aus_repo_konfiguration():
    rw = Regelwerk.lade(REPO_CONFIG / regeln.STANDARD_DATEINAME)
    assert rw.quelle and rw.quelle.endswith("ueberwachung.yaml")
    assert rw.erinnerung_werktage_vor_fixtermin == 1 and rw.zuordnung_konfidenz_min == 0.5 and rw.text_zuordnung is True
    assert "{frist}" in rw.texte.lieferant_offen


def test_regelwerk_aus_settings_pfad_und_ungueltige_datei(settings: Settings, tmp_path: Path):
    settings.kommunikation.anrede_datei = tmp_path / "config" / "anrede.yaml"
    assert regelwerk_pfad(settings) == tmp_path / "config" / "ueberwachung.yaml"
    assert Regelwerk.aus_settings(settings).quelle is None  # Datei fehlt -> Standard
    pfad = tmp_path / "config" / "ueberwachung.yaml"
    pfad.parent.mkdir(parents=True)
    pfad.write_text("mindest_alter_werktage: -3\n", encoding="utf-8")
    with pytest.raises(RegelwerkFehler):
        Regelwerk.aus_settings(settings)
    pfad.write_text("- liste\n", encoding="utf-8")
    with pytest.raises(RegelwerkFehler):
        Regelwerk.lade(pfad)
    pfad.write_text("rot_unter_werktagen: 3\nunbekannt: 1\ntexte:\n  abgeholt: 'fertig'\n", encoding="utf-8")
    rw = Regelwerk.lade(pfad)
    assert rw.rot_unter_werktagen == 3 and rw.texte.abgeholt == "fertig" and rw.texte.zugestellt.startswith("zugestellt")


# ---------------------------------------------------------------- Drossel


def test_drossel_schluessel_und_werktage(tmp_path: Path):
    d = Drossel(tmp_path / "nachfragen.json", erneut_nach_werktagen=2)
    ft = _ft()
    assert Drossel.schluessel("nachfrage_lieferant", ft) == "lieferant|WIK-2026-0100"
    assert Drossel.schluessel("status_spedition", ft) == Drossel.schluessel("nachfrage_spedition", ft) == "spedition|WIK-2026-0100"
    ohne = _ft(bestellnummer=None, ab_nummer=None, kundenbestellnummer=None)
    assert Drossel.vorgang(ohne) == "weingut beispiel|beispielmarkt|2026-11-11"
    assert d.erlaubt("nachfrage_lieferant", ft, HEUTE) and d.erlaubt("erinnerung", ft, HEUTE)
    d.merke("nachfrage_lieferant", ft, HEUTE)
    d.merke("erinnerung", ft, HEUTE)  # nicht gedrosselt -> kein Eintrag
    assert len(d) == 1 and d.letzte("nachfrage_lieferant", ft) == HEUTE
    assert not d.erlaubt("nachfrage_lieferant", ft, date(2026, 10, 27))  # Folgetag: 1 Werktag
    assert d.erlaubt("nachfrage_lieferant", ft, date(2026, 10, 28))  # 2 Werktage
    assert d.erlaubt("nachfrage_spedition", ft, date(2026, 10, 27))  # andere Gruppe
    assert d.vergiss("nachfrage_lieferant", ft) and not d.vergiss("nachfrage_lieferant", ft) and len(d) == 0


def test_drossel_speichern_und_laden_atomar(tmp_path: Path):
    pfad = tmp_path / "logs" / "nachfragen.json"
    d = Drossel(pfad)
    d.merke("nachfrage_spedition", _ft(), HEUTE)
    assert d.speichere() == pfad and json.loads(pfad.read_text(encoding="utf-8")) == {"spedition|WIK-2026-0100": "2026-10-26"}
    assert not [p for p in pfad.parent.iterdir() if p.suffix == ".tmp"]
    neu = Drossel(pfad)
    assert neu.eintraege == {"spedition|WIK-2026-0100": HEUTE} and not neu.erlaubt("status_spedition", _ft(), date(2026, 10, 27))
    pfad.write_text("{kaputt", encoding="utf-8")
    assert len(Drossel(pfad)) == 0
    pfad.write_text(json.dumps({"x|1": "kein datum", "y|2": "2026-10-20T08:00:00"}), encoding="utf-8")
    assert Drossel(pfad).eintraege == {"y|2": date(2026, 10, 20)}
    assert Drossel(None).speichere() is None


def test_drossel_aus_settings(settings: Settings, tmp_path: Path):
    settings.ueberwachung.merkdatei = tmp_path / "merk.json"
    settings.ueberwachung.erneut_nach_werktagen = 3
    d = Drossel.aus_settings(settings)
    assert d.pfad == tmp_path / "merk.json" and d.erneut_nach_werktagen == 3 and d.bundesland == "NW"
