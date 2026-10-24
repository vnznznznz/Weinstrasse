"""Tests für kontakte.zuordnung: Lieferant/Kunde -> Kontakte, Spedition je Kunde, Postfächer, Anrede, CC.

Fixture: tests/fixtures/excel/ansprechpartner_beispiel.xlsx (synthetisch) und die Test-Stammdaten
(wik_leh kennt Lieferant 150 "Weingut Beispiel").
"""

from __future__ import annotations

from pathlib import Path

import pytest

from auftragsautomat.config import Settings
from auftragsautomat.kontakte import Kontaktliste, Zuordner, lade_kontaktliste, person_aus_name, positionsrang
from auftragsautomat.models import Kontakt, Kontaktgruppe, Kunde, Lieferant
from auftragsautomat.stammdaten.laden import lade_alle

FIXTURE = Path(__file__).parent / "fixtures" / "excel" / "ansprechpartner_beispiel.xlsx"


@pytest.fixture()
def liste(settings: Settings) -> Kontaktliste:
    return lade_kontaktliste(FIXTURE, settings=settings)


@pytest.fixture()
def zuordner(liste: Kontaktliste, settings: Settings) -> Zuordner:
    return Zuordner(liste, lade_alle(settings), settings)


# ---------------------------------------------------------------- Lieferanten


def test_lieferant_orders_kontakt_bevorzugt(zuordner: Zuordner):
    kontakt = zuordner.kontakt_fuer_lieferant("Weingut Beispiel")
    assert kontakt is not None and (kontakt.name, kontakt.position) == ("Giulia Esempio", "Orders")
    alle = zuordner.kontakte_fuer_lieferant("Weingut Beispiel Srl")
    assert [k.name for k in alle] == ["Giulia Esempio", "Marco Esempio"]
    # ohne "Orders": erster Kontakt der Firma
    assert zuordner.kontakt_fuer_lieferant("Musterhof").name == "Max Muster"  # type: ignore[union-attr]
    assert zuordner.kontakt_fuer_lieferant("Weingut Musterhof GmbH").email == "vertrieb@musterhof.example.org"  # type: ignore[union-attr]


def test_lieferant_mehrfachfirma_am_und_gesplittet(zuordner: Zuordner):
    for text in ("Cantina Beta", "Cantina Gamma", "Cantina Alpha s.r.l."):
        kontakt = zuordner.kontakt_fuer_lieferant(text)
        assert kontakt is not None and kontakt.email == "ordini@cantine-abc.example.org", text
    assert zuordner.firma_fuer_lieferant("Cantina Beta") == "Cantina Alpha & Cantina Beta & Cantina Gamma"


def test_lieferant_modell_ueber_lexware_nummer_und_matchcode(zuordner: Zuordner, liste: Kontaktliste):
    # Verknüpfung mit den Stammdaten: wik_leh 150 "Weingut Beispiel"
    assert zuordner.nummern_lieferanten == {"Weingut Beispiel Srl": {"wik_leh": "150"}}
    giulia = liste.kontakt_fuer_email("orders@weingut-beispiel.example.org")
    assert giulia is not None and giulia.lexware_nummer == "150"
    assert zuordner.nummer_fuer(giulia, "wik_leh") == "150" and zuordner.nummer_fuer(giulia, "woe_leh") is None
    per_nummer = Lieferant(firma_id="wik_leh", lieferantennummer="150", name="ganz anderer Name")
    assert zuordner.kontakt_fuer_lieferant(per_nummer) is giulia
    per_matchcode = Lieferant(firma_id="woe_leh", lieferantennummer="999", name="Weingut Beispiel S.r.l.", matchcode="BEISPIEL")
    assert zuordner.kontakt_fuer_lieferant(per_matchcode) is giulia


def test_lieferant_unbekannt_unsicher_oder_gattungswort_ergibt_none(zuordner: Zuordner, caplog):
    assert zuordner.kontakt_fuer_lieferant("Bollina") is None  # nur in den Stammdaten, nicht in der Liste
    assert zuordner.kontakt_fuer_lieferant("") is None and zuordner.kontakt_fuer_lieferant(None) is None
    assert zuordner.kontakt_fuer_lieferant("Weingut") is None  # Gattungswort trifft kein Weingut
    assert zuordner.kontakt_fuer_lieferant("Cantina") is None
    with caplog.at_level("WARNING"):
        assert zuordner.kontakt_fuer_lieferant("Weingut Beispielberg Nord") is None  # ähnlich, aber unter der Schwelle
    assert "unsicher" in caplog.text


def test_lieferant_mehrdeutig_ergibt_none(settings: Settings, caplog):
    liste = Kontaktliste(
        kontakte=[],
        lieferanten={
            "Weingut Beispiel Nord": [Kontakt(gruppe=Kontaktgruppe.LIEFERANT, firma="Weingut Beispiel Nord", email="a@example.org")],
            "Weingut Beispiel Süd": [Kontakt(gruppe=Kontaktgruppe.LIEFERANT, firma="Weingut Beispiel Süd", email="b@example.org")],
        },
    )
    z = Zuordner(liste, None, settings)
    with caplog.at_level("WARNING"):
        assert z.kontakt_fuer_lieferant("Weingut Beispiel") is None
    assert "mehrdeutig" in caplog.text


# ---------------------------------------------------------------- Kunden


def test_kunde_produktzeile_vor_allgemein_und_dispo_vor_buchhaltung(zuordner: Zuordner):
    mit_lieferant = zuordner.kontakte_fuer_kunde("Beispielmarkt Nord", "Weingut Beispiel Srl")
    assert [(k.name, k.produkt) for k in mit_lieferant] == [
        ("Herr Beispiel", "Weingut Beispiel"),
        ("Frau Beispiel", "Allgemein"),
        ("Buchhaltung", "Allgemein"),
    ]
    # anderer Lieferant: Produktzeile des fremden Weinguts entfällt
    anderer = zuordner.kontakte_fuer_kunde("Beispielmarkt Nord", "Domaine Exemple")
    assert [k.name for k in anderer] == ["Frau Beispiel", "Buchhaltung"]
    ohne = zuordner.kontakte_fuer_kunde("Beispielmarkt Nord")
    assert [k.name for k in ohne] == ["Frau Beispiel", "Buchhaltung", "Herr Beispiel"]
    assert zuordner.kontakte_fuer_kunde(Kunde(firma_id="wik_leh", kundennummer="1", name="Beispielmarkt Nord GmbH"))[0].name == "Frau Beispiel"
    assert zuordner.kontakte_fuer_kunde("Unbekannte Firma") == [] and zuordner.kontakte_fuer_kunde(None) == []


def test_positionsrang():
    assert positionsrang("Disposition") == 0 and positionsrang("Logistik / Versand") == 0
    assert positionsrang("Einkauf Wein Italien") == 1 and positionsrang("Orders") == 1
    assert positionsrang("Inhaber") == 2 and positionsrang(None) == 2
    assert positionsrang("Buchhaltung") == 3 and positionsrang(None, "Rechnungen") == 3


def test_spedition_fuer_kunde(zuordner: Zuordner, settings: Settings):
    assert zuordner.spedition_fuer_kunde("Beispielmarkt Nord") == "Beispiel-Logistik"  # Klammerzusatz entfernt
    assert zuordner.spedition_fuer_kunde("Weinhandlung Beispiel") == settings.kontakte.standard_spedition
    assert zuordner.spedition_fuer_kunde("Niemand") == "Hillebrand"
    settings.kontakte.standard_spedition = "Andere Spedition"
    assert zuordner.spedition_fuer_kunde("Niemand") == "Andere Spedition"


# ---------------------------------------------------------------- Speditionen


def test_postfach_spedition_nach_zweck(zuordner: Zuordner):
    assert zuordner.postfach_spedition("Hillebrand", "neue_auftraege").email == "neue-auftraege@hillebrand.example.org"  # type: ignore[union-attr]
    assert zuordner.postfach_spedition("Hillebrand Gori", "status").email == "info@hillebrand.example.org"  # type: ignore[union-attr]
    assert zuordner.postfach_spedition("hillebrand", "zoll").name == "Karl Beispiel"  # type: ignore[union-attr]
    # keine Buchhaltung bei Hillebrand -> erster Disponent
    assert zuordner.postfach_spedition("Hillebrand", "buchhaltung").name == "Petra Beispiel"  # type: ignore[union-attr]
    # Beispiel-Logistik: kein Auftragspostfach -> Disposition; Buchhaltung vorhanden
    assert zuordner.postfach_spedition("Beispiel Logistik", "neue_auftraege").name == "Zentrale"  # type: ignore[union-attr]
    assert zuordner.postfach_spedition("Beispiel-Logistik", "buchhaltung").email == "buchhaltung@beispiel-logistik.example.org"  # type: ignore[union-attr]
    assert zuordner.postfach_spedition("Fremdspedition", "neue_auftraege") is None
    assert zuordner.postfach_spedition(None, "status") is None
    with pytest.raises(ValueError, match="Zweck"):
        zuordner.postfach_spedition("Hillebrand", "kaffee")  # type: ignore[arg-type]


def test_postfach_bevorzugt_postfach_vor_person(settings: Settings):
    liste = Kontaktliste(
        speditionen={
            "Sped": [
                Kontakt(gruppe=Kontaktgruppe.SPEDITION, firma="Sped", name="Lisa Beispiel", position="Neue Aufträge, Disposition", email="l@sped.example.org"),
                Kontakt(gruppe=Kontaktgruppe.SPEDITION, firma="Sped", name="Auftragspostfach", position="Neue Aufträge", email="orders@sped.example.org"),
                Kontakt(gruppe=Kontaktgruppe.SPEDITION, firma="Sped", name="Rechnung", position="Nur Fax", email=None),
            ]
        }
    )
    z = Zuordner(liste, None, settings)
    assert z.postfach_spedition("Sped", "neue_auftraege").email == "orders@sped.example.org"  # type: ignore[union-attr]
    assert z.postfach_spedition("Sped", "zoll").name == "Lisa Beispiel"  # type: ignore[union-attr]  # Disponent als Rückfall
    liste.speditionen["Sped"] = [liste.speditionen["Sped"][2]]
    assert Zuordner(liste, None, settings).postfach_spedition("Sped", "status") is None


# ---------------------------------------------------------------- Anrede


def test_anrede_und_sprache(zuordner: Zuordner, liste: Kontaktliste):
    erwartet = {
        "dispo@beispielmarkt.example.org": ("Sehr geehrte Frau Beispiel", "de"),
        "einkauf@beispielmarkt.example.org": ("Sehr geehrter Herr Beispiel", "de"),
        "rechnung@beispielmarkt.example.org": ("Sehr geehrte Damen und Herren", "de"),
        "info@weinhandlung-beispiel.example.org": ("Sehr geehrter Herr Beispiel", "de"),
        "orders@weingut-beispiel.example.org": ("Dear Ms Esempio", "en"),
        "export@weingut-beispiel.example.org": ("Dear Mr Esempio", "en"),
        "vertrieb@musterhof.example.org": ("Sehr geehrter Herr Muster", "de"),
        "buchhaltung@musterhof.example.org": ("Sehr geehrte Frau Muster", "de"),
        "commandes@domaine-exemple.example.org": ("Dear Mr Exemple", "en"),
        "neue-auftraege@hillebrand.example.org": ("Sehr geehrte Damen und Herren", "de"),
        "p.beispiel@hillebrand.example.org": ("Sehr geehrte Frau Beispiel", "de"),
    }
    for email, (anrede, sprache) in erwartet.items():
        kontakt = liste.kontakt_fuer_email(email)
        assert kontakt is not None, email
        assert zuordner.anrede_fuer(kontakt) == (anrede, sprache), email
    petra = liste.kontakt_fuer_email("p.beispiel@hillebrand.example.org")
    assert petra is not None and petra.anrede is None
    assert zuordner.ergaenze_anrede(petra) == ("Sehr geehrte Frau Beispiel", "de")
    assert (petra.anrede, petra.sprache) == ("Frau", "de")


def test_anrede_unbekannt_regeln_und_land(settings: Settings):
    liste = Kontaktliste()
    z = Zuordner(liste, None, settings, anrede_regeln={"Du@Example.org": {"anrede": "Hallo Kim", "sprache": "de"}})
    unbekannt = Kontakt(gruppe=Kontaktgruppe.LIEFERANT, firma="X", name="Simone Beispiel", land="Italien")
    assert z.anrede_fuer(unbekannt) == ("Dear Simone Beispiel", "en")
    assert z.anrede_fuer(Kontakt(gruppe=Kontaktgruppe.LIEFERANT, firma="X", name="Kim Beispiel", land="Österreich")) == ("Guten Tag Kim Beispiel", "de")
    assert z.anrede_fuer(Kontakt(gruppe=Kontaktgruppe.LIEFERANT, firma="X", name="Luca Rossi", sprache="en")) == ("Dear Mr Rossi", "en")
    assert z.anrede_fuer(Kontakt(gruppe=Kontaktgruppe.KUNDE, firma="X", name="Frau Dr. Beispiel")) == ("Sehr geehrte Frau Beispiel", "de")
    assert z.anrede_fuer(Kontakt(gruppe=Kontaktgruppe.KUNDE, firma="X", name="Kim Beispiel", anrede="Herr")) == ("Sehr geehrter Herr Beispiel", "de")
    assert z.anrede_fuer(Kontakt(gruppe=Kontaktgruppe.KUNDE, firma="X", name=None)) == ("Sehr geehrte Damen und Herren", "de")
    assert z.anrede_fuer(Kontakt(gruppe=Kontaktgruppe.KUNDE, firma="X", name="Team Logistik", sprache="en")) == ("Dear Sir or Madam", "en")
    assert z.anrede_fuer(Kontakt(gruppe=Kontaktgruppe.KUNDE, firma="X", name="Kim Beispiel", email="du@example.org")) == ("Hallo Kim", "de")


def test_person_aus_name():
    assert person_aus_name("Dr. Giulia Esempio").nachname == "Esempio"
    assert person_aus_name("Dr. Giulia Esempio").geschlecht == "w"
    assert person_aus_name("Herrn Max von Muster").nachname == "von Muster"
    assert person_aus_name("Mr Jean Exemple").geschlecht == "m"
    assert person_aus_name("Auftrags-Postfach").geschlecht == "neutral"
    assert person_aus_name("Buchhaltung").geschlecht == "neutral"
    assert person_aus_name("").geschlecht == "neutral" and person_aus_name(None).voller_name is None
    assert person_aus_name("Giulia").geschlecht == "w" and person_aus_name("Giulia").vorname == "Giulia"
    assert person_aus_name("Beispiel", anrede="Frau").nachname == "Beispiel"
    assert person_aus_name("Andrea Esempio").geschlecht == "m"  # italienisch männlich
    assert person_aus_name("Simone Beispiel").geschlecht == "unbekannt"


# ---------------------------------------------------------------- CC


def test_empfaenger_cc(zuordner: Zuordner, settings: Settings, liste: Kontaktliste):
    assert zuordner.empfaenger_cc() == []
    settings.kommunikation.cc_standard = ["Kollegin@example.org", "kollegin@example.org"]
    liste.kontakte.append(Kontakt(gruppe=Kontaktgruppe.INTERN, firma="Intern", name="Logistik", email="logistik@example.org"))
    assert zuordner.empfaenger_cc() == ["Kollegin@example.org", "logistik@example.org"]
    andere = settings.model_copy(deep=True)
    andere.kommunikation.cc_standard = ["x@example.org"]
    assert zuordner.empfaenger_cc(andere) == ["x@example.org", "logistik@example.org"]
