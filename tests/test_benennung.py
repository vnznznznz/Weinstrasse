"""Tests für benennung.py (Benennungsschema "Titulierung").

Alle Namen, Nummern und Kurznamen sind synthetisch ("Weingut Beispiel", "Frau Beispiel", "4711").
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import pytest

from auftragsautomat import benennung
from auftragsautomat.benennung import (
    OHNE_NUMMER,
    SPEDITIONSARTEN,
    UNBEKANNT,
    bereinige,
    dateiname,
    kurzname_kunde,
    kurzname_lieferant,
    mit_auftragsnummer,
    name_fuer_paket,
    normalisiere,
    pruefe_auftragskopie_name,
    spedition_fuer_paket,
    speditionsname,
)
from auftragsautomat.config import Firma, Settings
from auftragsautomat.models import (
    DOKUMENTKUERZEL,
    Belegfelder,
    Belegpaket,
    Eingangstyp,
    Extraktionsmethode,
    Kunde,
    KundeZuordnung,
    Lieferant,
    LieferantZuordnung,
    MatchStatus,
    RohBestellung,
)

DATUM = date(2026, 10, 24)
DATUM_TEXT = "24.10.2026"
TRENNER = " – "


def _firma(settings: Settings, firma_id: str) -> Firma:
    return settings.firma(firma_id)


def _roh(**felder) -> RohBestellung:
    basis: dict = {
        "eingangstyp": Eingangstyp.KUNDENBESTELLUNG_PDF,
        "quelldatei": Path("eingang/bestellung.pdf"),
        "extraktionsmethode": Extraktionsmethode.PDFPLUMBER,
    }
    basis.update(felder)
    return RohBestellung(**basis)


def _paket(
    firma_id: str | None = "wik_fh_seh",
    *,
    kunde: Kunde | None = None,
    lieferant: Lieferant | None = None,
    belegfelder: Belegfelder | None = None,
    ist_revision: bool = False,
    **roh_felder,
) -> Belegpaket:
    paket = Belegpaket(roh=_roh(**roh_felder), firma_id=firma_id, ist_revision=ist_revision)
    if kunde is not None:
        paket.kunde = KundeZuordnung(kunde=kunde, status=MatchStatus.SICHER, quelle="alias")
    if lieferant is not None:
        paket.lieferant = LieferantZuordnung(lieferant=lieferant, status=MatchStatus.SICHER, quelle="alias")
    if belegfelder is not None:
        paket.belegfelder = belegfelder
    return paket


def _kunde(matchcode: str | None = "Musterhandel", name: str = "Weinhandel Muster GmbH") -> Kunde:
    return Kunde(firma_id="wik_fh_seh", kundennummer="10001", name=name, matchcode=matchcode)


def _lieferant(matchcode: str | None = "Beispielgut", name: str = "Weingut Beispiel srl") -> Lieferant:
    return Lieferant(firma_id="wik_fh_seh", lieferantennummer="90001", name=name, matchcode=matchcode)


# ---------------------------------------------------------------------------
# Schema Weinstrasse Agentur (WS)
# ---------------------------------------------------------------------------


def test_ws_schema_ohne_nummer(settings: Settings):
    name = dateiname(
        "Cde",
        firma=_firma(settings, "weinstrasse_agentur"),
        lieferant="Weingut Beispiel",
        kunde="Weinhandel Muster",
        bestellnummer="4711",  # wird im WS-Schema nicht verwendet
        auftragsnummer=None,
        datum=DATUM,
        settings=settings,
    )
    assert name == f"Weingut Beispiel{TRENNER}Weinhandel Muster{TRENNER}Cde{TRENNER}{DATUM_TEXT}.pdf"


def test_ws_spar_mit_auftragsnummer(settings: Settings):
    name = dateiname(
        "Kopie",
        firma=_firma(settings, "weinstrasse_agentur"),
        lieferant="Weingut Beispiel",
        kunde="SPAR Beispielmarkt",
        bestellnummer=None,
        auftragsnummer="4711",
        datum=DATUM,
        settings=settings,
    )
    assert name == f"Weingut Beispiel{TRENNER}SPAR Beispielmarkt{TRENNER}4711{TRENNER}Kopie{TRENNER}{DATUM_TEXT}.pdf"


def test_ws_spar_ohne_auftragsnummer_hinweis_im_log(settings: Settings, caplog: pytest.LogCaptureFixture):
    with caplog.at_level(logging.WARNING, logger="auftragsautomat.benennung"):
        name = dateiname(
            "Cde",
            firma=_firma(settings, "weinstrasse_agentur"),
            lieferant="Weingut Beispiel",
            kunde="Spar",
            bestellnummer=None,
            auftragsnummer=None,
            datum=DATUM,
            settings=settings,
        )
    assert name == f"Weingut Beispiel{TRENNER}Spar{TRENNER}Cde{TRENNER}{DATUM_TEXT}.pdf"
    assert any("Kundenbestellnummer fehlt" in r.getMessage() for r in caplog.records)


def test_kunden_mit_auftragsnummer_normalisiert_und_teilstring(settings: Settings):
    assert mit_auftragsnummer("EUROSPAR Beispiel", settings)  # "Spar" als Teilstring, Großschreibung egal
    assert not mit_auftragsnummer("Weinhandel Muster", settings)
    assert not mit_auftragsnummer("", settings)
    settings.benennung.kunden_mit_auftragsnummer = ["Müller Beispiel"]
    assert mit_auftragsnummer("MUELLER BEISPIEL GmbH", settings)  # Umlaut vs. ue
    assert mit_auftragsnummer("Müller Beispiel", settings)
    settings.benennung.kunden_mit_auftragsnummer = [""]
    assert not mit_auftragsnummer("Spar", settings)  # leerer Eintrag trifft nie


# ---------------------------------------------------------------------------
# Schema WIK / WOE
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("firma_id", "kurz"),
    [("wik_fh_seh", "WIK"), ("wik_leh", "WIK"), ("woe_leh", "WOE")],
)
def test_wik_woe_schema(settings: Settings, firma_id: str, kurz: str):
    name = dateiname(
        "Cde",
        firma=_firma(settings, firma_id),
        lieferant="Weingut Beispiel",
        kunde="Weinhandel Muster",
        bestellnummer="4711",
        auftragsnummer="4711",
        datum=DATUM,
        settings=settings,
    )
    assert name == f"Weingut Beispiel{TRENNER}{kurz}{TRENNER}4711{TRENNER}Cde{TRENNER}{DATUM_TEXT}.pdf"
    assert "Weinhandel Muster" not in name  # WIK/WOE: kein Kundenname im Dateinamen


def test_wik_ohne_bestellnummer(settings: Settings, caplog: pytest.LogCaptureFixture):
    with caplog.at_level(logging.WARNING, logger="auftragsautomat.benennung"):
        name = dateiname(
            "Ls",
            firma=_firma(settings, "wik_leh"),
            lieferant="Weingut Beispiel",
            kunde="Weinhandel Muster",
            bestellnummer=None,
            auftragsnummer=None,
            datum=DATUM,
            settings=settings,
        )
    assert name == f"Weingut Beispiel{TRENNER}WIK{TRENNER}{OHNE_NUMMER}{TRENNER}Ls{TRENNER}{DATUM_TEXT}.pdf"
    assert any(OHNE_NUMMER in r.getMessage() for r in caplog.records)


def test_wik_bestellnummer_nur_leerzeichen_gilt_als_fehlend(settings: Settings):
    name = dateiname(
        "Cde",
        firma=_firma(settings, "wik_leh"),
        lieferant="Weingut Beispiel",
        kunde="Weinhandel Muster",
        bestellnummer="   ",
        auftragsnummer=None,
        datum=DATUM,
        settings=settings,
    )
    assert f"{TRENNER}{OHNE_NUMMER}{TRENNER}" in name


def test_ungueltiges_kuerzel(settings: Settings):
    with pytest.raises(ValueError, match="Dokumentkürzel"):
        dateiname(
            "AB",
            firma=_firma(settings, "wik_leh"),
            lieferant="Weingut Beispiel",
            kunde="Weinhandel Muster",
            bestellnummer="4711",
            auftragsnummer=None,
            datum=DATUM,
            settings=settings,
        )


@pytest.mark.parametrize("kuerzel", sorted(DOKUMENTKUERZEL))
def test_alle_dokumentkuerzel_erlaubt(settings: Settings, kuerzel: str):
    name = dateiname(
        kuerzel,
        firma=_firma(settings, "wik_leh"),
        lieferant="Weingut Beispiel",
        kunde="Weinhandel Muster",
        bestellnummer="4711",
        auftragsnummer=None,
        datum=DATUM,
        settings=settings,
    )
    assert f"{TRENNER}{kuerzel}{TRENNER}" in name


def test_endung_normalisierung(settings: Settings):
    felder = dict(
        firma=_firma(settings, "wik_leh"),
        lieferant="Weingut Beispiel",
        kunde="Weinhandel Muster",
        bestellnummer="4711",
        auftragsnummer=None,
        datum=DATUM,
        settings=settings,
    )
    assert dateiname("Cde", endung="pdf", **felder).endswith(".pdf")
    assert dateiname("Kd", endung=".eml", **felder).endswith(f"{DATUM_TEXT}.eml")
    assert dateiname("Cde", endung="", **felder).endswith(DATUM_TEXT)  # z. B. Dossier-Ordner


def test_datum_standard_heute(settings: Settings, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(benennung, "_heute", lambda: date(2026, 1, 2))
    name = dateiname(
        "Cde",
        firma=_firma(settings, "wik_leh"),
        lieferant="Weingut Beispiel",
        kunde="Weinhandel Muster",
        bestellnummer="4711",
        auftragsnummer=None,
        settings=settings,
    )
    assert name.endswith(f"{TRENNER}02.01.2026.pdf")


def test_datumsformat_und_trenner_aus_settings(settings: Settings):
    settings.benennung.datumsformat = "%Y-%m-%d"
    settings.benennung.trenner = "_"
    name = dateiname(
        "Cde",
        firma=_firma(settings, "wik_leh"),
        lieferant="Weingut Beispiel",
        kunde="Weinhandel Muster",
        bestellnummer="4711",
        auftragsnummer=None,
        datum=DATUM,
        settings=settings,
    )
    assert name == "Weingut Beispiel_WIK_4711_Cde_2026-10-24.pdf"


# ---------------------------------------------------------------------------
# Schema Spedition
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("art", SPEDITIONSARTEN)
def test_speditionsname(settings: Settings, art: str):
    name = speditionsname(
        art,  # type: ignore[arg-type]
        spedition="Spedition Beispiel",
        lieferant="Weingut Beispiel",
        kunde_oder_bestellnr="4711",
        datum=DATUM,
        settings=settings,
    )
    assert name == f"Spedition Beispiel{TRENNER}{art}{TRENNER}Weingut Beispiel{TRENNER}4711{TRENNER}{DATUM_TEXT}.pdf"


def test_speditionsname_ungueltige_art(settings: Settings):
    with pytest.raises(ValueError, match="Speditionsart"):
        speditionsname(
            "Auftrag",  # type: ignore[arg-type]
            spedition="Spedition Beispiel",
            lieferant="Weingut Beispiel",
            kunde_oder_bestellnr="4711",
            datum=DATUM,
            settings=settings,
        )


def test_speditionsname_fehlende_teile_unbekannt(settings: Settings):
    name = speditionsname(
        "Speditionsbestätigung",
        spedition=None,
        lieferant="",
        kunde_oder_bestellnr=None,
        datum=DATUM,
        settings=settings,
    )
    assert name == f"{UNBEKANNT}{TRENNER}Speditionsbestätigung{TRENNER}{UNBEKANNT}{TRENNER}{UNBEKANNT}{TRENNER}{DATUM_TEXT}.pdf"


# ---------------------------------------------------------------------------
# Sonderzeichen und Länge
# ---------------------------------------------------------------------------


def test_bereinige_sonderzeichen(settings: Settings):
    assert bereinige('Weingut "Beispiel" / Nr: 1?', settings) == "Weingut -Beispiel- - Nr- 1-"
    assert bereinige('Beispiel"/Süd', settings) == "Beispiel-Süd"  # Folge verbotener Zeichen -> ein Ersatzzeichen
    assert bereinige("a\\b*c<d>e|f", settings) == "a-b-c-d-e-f"
    assert bereinige("Zeile\x00eins\tzwei\nende", settings) == "Zeile-eins-zwei-ende"  # Steuerzeichen -> Ersatz
    assert bereinige("  Mehr   Leerzeichen   hier  ", settings) == "Mehr Leerzeichen hier"
    assert bereinige("Punkte am Ende... ", settings) == "Punkte am Ende"
    assert bereinige("Weingut Müller & Söhne – Château", settings) == "Weingut Müller & Söhne – Château"  # Umlaute bleiben
    assert bereinige("", settings) == ""


def test_bereinige_ersatzzeichen_und_laenge_aus_settings(settings: Settings):
    settings.benennung.ersatzzeichen = "_"
    assert bereinige("12/2026", settings) == "12_2026"
    settings.benennung.ersatzzeichen = ""
    assert bereinige("12/2026", settings) == "122026"
    settings.benennung.max_laenge = 10
    assert bereinige("a" * 30, settings) == "a" * 10
    assert bereinige("abcdefgh. x", settings) == "abcdefgh"  # Punkt/Leerzeichen nach dem Kürzen entfernt


def test_sonderzeichen_im_dateinamen(settings: Settings):
    name = dateiname(
        "Cde",
        firma=_firma(settings, "wik_leh"),
        lieferant='Weingut "Beispiel"/Süd',
        kunde="Kunde: Muster",
        bestellnummer="4711/2026?",
        auftragsnummer=None,
        datum=DATUM,
        settings=settings,
    )
    assert name == f"Weingut -Beispiel-Süd{TRENNER}WIK{TRENNER}4711-2026-{TRENNER}Cde{TRENNER}{DATUM_TEXT}.pdf"
    assert not any(z in name for z in '\\/:*?"<>|')


def test_leere_kurznamen_werden_unbekannt(settings: Settings):
    name = dateiname(
        "Cde",
        firma=_firma(settings, "weinstrasse_agentur"),
        lieferant="  ",
        kunde="???",
        bestellnummer=None,
        auftragsnummer=None,
        datum=DATUM,
        settings=settings,
    )
    assert name.startswith(f"{UNBEKANNT}{TRENNER}-{TRENNER}Cde")


def test_laengenkuerzung_nur_kurznamen(settings: Settings):
    settings.benennung.max_laenge = 80
    lieferant = "Weingut Beispiel " + "Langname " * 20
    kunde = "Kundenname " * 20
    name = dateiname(
        "Kopie",
        firma=_firma(settings, "weinstrasse_agentur"),
        lieferant=lieferant,
        kunde=kunde,
        bestellnummer=None,
        auftragsnummer=None,
        datum=DATUM,
        settings=settings,
    )
    assert len(name) <= 80
    assert name.endswith(f"{TRENNER}Kopie{TRENNER}{DATUM_TEXT}.pdf")
    teile = name[: -len(".pdf")].split(TRENNER)
    assert len(teile) == 4
    assert teile[0].startswith("Weingut Beispiel")
    assert teile[1].startswith("Kundenname")
    assert abs(len(teile[0]) - len(teile[1])) <= 2  # gleichmäßig gekürzt (je Schritt 1 Zeichen + Leerzeichen am Ende)
    assert not teile[0].endswith((" ", ".")) and not teile[1].endswith((" ", "."))


def test_laengenkuerzung_wik_nummer_und_firma_bleiben(settings: Settings):
    settings.benennung.max_laenge = 60
    name = dateiname(
        "Cde",
        firma=_firma(settings, "woe_leh"),
        lieferant="Weingut Beispiel mit sehr langem Namen und Zusatz GmbH & Co. KG",
        kunde="egal",
        bestellnummer="4711-2026-0001",
        auftragsnummer=None,
        datum=DATUM,
        settings=settings,
    )
    assert len(name) <= 60
    assert f"{TRENNER}WOE{TRENNER}4711-2026-0001{TRENNER}Cde{TRENNER}{DATUM_TEXT}.pdf" in name


def test_laengenkuerzung_hart_wenn_feste_teile_zu_lang(settings: Settings, caplog: pytest.LogCaptureFixture):
    settings.benennung.max_laenge = 30
    with caplog.at_level(logging.WARNING, logger="auftragsautomat.benennung"):
        name = dateiname(
            "Cde",
            firma=_firma(settings, "wik_leh"),
            lieferant="Weingut Beispiel",
            kunde="egal",
            bestellnummer="4711-2026-0001-0002-0003",
            auftragsnummer=None,
            datum=DATUM,
            settings=settings,
        )
    assert len(name) <= 30
    assert name.endswith(".pdf")
    assert any("hart abgeschnitten" in r.getMessage() for r in caplog.records)


def test_speditionsname_laengenkuerzung(settings: Settings):
    settings.benennung.max_laenge = 70
    name = speditionsname(
        "Speditionsauftrag",
        spedition="Spedition " + "Beispiel " * 10,
        lieferant="Weingut " + "Beispiel " * 10,
        kunde_oder_bestellnr="4711",
        datum=DATUM,
        settings=settings,
    )
    assert len(name) <= 70
    assert f"{TRENNER}Speditionsauftrag{TRENNER}" in name
    assert f"{TRENNER}4711{TRENNER}{DATUM_TEXT}.pdf" in name  # kurze Nummer bleibt unangetastet


# ---------------------------------------------------------------------------
# Verbotene Wörter (Auftragskopie)
# ---------------------------------------------------------------------------


def test_verbotene_woerter(settings: Settings):
    assert pruefe_auftragskopie_name("Weingut Beispiel – Muster – Kopie – 24.10.2026.pdf", settings) == []
    assert pruefe_auftragskopie_name("Auftragskopie-Beispiel_4711-24.10.2026.pdf", settings) == []
    assert pruefe_auftragskopie_name("Auftragsbestätigung-Beispiel.pdf", settings) == ["Auftragsbestätigung"]
    assert pruefe_auftragskopie_name("AUFTRAGSBESTAETIGUNG-Beispiel.pdf", settings) == ["Auftragsbestätigung"]
    assert pruefe_auftragskopie_name("Bitte bestätigen Sie.pdf", settings) == ["bestätigen"]
    assert pruefe_auftragskopie_name("Verbindliche Zusage.pdf", settings) == ["verbindlich"]
    assert pruefe_auftragskopie_name("Speditionsbestätigung – Muster.pdf", settings) == []  # "bestätigen" nicht enthalten
    assert pruefe_auftragskopie_name("", settings) == []


def test_verbotene_woerter_aus_settings_und_dedupliziert(settings: Settings):
    settings.kommunikation.verbotene_woerter_auftragskopie = ["Zusage", "ZUSAGE", "", "Garantie"]
    assert pruefe_auftragskopie_name("Zusage und Garantie.pdf", settings) == ["Zusage", "Garantie"]


def test_kopie_name_mit_verbotenem_wort_warnt(settings: Settings, caplog: pytest.LogCaptureFixture):
    with caplog.at_level(logging.WARNING, logger="auftragsautomat.benennung"):
        name = dateiname(
            "Kopie",
            firma=_firma(settings, "wik_leh"),
            lieferant="Weingut Beispiel",
            kunde="egal",
            bestellnummer="Auftragsbestätigung 4711",
            auftragsnummer=None,
            datum=DATUM,
            settings=settings,
        )
    assert pruefe_auftragskopie_name(name, settings) == ["Auftragsbestätigung"]
    assert any("verbotene Wörter" in r.getMessage() for r in caplog.records)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="auftragsautomat.benennung"):
        dateiname(
            "Cde",
            firma=_firma(settings, "wik_leh"),
            lieferant="Weingut Beispiel",
            kunde="egal",
            bestellnummer="Auftragsbestätigung 4711",
            auftragsnummer=None,
            datum=DATUM,
            settings=settings,
        )
    assert not caplog.records  # nur die Auftragskopie wird geprüft


def test_normalisiere():
    assert normalisiere("Auftragsbestätigung") == "auftragsbestaetigung"
    assert normalisiere("Château  Beispiel") == "chateau beispiel"
    assert normalisiere("STRASSE Straße") == "strasse strasse"
    assert normalisiere(None) == ""


# ---------------------------------------------------------------------------
# name_fuer_paket
# ---------------------------------------------------------------------------


def test_name_fuer_paket_mit_matchcode(settings: Settings):
    paket = _paket(
        "wik_fh_seh",
        kunde=_kunde(),
        lieferant=_lieferant(),
        kunde_text="Weinhandel Muster GmbH (laut PDF)",
        lieferant_text="Beispiel srl (laut PDF)",
        bestellnummer="4711",
        belegnummer="B-1",
    )
    assert kurzname_kunde(paket) == "Musterhandel"
    assert kurzname_lieferant(paket) == "Beispielgut"
    name = name_fuer_paket("Cde", paket, settings, datum=DATUM)
    assert name == f"Beispielgut{TRENNER}WIK{TRENNER}4711{TRENNER}Cde{TRENNER}{DATUM_TEXT}.pdf"


def test_name_fuer_paket_ws_mit_matchcode(settings: Settings):
    paket = _paket("weinstrasse_agentur", kunde=_kunde(), lieferant=_lieferant(), bestellnummer="4711")
    name = name_fuer_paket("Kopie", paket, settings, datum=DATUM)
    assert name == f"Beispielgut{TRENNER}Musterhandel{TRENNER}Kopie{TRENNER}{DATUM_TEXT}.pdf"


def test_name_fuer_paket_ws_spar_auftragsnummer_nur_kundenbestellnummer(settings: Settings):
    spar = _kunde(matchcode="Spar", name="SPAR Beispielmarkt")
    mit = _paket("weinstrasse_agentur", kunde=spar, lieferant=_lieferant(), bestellnummer="4711", belegnummer="B-1")
    assert name_fuer_paket("Kopie", mit, settings, datum=DATUM) == (
        f"Beispielgut{TRENNER}Spar{TRENNER}4711{TRENNER}Kopie{TRENNER}{DATUM_TEXT}.pdf"
    )
    ohne = _paket("weinstrasse_agentur", kunde=spar, lieferant=_lieferant(), bestellnummer=None, belegnummer="B-1")
    # Belegnummer des Dokuments ist keine Auftragsnummer: Name ohne Nummer
    assert name_fuer_paket("Kopie", ohne, settings, datum=DATUM) == (
        f"Beispielgut{TRENNER}Spar{TRENNER}Kopie{TRENNER}{DATUM_TEXT}.pdf"
    )


def test_name_fuer_paket_ohne_matchcode_name_dann_dokumenttext(settings: Settings):
    nur_name = _paket(
        "wik_leh",
        kunde=_kunde(matchcode=None),
        lieferant=_lieferant(matchcode="  "),
        kunde_text="Kunde laut PDF",
        lieferant_text="Lieferant laut PDF",
        bestellnummer="4711",
    )
    assert kurzname_kunde(nur_name) == "Weinhandel Muster GmbH"
    assert kurzname_lieferant(nur_name) == "Weingut Beispiel srl"
    assert name_fuer_paket("Cde", nur_name, settings, datum=DATUM).startswith(f"Weingut Beispiel srl{TRENNER}WIK")

    ohne_zuordnung = _paket(
        "weinstrasse_agentur", kunde_text=" Kunde laut PDF ", lieferant_text="Lieferant laut PDF", bestellnummer="4711"
    )
    assert kurzname_kunde(ohne_zuordnung) == "Kunde laut PDF"
    assert kurzname_lieferant(ohne_zuordnung) == "Lieferant laut PDF"
    assert name_fuer_paket("Cde", ohne_zuordnung, settings, datum=DATUM) == (
        f"Lieferant laut PDF{TRENNER}Kunde laut PDF{TRENNER}Cde{TRENNER}{DATUM_TEXT}.pdf"
    )

    leer = _paket("weinstrasse_agentur")
    assert kurzname_kunde(leer) == UNBEKANNT
    assert kurzname_lieferant(leer) == UNBEKANNT
    assert name_fuer_paket("Kd", leer, settings, datum=DATUM) == (
        f"{UNBEKANNT}{TRENNER}{UNBEKANNT}{TRENNER}Kd{TRENNER}{DATUM_TEXT}.pdf"
    )


def test_name_fuer_paket_wik_nummern_kette(settings: Settings):
    felder = dict(kunde=_kunde(), lieferant=_lieferant())
    kundenbestellnummer = _paket("wik_leh", bestellnummer="4711", belegnummer="B-1", **felder)
    assert f"{TRENNER}4711{TRENNER}" in name_fuer_paket("Cde", kundenbestellnummer, settings, datum=DATUM)
    belegnummer = _paket("wik_leh", bestellnummer=None, belegnummer="B-1", **felder)
    assert f"{TRENNER}B-1{TRENNER}" in name_fuer_paket("Cde", belegnummer, settings, datum=DATUM)
    keine = _paket("wik_leh", bestellnummer=None, belegnummer=None, **felder)
    assert f"{TRENNER}{OHNE_NUMMER}{TRENNER}" in name_fuer_paket("Cde", keine, settings, datum=DATUM)


def test_name_fuer_paket_revision(settings: Settings):
    paket = _paket("wik_leh", kunde=_kunde(), lieferant=_lieferant(), bestellnummer="4711", ist_revision=True)
    assert name_fuer_paket("Cde", paket, settings, datum=DATUM) == (
        f"Beispielgut{TRENNER}WIK{TRENNER}4711{TRENNER}Neu{TRENNER}{DATUM_TEXT}.pdf"
    )
    assert name_fuer_paket("Kopie", paket, settings, datum=DATUM) == (
        f"Beispielgut{TRENNER}WIK{TRENNER}4711{TRENNER}Neu Kopie{TRENNER}{DATUM_TEXT}.pdf"
    )
    assert f"{TRENNER}Ls{TRENNER}" in name_fuer_paket("Ls", paket, settings, datum=DATUM)  # andere Kürzel unverändert
    assert f"{TRENNER}Neu{TRENNER}" in name_fuer_paket("Neu", paket, settings, datum=DATUM)
    # Revision nur am Rohdokument markiert
    roh_revision = _paket("wik_leh", kunde=_kunde(), lieferant=_lieferant(), bestellnummer="4711", ist_revision=True)
    roh_revision.ist_revision = False
    roh_revision.roh.ist_revision = True
    assert f"{TRENNER}Neu{TRENNER}" in name_fuer_paket("Cde", roh_revision, settings, datum=DATUM)
    # keine Revision: Kürzel bleibt
    normal = _paket("wik_leh", kunde=_kunde(), lieferant=_lieferant(), bestellnummer="4711")
    assert f"{TRENNER}Cde{TRENNER}" in name_fuer_paket("Cde", normal, settings, datum=DATUM)


def test_name_fuer_paket_spedition(settings: Settings):
    felder = dict(kunde=_kunde(), lieferant=_lieferant(), belegfelder=Belegfelder(spedition="Spedition Beispiel"))
    wik = _paket("wik_leh", bestellnummer="4711", **felder)
    assert name_fuer_paket("Speditionsauftrag", wik, settings, datum=DATUM) == (
        f"Spedition Beispiel{TRENNER}Speditionsauftrag{TRENNER}Beispielgut{TRENNER}4711{TRENNER}{DATUM_TEXT}.pdf"
    )
    ws = _paket("weinstrasse_agentur", bestellnummer="4711", **felder)
    assert name_fuer_paket("Speditionsbestätigung", ws, settings, datum=DATUM) == (
        f"Spedition Beispiel{TRENNER}Speditionsbestätigung{TRENNER}Beispielgut{TRENNER}Musterhandel{TRENNER}{DATUM_TEXT}.pdf"
    )
    wik_ohne_nummer = _paket("wik_leh", **felder)
    assert f"{TRENNER}Musterhandel{TRENNER}" in name_fuer_paket("Speditionsauftrag", wik_ohne_nummer, settings, datum=DATUM)


def test_spedition_fuer_paket_standard_aus_firma(settings: Settings):
    ohne_feld = _paket("wik_leh")
    assert spedition_fuer_paket(ohne_feld, settings) == "Spedition Weinlogistik"  # tests/fixtures/config/firmen.yaml
    mit_feld = _paket("wik_leh", belegfelder=Belegfelder(spedition="Spedition Beispiel"))
    assert spedition_fuer_paket(mit_feld, settings) == "Spedition Beispiel"
    settings.firmen["wik_leh"].belegfelder_standard = {}
    assert spedition_fuer_paket(ohne_feld, settings) == UNBEKANNT
    assert f"{UNBEKANNT}{TRENNER}Speditionsauftrag" in name_fuer_paket("Speditionsauftrag", ohne_feld, settings, datum=DATUM)


def test_name_fuer_paket_datum_standard_heute(settings: Settings, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(benennung, "_heute", lambda: date(2026, 1, 2))
    paket = _paket("wik_leh", kunde=_kunde(), lieferant=_lieferant(), bestellnummer="4711")
    assert name_fuer_paket("Cde", paket, settings).endswith(f"{TRENNER}02.01.2026.pdf")
    assert name_fuer_paket("Cde", paket, settings, endung=".eml").endswith(".eml")


def test_name_fuer_paket_ohne_firma_oder_unbekannt(settings: Settings):
    with pytest.raises(ValueError, match="ohne Firma"):
        name_fuer_paket("Cde", _paket(None, bestellnummer="4711"), settings, datum=DATUM)
    with pytest.raises(KeyError, match="Unbekannte Firma"):
        name_fuer_paket("Cde", _paket("gibt_es_nicht", bestellnummer="4711"), settings, datum=DATUM)
    with pytest.raises(ValueError, match="Dokumentkürzel"):
        name_fuer_paket("AB", _paket("wik_leh", bestellnummer="4711"), settings, datum=DATUM)
