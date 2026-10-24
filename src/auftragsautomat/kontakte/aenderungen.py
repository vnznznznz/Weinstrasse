"""Änderungsvorschläge für die Kontaktliste aus E-Mail-Signaturen (docs/AUSBAUSTUFE_2.md, 2.3).

* :func:`erkenne_signatur` findet den Signaturblock unter der Grußformel ("Mit freundlichen
  Grüßen", "Best regards", "Cordiali saluti", "Cordialement", "Saludos" …) und liest daraus Name,
  Position, Firma, E-Mail-Adressen und Telefonnummern (``+49 …``, ``0049 …``, Durchwahlen; Fax
  getrennt).
* :func:`vergleiche` stellt die Signatur einem :class:`~auftragsautomat.models.Kontakt` gegenüber
  und liefert :class:`~auftragsautomat.models.KontaktAenderung` (neue E-Mail, neue Telefonnummer,
  geänderte Position, fehlender Name).
* :func:`aenderungen_aus_mail` macht das für eine gelesene Mail (Absender -> Kontakt; unbekannter
  Absender einer bekannten Firma -> Vorschlag "neu").
* :func:`uebernehme` schreibt eine Änderung in die Excel – **nur**, wenn
  ``settings.kontakte.automatisch_uebernehmen`` gesetzt ist oder der Anwender die Änderung in der
  UI freigegeben hat (``freigegeben=True``); sonst bleibt es beim Vorschlag (:func:`bericht`).
  Die Zelle ergibt sich aus der Kopfzeile des Blocks (Spalte) und ``Kontakt.quelle`` (Zeile);
  vor dem Schreiben wird geprüft, dass die Zelle noch den alten Wert enthält.
* Lexware-Stammdaten werden nie geändert (nur Hinweis im Bericht, Design Abschnitt 2.3).
"""

from __future__ import annotations

import logging
import re
from typing import TypedDict

from auftragsautomat.benennung import normalisiere
from auftragsautomat.config import DateiRefKonfig, Settings
from auftragsautomat.kontakte.liste import (
    KontakteFehler,
    Kontaktliste,
    erkenne_kopf,
    quelle_text,
    zelltext,
    zerlege_quelle,
)
from auftragsautomat.kontakte.zuordnung import firmenscore, firmenvarianten
from auftragsautomat.m365.basis import (
    ExcelBackend,
    M365Fehler,
    Mail,
    excel_sitzung,
    html_zu_text,
    parse_adresse,
    spaltenbuchstabe,
)
from auftragsautomat.matching.aliase import aehnlichkeit, normalisiere_name
from auftragsautomat.models import Kontakt, KontaktAenderung

__all__ = [
    "FELD_NEU",
    "FELDER_UEBERNEHMBAR",
    "Signatur",
    "aenderungen_aus_mail",
    "bericht",
    "erkenne_signatur",
    "normalisiere_telefon",
    "telefon_gleich",
    "uebernehme",
    "vergleiche",
]

logger = logging.getLogger(__name__)

#: Feld einer ``KontaktAenderung`` für einen bisher unbekannten Ansprechpartner (nie automatisch geschrieben).
FELD_NEU = "neu"
#: Felder, die ``uebernehme`` in die Excel schreiben kann.
FELDER_UEBERNEHMBAR: tuple[str, ...] = ("email", "telefon", "position", "name")
MAX_SIGNATURZEILEN = 25
#: Ab diesem Score (0..100) gilt die Firma einer Signatur als Firma der Kontaktliste.
SCHWELLE_FIRMA = 90.0


class Signatur(TypedDict):
    """Ergebnis von :func:`erkenne_signatur`."""

    name: str | None
    position: str | None
    firma: str | None
    emails: list[str]
    telefone: list[str]
    fax: list[str]
    grussformel: str | None
    zeilen: list[str]


_GRUSS_WORTE = (
    r"(?:mit\s+(?:den\s+)?(?:besten|freundlichen|herzlichen|lieben|sonnigen)\s+gr(?:ü|ue|u)(?:ß|ss)en"
    r"|(?:freundliche|viele|beste|herzliche|liebe|sch[öo]ne|sonnige)\s+gr(?:ü|ue|u)(?:ß|ss)e"
    r"|gr(?:ü|ue|u)(?:ß|ss)e?|mfg|lg|vg"
    r"|(?:with\s+)?(?:best|kind|warm|warmest)\s+(?:regards|wishes)|regards|best|sincerely"
    r"|yours\s+(?:sincerely|faithfully|truly)|many\s+thanks(?:\s+and\s+(?:best\s+|kind\s+)?regards)?"
    r"|thanks\s+and\s+(?:best\s+|kind\s+)?regards|cheers"
    r"|cordiali\s+saluti|distinti\s+saluti|un\s+caro\s+saluto|cari\s+saluti|saluti"
    r"|(?:bien\s+|très\s+|tres\s+)?cordialement|(?:bien\s+)?[àa]\s+vous|(?:meilleures\s+)?salutations"
    r"|(?:un\s+)?saludos(?:\s+cordiales)?|atentamente|un\s+cordial\s+saludo)"
)
_GRUSS_RE = re.compile(
    rf"^\s*{_GRUSS_WORTE}(?:\s+(?:aus|from|da|de)\s+[^\d@,;:]{{2,30}})?(?:\s*[/|,&-]\s*{_GRUSS_WORTE})?\s*[,.!;:]*\s*$",
    re.IGNORECASE,
)
_ZITAT_RE = re.compile(
    r"^\s*(?:>|(?:von|from|an|to|gesendet|sent|betreff|subject|datum|date|cc):\s|-{2,}\s*(?:original|urspr|weitergeleitete|forwarded)"
    r"|am\s.+\sschrieb\b|on\s.+\swrote:?\s*$|le\s.+\sa\s[ée]crit|il\s.+\sha\s+scritto|el\s.+\sescribi[óo])",
    re.IGNORECASE,
)
_FUSSTEXT_RE = re.compile(
    r"^\s*(?:this\s+(?:e-?mail|message)|diese\s+(?:e-?mail|nachricht)|der\s+inhalt\s+dieser|the\s+information|"
    r"confidentiality|disclaimer|haftungsausschluss|vertraulichkeit|please\s+consider\s+the\s+environment|"
    r"bitte\s+denken\s+sie\s+an\s+die\s+umwelt|geschäftsführ|gesch[äa]ftsf[üu]hrung:|handelsregister|amtsgericht|"
    r"ust-?id|vat\s*(?:no|id)|p\.?\s*iva|siret|registergericht)",
    re.IGNORECASE,
)
_TRENNER_RE = re.compile(r"^\s*[-_*=~]{2,}\s*$")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_URL_RE = re.compile(r"(?:https?://|www\.)\S+|\b[\w-]+\.(?:de|com|org|net|eu|it|fr|es|at|ch)\b(?:/\S*)?", re.IGNORECASE)
_VERTRETUNG_RE = re.compile(r"^(?:i\.\s?a\.|i\.\s?v\.|ppa\.?|p\.\s?p\.|per\s+procura)\s*", re.IGNORECASE)
_TITEL_RE = re.compile(r"^(?:dr|prof|dipl|ing|mag|mba|msc|bsc|dott|avv|dipl\.-ing)\.?$", re.IGNORECASE)
_FIRMA_RE = re.compile(
    r"\b(?:gmbh|ag|kg|ohg|e\.?\s?k\.?|ug|se|srl|s\.r\.l\.?|spa|s\.p\.a\.?|sas|s\.a\.s\.?|snc|s\.n\.c\.?|sarl|s\.a\.r\.l\.?|"
    r"sa|s\.a\.?|sl|s\.l\.?|ltd|limited|llc|inc|bv|b\.v\.?|plc|co\b|group|gruppe|holding|"
    r"weingut|winery|domaine|ch[âa]teau|cantina|cantine|azienda|tenuta|bodega|bodegas|vini|vino|wines?|vins?|"
    r"spedition|logistik|logistics|agentur|import|handel|weinhandel|vinothek|weinkontor|kontor)\b",
    re.IGNORECASE,
)
_POSITION_RE = re.compile(
    r"manager|assistant|assistent|leiter|leitung|disposition|dispo|orders?|export|import|sales|vertrieb|verkauf|"
    r"einkauf|buchhaltung|accounting|logistik|logistics|customer|service|kundenservice|gesch[äa]ftsf[üu]hr|ceo|cfo|"
    r"coo|director|direktor|head\b|inhaber|owner|mitarbeiter|sachbearbeit|team|back\s?office|administration|"
    r"verwaltung|support|marketing|operations|agent|responsabile|commerciale|ufficio|responsable|commercial|"
    r"administrat|r[ée]f[ée]rent|jefe|gerente|ventas|pedidos|key\s?account|supply|order\s?desk|dispatch|planung",
    re.IGNORECASE,
)
_ABTEILUNG_RE = re.compile(r"\b(?:team|abteilung|department|office|postfach|zentrale|service|support|kundenservice)\b", re.IGNORECASE)
_TEL_LABEL_RE = re.compile(
    r"(?:\b(?:tel(?:efon|efono|[ée]phone|ephone)?|phone|fon|mobil(?:e)?|handy|cell(?:ulare)?|direct(?:\s+line)?|durchwahl|dw|t|m|d|p|ph)\b\s*\.?\s*:?\s*)$",
    re.IGNORECASE,
)
_FAX_LABEL_RE = re.compile(r"(?:\b(?:fax|telefax|f)\b\s*\.?\s*:?\s*)$", re.IGNORECASE)
_NUMMER_RE = re.compile(r"(?<![\w])(?:\+|00)?\(?\d[\d\s/().‐-]{5,}\d")
_DATUM_RE = re.compile(r"^\d{1,2}\s*[./]\s*\d{1,2}\s*[./]\s*\d{2,4}$")
_KENNNUMMER_ZEILE_RE = re.compile(
    r"ust|vat|iban|bic|steuer|hrb|hra|handelsregister|amtsgericht|registro|p\.?\s*iva|partita|siret|siren|tva|"
    r"kontonummer|bank|rea\b|codice|c\.f\.|cif|nif",
    re.IGNORECASE,
)
_WHITESPACE_RE = re.compile(r"[ \t\xa0]+")


# ---------------------------------------------------------------------------
# Telefonnummern
# ---------------------------------------------------------------------------


def normalisiere_telefon(text: str | None) -> str:
    """Vergleichsform: nur Ziffern, ``+``/``00`` am Anfang als ``+``, ``(0)`` entfernt.
    ``"+49 (0) 6131 12 34-5"`` -> ``"+49613112345"``, ``"06131 12345"`` -> ``"0613112345"``."""
    if not text:
        return ""
    t = re.sub(r"\(\s*0\s*\)", "", str(text))
    t = t.strip()
    international = t.startswith("+") or t.startswith("00")
    ziffern = re.sub(r"\D", "", t)
    if international:
        ziffern = ziffern[2:] if ziffern.startswith("00") else ziffern
        return "+" + ziffern if ziffern else ""
    return ziffern


def telefon_gleich(a: str | None, b: str | None) -> bool:
    """``True``, wenn zwei Nummern dieselbe sind (auch "+49 6131 1234567" vs. "06131 1234567")."""
    na, nb = normalisiere_telefon(a), normalisiere_telefon(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    da, db = na.lstrip("+"), nb.lstrip("+")
    kurz_a = da.lstrip("0") if not na.startswith("+") else da
    kurz_b = db.lstrip("0") if not nb.startswith("+") else db
    if min(len(kurz_a), len(kurz_b)) < 7:
        return False
    return kurz_a.endswith(kurz_b) or kurz_b.endswith(kurz_a)


def _nummern_in_zeile(zeile: str) -> tuple[list[str], list[str]]:
    """(Telefonnummern, Faxnummern) einer Zeile, als bereinigte Anzeigeform."""
    if _KENNNUMMER_ZEILE_RE.search(zeile) or _DATUM_RE.match(zeile.strip()):
        return [], []
    telefone: list[str] = []
    faxe: list[str] = []
    for treffer in _NUMMER_RE.finditer(zeile):
        roh = treffer.group(0)
        ziffern = re.sub(r"\D", "", roh)
        if not 7 <= len(ziffern) <= 15 or _DATUM_RE.match(roh.strip()):
            continue
        davor = zeile[: treffer.start()]
        label_tel = bool(_TEL_LABEL_RE.search(davor))
        label_fax = bool(_FAX_LABEL_RE.search(davor))
        international = roh.lstrip("(").startswith(("+", "00"))
        if not (label_tel or label_fax or international or len(ziffern) >= 9):
            continue
        anzeige = _WHITESPACE_RE.sub(" ", roh).strip(" -/.")
        if label_fax:
            faxe.append(anzeige)
        else:
            telefone.append(anzeige)
    return telefone, faxe


# ---------------------------------------------------------------------------
# Signatur erkennen
# ---------------------------------------------------------------------------


def _zeilen(text: str) -> list[str]:
    if "<" in text and re.search(r"<\s*(?:html|body|div|p|br|table|span)\b", text, re.IGNORECASE):
        text = html_zu_text(text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return [_WHITESPACE_RE.sub(" ", z).strip() for z in text.split("\n")]


def _ist_name(zeile: str) -> str | None:
    z = _VERTRETUNG_RE.sub("", zeile).strip(" ,;")
    if not z or "@" in z or _URL_RE.search(z) or re.search(r"\d", z) or _FIRMA_RE.search(z) or _ABTEILUNG_RE.search(z):
        return None
    if _POSITION_RE.search(z) and not re.search(r"\b(?:head|team)\b", z, re.IGNORECASE):
        return None
    tokens = [t for t in z.replace(",", " ").split() if not _TITEL_RE.match(t)]
    if not 2 <= len(tokens) <= 5 or any(len(t) > 25 for t in tokens):
        return None
    if not all(re.fullmatch(r"[^\W\d_][\w'’.-]*", t) for t in tokens):
        return None
    if not tokens[0][0].isupper():
        return None
    return " ".join(tokens)


def _ist_position(zeile: str) -> bool:
    if not zeile or "@" in zeile or _URL_RE.search(zeile) or re.search(r"\d", zeile) or len(zeile) > 70:
        return False
    if _FIRMA_RE.search(zeile) and not _POSITION_RE.search(zeile):
        return False
    if _POSITION_RE.search(zeile):
        return True
    return len(zeile.split()) <= 3 and not _GRUSS_RE.match(zeile)


def _ist_firma(zeile: str) -> bool:
    if not zeile or "@" in zeile or _URL_RE.search(zeile) or len(zeile) > 80:
        return False
    if len(re.findall(r"\d", zeile)) >= 3:
        return False
    return bool(_FIRMA_RE.search(zeile)) and not _POSITION_RE.search(zeile)


def erkenne_signatur(text: str | None) -> Signatur:
    """Liest Name, Position, Firma, E-Mail-Adressen und Telefonnummern aus dem Signaturblock.

    Anker ist die erste Grußformel (Top-Posting: die eigene Signatur steht vor zitierten Mails);
    der Block endet an einer zitierten Nachricht ("Von:", "From:", "Am … schrieb", "> …"), an
    einem rechtlichen Fußtext oder nach :data:`MAX_SIGNATURZEILEN` Zeilen. Ohne Grußformel
    werden nur E-Mail-Adressen und Telefonnummern aus dem ganzen Text gesammelt. HTML wird
    vorher in Text gewandelt.
    """
    ergebnis: Signatur = {
        "name": None,
        "position": None,
        "firma": None,
        "emails": [],
        "telefone": [],
        "fax": [],
        "grussformel": None,
        "zeilen": [],
    }
    if not text or not text.strip():
        return ergebnis
    zeilen = _zeilen(text)
    anker = next((i for i, z in enumerate(zeilen) if z and _GRUSS_RE.match(z)), None)

    block: list[str] = []
    if anker is not None:
        ergebnis["grussformel"] = zeilen[anker]
        for z in zeilen[anker + 1 :]:
            if _ZITAT_RE.match(z) or _FUSSTEXT_RE.match(z):
                break
            if not z or _TRENNER_RE.match(z):
                continue
            block.append(z)
            if len(block) >= MAX_SIGNATURZEILEN:
                break
    ergebnis["zeilen"] = block
    quelle = block if block else [z for z in zeilen if z and not _ZITAT_RE.match(z)]

    for z in quelle:
        for email in _EMAIL_RE.findall(z):
            e = email.lower().rstrip(".")
            if e not in ergebnis["emails"]:
                ergebnis["emails"].append(e)
        telefone, faxe = _nummern_in_zeile(z)
        for t in telefone:
            if not any(telefon_gleich(t, v) for v in ergebnis["telefone"]):
                ergebnis["telefone"].append(t)
        for f in faxe:
            if not any(telefon_gleich(f, v) for v in ergebnis["fax"]):
                ergebnis["fax"].append(f)

    if not block:
        return ergebnis
    name_index = None
    for i, z in enumerate(block[:6]):
        name = _ist_name(z)
        if name:
            ergebnis["name"] = name
            name_index = i
            break
    if name_index is not None:
        rest = block[name_index + 1 :]
        if rest and _ist_position(rest[0]):
            ergebnis["position"] = rest[0].strip(" ,;")
        elif len(rest) > 1 and _ist_firma(rest[0]) and _ist_position(rest[1]):
            ergebnis["position"] = rest[1].strip(" ,;")
    for z in block:
        if _ist_firma(z):
            ergebnis["firma"] = z.strip(" ,;")
            break
    return ergebnis


# ---------------------------------------------------------------------------
# Vergleich und Vorschläge
# ---------------------------------------------------------------------------


def _namen_passen(kontakt_name: str | None, signatur_name: str | None) -> bool:
    """``True``, wenn die Signatur zur Person des Kontakts passt (oder ein Name fehlt)."""
    if not kontakt_name or not signatur_name:
        return True
    a, b = normalisiere_name(kontakt_name, rechtsformen_entfernen=False), normalisiere_name(signatur_name, rechtsformen_entfernen=False)
    if a == b:
        return True
    ta, tb = set(a.split()), set(b.split())
    anreden = {"frau", "herr", "herrn", "mr", "mrs", "ms", "dr", "prof"}
    ta -= anreden
    tb -= anreden
    if ta and tb and (ta <= tb or tb <= ta):
        return True
    return aehnlichkeit(a, b) >= 80


def vergleiche(kontakt: Kontakt, signatur: Signatur, *, quelle: str = "E-Mail-Signatur") -> list[KontaktAenderung]:
    """Unterschiede zwischen Kontakt und Signatur als Vorschläge (``uebernommen=False``).

    Geprüft werden E-Mail (neue Adresse, wenn die bisherige nicht in der Signatur vorkommt),
    Telefon (neue Nummer, Vergleich auch "+49…" gegen "0…"), Position (geändert, kein Teilstring
    der anderen) und ein fehlender Name. Passt der Signaturname nicht zur Person des Kontakts,
    gibt es keine Vorschläge (die Signatur stammt von jemand anderem).
    """
    if not _namen_passen(kontakt.name, signatur.get("name")):
        logger.info(
            "Signatur von '%s' passt nicht zu Kontakt '%s' (%s) – keine Vorschläge.",
            signatur.get("name"),
            kontakt.name,
            kontakt.quelle,
        )
        return []
    vorschlaege: list[KontaktAenderung] = []
    emails = signatur.get("emails") or []
    if emails and (not kontakt.email or kontakt.email.lower() not in emails):
        vorschlaege.append(KontaktAenderung(kontakt=kontakt, feld="email", alt=kontakt.email, neu=emails[0], quelle=quelle))
    telefone = signatur.get("telefone") or []
    if telefone and not any(telefon_gleich(kontakt.telefon, t) for t in telefone):
        vorschlaege.append(KontaktAenderung(kontakt=kontakt, feld="telefon", alt=kontakt.telefon, neu=telefone[0], quelle=quelle))
    position = signatur.get("position")
    if position:
        alt_norm, neu_norm = normalisiere(kontakt.position), normalisiere(position)
        if neu_norm and alt_norm != neu_norm and neu_norm not in alt_norm and alt_norm not in neu_norm:
            vorschlaege.append(KontaktAenderung(kontakt=kontakt, feld="position", alt=kontakt.position, neu=position, quelle=quelle))
    name = signatur.get("name")
    if name and not kontakt.name:
        vorschlaege.append(KontaktAenderung(kontakt=kontakt, feld="name", alt=None, neu=name, quelle=quelle))
    return vorschlaege


def _domain(email: str | None) -> str:
    return email.rsplit("@", 1)[1].lower() if email and "@" in email else ""


def _firma_fuer_absender(liste: Kontaktliste, absender: str, signatur: Signatur) -> str | None:
    """Firma der Kontaktliste, zu der ein unbekannter Absender gehört (gleiche Domain oder Firmenname)."""
    domain = _domain(absender)
    if domain:
        for kontakt in liste.kontakte:
            if _domain(kontakt.email) == domain:
                return kontakt.firma
    firma = signatur.get("firma")
    if firma:
        fn = normalisiere_name(firma)
        if fn:
            for kontakt in liste.kontakte:
                if firmenscore(fn, firmenvarianten(kontakt.firma)) >= SCHWELLE_FIRMA:
                    return kontakt.firma
    return None


def aenderungen_aus_mail(mail: Mail, kontaktliste: Kontaktliste) -> list[KontaktAenderung]:
    """Vorschläge aus einer gelesenen Mail: Absender -> Kontakt -> :func:`vergleiche`. Ist der
    Absender unbekannt, aber seine Domain/Firma in der Liste, entsteht ein Vorschlag ``feld="neu"``
    (``neu`` = "Name | Position | E-Mail | Telefon", ``kontakt`` = ein Kontakt derselben Firma als
    Anker); fremde Absender ergeben nichts."""
    text = mail.text or html_zu_text(mail.html)
    signatur = erkenne_signatur(text)
    absender = (mail.kopf.absender or "").strip().lower()
    quelle = f"Mail {mail.kopf.id} von {absender or '?'} ({mail.kopf.betreff or 'ohne Betreff'}, {mail.kopf.empfangen:%d.%m.%Y})"
    kontakt = kontaktliste.kontakt_fuer_email(absender)
    if kontakt is not None:
        if absender not in signatur["emails"]:
            signatur["emails"].insert(0, absender)
        return vergleiche(kontakt, signatur, quelle=quelle)
    firma = _firma_fuer_absender(kontaktliste, absender, signatur) if absender else None
    if firma is None:
        logger.debug("Absender %s gehört zu keiner Firma der Kontaktliste – kein Vorschlag.", absender)
        return []
    anker = next((k for k in kontaktliste.kontakte if k.firma == firma), None)
    name = signatur.get("name") or (mail.kopf.absender_name or "").strip() or None
    teile = [name or "?", signatur.get("position") or "", absender, signatur["telefone"][0] if signatur["telefone"] else ""]
    return [KontaktAenderung(kontakt=anker, feld=FELD_NEU, alt=firma, neu=" | ".join(teile), quelle=quelle)]


def bericht(aenderungen: list[KontaktAenderung]) -> str:
    """Markdown-Liste der Vorschläge für den Überwachungsbericht (leer -> Hinweis "keine")."""
    if not aenderungen:
        return "Keine Änderungsvorschläge zur Kontaktliste."
    zeilen = ["Änderungsvorschläge zur Kontaktliste (Übernahme nur nach Freigabe; Lexware-Stammdaten unverändert):"]
    for a in aenderungen:
        k = a.kontakt
        wer = f"{k.firma} / {k.name or k.position or '?'} ({k.quelle})" if k else "?"
        status = "übernommen" if a.uebernommen else "Vorschlag"
        if a.feld == FELD_NEU:
            zeilen.append(f"- [{status}] Neuer Ansprechpartner bei {a.alt or wer}: {a.neu} (Quelle: {a.quelle})")
        else:
            zeilen.append(f"- [{status}] {wer}: {a.feld} '{a.alt or ''}' -> '{a.neu}' (Quelle: {a.quelle})")
    return "\n".join(zeilen)


# ---------------------------------------------------------------------------
# Übernahme in die Excel
# ---------------------------------------------------------------------------


def _zelle_fuer(excel: ExcelBackend, datei: DateiRefKonfig, blatt: str, zeile: int, feld: str) -> tuple[str, str]:
    """(Zelladresse, aktueller Zellwert) für Feld und Excel-Zeile über die Kopfzeile des Blocks."""
    try:
        adresse, matrix = excel.lese_benutzten_bereich(datei, blatt)
    except M365Fehler as exc:
        raise KontakteFehler(f"Blatt '{blatt}' der Kontaktliste nicht lesbar: {exc}") from exc
    bereich = parse_adresse(adresse)
    index = zeile - bereich.zeile_von
    if not 0 <= index < len(matrix):
        raise KontakteFehler(f"Zeile {zeile} liegt außerhalb des benutzten Bereichs {adresse}.")
    kopf: dict[str, int] | None = None
    for i in range(index - 1, -1, -1):
        kopf = erkenne_kopf([zelltext(z) for z in matrix[i]])
        if kopf is not None:
            break
    if kopf is None or feld not in kopf:
        raise KontakteFehler(f"Keine Kopfzeile mit Spalte '{feld}' oberhalb von {quelle_text(blatt, zeile)} gefunden.")
    spalte = bereich.spalte_von + kopf[feld]
    aktuell = matrix[index][kopf[feld]] if kopf[feld] < len(matrix[index]) else ""
    return f"{spaltenbuchstabe(spalte)}{zeile}", zelltext(aktuell)


def uebernehme(
    aenderung: KontaktAenderung,
    excel: ExcelBackend,
    datei: DateiRefKonfig,
    *,
    settings: Settings,
    freigegeben: bool = False,
    pruefen: bool = True,
) -> bool:
    """Schreibt die Änderung in die Kontaktliste, wenn ``settings.kontakte.automatisch_uebernehmen``
    oder ``freigegeben`` gilt; sonst ``False`` (bleibt Vorschlag).

    Zelle = Spalte des Felds aus der Kopfzeile des Blocks + Zeile aus ``Kontakt.quelle``. Mit
    ``pruefen`` wird vorher verglichen, dass die Zelle noch den alten Wert (``alt`` bzw. den Wert
    im Kontakt) enthält; sonst :class:`KontakteFehler` statt stillschweigend zu überschreiben.
    Setzt bei Erfolg ``aenderung.uebernommen`` und das Feld am Kontakt.
    """
    if not (settings.kontakte.automatisch_uebernehmen or freigegeben):
        logger.info("Kontaktänderung %s bleibt Vorschlag (automatisch_uebernehmen aus, nicht freigegeben).", aenderung.feld)
        return False
    if aenderung.feld == FELD_NEU or aenderung.feld not in FELDER_UEBERNEHMBAR:
        raise KontakteFehler(
            f"Feld '{aenderung.feld}' wird nicht automatisch in die Excel geschrieben (erlaubt: {', '.join(FELDER_UEBERNEHMBAR)})."
        )
    if aenderung.kontakt is None:
        raise KontakteFehler("Änderung ohne Kontakt kann nicht übernommen werden.")
    kontakt = aenderung.kontakt
    blatt, zeile = zerlege_quelle(kontakt.quelle)
    with excel_sitzung(excel, datei):
        adresse, aktuell = _zelle_fuer(excel, datei, blatt, zeile, aenderung.feld)
        erwartet = aenderung.alt if aenderung.alt is not None else getattr(kontakt, aenderung.feld, None)
        if pruefen and normalisiere(aktuell) != normalisiere(erwartet or ""):
            raise KontakteFehler(
                f"Zelle {blatt}!{adresse} enthält '{aktuell}', erwartet '{erwartet or ''}' – Kontaktliste wurde zwischenzeitlich geändert."
            )
        try:
            # TODO-PRUEFEN: Graph range-PATCH in eine Zelle eines Tabellenobjekts (tbl_Spedition_*) am
            # echten Tenant prüfen (Tabelle bleibt erhalten? Konflikt, wenn ein Kollege die Datei offen hat?).
            excel.schreibe_bereich(datei, blatt, adresse, [[aenderung.neu]])
        except M365Fehler as exc:
            raise KontakteFehler(f"Schreiben in {blatt}!{adresse} fehlgeschlagen: {exc}") from exc
    setattr(kontakt, aenderung.feld, aenderung.neu)
    aenderung.uebernommen = True
    logger.info("Kontaktliste %s!%s: %s '%s' -> '%s' (%s)", blatt, adresse, aenderung.feld, aktuell, aenderung.neu, aenderung.quelle)
    return True
