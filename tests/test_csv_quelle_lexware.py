"""Tests für die echten Lexware-Exportformate (stammdaten/csv_quelle, Ausbaustufe 2 Abschnitt 4).

Fixtures: tests/fixtures/stammdaten_lexware/<firma>/{artikel,kunden,lieferanten}.csv mit den
Spaltenköpfen der echten Exporte (synthetische Daten, Domain example.org):

* Artikel: UTF-8 mit BOM, ";" ohne Anführungszeichen, "Einheit" = "75 cl", "Benutzerdefiniert 2" = Jahrgang,
  "Gesperrt" J/N, "EK Preis Lf.1" 0,00 = nicht gepflegt.
* Kunden: Kopfzeile in der DOS-Codepage (cp850), Datenzeilen cp1252, PLZ teils als "\\t46244" in
  Anführungszeichen, "Inaktiv" J/N, "EG ID", "EMAIL", "Bemerkung".
* Lieferanten: cp1252, jedes Feld in Anführungszeichen.

Die Spaltenzuordnung kommt aus der echten config/firmen.yaml (Anker ``x_lexware_export``), nur die
Dateipfade zeigen auf die Fixtures.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from pathlib import Path

import pytest

from auftragsautomat.config import CsvQuellen, Firma, Settings, lade_settings
from auftragsautomat.stammdaten.basis import Stammdaten
from auftragsautomat.stammdaten.csv_quelle import (
    CsvStammdatenQuelle,
    StammdatenFehler,
    lies_csv_zeilen,
    spaltenschluessel,
)
from auftragsautomat.stammdaten.laden import lade_stammdaten, leere_cache

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "tests" / "fixtures" / "stammdaten_lexware"
FIRMEN = ("weinstrasse_agentur", "wik_fh_seh", "wik_leh", "woe_leh")


@pytest.fixture(scope="module")
def produktiv_settings() -> Settings:
    """Die echte Konfiguration des Repos (config/firmen.yaml) – nur für die Spaltenzuordnung."""
    return lade_settings(REPO)


def _quelle(settings: Settings, firma_id: str) -> CsvStammdatenQuelle:
    firma = settings.firma(firma_id)
    assert firma.stammdaten.csv is not None
    konfig = firma.stammdaten.csv.model_copy(
        update={
            "artikel": FIXTURES / firma_id / "artikel.csv",
            "kunden": FIXTURES / firma_id / "kunden.csv",
            "lieferanten": FIXTURES / firma_id / "lieferanten.csv",
            "belege": None,
        }
    )
    return CsvStammdatenQuelle(firma_id, konfig)


# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------


def test_firmen_yaml_traegt_die_exportzuordnung(produktiv_settings: Settings):
    for firma_id in FIRMEN:
        csv = produktiv_settings.firma(firma_id).stammdaten.csv
        assert csv is not None, firma_id
        assert csv.kodierung == "cp1252"
        assert csv.spalten_artikel["aktiv"] == "!Gesperrt"
        assert csv.spalten_artikel["jahrgang"] == "Benutzerdefiniert 2"
        assert csv.spalten_artikel["ek_preis"] == "EK Preis Lf.1"
        assert csv.spalten_kunden["name"] == "Firma" and csv.spalten_kunden["aktiv"] == "!Inaktiv"
        assert csv.spalten_kunden["ust_id"] == "EG ID" and csv.spalten_kunden["email"] == "EMAIL"
        assert csv.spalten_lieferanten["matchcode"] == "Matchcode" and csv.spalten_lieferanten["aktiv"] == "!Inaktiv"


# ---------------------------------------------------------------------------
# Alle vier Fixture-Sätze laden
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("firma_id", FIRMEN)
def test_alle_firmen_ladbar(produktiv_settings: Settings, firma_id: str, caplog: pytest.LogCaptureFixture):
    quelle = _quelle(produktiv_settings, firma_id)
    with caplog.at_level(logging.WARNING):
        daten = Stammdaten.aus_quelle(quelle)
    assert 5 <= len(daten.artikel) <= 8
    assert 5 <= len(daten.kunden) <= 8
    assert 5 <= len(daten.lieferanten) <= 8
    assert daten.belege == []
    # Jede Zeile ist angekommen: keine übersprungenen Zeilen, keine unbekannten Wahrheitswerte
    assert not any("übersprungen" in r.getMessage() for r in caplog.records)
    assert not any("Unbekannter Wahrheitswert" in r.getMessage() for r in caplog.records)
    assert all(a.volumen_cl is not None for a in daten.artikel if a.einheit != "Stk")
    assert all(k.plz and not k.plz.startswith(("\t", " ")) for k in daten.kunden)
    assert all(k.name for k in daten.kunden) and all(lf.name for lf in daten.lieferanten)


def test_artikel_mapping_wik_leh(produktiv_settings: Settings):
    artikel = {a.artikelnummer: a for a in _quelle(produktiv_settings, "wik_leh").artikel()}
    gloria = artikel["1643"]
    assert gloria.bezeichnung == "Villa Armellina Gloria Secco Frizzante"
    assert gloria.warengruppe == "Cide"
    assert gloria.einheit == "75 cl"
    assert gloria.volumen_cl == Decimal("75")  # aus "Einheit"
    assert gloria.vk_preis_1 == Decimal("6.90")  # "Preis 1"
    assert gloria.ek_preis == Decimal("4.20")  # "EK Preis Lf.1"
    assert gloria.lieferantennummer == "72"  # "Lieferantennummer 1"
    assert gloria.matchcode == "GLORIA"
    assert gloria.jahrgang is None
    assert gloria.aktiv is True and gloria.ist_sondervariante is False
    assert artikel["958"].volumen_cl == Decimal("150") and artikel["958"].ist_sondervariante
    assert artikel["1644"].volumen_cl == Decimal("37.5")  # "37,5 cl"
    nero = artikel["2001"]
    assert nero.jahrgang == 2022  # Benutzerdefiniert 2
    assert artikel["2002"].jahrgang == 2021
    gesperrt = artikel["3003"]
    assert gesperrt.aktiv is False  # Gesperrt = J
    assert gesperrt.ek_preis is None  # EK 0,00 -> nicht gepflegt
    assert gesperrt.vk_preis_1 == Decimal("11.00")


def test_artikel_jahrgang_nur_vierstellig(produktiv_settings: Settings):
    artikel = {a.artikelnummer: a for a in _quelle(produktiv_settings, "woe_leh").artikel()}
    alt = artikel["4007"]
    assert alt.jahrgang is None  # "abc" in Benutzerdefiniert 2 zählt nicht
    assert alt.aktiv is False and alt.ek_preis is None and alt.lieferantennummer is None
    assert artikel["4003"].jahrgang == 2023


def test_artikel_holzkiste_aus_bezeichnung(produktiv_settings: Settings):
    artikel = {a.artikelnummer: a for a in _quelle(produktiv_settings, "wik_fh_seh").artikel()}
    assert artikel["5006"].holzkiste is True and artikel["5006"].ist_sondervariante
    assert artikel["5003"].volumen_cl == Decimal("37.5")


def test_kunden_mapping_mojibake_kopf(produktiv_settings: Settings, caplog: pytest.LogCaptureFixture):
    with caplog.at_level(logging.WARNING):
        kunden = {k.kundennummer: k for k in _quelle(produktiv_settings, "wik_leh").kunden()}
    # Kopfzeile in DOS-Codepage: wird erkannt und getrennt von den cp1252-Datenzeilen gelesen
    assert any("Kopfzeile" in r.getMessage() and "cp850" in r.getMessage() for r in caplog.records)
    handel = kunden["2100"]
    assert handel.name == "Beispiel Handels AG"  # "Firma"
    assert handel.name2 == "Zentrale Einkauf Wein"  # "Zusatz"
    assert handel.strasse == "Handelsstraße 1"  # Straße + HausNr, Umlaute der Datenzeilen intakt
    assert handel.plz == "5020" and handel.ort == "Salzburg" and handel.land == "Österreich"
    assert handel.preisgruppe == 1
    assert handel.matchcode == "HANDEL"
    assert handel.email == "einkauf@example.org"
    assert handel.ust_id == "ATU00000000"  # "EG ID"
    assert handel.bemerkung == "Fixtermin, Spedition Beispiel"
    assert handel.aktiv is True
    region = kunden["2300"]
    assert region.plz == "46244"  # exportiert als "\t46244"
    assert region.name2 == "Lager Nord"
    person = kunden["2400"]
    assert person.name == "Anna Beispiel"  # Firma leer -> Vorname + Name
    assert person.preisgruppe == 2
    assert kunden["2500"].aktiv is False  # Inaktiv = J


def test_lieferanten_mapping_mit_anfuehrungszeichen(produktiv_settings: Settings):
    lieferanten = {lf.lieferantennummer: lf for lf in _quelle(produktiv_settings, "woe_leh").lieferanten()}
    cide = lieferanten["72"]
    assert cide.name == "Cide srl" and cide.name2 == "Villa Armellina"
    assert cide.ort == "Treviso" and cide.land == "Italien"
    assert cide.matchcode == "CIDE"
    assert cide.email == "orders@example.org"
    assert cide.bemerkung == "Orders-Kontakt laut Kontaktliste"
    assert cide.aktiv is True
    assert lieferanten["210"].name == "Quinta Beispiel Lda." and lieferanten["210"].land == "Portugal"
    assert lieferanten["300"].name == "Franz Beispiel"  # Firma leer -> Vorname + Name
    assert lieferanten["300"].name2 == "Weinbauer"
    assert lieferanten["301"].aktiv is False
    assert lieferanten["301"].ort == "Bad Dürkheim"  # cp1252-Umlaute


def test_laden_mit_cache_ueber_firma(produktiv_settings: Settings):
    firma = produktiv_settings.firma("weinstrasse_agentur")
    assert firma.stammdaten.csv is not None
    konfig = firma.stammdaten.csv.model_copy(
        update={
            "artikel": FIXTURES / "weinstrasse_agentur" / "artikel.csv",
            "kunden": FIXTURES / "weinstrasse_agentur" / "kunden.csv",
            "lieferanten": FIXTURES / "weinstrasse_agentur" / "lieferanten.csv",
            "belege": None,
        }
    )
    firma2 = Firma.model_validate({**firma.model_dump(), "stammdaten": {"typ": "csv", "csv": konfig.model_dump()}})
    leere_cache(firma2.id)
    daten = lade_stammdaten(firma2, cache=False)
    assert daten.artikel_nach_nummer["11081"].vk_preis_1 == Decimal("6.90")
    assert daten.artikel_nach_nummer["11081"].ek_preis is None  # Agentur: kein EK gepflegt (0,00)
    assert daten.kunden_nach_nummer["9200"].matchcode == "SPAR"
    assert daten.lieferanten_nach_nummer["72"].name == "Cide srl"


# ---------------------------------------------------------------------------
# Kopfzeilen-Matching, Negation, Kodierung im Einzelnen
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Straße", "Straáe"),  # DOS-Mojibake (cp850 als cp1252 gelesen)
        ("Straße", "Strasse"),
        ("steuerbare Umsätze", "steuerbare Ums„tze"),
        ("Fällig zum", "F„llig zum"),
        ("Abfrage nach §13b UStG", "Abfrage nach õ13b UStG"),
        ("EMAIL", "  email "),
        ('"Lieferantennummer"', "Lieferantennummer"),
        ("Warengr.-Kurzbez.", "WARENGR.-KURZBEZ."),
    ],
)
def test_spaltenschluessel_tolerant(a: str, b: str):
    ka, kb = spaltenschluessel(a), spaltenschluessel(b)
    assert ka[0] == kb[0] or ka[1] == kb[1]


def test_spaltenschluessel_unterscheidet_verschiedene_spalten():
    assert spaltenschluessel("Straße") != spaltenschluessel("Liefer. Straße")
    assert spaltenschluessel("Name")[0] != spaltenschluessel("Vorname")[0]


def _schreibe(pfad: Path, text: str, kodierung: str = "cp1252") -> Path:
    pfad.parent.mkdir(parents=True, exist_ok=True)
    pfad.write_bytes(text.encode(kodierung))
    return pfad


def _konfig(ordner: Path, **extra: object) -> CsvQuellen:
    return CsvQuellen(
        artikel=ordner / "artikel.csv",
        kunden=ordner / "kunden.csv",
        lieferanten=ordner / "lieferanten.csv",
        belege=None,
        **extra,  # type: ignore[arg-type]
    )


def test_mojibake_kopfzeile_in_cp1252_wird_zugeordnet(tmp_path: Path):
    # Kopfzeile wie sie Lexware liefert, wenn die DOS-Codepage als cp1252 gelesen wird (ohne ü -> kein Decodierfehler)
    kopf = "Kundennummer;Name;Vorname;Firma;Zusatz;Straáe;HausNr;Postleitzahl;Ort;steuerbare Ums„tze;F„llig zum;Inaktiv"
    zeile = '4711;;;Weinhandlung Müller;;Bahnhofstraße;12;"\t47051";Duisburg;J;;N'
    _schreibe(tmp_path / "kunden.csv", kopf + "\r\n" + zeile + "\r\n")
    konfig = _konfig(
        tmp_path,
        spalten_kunden={"name": "Firma", "strasse": "Straße", "hausnr": "HausNr", "plz": "Postleitzahl", "aktiv": "!Inaktiv"},
    )
    kunden = CsvStammdatenQuelle("test", konfig).kunden()
    assert kunden[0].name == "Weinhandlung Müller"
    assert kunden[0].strasse == "Bahnhofstraße 12"
    assert kunden[0].plz == "47051"
    assert kunden[0].aktiv is True


def test_negation_syntax(tmp_path: Path):
    _schreibe(
        tmp_path / "artikel.csv",
        "Artikelnummer;Bezeichnung;Gesperrt;Einheit\n1;Gavi;J;75 cl\n2;Grillo;N;75 cl\n3;Nero;;0,375 l\n",
    )
    konfig = _konfig(tmp_path, spalten_artikel={"aktiv": "!Gesperrt", "einheit": "Einheit"})
    artikel = CsvStammdatenQuelle("test", konfig).artikel()
    assert [a.aktiv for a in artikel] == [False, True, True]  # leer -> Standard aktiv
    assert [a.volumen_cl for a in artikel] == [Decimal("75"), Decimal("75"), Decimal("37.5")]
    # Pflichtspalten-Fehlermeldung nennt die Spalte ohne "!"
    _schreibe(tmp_path / "artikel.csv", "Nummer;Bezeichnung\n1;x\n")
    with pytest.raises(StammdatenFehler, match="Artikelnummer"):
        CsvStammdatenQuelle("test", _konfig(tmp_path, spalten_artikel={"artikelnummer": "!Artikelnummer"})).artikel()


def test_kodierung_kopfzeile_cp850_datenzeilen_cp1252(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    kopf = "Lieferantennummer;Firma;Ort;Länderkürzel\r\n".encode("cp850")  # ü = 0x81, in cp1252 undefiniert
    daten = "7;Weingut Müller;Bad Dürkheim;DE\r\n".encode("cp1252")
    (tmp_path / "lieferanten.csv").write_bytes(kopf + daten)
    konfig = _konfig(tmp_path, spalten_lieferanten={"name": "Firma"})
    with caplog.at_level(logging.WARNING):
        lieferanten = CsvStammdatenQuelle("test", konfig).lieferanten()
    assert lieferanten[0].name == "Weingut Müller" and lieferanten[0].ort == "Bad Dürkheim"
    assert any("Kopfzeile" in r.getMessage() for r in caplog.records)


def test_kodierung_ganze_datei_cp850(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    # Die ganze Datei in cp850 (Datenzeile enthält ü = 0x81): Rückfall auf cp850 für alles
    text = "Lieferantennummer;Firma;Ort\r\n7;Weingut Müller;Bad Dürkheim\r\n"
    (tmp_path / "lieferanten.csv").write_bytes(text.encode("cp850"))
    konfig = _konfig(tmp_path, spalten_lieferanten={"name": "Firma"})
    with caplog.at_level(logging.WARNING):
        lieferanten = CsvStammdatenQuelle("test", konfig).lieferanten()
    assert lieferanten[0].name == "Weingut Müller"
    assert any("cp850" in r.getMessage() for r in caplog.records)


def test_anfuehrungszeichen_beidseitig_und_tabs(tmp_path: Path):
    text = '"Kundennummer";"Firma";"Postleitzahl";"Ort"\r\n"5";"Hotel Seeblick";"\t12345";" Musterstadt "\r\n'
    _schreibe(tmp_path / "kunden.csv", text)
    zeilen = lies_csv_zeilen(tmp_path / "kunden.csv", _konfig(tmp_path, spalten_kunden={"name": "Firma", "plz": "Postleitzahl"}), "kunden")
    assert zeilen[0][1]["plz"] == "12345" and zeilen[0][1]["ort"] == "Musterstadt" and zeilen[0][1]["name"] == "Hotel Seeblick"


def test_kunde_ohne_firma_und_ohne_namen_wird_uebersprungen(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    _schreibe(tmp_path / "kunden.csv", "Kundennummer;Name;Vorname;Firma\n1;;;\n2;Beispiel;Eva;\n")
    konfig = _konfig(tmp_path, spalten_kunden={"name": "Firma", "nachname": "Name", "vorname": "Vorname"})
    with caplog.at_level(logging.WARNING):
        kunden = CsvStammdatenQuelle("test", konfig).kunden()
    assert [k.name for k in kunden] == ["Eva Beispiel"]
    assert any("Zeile 2" in r.getMessage() and "übersprungen" in r.getMessage() for r in caplog.records)
