"""Stammdaten per ODBC direkt aus der Lexware-Datenbank, AUSSCHLIESSLICH LESEND.

Sicherheitsregeln dieses Moduls (siehe docs/ARCHITEKTUR.md "Kein Schreiben in die
Lexware-Datenbank"):

* ``pyodbc`` wird nur innerhalb der Methoden importiert; ohne pyodbc (Linux-Tests) ist das
  Modul importierbar, ein Zugriff löst :class:`StammdatenFehler` aus.
* Verbindung immer mit ``readonly=True`` und ``autocommit=False``; es wird **nie** ``commit()``
  aufgerufen (``close()`` verwirft die lesende Transaktion).
* Jede Abfrage wird mit :func:`pruefe_sql` geprüft: sie muss mit ``SELECT`` beginnen, darf nur
  eine Anweisung enthalten (kein ``;`` gefolgt von weiterem Text) und keine schreibenden
  Schlüsselwörter (INSERT/UPDATE/DELETE/DROP/...) enthalten.
* Die SQL kommt aus der Konfiguration (``OdbcQuelle.sql_artikel`` usw.). Fehlt sie, werden die
  Standardabfragen ``SQL_*_STANDARD`` verwendet. Deren Tabellen- und Spaltennamen sind
  **Annahmen** aus der Recherche (docs/recherche/odbc_lexware.md, Quellen: FubarDev-Mappings,
  Co-Orga, lex-forum) und am echten System zu prüfen (``# TODO-LEXWARE``). Die Spalten werden
  per ``AS <feldname>`` auf unsere Modellfelder gebracht; die Umwandlung in Modelle teilt
  sich das Modul mit ``csv_quelle`` (``zeile_zu_artikel`` usw.).
* :func:`erkunde_schema` / :func:`schreibe_schema_bericht` listen Tabellen und Spalten über
  die ODBC-Katalogfunktionen (``cursor.tables()`` / ``cursor.columns()``) für die Erkundung am
  echten System (CLI ``odbc-erkunden``).

Laut Recherche läuft Lexware premium 2026 in der Regel bereits auf PostgreSQL mit
zertifikatsgesichertem Zugang; der ODBC-Weg ist realistisch nur bei Installationen, die noch
SQL Anywhere verwenden (DSN ``LXSYDSN``). Standardweg bleibt der CSV-Export.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from auftragsautomat.config import OdbcQuelle as OdbcKonfig
from auftragsautomat.models import Artikel, Beleg, Kunde, Lieferant
from auftragsautomat.stammdaten.csv_quelle import StammdatenFehler, baue_modelle

__all__ = [
    "ODBC_KODIERUNG",
    "PYODBC_FEHLT",
    "SQL_ARTIKEL_STANDARD",
    "SQL_BELEGE_STANDARD",
    "SQL_KUNDEN_STANDARD",
    "SQL_LIEFERANTEN_STANDARD",
    "VERBINDUNGS_TIMEOUT_SEKUNDEN",
    "OdbcKonfig",
    "OdbcStammdatenQuelle",
    "StammdatenFehler",
    "erkunde_schema",
    "maskiere_verbindung",
    "pruefe_sql",
    "schema_bericht_markdown",
    "schreibe_schema_bericht",
    "verbindungszeichenfolge",
]

logger = logging.getLogger(__name__)

PYODBC_FEHLT = "pyodbc nicht installiert; pip install .[windows] (nur auf dem Windows-PC mit Lexware)"
VERBINDUNGS_TIMEOUT_SEKUNDEN = 15
# TODO-LEXWARE: SQL Anywhere liefert char-Spalten in Windows-1252; bei PostgreSQL (Unicode-Treiber,
# SQL_WCHAR) ist die Einstellung wirkungslos. Am System prüfen, ob Umlaute korrekt ankommen.
ODBC_KODIERUNG = "cp1252"

# ---------------------------------------------------------------------------
# Standardabfragen (Vorschläge, am echten System zu prüfen)
# ---------------------------------------------------------------------------

# TODO-LEXWARE: FK_Artikel.ArtikelNr/Bezeichnung sind belegt (FubarDev-Mapping); Warengruppe,
# Lieferantennummer, Einheit, Matchcode, EK-Preis, Gesperrt sind aus dem ASCII-Export abgeleitete
# Spaltennamen und unbelegt. VK Preisgruppe 1 = FK_Preismatrix.Vk_preis mit PreisgrpNr = 1 und
# Grundstaffel MengeNr = 0 (Annahme). Unter PostgreSQL sind alle Bezeichner klein (f1.fk_artikel).
SQL_ARTIKEL_STANDARD = """
SELECT a.ArtikelNr          AS artikelnummer,
       a.Bezeichnung        AS bezeichnung,
       a.Warengruppe        AS warengruppe,
       a.LieferantenNr1     AS lieferantennummer,
       a.Einheit            AS einheit,
       a.Matchcode          AS matchcode,
       a.EkPreis1           AS ek_preis,
       pm.Vk_preis          AS vk_preis_1,
       a.Gesperrt           AS gesperrt
FROM   FK_Artikel a
LEFT JOIN FK_Preismatrix pm
       ON pm.ArtikelNr = a.ArtikelNr
      AND pm.PreisgrpNr = 1
      AND pm.MengeNr = 0
"""

# TODO-LEXWARE: FK_Kunde.KundenNr/Anschrift_* und bInaktiv sind belegt (FubarDev-Mapping);
# PreisgrpNr, Anschrift_Land und Matchcode sind Annahmen. "name" = Firma, ersatzweise Name.
SQL_KUNDEN_STANDARD = """
SELECT k.KundenNr                                         AS kundennummer,
       COALESCE(NULLIF(k.Anschrift_Firma, ''), k.Anschrift_Name) AS name,
       k.Anschrift_Name                                   AS name2,
       k.Anschrift_Strasse                                AS strasse,
       k.Anschrift_Plz                                    AS plz,
       k.Anschrift_Ort                                    AS ort,
       k.Anschrift_Land                                   AS land,
       k.PreisgrpNr                                       AS preisgruppe,
       k.Matchcode                                        AS matchcode,
       k.bInaktiv                                         AS inaktiv
FROM   FK_Kunde k
"""

# TODO-LEXWARE: Für FK_Lieferant ist nur der Tabellenname belegt; Spalten analog zu FK_Kunde angenommen.
SQL_LIEFERANTEN_STANDARD = """
SELECT l.LieferantenNr                                    AS lieferantennummer,
       COALESCE(NULLIF(l.Anschrift_Firma, ''), l.Anschrift_Name) AS name,
       l.Anschrift_Name                                   AS name2,
       l.Anschrift_Ort                                    AS ort,
       l.Matchcode                                        AS matchcode,
       l.bInaktiv                                         AS inaktiv
FROM   FK_Lieferant l
"""

# TODO-LEXWARE: FK_Auftrag.AuftragsNr/AuftragsKennung/KundenNr/Datum_erfassung/Summen_netto_haupt sind
# belegt; AuftragsKennung 1 = AB, 2 = Lieferschein, 3 = Rechnung, 5 = Bestellung (Co-Orga-Liste).
# Die Spalte der Kundenbestellnummer ("Bestellnummer") ist NICHT belegt -> am System suchen
# (erkunde_schema, Spalten mit "bestell" im Namen). Ggf. Zeitraum einschränken (Datum_erfassung).
SQL_BELEGE_STANDARD = """
SELECT a.AuftragsKennung    AS belegart,
       a.AuftragsNr         AS belegnummer,
       a.Bestellnummer      AS kundenbestellnummer,
       a.KundenNr           AS partnernummer,
       a.Datum_erfassung    AS datum,
       a.Summen_netto_haupt AS netto
FROM   FK_Auftrag a
WHERE  a.AuftragsKennung IN (1, 2, 3, 5)
"""

_STANDARD_SQL: dict[str, str] = {
    "artikel": SQL_ARTIKEL_STANDARD,
    "kunden": SQL_KUNDEN_STANDARD,
    "lieferanten": SQL_LIEFERANTEN_STANDARD,
    "belege": SQL_BELEGE_STANDARD,
}

# ---------------------------------------------------------------------------
# SQL-Prüfung
# ---------------------------------------------------------------------------

_SCHREIBENDE_WOERTER = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|GRANT|REVOKE|EXEC|EXECUTE|MERGE|CALL|INTO|LOCK)\b",
    re.IGNORECASE,
)


def _ohne_kommentare_und_literale(sql: str) -> str:
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    sql = re.sub(r"--[^\n]*", " ", sql)
    sql = re.sub(r"'(?:[^']|'')*'", "''", sql)
    return sql


def pruefe_sql(sql: str | None, name: str = "SQL") -> str:
    """Stellt sicher, dass ``sql`` eine einzelne, rein lesende SELECT-Anweisung ist.

    Liefert die bereinigte SQL zurück; sonst :class:`StammdatenFehler`.
    """
    if sql is None or not sql.strip():
        raise StammdatenFehler(f"{name}: leere SQL-Abfrage")
    bereinigt = _ohne_kommentare_und_literale(sql).strip()
    if not re.match(r"^SELECT\b", bereinigt, re.IGNORECASE):
        raise StammdatenFehler(
            f"{name}: nur SELECT-Abfragen sind erlaubt (Abfrage beginnt mit {bereinigt[:40]!r})"
        )
    rumpf = bereinigt.rstrip().rstrip(";").strip()
    if ";" in rumpf:
        raise StammdatenFehler(f"{name}: nur eine Anweisung erlaubt (';' gefolgt von weiterem Text)")
    treffer = _SCHREIBENDE_WOERTER.search(rumpf)
    if treffer:
        raise StammdatenFehler(f"{name}: schreibendes Schlüsselwort '{treffer.group(1).upper()}' ist nicht erlaubt")
    return sql.strip()


# ---------------------------------------------------------------------------
# Verbindung
# ---------------------------------------------------------------------------


def _odbc_wert(wert: str) -> str:
    """Werte mit Sonderzeichen (``;``, ``{``, ``}``, Leerraum am Rand) in geschweifte Klammern setzen."""
    if any(z in wert for z in ";{}") or wert != wert.strip():
        return "{" + wert.replace("}", "}}") + "}"
    return wert


def _enthaelt_schluessel(verbindung: str, schluessel: str) -> bool:
    return re.search(rf"(^|;)\s*{schluessel}\s*=", verbindung, re.IGNORECASE) is not None


def verbindungszeichenfolge(konfig: OdbcKonfig) -> str:
    """Baut den ODBC-Connection-String: ``konfig.verbindung`` oder ``DSN=...;UID=...;PWD=...``.

    Das Passwort kommt ausschließlich aus der Umgebungsvariable ``konfig.passwort_env``.
    """
    if konfig.verbindung and konfig.verbindung.strip():
        basis = konfig.verbindung.strip()
    elif konfig.dsn and konfig.dsn.strip():
        basis = f"DSN={_odbc_wert(konfig.dsn.strip())}"
    else:
        raise StammdatenFehler("ODBC-Konfiguration: weder 'dsn' noch 'verbindung' angegeben")
    teile = [basis.rstrip(";")]
    if konfig.benutzer and not _enthaelt_schluessel(basis, "UID"):
        teile.append(f"UID={_odbc_wert(konfig.benutzer)}")
    if konfig.passwort_env:
        passwort = os.environ.get(konfig.passwort_env)
        if passwort is None:
            raise StammdatenFehler(
                f"Umgebungsvariable '{konfig.passwort_env}' mit dem ODBC-Passwort ist nicht gesetzt"
            )
        if not _enthaelt_schluessel(basis, "PWD"):
            teile.append(f"PWD={_odbc_wert(passwort)}")
    return ";".join(teile)


def maskiere_verbindung(verbindung: str | None) -> str:
    """Passwort im Connection-String durch *** ersetzen (für Log und Berichte)."""
    if not verbindung:
        return ""
    maskiert = re.sub(r"((?:^|;)\s*(?:PWD|Password)\s*=\s*)\{(?:[^}]|\}\})*\}", r"\1***", verbindung, flags=re.IGNORECASE)
    return re.sub(r"((?:^|;)\s*(?:PWD|Password)\s*=\s*)[^;]*", r"\1***", maskiert, flags=re.IGNORECASE)


def _importiere_pyodbc() -> Any:
    try:
        import pyodbc  # Windows-only Abhängigkeit (pip install .[windows])
    except ImportError as exc:
        raise StammdatenFehler(PYODBC_FEHLT) from exc
    return pyodbc


def _schliesse(objekt: Any, was: str) -> None:
    schliessen = getattr(objekt, "close", None)
    if schliessen is None:
        return
    try:
        schliessen()
    except Exception as exc:  # Treiberfehler beim Schließen dürfen das Ergebnis nicht verwerfen
        logger.debug("%s konnte nicht sauber geschlossen werden: %s", was, exc)


def _setze_dekodierung(pyodbc: Any, verbindung: Any, kodierung: str = ODBC_KODIERUNG) -> None:
    sql_char = getattr(pyodbc, "SQL_CHAR", None)
    setze = getattr(verbindung, "setdecoding", None)
    if sql_char is None or setze is None:
        return
    try:
        setze(sql_char, encoding=kodierung)
    except Exception as exc:
        logger.debug("Dekodierung %s konnte nicht gesetzt werden: %s", kodierung, exc)


def _verbinde(konfig: OdbcKonfig, firma_id: str | None = None) -> tuple[Any, Any]:
    """Öffnet eine rein lesende Verbindung (``readonly=True``, ``autocommit=False``)."""
    if konfig.nur_lesend is not True:
        raise StammdatenFehler("ODBC-Konfiguration: nur_lesend muss True sein (Lesezugriff ist Pflicht)")
    pyodbc = _importiere_pyodbc()
    verbindung = verbindungszeichenfolge(konfig)
    logger.info(
        "ODBC-Verbindung (nur lesend)%s: %s",
        f" für Firma {firma_id}" if firma_id else "",
        maskiere_verbindung(verbindung),
    )
    try:
        cn = pyodbc.connect(
            verbindung,
            readonly=True,
            autocommit=False,
            timeout=int(konfig.timeout_sekunden or VERBINDUNGS_TIMEOUT_SEKUNDEN),
        )
    except StammdatenFehler:
        raise
    except Exception as exc:  # pyodbc.Error und Treiberfehler
        raise StammdatenFehler(
            f"ODBC-Verbindung fehlgeschlagen ({maskiere_verbindung(verbindung)}): {exc}"
        ) from exc
    _setze_dekodierung(pyodbc, cn, konfig.kodierung or ODBC_KODIERUNG)
    return pyodbc, cn


def _zeilenwert(zeile: Any, attribut: str, index: int) -> Any:
    """Wert einer pyodbc-Zeile per Attribut (``row.table_name``) oder Index."""
    wert = getattr(zeile, attribut, None)
    if wert is not None:
        return wert
    try:
        return zeile[index]
    except (IndexError, TypeError, KeyError):
        return None


def _frage_ab(konfig: OdbcKonfig, sql: str, firma_id: str | None = None) -> list[dict[str, Any]]:
    """Führt eine (geprüfte) SELECT-Abfrage aus und liefert Zeilen als dict {spalte_klein: wert}."""
    _, cn = _verbinde(konfig, firma_id)
    try:
        cursor = cn.cursor()
        try:
            try:
                cursor.execute(sql)
                beschreibung = cursor.description or []
                spalten = [str(d[0]).strip().lower() for d in beschreibung]
                roh = cursor.fetchall()
            except Exception as exc:
                raise StammdatenFehler(f"SQL-Abfrage fehlgeschlagen: {exc}\nSQL: {sql}") from exc
        finally:
            _schliesse(cursor, "Cursor")
    finally:
        # Kein commit(): close() verwirft die lesende Transaktion.
        _schliesse(cn, "Verbindung")
    zeilen = [dict(zip(spalten, tuple(z), strict=False)) for z in roh]
    logger.debug("ODBC: %d Zeilen, Spalten %s", len(zeilen), spalten)
    return zeilen


# ---------------------------------------------------------------------------
# Quelle
# ---------------------------------------------------------------------------


class OdbcStammdatenQuelle:
    """Stammdatenquelle per ODBC (implementiert ``StammdatenQuelle``), ausschließlich SELECT."""

    def __init__(self, firma_id: str, konfig: OdbcKonfig) -> None:
        if konfig is None:
            raise StammdatenFehler(f"Firma {firma_id}: keine ODBC-Konfiguration (stammdaten.odbc) vorhanden")
        if konfig.nur_lesend is not True:
            raise StammdatenFehler(f"Firma {firma_id}: ODBC-Zugriff ist nur lesend erlaubt (nur_lesend=True)")
        if not (konfig.dsn and konfig.dsn.strip()) and not (konfig.verbindung and konfig.verbindung.strip()):
            raise StammdatenFehler(f"Firma {firma_id}: ODBC-Konfiguration braucht 'dsn' oder 'verbindung'")
        self.firma_id = firma_id
        self.konfig = konfig

    def __repr__(self) -> str:
        return f"OdbcStammdatenQuelle({self.firma_id}, {maskiere_verbindung(self.konfig.verbindung or f'DSN={self.konfig.dsn}')})"

    def sql_fuer(self, tabelle: str) -> str:
        """Geprüfte SQL für eine Tabelle: aus der Konfiguration, sonst die Standardabfrage."""
        konfiguriert = getattr(self.konfig, f"sql_{tabelle}", None)
        if konfiguriert and konfiguriert.strip():
            return pruefe_sql(konfiguriert, f"sql_{tabelle}")
        logger.warning(
            "Firma %s: sql_%s nicht konfiguriert; verwende die ungeprüfte Standardabfrage (TODO-LEXWARE)",
            self.firma_id,
            tabelle,
        )
        return pruefe_sql(_STANDARD_SQL[tabelle], f"SQL_{tabelle.upper()}_STANDARD")

    def _lade(self, tabelle: str) -> list:
        sql = self.sql_fuer(tabelle)
        zeilen = _frage_ab(self.konfig, sql, self.firma_id)
        nummeriert: list[tuple[int, Mapping[str, object]]] = list(enumerate(zeilen, start=1))
        return baue_modelle(self.firma_id, tabelle, nummeriert, f"ODBC {tabelle}")

    def artikel(self) -> list[Artikel]:
        return self._lade("artikel")

    def kunden(self) -> list[Kunde]:
        return self._lade("kunden")

    def lieferanten(self) -> list[Lieferant]:
        return self._lade("lieferanten")

    def belege(self) -> list[Beleg]:
        """Belege sind optional: schlägt die Abfrage fehl, läuft die Dublettenprüfung ohne Belege."""
        sql = self.sql_fuer("belege")
        try:
            zeilen = _frage_ab(self.konfig, sql, self.firma_id)
        except StammdatenFehler as exc:
            logger.warning(
                "Firma %s: Belege konnten nicht gelesen werden; Dublettenprüfung läuft ohne Belege: %s",
                self.firma_id,
                exc,
            )
            return []
        nummeriert: list[tuple[int, Mapping[str, object]]] = list(enumerate(zeilen, start=1))
        return baue_modelle(self.firma_id, "belege", nummeriert, "ODBC belege")


# ---------------------------------------------------------------------------
# Schema-Erkundung (für das echte System)
# ---------------------------------------------------------------------------


def erkunde_schema(konfig: OdbcKonfig, *, praefix: str | None = None) -> list[dict[str, Any]]:
    """Listet Tabellen und Spalten über die ODBC-Katalogfunktionen (nur lesend).

    Liefert je Spalte ``{"schema", "tabelle", "tabellentyp", "spalte", "typ", "groesse"}``.
    Systemtabellen werden ausgelassen; ``praefix`` (z. B. ``"FK_"``) filtert Tabellennamen.
    """
    _, cn = _verbinde(konfig)
    eintraege: list[dict[str, Any]] = []
    try:
        cursor = cn.cursor()
        try:
            try:
                tabellen_roh = cursor.tables().fetchall()
            except Exception as exc:
                raise StammdatenFehler(f"ODBC-Katalog (tables) nicht lesbar: {exc}") from exc
            tabellen: list[tuple[str | None, str, str | None]] = []
            for t in tabellen_roh:
                name = _zeilenwert(t, "table_name", 2)
                typ = _zeilenwert(t, "table_type", 3)
                schema = _zeilenwert(t, "table_schem", 1)
                if not name:
                    continue
                if typ and "SYSTEM" in str(typ).upper():
                    continue
                if praefix and not str(name).upper().startswith(praefix.upper()):
                    continue
                tabellen.append((str(schema) if schema else None, str(name), str(typ) if typ else None))
            for schema, name, typ in tabellen:
                try:
                    spalten = cursor.columns(table=name, schema=schema).fetchall()
                except Exception as exc:
                    logger.warning("Spalten der Tabelle %s nicht lesbar: %s", name, exc)
                    continue
                for s in spalten:
                    eintraege.append(
                        {
                            "schema": schema,
                            "tabelle": name,
                            "tabellentyp": typ,
                            "spalte": _zeilenwert(s, "column_name", 3),
                            "typ": _zeilenwert(s, "type_name", 5),
                            "groesse": _zeilenwert(s, "column_size", 6),
                        }
                    )
        finally:
            _schliesse(cursor, "Cursor")
    finally:
        _schliesse(cn, "Verbindung")
    logger.info("ODBC-Schema erkundet: %d Tabellen, %d Spalten", len({(e['schema'], e['tabelle']) for e in eintraege}), len(eintraege))
    return eintraege


_RELEVANTE_TABELLEN = ("artikel", "kunde", "lieferant", "auftrag", "preis", "nummernkreis", "nummerkreis", "warengr")
_RELEVANTE_SPALTEN = (("bestell", "Kundenbestellnummer"), ("preisgr", "Preisgruppe"), ("inaktiv", "Inaktiv"), ("gesperrt", "Gesperrt"))


def schema_bericht_markdown(konfig: OdbcKonfig, eintraege: Sequence[Mapping[str, Any]]) -> str:
    """Markdown-Bericht aus den Einträgen von :func:`erkunde_schema` (ohne Passwort)."""
    quelle = konfig.dsn or maskiere_verbindung(konfig.verbindung) or "(unbekannt)"
    tabellen: dict[tuple[str | None, str], list[Mapping[str, Any]]] = {}
    for e in eintraege:
        tabellen.setdefault((e.get("schema"), str(e.get("tabelle"))), []).append(e)

    zeilen: list[str] = [
        f"# ODBC-Schema (nur lesend): {quelle}",
        "",
        f"Erstellt: {datetime.now():%d.%m.%Y %H:%M}  ",
        f"Tabellen: {len(tabellen)}, Spalten: {len(eintraege)}",
        "",
        "## Hinweise für den Auftrags-Automaten",
        "",
    ]
    relevante = sorted(
        f"{s}.{t}" if s else t for (s, t) in tabellen if any(k in t.lower() for k in _RELEVANTE_TABELLEN)
    )
    zeilen.append(
        "* Tabellen mit Artikel/Kunde/Lieferant/Auftrag/Preis im Namen: "
        + (", ".join(f"`{t}`" for t in relevante) if relevante else "keine gefunden")
    )
    for stichwort, bedeutung in _RELEVANTE_SPALTEN:
        treffer = sorted(
            f"{e['tabelle']}.{e['spalte']}" for e in eintraege if stichwort in str(e.get("spalte") or "").lower()
        )
        zeilen.append(
            f"* Spalten mit \"{stichwort}\" im Namen (Kandidaten für {bedeutung}): "
            + (", ".join(f"`{t}`" for t in treffer) if treffer else "keine gefunden")
        )
    zeilen += ["", "## Tabellen", ""]
    for (schema, name), spalten in sorted(tabellen.items(), key=lambda kv: (kv[0][0] or "", kv[0][1].lower())):
        typ = spalten[0].get("tabellentyp") or "TABLE"
        titel = f"{schema}.{name}" if schema else name
        zeilen += [f"### {titel} ({typ})", "", "| Spalte | Typ | Größe |", "|---|---|---|"]
        for s in spalten:
            groesse = s.get("groesse")
            zeilen.append(f"| {s.get('spalte') or ''} | {s.get('typ') or ''} | {'' if groesse is None else groesse} |")
        zeilen.append("")
    return "\n".join(zeilen).rstrip() + "\n"


def schreibe_schema_bericht(
    konfig: OdbcKonfig,
    ziel: Path,
    *,
    eintraege: Sequence[Mapping[str, Any]] | None = None,
    praefix: str | None = None,
) -> Path:
    """Erkundet das Schema (oder nutzt ``eintraege``) und schreibt einen Markdown-Bericht nach ``ziel``."""
    if eintraege is None:
        eintraege = erkunde_schema(konfig, praefix=praefix)
    ziel = Path(ziel)
    ziel.parent.mkdir(parents=True, exist_ok=True)
    ziel.write_text(schema_bericht_markdown(konfig, eintraege), encoding="utf-8")
    logger.info("Schema-Bericht geschrieben: %s (%d Spalten)", ziel, len(eintraege))
    return ziel
