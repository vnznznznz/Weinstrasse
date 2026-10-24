"""Tests für kommunikation.anrede: Anredezeilen im Hausstil, Grußformeln, Regeln aus config/anrede.yaml."""

from __future__ import annotations

from pathlib import Path

import pytest

from auftragsautomat.kommunikation.anrede import (
    AnredeRegeln,
    anrede_regeln_fuer_zuordner,
    baue_anrede,
    grussformel,
    lade_anrede_regeln,
    sprache_normalisiert,
)
from auftragsautomat.models import Kontakt, Kontaktgruppe

REPO_CONFIG = Path(__file__).resolve().parents[1] / "config"


def _kontakt(name: str | None, *, email: str | None = None, anrede: str | None = None, gruppe: Kontaktgruppe = Kontaktgruppe.KUNDE) -> Kontakt:
    return Kontakt(gruppe=gruppe, firma="Beispiel GmbH", name=name, email=email, anrede=anrede)


@pytest.fixture()
def regeln() -> AnredeRegeln:
    return lade_anrede_regeln(REPO_CONFIG / "anrede.yaml")


def test_yaml_entspricht_standard(regeln: AnredeRegeln):
    standard = AnredeRegeln()
    assert regeln.anrede == standard.anrede
    assert regeln.grussformel == standard.grussformel
    assert regeln.du == [] and regeln.regeln == {}
    assert regeln.quelle == REPO_CONFIG / "anrede.yaml"


@pytest.mark.parametrize(
    ("name", "anrede", "sprache", "erwartet"),
    [
        ("Frau Beispiel", None, "de", "Hallo Frau Beispiel!"),
        ("Herr Beispiel", None, "de", "Hallo Herr Beispiel!"),
        ("Beispiel", "Frau", "de", "Hallo Frau Beispiel!"),
        ("Dr. Giulia Esempio", None, "en", "Dear Ms Esempio,"),
        ("Marco Esempio", None, "en", "Dear Mr Esempio,"),
        ("Giulia Esempio", None, "de", "Hallo Frau Esempio!"),
        ("Kim Beispiel", None, "de", "Hallo Kim Beispiel!"),  # Geschlecht unbekannt -> Name
        ("Kim Beispiel", None, "en", "Hello Kim Beispiel,"),
        ("Auftrags-Postfach", None, "de", "Guten Tag!"),  # Abteilung/Postfach -> neutral
        ("Buchhaltung", None, "en", "Hello,"),
        (None, None, "de", "Guten Tag!"),
        ("", None, "en", "Hello,"),
    ],
)
def test_baue_anrede(regeln: AnredeRegeln, name: str | None, anrede: str | None, sprache: str, erwartet: str):
    assert baue_anrede(_kontakt(name, anrede=anrede), sprache, regeln=regeln) == erwartet


def test_baue_anrede_ohne_kontakt_und_ohne_regeln():
    assert baue_anrede(None, "de") == "Guten Tag!" and baue_anrede(None, "en") == "Hello,"
    assert baue_anrede(_kontakt("Frau Beispiel"), "DE") == "Hallo Frau Beispiel!"
    assert baue_anrede(_kontakt("Frau Beispiel"), "english") == "Dear Ms Beispiel,"
    assert baue_anrede(_kontakt("Frau Beispiel"), "fr") == "Hallo Frau Beispiel!"  # unbekannte Sprache -> deutsch


def test_feste_regeln_und_du_liste():
    regeln = AnredeRegeln(
        du=["Kim@Example.org"],
        regeln={"einkauf@example.org": {"anrede": "Sehr geehrter Herr Beispiel", "sprache": "de"}},  # type: ignore[dict-item]
    )
    assert baue_anrede(_kontakt("Kim Beispiel", email="kim@example.org"), "de", regeln=regeln) == "Hallo Kim!"
    assert baue_anrede(_kontakt("Kim Beispiel", email="kim@example.org"), "en", regeln=regeln) == "Hello Kim,"
    # feste Regel: Satzzeichen wird ergänzt, Sprache der Regel gilt
    assert baue_anrede(_kontakt("Herr Beispiel", email="Einkauf@example.org"), "en", regeln=regeln) == "Sehr geehrter Herr Beispiel!"
    assert anrede_regeln_fuer_zuordner(regeln) == {"einkauf@example.org": {"anrede": "Sehr geehrter Herr Beispiel", "sprache": "de"}}
    assert anrede_regeln_fuer_zuordner(None) == {}


def test_platzhalter_bleiben_sicher():
    regeln = AnredeRegeln()
    regeln.anrede["de"].frau = "Hallo Frau {nachname} von {firma}!"
    assert baue_anrede(_kontakt("Frau Beispiel"), "de", regeln=regeln) == "Hallo Frau Beispiel von Beispiel GmbH!"
    regeln.anrede["de"].herr = "Hallo Herr {unbekannt} {nachname}!"
    assert baue_anrede(_kontakt("Herr Beispiel"), "de", regeln=regeln) == "Hallo Herr {unbekannt} Beispiel!"


def test_grussformel(regeln: AnredeRegeln):
    assert grussformel("de", regeln=regeln) == "Freundliche Grüße"
    assert grussformel("en", "wik_leh", regeln=regeln) == "Thank you and best regards"
    assert grussformel("en", "weinstrasse_agentur", regeln=regeln) == "Thank you and best regards"
    eigene = AnredeRegeln(firmen={"woe_leh": {"grussformel": {"de": "Herzliche Grüße"}}})  # type: ignore[dict-item]
    assert grussformel("de", "woe_leh", regeln=eigene) == "Herzliche Grüße"
    assert grussformel("en", "woe_leh", regeln=eigene) == "Thank you and best regards"
    assert grussformel("de") == "Freundliche Grüße"


def test_lade_regeln_fehlend_und_ungueltig(tmp_path: Path):
    assert lade_anrede_regeln(tmp_path / "fehlt.yaml").anrede["de"].frau == "Hallo Frau {nachname}!"
    assert lade_anrede_regeln(None).quelle is None
    kaputt = tmp_path / "anrede.yaml"
    kaputt.write_text("- liste\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Mapping"):
        lade_anrede_regeln(kaputt)
    kaputt.write_text("regeln: [a, b]\n", encoding="utf-8")
    with pytest.raises(ValueError, match="regeln"):
        lade_anrede_regeln(kaputt)
    kaputt.write_text("anrede:\n  de:\n    frau: nur\n", encoding="utf-8")
    with pytest.raises(ValueError, match="ungültig"):
        lade_anrede_regeln(kaputt)
    kaputt.write_text("regeln:\n  Du@Example.org:\n    sprache: en\n", encoding="utf-8")
    geladen = lade_anrede_regeln(kaputt)
    assert geladen.regel_fuer("du@example.org") is not None and geladen.regel_fuer("du@example.org").sprache == "en"  # type: ignore[union-attr]


def test_sprache_normalisiert():
    assert sprache_normalisiert("en") == "en" and sprache_normalisiert("EN-GB") == "en"
    assert sprache_normalisiert("de") == "de" and sprache_normalisiert(None) == "de" and sprache_normalisiert("it") == "de"
