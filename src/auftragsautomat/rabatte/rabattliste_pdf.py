"""Rabattlisten-PDF einlesen (Quelle 2 der Aktionen) und Aktionen im Gedächtnis ``config/aktionen.yaml`` halten.

:func:`lese_rabattliste` liest eine hochgeladene Rabattliste (Freigabe-UI, Seite "Rabatte", oder CLI
``rabatte-einlesen``) in eine :class:`~auftragsautomat.models.Rabattliste`:

1. **Text-PDF**: je Seite ``extract_tables()`` (pdfplumber); Tabellen mit erkennbarer Kopfzeile
   (gleiche Spaltenerkennung wie die Aktionen-Excel, :mod:`auftragsautomat.rabatte.aktionen_excel`)
   werden zeilenweise zu Aktionen. Liefern die Tabellen nichts, greift ein Zeilen-Regex: eine Zeile
   mit Rabatt (``10 %``, ``-10 %``, ``Rabatt 1,00 €``, ``Aktionspreis 5,90 €``) und Zeitraum
   (``01.11.–30.11.2026``, ``vom 1.11. bis 30.11.2026``) wird zur Aktion; Seitenkopf-Angaben
   ("Kunde:", "Lieferant:"/"Weingut", "Aktionszeitraum …") gelten als Vorgabe für Zeilen ohne eigene
   Angabe. Quelle ``pdf:<Datei>:<Seite>``, ``aktion_id`` zusätzlich mit laufender Nummer.
2. **Ohne Textebene oder ohne Treffer**: Claude Vision mit strukturierter Ausgabe
   (``client.messages.parse(..., output_format=RabattlisteVision)``, Konfidenz je Zeile; unter
   ``matching.konfidenz_schwelle`` -> ``KONFIDENZ_NIEDRIG``). Ohne API-Schlüssel gibt es keinen
   Traceback, sondern einen Prüfhinweis ``FELD_FEHLT``; dasselbe gilt für Ablehnung, abgeschnittene
   Antworten und API-Fehler.

Der Anwender prüft die Tabelle in der UI; :func:`speichere_aktionen` schreibt die übernommenen
Aktionen atomar nach ``rabatte.gedaechtnis_datei`` (YAML, Decimal als Text, Datum ISO, Zusammenführung
nach ``aktion_id``), :func:`lade_gespeicherte_aktionen` liest sie zurück (fehlende Datei = leere Liste).
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import anthropic
import pdfplumber
import yaml
from pydantic import BaseModel, Field, ValidationError

from auftragsautomat.config import Settings
from auftragsautomat.extraktion.text_pdf import ist_text_pdf
from auftragsautomat.extraktion.vision import (
    VisionApiFehler,
    VisionEingabeFehler,
    erzeuge_client,
    erzeuge_inhaltsbloecke,
)
from auftragsautomat.extraktion.zahlen import parse_datum, parse_dezimal, parse_rabatt
from auftragsautomat.models import (
    Aktion,
    Extraktionsmethode,
    Pruefcode,
    Pruefhinweis,
    Rabattliste,
    Schweregrad,
)
from auftragsautomat.rabatte.aktionen_excel import (
    aktion_aus_werten,
    aktion_aus_zeile,
    finde_kopfzeile,
    text_aus,
)

__all__ = [
    "KONFIDENZ_REGEX",
    "KONFIDENZ_TABELLE",
    "RABATTLISTE_BENUTZER_PROMPT",
    "RABATTLISTE_SYSTEM_PROMPT",
    "RabattZeileVision",
    "RabattlisteFehler",
    "RabattlisteVision",
    "aktionen_aus_text",
    "lade_gespeicherte_aktionen",
    "lese_rabattliste",
    "speichere_aktionen",
]

logger = logging.getLogger(__name__)

#: Konfidenz von Aktionen aus pdfplumber-Tabellen bzw. aus dem Zeilen-Regex.
KONFIDENZ_TABELLE = 0.9
KONFIDENZ_REGEX = 0.7
#: Mindestzahl erkannter Überschriften in einer PDF-Tabelle (Tabellen sind oft schmaler als die Excel).
_MINDEST_SPALTEN_PDF = 3

_KOPF = (
    "# Aktionen aus Rabattlisten-PDFs (nach Prüfung in der Freigabe-UI übernommen).\n"
    "# Diese Datei wird vom Auftrags-Automaten geschrieben; Kommentare gehen beim Speichern verloren.\n"
    "# Beträge als Text (Decimal), Datum als JJJJ-MM-TT, Quelle 'pdf:<Datei>:<Seite>'.\n\n"
)


class RabattlisteFehler(ValueError):
    """Die Rabattliste (PDF/YAML) kann nicht gelesen oder geschrieben werden."""


# ---------------------------------------------------------------------------
# Zeilen-Regex
# ---------------------------------------------------------------------------

_DATUM_OHNE_JAHR = r"\d{1,2}\.\s?\d{1,2}\.(?!\s?\d)"
_DATUM_VOLL = r"\d{1,2}\.\s?\d{1,2}\.\s?(?:\d{4}|\d{2})(?!\d)|\d{4}-\d{2}-\d{2}"
_ZEITRAUM = re.compile(
    rf"(?:\b(?:vom|von|ab)\s+)?(?P<von>{_DATUM_VOLL}|{_DATUM_OHNE_JAHR})\s*(?:-|–|—|bis|to)\s*(?P<bis>{_DATUM_VOLL})",
    re.I,
)
_PROZENT = re.compile(r"(?<![\d,.])(?P<vz>[-−–]\s*)?(?P<w>\d{1,2}(?:[.,]\d{1,2})?)\s*%")
_EUR_RABATT = re.compile(
    r"(?:(?:Rabatt|Nachlass|Abzug)\s*:?\s*(?:von\s+)?[-−–]?\s*(?P<w1>\d{1,4}[.,]\d{2})\s*(?:€|EUR|Euro)"
    r"|[-−–]?\s*(?P<w2>\d{1,4}[.,]\d{2})\s*(?:€|EUR|Euro)\s*(?:Rabatt|Nachlass|Abzug))",
    re.I,
)
_EUR_AKTIONSPREIS = re.compile(
    r"(?:(?:Aktionspreis|Sonderpreis|Aktions-?\s?VK|Aktions-?\s?Preis|AP)\s*:?\s*(?P<w1>\d{1,4}[.,]\d{2})\s*(?:€|EUR|Euro)?"
    r"|(?P<w2>\d{1,4}[.,]\d{2})\s*(?:€|EUR|Euro))",
    re.I,
)
_ARTNR = re.compile(r"(?:Art(?:ikel)?\.?\s*-?\s*(?:Nr|No)\.?|Artikelnummer)\s*:?\s*(?P<nr>[A-Za-z0-9][A-Za-z0-9\-_/]*)", re.I)
_KUNDE_ZEILE = re.compile(r"^\s*(?:Kunde|Kunden|Kundengruppe|Für|Fuer)\s*:?\s+(?P<wert>.+?)\s*$", re.I)
_LIEFERANT_ZEILE = re.compile(r"^\s*(?:Lieferant|Weingut|Produzent|Erzeuger)\s*:?\s+(?P<wert>.+?)\s*$", re.I)
_ZEITRAUM_SCHLUESSEL = re.compile(r"(?:Aktionszeitraum|Zeitraum|G[üu]ltig(?:keit)?|Laufzeit)", re.I)
_RABATT_SCHLUESSELWORT = re.compile(r"\b(?:Rabatt|Nachlass|Aktion|Aktionspreis|Sonderpreis|Abzug)\b", re.I)
_MWST = re.compile(r"\b(?:MwSt|USt|VAT|Mehrwertsteuer|Umsatzsteuer)\b", re.I)
_RESTZEICHEN = re.compile(r"^[\s:;,.\-–—()/]+|[\s:;,.\-–—()/]+$")


def _datum_mit_jahr(text: str, bezug: date | None) -> date | None:
    """Datum lesen; fehlt das Jahr ("01.11."), wird es vom Bis-Datum übernommen (Von > Bis -> Vorjahr)."""
    text = text.strip()
    datum = parse_datum(text)
    if datum is not None:
        return datum
    treffer = re.fullmatch(r"(\d{1,2})\.\s?(\d{1,2})\.", text)
    if treffer is None or bezug is None:
        return None
    tag, monat = int(treffer.group(1)), int(treffer.group(2))
    try:
        datum = date(bezug.year, monat, tag)
    except ValueError:
        return None
    if datum > bezug:
        try:
            datum = date(bezug.year - 1, monat, tag)
        except ValueError:
            return None
    return datum


def _zeitraum_aus(text: str) -> tuple[date, date, str] | None:
    treffer = _ZEITRAUM.search(text)
    if treffer is None:
        return None
    bis = parse_datum(treffer.group("bis"))
    if bis is None:
        return None
    von = _datum_mit_jahr(treffer.group("von"), bis)
    if von is None:
        return None
    return von, bis, treffer.group(0)


def _rabatt_aus_text(text: str) -> tuple[dict[str, Any], list[str]]:
    """Rabattangaben (Prozent, EUR, Aktionspreis) einer Zeile plus die gefundenen Textstücke."""
    werte: dict[str, Any] = {}
    funde: list[str] = []
    rabatt = parse_rabatt(text)
    if rabatt is not None:
        werte["rabatt_prozent"] = rabatt.prozent
        funde.append(rabatt.rohtext)
    else:
        prozent = _PROZENT.search(text)
        if prozent is not None and not _MWST.search(text):
            werte["rabatt_prozent"] = prozent.group("w")
            funde.append(prozent.group(0))
    eur = _EUR_RABATT.search(text)
    if eur is not None:
        werte["rabatt_eur"] = eur.group("w1") or eur.group("w2")
        funde.append(eur.group(0))
    else:
        preis = _EUR_AKTIONSPREIS.search(text)
        # Ein ausdrücklicher "Aktionspreis 5,90 €" zählt immer; ein nackter Betrag ("11,50 €") nur,
        # wenn die Zeile keinen Prozentrabatt nennt.
        if preis is not None and (preis.group("w1") or "rabatt_prozent" not in werte):
            werte["aktionspreis"] = preis.group("w1") or preis.group("w2")
            funde.append(preis.group(0))
    return werte, funde


def _artikeltext(text: str, entfernen: Iterable[str]) -> str | None:
    rest = text
    for stueck in entfernen:
        if stueck:
            rest = rest.replace(stueck, " ", 1)
    rest = _RABATT_SCHLUESSELWORT.sub(" ", rest)
    rest = _ZEITRAUM_SCHLUESSEL.sub(" ", rest)
    rest = re.sub(r"\b(?:für|fuer|alle\s+Kunden|je\s+Flasche|pro\s+Flasche)\b", " ", rest, flags=re.I)
    rest = _RESTZEICHEN.sub("", " ".join(rest.split()))
    rest = _RESTZEICHEN.sub("", rest)
    return rest or None


def aktionen_aus_text(
    zeilen: Iterable[str],
    *,
    quelle: str,
    settings: Settings,
    vorgaben: Mapping[str, Any] | None = None,
    start_nr: int = 1,
) -> tuple[list[Aktion], list[Pruefhinweis], dict[str, Any]]:
    """Zeilen-Regex über Textzeilen einer Seite.

    Liefert ``(Aktionen, Hinweise, Vorgaben)``; die Vorgaben (Kunde, Lieferant, Zeitraum aus dem
    Seitenkopf) werden fortgeschrieben, damit Folgeseiten sie weiterverwenden können.
    """
    konfig = settings.rabatte
    aktuelle: dict[str, Any] = dict(vorgaben or {})
    aktionen: list[Aktion] = []
    hinweise: list[Pruefhinweis] = []
    nr = start_nr
    for roh in zeilen:
        zeile = " ".join(str(roh).split())
        if not zeile:
            continue
        kunde = _KUNDE_ZEILE.match(zeile)
        if kunde is not None and not _PROZENT.search(zeile):
            aktuelle["kunde_kurz"] = kunde.group("wert")
            continue
        lieferant = _LIEFERANT_ZEILE.match(zeile)
        if lieferant is not None and not _PROZENT.search(zeile):
            aktuelle["lieferant_kurz"] = lieferant.group("wert")
            continue
        zeitraum = _zeitraum_aus(zeile)
        rabatt, funde = _rabatt_aus_text(zeile)
        if not rabatt:
            if zeitraum is not None and _ZEITRAUM_SCHLUESSEL.search(zeile):
                aktuelle["von"], aktuelle["bis"] = zeitraum[0], zeitraum[1]
                logger.debug("%s: Zeitraum-Vorgabe %s–%s aus %r", quelle, zeitraum[0], zeitraum[1], zeile)
            continue
        werte: dict[str, Any] = dict(rabatt)
        entfernen = list(funde)
        if zeitraum is not None:
            werte["von"], werte["bis"] = zeitraum[0], zeitraum[1]
            entfernen.append(zeitraum[2])
        elif "von" not in aktuelle:
            logger.debug("%s: Zeile %r hat Rabatt, aber keinen Zeitraum", quelle, zeile)
            continue
        artnr = _ARTNR.search(zeile)
        if artnr is not None:
            werte["artikelnummern"] = artnr.group("nr")
            entfernen.append(artnr.group(0))
        werte["artikel_text"] = _artikeltext(zeile, entfernen)
        aktion, zeilen_hinweise = aktion_aus_werten(
            werte,
            aktion_id=f"{quelle}:{nr}",
            quelle=quelle,
            konfig=konfig,
            firmen=settings.firmen,
            vorgaben=aktuelle,
            konfidenz=KONFIDENZ_REGEX,
        )
        hinweise.extend(zeilen_hinweise)
        if aktion is not None:
            aktionen.append(aktion)
            nr += 1
    return aktionen, hinweise, aktuelle


# ---------------------------------------------------------------------------
# Text-PDF: Tabellen + Zeilen
# ---------------------------------------------------------------------------


@dataclass
class _Seite:
    """Inhalt einer PDF-Seite: Text, erkannte Tabellen und der Text außerhalb der Tabellen."""

    nr: int
    text: str
    tabellen: list[list[list[Any]]]
    text_ausserhalb: str


def _text_ausserhalb(seite: Any, bboxes: list[tuple[float, float, float, float]]) -> str:
    """Text der Seite ohne die Zeichen innerhalb der Tabellenrahmen (für den Zeilen-Regex)."""
    if not bboxes:
        return seite.extract_text() or ""

    def ausserhalb(obj: Any) -> bool:
        try:
            x = (obj["x0"] + obj["x1"]) / 2
            y = (obj["top"] + obj["bottom"]) / 2
        except (KeyError, TypeError):
            return True
        return not any(b[0] <= x <= b[2] and b[1] <= y <= b[3] for b in bboxes)

    try:
        return seite.filter(ausserhalb).extract_text() or ""
    except Exception as exc:  # pragma: no cover - pdfminer-Sonderfälle
        logger.debug("Text außerhalb der Tabellen nicht lesbar (%s); nutze den ganzen Seitentext", exc)
        return seite.extract_text() or ""


def _pruefe_lesbar(pdf: Path) -> None:
    """Kaputte oder verschlüsselte PDFs -> :class:`RabattlisteFehler` (statt stillem Vision-Versuch)."""
    try:
        with pdfplumber.open(str(pdf)) as dokument:
            if not dokument.pages:
                raise RabattlisteFehler(f"Rabattliste {pdf} hat keine Seiten.")
    except RabattlisteFehler:
        raise
    except Exception as exc:
        raise RabattlisteFehler(f"Rabattliste {pdf} kann nicht gelesen werden: {exc}") from exc


def _seiten(pdf: Path) -> list[_Seite]:
    """Seiten einer Text-PDF; kaputte PDFs -> :class:`RabattlisteFehler`."""
    seiten: list[_Seite] = []
    try:
        with pdfplumber.open(str(pdf)) as dokument:
            for nr, seite in enumerate(dokument.pages, start=1):
                text = seite.extract_text() or ""
                tabellen: list[list[list[Any]]] = []
                bboxes: list[tuple[float, float, float, float]] = []
                try:
                    for tabelle in seite.find_tables() or []:
                        zeilen = tabelle.extract()
                        if zeilen:
                            tabellen.append(zeilen)
                            bboxes.append(tuple(tabelle.bbox))  # type: ignore[arg-type]
                except Exception as exc:  # pragma: no cover - pdfminer-Sonderfälle
                    logger.warning("Rabattliste %s, Seite %d: Tabellenerkennung fehlgeschlagen (%s)", pdf.name, nr, exc)
                seiten.append(_Seite(nr, text, tabellen, _text_ausserhalb(seite, bboxes)))
    except Exception as exc:
        raise RabattlisteFehler(f"Rabattliste {pdf} kann nicht gelesen werden: {exc}") from exc
    return seiten


_ZELLE_NUMERISCH = re.compile(r"[\d.,:/ %€-]+")


def _zelltext(wert: Any) -> str | None:
    """Zelltext einer PDF-Tabelle: Zeilenumbrüche in Datums-/Zahlenzellen ("01.11.20\\n26") werden
    entfernt, in Textzellen durch ein Leerzeichen ersetzt."""
    if wert is None:
        return None
    text = str(wert)
    kompakt = re.sub(r"\s*\n\s*", "", text).strip()
    if kompakt and _ZELLE_NUMERISCH.fullmatch(kompakt):
        return text_aus(kompakt)
    return text_aus(text)


def _aktionen_aus_tabellen(
    tabellen: list[list[list[Any]]], *, quelle: str, settings: Settings, vorgaben: Mapping[str, Any]
) -> tuple[list[Aktion], list[Pruefhinweis]]:
    aktionen: list[Aktion] = []
    hinweise: list[Pruefhinweis] = []
    nr = 1
    for tabelle in tabellen:
        zeilen = [[_zelltext(z) for z in zeile] for zeile in tabelle]
        kopf = finde_kopfzeile(zeilen, settings.rabatte, mindestens=_MINDEST_SPALTEN_PDF)
        if kopf is None:
            continue
        kopf_index, spalten = kopf
        if "von" not in spalten or "bis" not in spalten:
            # Zeitraum ggf. aus dem Seitenkopf; ohne beides kann die Zeile nicht gelesen werden
            if "von" not in vorgaben:
                continue
        for zeile in zeilen[kopf_index + 1 :]:
            if all(z is None for z in zeile):
                continue
            aktion, zeilen_hinweise = aktion_aus_zeile(
                zeile,
                spalten,
                aktion_id=f"{quelle}:{nr}",
                quelle=quelle,
                konfig=settings.rabatte,
                firmen=settings.firmen,
                vorgaben=vorgaben,
                konfidenz=KONFIDENZ_TABELLE,
            )
            hinweise.extend(zeilen_hinweise)
            if aktion is not None:
                aktionen.append(aktion)
                nr += 1
    return aktionen, hinweise


def _vorgaben_aus_text(text: str, vorgaben: Mapping[str, Any]) -> dict[str, Any]:
    """Kunde/Lieferant/Zeitraum aus den Kopfzeilen einer Seite (vor dem ersten Rabatt)."""
    aktuelle = dict(vorgaben)
    for roh in text.splitlines():
        zeile = " ".join(roh.split())
        if not zeile:
            continue
        kunde = _KUNDE_ZEILE.match(zeile)
        if kunde is not None and not _PROZENT.search(zeile):
            aktuelle["kunde_kurz"] = kunde.group("wert")
            continue
        lieferant = _LIEFERANT_ZEILE.match(zeile)
        if lieferant is not None and not _PROZENT.search(zeile):
            aktuelle["lieferant_kurz"] = lieferant.group("wert")
            continue
        zeitraum = _zeitraum_aus(zeile)
        if zeitraum is not None and _ZEITRAUM_SCHLUESSEL.search(zeile) and not _rabatt_aus_text(zeile)[0]:
            aktuelle["von"], aktuelle["bis"] = zeitraum[0], zeitraum[1]
    return aktuelle


def _lese_text_pdf(pdf: Path, settings: Settings) -> tuple[list[Aktion], list[Pruefhinweis], str]:
    aktionen: list[Aktion] = []
    hinweise: list[Pruefhinweis] = []
    texte: list[str] = []
    vorgaben: dict[str, Any] = {}
    for seite in _seiten(pdf):
        texte.append(seite.text)
        quelle = f"pdf:{pdf.name}:{seite.nr}"
        vorgaben = _vorgaben_aus_text(seite.text_ausserhalb, vorgaben)
        aus_tabellen, tab_hinweise = _aktionen_aus_tabellen(
            seite.tabellen, quelle=quelle, settings=settings, vorgaben=vorgaben
        )
        hinweise.extend(tab_hinweise)
        aktionen.extend(aus_tabellen)
        # Freitext außerhalb der Tabellen (Zeilen mit Zeitraum und Rabatt)
        aus_zeilen, zeilen_hinweise, vorgaben = aktionen_aus_text(
            seite.text_ausserhalb.splitlines(),
            quelle=quelle,
            settings=settings,
            vorgaben=vorgaben,
            start_nr=len(aus_tabellen) + 1,
        )
        hinweise.extend(zeilen_hinweise)
        aktionen.extend(aus_zeilen)
    return aktionen, hinweise, "\n".join(texte)


# ---------------------------------------------------------------------------
# Claude Vision (Scan ohne Textebene oder ohne Treffer)
# ---------------------------------------------------------------------------


# TODO-PRUEFEN: Mit echten (eingescannten) Rabattlisten prüfen, ob Claude ``seite``, ``von``/``bis``
# je Zeile und die Konfidenzen zuverlässig füllt und ob der Listenkopf-Zeitraum korrekt getrennt wird;
# sonst die Felder als Pflichtfelder führen oder den System-Prompt nachschärfen.


class RabattZeileVision(BaseModel):
    """Eine Aktionszeile der Rabattliste, so wie Claude sie gelesen hat."""

    laufnr: int = Field(description="Fortlaufende Nummer der Zeile ab 1 in Leserichtung.")
    seite: int | None = Field(description="Seite des Dokuments, auf der die Zeile steht; null wenn unklar.")
    kunde: str | None = Field(description="Kunde, Kundengruppe oder Region, für die der Rabatt gilt; null wenn für alle Kunden.")
    lieferant: str | None = Field(description="Weingut/Lieferant des Artikels; null wenn nicht genannt.")
    artikel: str | None = Field(description="Artikelbezeichnung wie gedruckt (ohne Preis, Rabatt, Zeitraum); null wenn die Zeile für alle Artikel gilt.")
    artikelnummer: str | None = Field(description="Artikelnummer, falls gedruckt; mehrere kommagetrennt; sonst null.")
    von: str | None = Field(description="Beginn des Aktionszeitraums als 'TT.MM.JJJJ'; null wenn nicht in der Zeile (dann gilt der Zeitraum der Liste).")
    bis: str | None = Field(description="Ende des Aktionszeitraums als 'TT.MM.JJJJ'; null wenn nicht in der Zeile.")
    rabatt_prozent: float | None = Field(description="Rabatt in Prozent als positive Zahl; null wenn kein Prozentrabatt.")
    rabatt_eur: float | None = Field(description="Rabatt in EUR je Flasche, Dezimalpunkt; null wenn keiner.")
    aktionspreis: float | None = Field(description="Aktionspreis in EUR je Flasche, Dezimalpunkt; null wenn keiner.")
    bezeichnung: str | None = Field(description="Name der Aktion, falls genannt; sonst null.")
    bemerkung: str | None = Field(description="Bedingungen oder Zusätze (z. B. 'nur Paletten', '11+1'); sonst null.")
    konfidenz: float = Field(description="Sicherheit beim Lesen der Zeile, 0 bis 1 (1 = sicher).")
    rohtext: str | None = Field(description="Die Zeile wörtlich.")


class RabattlisteVision(BaseModel):
    """Strukturierte Antwort der Claude API für eine Rabattliste."""

    titel: str | None = Field(description="Überschrift der Liste; sonst null.")
    kunde: str | None = Field(description="Kunde/Kundengruppe laut Listenkopf, falls die ganze Liste für ihn gilt; sonst null.")
    lieferant: str | None = Field(description="Weingut/Lieferant laut Listenkopf, falls die ganze Liste von ihm stammt; sonst null.")
    zeitraum_von: str | None = Field(description="Beginn des Aktionszeitraums laut Listenkopf als 'TT.MM.JJJJ'; sonst null.")
    zeitraum_bis: str | None = Field(description="Ende des Aktionszeitraums laut Listenkopf als 'TT.MM.JJJJ'; sonst null.")
    zeilen: list[RabattZeileVision] = Field(description="Alle Aktionszeilen in Leserichtung.")
    anmerkungen: list[str] = Field(description="Auffälligkeiten (unleserliche Stellen, Unklarheiten).")


RABATTLISTE_SYSTEM_PROMPT = """Du bist Sachbearbeiter im Weinhandel und liest Rabattlisten (Aktionslisten) aus.
Das Dokument nennt Aktionszeiträume mit Rabatten je Kunde, Kundengruppe, Weingut oder Artikel.
Übertrage die Liste vollständig und wahrheitsgetreu in das vorgegebene JSON-Schema.

Regeln:
1. Jede Zeile mit einem Rabatt (Prozent, EUR je Flasche oder Aktionspreis) wird eine Zeile in 'zeilen'; laufnr fortlaufend ab 1.
2. Zeitraum je Zeile als 'TT.MM.JJJJ' in von/bis. Steht der Zeitraum nur im Listenkopf, dort in zeitraum_von/zeitraum_bis eintragen und von/bis der Zeile null lassen. Fehlt bei 'von' das Jahr, nimm das Jahr von 'bis'.
3. rabatt_prozent als positive Zahl ('-10 %' -> 10). Ein Mehrwertsteuersatz ist KEIN Rabatt.
4. Beträge mit Dezimalpunkt ohne Währungszeichen. Nichts erfinden: was nicht im Dokument steht, ist null.
5. kunde: Kunde, Kundengruppe oder Region wörtlich; lieferant: Weingut/Erzeuger; artikel: Bezeichnung ohne Preis/Rabatt/Zeitraum; artikelnummer falls gedruckt.
6. konfidenz je Zeile zwischen 0 und 1; unleserliches mit niedriger Konfidenz, aber mit deinem besten Lesen angeben.
7. Texte so belassen, wie sie im Dokument stehen (keine Übersetzung); anmerkungen für Unklarheiten."""

RABATTLISTE_BENUTZER_PROMPT = (
    "Lies die angehängte Rabattliste vollständig aus: jede Aktionszeile mit Kunde/Kundengruppe, "
    "Weingut, Artikel, Artikelnummer, Zeitraum und Rabatt (Prozent, EUR oder Aktionspreis), Konfidenz je Zeile. "
    "Antworte ausschließlich im vorgegebenen JSON-Schema."
)


def _zeitlimit_sekunden(max_tokens: int) -> float:
    return min(3600.0, max(600.0, 3600.0 * max_tokens / 128_000))


def _api_hinweis(text: str, grad: Schweregrad = Schweregrad.FEHLER) -> Pruefhinweis:
    return Pruefhinweis(code=Pruefcode.FELD_FEHLT, schweregrad=grad, text=text, details={"feld": "vision"})


def _frage_claude(
    pdf: Path, settings: Settings, client: Any | None
) -> tuple[RabattlisteVision | None, list[Pruefhinweis]]:
    """Ruft die Claude API auf; jeder Fehler wird zum Prüfhinweis (kein Traceback in UI/CLI)."""
    claude = settings.claude
    if client is None:
        try:
            client = erzeuge_client(settings)
        except VisionApiFehler as exc:
            logger.warning("Rabattliste %s: Claude Vision nicht verfügbar – %s", pdf.name, exc)
            return None, [_api_hinweis(f"Rabattliste {pdf.name}: Claude Vision nicht verfügbar – {exc}")]
    try:
        bloecke = erzeuge_inhaltsbloecke(pdf)
    except VisionEingabeFehler as exc:
        return None, [_api_hinweis(f"Rabattliste {pdf.name}: {exc}")]
    anfrage: dict[str, Any] = {
        "model": claude.modell,
        "max_tokens": claude.max_tokens,
        "system": RABATTLISTE_SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": [*bloecke, {"type": "text", "text": RABATTLISTE_BENUTZER_PROMPT}]}],
        "output_format": RabattlisteVision,
        "output_config": {"effort": claude.effort},
        "timeout": _zeitlimit_sekunden(claude.max_tokens),
    }
    logger.info("Sende Rabattliste %s an die Claude API (Modell %s, effort %s)", pdf.name, claude.modell, claude.effort)
    try:
        antwort = client.messages.parse(**anfrage)
    except TypeError as exc:
        return None, [_api_hinweis(f"Claude API: Anmeldung nicht möglich ({exc}) – API-Schlüssel in '{claude.api_key_env}' setzen.")]
    except anthropic.AuthenticationError as exc:
        return None, [_api_hinweis(f"Claude API: Anmeldung fehlgeschlagen – API-Schlüssel in '{claude.api_key_env}' prüfen ({exc.message}).")]
    except anthropic.RateLimitError:
        return None, [_api_hinweis("Claude API: Ratenlimit erreicht – bitte später erneut versuchen.")]
    except anthropic.APIStatusError as exc:
        return None, [_api_hinweis(f"Claude API: Fehler {exc.status_code} – {exc.message}")]
    except anthropic.APIConnectionError as exc:
        return None, [_api_hinweis(f"Claude API nicht erreichbar – Netzwerk/Proxy prüfen ({exc.__class__.__name__}: {exc}).")]

    stop_reason = getattr(antwort, "stop_reason", None)
    if stop_reason == "refusal":
        details = getattr(antwort, "stop_details", None)
        erklaerung = getattr(details, "explanation", None) or "keine Begründung übermittelt"
        return None, [_api_hinweis(f"Claude hat die Verarbeitung der Rabattliste abgelehnt: {erklaerung}")]
    if stop_reason in ("max_tokens", "model_context_window_exceeded"):
        return None, [_api_hinweis(f"Claude-Antwort wurde abgeschnitten (stop_reason={stop_reason}) – Dokument aufteilen oder max_tokens erhöhen.")]
    ergebnis = getattr(antwort, "parsed_output", None)
    if ergebnis is None:
        return None, [_api_hinweis(f"Claude lieferte keine strukturierte Ausgabe (stop_reason={stop_reason}).")]
    if not isinstance(ergebnis, RabattlisteVision):
        try:
            ergebnis = RabattlisteVision.model_validate(ergebnis)
        except ValidationError as exc:
            return None, [_api_hinweis(f"Claude-Antwort passt nicht zum Schema: {exc.error_count()} Fehler.")]
    return ergebnis, []


def _zahl_text(wert: float | None) -> str | None:
    if wert is None or isinstance(wert, bool):
        return None
    return str(Decimal(str(wert)))


def _aktionen_aus_vision(
    ergebnis: RabattlisteVision, pdf: Path, settings: Settings
) -> tuple[list[Aktion], list[Pruefhinweis], str]:
    vorgaben: dict[str, Any] = {}
    for feld, wert in (
        ("kunde_kurz", ergebnis.kunde),
        ("lieferant_kurz", ergebnis.lieferant),
        ("von", ergebnis.zeitraum_von),
        ("bis", ergebnis.zeitraum_bis),
    ):
        if text_aus(wert):
            vorgaben[feld] = wert
    schwelle = settings.matching.konfidenz_schwelle
    aktionen: list[Aktion] = []
    hinweise: list[Pruefhinweis] = []
    zeilen_text: list[str] = []
    laufende: dict[int, int] = {}
    for zeile in ergebnis.zeilen:
        seite = zeile.seite if zeile.seite and zeile.seite > 0 else 1
        laufende[seite] = laufende.get(seite, 0) + 1
        quelle = f"pdf:{pdf.name}:{seite}"
        werte = {
            "kunde_kurz": zeile.kunde,
            "lieferant_kurz": zeile.lieferant,
            "artikel_text": zeile.artikel,
            "artikelnummern": zeile.artikelnummer,
            "von": zeile.von,
            "bis": zeile.bis,
            "rabatt_prozent": _zahl_text(zeile.rabatt_prozent),
            "rabatt_eur": _zahl_text(zeile.rabatt_eur),
            "aktionspreis": _zahl_text(zeile.aktionspreis),
            "bezeichnung": zeile.bezeichnung,
            "bemerkung": zeile.bemerkung,
        }
        konfidenz = max(0.0, min(1.0, float(zeile.konfidenz)))
        aktion, zeilen_hinweise = aktion_aus_werten(
            werte,
            aktion_id=f"{quelle}:{laufende[seite]}",
            quelle=quelle,
            konfig=settings.rabatte,
            firmen=settings.firmen,
            vorgaben=vorgaben,
            konfidenz=konfidenz,
        )
        hinweise.extend(zeilen_hinweise)
        if zeile.rohtext:
            zeilen_text.append(zeile.rohtext)
        if aktion is None:
            continue
        aktionen.append(aktion)
        if konfidenz < schwelle:
            hinweise.append(
                Pruefhinweis(
                    code=Pruefcode.KONFIDENZ_NIEDRIG,
                    schweregrad=Schweregrad.WARNUNG,
                    text=(
                        f"{aktion.aktion_id}: Zeile unsicher gelesen (Konfidenz {konfidenz * 100:.0f} % < "
                        f"{schwelle * 100:.0f} %) – bitte prüfen: {zeile.rohtext or aktion.artikel_text or '-'}"
                    ),
                    details={"aktion_id": aktion.aktion_id, "konfidenz": f"{konfidenz:.2f}", "schwelle": f"{schwelle:.2f}"},
                )
            )
    anmerkungen = [a for a in (text_aus(x) for x in ergebnis.anmerkungen) if a]
    for anmerkung in anmerkungen:
        logger.info("Anmerkung von Claude zu %s: %s", pdf.name, anmerkung)
    if anmerkungen:
        zeilen_text.append("Anmerkungen: " + " | ".join(anmerkungen))
    return aktionen, hinweise, "\n".join(zeilen_text)


# ---------------------------------------------------------------------------
# Öffentlich: PDF lesen
# ---------------------------------------------------------------------------


def lese_rabattliste(
    pdf: Path, settings: Settings, *, client: Any | None = None, vision_erzwingen: bool = False
) -> Rabattliste:
    """Liest eine Rabattlisten-PDF (siehe Modul-Docstring). Fehlende Datei -> ``FileNotFoundError``,
    kaputte Datei -> :class:`RabattlisteFehler`; alles andere landet in ``Rabattliste.hinweise``."""
    pdf = Path(pdf)
    if not pdf.is_file():
        raise FileNotFoundError(f"Rabattliste nicht gefunden: {pdf}")
    jetzt = datetime.now().astimezone()
    aktionen: list[Aktion] = []
    hinweise: list[Pruefhinweis] = []
    rohtext = ""
    methode = Extraktionsmethode.PDFPLUMBER
    ist_pdf = pdf.suffix.lower() == ".pdf"
    if ist_pdf:
        _pruefe_lesbar(pdf)
    hat_text = ist_pdf and not vision_erzwingen and ist_text_pdf(pdf)
    if hat_text:
        aktionen, hinweise, rohtext = _lese_text_pdf(pdf, settings)
        logger.info("Rabattliste %s: %d Aktionen aus der Textebene, %d Hinweise", pdf.name, len(aktionen), len(hinweise))
    if not aktionen:
        if hat_text:
            logger.info("Rabattliste %s: keine Aktionen aus der Textebene – versuche Claude Vision", pdf.name)
        ergebnis, api_hinweise = _frage_claude(pdf, settings, client)
        hinweise.extend(api_hinweise)
        if ergebnis is not None:
            methode = Extraktionsmethode.CLAUDE_VISION
            aktionen, vision_hinweise, vision_text = _aktionen_aus_vision(ergebnis, pdf, settings)
            hinweise.extend(vision_hinweise)
            rohtext = rohtext or vision_text
            logger.info("Rabattliste %s: %d Aktionen über Claude Vision", pdf.name, len(aktionen))
    if not aktionen and not any(h.schweregrad == Schweregrad.FEHLER for h in hinweise):
        hinweise.append(
            Pruefhinweis(
                code=Pruefcode.FELD_FEHLT,
                schweregrad=Schweregrad.FEHLER,
                text=f"Rabattliste {pdf.name}: keine Aktionszeile mit Zeitraum und Rabatt erkannt – Dokument prüfen.",
                details={"feld": "aktionen"},
            )
        )
    return Rabattliste(
        quelldatei=pdf,
        extraktionsmethode=methode,
        aktionen=aktionen,
        hinweise=hinweise,
        rohtext=rohtext or None,
        eingelesen_am=jetzt,
    )


# ---------------------------------------------------------------------------
# Gedächtnis: config/aktionen.yaml
# ---------------------------------------------------------------------------


def _als_yaml(aktion: Aktion) -> dict[str, Any]:
    """Aktion als YAML-taugliche Zuordnung: Decimal als Text, Datum ISO, leere Felder weggelassen."""
    daten: dict[str, Any] = {}
    for feld, wert in aktion.model_dump().items():
        if wert is None or wert == [] or (feld == "aktiv" and wert is True):
            continue
        if isinstance(wert, Decimal):
            wert = format(wert, "f")
        elif isinstance(wert, date):
            wert = wert.isoformat()
        daten[feld] = wert
    return daten


def speichere_aktionen(aktionen: Iterable[Aktion], datei: Path, *, ersetzen: bool = False) -> Path:
    """Schreibt Aktionen atomar nach ``datei`` (YAML).

    Standard: Zusammenführen mit dem vorhandenen Inhalt nach ``aktion_id`` (neue Fassung gewinnt,
    Reihenfolge der vorhandenen bleibt); ``ersetzen=True`` schreibt nur die übergebenen Aktionen.
    """
    datei = Path(datei)
    neue = list(aktionen)
    vorhandene: list[Aktion] = [] if ersetzen else lade_gespeicherte_aktionen(datei)
    nach_id: dict[str, Aktion] = {a.aktion_id: a for a in vorhandene}
    for aktion in neue:
        nach_id[aktion.aktion_id] = aktion
    text = _KOPF + yaml.safe_dump(
        {"aktionen": [_als_yaml(a) for a in nach_id.values()]},
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        width=100,
    )
    datei.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=datei.name + ".", suffix=".tmp", dir=datei.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(temp, datei)
    except OSError as exc:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise RabattlisteFehler(f"Aktionen können nicht nach {datei} geschrieben werden: {exc}") from exc
    logger.info("%d Aktionen gespeichert nach %s (%d neu/aktualisiert)", len(nach_id), datei, len(neue))
    return datei


def lade_gespeicherte_aktionen(datei: Path) -> list[Aktion]:
    """Liest ``config/aktionen.yaml``; fehlende Datei = leere Liste, ungültige Datei ->
    :class:`RabattlisteFehler`; einzelne ungültige Einträge werden mit Warnung übersprungen."""
    datei = Path(datei)
    if not datei.is_file():
        logger.info("Keine gespeicherten Aktionen (%s fehlt)", datei)
        return []
    try:
        daten = yaml.safe_load(datei.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise RabattlisteFehler(f"Gespeicherte Aktionen {datei} nicht lesbar: {exc}") from exc
    if daten is None:
        return []
    if not isinstance(daten, Mapping):
        raise RabattlisteFehler(f"{datei}: oberste Ebene muss eine Zuordnung mit 'aktionen' sein")
    roh = daten.get("aktionen") or []
    if not isinstance(roh, list):
        raise RabattlisteFehler(f"{datei}: 'aktionen' muss eine Liste sein")
    aktionen: list[Aktion] = []
    for index, eintrag in enumerate(roh, start=1):
        if not isinstance(eintrag, Mapping):
            logger.warning("%s: Eintrag %d ist keine Zuordnung und wird übersprungen", datei, index)
            continue
        werte = dict(eintrag)
        for feld in ("rabatt_prozent", "rabatt_eur", "aktionspreis"):
            if isinstance(werte.get(feld), float):
                werte[feld] = str(werte[feld])
            elif isinstance(werte.get(feld), str):
                werte[feld] = parse_dezimal(werte[feld])
        try:
            aktionen.append(Aktion.model_validate(werte))
        except ValidationError as exc:
            logger.warning("%s: Eintrag %d (%s) ungültig und übersprungen: %s", datei, index, werte.get("aktion_id", "?"), exc)
    logger.info("%d gespeicherte Aktionen aus %s geladen", len(aktionen), datei)
    return aktionen
