"""Tests für stammdaten.odbc_quelle mit einem Fake-pyodbc-Modul (kein echter Datenbankzugriff)."""

from __future__ import annotations

import logging
import sys
import types
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from auftragsautomat.config import OdbcQuelle
from auftragsautomat.models import Belegart
from auftragsautomat.stammdaten.basis import Stammdaten, StammdatenQuelle
from auftragsautomat.stammdaten.odbc_quelle import (
    SQL_ARTIKEL_STANDARD,
    SQL_BELEGE_STANDARD,
    SQL_KUNDEN_STANDARD,
    SQL_LIEFERANTEN_STANDARD,
    OdbcKonfig,
    OdbcStammdatenQuelle,
    StammdatenFehler,
    erkunde_schema,
    maskiere_verbindung,
    pruefe_sql,
    schema_bericht_markdown,
    schreibe_schema_bericht,
    verbindungszeichenfolge,
)

# ---------------------------------------------------------------------------
# Fake-pyodbc
# ---------------------------------------------------------------------------


class FakeRow:
    """Zeile mit Attribut- und Indexzugriff wie pyodbc.Row."""

    def __init__(self, **felder: Any) -> None:
        self._werte = list(felder.values())
        for name, wert in felder.items():
            setattr(self, name, wert)

    def __getitem__(self, index: int) -> Any:
        return self._werte[index]

    def __iter__(self):
        return iter(self._werte)

    def __len__(self) -> int:
        return len(self._werte)


class FakeCursor:
    def __init__(self, verbindung: FakeConnection) -> None:
        self.verbindung = verbindung
        self.description: list[tuple] | None = None
        self._zeilen: list = []
        self.geschlossen = False

    def execute(self, sql: str, *params: Any) -> FakeCursor:
        self.verbindung.pyodbc.ausgefuehrt.append(sql)
        fehler = self.verbindung.pyodbc.fehler_bei
        if fehler and fehler in sql:
            raise self.verbindung.pyodbc.Error(f"Tabelle unbekannt in: {fehler}")
        for marker, (spalten, zeilen) in self.verbindung.pyodbc.ergebnisse.items():
            if marker in sql:
                self.description = [(s, str, None, None, None, None, True) for s in spalten]
                self._zeilen = [tuple(z) for z in zeilen]
                return self
        self.description = []
        self._zeilen = []
        return self

    def fetchall(self) -> list:
        return list(self._zeilen)

    def tables(self, **kwargs: Any) -> FakeCursor:
        self._zeilen = list(self.verbindung.pyodbc.tabellen)
        return self

    def columns(self, table: str | None = None, **kwargs: Any) -> FakeCursor:
        self._zeilen = list(self.verbindung.pyodbc.spalten.get(table or "", []))
        return self

    def close(self) -> None:
        self.geschlossen = True


class FakeConnection:
    def __init__(self, pyodbc: Any, verbindung: str, **kwargs: Any) -> None:
        self.pyodbc = pyodbc
        self.verbindung = verbindung
        self.kwargs = kwargs
        self.commits = 0
        self.geschlossen = False
        self.cursors: list[FakeCursor] = []
        self.dekodierung: list[tuple] = []

    def cursor(self) -> FakeCursor:
        c = FakeCursor(self)
        self.cursors.append(c)
        return c

    def commit(self) -> None:  # darf NIE aufgerufen werden
        self.commits += 1
        raise AssertionError("commit() auf einer rein lesenden Verbindung")

    def setdecoding(self, sqltype: Any, encoding: str | None = None) -> None:
        self.dekodierung.append((sqltype, encoding))

    def close(self) -> None:
        self.geschlossen = True


def _fake_pyodbc() -> types.ModuleType:
    modul = types.ModuleType("pyodbc")
    modul.Error = type("Error", (Exception,), {})  # type: ignore[attr-defined]
    modul.SQL_CHAR = 1  # type: ignore[attr-defined]
    modul.verbindungen = []  # type: ignore[attr-defined]
    modul.ausgefuehrt = []  # type: ignore[attr-defined]
    modul.ergebnisse = {}  # type: ignore[attr-defined]
    modul.fehler_bei = None  # type: ignore[attr-defined]
    modul.tabellen = []  # type: ignore[attr-defined]
    modul.spalten = {}  # type: ignore[attr-defined]

    def connect(verbindung: str, **kwargs: Any) -> FakeConnection:
        cn = FakeConnection(modul, verbindung, **kwargs)
        modul.verbindungen.append(cn)  # type: ignore[attr-defined]
        return cn

    modul.connect = connect  # type: ignore[attr-defined]
    return modul


@pytest.fixture()
def pyodbc(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    modul = _fake_pyodbc()
    monkeypatch.setitem(sys.modules, "pyodbc", modul)
    monkeypatch.setenv("LEXWARE_ODBC_PW", "geheim;123")
    return modul


@pytest.fixture()
def konfig() -> OdbcQuelle:
    return OdbcQuelle(
        dsn="LXSYDSN",
        benutzer="automat",
        passwort_env="LEXWARE_ODBC_PW",
        sql_artikel="SELECT ArtikelNr AS artikelnummer, Bezeichnung AS bezeichnung, EK AS ek_preis, VK AS vk_preis_1, Gesperrt AS gesperrt FROM FK_Artikel",
        sql_kunden="SELECT KundenNr AS kundennummer, Firma AS name, PreisgrpNr AS preisgruppe, bInaktiv AS inaktiv FROM FK_Kunde",
        sql_lieferanten="SELECT LieferantenNr AS lieferantennummer, Firma AS name, Ort AS ort FROM FK_Lieferant",
        sql_belege="SELECT AuftragsKennung AS belegart, AuftragsNr AS belegnummer, Bestellnummer AS kundenbestellnummer, KundenNr AS partnernummer, Datum AS datum, Netto AS netto FROM FK_Auftrag",
    )


def _fuelle_ergebnisse(pyodbc: types.ModuleType) -> None:
    pyodbc.ergebnisse = {  # type: ignore[attr-defined]
        "FK_Artikel": (
            ["ARTIKELNUMMER", "Bezeichnung", "ek_preis", "VK_PREIS_1", " gesperrt "],
            [
                (2005, "Colosi Salina Bianco IGT 2023 0,75 l", Decimal("6.10"), Decimal("9.80"), 0),
                ("2004", "Colosi Nero d'Avola 2022 6er Holzkiste", 36.0, 58.0, 0),
                ("3003", "Bollina Gavi di Gavi DOCG 2022 0,75 l", Decimal("7.60"), None, 1),
                (None, "Ohne Nummer", None, None, 0),
            ],
        ),
        "FK_Kunde": (
            ["kundennummer", "name", "preisgruppe", "inaktiv"],
            [(3441, "Hegmann  ", 1, 0), (1392, "Weinhaus Menrath", None, 1)],
        ),
        "FK_Lieferant": (["lieferantennummer", "name", "ort"], [(87, "Cantine Colosi", "Messina")]),
        "FK_Auftrag": (
            ["belegart", "belegnummer", "kundenbestellnummer", "partnernummer", "datum", "netto"],
            [
                (1, "AB-2026-0101", "B-4711", 3441, datetime(2026, 9, 15, 0, 0), Decimal("414.00")),
                (5, "BE-2026-0055", None, 72, date(2026, 9, 15), 252.0),
                (0, "AG-1", None, 1, None, None),
            ],
        ),
    }


# ---------------------------------------------------------------------------
# Verbindung und Sicherheitsregeln
# ---------------------------------------------------------------------------


def test_quelle_erfuellt_protokoll(konfig: OdbcQuelle) -> None:
    quelle = OdbcStammdatenQuelle("wik_leh", konfig)
    assert isinstance(quelle, StammdatenQuelle)
    assert quelle.firma_id == "wik_leh"
    assert OdbcKonfig is OdbcQuelle
    assert "geheim" not in repr(quelle)


def test_verbindung_readonly_ohne_commit(pyodbc: types.ModuleType, konfig: OdbcQuelle) -> None:
    _fuelle_ergebnisse(pyodbc)
    quelle = OdbcStammdatenQuelle("wik_leh", konfig)
    artikel = quelle.artikel()
    assert len(artikel) == 3
    cn = pyodbc.verbindungen[0]  # type: ignore[attr-defined]
    assert cn.kwargs["readonly"] is True
    assert cn.kwargs["autocommit"] is False
    assert cn.commits == 0
    assert cn.geschlossen is True
    assert all(c.geschlossen for c in cn.cursors)
    assert cn.verbindung == "DSN=LXSYDSN;UID=automat;PWD={geheim;123}"
    assert cn.dekodierung == [(1, "cp1252")]


def test_passwort_nicht_im_log(pyodbc: types.ModuleType, konfig: OdbcQuelle, caplog: pytest.LogCaptureFixture) -> None:
    _fuelle_ergebnisse(pyodbc)
    with caplog.at_level(logging.DEBUG):
        OdbcStammdatenQuelle("wik_leh", konfig).lieferanten()
    assert "geheim" not in caplog.text
    assert "PWD=***" in caplog.text


def test_verbindungszeichenfolge_varianten(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PW", "pw")
    assert verbindungszeichenfolge(OdbcQuelle(dsn="LX")) == "DSN=LX"
    assert verbindungszeichenfolge(OdbcQuelle(dsn="LX", benutzer="u", passwort_env="PW")) == "DSN=LX;UID=u;PWD=pw"
    assert (
        verbindungszeichenfolge(OdbcQuelle(verbindung="Driver={SQL Anywhere 17};ServerName=lxdbsrv;", benutzer="u"))
        == "Driver={SQL Anywhere 17};ServerName=lxdbsrv;UID=u"
    )
    # vorhandene UID/PWD im Connection-String werden nicht doppelt angehängt
    assert verbindungszeichenfolge(OdbcQuelle(verbindung="DSN=LX;UID=a;PWD=b", benutzer="u", passwort_env="PW")) == "DSN=LX;UID=a;PWD=b"
    monkeypatch.delenv("PW")
    with pytest.raises(StammdatenFehler, match="PW"):
        verbindungszeichenfolge(OdbcQuelle(dsn="LX", passwort_env="PW"))
    with pytest.raises(StammdatenFehler, match="dsn"):
        verbindungszeichenfolge(OdbcQuelle())


def test_konfig_ohne_dsn_wird_abgelehnt() -> None:
    with pytest.raises(StammdatenFehler, match="dsn"):
        OdbcStammdatenQuelle("x", OdbcQuelle())


def test_pyodbc_fehlt(monkeypatch: pytest.MonkeyPatch, konfig: OdbcQuelle) -> None:
    monkeypatch.setitem(sys.modules, "pyodbc", None)  # erzwingt ImportError
    monkeypatch.setenv("LEXWARE_ODBC_PW", "x")
    quelle = OdbcStammdatenQuelle("wik_leh", konfig)  # Instanziieren geht ohne pyodbc
    with pytest.raises(StammdatenFehler, match=r"pip install \.\[windows\]"):
        quelle.artikel()


def test_verbindungsfehler(pyodbc: types.ModuleType, konfig: OdbcQuelle) -> None:
    def kaputt(verbindung: str, **kwargs: Any) -> None:
        raise pyodbc.Error("IM002 Datenquelle nicht gefunden")  # type: ignore[attr-defined]

    pyodbc.connect = kaputt  # type: ignore[attr-defined]
    with pytest.raises(StammdatenFehler, match="IM002") as info:
        OdbcStammdatenQuelle("wik_leh", konfig).kunden()
    assert "geheim" not in str(info.value)


# ---------------------------------------------------------------------------
# SQL-Prüfung
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM FK_Artikel",
        "UPDATE FK_Artikel SET x = 1",
        "SELECT 1; DROP TABLE FK_Artikel",
        "SELECT 1;DELETE FROM x",
        "select * into neu from FK_Artikel",
        "WITH a AS (SELECT 1) SELECT * FROM a",
        "INSERT INTO x SELECT 1",
        "EXEC sp_irgendwas",
        "",
        "   ",
        None,
    ],
)
def test_pruefe_sql_lehnt_ab(sql: str | None) -> None:
    with pytest.raises(StammdatenFehler):
        pruefe_sql(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "select a.ArtikelNr from FK_Artikel a;",
        "  -- Kommentar; mit Semikolon\n SELECT 1 FROM x WHERE a = '; DROP TABLE y'",
        "/* update später */ SELECT System_updated, created_by FROM x",
        SQL_ARTIKEL_STANDARD,
        SQL_KUNDEN_STANDARD,
        SQL_LIEFERANTEN_STANDARD,
        SQL_BELEGE_STANDARD,
    ],
)
def test_pruefe_sql_erlaubt(sql: str) -> None:
    assert pruefe_sql(sql) == sql.strip()


def test_nicht_select_wird_nicht_ausgefuehrt(pyodbc: types.ModuleType, konfig: OdbcQuelle) -> None:
    konfig.sql_artikel = "DELETE FROM FK_Artikel"
    with pytest.raises(StammdatenFehler, match="SELECT"):
        OdbcStammdatenQuelle("wik_leh", konfig).artikel()
    assert pyodbc.verbindungen == []  # type: ignore[attr-defined]
    assert pyodbc.ausgefuehrt == []  # type: ignore[attr-defined]


def test_mehrere_anweisungen_werden_abgelehnt(pyodbc: types.ModuleType, konfig: OdbcQuelle) -> None:
    konfig.sql_kunden = "SELECT 1 FROM FK_Kunde; DROP TABLE FK_Kunde"
    with pytest.raises(StammdatenFehler, match="eine Anweisung"):
        OdbcStammdatenQuelle("wik_leh", konfig).kunden()
    assert pyodbc.ausgefuehrt == []  # type: ignore[attr-defined]


def test_standard_sql_wenn_nicht_konfiguriert(pyodbc: types.ModuleType, caplog: pytest.LogCaptureFixture) -> None:
    konfig = OdbcQuelle(dsn="LX")
    _fuelle_ergebnisse(pyodbc)
    with caplog.at_level(logging.WARNING):
        artikel = OdbcStammdatenQuelle("wik_leh", konfig).artikel()
    assert pyodbc.ausgefuehrt == [SQL_ARTIKEL_STANDARD.strip()]  # type: ignore[attr-defined]
    assert len(artikel) == 3
    assert any("Standardabfrage" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# Mapping Zeilen -> Modelle
# ---------------------------------------------------------------------------


def test_artikel_mapping(pyodbc: types.ModuleType, konfig: OdbcQuelle, caplog: pytest.LogCaptureFixture) -> None:
    _fuelle_ergebnisse(pyodbc)
    with caplog.at_level(logging.WARNING):
        artikel = {a.artikelnummer: a for a in OdbcStammdatenQuelle("wik_leh", konfig).artikel()}
    assert set(artikel) == {"2005", "2004", "3003"}
    salina = artikel["2005"]
    assert salina.firma_id == "wik_leh"
    assert salina.ek_preis == Decimal("6.10")
    assert salina.vk_preis_1 == Decimal("9.80")
    assert salina.volumen_cl == Decimal("75")  # aus der Bezeichnung
    assert salina.jahrgang == 2023
    assert salina.aktiv is True
    holz = artikel["2004"]
    assert holz.holzkiste is True  # aus der Bezeichnung
    assert holz.ek_preis == Decimal("36")
    assert isinstance(holz.ek_preis, Decimal)
    assert holz.ist_sondervariante is True
    assert artikel["3003"].aktiv is False  # gesperrt = 1
    assert artikel["3003"].vk_preis_1 is None
    assert any("übersprungen" in r.getMessage() for r in caplog.records)  # Zeile ohne Nummer


def test_kunden_und_lieferanten_mapping(pyodbc: types.ModuleType, konfig: OdbcQuelle) -> None:
    _fuelle_ergebnisse(pyodbc)
    quelle = OdbcStammdatenQuelle("wik_leh", konfig)
    kunden = {k.kundennummer: k for k in quelle.kunden()}
    assert kunden["3441"].name == "Hegmann"  # Padding entfernt
    assert kunden["3441"].preisgruppe == 1
    assert kunden["3441"].aktiv is True
    assert kunden["1392"].preisgruppe is None
    assert kunden["1392"].aktiv is False  # bInaktiv = 1
    lieferanten = quelle.lieferanten()
    assert lieferanten[0].lieferantennummer == "87"
    assert lieferanten[0].ort == "Messina"


def test_belege_mapping(pyodbc: types.ModuleType, konfig: OdbcQuelle) -> None:
    _fuelle_ergebnisse(pyodbc)
    belege = OdbcStammdatenQuelle("wik_leh", konfig).belege()
    assert [b.belegart for b in belege] == [Belegart.AUFTRAGSBESTAETIGUNG, Belegart.BESTELLUNG, Belegart.SONSTIGE]
    assert belege[0].kundenbestellnummer == "B-4711"
    assert belege[0].partnernummer == "3441"
    assert belege[0].datum == date(2026, 9, 15)
    assert belege[0].netto == Decimal("414.00")
    assert belege[1].datum == date(2026, 9, 15)
    assert belege[1].netto == Decimal("252")
    assert belege[2].datum is None


def test_belege_fehler_liefert_leere_liste(pyodbc: types.ModuleType, konfig: OdbcQuelle, caplog: pytest.LogCaptureFixture) -> None:
    _fuelle_ergebnisse(pyodbc)
    pyodbc.fehler_bei = "FK_Auftrag"  # type: ignore[attr-defined]
    with caplog.at_level(logging.WARNING):
        assert OdbcStammdatenQuelle("wik_leh", konfig).belege() == []
    assert any("Dublettenprüfung" in r.getMessage() for r in caplog.records)
    # Bei Artikeln ist ein Datenbankfehler dagegen ein harter Fehler
    pyodbc.fehler_bei = "FK_Artikel"  # type: ignore[attr-defined]
    with pytest.raises(StammdatenFehler, match="Tabelle unbekannt"):
        OdbcStammdatenQuelle("wik_leh", konfig).artikel()


def test_stammdaten_aus_odbc_quelle(pyodbc: types.ModuleType, konfig: OdbcQuelle) -> None:
    _fuelle_ergebnisse(pyodbc)
    daten = Stammdaten.aus_quelle(OdbcStammdatenQuelle("wik_leh", konfig))
    assert len(daten.artikel) == 3
    assert daten.kunden_nach_nummer["3441"].name == "Hegmann"
    assert len(daten.belege) == 3
    # vier Abfragen, vier Verbindungen, keine commits
    assert len(pyodbc.verbindungen) == 4  # type: ignore[attr-defined]
    assert all(cn.commits == 0 and cn.geschlossen for cn in pyodbc.verbindungen)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Schema-Erkundung
# ---------------------------------------------------------------------------


def _fuelle_schema(pyodbc: types.ModuleType) -> None:
    pyodbc.tabellen = [  # type: ignore[attr-defined]
        FakeRow(table_cat=None, table_schem="F1", table_name="FK_Artikel", table_type="TABLE", remarks=None),
        FakeRow(table_cat=None, table_schem="F1", table_name="FK_Auftrag", table_type="TABLE", remarks=None),
        FakeRow(table_cat=None, table_schem="SYS", table_name="SYSTAB", table_type="SYSTEM TABLE", remarks=None),
        FakeRow(table_cat=None, table_schem="F1", table_name="BH_Journal", table_type="TABLE", remarks=None),
    ]
    pyodbc.spalten = {  # type: ignore[attr-defined]
        "FK_Artikel": [
            FakeRow(table_cat=None, table_schem="F1", table_name="FK_Artikel", column_name="ArtikelNr", data_type=1, type_name="char", column_size=20),
            FakeRow(table_cat=None, table_schem="F1", table_name="FK_Artikel", column_name="Bezeichnung", data_type=1, type_name="char", column_size=100),
        ],
        "FK_Auftrag": [
            FakeRow(table_cat=None, table_schem="F1", table_name="FK_Auftrag", column_name="AuftragsNr", data_type=4, type_name="integer", column_size=10),
            FakeRow(table_cat=None, table_schem="F1", table_name="FK_Auftrag", column_name="Bestellnummer", data_type=1, type_name="char", column_size=30),
        ],
        "BH_Journal": [
            FakeRow(table_cat=None, table_schem="F1", table_name="BH_Journal", column_name="Betrag", data_type=3, type_name="decimal", column_size=18),
        ],
    }


def test_erkunde_schema(pyodbc: types.ModuleType, konfig: OdbcQuelle) -> None:
    _fuelle_schema(pyodbc)
    eintraege = erkunde_schema(konfig)
    assert {e["tabelle"] for e in eintraege} == {"FK_Artikel", "FK_Auftrag", "BH_Journal"}  # keine Systemtabellen
    assert {"tabelle", "spalte", "typ", "schema", "groesse", "tabellentyp"} <= set(eintraege[0])
    artikel = [e for e in eintraege if e["tabelle"] == "FK_Artikel"]
    assert [(e["spalte"], e["typ"], e["groesse"]) for e in artikel] == [("ArtikelNr", "char", 20), ("Bezeichnung", "char", 100)]
    cn = pyodbc.verbindungen[0]  # type: ignore[attr-defined]
    assert cn.kwargs["readonly"] is True
    assert cn.commits == 0
    assert cn.geschlossen
    nur_fk = erkunde_schema(konfig, praefix="FK_")
    assert {e["tabelle"] for e in nur_fk} == {"FK_Artikel", "FK_Auftrag"}


def test_schreibe_schema_bericht(pyodbc: types.ModuleType, konfig: OdbcQuelle, tmp_path: Path) -> None:
    _fuelle_schema(pyodbc)
    ziel = schreibe_schema_bericht(konfig, tmp_path / "berichte" / "schema.md")
    assert ziel.is_file()
    text = ziel.read_text(encoding="utf-8")
    assert text.startswith("# ODBC-Schema (nur lesend): LXSYDSN")
    assert "### F1.FK_Artikel (TABLE)" in text
    assert "| ArtikelNr | char | 20 |" in text
    assert "`FK_Auftrag.Bestellnummer`" in text  # Kandidat für die Kundenbestellnummer
    assert "`F1.FK_Artikel`" in text and "`F1.FK_Auftrag`" in text
    assert "geheim" not in text


def test_schema_bericht_markdown_ohne_verbindung() -> None:
    konfig = OdbcQuelle(verbindung="Driver={SQL Anywhere 17};UID=u;PWD=geheim")
    text = schema_bericht_markdown(konfig, [{"schema": None, "tabelle": "T", "tabellentyp": None, "spalte": "S", "typ": "int", "groesse": None}])
    assert "PWD=***" in text
    assert "geheim" not in text
    assert "### T (TABLE)" in text
    assert "| S | int |  |" in text
    assert "keine gefunden" in text


def test_maskiere_verbindung() -> None:
    assert maskiere_verbindung("DSN=LX;UID=u;PWD=abc;Extra=1") == "DSN=LX;UID=u;PWD=***;Extra=1"
    assert maskiere_verbindung("DSN=LX;PWD={ge;h}}eim}") == "DSN=LX;PWD=***"
    assert maskiere_verbindung("Password=x") == "Password=***"
    assert maskiere_verbindung(None) == ""
