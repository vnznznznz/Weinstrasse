"""Eingehende Mails erkennen (docs/AUSBAUSTUFE_2.md, 2.4 ``eingang.py`` und 2.6 Schritt 2).

:func:`klassifiziere` macht aus einer gelesenen Mail (:class:`~auftragsautomat.m365.basis.Mail`) ein
:class:`~auftragsautomat.models.MailEreignis` oder ``None``:

1. **Speditionsmails** (Hillebrand "Auftragsbestätigung; Your Ref …; Our Ref: DEMB…", Terminänderung,
   Abholung, Zustellung) über :func:`auftragsautomat.tracking.hillebrand_mail.erkenne_ereignis`
   (``spedition_regeln`` = ``config/tracking_hillebrand.yaml``). Das Modul ``tracking`` wird parallel
   gebaut: Import lazy, bei ``ImportError`` greift eine eigene Minimal-Erkennung ("Our Ref: DEMB…",
   "laut Transporeon … am <Datum>").
2. **Lieferantenbestätigungen** (DE/EN/IT/ES/FR/PT: "Auftragsbestätigung", "order confirmation",
   "conferma d'ordine", "confirmación de pedido", "confirmation de commande", "confirmação …") mit
   Bezug auf unsere Order No. (``kundenreferenz``) und – wenn genannt – dem angekündigten
   Verladedatum (``neues_datum``).
3. **Terminänderungen** ("neuer Liefertermin", "verschoben auf", "new delivery date", "laut Transporeon …
   am <Datum>") -> ``neues_datum``.
4. **Kontaktänderungen**: nur ein Marker (neue Durchwahl/E-Mail, Nachfolge, "please update your records");
   den Vergleich mit der Kontaktliste macht ``kontakte.aenderungen``.

Zitierte und weitergeleitete Teile werden nicht ausgewertet (``extraktion.email_bestellung.teile_zitat``),
damit unsere eigenen Texte in Antworten nichts auslösen. :func:`klassifiziere_alle` liefert zusätzlich
Nebenereignisse (z. B. Kontaktänderung in einer Bestätigung); :func:`sammle_ereignisse` liest den
Posteingang über ein ``MailBackend``.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

from auftragsautomat.config import Settings
from auftragsautomat.extraktion.email_bestellung import teile_zitat
from auftragsautomat.extraktion.zahlen import finde_datum
from auftragsautomat.m365.basis import Mail, MailBackend, html_zu_text
from auftragsautomat.models import Ereignistyp, Kontaktgruppe, MailEreignis

logger = logging.getLogger(__name__)

__all__ = [
    "KONTAKTAENDERUNG_RE",
    "LIEFERANT_BESTAETIGUNG_RE",
    "TERMINAENDERUNG_RE",
    "VERLADEDATUM_RE",
    "klassifiziere",
    "klassifiziere_alle",
    "lade_spedition_regeln",
    "sammle_ereignisse",
]

_AUSZUG_LAENGE = 300

_DATUM = (
    r"(?P<datum>\d{1,2}\.\s?\d{1,2}\.(?:\s?\d{4}|\s?\d{2}(?!\d))?"
    r"|\d{4}-\d{2}-\d{2}"
    r"|\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-zéû]{3,9}\.?\s+\d{4}"
    r"|[A-Za-z]{3,9}\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4})"
)
_ARTIKEL = r"(?:den\s+|dem\s+|the\s+|il\s+|el\s+|le\s+|on\s+|am\s+|il\s+giorno\s+)?"

#: Bestätigung des Weinguts (DE/EN/IT/ES/FR/PT), Betreff oder Text.
# TODO-PRUEFEN: Wortlaut echter Bestätigungen der Weingüter (Betreffzeilen, Nummernformate "Order No.",
# Verladedatum-Formulierungen) gegen diese Muster prüfen und ergänzen; bisher nur synthetische Beispiele.
LIEFERANT_BESTAETIGUNG_RE = re.compile(
    r"auftrags?best(?:ä|ae)tigung|bestellbest(?:ä|ae)tigung|order\s+confirmation|confirmation\s+of\s+(?:your\s+)?order"
    r"|we\s+(?:hereby\s+)?confirm\s+(?:the\s+receipt\s+of\s+|receipt\s+of\s+)?your\s+order"
    r"|conferma\s+(?:d['’]|di\s+|dell['’])ordine|confermiamo\s+(?:la\s+ricezione\s+del(?:la)?\s+)?(?:vostro\s+|suo\s+)?ordine"
    r"|confirmaci[óo]n\s+de(?:l)?\s+pedido|confirmamos\s+(?:la\s+recepci[óo]n\s+de\s+)?(?:su|el)\s+pedido"
    r"|confirmation\s+de\s+(?:votre\s+)?commande|nous\s+confirmons\s+(?:la\s+r[ée]ception\s+de\s+)?votre\s+commande"
    r"|confirma[çc][ãa]o\s+(?:de|do|da)\s+(?:pedido|encomenda)"
    r"|hiermit\s+best(?:ä|ae)tigen\s+wir\s+(?:ihre\s+bestellung|den\s+eingang\s+ihrer\s+bestellung)",
    re.IGNORECASE,
)

#: Unsere Order No. in Betreff/Text ("Order No. WIK-2026-0100", "Ihre Bestellung 4711", "n. ordine 4711").
_ORDER_NO_RE = re.compile(
    r"(?:order\s*(?:no\.?|number|n[°º]|#|nr\.?)|your\s+order(?:\s+no\.?)?|purchase\s+order(?:\s+no\.?)?|p\.?o\.?\s*(?:no\.?|#)"
    r"|bestell-?\s*nr\.?|bestellnummer|ihre\s+bestellung(?:\s+nr\.?)?|unsere\s+bestellung|auftrags?-?\s*nr\.?"
    r"|(?:n\.?|nr\.?|numero)\s*(?:d['’])?ordine|vostro\s+ordine(?:\s+n\.?)?|ordine\s*(?:n\.?|nr\.?)"
    r"|(?:votre\s+)?commande\s*(?:n[°º]|no\.?)?|(?:su\s+)?pedido\s*(?:n[°º]|no\.?)?|encomenda\s*(?:n[°º]|no\.?)?"
    r"|your\s+ref(?:erence)?\.?|ihre\s+referenz)"
    r"\s*[:#]?\s*(?P<nr>[A-Z]{0,6}[-_/ ]?\d{2,}(?:[-_/.][A-Z0-9]+)*)",
    re.IGNORECASE,
)
#: Nummer der Bestätigung selbst (AB-Nr., confirmation no., conferma n.).
_BESTAETIGUNGS_NR_RE = re.compile(
    r"(?:ab-?\s*nr\.?|auftragsbest(?:ä|ae)tigung(?:s-?nr\.?|\s+nr\.?)|confirmation\s+(?:no\.?|number|#)"
    r"|conferma\s+(?:n\.?|nr\.?)|n[°º]\s*(?:de\s+)?confirmation|our\s+ref(?:erence)?\.?|unsere\s+(?:ab|referenz))"
    r"\s*[:#]?\s*(?P<nr>[A-Z]{0,6}[-_/ ]?\d{2,}(?:[-_/.][A-Z0-9]+)*)",
    re.IGNORECASE,
)

#: Terminänderung (Betreff/Text) mit Datum.
TERMINAENDERUNG_RE = re.compile(
    r"(?:neuer\s+(?:liefer|anliefer|abhol|verlade)?termin|neues\s+(?:liefer|verlade|anliefer)datum|liefer(?:termin|datum)\s+(?:wird\s+)?(?:verschoben|ge(?:ä|ae)ndert)"
    r"|verschoben\s+auf|verschiebt\s+sich\s+auf|verschieben\s+(?:wir\s+)?(?:die\s+lieferung\s+)?auf|jetzt\s+am"
    r"|new\s+(?:delivery|loading|pick-?up|shipping)\s+date|postponed\s+(?:to|until)|rescheduled\s+(?:to|for)|delayed\s+(?:to|until)|moved\s+to"
    r"|nuova\s+data\s+di\s+(?:consegna|carico)|rinviat[oa]\s+al|nueva\s+fecha\s+de\s+(?:entrega|carga)|aplazad[oa]\s+(?:al|hasta)"
    r"|nouvelle\s+date\s+de\s+(?:livraison|chargement)|report[ée]e?\s+au|laut\s+transporeon[^.\n]{0,80}?\bam)"
    r"\s*[:\-–]?\s*" + _ARTIKEL + _DATUM,
    re.IGNORECASE,
)
#: Terminänderung ohne verwertbares Datum (nur Marker, geringere Konfidenz).
_TERMINAENDERUNG_MARKER_RE = re.compile(
    r"termin(?:ä|ae)nderung|neuer\s+liefertermin|lieferverz(?:ö|oe)gerung|verschoben|verschiebt\s+sich|delivery\s+(?:is\s+)?delayed"
    r"|new\s+delivery\s+date|rescheduled|postponed|rinvi|aplazad|report[ée]e?\s+au",
    re.IGNORECASE,
)
#: Verladedatum / Abholbereitschaft in Lieferantenbestätigungen.
VERLADEDATUM_RE = re.compile(
    r"(?:loading\s+(?:date|day)(?:\s+is)?|loading\s+(?:is\s+)?(?:planned|scheduled|possible|confirmed)\s+(?:for|on)|(?:will\s+be\s+)?loaded\s+on"
    r"|(?:will\s+be\s+|is\s+|are\s+)?ready\s+(?:for\s+(?:pick-?up|collection|loading|shipment)\s+)?(?:on|from|by|as\s+of)|goods\s+(?:will\s+be\s+)?(?:ready|available)\s+(?:on|from|by)"
    r"|collection\s+(?:date|possible\s+from)|pick-?up\s+(?:date|possible\s+from)"
    r"|verlade(?:datum|termin|tag)(?:\s+ist)?|verladung\s+(?:am|ab|ist\s+(?:für|fuer)\s+den)|abhol(?:bereit|termin|datum)\s*(?:ab|am|ist\s+(?:für|fuer)\s+den)?|bereit\s+zur\s+abholung\s+(?:ab|am)|(?:ist|sind)\s+ab\s+dem|abholung\s+(?:möglich|moeglich)\s+ab"
    r"|pront[aoie]\s+(?:per\s+il\s+ritiro\s+)?(?:il|dal|a\s+partire\s+dal)|data\s+di\s+carico|carico\s+(?:previsto\s+)?(?:il|per\s+il)"
    r"|fecha\s+de\s+carga|lista?\s+para\s+(?:la\s+)?recogida\s+(?:el|desde)|date\s+de\s+chargement|chargement\s+(?:pr[ée]vu\s+)?le|pr[êe]te?\s+(?:à|a)\s+(?:partir\s+du|charger\s+le))"
    r"\s*[:\-–]?\s*" + _ARTIKEL + _DATUM,
    re.IGNORECASE,
)
#: Marker für geänderte Kontaktdaten (nur Hinweis; Vergleich macht kontakte.aenderungen).
KONTAKTAENDERUNG_RE = re.compile(
    r"neuen?\s+(?:telefon|ruf|mobil|handy)?nummer|neuen?\s+durchwahl|neuen?\s+e-?mail(?:-?adresse)?|neuen?\s+(?:kontakt|adress)daten|neuen?\s+anschrift"
    r"|kontaktdaten\s+(?:haben\s+sich\s+)?ge(?:ä|ae)ndert|ab\s+sofort\s+(?:erreichbar\s+|erreichen\s+sie\s+(?:mich|uns)\s+)?unter"
    r"|(?:bin|ist)\s+(?:ab\s+\S+\s+)?nicht\s+mehr\s+(?:im\s+(?:unternehmen|haus)|f(?:ü|ue)r\s+sie\s+zust(?:ä|ae)ndig|erreichbar)"
    r"|(?:mein|meine|unser|unsere)\s+nachfolger(?:in)?|(?:ü|ue)bernimmt\s+(?:ab|k(?:ü|ue)nftig|zuk(?:ü|ue)nftig)|neuer?\s+ansprechpartner(?:in)?"
    r"|new\s+(?:phone|telephone|mobile|contact)\s+(?:number|details)|new\s+e-?mail(?:\s+address)?|please\s+(?:update|note)\s+(?:your\s+records|my\s+new|our\s+new)"
    r"|i\s+(?:will\s+be|am)\s+leaving|no\s+longer\s+(?:with|responsible|in\s+charge)|will\s+take\s+over|is\s+taking\s+over|new\s+contact\s+person|changed?\s+(?:my|our)\s+(?:e-?mail|phone)"
    r"|nuovo\s+(?:numero|indirizzo|contatto|referente)|nuova\s+e-?mail|nouveau\s+num[ée]ro|nouvelle\s+adresse|nouveau\s+contact|nuev[oa]\s+(?:n[úu]mero|direcci[óo]n|contacto)",
    re.IGNORECASE,
)
_ADRESSE_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", re.IGNORECASE)
_MONATE_EN: dict[str, int] = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3, "apr": 4, "april": 4, "may": 5,
    "jun": 6, "june": 6, "jul": 7, "july": 7, "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}  # fmt: skip
_KURZDATUM_RE = re.compile(r"^\s*(?P<t>\d{1,2})\.\s?(?P<m>\d{1,2})\.\s?(?P<j>\d{4}|\d{2})?\s*$")
_EN_DATUM_RE = re.compile(
    r"^\s*(?:(?P<t1>\d{1,2})(?:st|nd|rd|th)?\s+(?P<m1>[A-Za-z]{3,9})\.?\s+(?P<j1>\d{4})|(?P<m2>[A-Za-z]{3,9})\.?\s+(?P<t2>\d{1,2})(?:st|nd|rd|th)?,?\s+(?P<j2>\d{4}))\s*$",
    re.IGNORECASE,
)

# Minimal-Erkennung der Speditionsmails (Rückfall ohne tracking-Modul)
_MIN_REFERENZ_RE = re.compile(r"Our\s*Ref\.?\s*:?\s*(?P<referenz>DEM[A-Z]?\d{3,})(?:\s*\[\s*job(?P<job>DEM\d+)\s*\])?", re.IGNORECASE)
_MIN_KUNDENREF_RE = re.compile(r"Your\s*Ref\.?\s*:?\s*(?P<kundenref>[^;\n]+)", re.IGNORECASE)
_MIN_BESTAETIGUNG_RE = re.compile(r"Auftragsbest(?:ä|ae|&auml;)tigung", re.IGNORECASE)
_MIN_ZEITFENSTER_RE = re.compile(r"laut\s+Transporeon[^0-9\n]{0,80}?am\s+(?P<datum>\d{1,2}\.\d{1,2}\.(?:\d{4}|\d{2})?)", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Hilfen
# ---------------------------------------------------------------------------


def _mailtext(mail: Mail) -> str:
    text = (mail.text or "").strip()
    if not text and mail.html:
        text = html_zu_text(mail.html)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    eigen, _zitat = teile_zitat(text)
    return eigen if eigen.strip() else text


def _absender(mail: Mail) -> str:
    treffer = _ADRESSE_RE.search(mail.kopf.absender or "")
    if treffer:
        return treffer.group(0).lower()
    return (mail.kopf.absender or "").strip().lower() or "unbekannt"


def _satz_um(text: str, treffer: re.Match[str]) -> str:
    anfang = max(text.rfind("\n", 0, treffer.start()), text.rfind(". ", 0, treffer.start()))
    anfang = anfang + 1 if anfang >= 0 else 0
    kandidaten = [i for i in (text.find("\n", treffer.end()), text.find(". ", treffer.end())) if i >= 0]
    ende = min(kandidaten) if kandidaten else len(text)
    return " ".join(text[anfang:ende].split())


def _auszug(text: str, laenge: int = _AUSZUG_LAENGE) -> str | None:
    t = " ".join((text or "").split())
    if not t:
        return None
    return t if len(t) <= laenge else t[: laenge - 1] + "…"


def _datum_mit_bezug(text: str | None, bezug: date | datetime | None) -> date | None:
    """"27.10.", "27.10.2026", "27.10.26", "2026-10-27", "27 October 2026", "October 27, 2026".
    Fehlt das Jahr, kommt es aus dem Empfangsdatum (Termin mehr als zwei Monate vor dem Empfang -> Folgejahr)."""
    if not text:
        return None
    text = text.strip()
    if isinstance(bezug, datetime):
        bezug = bezug.date()
    treffer = _EN_DATUM_RE.match(text)
    if treffer:
        g = treffer.groupdict()
        monat = _MONATE_EN.get((g["m1"] or g["m2"]).lower())
        if monat is None:
            return None
        try:
            return date(int(g["j1"] or g["j2"]), monat, int(g["t1"] or g["t2"]))
        except ValueError:
            return None
    treffer = _KURZDATUM_RE.match(text)
    if treffer:
        tag, monat = int(treffer["t"]), int(treffer["m"])
        jahr_text = treffer["j"]
        if jahr_text:
            jahr = int(jahr_text)
            if jahr < 100:
                jahr += 2000 if jahr < 80 else 1900
        else:
            jahr = (bezug or date.today()).year
        try:
            wert = date(jahr, monat, tag)
        except ValueError:
            return None
        if not jahr_text and bezug is not None and (bezug - wert).days > 61:
            try:
                wert = date(jahr + 1, monat, tag)
            except ValueError:
                return None
        return wert
    gefunden = finde_datum(text)
    return gefunden[0] if gefunden else None


def _datum_aus_treffer(treffer: re.Match[str] | None, bezug: datetime) -> date | None:
    if treffer is None or "datum" not in treffer.groupdict():
        return None
    return _datum_mit_bezug(treffer.group("datum"), bezug)


def _gruppe(treffer: re.Match[str] | None, name: str) -> str | None:
    if treffer is None:
        return None
    wert = treffer.groupdict().get(name)
    return " ".join(wert.split()) if wert else None


def _suche(regex: re.Pattern[str], *texte: str) -> re.Match[str] | None:
    for t in texte:
        if t:
            treffer = regex.search(t)
            if treffer:
                return treffer
    return None


def _kontaktgruppe(kontaktliste: Any, absender: str) -> Kontaktgruppe | None:
    """Gruppe des Absenders laut Kontaktliste (``kontakt_fuer_email``), sonst ``None``."""
    if kontaktliste is None:
        return None
    finder = getattr(kontaktliste, "kontakt_fuer_email", None)
    if finder is None:
        return None
    kontakt = finder(absender)
    return kontakt.gruppe if kontakt is not None else None


# ---------------------------------------------------------------------------
# Spedition (tracking.hillebrand_mail, sonst Minimal-Erkennung)
# ---------------------------------------------------------------------------


class _MinimalRegeln:
    """Ersatz für ``HillebrandKonfig``, wenn das Modul ``tracking`` fehlt: nur Absender-Domains."""

    def __init__(self, daten: dict[str, Any] | None) -> None:
        daten = daten or {}
        self.absender_domains = [str(d).lower().lstrip("@") for d in (daten.get("absender_domains") or [])]
        self.spedition = str(daten.get("spedition") or "Hillebrand")

    def ist_absender_bekannt(self, adresse: str | None) -> bool:
        if not adresse or "@" not in adresse:
            return False
        domain = adresse.rsplit("@", 1)[-1].lower()
        return any(domain == d or domain.endswith("." + d) for d in self.absender_domains)


def lade_spedition_regeln(regeln: Path | str | dict[str, Any] | Any | None) -> Any:
    """Lädt die Speditionsregeln (``config/tracking_hillebrand.yaml``): ``HillebrandKonfig`` über das
    tracking-Modul, bei ``ImportError`` :class:`_MinimalRegeln`. Ein bereits geladenes Objekt wird
    durchgereicht; ``None`` -> Standarddatei des tracking-Moduls bzw. leere Minimalregeln."""
    if regeln is not None and not isinstance(regeln, Path | str | dict):
        return regeln
    try:
        from auftragsautomat.tracking.basis import HillebrandKonfig, lade_hillebrand_konfig
    except ImportError:
        logger.warning("Modul tracking nicht verfügbar – Minimal-Erkennung der Speditionsmails.")
        if isinstance(regeln, dict):
            return _MinimalRegeln(regeln)
        if isinstance(regeln, Path | str) and Path(regeln).is_file():
            import yaml

            try:
                return _MinimalRegeln(yaml.safe_load(Path(regeln).read_text(encoding="utf-8")) or {})
            except (OSError, yaml.YAMLError) as exc:
                logger.warning("Speditionsregeln %s nicht lesbar: %s", regeln, exc)
        return _MinimalRegeln(None)
    if isinstance(regeln, dict):
        try:
            return HillebrandKonfig.model_validate(regeln)
        except ValueError:
            pass  # unvollständiges Dict: Werte über die Standarddatei legen
        try:
            standard = lade_hillebrand_konfig(None)
            return HillebrandKonfig.model_validate({**standard.model_dump(), **regeln})
        except Exception as exc:  # weder vollständig noch mit Standard kombinierbar
            logger.warning("Speditionsregeln (Dict) nicht als HillebrandKonfig ladbar (%s) – Minimal-Erkennung.", exc)
            return _MinimalRegeln(regeln)
    try:
        return lade_hillebrand_konfig(Path(regeln) if regeln else None)
    except Exception as exc:  # fehlende/kaputte Datei: Minimalregeln statt Abbruch des Mail-Laufs
        logger.warning("Speditionsregeln %s nicht ladbar (%s) – Minimal-Erkennung.", regeln, exc)
        return _MinimalRegeln(None)


def _spedition_minimal(mail: Mail, betreff: str, text: str, regeln: Any) -> MailEreignis | None:
    """Rückfall ohne tracking-Modul: "Our Ref: DEMB…" in Betreff/Text, Bestätigung über den Betreff,
    Lieferzeitfenster "laut Transporeon … am <Datum>"."""
    absender = _absender(mail)
    bekannt = bool(regeln is not None and getattr(regeln, "ist_absender_bekannt", lambda _a: False)(absender))
    ref = _suche(_MIN_REFERENZ_RE, betreff, text)
    referenz = _gruppe(ref, "referenz")
    if not bekannt and referenz is None:
        return None
    kundenreferenz = _gruppe(_suche(_MIN_KUNDENREF_RE, betreff, text), "kundenref")
    if kundenreferenz and ";" in kundenreferenz:
        kundenreferenz = kundenreferenz.split(";", 1)[0].strip()
    fenster = _MIN_ZEITFENSTER_RE.search(text)
    aenderung = TERMINAENDERUNG_RE.search(text) or TERMINAENDERUNG_RE.search(betreff)
    if _MIN_BESTAETIGUNG_RE.search(betreff):
        typ = Ereignistyp.SPEDITION_BESTAETIGUNG
        neues_datum = _datum_aus_treffer(fenster, mail.kopf.empfangen)
        auszug = _satz_um(text, fenster) if fenster else _auszug(text)
        konfidenz = 1.0 if bekannt else 0.8
    elif aenderung is not None or fenster is not None:
        typ = Ereignistyp.TERMINAENDERUNG
        treffer = aenderung or fenster
        neues_datum = _datum_aus_treffer(treffer, mail.kopf.empfangen)
        auszug = _satz_um(treffer.string, treffer) if treffer else _auszug(text)
        konfidenz = 0.9 if bekannt else 0.7
    elif referenz is not None:
        typ = Ereignistyp.SONSTIGES
        neues_datum = None
        auszug = _auszug(text)
        konfidenz = 0.5
    else:
        return None
    return MailEreignis(
        typ=typ,
        absender=absender,
        betreff=betreff.strip(),
        empfangen=mail.kopf.empfangen,
        referenz=referenz.upper() if referenz else None,
        kundenreferenz=kundenreferenz,
        neues_datum=neues_datum,
        text_auszug=auszug,
        mail_id=mail.kopf.id,
        konfidenz=konfidenz,
    )


def _spedition(mail: Mail, betreff: str, text: str, regeln: Any) -> MailEreignis | None:
    if isinstance(regeln, _MinimalRegeln):
        return _spedition_minimal(mail, betreff, text, regeln)
    try:
        from auftragsautomat.tracking.hillebrand_mail import erkenne_ereignis
    except ImportError:
        return _spedition_minimal(mail, betreff, text, regeln)
    try:
        return erkenne_ereignis(mail, regeln)
    except Exception as exc:  # die Speditionserkennung darf die übrige Klassifikation nicht stoppen
        logger.warning("Speditionserkennung fehlgeschlagen (%s: %s) – Rückfall auf Minimal-Erkennung.", type(exc).__name__, exc)
        return _spedition_minimal(mail, betreff, text, regeln)


# ---------------------------------------------------------------------------
# Lieferant, Termin, Kontakt
# ---------------------------------------------------------------------------


def _ereignis(mail: Mail, typ: Ereignistyp, **felder: Any) -> MailEreignis:
    return MailEreignis(
        typ=typ,
        absender=_absender(mail),
        betreff=(mail.kopf.betreff or "").strip(),
        empfangen=mail.kopf.empfangen,
        mail_id=mail.kopf.id,
        **felder,
    )


def _lieferant_bestaetigung(mail: Mail, betreff: str, text: str, gruppe: Kontaktgruppe | None) -> MailEreignis | None:
    if gruppe in (Kontaktgruppe.KUNDE, Kontaktgruppe.SPEDITION, Kontaktgruppe.INTERN):
        return None
    im_betreff = LIEFERANT_BESTAETIGUNG_RE.search(betreff)
    im_text = LIEFERANT_BESTAETIGUNG_RE.search(text)
    treffer = im_betreff or im_text
    if treffer is None:
        return None
    konfidenz = 0.9 if im_betreff else 0.75
    if gruppe == Kontaktgruppe.LIEFERANT:
        konfidenz = min(1.0, konfidenz + 0.1)
    kundenreferenz = _gruppe(_suche(_ORDER_NO_RE, betreff, text), "nr")
    referenz = _gruppe(_suche(_BESTAETIGUNGS_NR_RE, betreff, text), "nr")
    if referenz is not None and referenz == kundenreferenz:
        referenz = None
    verladung = VERLADEDATUM_RE.search(text) or VERLADEDATUM_RE.search(betreff)
    neues_datum = _datum_aus_treffer(verladung, mail.kopf.empfangen)
    if neues_datum is None:
        aenderung = TERMINAENDERUNG_RE.search(text)
        neues_datum = _datum_aus_treffer(aenderung, mail.kopf.empfangen)
        verladung = aenderung if neues_datum is not None else None
    if verladung is not None:
        auszug = _satz_um(verladung.string, verladung)
    else:
        auszug = _satz_um(text, im_text) if im_text else _auszug(text)
    return _ereignis(
        mail,
        Ereignistyp.LIEFERANT_BESTAETIGUNG,
        referenz=referenz,
        kundenreferenz=kundenreferenz,
        neues_datum=neues_datum,
        text_auszug=auszug or _auszug(text),
        konfidenz=konfidenz,
    )


def _terminaenderung(mail: Mail, betreff: str, text: str, gruppe: Kontaktgruppe | None) -> MailEreignis | None:
    treffer = TERMINAENDERUNG_RE.search(text) or TERMINAENDERUNG_RE.search(betreff)
    neues_datum = _datum_aus_treffer(treffer, mail.kopf.empfangen)
    if treffer is None or neues_datum is None:
        marker = _TERMINAENDERUNG_MARKER_RE.search(betreff) or _TERMINAENDERUNG_MARKER_RE.search(text)
        if marker is None:
            return None
        if neues_datum is None:
            kandidat = finde_datum(_satz_um(marker.string, marker))
            neues_datum = kandidat[0] if kandidat else None
        treffer = treffer or marker
        konfidenz = 0.7 if neues_datum is not None else 0.5
    else:
        konfidenz = 0.9
    if gruppe in (Kontaktgruppe.LIEFERANT, Kontaktgruppe.SPEDITION):
        konfidenz = min(1.0, konfidenz + 0.1)
    kundenreferenz = _gruppe(_suche(_ORDER_NO_RE, betreff, text), "nr")
    return _ereignis(
        mail,
        Ereignistyp.TERMINAENDERUNG,
        kundenreferenz=kundenreferenz,
        neues_datum=neues_datum,
        text_auszug=_satz_um(treffer.string, treffer) or _auszug(text),
        konfidenz=konfidenz,
    )


def _kontaktaenderung(mail: Mail, betreff: str, text: str, gruppe: Kontaktgruppe | None) -> MailEreignis | None:
    treffer = KONTAKTAENDERUNG_RE.search(text) or KONTAKTAENDERUNG_RE.search(betreff)
    if treffer is None:
        return None
    konfidenz = 0.7 if gruppe is not None else 0.6
    return _ereignis(
        mail,
        Ereignistyp.KONTAKTAENDERUNG,
        text_auszug=_satz_um(treffer.string, treffer) or _auszug(text),
        konfidenz=konfidenz,
    )


# ---------------------------------------------------------------------------
# Öffentliche Funktionen
# ---------------------------------------------------------------------------


def klassifiziere_alle(
    mail: Mail,
    *,
    spedition_regeln: Path | str | dict[str, Any] | Any | None = None,
    kontaktliste: Any | None = None,
) -> list[MailEreignis]:
    """Alle Ereignisse einer Mail: zuerst das Hauptereignis (Spedition > Lieferantenbestätigung >
    Terminänderung > Kontaktänderung), dann Nebenereignisse (Terminänderung mit Datum in einer
    Bestätigung, Kontaktänderungs-Marker). Leer, wenn die Mail nichts Erkennbares enthält.
    ``kontaktliste`` (``Kontaktliste`` mit ``kontakt_fuer_email``) hebt die Konfidenz bekannter Absender und
    schließt Kunden-/Speditionsabsender als Lieferantenbestätigung aus."""
    betreff = (mail.kopf.betreff or "").strip()
    text = _mailtext(mail)
    gruppe = _kontaktgruppe(kontaktliste, _absender(mail))
    regeln = lade_spedition_regeln(spedition_regeln)
    ereignisse: list[MailEreignis] = []

    haupt = _spedition(mail, betreff, text, regeln)
    if haupt is None:
        haupt = _lieferant_bestaetigung(mail, betreff, text, gruppe)
    termin = _terminaenderung(mail, betreff, text, gruppe)
    kontakt = _kontaktaenderung(mail, betreff, text, gruppe)
    if haupt is None:
        haupt = termin
        termin = None
    if haupt is None:
        haupt = kontakt
        kontakt = None
    if haupt is None:
        return []
    ereignisse.append(haupt)
    if (
        termin is not None
        and termin.neues_datum is not None
        and haupt.typ != Ereignistyp.TERMINAENDERUNG
        and termin.neues_datum != haupt.neues_datum  # dasselbe Datum steht schon im Hauptereignis
    ):
        ereignisse.append(termin)
    if kontakt is not None:
        ereignisse.append(kontakt)
    for e in ereignisse:
        logger.debug("Mail-Ereignis %s (%.2f): %s", e.typ.value, e.konfidenz, e.betreff)
    return ereignisse


def klassifiziere(
    mail: Mail,
    *,
    spedition_regeln: Path | str | dict[str, Any] | Any | None = None,
    kontaktliste: Any | None = None,
) -> MailEreignis | None:
    """Hauptereignis einer Mail oder ``None`` (siehe :func:`klassifiziere_alle`)."""
    ereignisse = klassifiziere_alle(mail, spedition_regeln=spedition_regeln, kontaktliste=kontaktliste)
    return ereignisse[0] if ereignisse else None


def sammle_ereignisse(
    mail_backend: MailBackend,
    settings: Settings,
    *,
    seit: datetime | date,
    kontaktliste: Any | None = None,
    maximal: int = 200,
    ordner: str | None = None,
) -> list[MailEreignis]:
    """Liest den Posteingang ab ``seit`` (``MailBackend.suche_mails``/``lese_mail``) und klassifiziert jede
    Mail; Speditionsregeln aus ``settings.tracking.hillebrand_konfig``. Eine kaputte Mail stoppt den Lauf
    nicht. Ergebnis chronologisch (älteste zuerst)."""
    if isinstance(seit, date) and not isinstance(seit, datetime):
        seit = datetime.combine(seit, time.min).astimezone()
    regeln = lade_spedition_regeln(settings.tracking.hillebrand_konfig)
    ereignisse: list[MailEreignis] = []
    try:
        koepfe = mail_backend.suche_mails(seit=seit, maximal=maximal, ordner=ordner)
    except Exception as exc:  # Posteingang nicht erreichbar: leer zurück, Fehler im Log
        logger.error("Posteingang konnte nicht gelesen werden: %s: %s", type(exc).__name__, exc)
        return []
    for kopf in koepfe:
        try:
            mail = mail_backend.lese_mail(kopf.id)
            ereignisse.extend(klassifiziere_alle(mail, spedition_regeln=regeln, kontaktliste=kontaktliste))
        except Exception as exc:  # eine Mail darf den Lauf nicht stoppen
            logger.warning("Mail %s übersprungen (%s: %s)", kopf.id, type(exc).__name__, exc)
    ereignisse.sort(key=lambda e: e.empfangen)
    logger.info("%d Mail(s) gelesen, %d Ereignis(se) erkannt.", len(koepfe), len(ereignisse))
    return ereignisse


def ereignisse_aus_mails(mails: Iterable[Mail], **argumente: Any) -> list[MailEreignis]:
    """Klassifiziert bereits gelesene Mails (z. B. aus ``.eml``-Fixtures über ``m365.fake.mail_aus_eml``)."""
    regeln = lade_spedition_regeln(argumente.pop("spedition_regeln", None))
    ergebnis: list[MailEreignis] = []
    for mail in mails:
        ergebnis.extend(klassifiziere_alle(mail, spedition_regeln=regeln, **argumente))
    return ergebnis
