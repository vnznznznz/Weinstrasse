"""Vision-Extraktion eingescannter Preislisten mit handschriftlichen Mengen (Eingangstyp C).

Das Dokument (PDF ohne Textebene oder Bilddatei) geht als Base64-Block an die Claude API.
Die Antwort kommt als strukturierte Ausgabe zurück (``client.messages.parse(...,
output_format=VisionErgebnis)``) und wird in eine :class:`RohBestellung` übersetzt.

Besonderheiten gegenüber der Text-Extraktion (``text_pdf``):

* Jede Zeile der Preisliste wird zur Position, auch ohne handschriftliche Menge
  (``menge=None``). Nur Positionen mit Menge erhalten Prüfhinweise.
* Konfidenzen (0..1) für Menge und Preis; unter ``settings.matching.konfidenz_schwelle``
  -> ``Pruefhinweis(KONFIDENZ_NIEDRIG, WARNUNG)`` für die Position.
* Durchgestrichene Zeilen/Preise bleiben mit ``durchgestrichen=True`` erhalten
  -> ``Pruefhinweis(DURCHGESTRICHEN, WARNUNG)``; ihre Menge zählt nicht zur Gesamtmenge
  (siehe ``RohBestellung.gesamtmenge``).
* Summenvermerke ("180 R", "Gesamt 72") -> :class:`Summenvermerk` + ``Pruefhinweis(SUMMENVERMERK, INFO)``.

API-Vorgaben: Modell ``settings.claude.modell``, ``max_tokens`` und ``output_config={"effort": ...}``
aus der Konfiguration; kein ``thinking``-, ``temperature``- oder ``tool_choice``-Parameter, kein Prefill.
``stop_reason == "refusal"`` -> :class:`VisionAbgelehnt`, ``"max_tokens"`` -> :class:`VisionAbgeschnitten`.

In Tests wird der Client über ``VisionExtraktor(settings, client=...)`` injiziert; es gibt keine
echten API-Aufrufe.
"""

from __future__ import annotations

import base64
import io
import logging
import math
import os
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import anthropic
import pdfplumber
from PIL import Image
from pydantic import BaseModel, Field

from auftragsautomat.config import Settings
from auftragsautomat.extraktion.text_pdf import (
    SPEDITIONSZEILE_HINWEIS,
    RevisionTreffer,
    finde_aktionshinweis,
    finde_revision,
    ist_speditionszeile,
    revisions_hinweis,
)
from auftragsautomat.extraktion.zahlen import glatt, parse_datum, parse_dezimal, parse_rabatt
from auftragsautomat.matching.volumen import parse_jahrgang, parse_volumen_cl
from auftragsautomat.models import (
    Eingangstyp,
    Extraktionsmethode,
    Pruefcode,
    Pruefhinweis,
    RohBestellung,
    RohPosition,
    Schweregrad,
    Summenvermerk,
)

__all__ = [
    "BENUTZER_PROMPT",
    "BILD_ENDUNGEN",
    "SYSTEM_PROMPT",
    "UNTERSTUETZTE_ENDUNGEN",
    "VisionAbgelehnt",
    "VisionAbgeschnitten",
    "VisionApiFehler",
    "VisionEingabeFehler",
    "VisionErgebnis",
    "VisionExtraktor",
    "VisionFehler",
    "VisionPosition",
    "VisionSummenvermerk",
    "erzeuge_client",
    "erzeuge_inhaltsbloecke",
    "pdf_hat_textebene",
    "wandle_in_rohbestellung",
]

logger = logging.getLogger(__name__)

# Grenzen der Claude API (Dokument-/Bild-Eingabe). Werte laut API-Referenz, Stand Oktober 2026.
MAX_PDF_BYTES = 32 * 1024 * 1024  # Anfragegröße insgesamt 32 MB
MAX_PDF_SEITEN = 100  # ab hier Warnung (harte Grenze je nach Kontextfenster 100 bzw. 600 Seiten)
MAX_BILD_BYTES = 5 * 1024 * 1024  # 5 MB je Bild
MAX_BILD_PIXEL = 8000  # längste Kante
ZIEL_BILD_PIXEL = 4000  # längste Kante nach Verkleinerung
MAX_BILDER_PRO_ANFRAGE = 100

_BILD_MEDIA_TYPES: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}
_BILD_ENDUNGEN_KONVERTIEREN = frozenset({".tif", ".tiff", ".bmp"})
BILD_ENDUNGEN = frozenset(_BILD_MEDIA_TYPES) | _BILD_ENDUNGEN_KONVERTIEREN
UNTERSTUETZTE_ENDUNGEN = BILD_ENDUNGEN | {".pdf"}

# Prüfung auf Textebene: so viele Seiten werden gelesen, ab so vielen Buchstaben/Ziffern gilt
# eine Seite als Textseite (Scanner-OCR-Reste von wenigen Zeichen zählen nicht).
_TEXTPRUEFUNG_SEITEN = 3
_TEXTPRUEFUNG_MIN_ZEICHEN = 20


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class VisionFehler(Exception):
    """Basisklasse aller Fehler der Vision-Extraktion."""


class VisionEingabeFehler(VisionFehler):
    """Datei fehlt, ist kaputt oder hat einen nicht unterstützten Typ."""


class VisionApiFehler(VisionFehler):
    """Fehler beim Aufruf der Claude API (Anmeldung, Ratenlimit, Server, Netzwerk)."""


class VisionAbgelehnt(VisionFehler):
    """Claude hat die Verarbeitung abgelehnt (``stop_reason == "refusal"``)."""

    def __init__(self, kategorie: str | None, erklaerung: str | None) -> None:
        self.kategorie = kategorie
        self.erklaerung = erklaerung
        super().__init__(
            "Claude hat die Verarbeitung des Dokuments abgelehnt "
            f"(Kategorie: {kategorie or 'unbekannt'}): {erklaerung or 'keine Begründung übermittelt'}"
        )


class VisionAbgeschnitten(VisionFehler):
    """Die Antwort wurde abgeschnitten (``stop_reason == "max_tokens"`` o. ä.)."""


# ---------------------------------------------------------------------------
# Strukturierte Ausgabe (JSON-Schema für die API)
# ---------------------------------------------------------------------------
#
# Für das JSON-Schema der API werden Zahlen als number/integer geführt (kein Decimal);
# die Umwandlung in Decimal passiert in ``wandle_in_rohbestellung``. Die Kernfelder sind
# Pflicht (ohne Default), damit das Modell jedes Feld explizit – ggf. mit null – ausgibt.
# Die Felder zu Rabatt, Aktion und Revised Order (Ausbaustufe 2) haben Defaults; fehlen sie
# in der Antwort, greift die deterministische Erkennung über ``zahlen.parse_rabatt`` bzw.
# ``text_pdf.finde_revision``/``finde_aktionshinweis`` auf Rohtext und Anmerkungen.
# TODO-PRUEFEN: Am echten Tenant prüfen, ob Claude die optionalen Felder rabatt_prozent /
# ist_revision / aktionshinweis zuverlässig füllt; sonst als Pflichtfelder führen.


class VisionPosition(BaseModel):
    """Eine Zeile der Preisliste, so wie Claude sie gelesen hat."""

    laufnr: int = Field(description="Fortlaufende Nummer der Zeile ab 1 in Leserichtung.")
    bezeichnung: str = Field(
        description="Artikelbezeichnung wie gedruckt, ohne Artikelnummer, Jahrgang, Volumen, Preis und Menge."
    )
    jahrgang: int | None = Field(description="Vierstelliger Jahrgang, sonst null.")
    menge_text: str | None = Field(
        description="Handschriftliche Bestellmenge wörtlich so, wie sie gelesen wurde (z. B. '12', '6 Kt'); null wenn keine Menge eingetragen ist."
    )
    menge: float | None = Field(
        description="Handschriftliche Bestellmenge als Zahl (Flaschen bzw. in der angegebenen Einheit); null wenn keine Menge eingetragen ist."
    )
    einheit: str | None = Field(description="Einheit der Menge, z. B. 'Fl', 'Kt', 'Stk'; null wenn nicht erkennbar.")
    preis: float | None = Field(
        description="Einzelpreis netto in EUR wie gedruckt, Dezimalpunkt statt Komma; null wenn kein Preis in der Zeile steht."
    )
    volumen_text: str | None = Field(description="Flaschenvolumen wie gedruckt, z. B. '0,75 l', '1,5 l', 'Magnum'; sonst null.")
    artikelnummer: str | None = Field(description="Artikelnummer des Lieferanten, falls in der Zeile gedruckt; sonst null.")
    hinweis: str | None = Field(description="Zusätze wie '11+1', 'Restposten', 'nur solange Vorrat'; sonst null.")
    durchgestrichen: bool = Field(description="true, wenn die Zeile oder der Preis durchgestrichen ist.")
    konfidenz_menge: float = Field(description="Sicherheit beim Lesen der Menge, 0 bis 1 (1 = sicher). 1.0, wenn keine Menge eingetragen ist.")
    konfidenz_preis: float | None = Field(description="Sicherheit beim Lesen des Preises, 0 bis 1; null wenn kein Preis.")
    rohtext: str | None = Field(description="Die komplette Zeile wörtlich, inklusive handschriftlicher Einträge.")
    rabatt_prozent: float | None = Field(
        default=None,
        description="Positionsrabatt in Prozent, falls in der Zeile ein Rabatt steht (z. B. 'Rabatt 10 %', '-10%', 'Aktion -10 %'); positiv angeben; null wenn kein Rabatt. Ein Mehrwertsteuersatz ist KEIN Rabatt.",
    )
    rabatt_text: str | None = Field(
        default=None, description="Der Rabatt wörtlich, wie er in der Zeile steht (z. B. 'Aktion -10 %'); null wenn kein Rabatt."
    )


class VisionSummenvermerk(BaseModel):
    """Handschriftlicher oder gedruckter Summenvermerk, z. B. '180 R' oder 'Gesamt 72'."""

    wert: float = Field(description="Die Zahl des Summenvermerks.")
    einheit: str | None = Field(description="Einheit oder Kürzel hinter der Zahl, z. B. 'R', 'Fl', 'Kt'; sonst null.")
    rohtext: str = Field(description="Der Summenvermerk wörtlich.")
    konfidenz: float = Field(description="Sicherheit beim Lesen, 0 bis 1.")


class VisionErgebnis(BaseModel):
    """Strukturierte Antwort der Claude API für eine Preisliste."""

    kunde_text: str | None = Field(description="Besteller / Kunde, falls im Dokument erkennbar; sonst null.")
    lieferant_text: str | None = Field(description="Weingut oder Händler, von dem die Preisliste stammt; sonst null.")
    belegnummer: str | None = Field(description="Belegnummer des Dokuments, falls vorhanden; sonst null.")
    bestellnummer: str | None = Field(description="Bestellnummer, falls vorhanden; sonst null.")
    datum: str | None = Field(description="Datum des Dokuments als 'TT.MM.JJJJ'; sonst null.")
    liefertag: str | None = Field(description="Gewünschter Liefertag als 'TT.MM.JJJJ'; sonst null.")
    lieferadresse: str | None = Field(description="Lieferadresse, falls genannt; sonst null.")
    positionen: list[VisionPosition] = Field(description="Alle Zeilen der Preisliste in Leserichtung.")
    summenvermerke: list[VisionSummenvermerk] = Field(description="Alle Summenvermerke im Dokument.")
    seitenzahl: int | None = Field(description="Anzahl der Seiten des Dokuments; null wenn unklar.")
    anmerkungen: list[str] = Field(description="Auffälligkeiten (unleserliche Stellen, Notizen am Rand, Unklarheiten).")
    aktionshinweis: str | None = Field(
        default=None,
        description="Aktionshinweis auf Belegebene wörtlich, z. B. 'Aktion KW 45', 'Promo', 'Aktionszeitraum 01.11.-30.11.'; null wenn keiner.",
    )
    ist_revision: bool = Field(
        default=False,
        description="true, wenn das Dokument eine geänderte/ersetzende Bestellung ist ('Revised order', 'REVISED', 'Änderung zur Bestellung', 'ersetzt Bestellung Nr. …', 'Storno + Neu').",
    )
    revision_von: str | None = Field(
        default=None,
        description="Nummer der ersetzten Bestellung, falls genannt ('ersetzt Bestellung Nr. 123' -> '123'); sonst null.",
    )


SYSTEM_PROMPT = """Du bist Sachbearbeiter im Weinhandel und liest eingescannte Preislisten aus.
Das Dokument ist eine Preisliste eines Weinguts oder Händlers, in die jemand Bestellmengen handschriftlich eingetragen hat.
Deine Aufgabe ist es, die Preisliste vollständig und wahrheitsgetreu in das vorgegebene JSON-Schema zu übertragen.

Regeln:
1. Gib JEDE Zeile der Preisliste als Position aus – auch Zeilen ohne eingetragene Menge (dann menge = null und menge_text = null). Nummeriere laufnr fortlaufend ab 1 in Leserichtung.
2. Handschriftliche Mengen: menge_text wörtlich so, wie du es liest; menge als Zahl. konfidenz_menge zwischen 0 und 1 gibt an, wie sicher du die Menge gelesen hast. Unleserliches bekommt eine niedrige Konfidenz (unter 0,5), ist aber trotzdem mit deinem besten Lesen anzugeben.
3. Durchgestrichene Zeilen oder Preise: durchgestrichen = true setzen und den sichtbaren Wert trotzdem angeben.
4. Summenvermerke wie "180 R", "Gesamt 72", "Σ 48 Fl." in summenvermerke aufnehmen: wert als Zahl, einheit bzw. Kürzel getrennt, rohtext wörtlich.
5. Nichts erfinden: Was nicht im Dokument steht, ist null. Keine Mengen aus Preisen oder Summen ableiten.
6. Jahrgang, Volumen (z. B. "0,75 l") und Artikelnummer in die eigenen Felder schreiben; bezeichnung enthält nur den Artikelnamen wie gedruckt.
7. Preise netto so übernehmen, wie sie gedruckt sind (keine Umrechnung, keine Mehrwertsteuer). preis mit Dezimalpunkt; der Originaltext steht im rohtext.
8. Kopfdaten: kunde_text (Besteller/Empfänger, falls erkennbar), lieferant_text (Weingut/Händler der Preisliste), belegnummer, bestellnummer, datum und liefertag als "TT.MM.JJJJ", lieferadresse.
9. hinweis für Zusätze wie "11+1" oder "Restposten"; anmerkungen für alles, was unklar ist (unleserliche Stellen, Randnotizen, mehrere Mengen in einer Zeile).
10. Texte so belassen, wie sie im Dokument stehen (Deutsch oder Italienisch), keine Übersetzung.
11. Rabatte: Steht in einer Zeile ein Positionsrabatt ("Rabatt 10 %", "-10%", "Aktion -10 %", "Aktionsrabatt 10%"), rabatt_prozent als positive Zahl und rabatt_text wörtlich angeben. Ein Mehrwertsteuersatz ("19 %") ist kein Rabatt.
12. Aktionshinweis auf Belegebene ("Aktion KW 45", "Promo", "Aktionszeitraum 01.11.-30.11.") wörtlich in aktionshinweis; sonst null.
13. Revised Order: Ist das Dokument eine geänderte oder ersetzende Bestellung ("Revised order", "REVISED", "Änderung zur Bestellung", "ersetzt Bestellung Nr. 123", "Storno + Neu"), ist_revision = true und revision_von = die genannte Nummer der ersetzten Bestellung (sonst null)."""

BENUTZER_PROMPT = (
    "Lies die angehängte eingescannte Preisliste vollständig aus. Jede Zeile wird eine Position, "
    "handschriftliche Mengen mit Konfidenz, durchgestrichene Werte markieren, Summenvermerke erfassen. "
    "Antworte ausschließlich im vorgegebenen JSON-Schema."
)


# ---------------------------------------------------------------------------
# Hilfsfunktionen: Datei -> Inhaltsblöcke
# ---------------------------------------------------------------------------


def _base64(daten: bytes) -> str:
    """Base64 ohne Zeilenumbrüche (so erwartet es die API)."""
    return base64.standard_b64encode(daten).decode("ascii")


def _pdf_info(datei: Path) -> tuple[int, bool]:
    """Liefert (Seitenzahl, hat Textebene) über pdfplumber. Kaputte PDFs -> VisionEingabeFehler."""
    try:
        with pdfplumber.open(str(datei)) as pdf:
            seiten = len(pdf.pages)
            for seite in pdf.pages[:_TEXTPRUEFUNG_SEITEN]:
                text = seite.extract_text() or ""
                if sum(1 for zeichen in text if zeichen.isalnum()) >= _TEXTPRUEFUNG_MIN_ZEICHEN:
                    return seiten, True
            return seiten, False
    except Exception as exc:  # pdfminer wirft je nach Defekt unterschiedliche Exceptions
        raise VisionEingabeFehler(f"PDF {datei.name} kann nicht gelesen werden: {exc}") from exc


def pdf_hat_textebene(datei: Path) -> bool:
    """True, wenn mindestens eine der ersten Seiten eine brauchbare Textebene hat.

    Lokale Prüfung mit pdfplumber (bewusst nicht aus ``text_pdf`` importiert, damit dieses Modul
    unabhängig bleibt). Kaputte PDFs -> :class:`VisionEingabeFehler`.
    """
    return _pdf_info(Path(datei))[1]


def _bild_nach_bytes(bild: Image.Image) -> tuple[str, bytes]:
    """Kodiert ein Pillow-Bild als PNG (bei Übergröße verkleinert; wenn immer noch zu groß als JPEG)."""
    if bild.mode not in ("RGB", "L", "RGBA"):
        bild = bild.convert("RGB")
    laengste = max(bild.size)
    if laengste > MAX_BILD_PIXEL:
        faktor = ZIEL_BILD_PIXEL / laengste
        neue_groesse = (max(1, round(bild.width * faktor)), max(1, round(bild.height * faktor)))
        logger.info("Bild %s px wird auf %s px verkleinert", bild.size, neue_groesse)
        bild = bild.resize(neue_groesse, Image.LANCZOS)
    puffer = io.BytesIO()
    bild.save(puffer, format="PNG", optimize=True)
    if puffer.tell() <= MAX_BILD_BYTES:
        return "image/png", puffer.getvalue()
    logger.info("PNG mit %d Bytes ist zu groß – als JPEG kodiert", puffer.tell())
    puffer = io.BytesIO()
    if bild.mode == "RGBA":
        bild = bild.convert("RGB")
    bild.save(puffer, format="JPEG", quality=88, optimize=True)
    if puffer.tell() > MAX_BILD_BYTES:
        raise VisionEingabeFehler(f"Bild ist auch nach Verkleinerung zu groß ({puffer.tell()} Bytes > {MAX_BILD_BYTES})")
    return "image/jpeg", puffer.getvalue()


def _lade_bilder(datei: Path) -> list[tuple[str, bytes]]:
    """Liest eine Bilddatei als Liste (media_type, bytes); Mehrseiten-TIFF ergibt mehrere Bilder."""
    endung = datei.suffix.lower()
    if endung in _BILD_MEDIA_TYPES:
        daten = datei.read_bytes()
        try:
            with Image.open(io.BytesIO(daten)) as bild:
                groesse = bild.size
        except Exception as exc:
            raise VisionEingabeFehler(f"Bild {datei.name} kann nicht gelesen werden: {exc}") from exc
        if len(daten) <= MAX_BILD_BYTES and max(groesse) <= MAX_BILD_PIXEL:
            return [(_BILD_MEDIA_TYPES[endung], daten)]
        logger.info("Bild %s (%d Bytes, %s px) wird für die API neu kodiert", datei.name, len(daten), groesse)
        with Image.open(io.BytesIO(daten)) as bild:
            bild.load()
            return [_bild_nach_bytes(bild)]

    # TIFF/BMP: mit Pillow nach PNG wandeln, Mehrseiten-TIFF Seite für Seite.
    ergebnis: list[tuple[str, bytes]] = []
    try:
        with Image.open(datei) as bild:
            seiten = getattr(bild, "n_frames", 1)
            if seiten > MAX_BILDER_PRO_ANFRAGE:
                logger.warning(
                    "%s hat %d Seiten – nur die ersten %d werden gesendet", datei.name, seiten, MAX_BILDER_PRO_ANFRAGE
                )
            for index in range(min(seiten, MAX_BILDER_PRO_ANFRAGE)):
                bild.seek(index)
                ergebnis.append(_bild_nach_bytes(bild.copy()))
    except VisionEingabeFehler:
        raise
    except Exception as exc:
        raise VisionEingabeFehler(f"Bild {datei.name} kann nicht gelesen werden: {exc}") from exc
    return ergebnis


def erzeuge_inhaltsbloecke(datei: Path) -> list[dict[str, Any]]:
    """Baut die Dokument-/Bild-Blöcke für die Nutzer-Nachricht (ohne den Text-Block).

    PDF -> ein Dokument-Block (base64, ``application/pdf``); Bilder -> ein Bild-Block je Bild
    (.png/.jpg/.jpeg/.gif/.webp direkt, .tif/.tiff/.bmp über Pillow nach PNG).
    """
    datei = Path(datei)
    if not datei.is_file():
        raise VisionEingabeFehler(f"Datei nicht gefunden: {datei}")
    endung = datei.suffix.lower()
    if endung == ".pdf":
        daten = datei.read_bytes()
        if len(daten) > MAX_PDF_BYTES:
            raise VisionEingabeFehler(f"PDF {datei.name} ist zu groß für die API ({len(daten)} Bytes > {MAX_PDF_BYTES})")
        return [
            {
                "type": "document",
                "source": {"type": "base64", "media_type": "application/pdf", "data": _base64(daten)},
            }
        ]
    if endung in BILD_ENDUNGEN:
        return [
            {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": _base64(daten)}}
            for media_type, daten in _lade_bilder(datei)
        ]
    raise VisionEingabeFehler(
        f"Dateityp '{endung or '(ohne Endung)'}' wird von der Vision-Extraktion nicht unterstützt "
        f"(erlaubt: {', '.join(sorted(UNTERSTUETZTE_ENDUNGEN))})"
    )


def _zeitlimit_sekunden(max_tokens: int) -> float:
    """Zeitlimit für den nicht-streamenden Aufruf.

    Das SDK verweigert nicht-streamende Aufrufe mit großem ``max_tokens`` ohne explizites
    Zeitlimit; wir rechnen wie das SDK (128k Tokens ~ 60 min), mindestens 10 min.
    """
    return min(3600.0, max(600.0, 3600.0 * max_tokens / 128_000))


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


def erzeuge_client(settings: Settings) -> anthropic.Anthropic:
    """Erzeugt den Anthropic-Client.

    Heißt die Umgebungsvariable ``ANTHROPIC_API_KEY``, liest das SDK sie selbst (``anthropic.Anthropic()``
    ohne Argument). Bei abweichendem ``settings.claude.api_key_env`` wird der Schlüssel aus dieser
    Variable übergeben; fehlt sie, gibt es einen :class:`VisionApiFehler`.
    """
    variable = settings.claude.api_key_env or "ANTHROPIC_API_KEY"
    try:
        if variable == "ANTHROPIC_API_KEY":
            if not os.environ.get(variable) and not os.environ.get("ANTHROPIC_AUTH_TOKEN"):
                # Ohne Schlüssel würde das SDK erst beim Senden mit einem TypeError abbrechen.
                raise VisionApiFehler(
                    "Claude API: kein API-Schlüssel – Umgebungsvariable ANTHROPIC_API_KEY (oder "
                    "ANTHROPIC_AUTH_TOKEN) setzen; Scans/Handschrift (Eingangstyp C) brauchen die Claude API."
                )
            return anthropic.Anthropic()
        schluessel = os.environ.get(variable)
        if not schluessel:
            raise VisionApiFehler(
                f"Umgebungsvariable '{variable}' (settings.claude.api_key_env) ist nicht gesetzt – "
                "API-Schlüssel für die Claude API fehlt."
            )
        return anthropic.Anthropic(api_key=schluessel)
    except anthropic.AnthropicError as exc:
        raise VisionApiFehler(f"Claude-Client konnte nicht erzeugt werden: {exc}") from exc


# ---------------------------------------------------------------------------
# Umwandlung VisionErgebnis -> RohBestellung
# ---------------------------------------------------------------------------


def _decimal_aus_zahl(wert: float | int | None, *, nachkommastellen: int | None = None) -> Decimal | None:
    """float/int aus der API -> Decimal über str() (nie direkt aus float). NaN/inf -> None."""
    if wert is None or isinstance(wert, bool):
        return None
    try:
        dezimal = Decimal(str(wert))
    except InvalidOperation:
        return None
    if not dezimal.is_finite():
        return None
    if nachkommastellen is not None:
        return dezimal.quantize(Decimal(1).scaleb(-nachkommastellen))
    if dezimal == dezimal.to_integral_value():
        return Decimal(int(dezimal))
    return dezimal.normalize()


def _konfidenz(wert: float | None) -> float | None:
    """Konfidenz auf 0..1 begrenzen; None bleibt None, NaN wird 0."""
    if wert is None:
        return None
    try:
        zahl = float(wert)
    except (TypeError, ValueError):
        return 0.0
    if math.isnan(zahl):
        return 0.0
    return min(1.0, max(0.0, zahl))


def _prozent(wert: float) -> str:
    return f"{wert * 100:.0f} %"


def _text(wert: str | None) -> str | None:
    if wert is None:
        return None
    bereinigt = " ".join(str(wert).split())
    return bereinigt or None


def _dezimal_text(wert: Decimal | None) -> str:
    return "" if wert is None else str(wert).replace(".", ",")


def _hat_menge(position: RohPosition) -> bool:
    return position.menge is not None and position.menge > 0


def _wandle_position(vp: VisionPosition, laufnr: int) -> RohPosition | None:
    bezeichnung = _text(vp.bezeichnung) or _text(vp.rohtext)
    if not bezeichnung:
        logger.warning("Position %s ohne Bezeichnung und Rohtext wird übersprungen", laufnr)
        return None

    menge = _decimal_aus_zahl(vp.menge)
    menge_text = _text(vp.menge_text)
    if menge is None and menge_text:
        # Das Modell hat die Menge gelesen, aber nicht als Zahl geliefert: deterministisch nachparsen.
        menge = parse_dezimal(menge_text)
        if menge is not None:
            logger.debug("Pos. %s: Menge %s aus menge_text %r abgeleitet", laufnr, menge, menge_text)
    if menge is not None and menge < 0:
        logger.warning("Pos. %s: negative Menge %s wird verworfen", laufnr, menge)
        menge = None

    preis = _decimal_aus_zahl(vp.preis, nachkommastellen=2)
    if preis is not None and preis < 0:
        logger.warning("Pos. %s: negativer Preis %s wird verworfen", laufnr, preis)
        preis = None

    jahrgang = vp.jahrgang if vp.jahrgang is not None and 1900 <= vp.jahrgang <= 2099 else None
    if jahrgang is None:
        jahrgang = parse_jahrgang(vp.volumen_text) or parse_jahrgang(bezeichnung)

    volumen_cl = parse_volumen_cl(vp.volumen_text) or parse_volumen_cl(bezeichnung)

    konfidenz_menge = _konfidenz(vp.konfidenz_menge)
    konfidenz_preis = _konfidenz(vp.konfidenz_preis) if preis is not None else None

    # Rabatt laut Dokument: aus der Antwort, sonst deterministisch aus Rabatttext/Hinweis/Rohtext
    hinweis = _text(vp.hinweis)
    rabatt_prozent = glatt(_decimal_aus_zahl(vp.rabatt_prozent, nachkommastellen=2))
    rabatt_text = _text(vp.rabatt_text)
    if rabatt_prozent is not None and not 0 < rabatt_prozent <= 100:
        logger.warning("Pos. %s: Rabatt %s %% ist unplausibel und wird verworfen", laufnr, rabatt_prozent)
        rabatt_prozent = None
    if rabatt_prozent is None:
        fund = parse_rabatt(rabatt_text) or parse_rabatt(hinweis) or parse_rabatt(_text(vp.rohtext))
        if fund is not None:
            rabatt_prozent = fund.prozent
            rabatt_text = rabatt_text or fund.rohtext
            logger.debug("Pos. %s: Rabatt %s %% aus dem Text abgeleitet (%r)", laufnr, fund.prozent, fund.rohtext)
    if ist_speditionszeile(bezeichnung, preis) and SPEDITIONSZEILE_HINWEIS.casefold() not in (hinweis or "").casefold():
        hinweis = f"{hinweis}; {SPEDITIONSZEILE_HINWEIS}" if hinweis else SPEDITIONSZEILE_HINWEIS

    return RohPosition(
        laufnr=laufnr,
        bezeichnung=bezeichnung,
        jahrgang=jahrgang,
        menge=menge,
        einheit=_text(vp.einheit),
        preis=preis,
        volumen_cl=volumen_cl,
        artikelnummer=_text(vp.artikelnummer),
        hinweis=hinweis,
        durchgestrichen=bool(vp.durchgestrichen),
        konfidenz_menge=konfidenz_menge,
        konfidenz_preis=konfidenz_preis,
        rohtext=_text(vp.rohtext),
        rabatt_prozent=rabatt_prozent,
        rabatt_text=rabatt_text,
    )


def _laufnummern(positionen: list[VisionPosition]) -> list[int]:
    """Laufnummern des Modells übernehmen, wenn sie eindeutig und positiv sind; sonst neu durchnummerieren."""
    nummern = [p.laufnr for p in positionen]
    if nummern and len(set(nummern)) == len(nummern) and all(n > 0 for n in nummern):
        return nummern
    if nummern:
        logger.warning("Laufnummern aus der Vision-Antwort sind nicht eindeutig (%s) – werden neu vergeben", nummern)
    return list(range(1, len(positionen) + 1))


def _positionshinweise(position: RohPosition, menge_text: str | None, schwelle: float) -> list[Pruefhinweis]:
    """Hinweise je Position. Positionen ohne Menge (None/0) bekommen keine Hinweise."""
    if not _hat_menge(position):
        return []
    hinweise: list[Pruefhinweis] = []
    laufnr = position.laufnr
    gelesen = menge_text or _dezimal_text(position.menge)
    if position.konfidenz_menge is not None and position.konfidenz_menge < schwelle:
        hinweise.append(
            Pruefhinweis(
                code=Pruefcode.KONFIDENZ_NIEDRIG,
                schweregrad=Schweregrad.WARNUNG,
                text=(
                    f"Pos. {laufnr} ({position.bezeichnung}): Menge '{gelesen}' unsicher gelesen "
                    f"(Konfidenz {_prozent(position.konfidenz_menge)} < {_prozent(schwelle)}) – bitte prüfen."
                ),
                position_laufnr=laufnr,
                details={
                    "feld": "menge",
                    "konfidenz": f"{position.konfidenz_menge:.2f}",
                    "schwelle": f"{schwelle:.2f}",
                    "menge_text": gelesen,
                },
            )
        )
    if position.preis is not None and position.konfidenz_preis is not None and position.konfidenz_preis < schwelle:
        hinweise.append(
            Pruefhinweis(
                code=Pruefcode.KONFIDENZ_NIEDRIG,
                schweregrad=Schweregrad.WARNUNG,
                text=(
                    f"Pos. {laufnr} ({position.bezeichnung}): Preis {_dezimal_text(position.preis)} € unsicher gelesen "
                    f"(Konfidenz {_prozent(position.konfidenz_preis)} < {_prozent(schwelle)}) – bitte prüfen."
                ),
                position_laufnr=laufnr,
                details={
                    "feld": "preis",
                    "konfidenz": f"{position.konfidenz_preis:.2f}",
                    "schwelle": f"{schwelle:.2f}",
                    "preis": _dezimal_text(position.preis),
                },
            )
        )
    if position.durchgestrichen:
        hinweise.append(
            Pruefhinweis(
                code=Pruefcode.DURCHGESTRICHEN,
                schweregrad=Schweregrad.WARNUNG,
                text=(
                    f"Pos. {laufnr} ({position.bezeichnung}): Zeile oder Preis ist im Dokument durchgestrichen – "
                    f"Menge {_dezimal_text(position.menge)} wird nicht gezählt, bitte prüfen."
                ),
                position_laufnr=laufnr,
                details={"menge": _dezimal_text(position.menge), "preis": _dezimal_text(position.preis)},
            )
        )
    return hinweise


def _datum_mit_hinweis(text: str | None, feld: str, hinweise: list[Pruefhinweis], *, pflicht: bool):
    """Datumstext -> date; unlesbar -> WARNUNG, fehlend -> INFO (nur bei pflicht)."""
    bereinigt = _text(text)
    if bereinigt is None:
        if pflicht:
            hinweise.append(
                Pruefhinweis(
                    code=Pruefcode.FELD_FEHLT,
                    schweregrad=Schweregrad.INFO,
                    text=f"Kein {feld} im Dokument erkannt.",
                    details={"feld": feld},
                )
            )
        return None
    datum = parse_datum(bereinigt)
    if datum is None:
        hinweise.append(
            Pruefhinweis(
                code=Pruefcode.FELD_FEHLT,
                schweregrad=Schweregrad.WARNUNG,
                text=f"{feld.capitalize()} '{bereinigt}' konnte nicht als Datum gelesen werden.",
                details={"feld": feld, "text": bereinigt},
            )
        )
    return datum


def wandle_in_rohbestellung(
    ergebnis: VisionErgebnis,
    datei: Path,
    konfidenz_schwelle: float,
    *,
    seiten: int | None = None,
) -> RohBestellung:
    """Übersetzt die strukturierte Claude-Antwort in die :class:`RohBestellung` (Eingangstyp C).

    * Zahlen werden über ``Decimal(str(x))`` in Decimal gewandelt, Preise auf 2 Nachkommastellen.
    * Positionen ohne Menge bleiben erhalten (``menge=None``), bekommen aber keine Hinweise.
    * Konfidenz unter ``konfidenz_schwelle`` -> KONFIDENZ_NIEDRIG (WARNUNG), durchgestrichen ->
      DURCHGESTRICHEN (WARNUNG), Summenvermerke -> SUMMENVERMERK (INFO).
    """
    hinweise: list[Pruefhinweis] = []
    positionen: list[RohPosition] = []

    for vp, laufnr in zip(ergebnis.positionen, _laufnummern(ergebnis.positionen), strict=True):
        position = _wandle_position(vp, laufnr)
        if position is None:
            continue
        positionen.append(position)
        hinweise.extend(_positionshinweise(position, _text(vp.menge_text), konfidenz_schwelle))

    summenvermerke: list[Summenvermerk] = []
    for vs in ergebnis.summenvermerke:
        wert = _decimal_aus_zahl(vs.wert)
        rohtext = _text(vs.rohtext) or _dezimal_text(wert)
        if wert is None:
            logger.warning("Summenvermerk %r ohne lesbaren Wert wird übersprungen", rohtext)
            continue
        einheit = _text(vs.einheit)
        konfidenz = _konfidenz(vs.konfidenz)
        summenvermerke.append(Summenvermerk(wert=wert, einheit=einheit, rohtext=rohtext, konfidenz=konfidenz))
        hinweise.append(
            Pruefhinweis(
                code=Pruefcode.SUMMENVERMERK,
                schweregrad=Schweregrad.INFO,
                text=f"Summenvermerk im Dokument: '{rohtext}' (Wert {_dezimal_text(wert)}{' ' + einheit if einheit else ''}).",
                details={
                    "rohtext": rohtext,
                    "wert": _dezimal_text(wert),
                    "einheit": einheit or "",
                    "konfidenz": "" if konfidenz is None else f"{konfidenz:.2f}",
                },
            )
        )

    kunde_text = _text(ergebnis.kunde_text)
    lieferant_text = _text(ergebnis.lieferant_text)
    for feld, wert in (("Kunde", kunde_text), ("Lieferant", lieferant_text)):
        if wert is None:
            hinweise.append(
                Pruefhinweis(
                    code=Pruefcode.FELD_FEHLT,
                    schweregrad=Schweregrad.WARNUNG,
                    text=f"{feld} im Dokument nicht erkannt – bitte in der Freigabe zuordnen.",
                    details={"feld": feld.lower()},
                )
            )
    datum = _datum_mit_hinweis(ergebnis.datum, "datum", hinweise, pflicht=True)
    liefertag = _datum_mit_hinweis(ergebnis.liefertag, "liefertag", hinweise, pflicht=False)

    if not positionen:
        hinweise.append(
            Pruefhinweis(
                code=Pruefcode.FELD_FEHLT,
                schweregrad=Schweregrad.FEHLER,
                text="Keine Positionen in der Preisliste erkannt – Dokument prüfen (Scanqualität, richtige Datei?).",
                details={"feld": "positionen"},
            )
        )
    elif not any(_hat_menge(p) for p in positionen):
        hinweise.append(
            Pruefhinweis(
                code=Pruefcode.FELD_FEHLT,
                schweregrad=Schweregrad.WARNUNG,
                text="Keine handschriftlichen Bestellmengen erkannt – alle Positionen ohne Menge.",
                details={"feld": "menge"},
            )
        )

    anmerkungen = [a for a in (_text(x) for x in ergebnis.anmerkungen) if a]
    for anmerkung in anmerkungen:
        logger.info("Anmerkung von Claude zu %s: %s", datei.name, anmerkung)
    zeilen = [p.rohtext for p in positionen if p.rohtext]
    if anmerkungen:
        zeilen.append("Anmerkungen: " + " | ".join(anmerkungen))
    rohtext = "\n".join(zeilen) or None

    # Aktionshinweis und Revised Order: aus der Antwort, sonst deterministisch aus Anmerkungen/Rohtext
    texte: list[str | None] = [*anmerkungen, *(p.rohtext for p in positionen), *(p.hinweis for p in positionen)]
    aktionshinweis = _text(ergebnis.aktionshinweis) or finde_aktionshinweis(texte)
    revision: RevisionTreffer | None
    if ergebnis.ist_revision:
        revision = RevisionTreffer(rohtext="laut Vision-Auswertung", revision_von=_text(ergebnis.revision_von))
    else:
        revision = finde_revision(texte)
    if revision is not None:
        hinweise.append(revisions_hinweis(revision))

    return RohBestellung(
        eingangstyp=Eingangstyp.PREISLISTE_SCAN,
        quelldatei=datei,
        extraktionsmethode=Extraktionsmethode.CLAUDE_VISION,
        kunde_text=kunde_text,
        lieferant_text=lieferant_text,
        belegnummer=_text(ergebnis.belegnummer),
        bestellnummer=_text(ergebnis.bestellnummer),
        datum=datum,
        liefertag=liefertag,
        lieferadresse=_text(ergebnis.lieferadresse),
        positionen=positionen,
        summenvermerke=summenvermerke,
        hinweise=hinweise,
        rohtext=rohtext,
        seiten=seiten if seiten is not None else ergebnis.seitenzahl,
        ist_revision=revision is not None,
        revision_von=revision.revision_von if revision else None,
        aktionshinweis=aktionshinweis,
    )


# ---------------------------------------------------------------------------
# Extraktor
# ---------------------------------------------------------------------------


class VisionExtraktor:
    """Extraktor für Eingangstyp C (Scan/Handschrift) über die Claude API.

    ``client`` kann für Tests injiziert werden (Objekt mit ``messages.parse(**kwargs)``). Ohne
    Client wird er beim ersten API-Aufruf über :func:`erzeuge_client` angelegt, damit
    ``kann_verarbeiten`` auch ohne API-Schlüssel funktioniert.
    """

    def __init__(self, settings: Settings, client: anthropic.Anthropic | None = None) -> None:
        self.settings = settings
        self._client = client
        self.letztes_ergebnis: VisionErgebnis | None = None
        self.letzte_nutzung: dict[str, Any] = {}

    # -- Schnittstelle Extraktor -------------------------------------------------

    def kann_verarbeiten(self, datei: Path) -> bool:
        """PDF ohne Textebene oder Bilddatei. Kaputte/fehlende Dateien -> False (mit Log)."""
        datei = Path(datei)
        if not datei.is_file():
            return False
        endung = datei.suffix.lower()
        if endung in BILD_ENDUNGEN:
            return True
        if endung != ".pdf":
            return False
        try:
            _, hat_text = _pdf_info(datei)
        except VisionEingabeFehler as exc:
            logger.warning("%s", exc)
            return False
        return not hat_text

    def extrahiere(self, datei: Path) -> RohBestellung:
        """Dokument an Claude senden und als RohBestellung (Eingangstyp C) zurückgeben."""
        return self.extrahiere_mit_modell(datei, self.settings.claude.modell)

    def extrahiere_mit_modell(self, datei: Path, modell: str) -> RohBestellung:
        """Wie :meth:`extrahiere`, aber mit explizitem Modell (für manuelle Vergleiche)."""
        datei = Path(datei)
        seiten: int | None = None
        if datei.suffix.lower() == ".pdf":
            seiten, hat_text = _pdf_info(datei)
            if hat_text:
                logger.info(
                    "PDF %s hat eine Textebene – Vision-Extraktion wird trotzdem ausgeführt", datei.name
                )
            if seiten > MAX_PDF_SEITEN:
                logger.warning("PDF %s hat %d Seiten – die API begrenzt Dokumente auf wenige hundert Seiten", datei.name, seiten)
        ergebnis = self.frage_claude(datei, modell)
        roh = wandle_in_rohbestellung(ergebnis, datei, self.settings.matching.konfidenz_schwelle, seiten=seiten)
        logger.info(
            "Vision-Extraktion %s: %d Positionen (%d mit Menge, %d durchgestrichen), %d Summenvermerke, %d Hinweise",
            datei.name,
            len(roh.positionen),
            sum(1 for p in roh.positionen if _hat_menge(p)),
            sum(1 for p in roh.positionen if p.durchgestrichen),
            len(roh.summenvermerke),
            len(roh.hinweise),
        )
        return roh

    # -- API -----------------------------------------------------------------------

    def baue_anfrage(self, datei: Path, modell: str | None = None) -> dict[str, Any]:
        """Argumente für ``client.messages.parse``: Dokument-/Bild-Blöcke vor dem Text-Block.

        Bewusst ohne ``thinking``, ``temperature``, ``tool_choice`` und Prefill (siehe API-Referenz
        für claude-opus-5-5).
        """
        claude = self.settings.claude
        inhalt: list[dict[str, Any]] = [*erzeuge_inhaltsbloecke(datei), {"type": "text", "text": BENUTZER_PROMPT}]
        return {
            "model": modell or claude.modell,
            "max_tokens": claude.max_tokens,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": inhalt}],
            "output_format": VisionErgebnis,
            "output_config": {"effort": claude.effort},
            "timeout": _zeitlimit_sekunden(claude.max_tokens),
        }

    def _client_holen(self) -> Any:
        if self._client is None:
            self._client = erzeuge_client(self.settings)
        return self._client

    def frage_claude(self, datei: Path, modell: str | None = None) -> VisionErgebnis:
        """Ruft die API auf und liefert die strukturierte Antwort (ohne Umwandlung)."""
        datei = Path(datei)
        anfrage = self.baue_anfrage(datei, modell)
        client = self._client_holen()
        logger.info(
            "Sende %s an die Claude API (Modell %s, effort %s, max_tokens %d, %d Inhaltsblöcke)",
            datei.name,
            anfrage["model"],
            anfrage["output_config"]["effort"],
            anfrage["max_tokens"],
            len(anfrage["messages"][0]["content"]),
        )
        try:
            antwort = client.messages.parse(**anfrage)
        except TypeError as exc:
            # Das SDK meldet fehlende Anmeldedaten erst beim Senden als TypeError
            raise VisionApiFehler(
                f"Claude API: Anmeldung nicht möglich ({exc}) – API-Schlüssel in der Umgebungsvariable "
                f"'{self.settings.claude.api_key_env}' setzen."
            ) from exc
        except anthropic.AuthenticationError as exc:
            raise VisionApiFehler(
                "Claude API: Anmeldung fehlgeschlagen – API-Schlüssel in der Umgebungsvariable "
                f"'{self.settings.claude.api_key_env}' prüfen ({exc.message})."
            ) from exc
        except anthropic.RateLimitError as exc:
            wartezeit = exc.response.headers.get("retry-after") if exc.response is not None else None
            raise VisionApiFehler(
                "Claude API: Ratenlimit erreicht – bitte später erneut versuchen"
                + (f" (empfohlene Wartezeit {wartezeit} s)" if wartezeit else "")
                + "."
            ) from exc
        except anthropic.APIStatusError as exc:
            raise VisionApiFehler(f"Claude API: Fehler {exc.status_code} – {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise VisionApiFehler(
                f"Claude API nicht erreichbar – Netzwerk/Proxy prüfen ({exc.__class__.__name__}: {exc})."
            ) from exc

        self._protokolliere_nutzung(antwort, anfrage["model"], datei)

        stop_reason = getattr(antwort, "stop_reason", None)
        if stop_reason == "refusal":
            details = getattr(antwort, "stop_details", None)
            raise VisionAbgelehnt(
                kategorie=getattr(details, "category", None),
                erklaerung=getattr(details, "explanation", None),
            )
        if stop_reason == "max_tokens":
            raise VisionAbgeschnitten(
                f"Die Antwort wurde bei max_tokens={anfrage['max_tokens']} abgeschnitten – "
                "settings.claude.max_tokens erhöhen oder das Dokument aufteilen."
            )
        if stop_reason == "model_context_window_exceeded":
            raise VisionAbgeschnitten(
                "Das Dokument übersteigt das Kontextfenster des Modells – Dokument aufteilen (weniger Seiten/Bilder)."
            )
        if stop_reason not in (None, "end_turn", "stop_sequence"):
            logger.warning("Unerwarteter stop_reason '%s' von der Claude API", stop_reason)

        ergebnis = getattr(antwort, "parsed_output", None)
        if ergebnis is None:
            raise VisionFehler(f"Claude lieferte keine strukturierte Ausgabe (stop_reason={stop_reason}).")
        if not isinstance(ergebnis, VisionErgebnis):
            ergebnis = VisionErgebnis.model_validate(ergebnis)
        self.letztes_ergebnis = ergebnis
        return ergebnis

    def _protokolliere_nutzung(self, antwort: Any, modell: str, datei: Path) -> None:
        nutzung = getattr(antwort, "usage", None)
        self.letzte_nutzung = {
            "modell": getattr(antwort, "model", None) or modell,
            "datei": datei.name,
            "input_tokens": getattr(nutzung, "input_tokens", None),
            "output_tokens": getattr(nutzung, "output_tokens", None),
            "cache_read_input_tokens": getattr(nutzung, "cache_read_input_tokens", None),
            "cache_creation_input_tokens": getattr(nutzung, "cache_creation_input_tokens", None),
            "stop_reason": getattr(antwort, "stop_reason", None),
            "request_id": getattr(antwort, "_request_id", None),
        }
        logger.info(
            "Claude-Nutzung für %s (Modell %s): Eingabe %s Tokens, Ausgabe %s Tokens, Cache gelesen %s, "
            "Cache angelegt %s, stop_reason=%s, request_id=%s",
            datei.name,
            self.letzte_nutzung["modell"],
            self.letzte_nutzung["input_tokens"],
            self.letzte_nutzung["output_tokens"],
            self.letzte_nutzung["cache_read_input_tokens"],
            self.letzte_nutzung["cache_creation_input_tokens"],
            self.letzte_nutzung["stop_reason"],
            self.letzte_nutzung["request_id"],
        )
