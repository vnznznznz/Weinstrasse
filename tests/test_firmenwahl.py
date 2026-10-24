"""Tests für matching.firmenwahl (Firmenkatalog, Firmengedaechtnis, merke_firma) und die neuen Stufen
Gedächtnis/Katalog in matching.firma, die FIRMA_UNKLAR-Details in matching.pruefung und das
Durchreichen (kundengruppe, firma_merken) in matching.pipeline.

Alle Namen sind synthetisch (tests/fixtures/config/firmenkatalog.yaml, "Frau Beispiel", example.org).
"""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from auftragsautomat.config import Settings
from auftragsautomat.matching.aliase import AliasTabellen
from auftragsautomat.matching.firma import (
    QUELLE_ALIAS_KUNDE,
    QUELLE_EINGANG_ORDNER,
    QUELLE_GEDAECHTNIS,
    QUELLE_KATALOG,
    QUELLE_STAMMDATEN,
    FirmaErgebnis,
    ermittle_firma,
)
from auftragsautomat.matching.firmenwahl import (
    Firmengedaechtnis,
    Firmenkatalog,
    Klassifikation,
    merke_firma,
)
from auftragsautomat.matching.pipeline import erstelle_belegpaket, setze_firma
from auftragsautomat.matching.pruefung import pruefe
from auftragsautomat.models import (
    Belegpaket,
    Eingangstyp,
    Extraktionsmethode,
    Firmenregel,
    Kunde,
    Kundengruppe,
    KundeZuordnung,
    Lieferant,
    LieferantZuordnung,
    MatchStatus,
    Pruefcode,
    Pruefhinweis,
    RohBestellung,
    RohPosition,
    Schweregrad,
)
from auftragsautomat.stammdaten.basis import Stammdaten

REGION_KUNDE = "Einkaufsregion Minden-Hannover"


@pytest.fixture()
def aliase(settings: Settings) -> AliasTabellen:
    return AliasTabellen.lade(settings.alias_ordner)


@pytest.fixture()
def katalog(settings: Settings) -> Firmenkatalog:
    return Firmenkatalog.aus_settings(settings)


@pytest.fixture()
def gedaechtnis_datei(settings: Settings, tmp_path: Path) -> Path:
    """Gedächtnisdatei je Test in tmp_path (nie in tests/fixtures schreiben)."""
    settings.firmenwahl.gedaechtnis_datei = tmp_path / "gedaechtnis" / "firmen_gedaechtnis.yaml"
    return settings.firmenwahl.gedaechtnis_datei


def _roh(settings: Settings, datei: str | Path = "bestellung.pdf", **felder) -> RohBestellung:
    quelldatei = Path(datei) if Path(datei).is_absolute() else settings.eingang_ordner / datei
    felder.setdefault("eingangstyp", Eingangstyp.KUNDENBESTELLUNG_PDF)
    felder.setdefault("extraktionsmethode", Extraktionsmethode.PDFPLUMBER)
    return RohBestellung(quelldatei=quelldatei, **felder)


def _kein_lader(settings: Settings) -> dict[str, Stammdaten]:
    raise AssertionError("Stammdaten dürfen in diesem Fall nicht geladen werden")


def _regel(kunde: str, lieferant: str | None, firma: str, **felder) -> Firmenregel:
    felder.setdefault("gelernt_am", datetime(2026, 10, 20, 9, 30))
    felder.setdefault("gelernt_von", "Frau Beispiel")
    return Firmenregel(kunde_kurz=kunde, lieferant_kurz=lieferant, firma_id=firma, **felder)


def _hinweise(ergebnis: FirmaErgebnis | Belegpaket, code: Pruefcode) -> list[Pruefhinweis]:
    hinweise = ergebnis.alle_hinweise if isinstance(ergebnis, Belegpaket) else ergebnis.hinweise
    return [h for h in hinweise if h.code == code]


# ---------------------------------------------------------------------------
# Firmenkatalog: laden
# ---------------------------------------------------------------------------


def test_katalog_fehlt_leer_oder_kaputt(tmp_path: Path, caplog):
    caplog.set_level(logging.WARNING)
    katalog = Firmenkatalog.lade(tmp_path / "gibtsnicht.yaml")
    assert katalog.leer and katalog.gruppen == [] and katalog.lieferanten == {}
    assert "fehlt" in caplog.text

    leer = tmp_path / "leer.yaml"
    leer.write_text("", encoding="utf-8")
    assert Firmenkatalog.lade(leer).leer

    kaputt = tmp_path / "kaputt.yaml"
    kaputt.write_text("kundengruppen: [unvollständig", encoding="utf-8")
    assert Firmenkatalog.lade(kaputt).leer
    assert "nicht lesbar" in caplog.text

    liste = tmp_path / "liste.yaml"
    liste.write_text("- a\n- b\n", encoding="utf-8")
    assert Firmenkatalog.lade(liste).leer

    ergebnis = Firmenkatalog.lade(tmp_path / "gibtsnicht.yaml").klassifiziere("Weinhandlung Beispiel")
    assert (ergebnis.kundengruppe, ergebnis.entscheidung, ergebnis.firmen) == (Kundengruppe.UNBEKANNT, "unbekannt", [])


def test_katalog_fixture_geladen(katalog: Firmenkatalog):
    assert [g.gruppe for g in katalog.gruppen] == [
        Kundengruppe.FACHHANDEL,
        Kundengruppe.EIGENSTAENDIGER_LEH,
        Kundengruppe.REGION,
        Kundengruppe.KETTE,
        Kundengruppe.GROSSKUNDE,
    ]
    fachhandel = katalog.gruppe(Kundengruppe.FACHHANDEL)
    assert fachhandel is not None
    assert (fachhandel.firmen, fachhandel.entscheidung) == (("weinstrasse_agentur", "wik_fh_seh"), "lieferant")
    region = katalog.gruppe("region")
    assert region is not None and region.firmen == ("wik_leh", "woe_leh") and region.entscheidung == "fragen"
    assert katalog.gruppe(Kundengruppe.GROSSKUNDE).kundennummern == ("99001",)
    assert katalog.lieferanten == {"Weingut Beispiel": "wik_fh_seh", "Cantina Esempio": "weinstrasse_agentur"}
    assert katalog.gruppe("unbekannt") is None


def test_katalog_rueckfall_firma_kundengruppen_und_validierung(settings: Settings, tmp_path: Path, caplog):
    caplog.set_level(logging.WARNING)
    datei = tmp_path / "katalog.yaml"
    datei.write_text(
        yaml.safe_dump(
            {
                "kundengruppen": {
                    "region": {"muster": ["Region"]},  # keine firmen -> aus firmen.yaml (kundengruppen)
                    "kette": {"firmen": ["wik_leh", "gibtsnicht"], "muster": ["Kette"]},
                    "eigenstaendiger_leh": {"firmen": ["wik_fh_seh", "woe_leh"], "entscheidung": "eindeutig", "muster": ["e.K."]},
                    "fachhandel": {"firmen": ["wik_fh_seh"], "entscheidung": "unsinn", "muster": ["Vinothek"]},
                    "hotellerie": {"firmen": ["wik_fh_seh"], "muster": ["Hotel"]},
                    "grosskunde": {"firmen": [], "muster": ["Großkunde"]},
                },
                "lieferanten": {"Weingut Beispiel": "wik_fh_seh", "Domaine Exemple": "gibtsnicht", "": "wik_leh"},
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    for firma_id in ("wik_leh", "woe_leh"):
        settings.firma(firma_id).kundengruppen = ["region", "kette"]  # grosskunde absichtlich nirgends
    katalog = Firmenkatalog.lade(datei, settings)

    region = katalog.gruppe(Kundengruppe.REGION)
    assert region.firmen == ("wik_leh", "woe_leh") and region.entscheidung == "fragen"
    kette = katalog.gruppe(Kundengruppe.KETTE)
    assert kette.firmen == ("wik_leh",) and kette.entscheidung == "eindeutig"
    assert "gibtsnicht" in caplog.text
    eigen = katalog.gruppe(Kundengruppe.EIGENSTAENDIGER_LEH)
    assert eigen.entscheidung == "fragen"  # "eindeutig" mit zwei Firmen -> fragen
    fach = katalog.gruppe(Kundengruppe.FACHHANDEL)
    assert fach.entscheidung == "eindeutig"  # unbekannte entscheidung -> Standard bei einer Firma
    assert katalog.gruppe("hotellerie") is None and "unbekannte Kundengruppe" in caplog.text
    gross = katalog.gruppe(Kundengruppe.GROSSKUNDE)
    assert gross.firmen == () and "keine (bekannte) Firma" in caplog.text
    assert katalog.lieferanten == {"Weingut Beispiel": "wik_fh_seh"}

    ergebnis = katalog.klassifiziere("Großkunde Beispiel")
    assert (ergebnis.kundengruppe, ergebnis.entscheidung, ergebnis.firmen) == (Kundengruppe.GROSSKUNDE, "unbekannt", [])


# ---------------------------------------------------------------------------
# Firmenkatalog: klassifizieren
# ---------------------------------------------------------------------------


def test_klassifiziere_region_kette_grosskunde(katalog: Firmenkatalog):
    region = katalog.klassifiziere(REGION_KUNDE)
    assert isinstance(region, Klassifikation)
    assert (region.kundengruppe, region.firmen, region.entscheidung) == (
        Kundengruppe.REGION,
        ["wik_leh", "woe_leh"],
        "fragen",
    )
    assert "Minden-Hannover" in region.grund and region.firma is None

    kette = katalog.klassifiziere("Supermarktkette Beispiel GmbH, Zentraleinkauf")
    assert (kette.kundengruppe, kette.entscheidung) == (Kundengruppe.KETTE, "fragen")
    assert "Supermarktkette" in kette.grund  # längstes Muster gewinnt
    assert "Zentraleinkauf" in kette.grund  # weitere Treffer werden genannt

    gross = katalog.klassifiziere("Beispiel Lebensmittel", kundennummer="99001")
    assert (gross.kundengruppe, gross.entscheidung, gross.firmen) == (
        Kundengruppe.GROSSKUNDE,
        "fragen",
        ["wik_leh", "woe_leh"],
    )
    assert "Kundennummer 99001" in gross.grund


def test_klassifiziere_eigenstaendiger_leh(katalog: Firmenkatalog):
    for text in ("Frischemarkt Beispiel e.K.", "Beispiel Markt GmbH", "Kaufmann Beispiel oHG"):
        ergebnis = katalog.klassifiziere(text)
        assert (ergebnis.kundengruppe, ergebnis.firmen, ergebnis.entscheidung) == (
            Kundengruppe.EIGENSTAENDIGER_LEH,
            ["wik_fh_seh"],
            "eindeutig",
        ), text
        assert ergebnis.firma == "wik_fh_seh"
    # "Markt GmbH" trifft nicht "Weinmarkt": Muster müssen ganze Wörter sein
    assert katalog.klassifiziere("Weinmarkt Beispiel GmbH").entscheidung == "unbekannt"


def test_klassifiziere_fachhandel_mit_und_ohne_lieferant(katalog: Firmenkatalog):
    ohne = katalog.klassifiziere("Vinothek Beispiel")
    assert (ohne.kundengruppe, ohne.entscheidung, ohne.firmen) == (
        Kundengruppe.FACHHANDEL,
        "lieferant",
        ["weinstrasse_agentur", "wik_fh_seh"],
    )

    wik = katalog.klassifiziere("Vinothek Beispiel", lieferant_text="Weingut Beispiel GmbH & Co. KG")
    assert (wik.entscheidung, wik.firmen) == ("eindeutig", ["wik_fh_seh"])
    assert "Weingut Beispiel" in wik.grund

    ws = katalog.klassifiziere("Weinhandlung Beispiel", lieferant_text="Cantina Esempio s.r.l.")
    assert (ws.entscheidung, ws.firmen) == ("eindeutig", ["weinstrasse_agentur"])

    fremd = katalog.klassifiziere("Weinhandlung Beispiel", lieferant_text="Domaine Inconnue")
    assert (fremd.entscheidung, fremd.firmen) == ("fragen", ["weinstrasse_agentur", "wik_fh_seh"])
    assert "nicht im Abschnitt 'lieferanten'" in fremd.grund


def test_klassifiziere_unbekannt_adresse_und_laengstes_muster(katalog: Firmenkatalog):
    unbekannt = katalog.klassifiziere("Hotel Beispiel", adresse="Seestraße 1\n23570 Lübeck")
    assert (unbekannt.kundengruppe, unbekannt.entscheidung, unbekannt.firmen) == (Kundengruppe.UNBEKANNT, "unbekannt", [])
    assert "Hotel Beispiel" in unbekannt.grund
    assert katalog.klassifiziere(None).entscheidung == "unbekannt"
    assert katalog.klassifiziere("").entscheidung == "unbekannt"

    adresse = katalog.klassifiziere(None, adresse="Weinhandlung Beispiel\nMusterweg 2\n12345 Musterstadt")
    assert adresse.kundengruppe == Kundengruppe.FACHHANDEL

    # Fachhandel ("Weinhandlung") schlägt eigenständigen LEH ("e.K.") – längstes Muster
    beides = katalog.klassifiziere("Weinhandlung Beispiel e.K.")
    assert beides.kundengruppe == Kundengruppe.FACHHANDEL
    assert "eigenstaendiger_leh" in beides.grund


def test_klassifiziere_firma_kandidaten(katalog: Firmenkatalog):
    eingeschraenkt = katalog.klassifiziere(REGION_KUNDE, firma_kandidaten=["woe_leh", "wik_fh_seh"])
    assert (eingeschraenkt.entscheidung, eingeschraenkt.firmen) == ("eindeutig", ["woe_leh"])
    assert "eingeschränkt auf woe_leh" in eingeschraenkt.grund
    fremd = katalog.klassifiziere(REGION_KUNDE, firma_kandidaten=["wik_fh_seh"])
    assert (fremd.entscheidung, fremd.firmen) == ("fragen", ["wik_leh", "woe_leh"])


def test_firma_fuer_lieferant(katalog: Firmenkatalog):
    assert katalog.firma_fuer_lieferant("Weingut Beispiel") == "wik_fh_seh"
    assert katalog.firma_fuer_lieferant("WEINGUT BEISPIEL GmbH") == "wik_fh_seh"
    assert katalog.firma_fuer_lieferant("Bestellung an Weingut Beispiel, Pfalz") == "wik_fh_seh"
    assert katalog.firma_fuer_lieferant("Cantina Esempio") == "weinstrasse_agentur"
    assert katalog.firma_fuer_lieferant("Weingut") is None  # nur ein Teil des Namens
    assert katalog.firma_fuer_lieferant("Domaine Inconnue") is None
    assert katalog.firma_fuer_lieferant(None) is None and katalog.firma_fuer_lieferant("") is None


# ---------------------------------------------------------------------------
# Firmengedaechtnis
# ---------------------------------------------------------------------------


def test_gedaechtnis_fehlende_datei(tmp_path: Path):
    gedaechtnis = Firmengedaechtnis.lade(tmp_path / "gibtsnicht.yaml")
    assert len(gedaechtnis) == 0 and gedaechtnis.regeln == []
    assert gedaechtnis.finde("Irgendwer", "Irgendwas") is None
    assert gedaechtnis.pfad == tmp_path / "gibtsnicht.yaml"


def test_gedaechtnis_merke_und_finde():
    gedaechtnis = Firmengedaechtnis()
    gedaechtnis.merke(_regel("Region Beispiel", None, "woe_leh"))
    gedaechtnis.merke(_regel("Region Beispiel", "Weingut Beispiel", "wik_leh"))

    # Kunde + Lieferant vor Kunde allgemein
    assert gedaechtnis.finde("Region Beispiel", "Weingut Beispiel").firma_id == "wik_leh"
    assert gedaechtnis.finde("region beispiel", "Weingut Beispiel GmbH").firma_id == "wik_leh"  # normalisiert
    assert gedaechtnis.finde("Region Beispiel", "Anderes Weingut").firma_id == "woe_leh"
    assert gedaechtnis.finde("REGION-BEISPIEL", None).firma_id == "woe_leh"
    assert gedaechtnis.finde("Unbekannt", "Weingut Beispiel") is None
    assert gedaechtnis.finde(None, "Weingut Beispiel") is None

    # gleicher Schlüssel (normalisiert) wird ersetzt
    gedaechtnis.merke(_regel("REGION BEISPIEL", "", "wik_leh", gelernt_von="Herr Beispiel"))
    assert len(gedaechtnis) == 2
    assert gedaechtnis.finde("Region Beispiel").firma_id == "wik_leh"
    assert gedaechtnis.finde("Region Beispiel").gelernt_von == "Herr Beispiel"
    assert [(r.kunde_kurz, r.lieferant_kurz) for r in gedaechtnis.regeln] == [
        ("REGION BEISPIEL", None),
        ("Region Beispiel", "Weingut Beispiel"),
    ]
    assert gedaechtnis.entferne("region beispiel", "weingut beispiel") is True
    assert gedaechtnis.entferne("region beispiel", "weingut beispiel") is False
    assert len(gedaechtnis) == 1
    with pytest.raises(ValueError):
        gedaechtnis.merke(Firmenregel(kunde_kurz="  ", firma_id="wik_leh"))


def test_gedaechtnis_tolerant_kurzname_im_dokumenttext():
    gedaechtnis = Firmengedaechtnis([_regel("MENRATH", None, "wik_leh"), _regel("AB", None, "woe_leh")])
    assert gedaechtnis.finde("Weinhaus Menrath").firma_id == "wik_leh"
    assert gedaechtnis.finde("Menrath Weinhaus GmbH").firma_id == "wik_leh"
    assert gedaechtnis.finde("Weinhaus Menrathstraße") is None  # kein ganzes Wort
    assert gedaechtnis.finde("AB Beispiel") is None  # zu kurz für die tolerante Suche
    assert gedaechtnis.finde("AB").firma_id == "woe_leh"  # exakt gilt immer

    gedaechtnis.merke(_regel("MENRATH", "CIDE", "woe_leh"))
    assert gedaechtnis.finde("Weinhaus Menrath", "Cide srl").firma_id == "woe_leh"
    assert gedaechtnis.finde("Weinhaus Menrath", "Bollina").firma_id == "wik_leh"
    assert gedaechtnis.finde_fuer_texte(
        ["Weinhaus Menrath", "Marktplatz 3"], ["Villa Armellina", "Cide"]
    ).firma_id == "woe_leh"
    assert gedaechtnis.finde_fuer_texte(["Weinhaus Menrath"], []).firma_id == "wik_leh"
    assert gedaechtnis.finde_fuer_texte([None, ""], ["Cide"]) is None


def test_gedaechtnis_speichern_atomar_und_laden(tmp_path: Path):
    datei = tmp_path / "unterordner" / "firmen_gedaechtnis.yaml"
    gedaechtnis = Firmengedaechtnis(pfad=datei)
    gedaechtnis.merke(_regel("Zweiter Kunde", None, "woe_leh", kundengruppe=Kundengruppe.REGION))
    gedaechtnis.merke(_regel("Erster Kunde", "Weingut Beispiel", "wik_leh", bemerkung="Test"))
    gedaechtnis.merke(_regel("Erster Kunde", None, "wik_fh_seh", gelernt_am=None, gelernt_von=None))

    ziel = gedaechtnis.speichere()
    assert ziel == datei and datei.exists()
    assert [p.name for p in datei.parent.iterdir()] == [datei.name]  # keine temporäre Datei übrig

    daten = yaml.safe_load(datei.read_text(encoding="utf-8"))
    assert isinstance(daten, list) and len(daten) == 3
    assert [(d["kunde_kurz"], d.get("lieferant_kurz")) for d in daten] == [
        ("Erster Kunde", None),
        ("Erster Kunde", "Weingut Beispiel"),
        ("Zweiter Kunde", None),
    ]
    assert daten[1]["firma_id"] == "wik_leh" and daten[1]["gelernt_am"] == "2026-10-20T09:30:00"
    assert daten[2]["kundengruppe"] == "region" and "gelernt_am" not in daten[0]
    assert datei.read_text(encoding="utf-8").startswith("# Firmen-Gedächtnis")

    geladen = Firmengedaechtnis.lade(datei)
    assert len(geladen) == 3
    assert geladen.finde("Erster Kunde", "Weingut Beispiel").firma_id == "wik_leh"
    assert geladen.finde("Erster Kunde").firma_id == "wik_fh_seh"
    regel = geladen.finde("Zweiter Kunde")
    assert (regel.firma_id, regel.kundengruppe, regel.gelernt_am, regel.gelernt_von, regel.quelle) == (
        "woe_leh",
        Kundengruppe.REGION,
        datetime(2026, 10, 20, 9, 30),
        "Frau Beispiel",
        "freigabe",
    )

    # zweites Speichern ersetzt die Datei vollständig
    geladen.entferne("Zweiter Kunde")
    geladen.speichere()
    assert len(yaml.safe_load(datei.read_text(encoding="utf-8"))) == 2

    with pytest.raises(ValueError):
        Firmengedaechtnis().speichere()


def test_gedaechtnis_ungueltige_eintraege(tmp_path: Path, caplog):
    caplog.set_level(logging.WARNING)
    datei = tmp_path / "g.yaml"
    datei.write_text(
        "- kunde_kurz: Guter Kunde\n  firma_id: wik_leh\n"
        "- kunde_kurz: Ohne Firma\n"
        "- nur ein Text\n"
        "- kunde_kurz: ''\n  firma_id: woe_leh\n",
        encoding="utf-8",
    )
    gedaechtnis = Firmengedaechtnis.lade(datei)
    assert [r.kunde_kurz for r in gedaechtnis.regeln] == ["Guter Kunde"]
    assert caplog.text.count("übersprungen") == 3

    datei.write_text("kaputt: [", encoding="utf-8")
    assert len(Firmengedaechtnis.lade(datei)) == 0
    datei.write_text("regeln:\n  - kunde_kurz: Im Mapping\n    firma_id: wik_leh\n", encoding="utf-8")
    assert Firmengedaechtnis.lade(datei).finde("Im Mapping").firma_id == "wik_leh"


# ---------------------------------------------------------------------------
# merke_firma
# ---------------------------------------------------------------------------


def _paket_mit_zuordnung(settings: Settings, firma_id: str | None = "wik_leh", **felder) -> Belegpaket:
    roh = _roh(settings, kunde_text="Weinmarkt Beispiel GmbH", lieferant_text="Cide srl")
    felder.setdefault(
        "kunde",
        KundeZuordnung(
            kunde=Kunde(firma_id="wik_leh", kundennummer="3441", name="Hegmann", matchcode="HEGMANN"),
            status=MatchStatus.SICHER,
        ),
    )
    felder.setdefault(
        "lieferant",
        LieferantZuordnung(
            lieferant=Lieferant(firma_id="wik_leh", lieferantennummer="72", name="Cide srl", matchcode="CIDE"),
            status=MatchStatus.SICHER,
        ),
    )
    return Belegpaket(roh=roh, firma_id=firma_id, firma_quelle="manuell", **felder)


def test_merke_firma_nur_mit_haekchen_und_einstellung(settings: Settings, gedaechtnis_datei: Path):
    assert merke_firma(_paket_mit_zuordnung(settings), settings) is None  # firma_merken aus
    assert merke_firma(_paket_mit_zuordnung(settings, firma_id=None, firma_merken=True), settings) is None
    settings.firmenwahl.merken = False
    assert merke_firma(_paket_mit_zuordnung(settings, firma_merken=True), settings) is None
    settings.firmenwahl.merken = True
    assert merke_firma(_paket_mit_zuordnung(settings, firma_id="gibtsnicht", firma_merken=True), settings) is None
    assert not gedaechtnis_datei.exists()


def test_merke_firma_schreibt_regel(settings: Settings, gedaechtnis_datei: Path):
    paket = _paket_mit_zuordnung(settings, firma_merken=True, kundengruppe=Kundengruppe.REGION)
    jetzt = datetime(2026, 10, 24, 14, 5, 59, 123456)
    regel = merke_firma(paket, settings, benutzer="Frau Beispiel", jetzt=jetzt)
    assert regel is not None
    assert (regel.kunde_kurz, regel.lieferant_kurz, regel.firma_id) == ("HEGMANN", "CIDE", "wik_leh")
    assert (regel.kundengruppe, regel.gelernt_am, regel.gelernt_von, regel.quelle) == (
        Kundengruppe.REGION,
        datetime(2026, 10, 24, 14, 5, 59),
        "Frau Beispiel",
        "freigabe",
    )
    assert regel.bemerkung == "Kunde laut Dokument: Weinmarkt Beispiel GmbH"
    assert gedaechtnis_datei.exists()
    geladen = Firmengedaechtnis.lade(gedaechtnis_datei)
    assert geladen.finde("HEGMANN", "CIDE").firma_id == "wik_leh"
    assert geladen.finde("Hegmann", "Bollina") is None  # nur für diesen Lieferanten gelernt

    # zweites Merken mit anderer Firma ersetzt die Regel, jetzt() als Funktion
    paket.firma_id = "woe_leh"
    regel2 = merke_firma(paket, settings, jetzt=lambda: datetime(2026, 10, 25, 8, 0))
    assert regel2.firma_id == "woe_leh" and regel2.gelernt_von is None
    assert regel2.gelernt_am == datetime(2026, 10, 25, 8, 0)
    geladen = Firmengedaechtnis.lade(gedaechtnis_datei)
    assert len(geladen) == 1 and geladen.finde("HEGMANN", "CIDE").firma_id == "woe_leh"


def test_merke_firma_ohne_zuordnung_nimmt_dokumenttext(settings: Settings, gedaechtnis_datei: Path):
    roh = _roh(settings, kunde_text=REGION_KUNDE)
    paket = Belegpaket(roh=roh, firma_id="woe_leh", firma_merken=True)
    regel = merke_firma(paket, settings)
    assert (regel.kunde_kurz, regel.lieferant_kurz, regel.bemerkung) == (REGION_KUNDE, None, None)
    assert regel.gelernt_am is not None

    # Name statt Matchcode; Lieferant aus dem Dokument
    paket.kunde = KundeZuordnung(kunde=Kunde(firma_id="woe_leh", kundennummer="8101", name="Region Beispiel"))
    paket.roh.lieferant_text = "Weingut Beispiel"
    regel = merke_firma(paket, settings)
    assert (regel.kunde_kurz, regel.lieferant_kurz) == ("Region Beispiel", "Weingut Beispiel")

    ohne_kunde = Belegpaket(roh=_roh(settings), firma_id="woe_leh", firma_merken=True)
    assert merke_firma(ohne_kunde, settings) is None


# ---------------------------------------------------------------------------
# ermittle_firma: Reihenfolge Ordner -> Gedächtnis -> Alias -> Katalog -> Stammdaten
# ---------------------------------------------------------------------------


def test_ordner_vor_gedaechtnis(settings: Settings, aliase: AliasTabellen):
    gedaechtnis = Firmengedaechtnis([_regel("Weinmarkt Kirchhellen", None, "woe_leh")])
    roh = _roh(settings, settings.eingang_ordner / "wik_fh_seh" / "x.pdf", kunde_text="Weinmarkt Kirchhellen")
    ergebnis = ermittle_firma(roh, settings, aliase, stammdaten_lader=_kein_lader, gedaechtnis=gedaechtnis)
    assert (ergebnis.firma_id, ergebnis.quelle) == ("wik_fh_seh", QUELLE_EINGANG_ORDNER)
    assert not _hinweise(ergebnis, Pruefcode.FIRMA_GELERNT)


def test_gedaechtnis_vor_alias_und_katalog(settings: Settings, aliase: AliasTabellen):
    # Kette: wiedererkannt über den gelernten Namen laut Dokument, nicht tolerant über den Matchcode (Befund 4)
    gedaechtnis = Firmengedaechtnis(
        [_regel("HEGMANN", None, "woe_leh", kundengruppe=Kundengruppe.KETTE, kunde_texte=["Weinmarkt Kirchhellen"])]
    )
    roh = _roh(settings, kunde_text="Weinmarkt Kirchhellen GmbH & Co. KG")  # Alias sagt wik_leh
    ergebnis = ermittle_firma(roh, settings, aliase, stammdaten_lader=_kein_lader, gedaechtnis=gedaechtnis)
    assert (ergebnis.firma_id, ergebnis.quelle) == ("woe_leh", QUELLE_GEDAECHTNIS)
    assert ergebnis.kundengruppe == Kundengruppe.KETTE
    assert ergebnis.kandidaten == ("woe_leh",)
    gelernt = _hinweise(ergebnis, Pruefcode.FIRMA_GELERNT)
    assert len(gelernt) == 1 and gelernt[0].schweregrad == Schweregrad.INFO
    assert gelernt[0].details == {
        "firma_id": "woe_leh",
        "gelernt_am": "20.10.2026 09:30",
        "gelernt_von": "Frau Beispiel",
        "kunde": "HEGMANN",
        "lieferant": "",
        "kundengruppe": "kette",
        "quelle": "freigabe",
    }
    assert "gelernt am 20.10.2026 09:30 von Frau Beispiel" in gelernt[0].text
    firma_id, quelle = ergebnis  # Tupel-Vertrag bleibt
    assert (firma_id, quelle) == ("woe_leh", QUELLE_GEDAECHTNIS)

    # ohne Regel: Alias wie bisher
    ergebnis = ermittle_firma(roh, settings, aliase, stammdaten_lader=_kein_lader, gedaechtnis=Firmengedaechtnis())
    assert (ergebnis.firma_id, ergebnis.quelle) == ("wik_leh", QUELLE_ALIAS_KUNDE)
    assert ergebnis.kundengruppe is None


def test_gedaechtnis_kunde_plus_lieferant_vor_kunde_allgemein(settings: Settings, aliase: AliasTabellen):
    gedaechtnis = Firmengedaechtnis(
        [_regel(REGION_KUNDE, None, "woe_leh"), _regel(REGION_KUNDE, "CIDE", "wik_leh")]
    )
    # Lieferant über die Produktmarke der Positionen (Villa Armellina -> Cide)
    roh = _roh(
        settings,
        kunde_text=REGION_KUNDE,
        positionen=[RohPosition(laufnr=1, bezeichnung="Villa Armellina Gloria Secco", menge=Decimal(6))],
    )
    ergebnis = ermittle_firma(roh, settings, aliase, stammdaten_lader=_kein_lader, gedaechtnis=gedaechtnis)
    assert (ergebnis.firma_id, ergebnis.quelle) == ("wik_leh", QUELLE_GEDAECHTNIS)
    assert ergebnis.kundengruppe == Kundengruppe.REGION  # aus dem Katalog, die Regel hat keine

    roh = _roh(settings, kunde_text=REGION_KUNDE, lieferant_text="Bollina srl")
    ergebnis = ermittle_firma(roh, settings, aliase, stammdaten_lader=_kein_lader, gedaechtnis=gedaechtnis)
    assert (ergebnis.firma_id, ergebnis.quelle) == ("woe_leh", QUELLE_GEDAECHTNIS)

    gedaechtnis = Firmengedaechtnis([_regel(REGION_KUNDE, None, "gibtsnicht")])
    ergebnis = ermittle_firma(roh, settings, aliase, stammdaten_lader=_kein_lader, gedaechtnis=gedaechtnis)
    assert ergebnis.firma_id is None  # unbekannte Firma in der Regel wird ignoriert -> Katalog fragt


def test_katalog_eindeutig_und_fachhandel_lieferant(settings: Settings, aliase: AliasTabellen):
    roh = _roh(settings, kunde_text="Frischemarkt Beispiel e.K.", kunde_adresse="Musterweg 1\n12345 Musterstadt")
    ergebnis = ermittle_firma(roh, settings, aliase, stammdaten_lader=_kein_lader)
    assert (ergebnis.firma_id, ergebnis.quelle) == ("wik_fh_seh", QUELLE_KATALOG)
    assert ergebnis.kundengruppe == Kundengruppe.EIGENSTAENDIGER_LEH
    assert ergebnis.hinweise == [] and ergebnis.kandidaten == ("wik_fh_seh",)

    roh = _roh(settings, kunde_text="Vinothek Beispiel", lieferant_text="Cantina Esempio s.r.l.")
    ergebnis = ermittle_firma(roh, settings, aliase, stammdaten_lader=_kein_lader)
    assert (ergebnis.firma_id, ergebnis.quelle) == ("weinstrasse_agentur", QUELLE_KATALOG)
    assert ergebnis.kundengruppe == Kundengruppe.FACHHANDEL

    # Fachhandel ohne bekannten Lieferanten -> fragen zwischen Agentur und WIK FH & SEH
    roh = _roh(settings, kunde_text="Vinothek Beispiel", lieferant_text="Domaine Inconnue")
    ergebnis = ermittle_firma(roh, settings, aliase, stammdaten_lader=_kein_lader)
    assert (ergebnis.firma_id, ergebnis.quelle) == (None, None)
    assert ergebnis.kandidaten == ("weinstrasse_agentur", "wik_fh_seh")
    assert ergebnis.hinweise[0].details["kandidaten"] == "weinstrasse_agentur, wik_fh_seh"


def test_katalog_fragen_wenn_unklar_an(settings: Settings, aliase: AliasTabellen):
    roh = _roh(settings, kunde_text=REGION_KUNDE, kunde_adresse="Lagerstraße 9\n31675 Bückeburg")
    ergebnis = ermittle_firma(roh, settings, aliase, stammdaten_lader=_kein_lader)  # keine Stammdaten-Stufe
    assert (ergebnis.firma_id, ergebnis.quelle) == (None, None)
    assert ergebnis.kundengruppe == Kundengruppe.REGION
    assert ergebnis.kandidaten == ("wik_leh", "woe_leh")
    assert len(ergebnis.hinweise) == 1
    hinweis = ergebnis.hinweise[0]
    assert (hinweis.code, hinweis.schweregrad) == (Pruefcode.FIRMA_UNKLAR, Schweregrad.WARNUNG)
    assert hinweis.text == "Bitte Firma wählen (Kundengruppe region): wik_leh oder woe_leh"
    assert hinweis.details["kandidaten"] == "wik_leh, woe_leh"
    assert hinweis.details["kundengruppe"] == "region"
    assert "Minden-Hannover" in hinweis.details["grund"]


def test_katalog_fragen_wenn_unklar_aus(settings: Settings, aliase: AliasTabellen):
    settings.firmenwahl.fragen_wenn_unklar = False

    def lader(s: Settings) -> dict[str, Stammdaten]:
        return {"woe_leh": Stammdaten("woe_leh", kunden=[Kunde(firma_id="woe_leh", kundennummer="8102", name=REGION_KUNDE)])}

    roh = _roh(settings, kunde_text=REGION_KUNDE)
    ergebnis = ermittle_firma(roh, settings, aliase, stammdaten_lader=lader)
    assert (ergebnis.firma_id, ergebnis.quelle) == ("woe_leh", QUELLE_STAMMDATEN)
    assert ergebnis.kundengruppe == Kundengruppe.REGION
    assert not _hinweise(ergebnis, Pruefcode.FIRMA_UNKLAR)
    assert ergebnis.kandidaten == ("wik_leh", "woe_leh")

    ergebnis = ermittle_firma(roh, settings, aliase, stammdaten_lader=lambda s: {})
    assert (ergebnis.firma_id, ergebnis.quelle) == (None, None)
    assert ergebnis.kundengruppe == Kundengruppe.REGION and ergebnis.hinweise == []


def test_gedaechtnis_nach_katalog(settings: Settings, aliase: AliasTabellen):
    settings.firmenwahl.gedaechtnis_vor_katalog = False
    gedaechtnis = Firmengedaechtnis(
        [_regel("HEGMANN", None, "woe_leh"), _regel(REGION_KUNDE, None, "woe_leh")]
    )
    # Alias gewinnt jetzt gegen das Gedächtnis
    roh = _roh(settings, kunde_text="Weinmarkt Kirchhellen")
    ergebnis = ermittle_firma(roh, settings, aliase, stammdaten_lader=_kein_lader, gedaechtnis=gedaechtnis)
    assert (ergebnis.firma_id, ergebnis.quelle) == ("wik_leh", QUELLE_ALIAS_KUNDE)
    # Katalog müsste fragen -> Gedächtnis antwortet
    roh = _roh(settings, kunde_text=REGION_KUNDE)
    ergebnis = ermittle_firma(roh, settings, aliase, stammdaten_lader=_kein_lader, gedaechtnis=gedaechtnis)
    assert (ergebnis.firma_id, ergebnis.quelle) == ("woe_leh", QUELLE_GEDAECHTNIS)
    assert len(_hinweise(ergebnis, Pruefcode.FIRMA_GELERNT)) == 1


def test_katalog_und_gedaechtnis_aus_settings_dateien(settings: Settings, aliase: AliasTabellen, gedaechtnis_datei: Path):
    """Ohne übergebene Objekte liest ermittle_firma Katalog und Gedächtnis aus settings.firmenwahl."""
    Firmengedaechtnis([_regel(REGION_KUNDE, None, "wik_leh")], pfad=gedaechtnis_datei).speichere()
    roh = _roh(settings, kunde_text=REGION_KUNDE)
    ergebnis = ermittle_firma(roh, settings, aliase, stammdaten_lader=_kein_lader)
    assert (ergebnis.firma_id, ergebnis.quelle) == ("wik_leh", QUELLE_GEDAECHTNIS)

    settings.firmenwahl.katalog_datei = gedaechtnis_datei.parent / "kein_katalog.yaml"
    roh = _roh(settings, kunde_text="Frischemarkt Beispiel e.K.")
    ergebnis = ermittle_firma(roh, settings, aliase, stammdaten_lader=lambda s: {})
    assert (ergebnis.firma_id, ergebnis.quelle, ergebnis.kundengruppe) == (None, None, None)


# ---------------------------------------------------------------------------
# pruefung: FIRMA_UNKLAR mit Kandidaten und Kundengruppe
# ---------------------------------------------------------------------------


def test_pruefung_firma_unklar_mit_kandidaten_und_kundengruppe(settings: Settings):
    warnung = Pruefhinweis(
        code=Pruefcode.FIRMA_UNKLAR,
        schweregrad=Schweregrad.WARNUNG,
        text="Bitte Firma wählen (Kundengruppe region): wik_leh oder woe_leh",
        details={"kandidaten": "wik_leh, woe_leh", "kundengruppe": "region"},
    )
    paket = Belegpaket(roh=_roh(settings, kunde_text=REGION_KUNDE), hinweise=[warnung], kundengruppe=Kundengruppe.REGION)
    pruefe(paket, None, settings)
    pruefe(paket, None, settings)  # idempotent
    firma_hinweise = _hinweise(paket, Pruefcode.FIRMA_UNKLAR)
    assert [h.schweregrad for h in firma_hinweise] == [Schweregrad.WARNUNG, Schweregrad.FEHLER]
    fehler = firma_hinweise[1]
    assert fehler.details == {"kandidaten": "wik_leh, woe_leh", "kundengruppe": "region"}
    assert "Kundengruppe region" in fehler.text and "wik_leh, woe_leh" in fehler.text
    assert paket.blockiert is True

    # Kundengruppe nur aus dem Paket, Kandidaten aus zwei Warnungen zusammengeführt
    paket = Belegpaket(
        roh=_roh(settings, kunde_text=REGION_KUNDE),
        hinweise=[
            Pruefhinweis(code=Pruefcode.FIRMA_UNKLAR, schweregrad=Schweregrad.WARNUNG, text="a", details={"kandidaten": "woe_leh, wik_fh_seh"}),
            Pruefhinweis(code=Pruefcode.FIRMA_UNKLAR, schweregrad=Schweregrad.WARNUNG, text="b", details={"kandidaten": "wik_leh, woe_leh"}),
        ],
        kundengruppe=Kundengruppe.KETTE,
    )
    pruefe(paket, None, settings)
    fehler = _hinweise(paket, Pruefcode.FIRMA_UNKLAR)[-1]
    assert fehler.details == {"kandidaten": "woe_leh, wik_fh_seh, wik_leh", "kundengruppe": "kette"}

    # ohne Kandidaten und Kundengruppe: wie bisher nur "kandidaten"
    paket = Belegpaket(roh=_roh(settings, kunde_text="Château Inconnu"))
    pruefe(paket, None, settings)
    fehler = _hinweise(paket, Pruefcode.FIRMA_UNKLAR)
    assert len(fehler) == 1 and fehler[0].details == {"kandidaten": ""}


# ---------------------------------------------------------------------------
# pipeline: kundengruppe, setze_firma(merken=True) -> Gedächtnis -> nächstes Paket
# ---------------------------------------------------------------------------


def test_pipeline_fragen_merken_und_wiederfinden(settings: Settings, gedaechtnis_datei: Path):
    roh = _roh(
        settings,
        "region.pdf",
        kunde_text=REGION_KUNDE,
        positionen=[RohPosition(laufnr=1, bezeichnung="Bollina Gavi di Gavi DOCG 2023", menge=Decimal(12), preis=Decimal("12.50"))],
    )
    paket = erstelle_belegpaket(roh, settings)
    assert (paket.firma_id, paket.firma_quelle, paket.kundengruppe) == (None, None, Kundengruppe.REGION)
    fehler = [h for h in _hinweise(paket, Pruefcode.FIRMA_UNKLAR) if h.schweregrad == Schweregrad.FEHLER]
    assert len(fehler) == 1
    assert fehler[0].details == {"kandidaten": "wik_leh, woe_leh", "kundengruppe": "region"}
    assert paket.blockiert is True and paket.firma_merken is False

    # Anwender wählt WOE ohne Häkchen: nichts gemerkt
    ohne = setze_firma(paket, "woe_leh", settings, merken=False)
    assert (ohne.firma_id, ohne.firma_quelle, ohne.kundengruppe, ohne.firma_merken) == (
        "woe_leh",
        "manuell",
        Kundengruppe.REGION,
        False,
    )
    assert not gedaechtnis_datei.exists()
    assert erstelle_belegpaket(roh, settings).firma_id is None

    # mit Häkchen: Regel wird geschrieben und beim nächsten Paket gefunden
    neu = setze_firma(paket, "woe_leh", settings, merken=True, benutzer="Frau Beispiel")
    assert neu.firma_merken is True and neu.firma_id == "woe_leh"
    assert Pruefcode.FIRMA_UNKLAR not in [h.code for h in neu.alle_hinweise]
    regeln = Firmengedaechtnis.lade(gedaechtnis_datei).regeln
    assert len(regeln) == 1
    regel = regeln[0]
    # Lieferant Bollina über die Positionen zugeordnet (woe_leh führt Bollina, 138)
    assert (regel.kunde_kurz, regel.lieferant_kurz, regel.firma_id) == (REGION_KUNDE, "BOLLINA", "woe_leh")
    assert (regel.kundengruppe, regel.gelernt_von, regel.quelle) == (Kundengruppe.REGION, "Frau Beispiel", "freigabe")

    wieder = erstelle_belegpaket(roh, settings)
    assert (wieder.firma_id, wieder.firma_quelle, wieder.kundengruppe) == ("woe_leh", QUELLE_GEDAECHTNIS, Kundengruppe.REGION)
    gelernt = _hinweise(wieder, Pruefcode.FIRMA_GELERNT)
    assert len(gelernt) == 1 and gelernt[0].details["gelernt_von"] == "Frau Beispiel"
    assert Pruefcode.FIRMA_UNKLAR not in [h.code for h in wieder.alle_hinweise]
    assert wieder.lieferant.lieferant is not None and wieder.lieferant.lieferant.lieferantennummer == "138"

    # anderer Lieferant desselben Kunden: Regel gilt nur für Bollina -> wieder fragen
    anders = _roh(settings, "region2.pdf", kunde_text=REGION_KUNDE, lieferant_text="Cantine Colosi")
    assert erstelle_belegpaket(anders, settings).firma_id is None


def test_gedaechtnis_findet_regel_ueber_dokumentnamen_statt_matchcode(settings: Settings, aliase: AliasTabellen, gedaechtnis_datei: Path):
    """Regression (Integration Stufe 2, Szenario Firmenwahl): Kunde und Lieferant wurden in der gewählten Firma
    über den Stamm zugeordnet, die Regel trägt deshalb die Matchcodes ("KELLEREI"/"QUINTA"). Beim nächsten
    Auftrag ist die Firma noch unbekannt; das Dokument nennt nur "Musterkellerei Nord GmbH" und das Weingut nur in
    der Ladeadresse/den Positionen. Früher wurde die Regel nicht gefunden und das Tool fragte erneut."""
    positionen = [RohPosition(laufnr=1, bezeichnung="Quinta Beispiel Vinho Verde DOC 0,75l", menge=Decimal(6), preis=Decimal("1.25"))]
    roh = _roh(settings, "lager.pdf", kunde_text="Musterkellerei Nord GmbH", ladeadresse="Quinta Beispiel Lda., Rua do Vinho 10, 4900 Viana do Castelo", positionen=positionen)
    paket = Belegpaket(
        roh=roh,
        firma_id="woe_leh",
        firma_merken=True,
        kundengruppe=Kundengruppe.REGION,
        kunde=KundeZuordnung(kunde=Kunde(firma_id="woe_leh", kundennummer="3100", name="Musterkellerei Nord GmbH", matchcode="KELLEREI"), status=MatchStatus.SICHER),
        lieferant=LieferantZuordnung(lieferant=Lieferant(firma_id="woe_leh", lieferantennummer="210", name="Quinta Beispiel Lda.", matchcode="QUINTA"), status=MatchStatus.SICHER),
    )
    regel = merke_firma(paket, settings, benutzer="Frau Beispiel")
    assert (regel.kunde_kurz, regel.lieferant_kurz) == ("KELLEREI", "QUINTA")
    assert regel.kunde_texte == ["Musterkellerei Nord GmbH"] and regel.lieferant_texte == ["Quinta Beispiel Lda."]
    gedaechtnis = Firmengedaechtnis.lade(gedaechtnis_datei)
    assert gedaechtnis.regeln[0].kunde_texte == ["Musterkellerei Nord GmbH"]  # YAML-Roundtrip

    zweiter = _roh(settings, "lager2.pdf", kunde_text="Musterkellerei Nord GmbH", positionen=positionen)
    ergebnis = ermittle_firma(zweiter, settings, aliase, stammdaten_lader=_kein_lader, gedaechtnis=gedaechtnis)
    assert (ergebnis.firma_id, ergebnis.quelle) == ("woe_leh", QUELLE_GEDAECHTNIS)
    assert [h.code for h in ergebnis.hinweise] == [Pruefcode.FIRMA_GELERNT]
    # anderer Lieferant desselben Kunden: die Regel gilt nur für Quinta -> nicht aus dem Gedächtnis
    anderer = _roh(
        settings, "lager3.pdf", kunde_text="Musterkellerei Nord GmbH",
        positionen=[RohPosition(laufnr=1, bezeichnung="Bollina Gavi di Gavi DOCG 2023", menge=Decimal(6), preis=Decimal("12.50"))],
    )
    assert ermittle_firma(anderer, settings, aliase, stammdaten_lader=_kein_lader, gedaechtnis=gedaechtnis).quelle != QUELLE_GEDAECHTNIS


def test_gedaechtnis_region_regel_gilt_nicht_fuer_andere_region_derselben_kette(settings: Settings, aliase: AliasTabellen):
    """Regression (Review Befund 4): Eine Antwort für die Einkaufsregion Rhein-Ruhr (Matchcode nur 'EDEKA') darf
    nicht tolerant über den Matchcode auf 'EDEKA Minden-Hannover' übergehen – dort muss das Tool fragen
    (Geschäftsregel 17). Auch ohne gespeicherte Kundengruppe, weil der Katalog die Bestellung als Region einstuft."""
    bollina = [RohPosition(laufnr=1, bezeichnung="Bollina Gavi di Gavi DOCG 2023", menge=Decimal(12), preis=Decimal("12.50"))]
    regel = _regel("EDEKA", "Bollina", "woe_leh", kunde_texte=["Edeka Zentrale Rhein-Ruhr"])
    gedaechtnis = Firmengedaechtnis([regel])
    minden = _roh(settings, "minden.pdf", kunde_text="EDEKA Minden-Hannover Zentraleinkauf", positionen=bollina)
    ergebnis = ermittle_firma(minden, settings, aliase, stammdaten_lader=_kein_lader, gedaechtnis=gedaechtnis)
    assert ergebnis.quelle != QUELLE_GEDAECHTNIS and ergebnis.firma_id is None
    assert ergebnis.kundengruppe == Kundengruppe.REGION
    assert not _hinweise(ergebnis, Pruefcode.FIRMA_GELERNT)
    # dieselbe Region (gelernter Name steht vollständig im Dokument): weiterhin aus dem Gedächtnis
    rhein_ruhr = _roh(settings, "rr.pdf", kunde_text="Edeka Zentrale Rhein-Ruhr GmbH", positionen=bollina)
    ergebnis = ermittle_firma(rhein_ruhr, settings, aliase, stammdaten_lader=_kein_lader, gedaechtnis=gedaechtnis)
    assert (ergebnis.firma_id, ergebnis.quelle) == ("woe_leh", QUELLE_GEDAECHTNIS)
    # Regel mit Kundengruppe Region: auch die direkte Suche nicht tolerant über den Matchcode
    region = Firmengedaechtnis([_regel("EDEKA", None, "woe_leh", kundengruppe=Kundengruppe.REGION)])
    assert region.finde("EDEKA Minden-Hannover Zentraleinkauf") is None
    assert region.finde("EDEKA").firma_id == "woe_leh"  # exakt gilt
    # tolerante Stufe per Einstellung abschaltbar
    settings.firmenwahl.gedaechtnis_tolerant = False
    datei = settings.firmenwahl.gedaechtnis_datei
    Firmengedaechtnis([_regel("MENRATH", None, "wik_leh")]).speichere(datei)
    aus = Firmengedaechtnis.aus_settings(settings)
    assert aus.tolerant is False and aus.finde("Weinhaus Menrath") is None and aus.finde("Menrath").firma_id == "wik_leh"


def test_pipeline_manuelle_firma_traegt_kundengruppe(settings: Settings):
    roh = _roh(settings, kunde_text=REGION_KUNDE)
    paket = erstelle_belegpaket(roh, settings, firma_id="wik_leh")
    assert (paket.firma_id, paket.firma_quelle, paket.kundengruppe) == ("wik_leh", "manuell", Kundengruppe.REGION)
    paket = erstelle_belegpaket(_roh(settings, kunde_text="Château Inconnu"), settings, firma_id="wik_leh")
    assert paket.kundengruppe is None
    kopie = Belegpaket.model_validate_json(paket.model_dump_json())
    assert kopie.kundengruppe is None and kopie.firma_merken is False
