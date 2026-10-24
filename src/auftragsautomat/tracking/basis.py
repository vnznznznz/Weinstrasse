"""Gemeinsame Grundlagen der Sendungsverfolgung (Stufenmodell Hillebrand).

Jede Quelle (Mail, API, Public Tracking, Portal) erfüllt das Protokoll
:class:`TrackingQuelle` und liefert dasselbe Modell :class:`~auftragsautomat.models.Sendung`.
Regeln für alle Quellen:

* ``aktualisiere`` liefert immer eine **Kopie** der Sendung, nie das Original.
* Bei Fehlern (Netz, Login, Parser) bleibt die Sendung inhaltlich unverändert und bekommt
  einen Eintrag in ``hinweise``; **es wird nie eine Exception nach außen gereicht**.
* Muster, URLs und Selektoren kommen aus ``config/tracking_hillebrand.yaml``
  (:class:`HillebrandKonfig`, Laden mit :func:`lade_hillebrand_konfig`).

Status-Ableitung: :func:`status_aus_meilensteinen` bestimmt aus eingetretenen und geplanten
Meilensteinen den fortgeschrittensten Status; geplante Meilensteine heben den Status nur,
wenn ihre ``voraussetzung`` (z. B. "abgeholt" für eine geplante Zustellung) erreicht ist.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable
from urllib.parse import quote

import yaml
from pydantic import BaseModel, Field, field_validator

from auftragsautomat.config import DEFAULT_PROJEKT_STAMM
from auftragsautomat.models import Ereignistyp, Meilenstein, Sendung, SendungStatus

logger = logging.getLogger(__name__)

__all__ = [
    "STATUS_RANG",
    "ApiKonfig",
    "BetreffMuster",
    "BrowserKonfig",
    "HillebrandKonfig",
    "KonfigFehler",
    "MeilensteinMuster",
    "QuellenName",
    "SelektorSatz",
    "Selektoren",
    "TrackingQuelle",
    "finde_datum_tolerant",
    "ist_bekannt",
    "ist_fortschritt",
    "jetzt",
    "kompiliere",
    "lade_hillebrand_konfig",
    "mit_hinweis",
    "normalisiere_referenz",
    "parse_datum_mit_bezug",
    "status_aus_meilensteinen",
    "status_mit_puenktlichkeit",
    "vereinige_meilensteine",
    "zeit_schluessel",
]

QuellenName = Literal["mail", "api", "public_tracking", "portal", "manuell", "keine"]


# ---------------------------------------------------------------------------
# Protokoll
# ---------------------------------------------------------------------------


@runtime_checkable
class TrackingQuelle(Protocol):
    """Eine Quelle für den Sendungsstatus (Mail, API, Public Tracking, Portal)."""

    name: str

    def verfuegbar(self) -> bool:
        """Kann die Quelle grundsätzlich benutzt werden (Zugangsdaten, Bibliothek, Daten)?"""
        ...

    def aktualisiere(self, sendung: Sendung) -> Sendung:
        """Liefert eine Kopie der Sendung mit neuem Status/Meilensteinen/Quelle/letzte_pruefung.

        Bei Erfolg steht ``quelle`` auf ``name`` (daran erkennt die Kette ein Ergebnis).
        Bei Fehlern: inhaltlich unverändert plus Hinweis; nie eine Exception.
        """
        ...


# ---------------------------------------------------------------------------
# Konfiguration (config/tracking_hillebrand.yaml)
# ---------------------------------------------------------------------------


class KonfigFehler(RuntimeError):
    """Die Tracking-Konfiguration fehlt oder ist unbrauchbar (z. B. ungültiges Regex)."""


def kompiliere(muster: str) -> re.Pattern[str]:
    """Kompiliert ein Muster aus der YAML (Groß-/Kleinschreibung egal, ``.`` über Zeilen).

    ``re`` cached kompilierte Muster, deshalb ist der wiederholte Aufruf günstig.
    """
    try:
        return re.compile(muster, re.I | re.S)
    except re.error as exc:
        raise KonfigFehler(f"Ungültiges Regex-Muster in tracking_hillebrand.yaml: {muster!r} ({exc})") from exc


def _pruefe_muster(wert: str | None) -> str | None:
    if wert is not None:
        kompiliere(wert)
    return wert


class BetreffMuster(BaseModel):
    bestaetigung: str
    terminaenderung: str | None = None

    @field_validator("bestaetigung", "terminaenderung")
    @classmethod
    def pruefe_muster(cls, wert: str | None) -> str | None:
        return _pruefe_muster(wert)


class MeilensteinMuster(BaseModel):
    """Ein Ereignis-/Meilensteinmuster (englische Textvorlage der API bzw. Mail)."""

    code: str
    muster: str
    status: SendungStatus
    geplant: bool = False
    ereignis: Ereignistyp = Ereignistyp.SONSTIGES
    voraussetzung: SendungStatus | None = None

    @field_validator("muster")
    @classmethod
    def pruefe_muster(cls, wert: str) -> str:
        _pruefe_muster(wert)
        return wert


class ApiKonfig(BaseModel):
    basis_url: str = "https://api.hillebrandgori.com/"
    token_url: str = "https://login.hillebrand.com/oauth2/aus95hq7r8iIqp14M0i7/v1/token"
    scope: str = "offline_access"
    pfad_sendungen: str = "/v6/shipments"
    pfad_ereignisse: str = "/v6/shipments/{id}/events"
    limit_pro_minute: int = 100
    timeout_sekunden: float = 30.0
    token_gueltigkeit_sekunden: int = 7200
    seitengroesse: int = 100
    inaktive_einschliessen: bool = True


class SelektorSatz(BaseModel):
    """CSS-Selektoren einer Seite (einfaches CSS: Tag, .klasse, #id, Nachfahren per Leerzeichen)."""

    timeline: str | None = None
    eintrag: str | None = None
    status: str | None = None
    status_regex: str | None = None
    herkunft: str | None = None
    ziel: str | None = None
    suche: str | None = None
    warten_auf: str | None = None

    @field_validator("status_regex")
    @classmethod
    def pruefe_muster(cls, wert: str | None) -> str | None:
        return _pruefe_muster(wert)


class Selektoren(BaseModel):
    public: SelektorSatz = Field(default_factory=SelektorSatz)
    portal: SelektorSatz = Field(default_factory=SelektorSatz)


class BrowserKonfig(BaseModel):
    timeout_sekunden: float = 30.0
    viewport_breite: int = 1400
    viewport_hoehe: int = 900
    slow_mo_ms: int = 50


class HillebrandKonfig(BaseModel):
    """Inhalt von ``config/tracking_hillebrand.yaml``."""

    sprache: str = "en"
    spedition: str = "Hillebrand"
    absender_domains: list[str] = Field(default_factory=list)
    betreff_regex: BetreffMuster
    referenz_regex: str
    referenz_frei_regex: str | None = None
    kundenreferenz_regex: str | None = None
    zeitfenster_regex: str | None = None
    terminaenderung_regex: str | None = None
    meilenstein_muster: list[MeilensteinMuster] = Field(default_factory=list)
    geplant_regex: str | None = None
    api: ApiKonfig = Field(default_factory=ApiKonfig)
    status_mapping: dict[str, SendungStatus] = Field(default_factory=dict)
    on_time_mapping: dict[str, bool] = Field(default_factory=dict)
    status_texte: dict[str, SendungStatus] = Field(default_factory=dict)
    public_tracking_url: str = "https://my.hillebrandgori.com/public-tracking/{suchbegriff}"
    portal_url: str = "https://my.hillebrandgori.com/"
    portal_tracking_url: str | None = None
    login_hosts: list[str] = Field(default_factory=lambda: ["login.hillebrand.com"])
    challenge_regex: str | None = None
    login_regex: str | None = None
    selektoren: Selektoren = Field(default_factory=Selektoren)
    browser: BrowserKonfig = Field(default_factory=BrowserKonfig)

    @field_validator(
        "referenz_regex",
        "referenz_frei_regex",
        "kundenreferenz_regex",
        "zeitfenster_regex",
        "terminaenderung_regex",
        "geplant_regex",
        "challenge_regex",
        "login_regex",
    )
    @classmethod
    def pruefe_muster(cls, wert: str | None) -> str | None:
        return _pruefe_muster(wert)

    @field_validator("absender_domains", "login_hosts")
    @classmethod
    def _klein(cls, werte: list[str]) -> list[str]:
        return [w.strip().lstrip("@").lower() for w in werte if w and w.strip()]

    # -- Hilfen -------------------------------------------------------------

    def ist_absender_bekannt(self, adresse: str | None) -> bool:
        """True, wenn die Absenderadresse zu einer konfigurierten Domain gehört (auch Subdomains)."""
        if not adresse or "@" not in adresse:
            return False
        domain = adresse.rsplit("@", 1)[1].strip().strip(">").lower()
        return any(domain == d or domain.endswith("." + d) for d in self.absender_domains)

    def ist_login_host(self, url: str | None) -> bool:
        """True, wenn die URL auf einem der Login-Hosts (Okta) liegt."""
        if not url:
            return False
        treffer = re.match(r"^[a-z][a-z0-9+.-]*://([^/?#]+)", url.strip(), re.I)
        host = (treffer.group(1) if treffer else url).rsplit("@", 1)[-1].split(":")[0].lower()
        return any(host == h or host.endswith("." + h) for h in self.login_hosts)

    def muster_fuer_code(self, code: str | None) -> MeilensteinMuster | None:
        if not code:
            return None
        code = code.strip().lower()
        for m in self.meilenstein_muster:
            if m.code.lower() == code:
                return m
        # Webhook-Varianten "…chg" (Datum geändert) auf das Grundmuster abbilden
        if code.endswith("chg"):
            return self.muster_fuer_code(code[:-3])
        return None

    def muster_fuer_text(self, text: str | None) -> MeilensteinMuster | None:
        """Erstes Meilensteinmuster, dessen Regex auf den Text passt."""
        if not text:
            return None
        for m in self.meilenstein_muster:
            if kompiliere(m.muster).search(text):
                return m
        return None

    def status_fuer_text(self, text: str | None) -> SendungStatus | None:
        """Statusbezeichnung der Oberfläche ("Delivered") -> SendungStatus; längste Übereinstimmung gewinnt."""
        if not text:
            return None
        t = " ".join(text.split()).strip().lower()
        if not t:
            return None
        treffer: tuple[int, SendungStatus] | None = None
        for bezeichnung, status in self.status_texte.items():
            b = bezeichnung.strip().lower()
            if b and (t == b or re.search(rf"(?<![a-z]){re.escape(b)}(?![a-z])", t)):
                if treffer is None or len(b) > treffer[0]:
                    treffer = (len(b), status)
        return treffer[1] if treffer else None

    def ist_geplant_text(self, text: str | None) -> bool:
        return bool(text and self.geplant_regex and kompiliere(self.geplant_regex).search(text))

    def ist_challenge(self, html: str | None) -> bool:
        """Cloudflare-Challenge ("Just a moment…") statt Sendungsdaten?"""
        return bool(html and self.challenge_regex and kompiliere(self.challenge_regex).search(html))

    def ist_login_seite(self, html: str | None, url: str | None = None) -> bool:
        """Okta-Anmeldeseite (am HTML oder an der URL erkannt)?"""
        if self.ist_login_host(url):
            return True
        return bool(html and self.login_regex and kompiliere(self.login_regex).search(html))

    def public_url(self, suchbegriff: str) -> str:
        return self.public_tracking_url.format(suchbegriff=quote(suchbegriff.strip(), safe=""))

    def portal_suche_url(self, suchbegriff: str) -> str:
        vorlage = self.portal_tracking_url or self.public_tracking_url
        return vorlage.format(suchbegriff=quote(suchbegriff.strip(), safe=""))


def lade_hillebrand_konfig(pfad: Path | str | None = None) -> HillebrandKonfig:
    """Lädt ``config/tracking_hillebrand.yaml``.

    Fehlt die angegebene Datei (z. B. in einer Test-Konfiguration ohne eigene Tracking-Datei),
    wird die Standarddatei aus dem Repository verwendet und das geloggt.
    """
    standard = DEFAULT_PROJEKT_STAMM / "config" / "tracking_hillebrand.yaml"
    datei = Path(pfad) if pfad else standard
    if not datei.exists():
        if datei != standard and standard.exists():
            logger.info("Tracking-Konfiguration %s fehlt, verwende %s", datei, standard)
            datei = standard
        else:
            raise KonfigFehler(f"Tracking-Konfiguration nicht gefunden: {datei}")
    try:
        roh = yaml.safe_load(datei.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise KonfigFehler(f"Tracking-Konfiguration {datei} ist nicht lesbar: {exc}") from exc
    if not isinstance(roh, dict):
        raise KonfigFehler(f"Tracking-Konfiguration {datei} muss ein Mapping sein")
    try:
        return HillebrandKonfig.model_validate(roh)
    except ValueError as exc:
        raise KonfigFehler(f"Tracking-Konfiguration {datei} ist ungültig: {exc}") from exc


# ---------------------------------------------------------------------------
# Status-Logik
# ---------------------------------------------------------------------------

# Fortschritt einer Sendung (höher = weiter). VERSPAETET/PROBLEM sind keine Fortschrittsstufen,
# sondern Ampelzustände; sie werden über ``status_mit_puenktlichkeit`` gesetzt.
STATUS_RANG: dict[SendungStatus, int] = {
    SendungStatus.UNBEKANNT: 0,
    SendungStatus.BEAUFTRAGT: 1,
    SendungStatus.BESTAETIGT: 2,
    SendungStatus.ABHOLUNG_GEPLANT: 3,
    SendungStatus.ABGEHOLT: 4,
    SendungStatus.UNTERWEGS: 5,
    SendungStatus.ZUSTELLUNG_GEPLANT: 6,
    SendungStatus.ZUGESTELLT: 7,
    SendungStatus.VERSPAETET: 1,
    SendungStatus.PROBLEM: 1,
}


def ist_bekannt(status: SendungStatus | None) -> bool:
    """Alles außer ``unbekannt`` zählt als Ergebnis einer Quelle."""
    return status is not None and status != SendungStatus.UNBEKANNT


def ist_fortschritt(neu: SendungStatus | None, alt: SendungStatus | None) -> bool:
    """True, wenn ``neu`` im Ablauf weiter ist als ``alt``."""
    return STATUS_RANG.get(neu or SendungStatus.UNBEKANNT, 0) > STATUS_RANG.get(alt or SendungStatus.UNBEKANNT, 0)


def status_aus_meilensteinen(
    meilensteine: list[Meilenstein],
    konfig: HillebrandKonfig | None = None,
    *,
    puenktlich: bool | None = None,
    basis: SendungStatus = SendungStatus.UNBEKANNT,
) -> SendungStatus:
    """Leitet den Sendungsstatus aus den Meilensteinen ab.

    * Eingetretene Meilensteine bestimmen den erreichten Fortschritt (Maximum).
    * Geplante Meilensteine heben den Status nur, wenn ihre ``voraussetzung`` erreicht ist
      (eine geplante Zustellung zählt erst nach der Abholung).
    * ``basis`` ist ein bereits bekannter Status (z. B. aus der API), der nicht unterschritten wird.
    * ``puenktlich=False`` ergibt ``verspaetet``, solange nicht zugestellt.

    Ohne ``konfig`` werden nur die Standardcodes der API/Webhooks erkannt.
    """
    konfig = konfig or _standard_konfig()
    erreicht = basis
    for m in meilensteine:
        if m.geplant:
            continue
        muster = _muster_fuer(m, konfig)
        if muster is not None and ist_fortschritt(muster.status, erreicht):
            erreicht = muster.status
    ergebnis = erreicht
    for m in meilensteine:
        if not m.geplant:
            continue
        muster = _muster_fuer(m, konfig)
        if muster is None:
            continue
        if muster.voraussetzung is not None and ist_fortschritt(muster.voraussetzung, erreicht):
            continue
        if ist_fortschritt(muster.status, ergebnis):
            ergebnis = muster.status
    return status_mit_puenktlichkeit(ergebnis, puenktlich)


def status_mit_puenktlichkeit(status: SendungStatus, puenktlich: bool | None) -> SendungStatus:
    """``verspaetet``, wenn die Spedition "late" meldet und die Sendung noch nicht zugestellt ist."""
    if puenktlich is False and status not in (SendungStatus.ZUGESTELLT, SendungStatus.UNBEKANNT):
        return SendungStatus.VERSPAETET
    return status


def _muster_fuer(m: Meilenstein, konfig: HillebrandKonfig) -> MeilensteinMuster | None:
    return konfig.muster_fuer_code(m.code) or konfig.muster_fuer_text(m.text)


_STANDARD_MUSTER: list[dict[str, Any]] = [
    {"code": "shprdy", "muster": r"ready for transport", "status": "bestaetigt"},
    {"code": "shpsupdeppln", "muster": r"planned (?:to )?depart", "status": "abholung_geplant", "geplant": True},
    {"code": "shpsupdepcnf", "muster": r"has departed", "status": "abgeholt", "ereignis": "abholung"},
    {
        "code": "shpcusarrpln",
        "muster": r"planned (?:to )?arriv",
        "status": "zustellung_geplant",
        "geplant": True,
        "voraussetzung": "abgeholt",
    },
    {"code": "shpcusarrcnf", "muster": r"has arrived|delivered", "status": "zugestellt", "ereignis": "zustellung"},
]


def _standard_konfig() -> HillebrandKonfig:
    """Minimale Konfiguration (nur Standardcodes), falls keine YAML geladen wurde."""
    return HillebrandKonfig(
        betreff_regex=BetreffMuster(bestaetigung=r"Our\s*Ref\s*:\s*(?P<referenz>DEM[A-Z]?\d+)"),
        referenz_regex=r"Our\s*Ref\s*:\s*(?P<referenz>DEM[A-Z]?\d+)",
        meilenstein_muster=[MeilensteinMuster.model_validate(m) for m in _STANDARD_MUSTER],
    )


# ---------------------------------------------------------------------------
# Kleine Helfer für alle Quellen
# ---------------------------------------------------------------------------


def jetzt() -> datetime:
    return datetime.now()


def normalisiere_referenz(text: str | None) -> str | None:
    """Referenz vergleichbar machen: Leerraum weg, Großschreibung, Job-Zusatz ``[job…]`` entfernen."""
    if text is None:
        return None
    t = re.sub(r"\[.*?\]", "", str(text))
    t = re.sub(r"\s+", "", t).strip().upper()
    return t or None


def mit_hinweis(sendung: Sendung, hinweis: str) -> Sendung:
    """Kopie der Sendung mit zusätzlichem Hinweis (ohne Doppelungen) und frischer ``letzte_pruefung``.

    Status, Meilensteine und Quelle bleiben unverändert (Fehlerfall einer Quelle).
    """
    hinweise = list(sendung.hinweise)
    if hinweis and hinweis not in hinweise:
        hinweise.append(hinweis)
    return sendung.model_copy(update={"hinweise": hinweise, "letzte_pruefung": jetzt()})


def zeit_schluessel(zeit: datetime | None) -> datetime:
    """Sortierschlüssel für Zeitpunkte mit **und** ohne Zeitzone.

    Graph und ``.eml`` liefern Empfangszeiten mit Zeitzone, Datumsangaben aus Mail-Texten, der API oder
    dem Portal sind zeitzonenlos; Python kann beide nicht vergleichen (``TypeError``). Zeitzonenbehaftete
    Werte werden deshalb in zeitzonenlose Ortszeit umgerechnet, ein fehlender Zeitpunkt zählt als
    ``datetime.min``.
    """
    if zeit is None:
        return datetime.min
    if zeit.tzinfo is not None:
        return zeit.astimezone().replace(tzinfo=None)
    return zeit


def vereinige_meilensteine(alt: list[Meilenstein], neu: list[Meilenstein]) -> list[Meilenstein]:
    """Alte und neue Meilensteine ohne Doppelte (Code + Zeitpunkt + geplant), chronologisch."""
    ergebnis: list[Meilenstein] = []
    gesehen: set[tuple[str, datetime | None, bool]] = set()
    for m in list(alt) + list(neu):
        schluessel = (m.code, m.zeitpunkt, m.geplant)
        if schluessel in gesehen:
            continue
        gesehen.add(schluessel)
        ergebnis.append(m)
    ergebnis.sort(key=lambda m: (zeit_schluessel(m.zeitpunkt), m.geplant))
    return ergebnis


_DATUM_KURZ = re.compile(r"^\s*(?P<t>\d{1,2})\.\s?(?P<m>\d{1,2})\.\s?(?P<j>\d{4}|\d{2})?\s*$")
_DATUM_ISO = re.compile(r"^\s*(?P<j>\d{4})-(?P<m>\d{1,2})-(?P<t>\d{1,2})")


def parse_datum_mit_bezug(text: str | None, bezug: date | datetime | None) -> date | None:
    """Liest "27.10.", "27.10.2026", "27.10.26" oder "2026-10-27".

    Fehlt das Jahr, wird es aus ``bezug`` (Empfangsdatum der Mail) ergänzt: Liegt der Tag mehr als
    zwei Monate vor dem Bezug, ist das Folgejahr gemeint (Mail im Dezember, Termin im Januar).
    """
    if not text:
        return None
    if isinstance(bezug, datetime):
        bezug = bezug.date()
    treffer = _DATUM_ISO.match(text)
    if treffer:
        try:
            return date(int(treffer["j"]), int(treffer["m"]), int(treffer["t"]))
        except ValueError:
            return None
    treffer = _DATUM_KURZ.match(text)
    if not treffer:
        return None
    tag, monat = int(treffer["t"]), int(treffer["m"])
    jahr_text = treffer["j"]
    if jahr_text:
        jahr = int(jahr_text)
        if jahr < 100:
            jahr += 2000 if jahr < 80 else 1900
    else:
        bezug = bezug or date.today()
        jahr = bezug.year
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


_MONATE_EN: dict[str, int] = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3, "apr": 4, "april": 4,
    "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7, "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9, "oct": 10, "october": 10, "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}  # fmt: skip
_MONAT_EN_RE = "|".join(sorted(_MONATE_EN, key=len, reverse=True))

# Reihenfolge: ISO, "22 May 2026", "May 22, 2026", deutsch mit Jahr, deutsch ohne Jahr.
_DATUM_TOLERANT = re.compile(
    r"(?<![\d.])(?:"
    r"(?P<iso_j>\d{4})-(?P<iso_m>\d{1,2})-(?P<iso_t>\d{1,2})"
    r"|(?P<et>\d{1,2})(?:st|nd|rd|th)?[\s.-]+(?P<em>" + _MONAT_EN_RE + r")\.?[\s,-]+(?P<ej>\d{4})"
    r"|(?P<am>" + _MONAT_EN_RE + r")\.?\s+(?P<at>\d{1,2})(?:st|nd|rd|th)?,?\s+(?P<aj>\d{4})"
    r"|(?P<t>\d{1,2})\.\s?(?P<m>\d{1,2})\.\s?(?P<j>\d{4}|\d{2})?"
    r")(?![\d]\d)",
    re.I,
)
_UHRZEIT = re.compile(r"(?<!\d)(?P<h>[01]?\d|2[0-3]):(?P<min>[0-5]\d)(?!\d)")


def finde_datum_tolerant(text: str | None, bezug: date | datetime | None = None) -> datetime | None:
    """Erstes Datum (deutsch, ISO oder englisch "22 May 2026" / "May 22, 2026") als ``datetime``.

    Eine Uhrzeit ``HH:MM`` nach dem Datum wird übernommen. Kurzdaten ohne Jahr ("27.10.")
    bekommen das Jahr aus ``bezug`` (siehe :func:`parse_datum_mit_bezug`).
    """
    if not text:
        return None
    for treffer in _DATUM_TOLERANT.finditer(text):
        g = treffer.groupdict()
        wert: date | None
        try:
            if g["iso_j"]:
                wert = date(int(g["iso_j"]), int(g["iso_m"]), int(g["iso_t"]))
            elif g["et"]:
                wert = date(int(g["ej"]), _MONATE_EN[g["em"].lower()], int(g["et"]))
            elif g["am"]:
                wert = date(int(g["aj"]), _MONATE_EN[g["am"].lower()], int(g["at"]))
            else:
                wert = parse_datum_mit_bezug(treffer.group(0), bezug)
        except (ValueError, KeyError):
            wert = None
        if wert is None:
            continue
        zeit = _UHRZEIT.search(text, treffer.end(), min(len(text), treffer.end() + 20))
        if zeit:
            return datetime(wert.year, wert.month, wert.day, int(zeit["h"]), int(zeit["min"]))
        return datetime(wert.year, wert.month, wert.day)
    return None
