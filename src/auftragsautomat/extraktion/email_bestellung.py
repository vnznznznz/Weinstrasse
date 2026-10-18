"""E-Mail-Extraktion eigener Bestellungen an das Weingut (Eingangstyp B).

Unterstützte Dateien:

* ``.eml`` – Standard-Mailformat, gelesen mit der Standardbibliothek ``email``.
  ``text/plain`` wird bevorzugt, sonst wird der HTML-Teil zu Text umgewandelt.
* ``.txt`` – reiner Text, optional mit Outlook-Kopfzeilen ("Von:", "An:", "Betreff:",
  "Gesendet:") am Anfang.
* ``.msg`` – Outlook-Format, nur wenn das Paket ``extract_msg`` installiert ist
  (Windows-Extra in ``pyproject.toml``). Ohne das Paket liefert :meth:`EmailExtraktor.extrahiere`
  eine :class:`RohBestellung` mit ``Pruefhinweis(FELD_FEHLT, FEHLER)`` und klarer Meldung.

Erkennungslogik (alles Heuristik, Unbekanntes bleibt ``None`` + ``Pruefhinweis``):

* **Lieferant (Weingut)**: Feld "Lieferant:"/"Weingut:" > "Bestellung … an <Weingut>"
  (Betreff oder Text) > Anrede ("Sehr geehrte Damen und Herren der Cide srl",
  "Liebes Team von Bollina", "Hallo Cide-Team") > Anzeigename des Empfängers
  ("Cide srl <ordini@cide.example>") > Domain der Empfängeradresse (nur bei
  Firmendomains). Die eigene Firma (``settings.firmen``) wird nie als Lieferant gelesen.
* **Endkunde**: Feld "Kunde:"/"Endkunde:"/"Kommission:" > "für unseren Kunden Weinhaus
  Menrath" > "Kommission Menrath" > "Kunde Menrath" > "Bestellung für Menrath" (Betreff
  zuerst, dann Text).
* **Belegnummer**: "Bestell-Nr.:", "Bestellung Nr.", "Beleg-Nr." oder die Nummer direkt
  hinter "Bestellung" im Betreff ("Bestellung BE-2026-0078 …").
* **Datum**: Feld "Datum:" im Text, sonst das Sendedatum der Mail.
* **Liefertag**: "Liefertermin:", "Lieferung am/bis/in …", "KW 44" (bleibt Rohtext +
  Warnung, wird nicht in ein Datum geraten).
* **Positionen** aus Textzeilen: ``[-•*] Menge [Einheit] Bezeichnung [Jahrgang] [à Preis]``,
  ``Bezeichnung: Menge Einheit [à Preis]``, ``Art.-Nr. 1643 …``, "Magnum"/"1,5 l" als
  Volumen, Hinweise wie "11+1"/"gratis". Zeilen ohne Einheit/Preis gelten nur im Block mit
  anderen Positionszeilen als Position. Findet der eigene Parser nichts, greift die
  Zeilen-/Tabellenheuristik aus ``text_pdf`` auf den Mailtext.
* **Zitate/Weiterleitungen**: Enthält der eigene Teil der Mail keine Bestellung, wird der
  zitierte Teil ("-----Ursprüngliche Nachricht-----", "Am … schrieb …") verwendet; dessen
  Kopfzeilen (An:, Betreff:) ersetzen dann die der Hülle.
* **PDF-Anhänge** werden in ein temporäres Verzeichnis geschrieben und mit
  ``text_pdf.TextPdfExtraktor`` gelesen. Der Kopf kommt aus der Mail (fehlende Felder aus
  dem PDF), die Positionen aus der Mail – nur wenn die Mail keine hat, aus dem PDF.
  PDF-Anhänge ohne Textebene (Scans) und Bildanhänge werden nur gemeldet, nicht
  ausgewertet (das leistet ``extraktion.vision`` auf der Einzeldatei).
"""

from __future__ import annotations

import email
import email.policy
import importlib
import logging
import re
import tempfile
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from email.message import Message
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from auftragsautomat.config import Settings
from auftragsautomat.extraktion import zahlen
from auftragsautomat.extraktion.text_pdf import TextPdfExtraktor, TextPdfFehler, ist_text_pdf, lese_kopffelder
from auftragsautomat.matching import volumen
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
    "EMAIL_ENDUNGEN",
    "MSG_HINWEIS_TEXT",
    "Adresse",
    "Anhang",
    "EmailExtraktor",
    "EmailFehler",
    "EmailInhalt",
    "MsgNichtVerfuegbar",
    "endkunde_aus_text",
    "html_zu_text",
    "lese_email",
    "lese_eml",
    "lese_msg",
    "lese_positionen",
    "lese_pseudokopf",
    "lese_txt",
    "lieferant_aus_anrede",
    "msg_unterstuetzt",
    "teile_zitat",
]

logger = logging.getLogger(__name__)

EMAIL_ENDUNGEN: frozenset[str] = frozenset({".eml", ".msg", ".txt"})
_BILD_ENDUNGEN = frozenset({".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".gif", ".webp"})

MSG_HINWEIS_TEXT = (
    "Outlook-Datei (.msg) kann nicht gelesen werden: das Python-Paket 'extract_msg' ist nicht "
    "installiert. Installieren mit 'pip install extract-msg' (Extra 'windows') oder die E-Mail in "
    "Outlook als .eml/.txt speichern."
)


class EmailFehler(RuntimeError):
    """Die E-Mail-Datei kann nicht gelesen werden (defekt, falsches Format, nicht lesbar)."""


class MsgNichtVerfuegbar(EmailFehler):
    """``.msg`` angefordert, aber ``extract_msg`` ist nicht installiert."""


# ---------------------------------------------------------------------------
# Datenstrukturen
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Adresse:
    """Eine E-Mail-Adresse mit Anzeigename ("Cide srl <ordini@cide.example>")."""

    name: str | None
    adresse: str | None

    @property
    def anzeige(self) -> str:
        return self.name or self.adresse or ""

    @property
    def domain(self) -> str | None:
        if self.adresse and "@" in self.adresse:
            return self.adresse.rsplit("@", 1)[1].strip().lower() or None
        return None


@dataclass
class Anhang:
    """Dateianhang einer E-Mail (Inhalt bereits dekodiert)."""

    dateiname: str
    inhalt: bytes
    content_type: str = "application/octet-stream"

    @property
    def endung(self) -> str:
        return Path(self.dateiname).suffix.lower()

    @property
    def ist_pdf(self) -> bool:
        return (
            self.endung == ".pdf"
            or self.content_type.lower() == "application/pdf"
            or self.inhalt[:5] == b"%PDF-"
        )

    @property
    def ist_bild(self) -> bool:
        return self.endung in _BILD_ENDUNGEN or self.content_type.lower().startswith("image/")


@dataclass
class EmailInhalt:
    """Lesbarer Inhalt einer E-Mail, unabhängig vom Dateiformat."""

    quelldatei: Path
    format: str = "eml"  # eml / txt / msg
    betreff: str | None = None
    absender: Adresse | None = None
    empfaenger: list[Adresse] = field(default_factory=list)
    datum: date | None = None
    text: str = ""
    anhaenge: list[Anhang] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------


def _normalisiere(text: str | None) -> str:
    if not text:
        return ""
    t = text.replace("ß", "ss").replace("ä", "ae").replace("ö", "oe").replace("ü", "ue")
    t = t.replace("Ä", "ae").replace("Ö", "oe").replace("Ü", "ue")
    t = "".join(c for c in unicodedata.normalize("NFKD", t) if not unicodedata.combining(c))
    t = re.sub(r"[^a-z0-9]+", " ", t.casefold())
    return re.sub(r"\s+", " ", t).strip()


def _adressen(text: str | None) -> list[Adresse]:
    """Zerlegt "Name <adr>, Name2 <adr2>" (auch mit ';' getrennt) in :class:`Adresse`-Objekte."""
    if not text:
        return []
    ergebnis: list[Adresse] = []
    try:
        paare = getaddresses([text.replace(";", ",")])
    except Exception as exc:  # pragma: no cover - getaddresses ist sehr tolerant
        logger.debug("Adresskopf nicht zerlegbar (%s): %r", exc, text)
        paare = [("", text)]
    for name, adresse in paare:
        name = name.strip().strip('"').strip()
        adresse = adresse.strip()
        if not name and not adresse:
            continue
        if not adresse and "@" not in name:
            # getaddresses liefert bei bloßen Namen ("Cide srl") den Text als Adresse
            ergebnis.append(Adresse(name=name or None, adresse=None))
            continue
        if adresse and "@" not in adresse and not name:
            ergebnis.append(Adresse(name=adresse, adresse=None))
            continue
        ergebnis.append(Adresse(name=name or None, adresse=adresse or None))
    return ergebnis


def _datum_aus_header(text: str | None) -> date | None:
    if not text:
        return None
    try:
        return parsedate_to_datetime(text).date()
    except (TypeError, ValueError, IndexError):
        return zahlen.parse_datum(text)


def _bytes_zu_text(daten: bytes, kodierung: str | None = None) -> str:
    for versuch in filter(None, [kodierung, "utf-8-sig", "cp1252", "latin-1"]):
        try:
            return daten.decode(versuch)
        except (UnicodeDecodeError, LookupError):
            continue
    return daten.decode("utf-8", errors="replace")


def _sicherer_dateiname(name: str | None, nr: int, endung: str = ".pdf") -> str:
    basis = Path(name or "").name if name else ""
    basis = re.sub(r"[^\w.\-() ]+", "_", basis).strip(" .")
    if not basis or basis.startswith("."):
        basis = f"anhang_{nr}{endung}"
    if not basis.lower().endswith(endung):
        basis += endung
    return f"{nr:02d}_{basis}"


# ---------------------------------------------------------------------------
# HTML -> Text
# ---------------------------------------------------------------------------

_HTML_BLOCK = frozenset(
    {
        "p",
        "div",
        "br",
        "tr",
        "li",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "table",
        "ul",
        "ol",
        "section",
        "article",
        "header",
        "footer",
        "blockquote",
        "pre",
        "hr",
        "address",
        "dd",
        "dt",
    }
)
_HTML_IGNORIEREN = frozenset({"style", "script", "head", "title", "meta", "link"})
_HTML_ZELLE = frozenset({"td", "th"})


class _HtmlZuText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._teile: list[str] = []
        self._ignorieren = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in _HTML_IGNORIEREN:
            self._ignorieren += 1
        elif tag in _HTML_BLOCK:
            self._teile.append("\n")
        elif tag in _HTML_ZELLE:
            self._teile.append("\t")  # wird zu drei Leerzeichen: text_pdf trennt daran Spalten

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _HTML_IGNORIEREN:
            self._ignorieren = max(0, self._ignorieren - 1)
        elif tag in _HTML_BLOCK and tag != "br":
            self._teile.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_data(self, data: str) -> None:
        if not self._ignorieren:
            self._teile.append(data)

    def text(self) -> str:
        roh = "".join(self._teile).replace("\xa0", " ").replace("\r", "")
        zeilen = [re.sub(r"\s*\t\s*", "   ", re.sub(r" +", " ", z)).strip() for z in roh.split("\n")]
        text = "\n".join(zeilen)
        return re.sub(r"\n{3,}", "\n\n", text).strip()


def html_zu_text(html: str | None) -> str:
    """Wandelt HTML in lesbaren Text (Absätze/Zeilenumbrüche erhalten, Tabellenzellen getrennt)."""
    if not html:
        return ""
    parser = _HtmlZuText()
    try:
        parser.feed(html)
        parser.close()
    except Exception as exc:  # pragma: no cover - HTMLParser wirft praktisch nie
        logger.warning("HTML-Teil nicht sauber lesbar (%s), nutze Roh-Text", exc)
        return re.sub(r"<[^>]+>", " ", html)
    return parser.text()


# ---------------------------------------------------------------------------
# .eml lesen
# ---------------------------------------------------------------------------


def _header(msg: Message, name: str) -> str | None:
    try:
        wert = msg.get(name)
    except Exception as exc:  # defekte Kopfzeile
        logger.debug("Kopfzeile %s nicht lesbar: %s", name, exc)
        return None
    if wert is None:
        return None
    text = str(wert).replace("\r", " ").replace("\n", " ").strip()
    return re.sub(r"\s+", " ", text) or None


def _textinhalt(teil: Message) -> str:
    try:
        inhalt = teil.get_content()
        if isinstance(inhalt, str):
            return inhalt
        if isinstance(inhalt, bytes):
            return _bytes_zu_text(inhalt, teil.get_content_charset())
    except (LookupError, UnicodeDecodeError, KeyError, AttributeError) as exc:
        logger.debug("Textteil nicht direkt dekodierbar (%s), nutze Rohdaten", exc)
    daten = teil.get_payload(decode=True)
    if isinstance(daten, bytes):
        return _bytes_zu_text(daten, teil.get_content_charset())
    return str(daten or "")


def _inhalt_aus_message(msg: Message, datei: Path) -> EmailInhalt:
    text_teile: list[str] = []
    html_teile: list[str] = []
    anhaenge: list[Anhang] = []
    nr = 0
    for teil in msg.walk():
        if teil.is_multipart():
            continue
        ctype = teil.get_content_type().lower()
        dateiname = teil.get_filename()
        disposition = (teil.get_content_disposition() or "").lower()
        ist_anhang = bool(dateiname) or disposition == "attachment"
        if ctype == "text/plain" and not ist_anhang:
            text_teile.append(_textinhalt(teil))
            continue
        if ctype == "text/html" and not ist_anhang:
            html_teile.append(_textinhalt(teil))
            continue
        daten = teil.get_payload(decode=True)
        if not isinstance(daten, bytes) or not daten:
            continue
        nr += 1
        anhaenge.append(Anhang(dateiname=dateiname or f"anhang_{nr}", inhalt=daten, content_type=ctype))

    text = "\n".join(t for t in text_teile if t.strip()).strip()
    if not text and html_teile:
        text = html_zu_text("\n".join(html_teile))
    absender = _adressen(_header(msg, "From"))
    empfaenger = _adressen(_header(msg, "To")) + _adressen(_header(msg, "Cc"))
    return EmailInhalt(
        quelldatei=datei,
        format="eml",
        betreff=_header(msg, "Subject"),
        absender=absender[0] if absender else None,
        empfaenger=empfaenger,
        datum=_datum_aus_header(_header(msg, "Date")),
        text=text.replace("\r\n", "\n").replace("\r", "\n"),
        anhaenge=anhaenge,
    )


def lese_eml(datei: Path) -> EmailInhalt:
    """Liest eine .eml-Datei (RFC 5322) mit der Standardbibliothek."""
    datei = Path(datei)
    try:
        with open(datei, "rb") as f:
            msg = email.message_from_binary_file(f, policy=email.policy.default)
    except OSError as exc:
        raise EmailFehler(f"E-Mail-Datei kann nicht gelesen werden: {datei} ({exc})") from exc
    except Exception as exc:  # pragma: no cover - der Parser ist sehr tolerant
        raise EmailFehler(f"E-Mail-Datei ist kein lesbares .eml: {datei} ({exc})") from exc
    inhalt = _inhalt_aus_message(msg, datei)
    if not inhalt.text and not inhalt.anhaenge and not inhalt.betreff:
        logger.warning("%s: weder Text, Betreff noch Anhänge gefunden", datei.name)
    return inhalt


# ---------------------------------------------------------------------------
# .txt lesen (mit optionalen Outlook-Kopfzeilen)
# ---------------------------------------------------------------------------

_PSEUDOKOPF_ZEILE = re.compile(
    r"^(?P<name>Von|From|Da|An|To|A|Cc|CC|Bcc|Betreff|Subject|Oggetto|Gesendet|Sent|Datum|Date|Data|Inviato)"
    r"\s*:\s*(?P<wert>.*)$",
    re.I,
)
_PSEUDOKOPF_FELD: dict[str, str] = {
    "von": "von", "from": "von", "da": "von",
    "an": "an", "to": "an", "a": "an",
    "cc": "cc", "bcc": "cc",
    "betreff": "betreff", "subject": "betreff", "oggetto": "betreff",
    "gesendet": "datum", "sent": "datum", "datum": "datum", "date": "datum", "data": "datum", "inviato": "datum",
}  # fmt: skip


def lese_pseudokopf(text: str) -> tuple[dict[str, str], str]:
    """Trennt Outlook-artige Kopfzeilen ("Von:", "An:", "Betreff:", "Gesendet:") vom Text.

    Liefert ``({"von", "an", "cc", "betreff", "datum"}, resttext)``. Ohne mindestens eines der
    Felder Betreff/Von/An gilt der Text als kopflos und wird unverändert zurückgegeben.
    """
    zeilen = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    start = 0
    while start < len(zeilen) and not zeilen[start].strip():
        start += 1
    felder: dict[str, str] = {}
    letztes: str | None = None
    index = start
    while index < len(zeilen):
        zeile = zeilen[index]
        if not zeile.strip():
            break
        treffer = _PSEUDOKOPF_ZEILE.match(zeile.strip())
        if treffer:
            feld = _PSEUDOKOPF_FELD[treffer.group("name").casefold()]
            wert = treffer.group("wert").strip()
            if feld in felder and feld == "an":
                felder[feld] = f"{felder[feld]}, {wert}"
            elif feld not in felder:
                felder[feld] = wert
            letztes = feld
        elif letztes and zeile[:1] in (" ", "\t"):
            felder[letztes] = f"{felder[letztes]} {zeile.strip()}".strip()
        else:
            break
        index += 1
    if not felder or not ({"betreff", "von", "an"} & set(felder)):
        return {}, text
    rest = "\n".join(zeilen[index:]).strip("\n")
    return felder, rest


def lese_txt(datei: Path) -> EmailInhalt:
    """Liest eine Textdatei (UTF-8 oder Windows-1252), optional mit Outlook-Kopfzeilen."""
    datei = Path(datei)
    try:
        roh = _bytes_zu_text(datei.read_bytes())
    except OSError as exc:
        raise EmailFehler(f"Textdatei kann nicht gelesen werden: {datei} ({exc})") from exc
    kopf, text = lese_pseudokopf(roh)
    absender = _adressen(kopf.get("von"))
    return EmailInhalt(
        quelldatei=datei,
        format="txt",
        betreff=kopf.get("betreff") or None,
        absender=absender[0] if absender else None,
        empfaenger=_adressen(kopf.get("an")) + _adressen(kopf.get("cc")),
        datum=zahlen.parse_datum(kopf.get("datum")),
        text=text.replace("\r\n", "\n").replace("\r", "\n"),
    )


# ---------------------------------------------------------------------------
# .msg lesen (Outlook, nur mit extract_msg)
# ---------------------------------------------------------------------------


def _lade_extract_msg() -> Any | None:
    try:
        return importlib.import_module("extract_msg")
    except ImportError:
        return None


def msg_unterstuetzt() -> bool:
    """True, wenn das Paket ``extract_msg`` importierbar ist (.msg-Dateien lesbar)."""
    return _lade_extract_msg() is not None


def _msg_text(wert: Any) -> str | None:
    if wert is None:
        return None
    if isinstance(wert, bytes):
        wert = _bytes_zu_text(wert)
    text = str(wert).strip()
    return text or None


def lese_msg(datei: Path) -> EmailInhalt:
    """Liest eine Outlook-.msg-Datei über ``extract_msg``; ohne das Paket :class:`MsgNichtVerfuegbar`."""
    datei = Path(datei)
    modul = _lade_extract_msg()
    if modul is None:
        raise MsgNichtVerfuegbar(MSG_HINWEIS_TEXT)
    # Annahme zur extract_msg-API (openMsg/Message, subject, sender, to, cc, date, body, htmlBody,
    # attachments[].longFilename/.data): am Windows-PC mit installiertem Paket prüfen.
    oeffnen = getattr(modul, "openMsg", None) or getattr(modul, "Message", None)
    if oeffnen is None:
        raise MsgNichtVerfuegbar("extract_msg ist installiert, bietet aber weder openMsg noch Message an.")
    try:
        msg = oeffnen(str(datei))
    except Exception as exc:
        raise EmailFehler(f".msg-Datei kann nicht gelesen werden: {datei} ({exc})") from exc
    try:
        betreff = _msg_text(getattr(msg, "subject", None))
        absender = _adressen(_msg_text(getattr(msg, "sender", None)))
        empfaenger = _adressen(_msg_text(getattr(msg, "to", None))) + _adressen(_msg_text(getattr(msg, "cc", None)))
        datum_roh = getattr(msg, "date", None)
        if isinstance(datum_roh, datetime):
            datum: date | None = datum_roh.date()
        elif isinstance(datum_roh, date):
            datum = datum_roh
        else:
            datum = _datum_aus_header(_msg_text(datum_roh))
        text = _msg_text(getattr(msg, "body", None)) or ""
        if not text.strip():
            text = html_zu_text(_msg_text(getattr(msg, "htmlBody", None)))
        anhaenge: list[Anhang] = []
        for nr, anhang in enumerate(getattr(msg, "attachments", None) or [], start=1):
            daten = getattr(anhang, "data", None)
            if not isinstance(daten, bytes) or not daten:
                continue  # eingebettete Nachrichten o. ä.
            name = (
                _msg_text(getattr(anhang, "longFilename", None))
                or _msg_text(getattr(anhang, "shortFilename", None))
                or _msg_text(getattr(anhang, "name", None))
                or f"anhang_{nr}"
            )
            ctype = _msg_text(getattr(anhang, "mimetype", None)) or "application/octet-stream"
            anhaenge.append(Anhang(dateiname=name, inhalt=daten, content_type=ctype))
    except EmailFehler:
        raise
    except Exception as exc:
        raise EmailFehler(f".msg-Inhalt kann nicht ausgewertet werden: {datei} ({exc})") from exc
    finally:
        schliessen = getattr(msg, "close", None)
        if callable(schliessen):
            try:
                schliessen()
            except Exception as exc:  # pragma: no cover
                logger.debug("msg.close() fehlgeschlagen: %s", exc)
    return EmailInhalt(
        quelldatei=datei,
        format="msg",
        betreff=betreff,
        absender=absender[0] if absender else None,
        empfaenger=empfaenger,
        datum=datum,
        text=text.replace("\r\n", "\n").replace("\r", "\n"),
        anhaenge=anhaenge,
    )


def lese_email(datei: Path) -> EmailInhalt:
    """Liest .eml/.txt/.msg anhand der Dateiendung."""
    datei = Path(datei)
    endung = datei.suffix.lower()
    if endung not in EMAIL_ENDUNGEN:
        raise ValueError(f"Keine E-Mail-Datei: {datei.name} (erwartet {', '.join(sorted(EMAIL_ENDUNGEN))})")
    if not datei.is_file():
        raise FileNotFoundError(f"E-Mail-Datei nicht gefunden: {datei}")
    if endung == ".eml":
        return lese_eml(datei)
    if endung == ".msg":
        return lese_msg(datei)
    return lese_txt(datei)


# ---------------------------------------------------------------------------
# Zitate / Weiterleitungen
# ---------------------------------------------------------------------------

_ZITAT_TRENNER = re.compile(
    r"^(?:-{2,}\s*(?:Ursprüngliche Nachricht|Ursprungliche Nachricht|Original Message|Originalnachricht"
    r"|Messaggio originale|Weitergeleitete Nachricht|Forwarded message|Original-Nachricht)\s*-{2,}"
    r"|_{10,}"
    r"|Am\s.{3,80}?\sschrieb\s.{1,80}?:"
    r"|On\s.{3,80}?\swrote:"
    r"|Il\s(?:giorno\s)?.{3,80}?\sha\sscritto:"
    r"|Le\s.{3,80}?\sa\sécrit\s?:)\s*$",
    re.I,
)
_VON_ZEILE = re.compile(r"^(?:Von|From|Da)\s*:\s*\S", re.I)
_KOPF_FOLGE = re.compile(r"^(?:Gesendet|Sent|An|To|Betreff|Subject|Cc|Inviato|A|Oggetto)\s*:", re.I)
_ZITATZEICHEN = re.compile(r"^(?:\s?>)+\s?")


def _ohne_zitatzeichen(text: str) -> str:
    return "\n".join(_ZITATZEICHEN.sub("", z) for z in text.split("\n"))


def teile_zitat(text: str) -> tuple[str, str | None]:
    """Trennt den eigenen Teil einer Mail vom zitierten/weitergeleiteten Teil.

    Liefert ``(eigener_teil, zitat_ohne_zitatzeichen_oder_None)``. Erkannt werden
    "-----Ursprüngliche Nachricht-----", Outlook-Trennlinien, "Am … schrieb …:", "On … wrote:",
    ein "Von:"-Block mit folgender "An:"/"Betreff:"-Zeile sowie ">"-zitierte Zeilen.
    """
    zeilen = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    for index, zeile in enumerate(zeilen):
        s = zeile.strip()
        if not s:
            continue
        if _ZITAT_TRENNER.match(s):
            eigen = "\n".join(zeilen[:index]).strip("\n")
            return eigen, _ohne_zitatzeichen("\n".join(zeilen[index + 1 :])).strip("\n")
        if _VON_ZEILE.match(s) and any(_KOPF_FOLGE.match(f.strip()) for f in zeilen[index + 1 : index + 6]):
            eigen = "\n".join(zeilen[:index]).strip("\n")
            return eigen, _ohne_zitatzeichen("\n".join(zeilen[index:])).strip("\n")
        if s.startswith(">"):
            eigen = "\n".join(zeilen[:index]).strip("\n")
            return eigen, _ohne_zitatzeichen("\n".join(zeilen[index:])).strip("\n")
    return text, None


# ---------------------------------------------------------------------------
# Namen: Lieferant und Endkunde
# ---------------------------------------------------------------------------

_ANREDE_START = re.compile(
    r"^(?:Sehr\s+geehrte[rs]?|Sehr\s+verehrte[rs]?|Liebe[rs]?|Hallo|Guten\s+(?:Tag|Morgen|Abend)|Moin|Servus"
    r"|Grüß\s+Gott|Dear|Hi|Hello|Gentili|Gentile|Gentilissim[oiae]|Buongiorno|Buonasera|Salve|Egregi[oa]?"
    r"|Spettabile|Spett\.?le|Bonjour|Estimad[oa]s?)\b\s*",
    re.I,
)
_NAME_ENDE_LOOKAHEAD = (
    r"(?=\s*(?:[,;:!?()|/\[\]{}<>\"„“”]|\s[–—-]\s|$|\.\s+[A-ZÄÖÜ][a-zäöüß]{2,}"
    r"|\s+(?i:für|fuer|zu|zum|zur|bis|ab|am|in|mit|und|wegen|bzgl\.?|betr\.?|bitte|per|KW|Liefer\w*|vom"
    r"|anbei|als|im|über|ueber|aus|from|for|to|at|hiermit|wir|ich|folgende[nrs]?|nachstehend\w*)\b))"
)
_NAME = r"(?P<name>[A-ZÄÖÜ][\w&.'’\-]*(?:\s+[\w&.'’\-]+){0,5}?)" + _NAME_ENDE_LOOKAHEAD
_FIRMENMARKER = re.compile(
    r"\b(?:srl|s\.r\.l\.?|GmbH|AG|KG|OHG|GbR|e\.?K\.?|S\.?p\.?A\.?|snc|sas|Ltd\.?|Inc\.?|Sarl|S\.?A\.?S\.?|BV|B\.V\.?"
    r"|Weingut|Cantina|Cantine|Azienda|Tenuta|Castello|Vigneti|Domaine|Château|Chateau|Bodega|Winery|Vineyards?"
    r"|Società|Societa|Soc\.|Agricola|Team|Firma|Vinothek|Weinhandel|Weinhaus|Kellerei|Import|Handel|srls)\b",
    re.I,
)
_PERSONENANREDE = re.compile(r"^(?:Herr|Herrn|Frau|Mr\.?|Mrs\.?|Ms\.?|Sig\.?|Signor|Signora|Dott\.?|Dr\.?|Prof\.?)\b", re.I)
_ANREDE_FIRMA_MUSTER: list[re.Pattern[str]] = [
    # "Damen und Herren der Cide srl", "Team von Bollina", "Signori della Cantina Colosi"
    re.compile(
        r"(?i:(?:Damen\s+und\s+Herren|Team|Kolleg(?:inn)?en(?:\s+und\s+Kollegen)?|Mitarbeiter(?:innen)?"
        r"|Ladies\s+and\s+Gentlemen|Sirs?|Madams?|Signori|Signore|Signora|Signor)"
        r"\s+(?:der|des|von|vom|bei|of|at|from|della|delle|dell['’]|dei|degli|del|di|da|de|chez)\s+)" + _NAME
    ),
    # "Cide-Team", "Cide srl Team"
    re.compile(_NAME.replace(_NAME_ENDE_LOOKAHEAD, "") + r"[\s\-]+(?i:Team)\b"),
    # "Team Cide"
    re.compile(r"(?i:\bTeam)[\s\-]+" + _NAME),
    # "Spettabile Cide srl", "Spett.le Ditta Cide"
    re.compile(r"(?i:\bSpett(?:abile|\.?le))\s+(?:(?i:Ditta|Società|Azienda)\s+)?" + _NAME),
]
_ANREDE_DIREKT = re.compile(r"^(?:(?i:Firma)\s+)?" + _NAME)
_NAME_STOPP = frozenset(
    {
        "sie", "ihnen", "ihr", "ihre", "ihren", "ihrer", "ihrem", "ihres", "uns", "unser", "unsere", "unseren",
        "unserer", "unserem", "die", "der", "das", "den", "dem", "des", "ein", "eine", "einen", "einer", "einem",
        "alle", "alles", "jeden", "jede", "jedes", "diese", "dieser", "dieses", "diesen", "mich", "dich", "euch",
        "mein", "meine", "dein", "deine", "euer", "eure", "herr", "herrn", "frau", "rückfragen", "rueckfragen",
        "rückfrage", "lieferung", "anlieferung", "kw", "kalenderwoche", "woche", "heute", "morgen", "weihnachten",
        "ostern", "bestellung", "bestellungen", "auftrag", "angebot", "info", "information", "kenntnisnahme",
        "weiterleitung", "freigabe", "prüfung", "pruefung", "ansicht", "abholung", "versand", "zahlung", "rechnung",
        "lager", "januar", "februar", "märz", "maerz", "april", "mai", "juni", "juli", "august", "september",
        "oktober", "november", "dezember", "montag", "dienstag", "mittwoch", "donnerstag", "freitag", "samstag",
        "sonntag", "unterlagen", "akten", "weitere", "weiteres", "sofort", "sofortige", "sofortigen", "nächste",
        "naechste", "nächsten", "naechsten", "neue", "neuen", "folgende", "folgenden", "obige", "obigen", "was",
        "wen", "wer", "team", "damen", "zusammen", "liebe", "lieber", "you", "us", "our", "the", "your", "customer",
        "kunde", "kunden", "endkunde", "kommission", "oben", "unten", "genannte", "genannten", "ihrerseits",
    }
)  # fmt: skip
_NAME_SCHNITT = re.compile(
    r"\s*(?:[,;:!?()|/\[\]{}<>\"„“”]|\s[–—-]\s"
    r"|\s+(?:für|fuer|zu|zum|zur|bis|ab|am|in|mit|und|wegen|bzgl\.?|betr\.?|bitte|per|KW|Liefer\w*|vom"
    r"|anbei|als|im|über|ueber|aus|from|for|to|at|hiermit|wir|ich)\b).*$",
    re.I,
)
_NAME_PRAEFIX = re.compile(r"^(?:Firma|Fa\.|die|der|das|den|an|bei|Herrn|Herr|Frau|z\.?\s?Hd\.?)\s+", re.I)


def _schneide_name(wert: str | None) -> str | None:
    """Kürzt einen Firmen-/Kundennamen an Satzzeichen und Füllwörtern ("Cide srl, Treviso für …")."""
    if not wert:
        return None
    s = re.sub(r"\s+", " ", wert).strip()
    s = _NAME_PRAEFIX.sub("", s)
    s = _NAME_SCHNITT.sub("", s).strip(" .,;:-–—'\"")
    if len(s) < 2 or not re.search(r"[A-Za-zÄÖÜäöüß]{2}", s):
        return None
    if _normalisiere(s) in _NAME_STOPP or _normalisiere(s.split()[0]) in _NAME_STOPP:
        return None
    return s


def lieferant_aus_anrede(text: str | None) -> str | None:
    """Liest den Lieferanten aus der Anrede ("Sehr geehrte Damen und Herren der Cide srl").

    Personenanreden ("Sehr geehrter Herr Rossi") liefern ``None``; ein bloßer Name hinter
    "Hallo"/"Guten Tag" nur, wenn er wie eine Firma aussieht ("Guten Tag Cide srl").
    """
    if not text:
        return None
    for zeile in text.split("\n"):
        s = zeile.strip()
        if not s or not _ANREDE_START.match(s):
            continue
        rest = _ANREDE_START.sub("", s, count=1).strip()
        if not rest:
            continue
        for muster in _ANREDE_FIRMA_MUSTER:
            treffer = muster.search(rest)
            if treffer:
                name = _schneide_name(treffer.group("name"))
                if name:
                    return name
        if _PERSONENANREDE.match(rest):
            return None
        treffer = _ANREDE_DIREKT.match(rest)
        if treffer:
            name = _schneide_name(treffer.group("name"))
            if name and _FIRMENMARKER.search(name):
                return name
        return None
    return None


_KUNDE_MUSTER: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(
            r"(?i:\bf[üu]r\s+(?:unseren|unsere[nr]?|unser|unsern|den|die|das|einen|eine)?\s*"
            r"(?:Kunden|Kunde|Kundin|Endkunden|Endkunde|Kommission)\s+)" + _NAME
        ),
        "fuer_kunden",
    ),
    (re.compile(r"(?i:\bKommission)\s*:?\s+" + _NAME), "kommission"),
    (re.compile(r"(?i:\b(?:End)?Kunde)\s+" + _NAME), "kunde"),
    (re.compile(r"(?i:\bf[üu]r)\s+" + _NAME), "fuer"),
]
_KUNDE_RANG = {"kopffeld": 0, "fuer_kunden": 1, "kommission": 2, "kunde": 3, "fuer": 4}


def endkunde_aus_text(text: str | None) -> list[tuple[str, str]]:
    """Alle Endkunden-Kandidaten eines Textes als ``(name, muster)`` in Textreihenfolge."""
    if not text:
        return []
    kandidaten: list[tuple[str, str]] = []
    gesehen: set[str] = set()
    for zeile in text.split("\n"):
        s = zeile.strip()
        if not s:
            continue
        for muster, art in _KUNDE_MUSTER:  # Reihenfolge = Rang, erster Treffer je Name gewinnt
            for treffer in muster.finditer(s):
                name = _schneide_name(treffer.group("name"))
                if name and _normalisiere(name) not in gesehen:
                    gesehen.add(_normalisiere(name))
                    kandidaten.append((name, art))
    return kandidaten


# TODO-LEXWARE: Betreffzeile, Textbaustein und PDF-Dateiname des Lexware-Mailversands ("Bestellung per
# E-Mail", z. B. "Bestellung BE-2026-0077") am echten System prüfen und die Muster hier ergänzen.
_BESTELLNUMMER_FREI = re.compile(
    r"\b(?:Bestellung|Bestell|Order|Auftrag|Purchase\s+Order|PO|Ordine)\s*[-–]?\s*(?:Nr\.?|No\.?|Nummer|#)?\s*:?\s*"
    r"(?P<wert>(?:[A-Z]{1,5}[-_/]?\d{2,}[A-Za-z0-9\-/_]*|\d{4,}))(?![-./]\d)\b"
)


def _belegnummer_aus_text(*texte: str | None) -> str | None:
    for text in texte:
        if not text:
            continue
        treffer = _BESTELLNUMMER_FREI.search(text)
        if treffer:
            return treffer.group("wert").rstrip("-_/")
    return None


_LIEFERBEDINGUNG = re.compile(
    r"\b(?:Lieferbedingung(?:en)?|Incoterms?|Lieferkondition(?:en)?|Frachtbedingung(?:en)?)\s*:\s*(?P<wert>[^\n,;]{2,40})"
    r"|(?P<frei>\b(?:frei\s+Haus|frei\s+Lager|ab\s+Werk|ab\s+Keller|ab\s+Hof|ex\s+cellar|ex\s+works|franco\s+cantina"
    r"|franco\s+destino|unfrei|EXW|FCA|DAP|DDP|CIF|FOB)\b)",
    re.I,
)
_LADEADRESSE = re.compile(
    r"\b(?:Ladeadresse|Abholadresse|Ladestelle|Abholort|Abholung\s+bei|Abholung\s+ab|Pick-?up(?:\s+address)?|Ladeort)"
    r"\s*:?\s*(?P<wert>[^\n]{3,})",
    re.I,
)


def _lieferbedingung_aus_text(*texte: str | None) -> str | None:
    for text in texte:
        if not text:
            continue
        treffer = _LIEFERBEDINGUNG.search(text)
        if treffer:
            wert = treffer.group("wert") or treffer.group("frei")
            return re.sub(r"\s+", " ", wert).strip(" .") or None
    return None


def _ladeadresse_aus_text(*texte: str | None) -> str | None:
    for text in texte:
        if not text:
            continue
        treffer = _LADEADRESSE.search(text)
        if treffer:
            return treffer.group("wert").strip(" .,;") or None
    return None


_LIEFERTAG_FREITEXT = re.compile(
    r"\b(?:Lieferung|Anlieferung|Zustellung|Auslieferung|liefern|Versand)\s+(?:bitte\s+)?(?:bis|zum|zur|am|in|ab"
    r"|spätestens|spaetestens|für|fuer|per|möglichst|moeglichst)\s+(?:zum\s+|zur\s+|der\s+|dem\s+|den\s+"
    r"|spätestens\s+|spaetestens\s+)?(?P<wert>[^\n,;]{2,40})",
    re.I,
)
_KW_MUSTER = re.compile(r"\b(?:KW|Kalenderwoche|Woche)\s*\.?\s*(?P<kw>\d{1,2})(?:\s*/\s*(?P<jahr>\d{2,4}))?", re.I)


def _liefertag_aus_freitext(*texte: str | None) -> str | None:
    """Rohwert des Liefertags aus Fließtext ("Lieferung bitte bis 28.10.2026", "Lieferung in KW 44")."""
    rohwert: str | None = None
    for text in texte:
        if not text:
            continue
        for zeile in text.split("\n"):
            treffer = _LIEFERTAG_FREITEXT.search(zeile)
            if treffer:
                wert = treffer.group("wert").strip(" .")
                gefunden = zahlen.finde_datum(wert)
                if gefunden:
                    return gefunden[1]
                kw = _KW_MUSTER.search(wert)
                if kw and rohwert is None:
                    rohwert = kw.group(0)
                elif rohwert is None:
                    rohwert = wert
            elif rohwert is None:
                kw = _KW_MUSTER.search(zeile)
                if kw and re.search(r"\bLiefer|\bAnliefer|\bZustell", zeile, re.I):
                    rohwert = kw.group(0)
    return rohwert


# ---------------------------------------------------------------------------
# Positionen aus Textzeilen
# ---------------------------------------------------------------------------

_E = zahlen.EINHEIT_MUSTER
_PREIS = r"\d{1,3}(?:\.\d{3})*,\d{1,2}|\d{1,6}\.\d{2}"
_WAEHRUNG = r"(?:€|EUR|Euro|eur)"
_PREIS_SCHLUESSEL = (
    r"(?:à|á|a|je|zu|zum\s+Preis\s+von|zum|@|EP|EK|VK|Einzelpreis|Einzelpreis\s+netto|Preis|Stückpreis|netto)"
)
_JE_EINHEIT = r"(?:\s*(?:/|je|pro|per)\s*(?:Fl\.?|Flasche|Stk\.?|St\.?|Stück|Btl\.?|Bottle))?"
_PREIS_TEIL = (
    rf"(?:\s*[,;]?\s*(?:(?P<schluessel>{_PREIS_SCHLUESSEL})\s*:?\s*)?(?P<waehrung_vor>{_WAEHRUNG})?\s*"
    rf"(?P<preis>{_PREIS})\s*(?P<waehrung>{_WAEHRUNG})?{_JE_EINHEIT}(?:\s*netto)?)?"
)
_GESAMT_TEIL = (
    rf"(?:\s*[=,;(]?\s*(?:gesamt|Gesamt|Summe|total)?\s*:?\s*(?:{_WAEHRUNG})?\s*(?P<gesamt>{_PREIS})"
    rf"\s*(?:{_WAEHRUNG})?\)?)?"
)
_GEBINDE = rf"(?:\s*(?:x|×|\*|à|á|je|mal)(?:\s*(?P<n2>\d{{1,3}})(?![\d,.])(?:\s*(?:{_E})\.?)?)?)?"
_MENGE_TEIL = rf"(?P<mengeteil>\d{{1,4}}(?:\s*(?:{_E})\.?)?{_GEBINDE})"
_MENGE_TEIL_MIT_EINHEIT = rf"(?P<mengeteil>\d{{1,4}}\s*(?:{_E})\.?{_GEBINDE})"
_MENGE_VORN = re.compile(rf"^{_MENGE_TEIL}\s+(?P<bez>.+?){_PREIS_TEIL}{_GESAMT_TEIL}\s*$", re.I)
_MENGE_VORN_ARTNR = re.compile(
    rf"^(?P<artnr>[A-Za-z]{{0,3}}-?\d{{3,6}})\s+{_MENGE_TEIL_MIT_EINHEIT}\s+(?P<bez>.+?){_PREIS_TEIL}{_GESAMT_TEIL}\s*$",
    re.I,
)
_MENGE_HINTEN_EINHEIT = re.compile(
    rf"^(?P<bez>[^\W\d_].*?)(?:\s*[:–—\-]\s*|\s+){_MENGE_TEIL_MIT_EINHEIT}{_PREIS_TEIL}{_GESAMT_TEIL}\s*$",
    re.I,
)
_MENGE_HINTEN_DOPPELPUNKT = re.compile(
    rf"^(?P<bez>[^\W\d_][^:]{{2,80}}?)\s*:\s*{_MENGE_TEIL}{_PREIS_TEIL}{_GESAMT_TEIL}\s*$", re.I
)
_HAT_EINHEIT = re.compile(rf"\d\s*(?:{_E}|x|×|\*)(?![A-Za-zäöü])", re.I)
_VOLUMEN_ZAHL = re.compile(r"^(?:0[.,]\d{1,3}|1[.,]5|3[.,]0|1[.,]0|0[.,]7)$")
_BULLET = re.compile(r"^(?:[-–—•*·▪►>o]\s+)+")
_NUMMERIERUNG = re.compile(
    r"^\(?\d{1,2}[.)]\s+(?!(?:Jan|Feb|Mär|Maer|Mar|Apr|Mai|May|Jun|Jul|Aug|Sep|Okt|Oct|Nov|Dez|Dec)\w*\b)"
)
_ARTNR = re.compile(
    r"[(\[]?\b(?:Art(?:ikel)?\.?\s*-?\s*(?:Nr|No|Nummer)?\.?\s*:?|#)\s*(?P<nr>[A-Za-z]?\d[A-Za-z0-9\-/.]{1,14})\b[\])]?",
    re.I,
)
_HINWEIS_MUSTER = re.compile(
    r"(?<![\d,.])(\d{1,2}\s*\+\s*\d{1,2})(?![\d,.])"
    r"|\b(gratis|kostenlos|kostenfrei|unentgeltlich|Naturalrabatt|Gratisflaschen?|Freiware|ohne\s+Berechnung"
    r"|o\.\s?B\.|Muster|Probeflaschen?|Dreingabe|Aktion)\b",
    re.I,
)
_SUMMENZEILE = re.compile(
    r"^(?:Summe|Gesamt|Gesamtmenge|Zwischensumme|Netto|Brutto|MwSt|USt|Total|Insgesamt|Endbetrag|Übertrag|zzgl)",
    re.I,
)
_BUCHSTABEN = re.compile(r"[A-Za-zÄÖÜäöüß]{2}")
_UNERWUENSCHTE_BEZ = re.compile(
    r"^(?:Datum|Liefer|Bestell|Kunde|Tel|Fax|Mobil|E-?Mail|Seite|Uhr|Stunden?|Tage?|Jahre?|Wochen?|Minuten?"
    r"|Prozent|%|Mal|Kartons?\s*$|Flaschen\s*$|Rabatt|Skonto|Zahlungsziel|Werktage|Palette)",
    re.I,
)
# Zeilen über Anhänge/Dokumente sind keine Positionen ("1 x Preisliste anbei", "2 Exemplare als PDF")
_KEINE_POSITION = re.compile(
    r"\b(?:anbei|beigefügt|beigefuegt|beiliegend|Anhang|Anlage|Preisliste|Bestellformular|Datei|PDF|Kopie|Kopien"
    r"|Exemplare?|Seiten?|Unterschrift|Rückfragen|Rueckfragen)\b",
    re.I,
)
_STOPP_BEZ_WOERTER = frozenset(
    {
        "tel", "telefon", "fax", "mobil", "handy", "phone", "plz", "zip", "nr", "seite", "page", "ust", "ustid",
        "ustidnr", "steuernr", "iban", "bic", "konto", "summe", "gesamt", "total", "netto", "brutto", "mwst",
        "rabatt", "skonto", "zahlungsziel", "kw", "kalenderwoche", "woche", "jahr", "jahrgang", "menge", "anzahl",
        "pos", "position", "artikel", "art", "preis", "ek", "vk", "kunde", "kundennummer", "kundennr", "lieferant",
        "datum", "liefertermin", "lieferung", "bestellung", "bestellnummer", "bestellnr", "auftrag", "beleg",
        "rechnung", "ihre", "unsere", "die", "der", "das", "wir", "sie", "bitte", "insgesamt", "zwischensumme",
        "versand", "fracht", "porto", "zeile", "zeilen", "mindestbestellmenge", "mindestmenge", "lager", "bestand",
    }
)  # fmt: skip


def _ist_kopffeldzeile(zeile: str) -> bool:
    return bool(lese_kopffelder([zeile]))


def _hinweis_trennen(text: str) -> tuple[str, str | None]:
    hinweise: list[str] = []

    def merken(treffer: re.Match[str]) -> str:
        wert = treffer.group(0).strip()
        if wert.casefold() not in {h.casefold() for h in hinweise}:
            hinweise.append(wert)
        return " "

    bereinigt = _HINWEIS_MUSTER.sub(merken, text)
    bereinigt = re.sub(r"\(\s*\)|\[\s*\]", " ", bereinigt)
    bereinigt = re.sub(r"\s+", " ", bereinigt).strip(" -–—,;:/·•")
    return bereinigt, ("; ".join(hinweise) if hinweise else None)


@dataclass
class _Kandidat:
    index: int
    position: RohPosition
    sicher: bool


def _bereinige_bezeichnung(bez: str) -> str:
    bez = re.sub(r"\s+", " ", bez).strip()
    bez = re.sub(r"^(?:à|á|je|a)\s+", "", bez, flags=re.I)
    bez = re.sub(r"\s+(?:à|á|je|zu|zum|a|@|EP|EK|VK|Einzelpreis|Preis)\s*:?\s*$", "", bez, flags=re.I)
    return bez.strip(" -–—,;:/·•()[]")


def _parse_positionszeile(zeile: str) -> _Kandidat | None:
    s = zeile.strip()
    s = _BULLET.sub("", s)
    s = _NUMMERIERUNG.sub("", s)
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) < 3:
        return None
    if _SUMMENZEILE.match(s) or _ANREDE_START.match(s) or _KEINE_POSITION.search(s) or _ist_kopffeldzeile(s):
        return None

    artikelnummer: str | None = None
    treffer_art = _ARTNR.search(s)
    if treffer_art:
        artikelnummer = treffer_art.group("nr").rstrip(".")
        s = re.sub(r"\s+", " ", s[: treffer_art.start()] + " " + s[treffer_art.end() :]).strip(" -–—:,;")

    for muster, form in (
        (_MENGE_VORN_ARTNR, "vorn"),
        (_MENGE_VORN, "vorn"),
        (_MENGE_HINTEN_EINHEIT, "hinten"),
        (_MENGE_HINTEN_DOPPELPUNKT, "hinten"),
    ):
        treffer = muster.match(s)
        if not treffer:
            continue
        g = treffer.groupdict()
        bez = _bereinige_bezeichnung(g.get("bez") or "")
        if not _BUCHSTABEN.search(bez) or _UNERWUENSCHTE_BEZ.match(bez):
            continue
        if form == "hinten":
            woerter = [_normalisiere(w) for w in bez.split()]
            if not woerter or woerter[0] in _STOPP_BEZ_WOERTER or _normalisiere(bez) in _STOPP_BEZ_WOERTER:
                continue
        menge_obj = zahlen.parse_menge(g["mengeteil"])
        if menge_obj is None or menge_obj.wert <= 0:
            continue
        if not artikelnummer and g.get("artnr"):
            artikelnummer = g["artnr"]

        preis_text = g.get("preis")
        if preis_text and not (g.get("waehrung") or g.get("waehrung_vor") or g.get("schluessel")):
            if _VOLUMEN_ZAHL.match(preis_text):
                # "Gloria 2023 0,75" – nackte Volumenzahl ist kein Preis
                bez = f"{bez} {preis_text}".strip()
                preis_text = None
        preis = zahlen.parse_dezimal(preis_text)

        bez, hinweis = _hinweis_trennen(bez)
        if not bez:
            continue
        jahrgang = volumen.parse_jahrgang(bez)
        volumen_cl = volumen.parse_volumen_cl(bez)
        einheit = menge_obj.einheit
        hat_einheit = bool(_HAT_EINHEIT.search(g["mengeteil"]))
        sicher = bool(
            hat_einheit
            or preis is not None
            or artikelnummer
            or jahrgang is not None
            or volumen_cl is not None
            or menge_obj.gebinde is not None
            or form == "hinten"
        )
        position = RohPosition(
            laufnr=0,
            bezeichnung=bez,
            jahrgang=jahrgang,
            menge=menge_obj.wert,
            einheit=einheit,
            preis=preis,
            volumen_cl=volumen_cl,
            artikelnummer=artikelnummer,
            hinweis=hinweis,
            durchgestrichen=False,
            rohtext=zeile.strip(),
        )
        return _Kandidat(index=-1, position=position, sicher=sicher)
    return None


def lese_positionen(zeilen: list[str]) -> list[RohPosition]:
    """Liest Bestellpositionen aus E-Mail-Textzeilen (Leerzeilen erlaubt, bestimmen Blöcke).

    Erkannt werden ``[-•*] Menge [Einheit] Bezeichnung [à Preis]``, ``Bezeichnung: Menge
    Einheit``, ``Art.-Nr. 1643 60 Fl …``, Gebinde ("12 x 6", "2 Kt à 6"), Hinweise ("11+1",
    "gratis", auch als Folgezeile). Zeilen ohne Einheit/Preis/Artikelnummer/Jahrgang zählen nur,
    wenn eine Nachbarzeile ebenfalls eine Position ist. ``laufnr`` wird 1..n vergeben.
    """
    kandidaten: dict[int, _Kandidat] = {}
    for index, zeile in enumerate(zeilen):
        if not zeile.strip():
            continue
        kandidat = _parse_positionszeile(zeile)
        if kandidat is None:
            continue
        kandidat.index = index
        kandidaten[index] = kandidat

    def nachbar(index: int) -> bool:
        for schritt in (-1, 1):
            j = index + schritt
            if j in kandidaten:
                return True
            if 0 <= j < len(zeilen) and not zeilen[j].strip() and (j + schritt) in kandidaten:
                return True
        return False

    positionen: list[RohPosition] = []
    letzter_index: int | None = None
    for index, zeile in enumerate(zeilen):
        kandidat = kandidaten.get(index)
        if kandidat is not None:
            if not kandidat.sicher and not nachbar(index):
                logger.debug("Lose Positionszeile ohne Nachbarn verworfen: %r", zeile.strip())
                continue
            positionen.append(kandidat.position)
            letzter_index = index
            continue
        s = _BULLET.sub("", zeile.strip())
        if positionen and letzter_index is not None and index == letzter_index + 1 and _HINWEIS_MUSTER.fullmatch(s):
            vorher = positionen[-1]
            hinweis = f"{vorher.hinweis}; {s}" if vorher.hinweis else s
            positionen[-1] = vorher.model_copy(update={"hinweis": hinweis})
            letzter_index = index
    for nr, position in enumerate(positionen, start=1):
        position.laufnr = nr
    return positionen


# ---------------------------------------------------------------------------
# Extraktor
# ---------------------------------------------------------------------------

_SIGNATUR_START = re.compile(
    r"^(?:--\s*$|Mit freundlichen Grüßen|Mit freundlichem Gruß|Mit besten Grüßen|Freundliche Grüße|Viele Grüße"
    r"|Beste Grüße|Herzliche Grüße|Schöne Grüße|Liebe Grüße|MfG\b|Kind regards|Best regards|Regards|Cordiali saluti"
    r"|Distinti saluti|Saluti|Grazie e saluti|Vielen Dank und)",
    re.I,
)
_GENERISCHE_DOMAINS = frozenset(
    {
        "gmail.com", "googlemail.com", "web.de", "gmx.de", "gmx.net", "gmx.at", "gmx.ch", "t-online.de", "outlook.com",
        "outlook.de", "hotmail.com", "hotmail.de", "live.com", "live.de", "yahoo.com", "yahoo.de", "icloud.com",
        "me.com", "aol.com", "freenet.de", "posteo.de", "mail.de", "libero.it", "alice.it", "tin.it", "virgilio.it",
        "tiscali.it", "pec.it", "example.com", "example.org", "example.net",
    }
)  # fmt: skip


def _hinweis(code: Pruefcode, grad: Schweregrad, text: str, **details: str) -> Pruefhinweis:
    return Pruefhinweis(code=code, schweregrad=grad, text=text, details=details)


class EmailExtraktor:
    """Extraktor für eigene Bestellungen an das Weingut aus .eml/.txt/.msg (Eingangstyp B)."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._text_pdf = TextPdfExtraktor(settings)
        self._eigene_namen: list[str] = []
        for firma in settings.firmen.values():
            for name in (firma.name, firma.lexware_firmenname):
                norm = _normalisiere(name)
                if len(norm) >= 4 and norm not in self._eigene_namen:
                    self._eigene_namen.append(norm)

    # -- Schnittstelle ------------------------------------------------------

    def kann_verarbeiten(self, datei: Path) -> bool:
        datei = Path(datei)
        endung = datei.suffix.lower()
        if endung not in EMAIL_ENDUNGEN or not datei.is_file():
            return False
        if endung == ".msg" and not msg_unterstuetzt():
            logger.warning("%s: %s", datei.name, MSG_HINWEIS_TEXT)
            return False
        return True

    def extrahiere(self, datei: Path) -> RohBestellung:
        datei = Path(datei)
        endung = datei.suffix.lower()
        if endung not in EMAIL_ENDUNGEN:
            raise ValueError(f"Keine E-Mail-Datei: {datei.name} (erwartet {', '.join(sorted(EMAIL_ENDUNGEN))})")
        if not datei.is_file():
            raise FileNotFoundError(f"E-Mail-Datei nicht gefunden: {datei}")
        if endung == ".msg" and not msg_unterstuetzt():
            logger.error("%s: %s", datei.name, MSG_HINWEIS_TEXT)
            return RohBestellung(
                eingangstyp=Eingangstyp.EIGENE_BESTELLUNG_EMAIL,
                quelldatei=datei,
                extraktionsmethode=Extraktionsmethode.EMAIL,
                hinweise=[_hinweis(Pruefcode.FELD_FEHLT, Schweregrad.FEHLER, MSG_HINWEIS_TEXT, datei=datei.name)],
            )
        inhalt = lese_email(datei)
        return self.extrahiere_inhalt(inhalt)

    # -- Aufbau -------------------------------------------------------------

    def _ist_eigene_firma(self, text: str | None) -> bool:
        """True, wenn der Text eine unserer Firmen nennt (auch verkürzt, z. B. die Mail-Domain "wein-import-kreis")."""
        norm = _normalisiere(text)
        if not norm:
            return False
        if any(name in norm for name in self._eigene_namen):
            return True
        return len(norm) >= 8 and any(norm in name for name in self._eigene_namen)

    def extrahiere_inhalt(self, inhalt: EmailInhalt) -> RohBestellung:
        """Baut die :class:`RohBestellung` aus einem bereits gelesenen :class:`EmailInhalt`."""
        hinweise: list[Pruefhinweis] = []
        datei = Path(inhalt.quelldatei)
        betreff = (inhalt.betreff or "").strip() or None
        empfaenger = list(inhalt.empfaenger)
        absender = inhalt.absender
        datum_mail = inhalt.datum

        # Eigener Teil vs. Zitat/Weiterleitung
        eigen, zitat = teile_zitat(inhalt.text or "")
        text = eigen
        if zitat and not self._hat_bestellinhalt(eigen) and self._hat_bestellinhalt(zitat):
            kopf_zitat, text_zitat = lese_pseudokopf(zitat)
            if kopf_zitat:
                betreff = kopf_zitat.get("betreff") or betreff
                an = _adressen(kopf_zitat.get("an")) + _adressen(kopf_zitat.get("cc"))
                if an:
                    empfaenger = an
                von = _adressen(kopf_zitat.get("von"))
                if von:
                    absender = von[0]
                datum_mail = zahlen.parse_datum(kopf_zitat.get("datum")) or datum_mail
            text = text_zitat
            hinweise.append(
                _hinweis(
                    Pruefcode.FELD_FEHLT,
                    Schweregrad.INFO,
                    "Bestelltext aus der zitierten/weitergeleiteten Nachricht übernommen.",
                )
            )
            logger.info("%s: eigener Mailteil ohne Bestellung, nutze zitierten Teil", datei.name)

        zeilen = [z.rstrip() for z in text.split("\n")]
        zeilen_vor_signatur = self._bis_signatur(zeilen)
        kopf = lese_kopffelder([betreff or "", *(z.strip() for z in zeilen if z.strip())])

        # Lieferant / Endkunde
        lieferant_text, lieferant_quelle = self._finde_lieferant(kopf, betreff, zeilen_vor_signatur, empfaenger, absender)
        kunde_text, kunde_quelle = self._finde_endkunde(kopf, betreff, zeilen_vor_signatur, lieferant_text)
        kunde_adresse: str | None = None

        empfaenger_firma_text = self._eigene_firma_text(absender, empfaenger, zeilen)

        # Nummern, Daten
        belegnummer = kopf.get("belegnummer") or kopf.get("bestellnummer") or _belegnummer_aus_text(betreff, text)
        bestellnummer = kopf.get("ihre_bestellnummer")
        datum_explizit = zahlen.parse_datum(kopf.get("datum"))
        liefertag_roh = kopf.get("liefertag")
        if not liefertag_roh or not zahlen.finde_datum(liefertag_roh):
            liefertag_roh = _liefertag_aus_freitext(betreff, text) or liefertag_roh
        liefertag = zahlen.parse_datum(liefertag_roh)

        # Textheuristiken aus text_pdf (Lieferadresse, Summenvermerke, Tabellen-Fallback)
        basis = self._basis_aus_text(text, datei)
        lieferadresse = basis.lieferadresse if basis else None
        summenvermerke: list[Summenvermerk] = list(basis.summenvermerke) if basis else []

        # Positionen aus dem Mailtext
        positionen = lese_positionen(zeilen_vor_signatur)
        positionsquelle = "mailtext"
        if not positionen and basis and basis.positionen:
            positionen = [p.model_copy() for p in basis.positionen]
            positionsquelle = "mailtext (Tabellenheuristik)"

        # Anhänge
        anhang_ergebnisse = self._verarbeite_anhaenge(inhalt, hinweise)
        mail_hatte_positionen = bool(positionen)
        for name, roh in anhang_ergebnisse:
            if not roh.positionen:
                continue
            if mail_hatte_positionen:
                hinweise.append(
                    _hinweis(
                        Pruefcode.FELD_FEHLT,
                        Schweregrad.INFO,
                        f"PDF-Anhang '{name}' enthält {len(roh.positionen)} Positionen; verwendet wurden die "
                        f"{len(positionen)} Positionen aus dem Mailtext.",
                        anhang=name,
                    )
                )
                mail_gesamt = sum((p.menge or Decimal(0)) for p in positionen)
                if roh.gesamtmenge != mail_gesamt:
                    hinweise.append(
                        _hinweis(
                            Pruefcode.SUMME_ABWEICHUNG,
                            Schweregrad.WARNUNG,
                            f"Gesamtmenge im Mailtext ({mail_gesamt}) weicht vom PDF-Anhang '{name}' "
                            f"({roh.gesamtmenge}) ab.",
                            anhang=name,
                            mailtext=str(mail_gesamt),
                            anhang_menge=str(roh.gesamtmenge),
                        )
                    )
                continue
            versatz = len(positionen)
            for p in roh.positionen:
                positionen.append(p.model_copy(update={"laufnr": p.laufnr + versatz}))
            for h in roh.hinweise:
                if h.position_laufnr is not None:
                    hinweise.append(h.model_copy(update={"position_laufnr": h.position_laufnr + versatz}))
            summenvermerke.extend(roh.summenvermerke)
            positionsquelle = f"PDF-Anhang {name}"
            hinweise.append(
                _hinweis(
                    Pruefcode.FELD_FEHLT,
                    Schweregrad.INFO,
                    f"{len(roh.positionen)} Positionen aus dem PDF-Anhang '{name}' übernommen.",
                    anhang=name,
                )
            )

        # Kopffelder: Mail zuerst, fehlende aus den PDF-Anhängen
        datum_anhang: date | None = None
        for name, roh in anhang_ergebnisse:
            if roh.belegnummer and belegnummer and roh.belegnummer.casefold() != belegnummer.casefold():
                hinweise.append(
                    _hinweis(
                        Pruefcode.FELD_FEHLT,
                        Schweregrad.WARNUNG,
                        f"Belegnummer in der Mail ('{belegnummer}') und im PDF-Anhang '{name}' "
                        f"('{roh.belegnummer}') weichen ab.",
                        anhang=name,
                        mail=belegnummer,
                        anhang_wert=roh.belegnummer,
                    )
                )
            if not lieferant_text and roh.lieferant_text and not self._ist_eigene_firma(roh.lieferant_text):
                lieferant_text, lieferant_quelle = roh.lieferant_text, f"PDF-Anhang {name}"
            if not kunde_text and roh.kunde_text and not self._ist_eigene_firma(roh.kunde_text):
                kunde_text, kunde_quelle = roh.kunde_text, f"PDF-Anhang {name}"
                kunde_adresse = roh.kunde_adresse
            belegnummer = belegnummer or roh.belegnummer
            bestellnummer = bestellnummer or roh.bestellnummer
            datum_anhang = datum_anhang or roh.datum
            if liefertag is None and roh.liefertag is not None:
                liefertag = roh.liefertag
                liefertag_roh = roh.liefertag.isoformat()
            lieferadresse = lieferadresse or roh.lieferadresse
        datum = datum_explizit or datum_anhang or datum_mail

        # Laufnummern und Positionshinweise
        for nr, position in enumerate(positionen, start=1):
            if position.laufnr != nr:
                for h in hinweise:
                    if h.position_laufnr == position.laufnr and h.position_laufnr != nr:
                        h.position_laufnr = nr
                position.laufnr = nr
            if position.menge is None and not any(
                h.code == Pruefcode.FELD_FEHLT and h.position_laufnr == nr for h in hinweise
            ):
                hinweise.append(
                    Pruefhinweis(
                        code=Pruefcode.FELD_FEHLT,
                        schweregrad=Schweregrad.WARNUNG,
                        text=f"Position {nr}: Menge nicht erkannt ({position.bezeichnung}).",
                        position_laufnr=nr,
                    )
                )
        for vermerk in summenvermerke:
            hinweise.append(
                _hinweis(
                    Pruefcode.SUMMENVERMERK,
                    Schweregrad.INFO,
                    f"Summenvermerk erkannt: {vermerk.rohtext}",
                    wert=str(vermerk.wert),
                    einheit=vermerk.einheit or "",
                )
            )

        # Plausibilität: eigene Firma als Absender?
        if absender and absender.anzeige and not self._ist_eigene_firma(absender.name):
            if any(self._ist_eigene_firma(e.name) for e in empfaenger):
                hinweise.append(
                    _hinweis(
                        Pruefcode.FELD_FEHLT,
                        Schweregrad.WARNUNG,
                        f"Absender '{absender.anzeige}' ist nicht die eigene Firma, Empfänger schon – bitte prüfen, "
                        "ob es wirklich eine eigene Bestellung an das Weingut ist.",
                        absender=absender.anzeige,
                    )
                )

        # Fehlende Kopffelder
        if not lieferant_text:
            hinweise.append(_hinweis(Pruefcode.FELD_FEHLT, Schweregrad.WARNUNG, "Lieferant/Weingut nicht erkannt."))
        if not kunde_text:
            hinweise.append(_hinweis(Pruefcode.FELD_FEHLT, Schweregrad.WARNUNG, "Endkunde nicht erkannt."))
        if not belegnummer:
            hinweise.append(
                _hinweis(Pruefcode.FELD_FEHLT, Schweregrad.WARNUNG, "Bestellnummer (Belegnummer) nicht erkannt.")
            )
        if datum is None:
            hinweise.append(_hinweis(Pruefcode.FELD_FEHLT, Schweregrad.WARNUNG, "Belegdatum nicht erkannt."))
        if liefertag is None:
            text_hinweis = "Liefertag nicht erkannt."
            if liefertag_roh:
                text_hinweis = f"Liefertag nicht als Datum lesbar: '{liefertag_roh}'."
            hinweise.append(
                _hinweis(Pruefcode.FELD_FEHLT, Schweregrad.WARNUNG, text_hinweis, rohtext=liefertag_roh or "")
            )
        if not positionen:
            anhaenge = ", ".join(a.dateiname for a in inhalt.anhaenge) or "keine Anhänge"
            hinweise.append(
                _hinweis(
                    Pruefcode.FELD_FEHLT,
                    Schweregrad.FEHLER,
                    f"Keine Positionen in der E-Mail erkannt ({anhaenge}).",
                )
            )

        logger.info(
            "%s: Typ B, %d Positionen (%s), Lieferant %r (%s), Endkunde %r (%s), Belegnr %r, Liefertag %s, %d Anhänge",
            datei.name,
            len(positionen),
            positionsquelle,
            lieferant_text,
            lieferant_quelle,
            kunde_text,
            kunde_quelle,
            belegnummer,
            liefertag,
            len(inhalt.anhaenge),
        )
        return RohBestellung(
            eingangstyp=Eingangstyp.EIGENE_BESTELLUNG_EMAIL,
            quelldatei=datei,
            extraktionsmethode=Extraktionsmethode.EMAIL,
            kunde_text=kunde_text or None,
            kunde_adresse=kunde_adresse,
            lieferant_text=lieferant_text or None,
            belegnummer=belegnummer,
            bestellnummer=bestellnummer,
            datum=datum,
            liefertag=liefertag,
            lieferadresse=lieferadresse,
            positionen=positionen,
            summenvermerke=summenvermerke,
            hinweise=hinweise,
            rohtext=self._rohtext(inhalt, betreff, absender, empfaenger, text),
            seiten=None,
            kundennummer_dokument=kopf.get("kundennummer"),
            empfaenger_firma_text=empfaenger_firma_text,
            ladeadresse=_ladeadresse_aus_text(text),
            lieferbedingung=_lieferbedingung_aus_text(betreff, text),
        )

    def _eigene_firma_text(
        self, absender: Adresse | None, empfaenger: list[Adresse], zeilen: list[str]
    ) -> str | None:
        """Name unserer Firma, wie er in der Mail steht (Absender, Empfänger oder Signatur)."""
        kandidaten: list[str | None] = [absender.name if absender else None]
        kandidaten.extend(e.name for e in empfaenger)
        kandidaten.extend(z.strip() for z in zeilen if z.strip())
        for kandidat in kandidaten:
            if kandidat and self._ist_eigene_firma(kandidat) and len(kandidat) <= 80:
                return kandidat.strip(" ,;:")
        return None

    # -- Teilschritte --------------------------------------------------------

    def _hat_bestellinhalt(self, text: str) -> bool:
        if not text.strip():
            return False
        zeilen = text.split("\n")
        if lese_positionen(self._bis_signatur(zeilen)):
            return True
        kopf = lese_kopffelder([z.strip() for z in zeilen if z.strip()])
        return bool({"kunde", "lieferant", "bestellnummer", "belegnummer", "bestellung_an"} & set(kopf))

    @staticmethod
    def _bis_signatur(zeilen: list[str]) -> list[str]:
        for index, zeile in enumerate(zeilen):
            if index > 0 and _SIGNATUR_START.match(zeile.strip()):
                return zeilen[:index]
        return list(zeilen)

    def _basis_aus_text(self, text: str, datei: Path) -> RohBestellung | None:
        if not text.strip():
            return None
        try:
            return self._text_pdf.extrahiere_text(text, datei)
        except Exception as exc:
            logger.warning("%s: Textheuristik aus text_pdf fehlgeschlagen (%s)", datei.name, exc)
            return None

    def _kandidat_ok(self, name: str | None, *, nicht: str | None = None) -> str | None:
        name = _schneide_name(name)
        if not name or self._ist_eigene_firma(name):
            return None
        if nicht:
            a, b = _normalisiere(name), _normalisiere(nicht)
            if a and b and (a == b or a in b or b in a):
                return None
        return name

    def _finde_lieferant(
        self,
        kopf: dict[str, str],
        betreff: str | None,
        zeilen: list[str],
        empfaenger: list[Adresse],
        absender: Adresse | None,
    ) -> tuple[str | None, str]:
        name = self._kandidat_ok(kopf.get("lieferant"))
        if name:
            return name, "Feld Lieferant/Weingut"
        name = self._kandidat_ok(kopf.get("bestellung_an"))
        if name:
            return name, "Bestellung an"
        name = self._kandidat_ok(lieferant_aus_anrede("\n".join(zeilen)))
        if name:
            return name, "Anrede"
        absender_domain = absender.domain if absender else None
        for adresse in empfaenger:
            if adresse.domain and absender_domain and adresse.domain == absender_domain:
                continue  # Kopie an Kollegen
            name = self._kandidat_ok(adresse.name)
            if name and not _PERSONENANREDE.match(name):
                return name, "Empfänger"
        for adresse in empfaenger:
            domain = adresse.domain
            if not domain or domain in _GENERISCHE_DOMAINS or domain == absender_domain:
                continue
            label = domain.split(".")[0]
            if len(label) >= 3 and not self._ist_eigene_firma(label):
                return label, "Empfänger-Domain"
        return None, "nicht gefunden"

    def _finde_endkunde(
        self, kopf: dict[str, str], betreff: str | None, zeilen: list[str], lieferant: str | None
    ) -> tuple[str | None, str]:
        name = self._kandidat_ok(kopf.get("kunde"), nicht=lieferant)
        if name:
            return name, "Feld Kunde"
        beste: tuple[int, int, str, str] | None = None
        for quelle_nr, quelle in enumerate((betreff or "", "\n".join(zeilen))):
            for name_roh, art in endkunde_aus_text(quelle):
                name = self._kandidat_ok(name_roh, nicht=lieferant)
                if not name:
                    continue
                rang = (_KUNDE_RANG[art], quelle_nr)
                if beste is None or rang < beste[:2]:
                    beste = (*rang, name, art)
        if beste:
            return beste[2], f"Text ({beste[3]})"
        return None, "nicht gefunden"

    def _verarbeite_anhaenge(self, inhalt: EmailInhalt, hinweise: list[Pruefhinweis]) -> list[tuple[str, RohBestellung]]:
        ergebnisse: list[tuple[str, RohBestellung]] = []
        pdfs = [a for a in inhalt.anhaenge if a.ist_pdf]
        for anhang in inhalt.anhaenge:
            if anhang.ist_pdf:
                continue
            if anhang.ist_bild:
                hinweise.append(
                    _hinweis(
                        Pruefcode.FELD_FEHLT,
                        Schweregrad.WARNUNG,
                        f"Bildanhang '{anhang.dateiname}' wurde nicht ausgewertet – bei einem Scan die Datei "
                        "einzeln als Eingangstyp C (Vision) verarbeiten.",
                        anhang=anhang.dateiname,
                    )
                )
            else:
                hinweise.append(
                    _hinweis(
                        Pruefcode.FELD_FEHLT,
                        Schweregrad.INFO,
                        f"Anhang '{anhang.dateiname}' ({anhang.content_type}) wurde nicht ausgewertet.",
                        anhang=anhang.dateiname,
                    )
                )
        if not pdfs:
            return ergebnisse
        with tempfile.TemporaryDirectory(prefix="auftragsautomat_mail_") as tmp:
            for nr, anhang in enumerate(pdfs, start=1):
                pfad = Path(tmp) / _sicherer_dateiname(anhang.dateiname, nr)
                pfad.write_bytes(anhang.inhalt)
                if not ist_text_pdf(pfad):
                    hinweise.append(
                        _hinweis(
                            Pruefcode.FELD_FEHLT,
                            Schweregrad.WARNUNG,
                            f"PDF-Anhang '{anhang.dateiname}' hat keine Textebene (Scan) oder ist defekt – bitte "
                            "einzeln als Eingangstyp C (Vision) verarbeiten.",
                            anhang=anhang.dateiname,
                        )
                    )
                    continue
                try:
                    roh = self._text_pdf.extrahiere(pfad)
                except TextPdfFehler as exc:
                    logger.warning("%s: PDF-Anhang %s nicht lesbar: %s", inhalt.quelldatei.name, anhang.dateiname, exc)
                    hinweise.append(
                        _hinweis(
                            Pruefcode.FELD_FEHLT,
                            Schweregrad.WARNUNG,
                            f"PDF-Anhang '{anhang.dateiname}' konnte nicht gelesen werden: {exc}",
                            anhang=anhang.dateiname,
                        )
                    )
                    continue
                logger.info(
                    "%s: PDF-Anhang %s gelesen (%d Positionen, Belegnr %r)",
                    inhalt.quelldatei.name,
                    anhang.dateiname,
                    len(roh.positionen),
                    roh.belegnummer,
                )
                ergebnisse.append((anhang.dateiname, roh))
        return ergebnisse

    @staticmethod
    def _rohtext(
        inhalt: EmailInhalt, betreff: str | None, absender: Adresse | None, empfaenger: list[Adresse], text: str
    ) -> str:
        kopf = [
            f"Betreff: {betreff or ''}",
            f"Von: {absender.anzeige if absender else ''}",
            f"An: {', '.join(e.anzeige for e in empfaenger)}",
            f"Datum: {inhalt.datum.isoformat() if inhalt.datum else ''}",
        ]
        if inhalt.anhaenge:
            kopf.append("Anhänge: " + ", ".join(a.dateiname for a in inhalt.anhaenge))
        return "\n".join(kopf) + "\n\n" + text.strip()
