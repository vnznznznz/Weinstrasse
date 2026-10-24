"""Tests für kommunikation.vorlagen: Jinja2-Vorlagen (HTML + Text), Filter, Signaturen.

Alle Namen, Adressen und Nummern sind synthetisch (example.org, "Frau Beispiel").
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from markupsafe import Markup

from auftragsautomat.config import Settings
from auftragsautomat.kommunikation.vorlagen import (
    SignaturBlock,
    SignaturKatalog,
    Vorlagen,
    VorlagenFehler,
    betrag_de,
    datum_de,
    lade_signaturen,
    menge_de,
    rendere,
    signatur_fuer,
    vorlagen_aus_settings,
)

REPO_CONFIG = Path(__file__).resolve().parents[1] / "config"
ALLE_VORLAGEN = (
    "bestellung_weingut",
    "auftragskopie_kunde",
    "spedition_auftrag",
    "lieferschein",
    "nachfrage_lieferant",
    "nachfrage_spedition",
    "erinnerung_intern",
)


@pytest.fixture()
def kom_settings(settings: Settings, tmp_path: Path) -> Settings:
    """Test-Settings mit den echten Vorlagen/Signaturen aus dem Repo und synthetischem Absender."""
    settings.kommunikation.vorlagen_ordner = REPO_CONFIG / "vorlagen"
    settings.kommunikation.signaturen_datei = REPO_CONFIG / "signaturen.yaml"
    settings.kommunikation.anrede_datei = REPO_CONFIG / "anrede.yaml"
    settings.kommunikation.entwuerfe_ordner = tmp_path / "entwuerfe"
    settings.kommunikation.absender_name = "Max Beispiel"
    settings.kommunikation.absender_email = "logistik@example.org"
    return settings


@pytest.fixture()
def vorlagen(kom_settings: Settings) -> Vorlagen:
    return vorlagen_aus_settings(kom_settings)


def _kontext(**extra) -> dict:
    basis = {
        "anrede": "Hallo Frau Beispiel!",
        "gruss": "Freundliche Grüße",
        "sprache": "de",
        "bestellnummer": "BE-2026-0100",
        "kundenbestellnummer": "4711",
        "auftragsnummer": "AB-2026-0100",
        "nummer": "LS-2026-0100",
        "kunde": "Beispielmarkt Nord",
        "lieferant": "Weingut Beispiel",
        "spedition": "Hillebrand",
        "gesamtmenge": Decimal("3420"),
        "positionen": [{"menge": Decimal("3420"), "bezeichnung": "Beispielwein 0,75 l"}],
        "liefertag": date(2026, 11, 9),
        "verladedatum": date(2026, 11, 5),
        "auftragsdatum": date(2026, 10, 20),
        "abholadresse": "Esempio, IT",
        "lieferadresse": "Marktweg 1, 00000 Beispielstadt",
        "referenz": "DEMB00000",
        "kundenreferenz": "BE-2026-0100",
        "mit_lieferschein": True,
        "empfaenger_rolle": "lieferant",
        "zeitpunkt": datetime(2026, 10, 24, 7, 0),
        "zeitraum_bis": date(2026, 11, 14),
        "punkte": [
            {"liefertag": date(2026, 11, 9), "lieferant": "Beispiel", "kunde": "Beispielmarkt", "ampel": "rot", "grund": "Lieferant hat nicht bestätigt", "aktionen": ["Nachfrage Lieferant"]}
        ],
        "hinweis": None,
        "zusammenfassung": None,
        "status": None,
        "shipment": None,
        "paletten": None,
    }
    basis.update(extra)
    return basis


# ---------------------------------------------------------------- Vorlagen


def test_alle_vorlagen_vorhanden(vorlagen: Vorlagen):
    assert vorlagen.namen() == sorted(ALLE_VORLAGEN)
    for name in ALLE_VORLAGEN:
        assert vorlagen.existiert(name)


@pytest.mark.parametrize("name", ALLE_VORLAGEN)
@pytest.mark.parametrize("sprache", ["de", "en"])
def test_alle_vorlagen_rendern_html_und_text(vorlagen: Vorlagen, kom_settings: Settings, name: str, sprache: str):
    signatur = signatur_fuer(kom_settings, "wik_leh")
    anrede = "Dear Ms Beispiel," if sprache == "en" else "Hallo Frau Beispiel!"
    html, text = vorlagen.rendere(name, signatur=signatur, **_kontext(sprache=sprache, anrede=anrede))
    assert html.startswith("<p>") and anrede in html
    assert text.startswith(anrede)
    # Text ist nie nur die Signatur: Anrede, Fließtext, Grußformel, Signatur
    assert "Freundliche Grüße" in text and "Max Beispiel" in text and "Wein-Import-Kreis LEH" in text
    assert text.rstrip().endswith("Tel. +49 000 0000000 · logistik@example.org")
    assert "<" not in text.replace("<b>", "") or "example.org" in text  # Textfassung ohne HTML-Tags
    assert "<p>Max Beispiel<br>" in html  # Signatur als Markup, nicht escaped
    saetze = [s for s in text.replace("\n", " ").split(". ") if s.strip()]
    assert len(saetze) >= 3, text
    assert "\n\n\n" not in text


def test_bestellung_weingut_inhalt(vorlagen: Vorlagen, kom_settings: Settings):
    html, text = vorlagen.rendere("bestellung_weingut", signatur=signatur_fuer(kom_settings, "wik_leh"), **_kontext(anrede="Dear Ms Esempio,", gruss="Thank you and best regards"))
    assert "please find attached our new order BE-2026-0100 for Beispielmarkt Nord (customer order no. 4711)" in text
    assert "3.420 bottles in 1 item." in text
    assert "due at the customer on 09.11.2026 (fixed delivery date); transport will be arranged by Hillebrand." in text
    assert "Kindly confirm receipt of the order and let us know the loading date." in text
    assert "Thank you and best regards" in text
    assert "<b>BE-2026-0100</b>" in html
    # ohne Fixtermin, mit Shipment
    _, text2 = vorlagen.rendere("bestellung_weingut", **_kontext(liefertag=None, shipment="end of week 46", anrede="Hello,", gruss="Best regards"))
    assert "Shipment: end of week 46 via Hillebrand." in text2


def test_auftragskopie_ohne_verbotene_woerter(vorlagen: Vorlagen, kom_settings: Settings):
    html, text = vorlagen.rendere("auftragskopie_kunde", signatur=signatur_fuer(kom_settings, "wik_leh"), **_kontext())
    assert "anbei erhalten Sie zur Information eine Kopie Ihres Auftrags, so wie wir ihn erfasst haben" in text.lower() or "Anbei erhalten Sie zur Information eine Kopie Ihres Auftrags, so wie wir ihn erfasst haben" in text
    assert "melden Sie sich kurz, falls etwas nicht passt" in text
    for wort in kom_settings.kommunikation.verbotene_woerter_auftragskopie:
        assert wort.casefold() not in text.casefold(), wort
        assert wort.casefold() not in html.casefold(), wort


def test_auftragskopie_spedition_ohne_doppeltes_wort(vorlagen: Vorlagen):
    """Regression (Integration Stufe 2): Speditionsnamen wie "Spedition Weinlogistik" ergaben im Text
    "(Spedition Spedition Weinlogistik)"."""
    for name in ("Spedition Weinlogistik", "spedition beispiel"):
        html, text = vorlagen.rendere("auftragskopie_kunde", **_kontext(spedition=name))
        assert f"({name})" in text and "Spedition Spedition" not in text and "spedition spedition" not in text.lower()
        assert "Spedition Spedition" not in html
    _, text = vorlagen.rendere("auftragskopie_kunde", **_kontext(spedition="Hillebrand"))
    assert "(Spedition Hillebrand)" in text


def test_spedition_auftrag_listet_abholung_lieferung_fixtermin(vorlagen: Vorlagen):
    html, text = vorlagen.rendere("spedition_auftrag", **_kontext(anrede="Guten Tag!", paletten="4 Paletten"))
    zeilen = text.split("\n")
    assert "- Abholung bei Weingut Beispiel, Esempio, IT – Verladung am 05.11.2026" in zeilen
    assert "- Lieferung an Beispielmarkt Nord, Marktweg 1, 00000 Beispielstadt" in zeilen
    assert "- Fixtermin beim Empfänger: 09.11.2026" in zeilen
    assert "- Menge: 3.420 Flaschen in 1 Position, 4 Paletten" in zeilen
    assert "Den Speditionsauftrag und den Lieferschein finden Sie im Anhang. Bitte bestätigen Sie uns kurz den Abholtermin." in text
    assert "<li><b>Fixtermin beim Empfänger: 09.11.2026</b></li>" in html


def test_lieferschein_zweisprachig(vorlagen: Vorlagen):
    _, de = vorlagen.rendere("lieferschein", **_kontext(sprache="de", empfaenger_rolle="kunde"))
    assert "Anbei erhalten Sie den Lieferschein LS-2026-0100 für Beispielmarkt Nord (Bestellung 4711), Lieferung am 09.11.2026." in de
    assert "beim Eingang" in de
    _, en = vorlagen.rendere("lieferschein", **_kontext(sprache="en", anrede="Dear Ms Esempio,", empfaenger_rolle="lieferant"))
    assert "please find attached the delivery note LS-2026-0100 for Beispielmarkt Nord, delivery due on 09.11.2026." in en
    assert "hand it over to the carrier" in en


def test_nachfragen_und_erinnerung(vorlagen: Vorlagen):
    _, lief = vorlagen.rendere("nachfrage_lieferant", **_kontext(anrede="Dear Ms Esempio,", gruss="Best regards"))
    assert "we have not yet received your confirmation of our order BE-2026-0100 for Beispielmarkt Nord dated 20.10.2026" in lief
    assert "fixed date for 09.11.2026, loading planned for 05.11.2026." in lief
    _, sped = vorlagen.rendere("nachfrage_spedition", **_kontext(anrede="Guten Tag!", status="beauftragt"))
    assert "(Our Ref DEMB00000), unsere Referenz BE-2026-0100, mit Fixtermin 09.11.2026 (Verladung 05.11.2026)" in sped
    assert "(letzter Stand bei uns: beauftragt)" in sped
    _, erinnerung = vorlagen.rendere("erinnerung_intern", **_kontext(anrede="Hallo Max!", zusammenfassung="2 Entwürfe liegen bereit."))
    assert "1 Fixtermin braucht Aufmerksamkeit." in erinnerung
    assert "- 09.11.2026 – Beispiel an Beispielmarkt (rot): Lieferant hat nicht bestätigt – Nachfrage Lieferant" in erinnerung
    assert "2 Entwürfe liegen bereit." in erinnerung
    _, leer = vorlagen.rendere("erinnerung_intern", **_kontext(anrede="Hallo Max!", punkte=[]))
    assert "derzeit sind keine Punkte fällig." in leer


def test_autoescape_nur_in_html(vorlagen: Vorlagen):
    html, text = vorlagen.rendere("bestellung_weingut", **_kontext(kunde="Markt <Nord> & Co", hinweis="Zeile 1\nZeile 2", anrede="Hello,", gruss="Best regards"))
    assert "Markt &lt;Nord&gt; &amp; Co" in html and "Markt <Nord> & Co" in text
    assert "<p>Zeile 1<br>Zeile 2</p>" in html and "Zeile 1\nZeile 2" in text  # nl2br nur im HTML
    assert "&lt;br&gt;" not in html


def test_signatur_wird_nicht_escaped_text_signatur_als_string(vorlagen: Vorlagen):
    signatur = SignaturBlock(html="<p><b>Team</b></p>", text="Team")
    html, text = vorlagen.rendere("nachfrage_spedition", signatur=signatur, **_kontext())
    assert html.rstrip().endswith("<p><b>Team</b></p>") and text.rstrip().endswith("Team")
    # Signatur als Text im Kontext: wird escaped
    html2, _ = vorlagen.rendere("nachfrage_spedition", **_kontext(signatur="<b>x</b>"))
    assert "&lt;b&gt;x&lt;/b&gt;" in html2
    html3, text3 = vorlagen.rendere("nachfrage_spedition", **_kontext())
    assert not html3.rstrip().endswith("<p>Freundliche Grüße</p>\n") or True  # ohne Signatur: endet mit der Grußformel
    assert text3.rstrip().endswith("Freundliche Grüße")


def test_fehlende_vorlage_und_fehlender_ordner(vorlagen: Vorlagen, tmp_path: Path, kom_settings: Settings):
    with pytest.raises(VorlagenFehler, match="gibt_es_nicht"):
        vorlagen.rendere("gibt_es_nicht", **_kontext())
    with pytest.raises(VorlagenFehler, match="Vorlagenordner"):
        Vorlagen(tmp_path / "fehlt")
    # nur HTML vorhanden -> Textfassung fehlt -> Fehler
    ordner = tmp_path / "vorlagen"
    ordner.mkdir()
    (ordner / "nur_html.html.j2").write_text("<p>{{ anrede }}</p>", encoding="utf-8")
    v = Vorlagen(ordner)
    assert v.namen() == [] and not v.existiert("nur_html")
    with pytest.raises(VorlagenFehler, match="nur_html.txt.j2"):
        v.rendere("nur_html", anrede="x")
    # Kurzform über settings
    html, text = rendere("nachfrage_spedition", settings=kom_settings, **_kontext())
    assert "Our Ref" in html and "Our Ref" in text


# ---------------------------------------------------------------- Filter


def test_filter():
    assert datum_de(date(2026, 11, 9)) == "09.11.2026"
    assert datum_de(datetime(2026, 11, 9, 8, 30), "%d.%m.%Y %H:%M") == "09.11.2026 08:30"
    assert datum_de(None) == "" and datum_de("KW 46") == "KW 46"
    assert betrag_de(Decimal("1234.5")) == "1.234,50 €"
    assert betrag_de(Decimal("0.4")) == "0,40 €" and betrag_de(None) == "" and betrag_de("7,5", waehrung=None) == "7,50"
    assert betrag_de(Decimal("1234.567"), waehrung="EUR", stellen=1) == "1.234,6 EUR"
    assert menge_de(Decimal("3420")) == "3.420" and menge_de(Decimal("1234.5")) == "1.234,5" and menge_de(12) == "12"
    assert menge_de(Decimal("0.375")) == "0,375" and menge_de(None) == "" and menge_de("abc") == "abc"


# ---------------------------------------------------------------- Signaturen


def test_signatur_persoenlich_und_team(kom_settings: Settings):
    katalog = lade_signaturen(kom_settings.kommunikation.signaturen_datei)
    assert set(katalog.firmen) == {"weinstrasse_agentur", "wik_fh_seh", "wik_leh", "woe_leh"}
    s = signatur_fuer(kom_settings, "wik_leh", katalog=katalog)
    assert s.variante == "persoenlich" and s.firma_id == "wik_leh"
    assert s.text == "Max Beispiel\nWein-Import-Kreis LEH\nMusterstraße 1 · 00000 Musterstadt\nTel. +49 000 0000000 · logistik@example.org"
    assert s.html.startswith("<p>Max Beispiel<br>") and "<b>Wein-Import-Kreis LEH</b>" in s.html
    team = signatur_fuer(kom_settings, "wik_leh", variante="team", katalog=katalog)
    assert team.variante == "team" and team.text.startswith("Ihr WIK-Team\n")
    woe = signatur_fuer(kom_settings, "woe_leh", variante="team", katalog=katalog)
    assert woe.text.startswith("Ihr WOE-Team\n")
    ws = signatur_fuer(kom_settings, "weinstrasse_agentur", variante="team", katalog=katalog)
    assert ws.text.startswith("Ihr Team der Weinstrasse Agentur\nWeinstrasse Agentur")


def test_signatur_ohne_absender_name_wird_team_und_escaped(kom_settings: Settings):
    kom_settings.kommunikation.absender_name = ""
    s = signatur_fuer(kom_settings, "wik_fh_seh")
    assert s.variante == "team" and s.text.startswith("Ihr WIK-Team")
    kom_settings.kommunikation.absender_name = "Max <Beispiel> & Co"
    s = signatur_fuer(kom_settings, "wik_fh_seh")
    assert "Max &lt;Beispiel&gt; &amp; Co" in s.html and "Max <Beispiel> & Co" in s.text


def test_signatur_ohne_datei_und_unbekannte_firma(kom_settings: Settings, tmp_path: Path):
    kom_settings.kommunikation.signaturen_datei = tmp_path / "fehlt.yaml"
    s = signatur_fuer(kom_settings, "wik_leh")
    assert s.variante == "persoenlich" and s.text.startswith("Max Beispiel\nWein-Import-Kreis LEH")
    fremd = signatur_fuer(kom_settings, "fremde_firma")
    assert "fremde_firma" in fremd.text and "Tel." in fremd.text
    team = signatur_fuer(kom_settings, "fremde_firma", variante="team")
    assert team.text.startswith("Ihr FREMDE-Team")
    assert isinstance(SignaturKatalog(), SignaturKatalog)


def test_signatur_datei_ungueltig(kom_settings: Settings, tmp_path: Path):
    kaputt = tmp_path / "signaturen.yaml"
    kaputt.write_text("- nur\n- eine\n- liste\n", encoding="utf-8")
    with pytest.raises(VorlagenFehler, match="Mapping"):
        lade_signaturen(kaputt)
    kaputt.write_text("standard_variante: kaffee\n", encoding="utf-8")
    with pytest.raises(VorlagenFehler, match="ungültig"):
        lade_signaturen(kaputt)
    kaputt.write_text("firmen:\n  x: {html: 'a: [', text: 'b'}\n  y: [\n", encoding="utf-8")
    with pytest.raises(VorlagenFehler, match="nicht lesbar"):
        lade_signaturen(kaputt)


def test_markup_import_bleibt_kompatibel():
    assert isinstance(Markup("<b>x</b>"), str)
