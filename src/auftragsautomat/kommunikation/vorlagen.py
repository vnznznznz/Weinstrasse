"""Jinja2-Vorlagen und Signaturen für E-Mail-Entwürfe (docs/AUSBAUSTUFE_2.md, Abschnitt 2.4).

* Vorlagen liegen in ``settings.kommunikation.vorlagen_ordner`` (Standard ``config/vorlagen``), jede
  als ``<name>.html.j2`` (HTML, Autoescape an) **und** ``<name>.txt.j2`` (Klartext-Alternative,
  Autoescape aus). :meth:`Vorlagen.rendere` liefert beide Fassungen.
* Filter: ``nl2br`` (erst escapen, dann ``Markup("<br>").join`` – im Textkontext bleibt der Text
  unverändert), ``datum_de`` (``24.10.2026``), ``betrag_de`` (``1.234,56 €``), ``menge_de`` (``3.420``).
* Signaturen kommen aus ``config/signaturen.yaml`` (je Firma eine persönliche und eine Team-Variante
  mit Platzhaltern ``absender_name``, ``firma_name``, ``telefon``, ``adresse``); Graph fügt keine
  Outlook-Signatur ein (Recherche ``excel_lokal_und_vorlagen.md``, B.2). Die Signatur wird den Vorlagen
  als ``signatur`` übergeben (HTML als ``Markup``, Text als ``str``); jede Vorlage endet mit ihr.

Alle Texte der Vorlagen sind kurz und freundlich (Anrede, zwei bis vier Sätze, Grußformel,
Signatur) – nie nur die Signatur.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal

import yaml
from jinja2 import Environment, FileSystemLoader, TemplateNotFound, pass_eval_context, select_autoescape
from jinja2.nodes import EvalContext
from markupsafe import Markup, escape
from pydantic import BaseModel, Field

from auftragsautomat.config import Settings

logger = logging.getLogger(__name__)

__all__ = [
    "ENDUNG_HTML",
    "ENDUNG_TEXT",
    "FirmenSignatur",
    "SignaturBlock",
    "SignaturKatalog",
    "SignaturVorlage",
    "Signaturvariante",
    "Vorlagen",
    "VorlagenFehler",
    "betrag_de",
    "datum_de",
    "erzeuge_environment",
    "lade_signaturen",
    "menge_de",
    "nl2br",
    "rendere",
    "signatur_fuer",
    "vorlagen_aus_settings",
]

ENDUNG_HTML = ".html.j2"
ENDUNG_TEXT = ".txt.j2"
DATUMSFORMAT = "%d.%m.%Y"

Signaturvariante = Literal["persoenlich", "team"]


class VorlagenFehler(RuntimeError):
    """Vorlagenordner fehlt, eine Vorlage fehlt (HTML oder Text) oder die Signaturdatei ist ungültig."""


# ---------------------------------------------------------------------------
# Filter
# ---------------------------------------------------------------------------


@pass_eval_context
def nl2br(eval_ctx: EvalContext, wert: Any) -> Markup | str:
    """Zeilenumbrüche -> ``<br>``. Der Text wird zuerst escaped, dann mit ``Markup("<br>")`` verbunden
    (``Markup.replace`` würde die Argumente escapen, Recherche B.3). Ohne Autoescape (Textvorlagen)
    bleibt der Text unverändert."""
    if wert is None:
        return Markup("") if eval_ctx.autoescape else ""
    text = str(wert)
    if not eval_ctx.autoescape:
        return text
    return Markup("<br>").join(escape(zeile) for zeile in text.splitlines())


def datum_de(wert: Any, format: str = DATUMSFORMAT) -> str:
    """``date``/``datetime`` -> ``24.10.2026`` (``datetime`` mit Uhrzeit, wenn ``format`` sie enthält);
    Text wird durchgereicht, ``None`` ergibt ``""``."""
    if wert is None or wert == "":
        return ""
    if isinstance(wert, datetime | date):
        return wert.strftime(format)
    return str(wert)


def _dezimal(wert: Any) -> Decimal | None:
    if wert is None or wert == "":
        return None
    if isinstance(wert, Decimal):
        return wert
    try:
        return Decimal(str(wert).replace(",", "."))
    except (InvalidOperation, ValueError):
        return None


def _deutsch(zahl: Decimal, stellen: int) -> str:
    q = zahl.quantize(Decimal(1).scaleb(-stellen)) if stellen > 0 else zahl.quantize(Decimal(1))
    text = f"{q:,.{stellen}f}"
    return text.replace(",", "\x00").replace(".", ",").replace("\x00", ".")


def betrag_de(wert: Any, waehrung: str | None = "€", stellen: int = 2) -> str:
    """Decimal -> ``1.234,56 €`` (deutsches Zahlenformat); ``None``/leer -> ``""``."""
    zahl = _dezimal(wert)
    if zahl is None:
        return "" if wert in (None, "") else str(wert)
    text = _deutsch(zahl, stellen)
    return f"{text} {waehrung}" if waehrung else text


def menge_de(wert: Any) -> str:
    """Decimal -> ``3.420`` (ganzzahlig) bzw. ``1.234,5`` (bis zu drei Nachkommastellen, ohne Nullen)."""
    zahl = _dezimal(wert)
    if zahl is None:
        return "" if wert in (None, "") else str(wert)
    if zahl == zahl.to_integral_value():
        return _deutsch(zahl, 0)
    text = _deutsch(zahl, 3)
    return text.rstrip("0").rstrip(",")


# ---------------------------------------------------------------------------
# Environment und Vorlagen
# ---------------------------------------------------------------------------


def erzeuge_environment(ordner: Path) -> Environment:
    """Jinja2-Environment für den Vorlagenordner: Autoescape für ``.html``/``.html.j2``, nicht für
    ``.txt``/``.txt.j2``; ``trim_blocks``/``lstrip_blocks``; eigene Filter."""
    env = Environment(
        loader=FileSystemLoader(str(ordner)),
        autoescape=select_autoescape(
            enabled_extensions=("html", "htm", "html.j2"),
            disabled_extensions=("txt", "txt.j2"),
            default_for_string=True,
            default=True,
        ),
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=False,
    )
    env.filters["nl2br"] = nl2br
    env.filters["datum_de"] = datum_de
    env.filters["betrag_de"] = betrag_de
    env.filters["menge_de"] = menge_de
    return env


@dataclass(frozen=True)
class SignaturBlock:
    """Gerenderte Signatur: HTML-Fassung (wird als ``Markup`` eingesetzt) und Textfassung."""

    html: str
    text: str
    variante: str = "persoenlich"
    firma_id: str | None = None


def _bereinige_html(html: str) -> str:
    zeilen = [z.rstrip() for z in html.replace("\r\n", "\n").split("\n")]
    return "\n".join(z for z in zeilen if z.strip()).strip()


def _bereinige_text(text: str) -> str:
    zeilen = [z.rstrip() for z in text.replace("\r\n", "\n").split("\n")]
    ergebnis: list[str] = []
    leer = 0
    for zeile in zeilen:
        if not zeile:
            leer += 1
            if leer > 1:
                continue
        else:
            leer = 0
        ergebnis.append(zeile)
    return "\n".join(ergebnis).strip()


class Vorlagen:
    """Vorlagen eines Ordners; jede Vorlage muss als HTML und Text vorliegen."""

    def __init__(self, ordner: Path) -> None:
        self.ordner = Path(ordner)
        if not self.ordner.is_dir():
            raise VorlagenFehler(f"Vorlagenordner nicht gefunden: {self.ordner}")
        self.env = erzeuge_environment(self.ordner)

    def namen(self) -> list[str]:
        """Namen aller Vorlagen, die als ``.html.j2`` **und** ``.txt.j2`` vorliegen (sortiert)."""
        html = {p.name[: -len(ENDUNG_HTML)] for p in self.ordner.glob(f"*{ENDUNG_HTML}")}
        text = {p.name[: -len(ENDUNG_TEXT)] for p in self.ordner.glob(f"*{ENDUNG_TEXT}")}
        return sorted(html & text)

    def existiert(self, name: str) -> bool:
        return (self.ordner / f"{name}{ENDUNG_HTML}").is_file() and (self.ordner / f"{name}{ENDUNG_TEXT}").is_file()

    def _vorlage(self, dateiname: str) -> Any:
        try:
            return self.env.get_template(dateiname)
        except TemplateNotFound as exc:
            raise VorlagenFehler(f"Vorlage '{dateiname}' fehlt im Ordner {self.ordner}.") from exc

    def rendere(self, name: str, *, signatur: SignaturBlock | str | None = None, **kontext: Any) -> tuple[str, str]:
        """Rendert ``<name>.html.j2`` und ``<name>.txt.j2`` -> ``(html, text)``.

        ``signatur`` (``SignaturBlock``) wird der HTML-Vorlage als ``Markup`` und der Textvorlage als Text
        übergeben; ohne Signatur ist ``signatur`` leer. Ein reiner Text als Signatur wird im HTML escaped.
        """
        block: SignaturBlock | None
        if signatur is None or isinstance(signatur, SignaturBlock):
            block = signatur
        else:
            block = SignaturBlock(html=str(escape(str(signatur))), text=str(signatur))
        html_vorlage = self._vorlage(f"{name}{ENDUNG_HTML}")
        text_vorlage = self._vorlage(f"{name}{ENDUNG_TEXT}")
        html = html_vorlage.render(**kontext, signatur=Markup(block.html) if block else Markup(""))
        text = text_vorlage.render(**kontext, signatur=block.text if block else "")
        return _bereinige_html(html), _bereinige_text(text)


# Environments für Vorlagen aus Strings (Signaturen in signaturen.yaml): HTML escaped, Text nicht.
_TEXT_ENV = Environment(autoescape=False, trim_blocks=True, lstrip_blocks=True, keep_trailing_newline=False)
_HTML_ENV = Environment(autoescape=True, trim_blocks=True, lstrip_blocks=True, keep_trailing_newline=False)
for _env in (_TEXT_ENV, _HTML_ENV):
    _env.filters["nl2br"] = nl2br
    _env.filters["datum_de"] = datum_de
    _env.filters["betrag_de"] = betrag_de
    _env.filters["menge_de"] = menge_de


def vorlagen_aus_settings(settings: Settings) -> Vorlagen:
    """``Vorlagen`` für ``settings.kommunikation.vorlagen_ordner``."""
    return Vorlagen(Path(settings.kommunikation.vorlagen_ordner))


def rendere(name: str, *, settings: Settings, signatur: SignaturBlock | None = None, **kontext: Any) -> tuple[str, str]:
    """Kurzform: ``vorlagen_aus_settings(settings).rendere(name, signatur=..., **kontext)``."""
    return vorlagen_aus_settings(settings).rendere(name, signatur=signatur, **kontext)


# ---------------------------------------------------------------------------
# Signaturen (config/signaturen.yaml)
# ---------------------------------------------------------------------------


class SignaturVorlage(BaseModel):
    html: str
    text: str


class FirmenSignatur(BaseModel):
    firma_name: str | None = None
    team_name: str | None = None
    variante: Signaturvariante | None = None  # überschreibt standard_variante für diese Firma
    werte: dict[str, str] = Field(default_factory=dict)  # telefon, adresse, ...
    html: str | None = None
    text: str | None = None


_STANDARD_HTML = (
    "<p>{{ absender_name }}<br>\n<b>{{ firma_name }}</b><br>\n{{ adresse }}<br>\n"
    "Tel. {{ telefon }}{% if absender_email %} · {{ absender_email }}{% endif %}</p>"
)
_STANDARD_TEXT = (
    "{{ absender_name }}\n{{ firma_name }}\n{{ adresse }}\n"
    "Tel. {{ telefon }}{% if absender_email %} · {{ absender_email }}{% endif %}"
)
_TEAM_HTML = _STANDARD_HTML.replace("{{ absender_name }}", "{{ team_name }}")
_TEAM_TEXT = _STANDARD_TEXT.replace("{{ absender_name }}", "{{ team_name }}")


class SignaturKatalog(BaseModel):
    """Inhalt von ``config/signaturen.yaml``; ohne Datei gelten eingebaute Standardvorlagen."""

    standard_variante: Signaturvariante = "persoenlich"
    platzhalter: dict[str, str] = Field(default_factory=lambda: {"telefon": "", "adresse": ""})
    firmen: dict[str, FirmenSignatur] = Field(default_factory=dict)
    standard: SignaturVorlage = Field(default_factory=lambda: SignaturVorlage(html=_STANDARD_HTML, text=_STANDARD_TEXT))
    team: SignaturVorlage = Field(default_factory=lambda: SignaturVorlage(html=_TEAM_HTML, text=_TEAM_TEXT))
    quelle: Path | None = None


def lade_signaturen(pfad: Path | None) -> SignaturKatalog:
    """Lädt ``config/signaturen.yaml``; fehlt die Datei, gelten die eingebauten Standardvorlagen (Info im Log)."""
    if pfad is None or not Path(pfad).is_file():
        logger.info("Signaturdatei %s fehlt – eingebaute Standard-Signatur wird verwendet.", pfad)
        return SignaturKatalog()
    try:
        roh = yaml.safe_load(Path(pfad).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise VorlagenFehler(f"Signaturdatei {pfad} ist nicht lesbar: {exc}") from exc
    if not isinstance(roh, dict):
        raise VorlagenFehler(f"Signaturdatei {pfad} muss ein Mapping sein.")
    try:
        katalog = SignaturKatalog.model_validate(roh)
    except ValueError as exc:
        raise VorlagenFehler(f"Signaturdatei {pfad} ist ungültig: {exc}") from exc
    katalog.quelle = Path(pfad)
    return katalog


def _firma_name(settings: Settings, firma_id: str | None) -> str:
    if firma_id and firma_id in settings.firmen:
        return settings.firmen[firma_id].name
    return firma_id or ""


def _firma_kurz(settings: Settings, firma_id: str | None) -> str:
    if firma_id and firma_id in settings.firmen:
        return settings.firmen[firma_id].kurz
    return (firma_id or "").split("_")[0].upper()


def signatur_fuer(
    settings: Settings,
    firma_id: str | None,
    *,
    variante: Signaturvariante | None = None,
    katalog: SignaturKatalog | None = None,
) -> SignaturBlock:
    """Signatur für eine Firma: ``variante`` > Firmeneintrag > ``standard_variante``; ohne
    ``settings.kommunikation.absender_name`` immer die Team-Signatur. Platzhalter werden mit den
    Einstellungen, dem Firmennamen und den Werten der YAML gefüllt (HTML escaped)."""
    # TODO-PRUEFEN: Platzhalterwerte (Telefon, Adresse) je Firma in config/signaturen.yaml mit dem Anwender
    # ausfüllen und Darstellung der HTML-Signatur im Graph-Entwurf (Outlook klassisch/neu) ansehen.
    katalog = katalog or lade_signaturen(settings.kommunikation.signaturen_datei)
    eintrag = katalog.firmen.get(firma_id or "", FirmenSignatur())
    absender_name = (settings.kommunikation.absender_name or "").strip()
    gewaehlt: Signaturvariante = variante or eintrag.variante or katalog.standard_variante
    if gewaehlt == "persoenlich" and not absender_name:
        logger.info("Kein absender_name konfiguriert – Team-Signatur für %s.", firma_id)
        gewaehlt = "team"
    werte: dict[str, Any] = {**katalog.platzhalter, **eintrag.werte}
    werte.update(
        absender_name=absender_name,
        absender_email=settings.kommunikation.absender_email or "",
        firma_name=eintrag.firma_name or _firma_name(settings, firma_id),
        team_name=eintrag.team_name or f"Ihr {_firma_kurz(settings, firma_id)}-Team",
        firma_id=firma_id or "",
    )
    if gewaehlt == "team":
        html_vorlage, text_vorlage = katalog.team.html, katalog.team.text
    else:
        html_vorlage = eintrag.html or katalog.standard.html
        text_vorlage = eintrag.text or katalog.standard.text
    html = _HTML_ENV.from_string(html_vorlage).render(**werte)
    text = _TEXT_ENV.from_string(text_vorlage).render(**werte)
    return SignaturBlock(html=_bereinige_html(html), text=_bereinige_text(text), variante=gewaehlt, firma_id=firma_id)
