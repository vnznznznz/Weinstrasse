"""Tests für matching.aliase (Laden, Normalisierung, Suche, Rückschreiben)."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

import pytest
import yaml

from auftragsautomat.config import Settings
from auftragsautomat.matching.aliase import (
    AliasEintrag,
    AliasFehler,
    AliasKonflikt,
    AliasTabellen,
    AliasTreffer,
    aehnlichkeit,
    enthaelt_als_wort,
    normalisiere_name,
    speichere_alias_artikel,
    speichere_alias_kunde,
    speichere_alias_lieferant,
    speichere_ueberspringen,
)


@pytest.fixture()
def aliase(settings: Settings) -> AliasTabellen:
    return AliasTabellen.lade(settings.alias_ordner)


@pytest.fixture()
def alias_kopie(settings: Settings, tmp_path: Path) -> Path:
    """Kopie der echten Alias-Tabellen zum Beschreiben."""
    ziel = tmp_path / "aliase"
    shutil.copytree(settings.alias_ordner, ziel)
    return ziel


# ---------------------------------------------------------------------------
# Normalisierung
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, erwartet",
    [
        ("Weinmarkt Kirchhellen GmbH & Co. KG", "weinmarkt kirchhellen"),
        ("Weinmarkt Kirchhellen GmbH + Co KG", "weinmarkt kirchhellen"),
        ("Bollina s.r.l.", "bollina"),
        ("Bollina srl", "bollina"),
        ("Cide S.p.A.", "cide"),
        ("Vinothek Schmidt GmbH", "vinothek schmidt"),
        ("Weinhandlung Müller-Lüdenscheid", "weinhandlung mueller luedenscheid"),
        ("Hegmann / Bottrop-Kirchhellen", "hegmann bottrop kirchhellen"),
        ("Colosi Nero d'Avola Sicilia DOC 2022 0,75l", "colosi nero davola sicilia doc 2022 0 75 l"),
        ("  Mehrfache   Leerzeichen  ", "mehrfache leerzeichen"),
        ("Café Crème", "cafe creme"),
        ("GmbH", "gmbh"),  # nur Rechtsform: bleibt erhalten, damit nichts leer wird
        ("", ""),
        (None, ""),
    ],
)
def test_normalisiere_name(text, erwartet):
    assert normalisiere_name(text) == erwartet


def test_normalisiere_name_ohne_rechtsformen_entfernen():
    assert normalisiere_name("Bollina srl", rechtsformen_entfernen=False) == "bollina srl"


def test_enthaelt_als_wort():
    assert enthaelt_als_wort("weinmarkt kirchhellen", "weinmarkt kirchhellen")
    assert enthaelt_als_wort("lieferung an hegmann bottrop", "hegmann")
    assert not enthaelt_als_wort("hegmanns weinmarkt", "hegmann")  # kein ganzes Wort
    assert not enthaelt_als_wort("", "hegmann")
    assert not enthaelt_als_wort("hegmann", "")


def test_aehnlichkeit():
    assert aehnlichkeit("weinhaus menrath", "weinhaus menrath") == 100.0
    assert aehnlichkeit("", "x") == 0.0
    teil = aehnlichkeit("weinhandlung mueller", "weinhandlung mueller luedenscheid")
    assert 85 <= teil < 95  # Teilmenge: hoch, aber klar unter dem exakten Treffer
    assert aehnlichkeit("weinhaus menrath", "hegmann") < 40


# ---------------------------------------------------------------------------
# Laden und Suche mit den echten Tabellen aus config/aliase
# ---------------------------------------------------------------------------


def test_lade_echte_tabellen(aliase: AliasTabellen, settings: Settings):
    assert aliase.ordner == settings.alias_ordner
    assert len(aliase.kunden_eintraege) == 5
    assert len(aliase.lieferanten_eintraege) == 4
    assert len(aliase.marken_eintraege) == 4
    assert len(aliase.artikel_eintraege) == 6
    assert len(aliase.ueberspringen_eintraege) == 1
    assert aliase.firmen() == {"wik_leh"}
    assert "5 Kunden-" in repr(aliase)


def test_kunde_kirchhellen(aliase: AliasTabellen):
    treffer = aliase.kunde("Weinmarkt Kirchhellen GmbH & Co. KG")
    assert treffer == AliasTreffer(
        "3441", "wik_leh", "Hegmann / Bottrop-Kirchhellen", "Weinmarkt Kirchhellen"
    )


def test_kunde_laengster_alias_gewinnt(aliase: AliasTabellen):
    treffer = aliase.kunde("Hegmann Bottrop-Kirchhellen")
    assert treffer is not None
    assert treffer.nummer == "3441"
    assert treffer.alias == "Hegmann Bottrop-Kirchhellen"


def test_kunde_alias_als_wortteilstueck(aliase: AliasTabellen):
    treffer = aliase.kunde("Lieferung an HEGMANN, Bottrop")
    assert treffer is not None and treffer.nummer == "3441"
    # "Menrath" als Teil eines anderen Wortes zählt nicht
    assert aliase.kunde("Menrathstraße 5") is None


def test_kunde_firma_filter(aliase: AliasTabellen):
    assert aliase.kunde("Weinhaus Menrath", firma_id="wik_leh") is not None
    assert aliase.kunde("Weinhaus Menrath", firma_id="woe_leh") is None


def test_kunde_kein_treffer(aliase: AliasTabellen):
    assert aliase.kunde("Unbekannter Kunde") is None
    assert aliase.kunde("") is None
    assert aliase.kunde(None) is None


def test_kunden_treffer_mehrere(aliase: AliasTabellen):
    treffer = aliase.kunden_treffer("Hegmann bestellt für Menrath")
    assert [t.nummer for t in treffer] == ["3441", "1392"]


def test_lieferant(aliase: AliasTabellen):
    assert aliase.lieferant("Cantine Colosi").nummer == "87"
    assert aliase.lieferant("Bollina s.r.l.").nummer == "138"
    assert aliase.lieferant("Cide srl, Treviso").nummer == "72"
    assert aliase.lieferant("Weingut Beispiel") is None


def test_lieferant_fuer_marke(aliase: AliasTabellen):
    treffer = aliase.lieferant_fuer_marke("Villa Armellina Gloria Secco Frizzante 0,75 l")
    assert treffer is not None
    assert (treffer.nummer, treffer.alias) == ("72", "Villa Armellina")
    assert aliase.lieferant_fuer_marke("Colosi Nero d'Avola Sicilia DOC 2022").nummer == "87"
    assert aliase.lieferant_fuer_marke("Weingut Beispiel Riesling") is None


def test_artikel(aliase: AliasTabellen):
    assert aliase.artikel("Villa Armellina Gloria Secco Frizzante 0,75 l").nummer == "1643"
    assert aliase.artikel("Gloria Secco Frizzante 0,375").nummer == "1643"
    assert aliase.artikel("Villa Armellina Rosato Secco Frizzante Magnum 1,5 l").nummer == "957"
    assert aliase.artikel("Colosi Nero d'Avola") is None
    assert aliase.artikel("Villa Armellina Gloria", firma_id="woe_leh") is None


def test_ist_zu_ueberspringen(aliase: AliasTabellen):
    assert aliase.ist_zu_ueberspringen("DAA Neptun") is True
    assert aliase.ist_zu_ueberspringen("DAA Neptun Aufsteller 0,00") is True
    assert aliase.ist_zu_ueberspringen("Neptun") is False
    assert aliase.ist_zu_ueberspringen(None) is False


def test_alle_firmen_fuer(aliase: AliasTabellen):
    assert aliase.alle_firmen_fuer_kunde("3441") == ["wik_leh"]
    assert aliase.alle_firmen_fuer_kunde("9999") == []
    assert aliase.alle_firmen_fuer_lieferant("72") == ["wik_leh"]
    assert aliase.aliase_fuer_kunde("1392") == ["Weinhaus Menrath", "Menrath"]
    assert aliase.aliase_fuer_artikel("957", "wik_leh") == [
        "Villa Armellina Rosato Secco Frizzante",
        "Rosato Secco Frizzante",
        "Villa Armellina Rosato",
    ]


# ---------------------------------------------------------------------------
# Robustheit beim Laden
# ---------------------------------------------------------------------------


def test_fehlende_dateien_ergeben_leere_tabellen(tmp_path: Path, caplog):
    with caplog.at_level(logging.WARNING):
        tabellen = AliasTabellen.lade(tmp_path / "gibt_es_nicht")
    assert tabellen.kunde("Weinhaus Menrath") is None
    assert tabellen.ist_zu_ueberspringen("DAA Neptun") is False
    assert tabellen.firmen() == set()
    assert "kunden.yaml" in caplog.text and "fehlt" in caplog.text


def test_kaputtes_yaml(tmp_path: Path):
    (tmp_path / "kunden.yaml").write_text("kunden: [unvollständig", encoding="utf-8")
    with pytest.raises(AliasFehler):
        AliasTabellen.lade(tmp_path)


def test_falsche_struktur(tmp_path: Path):
    (tmp_path / "kunden.yaml").write_text("- nur eine liste\n", encoding="utf-8")
    with pytest.raises(AliasFehler):
        AliasTabellen.lade(tmp_path)
    (tmp_path / "kunden.yaml").write_text("kunden: 'kein liste'\n", encoding="utf-8")
    with pytest.raises(AliasFehler):
        AliasTabellen.lade(tmp_path)


def test_tolerante_eintraege(tmp_path: Path, caplog):
    (tmp_path / "kunden.yaml").write_text(
        "kunden:\n"
        "  - kundennummer: 3441\n"  # Zahl statt Text
        "    firma: wik_leh\n"
        "    aliase: Hegmann\n"  # einzelner String statt Liste
        "  - kundennummer: '9'\n"  # ohne firma -> ignoriert
        "    aliase: ['Ohne Firma']\n"
        "  - firma: wik_leh\n"  # ohne Nummer -> ignoriert
        "    aliase: ['Ohne Nummer']\n"
        "  - kundennummer: '10'\n"
        "    firma: wik_leh\n"
        "    aliase: ['', '   ', '!!!']\n"  # leere/unbrauchbare Aliase
        "  - nur ein string\n",
        encoding="utf-8",
    )
    with caplog.at_level(logging.WARNING):
        tabellen = AliasTabellen.lade(tmp_path)
    assert tabellen.kunde("Hegmann") == AliasTreffer("3441", "wik_leh", None, "Hegmann")
    assert tabellen.kunde("Ohne Firma") is None
    assert tabellen.kunde("Ohne Nummer") is None
    assert "ohne 'firma'" in caplog.text


def test_mehrdeutiger_alias_warnung(tmp_path: Path, caplog):
    (tmp_path / "kunden.yaml").write_text(
        "kunden:\n"
        "  - {kundennummer: '1', firma: wik_leh, aliase: ['Müller']}\n"
        "  - {kundennummer: '2', firma: wik_leh, aliase: ['Mueller']}\n"
        "  - {kundennummer: '3', firma: woe_leh, aliase: ['Mueller']}\n",
        encoding="utf-8",
    )
    with caplog.at_level(logging.WARNING):
        tabellen = AliasTabellen.lade(tmp_path)
    assert "sowohl auf 1 als auch auf 2" in caplog.text
    assert tabellen.kunde("Müller").nummer == "1"  # erster Eintrag gewinnt
    assert tabellen.kunde("Müller", firma_id="woe_leh").nummer == "3"


def test_manuell_gebaute_tabellen():
    tabellen = AliasTabellen(
        artikel=[AliasEintrag("1644", "wik_leh", "Gloria klein", normalisiere_name("Gloria klein"))],
    )
    assert tabellen.artikel("Gloria klein 0,375 l").nummer == "1644"
    assert tabellen.ordner is None


# ---------------------------------------------------------------------------
# Rückschreiben aus der Freigabe-UI
# ---------------------------------------------------------------------------


def test_speichere_alias_kunde_ergaenzt_vorhandenen_eintrag(alias_kopie: Path):
    pfad = speichere_alias_kunde(alias_kopie, "3441", "wik_leh", "Hegmann Weinmarkt")
    assert pfad == alias_kopie / "kunden.yaml"
    daten = yaml.safe_load(pfad.read_text(encoding="utf-8"))
    assert [k["kundennummer"] for k in daten["kunden"]] == ["3441", "1392"]  # Reihenfolge stabil
    assert daten["kunden"][0]["aliase"] == [
        "Weinmarkt Kirchhellen",
        "Hegmann",
        "Hegmann Bottrop-Kirchhellen",
        "Hegmann Weinmarkt",
    ]
    assert daten["kunden"][0]["anzeigename"] == "Hegmann / Bottrop-Kirchhellen"
    tabellen = AliasTabellen.lade(alias_kopie)
    assert tabellen.kunde("Hegmann Weinmarkt GmbH").alias == "Hegmann Weinmarkt"
    # gleich nach Normalisierung wie "Weinmarkt Kirchhellen": keine Dublette
    speichere_alias_kunde(alias_kopie, "3441", "wik_leh", "Weinmarkt Kirchhellen GmbH & Co. KG")
    daten = yaml.safe_load(pfad.read_text(encoding="utf-8"))
    assert len(daten["kunden"][0]["aliase"]) == 4


def test_speichere_alias_kunde_neuer_eintrag_und_keine_dubletten(alias_kopie: Path):
    speichere_alias_kunde(
        alias_kopie, "2100", "wik_leh", "Müller Duisburg", anzeigename="Weinhandlung Müller"
    )
    speichere_alias_kunde(alias_kopie, "2100", "wik_leh", "MUELLER DUISBURG")  # gleich nach Normalisierung
    daten = yaml.safe_load((alias_kopie / "kunden.yaml").read_text(encoding="utf-8"))
    neu = daten["kunden"][-1]
    assert neu == {
        "kundennummer": "2100",
        "firma": "wik_leh",
        "anzeigename": "Weinhandlung Müller",
        "aliase": ["Müller Duisburg"],
    }
    assert AliasTabellen.lade(alias_kopie).kunde("Müller Duisburg").nummer == "2100"


def test_speichere_alias_kunde_konflikt(alias_kopie: Path):
    with pytest.raises(AliasKonflikt):
        speichere_alias_kunde(alias_kopie, "1392", "wik_leh", "Hegmann")
    # andere Firma: kein Konflikt
    speichere_alias_kunde(alias_kopie, "1392", "woe_leh", "Hegmann")
    tabellen = AliasTabellen.lade(alias_kopie)
    assert tabellen.kunde("Hegmann", firma_id="wik_leh").nummer == "3441"
    assert tabellen.kunde("Hegmann", firma_id="woe_leh").nummer == "1392"
    # mit ueberschreiben wird der Alias verschoben
    speichere_alias_kunde(alias_kopie, "1392", "wik_leh", "Hegmann", ueberschreiben=True)
    tabellen = AliasTabellen.lade(alias_kopie)
    assert tabellen.kunde("Hegmann", firma_id="wik_leh").nummer == "1392"
    assert tabellen.kunde("Weinmarkt Kirchhellen", firma_id="wik_leh").nummer == "3441"


def test_speichere_alias_ungueltig(alias_kopie: Path):
    with pytest.raises(AliasFehler):
        speichere_alias_kunde(alias_kopie, "3441", "wik_leh", "   ")
    with pytest.raises(AliasFehler):
        speichere_alias_kunde(alias_kopie, "", "wik_leh", "Alias")
    with pytest.raises(AliasFehler):
        speichere_alias_kunde(alias_kopie, "3441", "wik_leh", "!!!")
    with pytest.raises(AliasFehler):
        speichere_alias_lieferant(alias_kopie, "72", "wik_leh")


def test_speichere_alias_lieferant_mit_produktmarke(alias_kopie: Path):
    speichere_alias_lieferant(
        alias_kopie, "150", "wik_leh", "Weingut Beispiel", produktmarke="Beispiel", anzeigename="Beispiel"
    )
    tabellen = AliasTabellen.lade(alias_kopie)
    assert tabellen.lieferant("Weingut Beispiel GmbH").nummer == "150"
    assert tabellen.lieferant_fuer_marke("Beispiel Riesling trocken 2023").nummer == "150"
    # vorhandene Einträge bleiben erhalten
    assert tabellen.lieferant_fuer_marke("Villa Armellina Gloria").nummer == "72"
    speichere_alias_lieferant(alias_kopie, "72", "wik_leh", produktmarke="Armellina")
    assert AliasTabellen.lade(alias_kopie).lieferant_fuer_marke("Armellina Gloria").nummer == "72"


def test_speichere_alias_artikel_und_ueberspringen(alias_kopie: Path):
    speichere_alias_artikel(alias_kopie, "2001", "wik_leh", "Nero d'Avola 2022")
    speichere_ueberspringen(alias_kopie, "Werbeaufsteller")
    speichere_ueberspringen(alias_kopie, "WERBEAUFSTELLER")  # keine Dublette
    tabellen = AliasTabellen.lade(alias_kopie)
    assert tabellen.artikel("Colosi Nero d'Avola 2022 0,75 l").nummer == "2001"
    assert tabellen.artikel("Villa Armellina Gloria").nummer == "1643"  # unverändert
    assert tabellen.ist_zu_ueberspringen("Werbeaufsteller Holz")
    assert tabellen.ist_zu_ueberspringen("DAA Neptun")
    daten = yaml.safe_load((alias_kopie / "artikel.yaml").read_text(encoding="utf-8"))
    assert daten["ueberspringen"] == ["DAA Neptun", "Werbeaufsteller"]


def test_speichere_in_leeren_ordner(tmp_path: Path):
    ordner = tmp_path / "neu"
    speichere_alias_kunde(ordner, "7001", "wik_fh_seh", "Seeblick")
    speichere_alias_artikel(ordner, "5001", "wik_fh_seh", "Nero")
    speichere_ueberspringen(ordner, "Pfand")
    tabellen = AliasTabellen.lade(ordner)
    assert tabellen.kunde("Hotel Seeblick").nummer == "7001"
    assert tabellen.artikel("Nero d'Avola").nummer == "5001"  # "nero" ist ein ganzes Wort in "nero davola"
    assert tabellen.artikel("Nerone") is None
    assert tabellen.ist_zu_ueberspringen("Pfand Kiste")
    assert not (ordner / "kunden.yaml.tmp").exists()
