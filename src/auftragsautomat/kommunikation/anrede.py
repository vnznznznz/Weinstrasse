"""Anrede und Grußformel im Hausstil (``config/anrede.yaml``).

* :func:`baue_anrede` – ``"Hallo Frau Beispiel!"`` (deutsch) bzw. ``"Dear Ms Example,"`` (englisch);
  Geschlecht und Namensteile liefert :func:`auftragsautomat.kontakte.zuordnung.person_aus_name`
  (Kontaktliste), feste Regeln je E-Mail-Adresse und eine Du-Liste kommen aus der YAML.
* :func:`grussformel` – Grußformel je Sprache, optional je Firma abweichend.
* :func:`lade_anrede_regeln` – liest ``config/anrede.yaml``; fehlt die Datei, gelten die eingebauten
  Standardformen (identisch mit der ausgelieferten Datei).
* :func:`anrede_regeln_fuer_zuordner` – die festen Regeln im Format, das
  :class:`auftragsautomat.kontakte.Zuordner` als ``anrede_regeln`` erwartet.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

from auftragsautomat.kontakte.zuordnung import Person, person_aus_name
from auftragsautomat.models import Kontakt

logger = logging.getLogger(__name__)

__all__ = [
    "AnredeFormen",
    "AnredeRegel",
    "AnredeRegeln",
    "FirmenAnrede",
    "Sprache",
    "anrede_regeln_fuer_zuordner",
    "baue_anrede",
    "grussformel",
    "lade_anrede_regeln",
    "sprache_normalisiert",
]

Sprache = Literal["de", "en"]

_PLATZHALTER = re.compile(r"\{(name|vorname|nachname|firma)\}")


class AnredeRegel(BaseModel):
    anrede: str | None = None
    sprache: Sprache | None = None


class AnredeFormen(BaseModel):
    """Anredezeilen einer Sprache mit Platzhaltern ``{name}``, ``{vorname}``, ``{nachname}``, ``{firma}``."""

    frau: str
    herr: str
    name: str  # Geschlecht unbekannt, Name bekannt
    du: str  # Adressen der Du-Liste
    neutral: str  # Postfach / Abteilung / kein Kontakt


class FirmenAnrede(BaseModel):
    grussformel: dict[str, str] = Field(default_factory=dict)


def _standard_formen() -> dict[str, AnredeFormen]:
    return {
        "de": AnredeFormen(
            frau="Hallo Frau {nachname}!",
            herr="Hallo Herr {nachname}!",
            name="Hallo {name}!",
            du="Hallo {vorname}!",
            neutral="Guten Tag!",
        ),
        "en": AnredeFormen(
            frau="Dear Ms {nachname},",
            herr="Dear Mr {nachname},",
            name="Hello {name},",
            du="Hello {vorname},",
            neutral="Hello,",
        ),
    }


class AnredeRegeln(BaseModel):
    """Inhalt von ``config/anrede.yaml``."""

    anrede: dict[str, AnredeFormen] = Field(default_factory=_standard_formen)
    grussformel: dict[str, str] = Field(default_factory=lambda: {"de": "Freundliche Grüße", "en": "Thank you and best regards"})
    firmen: dict[str, FirmenAnrede] = Field(default_factory=dict)
    du: list[str] = Field(default_factory=list)
    regeln: dict[str, AnredeRegel] = Field(default_factory=dict)
    quelle: Path | None = None

    def formen(self, sprache: str) -> AnredeFormen:
        """Formen der Sprache; unbekannte Sprache -> ``de``, fehlt auch die -> eingebaute Standardformen."""
        sprache = sprache_normalisiert(sprache)
        if sprache in self.anrede:
            return self.anrede[sprache]
        if "de" in self.anrede:
            return self.anrede["de"]
        return _standard_formen()[sprache]

    def regel_fuer(self, email: str | None) -> AnredeRegel | None:
        if not email:
            return None
        return self.regeln.get(email.strip().lower())

    def ist_du(self, email: str | None) -> bool:
        return bool(email) and email.strip().lower() in {d.strip().lower() for d in self.du}


_STANDARD_REGELN = AnredeRegeln()


def sprache_normalisiert(sprache: str | None) -> Sprache:
    """``"en"``/``"EN"``/``"english"`` -> ``"en"``, alles andere -> ``"de"``."""
    s = (sprache or "").strip().lower()
    return "en" if s.startswith("en") else "de"


def lade_anrede_regeln(pfad: Path | None) -> AnredeRegeln:
    """Lädt ``config/anrede.yaml``; fehlt die Datei, gelten die Standardformen (Info im Log).
    Eine ungültige Datei ergibt ``ValueError`` (Konfigurationsfehler, kein stilles Raten)."""
    if pfad is None or not Path(pfad).is_file():
        logger.info("Anrede-Datei %s fehlt – eingebaute Standardformen werden verwendet.", pfad)
        return AnredeRegeln()
    try:
        roh = yaml.safe_load(Path(pfad).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"Anrede-Datei {pfad} ist nicht lesbar: {exc}") from exc
    if not isinstance(roh, dict):
        raise ValueError(f"Anrede-Datei {pfad} muss ein Mapping sein.")
    regeln_roh = roh.get("regeln") or {}
    if not isinstance(regeln_roh, dict):
        raise ValueError(f"Anrede-Datei {pfad}: 'regeln' muss ein Mapping E-Mail -> Regel sein.")
    roh = {**roh, "regeln": {str(k).strip().lower(): v or {} for k, v in regeln_roh.items()}}
    try:
        regeln = AnredeRegeln.model_validate(roh)
    except ValueError as exc:
        raise ValueError(f"Anrede-Datei {pfad} ist ungültig: {exc}") from exc
    regeln.quelle = Path(pfad)
    return regeln


def _fuelle(vorlage: str, felder: dict[str, str]) -> str:
    """Ersetzt nur die bekannten Platzhalter (Namen mit geschweiften Klammern bleiben unversehrt)."""
    text = _PLATZHALTER.sub(lambda m: felder.get(m.group(1), ""), vorlage)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def _mit_satzzeichen(text: str, sprache: Sprache) -> str:
    text = text.strip()
    if not text or text[-1] in ",!.:;":
        return text
    return text + ("," if sprache == "en" else "!")


def _felder(person: Person, kontakt: Kontakt) -> dict[str, str]:
    voller_name = person.voller_name or (kontakt.name or "").strip()
    nachname = person.nachname or voller_name
    return {
        "name": voller_name,
        "vorname": person.vorname or voller_name,
        "nachname": nachname,
        "firma": kontakt.firma or "",
    }


def baue_anrede(kontakt: Kontakt | None, sprache: str, *, regeln: AnredeRegeln | None = None) -> str:
    """Anredezeile mit Satzzeichen im Hausstil.

    Reihenfolge: feste Regel je E-Mail-Adresse (``regeln.regeln``) > Postfach/Abteilung oder kein
    Kontakt -> neutrale Form > Du-Liste > Frau/Herr nach Geschlecht (``Kontakt.anrede``, Namenspräfix,
    Vornamensliste) > Name ohne Geschlecht. Beispiele: ``"Hallo Frau Beispiel!"``, ``"Dear Ms Example,"``,
    ``"Guten Tag!"``, ``"Hello,"``.
    """
    regeln = regeln or _STANDARD_REGELN
    sp = sprache_normalisiert(sprache)
    formen = regeln.formen(sp)
    if kontakt is None:
        return formen.neutral
    regel = regeln.regel_fuer(kontakt.email)
    if regel is not None and regel.anrede:
        return _mit_satzzeichen(regel.anrede, sprache_normalisiert(regel.sprache or sp))
    person = person_aus_name(kontakt.name, anrede=kontakt.anrede)
    if not person.ist_person:
        return formen.neutral
    felder = _felder(person, kontakt)
    if regeln.ist_du(kontakt.email) and person.vorname:
        return _fuelle(formen.du, felder)
    if person.geschlecht == "w":
        return _fuelle(formen.frau, felder)
    if person.geschlecht == "m":
        return _fuelle(formen.herr, felder)
    return _fuelle(formen.name, felder)


def grussformel(sprache: str, firma_id: str | None = None, *, regeln: AnredeRegeln | None = None) -> str:
    """Grußformel je Sprache; ``regeln.firmen[firma_id].grussformel[sprache]`` hat Vorrang."""
    regeln = regeln or _STANDARD_REGELN
    sp = sprache_normalisiert(sprache)
    if firma_id and firma_id in regeln.firmen:
        eigene = regeln.firmen[firma_id].grussformel.get(sp)
        if eigene:
            return eigene.strip()
    return (regeln.grussformel.get(sp) or _STANDARD_REGELN.grussformel[sp]).strip()


def anrede_regeln_fuer_zuordner(regeln: AnredeRegeln | None) -> dict[str, dict[str, str]]:
    """Feste Regeln (E-Mail -> ``{"anrede": …, "sprache": …}``) für ``Zuordner(anrede_regeln=…)``."""
    if regeln is None:
        return {}
    ergebnis: dict[str, dict[str, str]] = {}
    for email, regel in regeln.regeln.items():
        eintrag = {k: v for k, v in (("anrede", regel.anrede), ("sprache", regel.sprache)) if v}
        if eintrag:
            ergebnis[email.strip().lower()] = eintrag
    return ergebnis
