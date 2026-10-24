"""Firmenwahl nach Katalog (Kundengruppen) mit Gedächtnis je Kunde + Lieferant.

Fachlicher Hintergrund (docs/AUSBAUSTUFE_2.md, Abschnitt 8; docs/ARCHITEKTUR.md, Geschäftsregel 17):

* **Fachhandel** -> Weinstrasse Agentur **oder** WIK FH & SEH, je nach Lieferant (Abschnitt
  ``lieferanten`` in ``config/firmenkatalog.yaml``).
* **Eigenständige Lebensmittel-Einzelhändler** (eigene Kaufleute) -> WIK FH & SEH.
* **Einkaufsregionen**, **große Ketten**, **Großkunden** -> WIK LEH **oder** WOE -> das Tool fragt in
  der Freigabe und merkt sich die Antwort je Kunde + Lieferant (``config/firmen_gedaechtnis.yaml``).

Drei Bausteine:

:class:`Firmenkatalog`
    Liest ``config/firmenkatalog.yaml`` (fehlende oder leere Datei -> leerer Katalog mit Warnung, nie
    eine Exception). :meth:`Firmenkatalog.klassifiziere` ordnet einen Kundentext (plus Adresse,
    Kundennummer) einer :class:`~auftragsautomat.models.Kundengruppe` zu: die ``muster`` der Gruppen
    werden normalisiert (:func:`~auftragsautomat.matching.aliase.normalisiere_name`, Rechtsformen
    bleiben erhalten, damit "e.K." oder "Markt GmbH" greifen) und als ganze Wortfolge im Kundentext
    und in der Adresse gesucht. Passen mehrere Gruppen, gewinnt das längste Muster (dann die
    Reihenfolge in der Datei). Die Firmen einer Gruppe kommen aus der Datei; fehlen sie, aus
    ``Firma.kundengruppen`` in ``config/firmen.yaml`` (Rückfall). Nur in ``settings.firmen`` bekannte
    Firmen zählen, unbekannte IDs werden mit Warnung verworfen.
    Ergebnis :class:`Klassifikation` mit ``entscheidung``:

    * ``"eindeutig"`` – genau eine Firma (``firmen[0]``),
    * ``"lieferant"`` – die Gruppe entscheidet nach Lieferant, aber kein Lieferantentext bekannt;
      der Aufrufer versucht :meth:`Firmenkatalog.firma_fuer_lieferant` mit dem, was er weiß,
    * ``"fragen"`` – mehrere Firmen möglich (Region/Kette/Großkunde, oder Fachhandelslieferant nicht
      im Katalog): der Anwender wählt, die Antwort wird gemerkt,
    * ``"unbekannt"`` – kein Muster passt (oder der Gruppe ist keine Firma zugeordnet).

:class:`Firmengedaechtnis`
    YAML-Liste von :class:`~auftragsautomat.models.Firmenregel` (fehlende Datei -> leer).
    :meth:`Firmengedaechtnis.finde` sucht erst Kunde + Lieferant, dann Kunde allgemein
    (``lieferant_kurz`` leer); Schlüssel werden mit ``normalisiere_name`` verglichen. Zusätzlich gibt
    es eine tolerante Stufe, bei der der gelernte Kurzname (meist der Lexware-Matchcode, z. B.
    "Menrath") als ganzes Wort im Kundentext des Dokuments ("Weinhaus Menrath") vorkommen darf –
    sonst wäre das Gedächtnis beim nächsten Dokument wertlos, denn vor dem Stammdaten-Matching
    kennt die Firmen-Ermittlung nur den Text aus dem Dokument. :meth:`Firmengedaechtnis.merke`
    ersetzt eine Regel mit gleichem Schlüssel; :meth:`Firmengedaechtnis.speichere` schreibt atomar
    (temporäre Datei + ``os.replace``, UTF-8, sortiert).

:func:`merke_firma`
    Aus der Freigabe: schreibt die gewählte Firma als Regel, wenn ``paket.firma_merken`` gesetzt ist,
    eine Firma feststeht und ``settings.firmenwahl.merken`` an ist. ``kunde_kurz`` = Matchcode oder
    Name des zugeordneten Kunden, sonst der Kundentext des Dokuments; ``lieferant_kurz`` analog aus
    dem Lieferanten (``None``, wenn unbekannt). Nie eine Exception wegen Schreibproblemen: dann
    ``None`` mit Fehlermeldung im Log.

Die Einbettung in die Reihenfolge der Firmen-Ermittlung (Eingangsordner -> Gedächtnis ->
Kunden-Alias -> Katalog -> Stammdaten -> Empfänger) steht in :mod:`auftragsautomat.matching.firma`.
"""

from __future__ import annotations

import logging
import os
import tempfile
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

import yaml
from pydantic import ValidationError

from auftragsautomat.config import Settings
from auftragsautomat.matching.aliase import enthaelt_als_wort, normalisiere_name
from auftragsautomat.models import Belegpaket, Firmenregel, Kundengruppe

__all__ = [
    "ENTSCHEIDUNGEN",
    "Firmengedaechtnis",
    "Firmenkatalog",
    "Gruppenregel",
    "Klassifikation",
    "merke_firma",
]

logger = logging.getLogger(__name__)

Entscheidung = Literal["eindeutig", "lieferant", "fragen", "unbekannt"]
#: Erlaubte Werte für ``entscheidung`` je Kundengruppe in der Katalogdatei.
ENTSCHEIDUNGEN: tuple[str, ...] = ("eindeutig", "lieferant", "fragen")

#: Kürzere gelernte Kurznamen werden in der toleranten Suche nicht als Wort im Kundentext gesucht
#: (zu viele Zufallstreffer); exakte Treffer gelten immer.
_MIN_LAENGE_TOLERANT = 3

#: Kundengruppen, bei denen der Anwender die Firma entscheidet (Geschäftsregel 17). Für sie wird der gelernte
#: Matchcode (bei Ketten oft nur der Kettenname, z. B. "EDEKA") nicht tolerant verglichen, sonst ginge eine
#: Antwort für eine Einkaufsregion auf alle Regionen derselben Kette über.
FRAGEN_GRUPPEN: frozenset[Kundengruppe] = frozenset({Kundengruppe.REGION, Kundengruppe.KETTE, Kundengruppe.GROSSKUNDE})

_KOPF_GEDAECHTNIS = (
    "# Firmen-Gedächtnis des Auftrags-Automaten: gelernte Zuordnung Kunde (+ Lieferant) -> Firma.\n"
    "# Wird vom Tool geschrieben (Freigabe: Häkchen \"für diesen Kunden und Lieferanten merken\").\n"
    "# Einträge dürfen von Hand geändert oder gelöscht werden; Schlüssel werden normalisiert\n"
    "# verglichen (Kleinschreibung, Umlaute, Satzzeichen egal). lieferant_kurz leer = alle Lieferanten.\n"
)


def _norm_muster(text: str | None) -> str:
    """Normalisierung für Katalogmuster und Kundentexte: Rechtsformen bleiben erhalten."""
    return normalisiere_name(text, rechtsformen_entfernen=False)


def _als_textliste(wert: object) -> list[str]:
    if wert is None:
        return []
    if isinstance(wert, str):
        return [wert] if wert.strip() else []
    if isinstance(wert, (list, tuple)):
        return [str(w).strip() for w in wert if w is not None and str(w).strip()]
    return [str(wert).strip()] if str(wert).strip() else []


def _lies_yaml(pfad: Path, bezeichnung: str) -> object | None:
    """YAML-Datei lesen; fehlend/leer/kaputt -> ``None`` mit Log (nie eine Exception)."""
    if not pfad.exists():
        logger.warning("%s %s fehlt; es wird ohne gearbeitet", bezeichnung, pfad)
        return None
    try:
        daten = yaml.safe_load(pfad.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        logger.error("%s %s ist nicht lesbar (%s); es wird ohne gearbeitet", bezeichnung, pfad, exc)
        return None
    if daten is None:
        logger.warning("%s %s ist leer", bezeichnung, pfad)
    return daten


# ---------------------------------------------------------------------------
# Katalog
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Klassifikation:
    """Ergebnis von :meth:`Firmenkatalog.klassifiziere`."""

    kundengruppe: Kundengruppe
    firmen: list[str]
    entscheidung: Entscheidung
    grund: str

    @property
    def firma(self) -> str | None:
        """Die Firma bei ``entscheidung == "eindeutig"``, sonst ``None``."""
        return self.firmen[0] if self.entscheidung == "eindeutig" and self.firmen else None


@dataclass(frozen=True)
class Gruppenregel:
    """Eine Kundengruppe aus der Katalogdatei."""

    gruppe: Kundengruppe
    firmen: tuple[str, ...]
    entscheidung: str  # "eindeutig" | "lieferant" | "fragen"
    muster: tuple[tuple[str, str], ...] = ()  # (wie in der Datei, normalisiert)
    kundennummern: tuple[str, ...] = ()
    beschreibung: str | None = None

    def treffer(self, text_norm: str, kundennummer: str | None) -> tuple[str, int] | None:
        """(getroffenes Muster, Länge der Normalform) oder ``None``; Kundennummer zählt als längster Treffer."""
        if kundennummer and kundennummer.strip() in self.kundennummern:
            return f"Kundennummer {kundennummer.strip()}", 10_000
        bester: tuple[str, int] | None = None
        for original, norm in self.muster:
            if norm and enthaelt_als_wort(text_norm, norm) and (bester is None or len(norm) > bester[1]):
                bester = (original, len(norm))
        return bester


def _firmen_fuer_gruppe(gruppe: Kundengruppe, settings: Settings) -> list[str]:
    """Rückfall: alle Firmen, deren ``Firma.kundengruppen`` die Gruppe nennen (Reihenfolge wie firmen.yaml)."""
    return [firma.id for firma in settings.firmen.values() if gruppe.value in firma.kundengruppen]


def _bekannte_firmen(firmen: Iterable[str], settings: Settings | None, woher: str) -> list[str]:
    bekannt: list[str] = []
    for firma_id in firmen:
        firma_id = str(firma_id).strip()
        if not firma_id or firma_id in bekannt:
            continue
        if settings is not None and firma_id not in settings.firmen:
            logger.warning("Firmenkatalog: %s verweist auf unbekannte Firma %r (nicht in firmen.yaml)", woher, firma_id)
            continue
        bekannt.append(firma_id)
    return bekannt


class Firmenkatalog:
    """Kundengruppen mit Mustern und Firmen sowie Fachhandelslieferanten -> Firma (siehe Modul-Docstring)."""

    def __init__(
        self,
        gruppen: Iterable[Gruppenregel] = (),
        lieferanten: Mapping[str, str] | None = None,
        *,
        pfad: Path | None = None,
    ) -> None:
        self.gruppen: list[Gruppenregel] = list(gruppen)
        self.lieferanten: dict[str, str] = dict(lieferanten or {})
        self.pfad = pfad
        self._lieferanten_norm: list[tuple[str, str, str]] = sorted(
            (
                (normalisiere_name(name), name, firma_id)
                for name, firma_id in self.lieferanten.items()
                if normalisiere_name(name)
            ),
            key=lambda e: -len(e[0]),
        )

    def __repr__(self) -> str:
        return f"Firmenkatalog({len(self.gruppen)} Gruppen, {len(self.lieferanten)} Lieferanten, {self.pfad})"

    @property
    def leer(self) -> bool:
        return not self.gruppen and not self.lieferanten

    def gruppe(self, kundengruppe: Kundengruppe | str) -> Gruppenregel | None:
        wert = kundengruppe.value if isinstance(kundengruppe, Kundengruppe) else str(kundengruppe)
        return next((g for g in self.gruppen if g.gruppe.value == wert), None)

    # -- laden -------------------------------------------------------------

    @classmethod
    def aus_settings(cls, settings: Settings) -> Firmenkatalog:
        """Katalog aus ``settings.firmenwahl.katalog_datei`` (Firmen gegen ``settings.firmen`` geprüft)."""
        return cls.lade(Path(settings.firmenwahl.katalog_datei), settings)

    @classmethod
    def lade(cls, pfad: Path | str, settings: Settings | None = None) -> Firmenkatalog:
        """Liest die Katalogdatei. Fehlende, leere oder kaputte Datei -> leerer Katalog mit Warnung.

        Mit ``settings`` werden die Firmen-IDs gegen ``settings.firmen`` geprüft (unbekannte mit Warnung
        verworfen) und fehlende ``firmen`` einer Gruppe aus ``Firma.kundengruppen`` ergänzt.
        """
        pfad = Path(pfad)
        daten = _lies_yaml(pfad, "Firmenkatalog")
        if not isinstance(daten, dict):
            if daten is not None:
                logger.warning("Firmenkatalog %s hat kein Abbildungs-Format (erwartet kundengruppen/lieferanten)", pfad)
            return cls(pfad=pfad)

        gruppen = cls._gruppen_aus(daten.get("kundengruppen"), settings, pfad)
        lieferanten = cls._lieferanten_aus(daten.get("lieferanten"), settings, pfad)
        katalog = cls(gruppen, lieferanten, pfad=pfad)
        logger.info(
            "Firmenkatalog %s geladen: %d Kundengruppen, %d Fachhandelslieferanten",
            pfad,
            len(katalog.gruppen),
            len(katalog.lieferanten),
        )
        return katalog

    @staticmethod
    def _gruppen_aus(roh: object, settings: Settings | None, pfad: Path) -> list[Gruppenregel]:
        if roh is None:
            logger.warning("Firmenkatalog %s: Abschnitt 'kundengruppen' fehlt", pfad)
            return []
        if not isinstance(roh, dict):
            logger.warning("Firmenkatalog %s: 'kundengruppen' muss eine Abbildung Gruppe -> Angaben sein", pfad)
            return []
        gruppen: list[Gruppenregel] = []
        for name, angaben in roh.items():
            try:
                gruppe = Kundengruppe(str(name).strip().lower())
            except ValueError:
                logger.warning(
                    "Firmenkatalog %s: unbekannte Kundengruppe %r (erlaubt: %s)",
                    pfad,
                    name,
                    ", ".join(g.value for g in Kundengruppe if g is not Kundengruppe.UNBEKANNT),
                )
                continue
            if gruppe is Kundengruppe.UNBEKANNT:
                logger.warning("Firmenkatalog %s: Gruppe 'unbekannt' ist reserviert und wird übergangen", pfad)
                continue
            angaben = angaben if isinstance(angaben, dict) else {}
            firmen = _bekannte_firmen(_als_textliste(angaben.get("firmen")), settings, f"Gruppe '{gruppe.value}'")
            if not firmen and settings is not None:
                firmen = _firmen_fuer_gruppe(gruppe, settings)
                if firmen:
                    logger.debug(
                        "Firmenkatalog: Firmen für Gruppe %s aus firmen.yaml (kundengruppen): %s",
                        gruppe.value,
                        ", ".join(firmen),
                    )
            if not firmen:
                logger.warning("Firmenkatalog %s: Gruppe '%s' hat keine (bekannte) Firma", pfad, gruppe.value)

            entscheidung = str(angaben.get("entscheidung") or "").strip().lower()
            if entscheidung and entscheidung not in ENTSCHEIDUNGEN:
                logger.warning(
                    "Firmenkatalog %s: Gruppe '%s' hat unbekannte entscheidung %r (erlaubt: %s)",
                    pfad,
                    gruppe.value,
                    entscheidung,
                    ", ".join(ENTSCHEIDUNGEN),
                )
                entscheidung = ""
            if not entscheidung:
                entscheidung = "eindeutig" if len(firmen) <= 1 else "fragen"
            if entscheidung == "eindeutig" and len(firmen) > 1:
                logger.warning(
                    "Firmenkatalog %s: Gruppe '%s' ist 'eindeutig', nennt aber mehrere Firmen (%s); das Tool fragt",
                    pfad,
                    gruppe.value,
                    ", ".join(firmen),
                )
                entscheidung = "fragen"

            muster = tuple(
                (m, _norm_muster(m)) for m in _als_textliste(angaben.get("muster")) if _norm_muster(m)
            )
            kundennummern = tuple(_als_textliste(angaben.get("kundennummern")))
            beschreibung = angaben.get("beschreibung")
            gruppen.append(
                Gruppenregel(
                    gruppe=gruppe,
                    firmen=tuple(firmen),
                    entscheidung=entscheidung,
                    muster=muster,
                    kundennummern=kundennummern,
                    beschreibung=str(beschreibung) if beschreibung else None,
                )
            )
        return gruppen

    @staticmethod
    def _lieferanten_aus(roh: object, settings: Settings | None, pfad: Path) -> dict[str, str]:
        if roh is None:
            return {}
        if not isinstance(roh, dict):
            logger.warning("Firmenkatalog %s: 'lieferanten' muss eine Abbildung Lieferant -> Firma sein", pfad)
            return {}
        lieferanten: dict[str, str] = {}
        for name, firma_id in roh.items():
            name_text = str(name).strip() if name is not None else ""
            if not name_text or not normalisiere_name(name_text):
                logger.warning("Firmenkatalog %s: Lieferant ohne Namen übergangen", pfad)
                continue
            bekannt = _bekannte_firmen([str(firma_id or "")], settings, f"Lieferant '{name_text}'")
            if not bekannt:
                continue
            lieferanten[name_text] = bekannt[0]
        return lieferanten

    # -- klassifizieren ----------------------------------------------------

    def firma_fuer_lieferant(self, lieferant_text: str | None) -> str | None:
        """Firma laut Abschnitt ``lieferanten`` (Fachhandel): der Katalogname kommt alias-normalisiert
        als ganze Wortfolge im Lieferantentext vor (wie Alias-Treffer); längster Name gewinnt.
        ``None``, wenn nichts passt."""
        text_norm = normalisiere_name(lieferant_text)
        if not text_norm:
            return None
        # TODO-PRUEFEN: Der Abschnitt "lieferanten" in config/firmenkatalog.yaml ist noch leer; bis der
        # Anwender die Fachhandelslieferanten der Agentur bzw. von WIK FH & SEH einträgt, fragt das Tool
        # bei jedem Fachhandelskunden einmal je Lieferant und merkt sich die Antwort.
        for name_norm, name, firma_id in self._lieferanten_norm:
            if name_norm == text_norm or enthaelt_als_wort(text_norm, name_norm):
                logger.debug("Firmenkatalog: Lieferant %r -> %s (Eintrag %r)", lieferant_text, firma_id, name)
                return firma_id
        return None

    def klassifiziere(
        self,
        kunde_text: str | None,
        adresse: str | None = None,
        kundennummer: str | None = None,
        *,
        lieferant_text: str | None = None,
        firma_kandidaten: Iterable[str] | None = None,
    ) -> Klassifikation:
        """Kundengruppe und mögliche Firmen zu einem Kundentext (siehe Modul-Docstring).

        ``firma_kandidaten`` (z. B. aus einem mehrdeutigen Kunden-Alias) schränken die Firmen der Gruppe
        ein, sofern die Schnittmenge nicht leer ist.
        """
        text_norm = " ".join(t for t in (_norm_muster(kunde_text), _norm_muster(adresse)) if t)
        anzeige = (kunde_text or "").strip() or next(iter((adresse or "").strip().splitlines()), "") or "-"

        bester: tuple[Gruppenregel, str, int] | None = None
        weitere: list[str] = []
        for regel in self.gruppen:
            treffer = regel.treffer(text_norm, kundennummer)
            if treffer is None:
                continue
            if bester is None or treffer[1] > bester[2]:
                if bester is not None:
                    weitere.append(f"{bester[0].gruppe.value} ('{bester[1]}')")
                bester = (regel, treffer[0], treffer[1])
            else:
                weitere.append(f"{regel.gruppe.value} ('{treffer[0]}')")

        if bester is None:
            grund = f"kein Katalogmuster passt auf '{anzeige}'"
            logger.debug("Firmenkatalog: %s", grund)
            return Klassifikation(Kundengruppe.UNBEKANNT, [], "unbekannt", grund)

        regel, muster, _ = bester
        grund = f"Kundengruppe {regel.gruppe.value}: Muster '{muster}' in '{anzeige}'"
        if weitere:
            grund += f" (weitere Treffer: {', '.join(weitere)})"

        firmen = list(regel.firmen)
        if firma_kandidaten is not None:
            kandidaten = [k for k in firma_kandidaten if k in firmen]
            if kandidaten and len(kandidaten) < len(firmen):
                grund += f"; eingeschränkt auf {', '.join(kandidaten)}"
                firmen = kandidaten
        if not firmen:
            grund += "; der Gruppe ist keine Firma zugeordnet"
            logger.warning("Firmenkatalog: %s", grund)
            return Klassifikation(regel.gruppe, [], "unbekannt", grund)
        if len(firmen) == 1:
            logger.info("Firmenkatalog: %s -> %s", grund, firmen[0])
            return Klassifikation(regel.gruppe, firmen, "eindeutig", grund)

        if regel.entscheidung == "lieferant":
            if not normalisiere_name(lieferant_text):
                grund += "; Firma hängt vom Lieferanten ab, Lieferant im Dokument unbekannt"
                logger.info("Firmenkatalog: %s", grund)
                return Klassifikation(regel.gruppe, firmen, "lieferant", grund)
            firma = self.firma_fuer_lieferant(lieferant_text)
            if firma is not None and firma in firmen:
                grund += f"; Lieferant '{lieferant_text}' -> {firma}"
                logger.info("Firmenkatalog: %s", grund)
                return Klassifikation(regel.gruppe, [firma], "eindeutig", grund)
            if firma is not None:
                grund += f"; Lieferant '{lieferant_text}' zeigt auf {firma}, das gehört nicht zur Gruppe"
            else:
                grund += f"; Lieferant '{lieferant_text}' nicht im Abschnitt 'lieferanten' des Katalogs"
            logger.info("Firmenkatalog: %s -> fragen", grund)
            return Klassifikation(regel.gruppe, firmen, "fragen", grund)

        grund += f"; mehrere Firmen möglich ({', '.join(firmen)})"
        logger.info("Firmenkatalog: %s -> fragen", grund)
        return Klassifikation(regel.gruppe, firmen, "fragen", grund)


# ---------------------------------------------------------------------------
# Gedächtnis
# ---------------------------------------------------------------------------


def _schluessel(text: str | None) -> str:
    return normalisiere_name(text)


def _lieferant_schluessel(text: str | None) -> str | None:
    norm = normalisiere_name(text)
    return norm or None


def _regel_sortierung(regel: Firmenregel) -> tuple[str, str]:
    return (_schluessel(regel.kunde_kurz), _lieferant_schluessel(regel.lieferant_kurz) or "")


class Firmengedaechtnis:
    """Gelernte Regeln Kunde (+ Lieferant) -> Firma (siehe Modul-Docstring)."""

    def __init__(self, regeln: Iterable[Firmenregel] = (), *, pfad: Path | None = None, tolerant: bool = True) -> None:
        self.pfad = pfad
        self.tolerant = tolerant  # False: nur exakte Schlüssel (Einstellung firmenwahl.gedaechtnis_tolerant)
        self._regeln: dict[tuple[str, str | None], Firmenregel] = {}
        for regel in regeln:
            self.merke(regel)

    def __repr__(self) -> str:
        return f"Firmengedaechtnis({len(self._regeln)} Regeln, {self.pfad})"

    def __len__(self) -> int:
        return len(self._regeln)

    @property
    def regeln(self) -> list[Firmenregel]:
        """Alle Regeln, sortiert nach Kunde und Lieferant (Kopie)."""
        return sorted(self._regeln.values(), key=_regel_sortierung)

    # -- laden / speichern ---------------------------------------------------

    @classmethod
    def aus_settings(cls, settings: Settings) -> Firmengedaechtnis:
        return cls.lade(Path(settings.firmenwahl.gedaechtnis_datei), tolerant=settings.firmenwahl.gedaechtnis_tolerant)

    @classmethod
    def lade(cls, pfad: Path | str, *, tolerant: bool = True) -> Firmengedaechtnis:
        """Liest die YAML-Liste von Firmenregeln; fehlende/leere/kaputte Datei -> leeres Gedächtnis.

        Einzelne ungültige Einträge werden mit Warnung übersprungen, die übrigen bleiben erhalten.
        ``tolerant=False`` schaltet die tolerante Suche (Kurzname als Wort im Kundentext) ab.
        """
        pfad = Path(pfad)
        if not pfad.exists():
            logger.info("Firmen-Gedächtnis %s gibt es noch nicht; es beginnt leer", pfad)
            return cls(pfad=pfad, tolerant=tolerant)
        daten = _lies_yaml(pfad, "Firmen-Gedächtnis")
        if daten is None:
            return cls(pfad=pfad, tolerant=tolerant)
        if isinstance(daten, dict):
            daten = daten.get("regeln", [])
        if not isinstance(daten, list):
            logger.warning("Firmen-Gedächtnis %s: erwartet eine Liste von Regeln", pfad)
            return cls(pfad=pfad, tolerant=tolerant)
        regeln: list[Firmenregel] = []
        for i, eintrag in enumerate(daten, start=1):
            if not isinstance(eintrag, dict):
                logger.warning("Firmen-Gedächtnis %s: Eintrag %d ist keine Abbildung; übersprungen", pfad, i)
                continue
            try:
                regel = Firmenregel.model_validate(eintrag)
            except ValidationError as exc:
                logger.warning("Firmen-Gedächtnis %s: Eintrag %d ungültig (%s); übersprungen", pfad, i, exc.errors()[0].get("msg", exc))
                continue
            if not _schluessel(regel.kunde_kurz) or not regel.firma_id.strip():
                logger.warning("Firmen-Gedächtnis %s: Eintrag %d ohne Kunde oder Firma; übersprungen", pfad, i)
                continue
            regeln.append(regel)
        gedaechtnis = cls(regeln, pfad=pfad, tolerant=tolerant)
        logger.info("Firmen-Gedächtnis %s geladen: %d Regeln", pfad, len(gedaechtnis))
        return gedaechtnis

    def speichere(self, pfad: Path | str | None = None) -> Path:
        """Schreibt alle Regeln sortiert als YAML (UTF-8) – atomar über eine temporäre Datei und ``os.replace``."""
        ziel = Path(pfad) if pfad is not None else self.pfad
        if ziel is None:
            raise ValueError("Firmen-Gedächtnis: kein Dateipfad zum Speichern bekannt")
        ziel.parent.mkdir(parents=True, exist_ok=True)
        inhalt = _KOPF_GEDAECHTNIS + yaml.safe_dump(
            [regel.model_dump(mode="json", exclude_none=True) for regel in self.regeln],
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        )
        fd, tmp_name = tempfile.mkstemp(prefix=f".{ziel.name}.", suffix=".tmp", dir=ziel.parent)
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(inhalt)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, ziel)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        self.pfad = ziel
        logger.info("Firmen-Gedächtnis gespeichert: %s (%d Regeln)", ziel, len(self._regeln))
        return ziel

    # -- Regeln ------------------------------------------------------------------

    @staticmethod
    def schluessel(kunde_kurz: str | None, lieferant_kurz: str | None = None) -> tuple[str, str | None]:
        """Normalisierter Schlüssel (Kunde, Lieferant oder None)."""
        return _schluessel(kunde_kurz), _lieferant_schluessel(lieferant_kurz)

    def merke(self, regel: Firmenregel) -> Firmenregel:
        """Nimmt die Regel auf; eine vorhandene Regel mit gleichem Schlüssel wird ersetzt.

        Kurznamen werden getrimmt, ein leerer Lieferant wird zu ``None`` (Kunde allgemein).
        Liefert die gespeicherte Regel. Ohne Kunden-Kurzname -> ``ValueError``.
        """
        schluessel = self.schluessel(regel.kunde_kurz, regel.lieferant_kurz)
        if not schluessel[0]:
            raise ValueError("Firmenregel ohne Kunden-Kurzname")
        kunde_kurz = regel.kunde_kurz.strip()
        lieferant_kurz = (regel.lieferant_kurz or "").strip() or None
        if (kunde_kurz, lieferant_kurz) != (regel.kunde_kurz, regel.lieferant_kurz):
            regel = regel.model_copy(update={"kunde_kurz": kunde_kurz, "lieferant_kurz": lieferant_kurz})
        alt = self._regeln.get(schluessel)
        if alt is not None and alt.firma_id != regel.firma_id:
            logger.info(
                "Firmen-Gedächtnis: Regel für Kunde %r / Lieferant %r wechselt von %s zu %s",
                regel.kunde_kurz,
                regel.lieferant_kurz,
                alt.firma_id,
                regel.firma_id,
            )
        self._regeln[schluessel] = regel
        return regel

    def entferne(self, kunde_kurz: str, lieferant_kurz: str | None = None) -> bool:
        """Löscht die Regel mit diesem Schlüssel; ``True``, wenn es sie gab."""
        return self._regeln.pop(self.schluessel(kunde_kurz, lieferant_kurz), None) is not None

    def finde(
        self, kunde_kurz: str | None, lieferant_kurz: str | None = None, *, streng: bool = False
    ) -> Firmenregel | None:
        """Regel zu Kunde + Lieferant, sonst zu Kunde allgemein (``lieferant_kurz`` leer); sonst ``None``.

        Erst exakte Schlüssel, dann tolerant: der gelernte Kurzname kommt als ganzes Wort im
        übergebenen Kundentext vor (bzw. der gelernte Lieferant im Lieferantentext oder umgekehrt).
        ``streng`` (Dokument einer Kundengruppe, bei der gefragt wird) bzw. eine Regel einer solchen
        Gruppe (:data:`FRAGEN_GRUPPEN`): tolerant nur über die gelernten Namen (``kunde_texte``), die
        vollständig im Kundentext stehen müssen – nie über den Matchcode.
        """
        kunde, lieferant = self.schluessel(kunde_kurz, lieferant_kurz)
        if not kunde:
            return None
        if lieferant is not None:
            regel = self._regeln.get((kunde, lieferant))
            if regel is not None:
                return regel
        regel = self._regeln.get((kunde, None))
        if regel is not None:
            return regel
        if lieferant is not None:
            regel = self._tolerant(kunde, lieferant, nur_spezifisch=True, streng=streng)
            if regel is not None:
                return regel
        return self._tolerant(kunde, None, nur_spezifisch=False, streng=streng)

    def finde_fuer_texte(
        self, kunde_texte: Iterable[str | None], lieferant_texte: Iterable[str | None], *, streng: bool = False
    ) -> Firmenregel | None:
        """Wie :meth:`finde`, aber über mehrere Kandidatentexte (Kundentext, Adresse, Alias-Namen …):
        zuerst alle Kombinationen Kunde + Lieferant, dann Kunde allgemein."""
        kunden = [k for k in dict.fromkeys(kunde_texte) if _schluessel(k)]
        lieferanten = [lf for lf in dict.fromkeys(lieferant_texte) if _lieferant_schluessel(lf)]
        for kunde in kunden:
            for lieferant in lieferanten:
                regel = self._spezifisch(kunde, lieferant, streng=streng)
                if regel is not None:
                    return regel
        for kunde in kunden:
            regel = self.finde(kunde, None, streng=streng)
            if regel is not None:
                return regel
        return None

    def _spezifisch(self, kunde_kurz: str, lieferant_kurz: str, *, streng: bool = False) -> Firmenregel | None:
        kunde, lieferant = self.schluessel(kunde_kurz, lieferant_kurz)
        if not kunde or lieferant is None:
            return None
        regel = self._regeln.get((kunde, lieferant))
        if regel is not None:
            return regel
        return self._tolerant(kunde, lieferant, nur_spezifisch=True, streng=streng)

    def _tolerant(
        self, kunde: str, lieferant: str | None, *, nur_spezifisch: bool, streng: bool = False
    ) -> Firmenregel | None:
        """Tolerante Suche: gelernter Kunden-Kurzname als Wort im Kundentext (oder umgekehrt), längster zuerst.

        Bei ``streng`` bzw. Regeln aus :data:`FRAGEN_GRUPPEN` zählen nur die gelernten Namen (``kunde_texte``,
        nicht der Matchcode), und nur wenn sie vollständig als Wortfolge im Kundentext stehen.
        """
        if not self.tolerant:
            return None
        kandidaten: list[tuple[int, Firmenregel]] = []
        for (_r_kunde, r_lieferant), regel in self._regeln.items():
            if nur_spezifisch and r_lieferant is None:
                continue
            if not nur_spezifisch and r_lieferant is not None:
                continue
            kunden_schluessel, lieferanten_schluessel = _regel_schluessel(regel)
            if streng or regel.kundengruppe in FRAGEN_GRUPPEN:
                matchcode = _schluessel(regel.kunde_kurz)
                passend = [k for k in kunden_schluessel if k != matchcode and _passt_tolerant(k, kunde, beidseitig=False)]
            else:
                passend = [k for k in kunden_schluessel if _passt_tolerant(k, kunde)]
            if not passend:
                continue
            if r_lieferant is not None and not (
                lieferant is not None and any(_passt_tolerant(lf, lieferant) for lf in lieferanten_schluessel)
            ):
                continue
            kandidaten.append((max(len(k) for k in passend), regel))
        if not kandidaten:
            return None
        kandidaten.sort(key=lambda e: -e[0])
        regel = kandidaten[0][1]
        logger.debug("Firmen-Gedächtnis: toleranter Treffer %r/%r für %r", regel.kunde_kurz, regel.lieferant_kurz, kunde)
        return regel


def _regel_schluessel(regel: Firmenregel) -> tuple[list[str], list[str]]:
    """Normalisierte Schlüssel einer Regel: Kurzname plus die weiteren Schreibweisen (``kunde_texte`` /
    ``lieferant_texte``, z. B. der Kundenname laut Dokument), ohne Leere und Doppelte."""
    kunden = [_schluessel(t) for t in [regel.kunde_kurz, *regel.kunde_texte]]
    lieferanten = [_schluessel(t) for t in [regel.lieferant_kurz, *regel.lieferant_texte]] if regel.lieferant_kurz else []
    return [k for k in dict.fromkeys(kunden) if k], [lf for lf in dict.fromkeys(lieferanten) if lf]


def _passt_tolerant(gelernt: str, text: str, *, beidseitig: bool = True) -> bool:
    """Gelernter Schlüssel als Wortfolge im Text (``beidseitig``: auch der Text im gelernten Schlüssel)."""
    if gelernt == text:
        return True
    if len(gelernt) < _MIN_LAENGE_TOLERANT or len(text) < _MIN_LAENGE_TOLERANT:
        return False
    return enthaelt_als_wort(text, gelernt) or (beidseitig and enthaelt_als_wort(gelernt, text))


# ---------------------------------------------------------------------------
# Aus der Freigabe merken
# ---------------------------------------------------------------------------


def _weitere_texte(kurz: str | None, *texte: str | None) -> list[str]:
    """Weitere Schreibweisen (getrimmt, ohne Leere, ohne Doppelte und ohne den Kurznamen selbst)."""
    kurz_norm = normalisiere_name(kurz)
    ergebnis: list[str] = []
    gesehen: set[str] = {kurz_norm} if kurz_norm else set()
    for text in texte:
        wert = (text or "").strip()
        norm = normalisiere_name(wert)
        if norm and norm not in gesehen:
            gesehen.add(norm)
            ergebnis.append(wert)
    return ergebnis


def _kurzname(matchcode: str | None, name: str | None) -> str | None:
    for wert in (matchcode, name):
        if wert and wert.strip():
            return wert.strip()
    return None


def merke_firma(
    paket: Belegpaket,
    settings: Settings,
    *,
    benutzer: str | None = None,
    gedaechtnis: Firmengedaechtnis | None = None,
    jetzt: Callable[[], datetime] | datetime | None = None,
) -> Firmenregel | None:
    """Schreibt die im Paket gewählte Firma als Regel ins Firmen-Gedächtnis (siehe Modul-Docstring).

    Nur wenn ``paket.firma_id`` gesetzt ist, ``paket.firma_merken`` an ist und
    ``settings.firmenwahl.merken`` erlaubt. ``gedaechtnis`` ersetzt das Laden aus
    ``settings.firmenwahl.gedaechtnis_datei``; ``jetzt`` (Wert oder Funktion) ersetzt ``datetime.now``.
    Liefert die Regel oder ``None`` (nichts gemerkt).
    """
    if not paket.firma_id:
        logger.debug("Firma nicht gemerkt: keine Firma im Paket")
        return None
    if not paket.firma_merken:
        logger.debug("Firma nicht gemerkt: paket.firma_merken ist aus")
        return None
    if not settings.firmenwahl.merken:
        logger.info("Firma nicht gemerkt: firmenwahl.merken ist in den Einstellungen aus")
        return None
    if paket.firma_id not in settings.firmen:
        logger.warning("Firma %r nicht gemerkt: nicht in firmen.yaml", paket.firma_id)
        return None

    kunde = paket.kunde.kunde
    kunde_kurz = _kurzname(kunde.matchcode, kunde.name) if kunde is not None else None
    if kunde_kurz is None:
        kunde_kurz = (paket.roh.kunde_text or "").strip() or None
    if not kunde_kurz or not normalisiere_name(kunde_kurz):
        logger.warning("Firma %s nicht gemerkt: kein Kundenname bekannt (%s)", paket.firma_id, paket.roh.quelldatei)
        return None

    lieferant = paket.lieferant.lieferant
    lieferant_kurz = _kurzname(lieferant.matchcode, lieferant.name) if lieferant is not None else None
    if lieferant_kurz is None:
        lieferant_kurz = (paket.roh.lieferant_text or "").strip() or None

    if jetzt is None:
        zeitpunkt = datetime.now()
    elif isinstance(jetzt, datetime):
        zeitpunkt = jetzt
    else:
        zeitpunkt = jetzt()

    bemerkung = None
    dokument_text = (paket.roh.kunde_text or "").strip()
    if dokument_text and normalisiere_name(dokument_text) != normalisiere_name(kunde_kurz):
        bemerkung = f"Kunde laut Dokument: {dokument_text}"
    # Beim nächsten Auftrag ist die Firma (und damit der Matchcode) noch unbekannt: die Regel muss auch über
    # die Namen laut Dokument und Stamm wiedererkannt werden.
    kunde_texte = _weitere_texte(kunde_kurz, paket.roh.kunde_text, kunde.name if kunde is not None else None)
    lieferant_texte = (
        _weitere_texte(
            lieferant_kurz,
            paket.roh.lieferant_text,
            lieferant.name if lieferant is not None else None,
            lieferant.name2 if lieferant is not None else None,
        )
        if lieferant_kurz
        else []
    )

    regel = Firmenregel(
        kunde_kurz=kunde_kurz,
        lieferant_kurz=lieferant_kurz,
        firma_id=paket.firma_id,
        kundengruppe=paket.kundengruppe,
        gelernt_am=zeitpunkt.replace(microsecond=0),
        gelernt_von=(benutzer or "").strip() or None,
        quelle="freigabe",
        bemerkung=bemerkung,
        kunde_texte=kunde_texte,
        lieferant_texte=lieferant_texte,
    )

    if gedaechtnis is None:
        gedaechtnis = Firmengedaechtnis.aus_settings(settings)
    gedaechtnis.merke(regel)
    try:
        pfad = gedaechtnis.speichere(gedaechtnis.pfad or Path(settings.firmenwahl.gedaechtnis_datei))
    except OSError as exc:
        logger.error("Firmen-Gedächtnis konnte nicht gespeichert werden (%s); Regel nur im Speicher", exc)
        return None
    logger.info(
        "Firma %s für Kunde %r / Lieferant %r gemerkt (%s, von %s) -> %s",
        regel.firma_id,
        regel.kunde_kurz,
        regel.lieferant_kurz or "alle",
        regel.kundengruppe.value if regel.kundengruppe else "Kundengruppe unbekannt",
        regel.gelernt_von or "-",
        pfad,
    )
    return regel
