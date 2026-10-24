"""Kurznamen für Lieferant und Kunde in der Fixterminüberwachung.

Die Spalten "Lieferant" und "Kunde" der Fixterminüberwachung enthalten Kurznamen (in der Regel die
Lexware-Matchcodes), die zugleich die Dropdown-Quelle des Blatts "Ressourcen für Auto DropDown"
speisen. Ein Kurzname wird in dieser Reihenfolge ermittelt (:class:`Kurznamen`):

1. ``config/aliase/kurznamen.yaml`` (Lexware-Nummer + Firma -> Kurzname, vom Anwender gepflegt),
2. Lexware-Matchcode (``Kunde.matchcode`` / ``Lieferant.matchcode``),
3. erster Wortbestandteil des Namens ohne Rechtsform und Gattungswort
   (:func:`erster_wortbestandteil`, "Weinhaus Beispiel GmbH" -> "Beispiel"),
4. ohne Stammdaten: der Text aus dem Dokument, sonst ``Unbekannt``.

Anschließend wird der Name an die vorhandene Dropdown-Ressourcenliste angeglichen
(:meth:`Kurznamen.angleichen`): gleiche Schreibweise nach ``benennung.normalisiere`` oder
rapidfuzz-Ähnlichkeit >= ``schwelle`` (Standard 90) übernimmt die vorhandene Schreibweise; sonst
bleibt der Name und wird in :attr:`Kurznamen.neue_namen` gemeldet, damit der Anwender die
Ressourcenliste prüfen kann. Die Ressourcenliste selbst wird nie beschrieben (Formeln).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml
from rapidfuzz import fuzz, process

from auftragsautomat.benennung import UNBEKANNT, normalisiere
from auftragsautomat.config import DateiRefKonfig, Settings
from auftragsautomat.m365.basis import ExcelBackend, M365NichtGefunden
from auftragsautomat.models import Belegpaket, Kunde, Lieferant

__all__ = [
    "GENERISCHE_WOERTER",
    "KurznameErgebnis",
    "Kurznamen",
    "KurznamenDaten",
    "KurznamenFehler",
    "RECHTSFORMEN",
    "RESSOURCEN_BLATT",
    "STANDARD_DATEINAME",
    "STANDARD_SCHWELLE",
    "erster_wortbestandteil",
    "lade_kurznamen_yaml",
    "vorhandene_kurznamen",
]

logger = logging.getLogger(__name__)

#: Dateiname der Kurznamen-Tabelle unter ``settings.alias_ordner``.
STANDARD_DATEINAME = "kurznamen.yaml"
#: Zweites Blatt der Fixterminüberwachung (Dropdown-Quelle, nie beschreiben).
RESSOURCEN_BLATT = "Ressourcen für Auto DropDown"
#: rapidfuzz-Score (0-100), ab dem ein vorhandener Dropdown-Name übernommen wird.
STANDARD_SCHWELLE = 90.0

#: Rechtsformen (normalisiert, ohne Punkte), die am Namensende bzw. als eigenes Wort entfallen.
RECHTSFORMEN: frozenset[str] = frozenset(
    {
        "gmbh", "ag", "kg", "ohg", "gbr", "ek", "ug", "mbh", "co", "cokg", "eg", "ev", "se",
        "srl", "spa", "sas", "sarl", "sa", "sl", "slu", "lda", "ltd", "llc", "bv", "nv", "snc",
        "ss", "scarl", "soc", "societa", "scpa", "sca", "inc", "plc", "sagl", "sasu", "eurl",
        "haftungsbeschraenkt", "und", "&", "e", "v", "i", "l", "gmbhcokg",
    }
)
#: Gattungswörter am Namensanfang, die kein Kurzname sein sollen ("Weingut Beispiel" -> "Beispiel").
GENERISCHE_WOERTER: tuple[str, ...] = (
    "weingut", "weinhaus", "weinkellerei", "weinhandel", "weinhandlung", "weinmarkt", "weine",
    "kellerei", "sektkellerei", "cantina", "cantine", "azienda", "agricola", "vitivinicola",
    "vinicola", "tenuta", "tenute", "fattoria", "podere", "poderi", "castello", "casa", "societa",
    "soc", "agr", "domaine", "domaines", "chateau", "maison", "vignobles", "vignoble", "cave",
    "caves", "bodega", "bodegas", "vinos", "vini", "quinta", "adega", "weinbau", "winzer",
    "winzerhof", "winzergenossenschaft", "wg", "familie", "fam", "gebr", "gebrueder",
    # Artikel/Präpositionen, die nach einem Gattungswort folgen ("Domaine du …", "Tenuta di …")
    "de", "du", "des", "la", "le", "les", "di", "da", "del", "della", "dei", "delle", "degli",
    "el", "los", "las", "von", "zum", "zur", "am", "im", "the",
)

_RECHTSFORM_PUNKTE = re.compile(r"[.·]")
_WORTTRENNER = re.compile(r"[\s,/;()\[\]\-–—]+")


class KurznamenFehler(Exception):
    """``kurznamen.yaml`` ist nicht lesbar oder hat ein unerwartetes Format."""


Quelle = Literal["alias", "matchcode", "name", "text", "unbekannt"]


@dataclass(frozen=True)
class KurznameErgebnis:
    """Ermittelter Kurzname mit Herkunft und Abgleich gegen die Ressourcenliste."""

    kurzname: str
    quelle: Quelle
    vorhanden: bool  # True = Schreibweise aus der Dropdown-Ressourcenliste übernommen
    ermittelt: str  # der Name vor dem Abgleich (gleich ``kurzname``, wenn nichts angepasst wurde)


@dataclass
class KurznamenDaten:
    """Inhalt von ``kurznamen.yaml``: Schlüssel ``(firma_id | None, nummer)`` -> Kurzname."""

    kunden: dict[tuple[str | None, str], str] = field(default_factory=dict)
    lieferanten: dict[tuple[str | None, str], str] = field(default_factory=dict)
    rechtsformen: set[str] = field(default_factory=set)
    generische_woerter: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# YAML
# ---------------------------------------------------------------------------


def _als_text(wert: Any) -> str | None:
    if wert is None:
        return None
    text = str(wert).strip()
    return text or None


def _eintraege(daten: Mapping[str, Any], wurzel: str, nummer_feld: str, pfad: Path) -> dict[tuple[str | None, str], str]:
    ergebnis: dict[tuple[str | None, str], str] = {}
    liste = daten.get(wurzel) or []
    if not isinstance(liste, list):
        raise KurznamenFehler(f"{pfad}: '{wurzel}' muss eine Liste sein.")
    for nr, eintrag in enumerate(liste, start=1):
        if not isinstance(eintrag, Mapping):
            raise KurznamenFehler(f"{pfad}: Eintrag {nr} unter '{wurzel}' muss ein Mapping sein.")
        nummer = _als_text(eintrag.get(nummer_feld)) or _als_text(eintrag.get("nummer"))
        kurzname = _als_text(eintrag.get("kurzname"))
        if not nummer or not kurzname:
            logger.warning("%s: Eintrag %s unter '%s' ohne %s/kurzname wird übersprungen", pfad, nr, wurzel, nummer_feld)
            continue
        firma = _als_text(eintrag.get("firma"))
        schluessel = (firma, nummer)
        if schluessel in ergebnis and ergebnis[schluessel] != kurzname:
            logger.warning("%s: doppelter Eintrag %s/%s unter '%s'; '%s' gewinnt", pfad, firma or "alle", nummer, wurzel, kurzname)
        ergebnis[schluessel] = kurzname
    return ergebnis


def lade_kurznamen_yaml(pfad: Path) -> KurznamenDaten:
    """Liest ``kurznamen.yaml``; fehlt die Datei, ist das Ergebnis leer (Hinweis im Log)."""
    pfad = Path(pfad)
    if not pfad.exists():
        logger.info("Kurznamen-Datei %s fehlt; es gelten Matchcodes und Namen", pfad)
        return KurznamenDaten()
    try:
        text = pfad.read_text(encoding="utf-8")
    except OSError as exc:
        raise KurznamenFehler(f"Kurznamen-Datei {pfad} kann nicht gelesen werden: {exc}") from exc
    try:
        daten = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise KurznamenFehler(f"Kurznamen-Datei {pfad} ist kein gültiges YAML: {exc}") from exc
    if daten is None:
        return KurznamenDaten()
    if not isinstance(daten, Mapping):
        raise KurznamenFehler(f"Kurznamen-Datei {pfad}: oberste Ebene muss eine Zuordnung (Mapping) sein.")
    rechtsformen = {normalisiere(_RECHTSFORM_PUNKTE.sub("", str(w))) for w in (daten.get("rechtsformen") or []) if _als_text(w)}
    generisch = [normalisiere(str(w)) for w in (daten.get("generische_woerter") or []) if _als_text(w)]
    return KurznamenDaten(
        kunden=_eintraege(daten, "kunden", "kundennummer", pfad),
        lieferanten=_eintraege(daten, "lieferanten", "lieferantennummer", pfad),
        rechtsformen=rechtsformen,
        generische_woerter=generisch,
    )


# ---------------------------------------------------------------------------
# Namensableitung
# ---------------------------------------------------------------------------


def erster_wortbestandteil(
    name: str | None,
    *,
    rechtsformen: Iterable[str] = (),
    generische_woerter: Iterable[str] = (),
) -> str:
    """Erster aussagekräftiger Wortbestandteil eines Firmennamens.

    Rechtsformen (GmbH, srl, SAS …) entfallen überall, Gattungswörter (Weingut, Cantina, Domaine …)
    nur am Anfang. Bleibt nichts übrig, wird das erste Wort des Originalnamens genommen; leere
    Namen ergeben ``""``. Die Schreibweise des Treffers bleibt wie im Namen.
    """
    if not name or not name.strip():
        return ""
    alle_rechtsformen = set(RECHTSFORMEN) | {normalisiere(_RECHTSFORM_PUNKTE.sub("", r)) for r in rechtsformen}
    alle_generisch = set(GENERISCHE_WOERTER) | {normalisiere(w) for w in generische_woerter}
    woerter = [w for w in _WORTTRENNER.split(name.strip()) if w]
    kandidaten = [w for w in woerter if normalisiere(_RECHTSFORM_PUNKTE.sub("", w)) not in alle_rechtsformen]
    while kandidaten and normalisiere(_RECHTSFORM_PUNKTE.sub("", kandidaten[0])) in alle_generisch:
        kandidaten.pop(0)
    treffer = kandidaten[0] if kandidaten else woerter[0]
    return treffer.strip(".,;:'\"")


def vorhandene_kurznamen(excel: ExcelBackend, datei: DateiRefKonfig, blatt: str = RESSOURCEN_BLATT) -> list[str]:
    """Alle Kurznamen aus dem Ressourcen-Blatt der Fixterminüberwachung (nur lesen).

    Es werden alle nicht leeren Textzellen des benutzten Bereichs gesammelt (Reihenfolge wie im
    Blatt, Dubletten entfernt). Spaltenüberschriften wie "Lieferant"/"Kunde" und unausgewertete
    Formeltexte (``=…``) werden übersprungen. Fehlt das Blatt, ist die Liste leer (Hinweis im Log),
    damit das Register auch ohne Ressourcenliste arbeitet.
    """
    try:
        _, werte = excel.lese_benutzten_bereich(datei, blatt)
    except M365NichtGefunden as exc:
        logger.warning("Ressourcen-Blatt '%s' nicht lesbar (%s); Kurznamen werden nicht angeglichen", blatt, exc)
        return []
    ueberschriften = {"lieferant", "lieferanten", "kunde", "kunden", "spedition", "speditionen", "name", "kurzname"}
    namen: list[str] = []
    gesehen: set[str] = set()
    for zeile in werte:
        for wert in zeile:
            if not isinstance(wert, str):
                continue
            text = wert.strip()
            if not text or text.startswith("=") or text.startswith("#"):
                continue
            norm = normalisiere(text)
            if norm in ueberschriften or norm in gesehen:
                continue
            gesehen.add(norm)
            namen.append(text)
    return namen


# ---------------------------------------------------------------------------
# Kurznamen
# ---------------------------------------------------------------------------


class Kurznamen:
    """Kurznamen je Lexware-Nummer mit Abgleich gegen die Dropdown-Ressourcenliste."""

    def __init__(
        self,
        daten: KurznamenDaten | None = None,
        *,
        vorhandene: Iterable[str] = (),
        schwelle: float = STANDARD_SCHWELLE,
    ) -> None:
        self.daten = daten or KurznamenDaten()
        self.schwelle = float(schwelle)
        self._vorhandene: list[str] = []
        self._vorhandene_norm: dict[str, str] = {}
        self._neue: dict[str, str] = {}  # normalisiert -> Name
        self.mit_vorhandenen(vorhandene)

    @classmethod
    def lade(
        cls,
        pfad: Path | None = None,
        *,
        settings: Settings | None = None,
        vorhandene: Iterable[str] = (),
        schwelle: float = STANDARD_SCHWELLE,
    ) -> Kurznamen:
        """Lädt ``kurznamen.yaml`` (``pfad``, sonst ``settings.alias_ordner/kurznamen.yaml``)."""
        if pfad is None:
            if settings is None:
                raise KurznamenFehler("Kurznamen.lade braucht einen Pfad oder settings.")
            pfad = Path(settings.alias_ordner) / STANDARD_DATEINAME
        return cls(lade_kurznamen_yaml(pfad), vorhandene=vorhandene, schwelle=schwelle)

    # -- Ressourcenliste ------------------------------------------------------------

    @property
    def vorhandene(self) -> list[str]:
        """Kurznamen der Dropdown-Ressourcenliste (Kopie)."""
        return list(self._vorhandene)

    def mit_vorhandenen(self, namen: Iterable[str]) -> None:
        """Ergänzt die Dropdown-Ressourcenliste um ``namen`` (Dubletten nach Normalisierung entfallen)."""
        for name in namen:
            text = str(name).strip()
            if not text:
                continue
            norm = normalisiere(text)
            if norm not in self._vorhandene_norm:
                self._vorhandene_norm[norm] = text
                self._vorhandene.append(text)

    @property
    def neue_namen(self) -> list[str]:
        """Ermittelte Kurznamen, die (noch) nicht in der Ressourcenliste stehen (sortiert)."""
        return sorted(self._neue.values(), key=str.casefold)

    def angleichen(self, name: str) -> tuple[str, bool]:
        """Gleicht ``name`` an die Ressourcenliste an.

        Rückgabe ``(kurzname, vorhanden)``: gleiche Schreibweise nach Normalisierung oder
        rapidfuzz-Ähnlichkeit >= ``schwelle`` liefert den vorhandenen Namen; sonst bleibt ``name``,
        und er wird in :attr:`neue_namen` vermerkt (nicht für ``Unbekannt``).
        """
        text = (name or "").strip()
        if not text:
            return text, False
        norm = normalisiere(text)
        if norm in self._vorhandene_norm:
            return self._vorhandene_norm[norm], True
        if self._vorhandene_norm:
            treffer = process.extractOne(norm, list(self._vorhandene_norm), scorer=fuzz.ratio, score_cutoff=self.schwelle)
            if treffer is not None:
                vorhanden = self._vorhandene_norm[treffer[0]]
                logger.info("Kurzname '%s' an vorhandenen Dropdown-Namen '%s' angeglichen (Score %.0f)", text, vorhanden, treffer[1])
                return vorhanden, True
        if norm != normalisiere(UNBEKANNT) and norm not in self._neue:
            self._neue[norm] = text
            if self._vorhandene_norm:
                logger.info("Kurzname '%s' steht noch nicht in der Dropdown-Ressourcenliste", text)
        return text, False

    # -- Ermittlung -------------------------------------------------------------------

    def _ermittle(
        self,
        tabelle: Mapping[tuple[str | None, str], str],
        *,
        firma_id: str | None,
        nummer: str | None,
        matchcode: str | None,
        name: str | None,
        text: str | None,
    ) -> KurznameErgebnis:
        ermittelt: str | None = None
        quelle: Quelle = "unbekannt"
        if nummer:
            ermittelt = tabelle.get((firma_id, nummer)) if firma_id else None
            if ermittelt is None:
                ermittelt = tabelle.get((None, nummer))
            if ermittelt:
                quelle = "alias"
        if not ermittelt and matchcode and matchcode.strip():
            ermittelt, quelle = matchcode.strip(), "matchcode"
        if not ermittelt and name and name.strip():
            ermittelt = erster_wortbestandteil(
                name, rechtsformen=self.daten.rechtsformen, generische_woerter=self.daten.generische_woerter
            )
            quelle = "name"
        if not ermittelt and text and text.strip():
            ermittelt = erster_wortbestandteil(
                text, rechtsformen=self.daten.rechtsformen, generische_woerter=self.daten.generische_woerter
            ) or text.strip()
            quelle = "text"
        if not ermittelt:
            return KurznameErgebnis(UNBEKANNT, "unbekannt", False, UNBEKANNT)
        kurzname, vorhanden = self.angleichen(ermittelt)
        return KurznameErgebnis(kurzname, quelle, vorhanden, ermittelt)

    def ermittle_kunde(self, kunde: Kunde | None, *, text: str | None = None, firma_id: str | None = None) -> KurznameErgebnis:
        """Kurzname des Kunden (``text`` = Kundenname laut Dokument als letzter Rückfall)."""
        if kunde is None:
            return self._ermittle(self.daten.kunden, firma_id=firma_id, nummer=None, matchcode=None, name=None, text=text)
        return self._ermittle(
            self.daten.kunden,
            firma_id=firma_id or kunde.firma_id,
            nummer=kunde.kundennummer,
            matchcode=kunde.matchcode,
            name=kunde.name,
            text=text,
        )

    def ermittle_lieferant(
        self, lieferant: Lieferant | None, *, text: str | None = None, firma_id: str | None = None
    ) -> KurznameErgebnis:
        """Kurzname des Lieferanten (``text`` = Lieferantenname laut Dokument als letzter Rückfall)."""
        if lieferant is None:
            return self._ermittle(self.daten.lieferanten, firma_id=firma_id, nummer=None, matchcode=None, name=None, text=text)
        return self._ermittle(
            self.daten.lieferanten,
            firma_id=firma_id or lieferant.firma_id,
            nummer=lieferant.lieferantennummer,
            matchcode=lieferant.matchcode,
            name=lieferant.name,
            text=text,
        )

    def kunde(self, kunde: Kunde | None, *, text: str | None = None, firma_id: str | None = None) -> str:
        return self.ermittle_kunde(kunde, text=text, firma_id=firma_id).kurzname

    def lieferant(self, lieferant: Lieferant | None, *, text: str | None = None, firma_id: str | None = None) -> str:
        return self.ermittle_lieferant(lieferant, text=text, firma_id=firma_id).kurzname

    def fuer_paket(self, paket: Belegpaket) -> tuple[str, str]:
        """``(lieferant_kurz, kunde_kurz)`` für ein Belegpaket (zugeordnete Stammdaten, sonst Dokumenttext)."""
        lieferant = self.lieferant(paket.lieferant.lieferant, text=paket.roh.lieferant_text, firma_id=paket.firma_id)
        kunde = self.kunde(paket.kunde.kunde, text=paket.roh.kunde_text, firma_id=paket.firma_id)
        return lieferant, kunde
