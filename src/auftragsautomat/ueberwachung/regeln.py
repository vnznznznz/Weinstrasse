"""Regelwerk des täglichen Überwachungslaufs (docs/AUSBAUSTUFE_2.md, Abschnitt 2.6, Schritt 3).

:func:`bewerte` beurteilt einen Fixtermin (Zeile der Fixterminüberwachung) zusammen mit dem
Tracking-Stand der Sendung und liefert Ampel, Begründung und die fälligen Maßnahmen:

* Lieferant hat nicht bestätigt -> ab ``nachfrage_lieferant_werktage`` Werktagen vor dem Fixtermin
  ``nachfrage_lieferant`` (gelb; rot, wenn weniger als ``rot_unter_werktagen`` Werktage bis zum Fixtermin
  bleiben); vorher nur gelb ohne Entwurf. Die Frist steht in der Begründung; ist sie überschritten, heißt
  es "überfällig".
* Spedition hat nicht bestätigt -> ``nachfrage_spedition`` (analog ab ``nachfrage_spedition_werktage``).
* Verladedatum erreicht, aber nicht abgeholt -> ``status_spedition`` + ``tracking``.
* Fixtermin heute oder morgen -> ``erinnerung``.
* Fixtermin überschritten und nicht zugestellt -> rot; zugestellt -> grau; alles bestätigt und
  abgeholt -> grün.

Die Schwellen kommen aus ``settings.ueberwachung`` (:class:`~auftragsautomat.config.UeberwachungKonfig`);
Feinheiten (Mindestalter des Auftrags vor der ersten Nachfrage, Rot-Schwelle, Texte) aus
``config/ueberwachung.yaml`` (:class:`Regelwerk`). Nachfragen an den Lieferanten gibt es frühestens
``mindest_alter_werktage`` Werktage nach dem Auftragsdatum, damit niemand am Tag der Bestellung
schon gemahnt wird.

:class:`Drossel` sorgt dafür, dass dieselbe Maßnahme je Vorgang höchstens alle
``erneut_nach_werktagen`` Werktage ausgelöst wird (Merkdatei ``settings.ueberwachung.merkdatei``,
JSON ``{schlüssel: letztes Datum}``).
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, ValidationError

from auftragsautomat.benennung import normalisiere
from auftragsautomat.config import Settings, UeberwachungKonfig
from auftragsautomat.models import Ampel, Fixtermin, Sendung, SendungStatus
from auftragsautomat.termine import kalender

logger = logging.getLogger(__name__)

__all__ = [
    "AMPEL_RANG",
    "DROSSEL_GRUPPE",
    "GEDROSSELTE_MASSNAHMEN",
    "MASSNAHMEN",
    "MASSNAHME_BEZEICHNUNG",
    "OHNE_SPEDITION",
    "STANDARD_DATEINAME",
    "Bewertung",
    "Drossel",
    "Massnahme",
    "MassnahmeTyp",
    "Regelwerk",
    "RegelwerkFehler",
    "Texte",
    "bewerte",
    "hoechste_ampel",
    "regelwerk_pfad",
    "tracking_abgeholt",
    "tracking_bekannt",
    "tracking_problem",
    "tracking_zugestellt",
]

MassnahmeTyp = Literal["nachfrage_lieferant", "nachfrage_spedition", "status_spedition", "erinnerung", "tracking"]
#: Alle Maßnahmentypen in Anzeige-Reihenfolge.
MASSNAHMEN: tuple[MassnahmeTyp, ...] = (
    "nachfrage_lieferant",
    "nachfrage_spedition",
    "status_spedition",
    "erinnerung",
    "tracking",
)
#: Maßnahmen, die einen E-Mail-Entwurf je Vorgang erzeugen und deshalb gedrosselt werden.
GEDROSSELTE_MASSNAHMEN: frozenset[str] = frozenset({"nachfrage_lieferant", "nachfrage_spedition", "status_spedition"})
#: Drossel-Gruppe je Maßnahme: Nachfrage und Statusanfrage an die Spedition gehen an dasselbe Postfach
#: und zählen deshalb als eine Maßnahme.
DROSSEL_GRUPPE: dict[str, str] = {
    "nachfrage_lieferant": "lieferant",
    "nachfrage_spedition": "spedition",
    "status_spedition": "spedition",
}
#: Deutsche Bezeichnung je Maßnahme (Bericht, Erinnerung).
MASSNAHME_BEZEICHNUNG: dict[str, str] = {
    "nachfrage_lieferant": "Nachfrage Lieferant",
    "nachfrage_spedition": "Nachfrage Spedition",
    "status_spedition": "Statusanfrage Spedition",
    "erinnerung": "Erinnerung intern",
    "tracking": "Tracking",
}
#: Rangfolge der Ampel (höchster Wert gewinnt bei mehreren Befunden).
AMPEL_RANG: dict[str, int] = {"grau": 0, "gruen": 1, "gelb": 2, "rot": 3}
#: Speditionswerte, bei denen keine Speditionsbestätigung erwartet wird (normalisiert).
OHNE_SPEDITION: frozenset[str] = frozenset({"selbstabholung", "abholung", "keine", "ohne", "kunde holt ab"})
#: Standard-Dateiname des Regelwerks im Konfigurationsordner.
STANDARD_DATEINAME = "ueberwachung.yaml"

_ABGEHOLT_STATUS = (
    SendungStatus.ABGEHOLT,
    SendungStatus.UNTERWEGS,
    SendungStatus.ZUSTELLUNG_GEPLANT,
    SendungStatus.ZUGESTELLT,
)
_PROBLEM_STATUS = (SendungStatus.VERSPAETET, SendungStatus.PROBLEM)
_PLATZHALTER_RE = re.compile(r"\{(\w+)\}")


class RegelwerkFehler(ValueError):
    """``config/ueberwachung.yaml`` ist nicht lesbar oder enthält ungültige Werte."""


# ---------------------------------------------------------------------------
# Konfiguration (config/ueberwachung.yaml)
# ---------------------------------------------------------------------------


class Texte(BaseModel):
    """Begründungstexte der Ampel (Platzhalter in geschweiften Klammern, siehe ``config/ueberwachung.yaml``)."""

    zugestellt: str = "zugestellt laut Tracking ({quelle})"
    tracking_problem: str = "Tracking meldet '{status}' ({quelle})"
    ueberschritten_nicht_abgeholt: str = "Fixtermin {liefertag} überschritten, nicht abgeholt"
    ueberschritten_nicht_zugestellt: str = "Fixtermin {liefertag} überschritten, laut Tracking noch nicht zugestellt ({status})"
    vergangen_abgeholt: str = "abgeholt, Fixtermin {liefertag} vergangen (keine Zustellinformation)"
    erinnerung_heute: str = "Fixtermin heute ({liefertag})"
    erinnerung_morgen: str = "Fixtermin am nächsten Werktag ({liefertag})"
    verladung_ohne_abholung: str = "Verladedatum {verladedatum} erreicht, nicht als abgeholt markiert"
    lieferant_offen: str = "Bestätigung des Lieferanten fehlt (Frist {frist}, {werktage} Werktage vor Fixtermin)"
    lieferant_ueberfaellig: str = "Bestätigung des Lieferanten überfällig (Frist {frist} überschritten)"
    spedition_offen: str = "Bestätigung der Spedition fehlt (Frist {frist}, {werktage} Werktage vor Fixtermin)"
    spedition_ueberfaellig: str = "Bestätigung der Spedition überfällig (Frist {frist} überschritten)"
    noch_zu_frisch: str = "Auftrag vom {auftragsdatum}, Nachfrage frühestens nach {werktage} Werktag(en)"
    alles_bestaetigt: str = "Lieferant und Spedition haben bestätigt"
    verladung_geplant: str = "Verladung am {verladedatum}"
    abgeholt: str = "abgeholt"
    tracking_abgeholt: str = "laut Tracking {status} ({quelle}), Häkchen 'Abgeholt' fehlt"


class Regelwerk(BaseModel):
    """Feinheiten der Überwachung aus ``config/ueberwachung.yaml``; fehlt die Datei, gelten die Standardwerte."""

    mindest_alter_werktage: int = Field(default=1, ge=0)
    rot_unter_werktagen: int = Field(default=5, ge=0)
    erinnerung_werktage_vor_fixtermin: int = Field(default=1, ge=0)
    zuordnung_konfidenz_min: float = Field(default=0.5, ge=0.0, le=1.0)
    text_zuordnung: bool = True
    texte: Texte = Field(default_factory=Texte)
    quelle: str | None = None

    @classmethod
    def lade(cls, pfad: Path | str | None) -> Regelwerk:
        """Liest die YAML; fehlende Datei -> Standardwerte (Info im Log), kaputte Datei -> :class:`RegelwerkFehler`."""
        if pfad is None:
            return cls()
        pfad = Path(pfad)
        if not pfad.is_file():
            logger.info("Kein Regelwerk unter %s; Standardwerte der Überwachung gelten.", pfad)
            return cls()
        try:
            roh = yaml.safe_load(pfad.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise RegelwerkFehler(f"Regelwerk {pfad} nicht lesbar: {exc}") from exc
        if not isinstance(roh, dict):
            raise RegelwerkFehler(f"Regelwerk {pfad}: erwartet eine Zuordnung (Schlüssel: Wert), nicht {type(roh).__name__}.")
        daten = {k: v for k, v in roh.items() if k in cls.model_fields}
        unbekannt = sorted(k for k in roh if k not in cls.model_fields)
        if unbekannt:
            logger.warning("Regelwerk %s: unbekannte Schlüssel werden ignoriert: %s", pfad, ", ".join(unbekannt))
        daten["quelle"] = str(pfad)
        try:
            return cls.model_validate(daten)
        except ValidationError as exc:
            raise RegelwerkFehler(f"Regelwerk {pfad} ungültig: {exc}") from exc

    @classmethod
    def aus_settings(cls, settings: Settings, pfad: Path | str | None = None) -> Regelwerk:
        """Regelwerk laut Konfiguration (``pfad`` oder :func:`regelwerk_pfad`)."""
        return cls.lade(Path(pfad) if pfad is not None else regelwerk_pfad(settings))


def regelwerk_pfad(settings: Settings) -> Path:
    """``config/ueberwachung.yaml`` neben den übrigen Konfigurationsdateien (Ordner von ``kommunikation.anrede_datei``)."""
    return Path(settings.kommunikation.anrede_datei).parent / STANDARD_DATEINAME


# ---------------------------------------------------------------------------
# Ergebnis der Bewertung
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Massnahme:
    """Eine fällige Maßnahme mit Begründung."""

    typ: MassnahmeTyp
    begruendung: str

    @property
    def bezeichnung(self) -> str:
        return MASSNAHME_BEZEICHNUNG.get(self.typ, self.typ)


@dataclass
class Bewertung:
    """Ampel, Begründung und Maßnahmen eines Fixtermins; als Tupel ``(ampel, grund, massnahmen)`` entpackbar."""

    ampel: Ampel
    grund: str
    massnahmen: list[Massnahme] = field(default_factory=list)
    werktage_bis: int = 0
    befunde: list[tuple[Ampel, str]] = field(default_factory=list)

    def __iter__(self) -> Iterator[object]:
        yield self.ampel
        yield self.grund
        yield self.massnahmen

    def hat(self, typ: str) -> bool:
        return any(m.typ == typ for m in self.massnahmen)

    @property
    def typen(self) -> list[str]:
        return [m.typ for m in self.massnahmen]


def hoechste_ampel(ampeln: list[Ampel] | tuple[Ampel, ...]) -> Ampel:
    """Die Ampel mit dem höchsten Rang (rot > gelb > gruen > grau); leer -> ``"gruen"``."""
    if not ampeln:
        return "gruen"
    return max(ampeln, key=lambda a: AMPEL_RANG.get(a, 0))


# ---------------------------------------------------------------------------
# Tracking-Hilfen
# ---------------------------------------------------------------------------


def tracking_bekannt(sendung: Sendung | None) -> bool:
    """Hat die Sendung einen Stand aus einer echten Quelle (nicht ``keine``/``unbekannt``)?"""
    return sendung is not None and sendung.quelle != "keine" and sendung.status != SendungStatus.UNBEKANNT


def tracking_abgeholt(sendung: Sendung | None) -> bool:
    return tracking_bekannt(sendung) and sendung.status in _ABGEHOLT_STATUS  # type: ignore[union-attr]


def tracking_zugestellt(sendung: Sendung | None) -> bool:
    return tracking_bekannt(sendung) and sendung.status == SendungStatus.ZUGESTELLT  # type: ignore[union-attr]


def tracking_problem(sendung: Sendung | None) -> bool:
    return tracking_bekannt(sendung) and sendung.status in _PROBLEM_STATUS  # type: ignore[union-attr]


def _datum(wert: date | None) -> str:
    return wert.strftime("%d.%m.%Y") if wert is not None else "?"


def _text(vorlage: str, **werte: object) -> str:
    """Füllt die Platzhalter einer Textvorlage; unbekannte Platzhalter bleiben lesbar stehen."""

    def ersetze(treffer: re.Match[str]) -> str:
        name = treffer.group(1)
        return str(werte[name]) if name in werte else treffer.group(0)

    return _PLATZHALTER_RE.sub(ersetze, vorlage)


def _konfig(konfig: UeberwachungKonfig | Settings | None) -> UeberwachungKonfig:
    if konfig is None:
        return UeberwachungKonfig()
    if isinstance(konfig, Settings):
        return konfig.ueberwachung
    return konfig


def _ohne_spedition(fixtermin: Fixtermin) -> bool:
    """Selbstabholung o. ä.: keine Speditionsbestätigung erwartet."""
    return normalisiere(fixtermin.spedition) in OHNE_SPEDITION if fixtermin.spedition else False


def _alt_genug(fixtermin: Fixtermin, heute: date, regelwerk: Regelwerk, bundesland: str) -> bool:
    """Ist der Auftrag alt genug für eine Nachfrage? Ohne Auftragsdatum ja."""
    if fixtermin.auftragsdatum is None or regelwerk.mindest_alter_werktage <= 0:
        return True
    return kalender.werktage_zwischen(fixtermin.auftragsdatum, heute, bundesland) >= regelwerk.mindest_alter_werktage


# ---------------------------------------------------------------------------
# Bewertung
# ---------------------------------------------------------------------------


def bewerte(
    fixtermin: Fixtermin,
    sendung: Sendung | None,
    heute: date | datetime,
    *,
    konfig: UeberwachungKonfig | Settings | None = None,
    regelwerk: Regelwerk | None = None,
) -> Bewertung:
    """Beurteilt einen Fixtermin: ``(ampel, grund, massnahmen)``.

    ``heute`` ist der Stichtag des Laufs (für Tests fest vorgegeben). ``konfig`` sind die Schwellen aus
    ``settings.ueberwachung`` (oder die ``Settings`` selbst), ``regelwerk`` die Feinheiten aus
    ``config/ueberwachung.yaml``; beide dürfen fehlen (Standardwerte).
    """
    k = _konfig(konfig)
    r = regelwerk or Regelwerk()
    t = r.texte
    bl = k.bundesland or kalender.STANDARD_BUNDESLAND
    tag = heute.date() if isinstance(heute, datetime) else heute
    werktage_bis = kalender.werktage_zwischen(tag, fixtermin.liefertag, bl)
    vergangen = fixtermin.liefertag < tag
    quelle = sendung.quelle if sendung is not None else "keine"
    status = sendung.status.value if sendung is not None else SendungStatus.UNBEKANNT.value
    abgeholt = fixtermin.abgeholt or tracking_abgeholt(sendung)

    befunde: list[tuple[Ampel, str]] = []
    massnahmen: list[Massnahme] = []

    def befund(ampel: Ampel, grund: str, *neue: Massnahme) -> None:
        befunde.append((ampel, grund))
        for m in neue:
            if not any(v.typ == m.typ for v in massnahmen):
                massnahmen.append(m)

    # 1) Abgeschlossen: zugestellt laut Tracking
    if tracking_zugestellt(sendung):
        grund = _text(t.zugestellt, quelle=quelle, liefertag=_datum(fixtermin.liefertag))
        return Bewertung("grau", grund, [], werktage_bis, [("grau", grund)])

    # 2) Tracking meldet Verspätung/Problem
    if tracking_problem(sendung):
        grund = _text(t.tracking_problem, status=status, quelle=quelle)
        befund("rot", grund, Massnahme("status_spedition", grund), Massnahme("tracking", grund))

    # 3) Fixtermin überschritten
    if vergangen:
        if abgeholt:
            if tracking_bekannt(sendung):
                grund = _text(t.ueberschritten_nicht_zugestellt, liefertag=_datum(fixtermin.liefertag), status=status)
                befund("rot", grund, Massnahme("status_spedition", grund), Massnahme("tracking", grund))
            elif not befunde:
                grund = _text(t.vergangen_abgeholt, liefertag=_datum(fixtermin.liefertag))
                return Bewertung("grau", grund, [], werktage_bis, [("grau", grund)])
        else:
            grund = _text(t.ueberschritten_nicht_abgeholt, liefertag=_datum(fixtermin.liefertag))
            befund("rot", grund, Massnahme("status_spedition", grund), Massnahme("tracking", grund))

    # 4) Fixtermin heute / nächster Werktag
    if 0 <= werktage_bis <= r.erinnerung_werktage_vor_fixtermin:
        vorlage = t.erinnerung_heute if werktage_bis == 0 else t.erinnerung_morgen
        grund = _text(vorlage, liefertag=_datum(fixtermin.liefertag))
        befund("gruen" if abgeholt else "gelb", grund, Massnahme("erinnerung", grund))

    # 5) Verladedatum erreicht, nicht abgeholt
    if not vergangen and not abgeholt and fixtermin.verladedatum is not None and fixtermin.verladedatum <= tag:
        grund = _text(t.verladung_ohne_abholung, verladedatum=_datum(fixtermin.verladedatum))
        ampel: Ampel = "rot" if werktage_bis < r.rot_unter_werktagen else "gelb"
        befund(ampel, grund, Massnahme("status_spedition", grund), Massnahme("tracking", grund))

    # 6) Bestätigungen (nur solange die Ware nicht abgeholt ist)
    if not abgeholt:
        alt_genug = _alt_genug(fixtermin, tag, r, bl)
        dringend: Ampel = "rot" if werktage_bis < r.rot_unter_werktagen else "gelb"
        # Nachfragen erst ab der Schwelle (x Werktage vor dem Fixtermin, settings.ueberwachung.nachfrage_*_werktage);
        # vorher nur gelb ohne Entwurf (docs/AUSBAUSTUFE_2.md 2.6)
        if not fixtermin.lieferant_bestaetigt:
            frist = kalender.addiere_werktage(fixtermin.liefertag, -k.nachfrage_lieferant_werktage, bl)
            vorlage = t.lieferant_ueberfaellig if tag > frist else t.lieferant_offen
            grund = _text(vorlage, frist=_datum(frist), werktage=k.nachfrage_lieferant_werktage)
            if tag < frist:
                befund("gelb", grund)
            elif alt_genug:
                befund(dringend, grund, Massnahme("nachfrage_lieferant", grund))
            else:
                zusatz = _text(t.noch_zu_frisch, auftragsdatum=_datum(fixtermin.auftragsdatum), werktage=r.mindest_alter_werktage)
                befund("gelb", f"{grund}; {zusatz}")
        if not fixtermin.spedition_bestaetigt and not _ohne_spedition(fixtermin):
            frist = kalender.addiere_werktage(fixtermin.liefertag, -k.nachfrage_spedition_werktage, bl)
            vorlage = t.spedition_ueberfaellig if tag > frist else t.spedition_offen
            grund = _text(vorlage, frist=_datum(frist), werktage=k.nachfrage_spedition_werktage)
            if any(m.typ == "status_spedition" for m in massnahmen):
                befund(dringend, grund)  # die Statusanfrage deckt die Nachfrage mit ab
            elif tag < frist:
                befund("gelb", grund)
            elif alt_genug:
                befund(dringend, grund, Massnahme("nachfrage_spedition", grund))
            else:
                zusatz = _text(t.noch_zu_frisch, auftragsdatum=_datum(fixtermin.auftragsdatum), werktage=r.mindest_alter_werktage)
                befund("gelb", f"{grund}; {zusatz}")

    # 7) Hinweis: Tracking kennt die Abholung, das Häkchen fehlt
    if tracking_abgeholt(sendung) and not fixtermin.abgeholt:
        befund("gruen", _text(t.tracking_abgeholt, status=status, quelle=quelle))

    # 8) Nichts zu beanstanden
    if not befunde:
        if abgeholt:
            befund("gruen", t.abgeholt)
        else:
            grund = t.alles_bestaetigt
            if fixtermin.verladedatum is not None:
                grund = f"{grund}; {_text(t.verladung_geplant, verladedatum=_datum(fixtermin.verladedatum))}"
            befund("gruen", grund)

    ampel = hoechste_ampel([a for a, _ in befunde])
    gruende: list[str] = []
    for _, g in befunde:
        if g not in gruende:
            gruende.append(g)
    return Bewertung(ampel, "; ".join(gruende), massnahmen, werktage_bis, befunde)


# ---------------------------------------------------------------------------
# Drossel (Merkdatei)
# ---------------------------------------------------------------------------


class Drossel:
    """Merkt sich je Maßnahme und Vorgang das Datum der letzten Auslösung (JSON-Merkdatei).

    Schlüssel: ``<gruppe>|<vorgang>`` mit Gruppe laut :data:`DROSSEL_GRUPPE` (sonst der Typ) und
    Vorgang = Bestellnummer, sonst AB-Nummer, sonst Kundenbestellnummer, sonst
    ``<lieferant>|<kunde>|<liefertag>`` (normalisiert). ``pfad=None`` arbeitet nur im Speicher.
    """

    def __init__(
        self,
        pfad: Path | str | None,
        *,
        erneut_nach_werktagen: int = 2,
        bundesland: str = kalender.STANDARD_BUNDESLAND,
    ) -> None:
        self.pfad = Path(pfad) if pfad is not None else None
        self.erneut_nach_werktagen = max(0, erneut_nach_werktagen)
        self.bundesland = bundesland
        self._eintraege: dict[str, date] = {}
        self.lade()

    @classmethod
    def aus_settings(cls, settings: Settings, pfad: Path | str | None = None) -> Drossel:
        k = settings.ueberwachung
        return cls(pfad if pfad is not None else k.merkdatei, erneut_nach_werktagen=k.erneut_nach_werktagen, bundesland=k.bundesland)

    # -- Datei ----------------------------------------------------------------------------

    def lade(self) -> None:
        """Liest die Merkdatei; fehlt sie oder ist sie kaputt, startet die Drossel leer (Warnung)."""
        self._eintraege = {}
        if self.pfad is None or not self.pfad.is_file():
            return
        try:
            roh = json.loads(self.pfad.read_text(encoding="utf-8") or "{}")
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Merkdatei %s nicht lesbar (%s); Drossel startet leer.", self.pfad, exc)
            return
        if not isinstance(roh, dict):
            logger.warning("Merkdatei %s hat ein unerwartetes Format; Drossel startet leer.", self.pfad)
            return
        for schluessel, wert in roh.items():
            try:
                self._eintraege[str(schluessel)] = date.fromisoformat(str(wert)[:10])
            except ValueError:
                logger.warning("Merkdatei %s: Eintrag %r mit ungültigem Datum %r übersprungen.", self.pfad, schluessel, wert)

    def speichere(self) -> Path | None:
        """Schreibt die Merkdatei atomar (Temp-Datei + ``os.replace``); ohne Pfad passiert nichts."""
        if self.pfad is None:
            return None
        self.pfad.parent.mkdir(parents=True, exist_ok=True)
        daten = {k: v.isoformat() for k, v in sorted(self._eintraege.items())}
        fd, tmp = tempfile.mkstemp(prefix=".nachfragen_", suffix=".tmp", dir=str(self.pfad.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(daten, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.pfad)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return self.pfad

    # -- Schlüssel ---------------------------------------------------------------------------

    @staticmethod
    def vorgang(fixtermin: Fixtermin) -> str:
        """Kennung des Vorgangs: Bestellnummer, AB-Nummer, Kundenbestellnummer, sonst Lieferant|Kunde|Liefertag."""
        for nummer in (fixtermin.bestellnummer, fixtermin.ab_nummer, fixtermin.kundenbestellnummer):
            if nummer and nummer.strip():
                return re.sub(r"\s+", "", nummer.strip().upper())
        return f"{normalisiere(fixtermin.lieferant_kurz)}|{normalisiere(fixtermin.kunde_kurz)}|{fixtermin.liefertag.isoformat()}"

    @classmethod
    def schluessel(cls, typ: str, fixtermin: Fixtermin) -> str:
        return f"{DROSSEL_GRUPPE.get(typ, typ)}|{cls.vorgang(fixtermin)}"

    # -- Abfrage ------------------------------------------------------------------------------

    def letzte(self, typ: str, fixtermin: Fixtermin) -> date | None:
        return self._eintraege.get(self.schluessel(typ, fixtermin))

    def erlaubt(self, typ: str, fixtermin: Fixtermin, heute: date) -> bool:
        """Darf die Maßnahme heute ausgelöst werden? Nicht gedrosselte Typen immer."""
        if typ not in GEDROSSELTE_MASSNAHMEN:
            return True
        letzte = self.letzte(typ, fixtermin)
        if letzte is None:
            return True
        return kalender.werktage_zwischen(letzte, heute, self.bundesland) >= self.erneut_nach_werktagen

    def merke(self, typ: str, fixtermin: Fixtermin, heute: date) -> None:
        if typ in GEDROSSELTE_MASSNAHMEN:
            self._eintraege[self.schluessel(typ, fixtermin)] = heute

    def vergiss(self, typ: str, fixtermin: Fixtermin) -> bool:
        """Nimmt einen Merkeintrag zurück (z. B. wenn der Entwurf nicht gespeichert werden konnte)."""
        return self._eintraege.pop(self.schluessel(typ, fixtermin), None) is not None

    def __len__(self) -> int:
        return len(self._eintraege)

    @property
    def eintraege(self) -> dict[str, date]:
        return dict(self._eintraege)
