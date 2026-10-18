"""Alias-Tabellen (``config/aliase/*.yaml``) und Text-Normalisierung für das Matching.

Ein Alias ist eine Schreibweise, wie sie in einem Dokument vorkommt, und zeigt auf eine
Kunden-, Lieferanten- oder Artikelnummer in einem Lexware-Mandanten (``firma``). Aliase
schlagen das Fuzzy-Matching (Score 100). Dateien:

* ``kunden.yaml``      – ``kunden: [{kundennummer, firma, anzeigename?, aliase: [...]}]``
* ``lieferanten.yaml`` – ``lieferanten: [{lieferantennummer, firma, anzeigename?, aliase: [...],
  produktmarken: [...]}]``; Produktmarken ordnen Artikelbezeichnungen ("Villa Armellina ...")
  einem Lieferanten zu.
* ``artikel.yaml``     – ``artikel: [{artikelnummer, firma, anzeigename?, aliase: [...]}]`` und
  ``ueberspringen: [...]`` (Positionen, die immer übersprungen und gemeldet werden).

Vergleich: Text und Alias werden mit :func:`normalisiere_name` normalisiert (Kleinschreibung,
Umlaute/Akzente, Satzzeichen, Rechtsformen wie "GmbH", "& Co. KG", "srl", Mehrfach-Leerzeichen).
Ein Alias trifft, wenn er als ganzes Wort bzw. ganze Wortfolge im Text vorkommt
("Weinmarkt Kirchhellen GmbH & Co. KG" trifft den Alias "Weinmarkt Kirchhellen"). Treffen
mehrere Aliase, gewinnt der längste; bei gleicher Länge der zuerst definierte.

Fehlende Dateien ergeben leere Tabellen (Warnung im Log), kaputte YAML-Dateien einen
:class:`AliasFehler`. Die ``speichere_alias_*``-Funktionen schreiben neue Aliase aus der
Freigabe-UI zurück (YAML-Kommentare gehen dabei verloren, die Reihenfolge bleibt stabil).

Zusätzlich stellt das Modul das gemeinsame Ähnlichkeitsmaß :func:`aehnlichkeit` für
``partner.py`` und ``artikel.py`` bereit.
"""

from __future__ import annotations

import logging
import os
import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml
from rapidfuzz import fuzz

__all__ = [
    "ARTIKEL_DATEI",
    "KUNDEN_DATEI",
    "LIEFERANTEN_DATEI",
    "AliasEintrag",
    "AliasFehler",
    "AliasKonflikt",
    "AliasTabellen",
    "AliasTreffer",
    "aehnlichkeit",
    "enthaelt_als_wort",
    "normalisiere_name",
    "speichere_alias_artikel",
    "speichere_alias_kunde",
    "speichere_alias_lieferant",
    "speichere_ueberspringen",
]

logger = logging.getLogger(__name__)

KUNDEN_DATEI = "kunden.yaml"
LIEFERANTEN_DATEI = "lieferanten.yaml"
ARTIKEL_DATEI = "artikel.yaml"

_KOPF_KUNDEN = (
    '# Kunden-Aliase: "Name wie er im PDF steht" -> Kundennummer in Lexware.\n'
    "# Diese Datei wird von der Freigabe-UI gepflegt; Kommentare gehen beim Speichern verloren.\n\n"
)
_KOPF_LIEFERANTEN = (
    '# Lieferanten-Aliase: "Name wie er im PDF/Artikel steht" -> Lieferantennummer in Lexware.\n'
    "# produktmarken: Produkte dieser Marken laufen über diesen Lieferanten.\n"
    "# Diese Datei wird von der Freigabe-UI gepflegt; Kommentare gehen beim Speichern verloren.\n\n"
)
_KOPF_ARTIKEL = (
    '# Artikel-Aliase: "Bezeichnung wie sie im PDF steht" -> Artikelnummer in Lexware.\n'
    "# ueberspringen: Positionen, die immer übersprungen und gemeldet werden.\n"
    "# Diese Datei wird von der Freigabe-UI gepflegt; Kommentare gehen beim Speichern verloren.\n\n"
)


class AliasFehler(ValueError):
    """Alias-Datei ist nicht lesbar/ungültig oder ein Alias kann nicht gespeichert werden."""


class AliasKonflikt(AliasFehler):
    """Der Alias zeigt in derselben Firma bereits auf eine andere Nummer."""


# ---------------------------------------------------------------------------
# Normalisierung und Ähnlichkeit (gemeinsam für Aliase, Partner- und Artikel-Matching)
# ---------------------------------------------------------------------------

_UMLAUTE = str.maketrans(
    {"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue", "ẞ": "Ss"}
)

# Rechtsformen als Token-Folgen (nach Kleinschreibung, "&"/"+" -> "und", Satzzeichen -> Leerzeichen).
_RECHTSFORMEN: tuple[tuple[str, ...], ...] = (
    ("gmbh", "und", "co", "kg"),
    ("gmbh", "und", "co", "kgaa"),
    ("gmbh", "und", "co"),
    ("gmbh", "co", "kg"),
    ("ag", "und", "co", "kg"),
    ("und", "co", "kg"),
    ("und", "co", "ohg"),
    ("und", "co"),
    ("co", "kg"),
    ("ug", "haftungsbeschraenkt"),
    ("haftungsbeschraenkt",),
    ("gmbh",),
    ("ggmbh",),
    ("mbh",),
    ("ag",),
    ("kg",),
    ("kgaa",),
    ("ohg",),
    ("gbr",),
    ("ug",),
    ("e", "k"),
    ("ek",),
    ("e", "kfr"),
    ("e", "kfm"),
    ("e", "v"),
    ("ev",),
    ("eg",),
    ("s", "r", "l"),
    ("srl",),
    ("s", "p", "a"),
    ("spa",),
    ("s", "a", "s"),
    ("sas",),
    ("s", "a", "r", "l"),
    ("sarl",),
    ("s", "a"),
    ("sa",),
    ("s", "l"),
    ("sl",),
    ("ltd",),
    ("limited",),
    ("inc",),
    ("llc",),
    ("plc",),
    ("b", "v"),
    ("bv",),
    ("n", "v"),
    ("nv",),
)
_RECHTSFORMEN_SORTIERT = sorted(_RECHTSFORMEN, key=len, reverse=True)
_MAX_RECHTSFORM_LAENGE = max(len(m) for m in _RECHTSFORMEN)


def _ohne_akzente(text: str) -> str:
    """Umlaute -> ae/oe/ue/ss, übrige Akzente entfernen (Cà -> ca, Müller -> Mueller)."""
    text = text.translate(_UMLAUTE)
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def _ohne_rechtsformen(tokens: list[str]) -> list[str]:
    ergebnis: list[str] = []
    i = 0
    while i < len(tokens):
        for muster in _RECHTSFORMEN_SORTIERT:
            n = len(muster)
            if n <= len(tokens) - i and tuple(tokens[i : i + n]) == muster:
                i += n
                break
        else:
            ergebnis.append(tokens[i])
            i += 1
    return ergebnis


def normalisiere_name(text: str | None, *, rechtsformen_entfernen: bool = True) -> str:
    """Normalisiert einen Namen oder eine Bezeichnung für Alias- und Fuzzy-Vergleiche.

    Kleinschreibung, Umlaute/Akzente aufgelöst, "&"/"+" -> "und", Apostrophe entfernt
    (d'Avola -> davola), Ziffern und Buchstaben getrennt ("0,75l" -> "0 75 l"), alle übrigen
    Satzzeichen -> Leerzeichen, Mehrfach-Leerzeichen zusammengefasst, Rechtsformen entfernt
    (nur, wenn danach noch etwas übrig bleibt).

    >>> normalisiere_name("Weinmarkt Kirchhellen GmbH & Co. KG")
    'weinmarkt kirchhellen'
    >>> normalisiere_name("Bollina s.r.l.")
    'bollina'
    >>> normalisiere_name("Weinhandlung Müller-Lüdenscheid")
    'weinhandlung mueller luedenscheid'
    """
    if not text:
        return ""
    t = _ohne_akzente(str(text)).lower()
    t = t.replace("&", " und ").replace("+", " und ")
    t = re.sub(r"['’`´]", "", t)
    t = re.sub(r"(\d)([a-z])", r"\1 \2", t)
    t = re.sub(r"([a-z])(\d)", r"\1 \2", t)
    t = re.sub(r"[^a-z0-9]+", " ", t)
    tokens = t.split()
    if rechtsformen_entfernen and tokens:
        bereinigt = _ohne_rechtsformen(tokens)
        if bereinigt:
            tokens = bereinigt
    return " ".join(tokens)


def enthaelt_als_wort(text_norm: str, teil_norm: str) -> bool:
    """``True``, wenn ``teil_norm`` als ganzes Wort bzw. ganze Wortfolge in ``text_norm`` vorkommt
    (beide bereits normalisiert)."""
    if not text_norm or not teil_norm:
        return False
    return f" {teil_norm} " in f" {text_norm} "


def aehnlichkeit(a: str, b: str) -> float:
    """Ähnlichkeit zweier normalisierter Texte, 0..100.

    Mischung aus ``token_set_ratio`` (robust gegen Wortreihenfolge und Zusatzwörter) und
    ``ratio`` (bestraft fehlende/zusätzliche Wörter): ``0,6 * token_set + 0,4 * ratio``.
    Damit erhält "weinhandlung mueller" gegen "weinhandlung mueller luedenscheid" rund 90,
    gegen "weinhandlung mueller" aber 100, so dass der exakte Name eindeutig gewinnt.
    """
    if not a or not b:
        return 0.0
    if a == b:
        return 100.0
    return round(0.6 * fuzz.token_set_ratio(a, b) + 0.4 * fuzz.ratio(a, b), 1)


# ---------------------------------------------------------------------------
# Datenklassen
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AliasTreffer:
    """Ergebnis einer Alias-Suche: Nummer im Mandanten ``firma_id`` und der getroffene Alias."""

    nummer: str
    firma_id: str
    anzeigename: str | None = None
    alias: str = ""


@dataclass(frozen=True)
class AliasEintrag:
    """Ein einzelner Alias (eine Schreibweise) für eine Nummer in einer Firma."""

    nummer: str
    firma_id: str
    alias: str
    alias_norm: str
    anzeigename: str | None = None
    art: str = "alias"  # "alias" | "produktmarke" | "ueberspringen"

    def treffer(self) -> AliasTreffer:
        return AliasTreffer(self.nummer, self.firma_id, self.anzeigename, self.alias)


# ---------------------------------------------------------------------------
# YAML lesen
# ---------------------------------------------------------------------------


def _lies_yaml(pfad: Path, *, warnen_wenn_fehlt: bool = True) -> dict:
    if not pfad.exists():
        if warnen_wenn_fehlt:
            logger.warning("Alias-Datei %s fehlt; verwende leere Tabelle", pfad)
        return {}
    try:
        try:
            text = pfad.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            logger.warning("Alias-Datei %s ist nicht UTF-8; lese als cp1252", pfad)
            text = pfad.read_text(encoding="cp1252")
    except OSError as exc:
        raise AliasFehler(f"Alias-Datei {pfad} kann nicht gelesen werden: {exc}") from exc
    try:
        daten = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise AliasFehler(f"Alias-Datei {pfad} ist kein gültiges YAML: {exc}") from exc
    if daten is None:
        return {}
    if not isinstance(daten, Mapping):
        raise AliasFehler(f"Alias-Datei {pfad}: oberste Ebene muss eine Zuordnung (Mapping) sein")
    return dict(daten)


def _als_liste(wert: object) -> list[object]:
    if wert is None:
        return []
    if isinstance(wert, str | int | float):
        return [wert]
    if isinstance(wert, list | tuple):
        return list(wert)
    return []


def _als_text(wert: object) -> str | None:
    if wert is None:
        return None
    text = str(wert).strip()
    return text or None


def _eintraege_aus(
    daten: Mapping[str, object],
    *,
    wurzel: str,
    nummer_feld: str,
    listenfeld: str,
    art: str,
    datei: Path,
) -> list[AliasEintrag]:
    """Liest ``daten[wurzel]`` (Liste von Zuordnungen) in :class:`AliasEintrag`-Objekte."""
    roh = daten.get(wurzel)
    if roh is None:
        return []
    if not isinstance(roh, list):
        raise AliasFehler(f"{datei}: '{wurzel}' muss eine Liste sein")
    eintraege: list[AliasEintrag] = []
    gesehen: dict[tuple[str, str], str] = {}  # (firma, alias_norm) -> nummer
    for i, element in enumerate(roh, start=1):
        if not isinstance(element, Mapping):
            logger.warning(
                "%s: Eintrag %d unter '%s' ist keine Zuordnung und wird ignoriert", datei, i, wurzel
            )
            continue
        nummer = _als_text(element.get(nummer_feld))
        if nummer is None:
            logger.warning(
                "%s: Eintrag %d unter '%s' hat keine '%s' und wird ignoriert", datei, i, wurzel, nummer_feld
            )
            continue
        firma = _als_text(element.get("firma") or element.get("firma_id"))
        if firma is None:
            logger.warning(
                "%s: %s %s ohne 'firma' wird ignoriert (Firma ist Pflicht)", datei, nummer_feld, nummer
            )
            continue
        anzeigename = _als_text(element.get("anzeigename"))
        normen_im_eintrag: set[str] = set()
        for alias_roh in _als_liste(element.get(listenfeld)):
            alias = _als_text(alias_roh)
            if alias is None:
                continue
            norm = normalisiere_name(alias)
            if not norm:
                logger.warning(
                    "%s: Alias %r für %s %s ist nach Normalisierung leer und wird ignoriert",
                    datei,
                    alias,
                    nummer_feld,
                    nummer,
                )
                continue
            if norm in normen_im_eintrag:
                continue
            normen_im_eintrag.add(norm)
            andere = gesehen.get((firma, norm))
            if andere is not None and andere != nummer:
                logger.warning(
                    "%s: Alias %r (Firma %s) zeigt sowohl auf %s als auch auf %s; der erste Eintrag gewinnt",
                    datei,
                    alias,
                    firma,
                    andere,
                    nummer,
                )
            gesehen.setdefault((firma, norm), nummer)
            eintraege.append(AliasEintrag(nummer, firma, alias, norm, anzeigename, art))
    return eintraege


def _ueberspringen_aus(daten: Mapping[str, object], datei: Path) -> list[AliasEintrag]:
    roh = daten.get("ueberspringen")
    if roh is None:
        return []
    if not isinstance(roh, list):
        raise AliasFehler(f"{datei}: 'ueberspringen' muss eine Liste sein")
    eintraege: list[AliasEintrag] = []
    for element in roh:
        text = _als_text(element)
        if text is None:
            continue
        norm = normalisiere_name(text)
        if not norm:
            logger.warning("%s: Überspringen-Eintrag %r ist nach Normalisierung leer", datei, text)
            continue
        eintraege.append(AliasEintrag("", "*", text, norm, None, "ueberspringen"))
    return eintraege


# ---------------------------------------------------------------------------
# Alias-Tabellen
# ---------------------------------------------------------------------------


class AliasTabellen:
    """Alle Alias-Tabellen eines ``config/aliase``-Ordners mit Suchfunktionen."""

    def __init__(
        self,
        kunden: Iterable[AliasEintrag] = (),
        lieferanten: Iterable[AliasEintrag] = (),
        produktmarken: Iterable[AliasEintrag] = (),
        artikel: Iterable[AliasEintrag] = (),
        ueberspringen: Iterable[AliasEintrag] = (),
        ordner: Path | None = None,
    ) -> None:
        self.kunden_eintraege: list[AliasEintrag] = list(kunden)
        self.lieferanten_eintraege: list[AliasEintrag] = list(lieferanten)
        self.marken_eintraege: list[AliasEintrag] = list(produktmarken)
        self.artikel_eintraege: list[AliasEintrag] = list(artikel)
        self.ueberspringen_eintraege: list[AliasEintrag] = list(ueberspringen)
        self.ordner = ordner

    @classmethod
    def lade(cls, alias_ordner: Path) -> AliasTabellen:
        """Liest kunden.yaml, lieferanten.yaml und artikel.yaml aus ``alias_ordner``.

        Fehlende Dateien ergeben leere Tabellen (Warnung), ungültige Dateien einen
        :class:`AliasFehler`.
        """
        alias_ordner = Path(alias_ordner)
        if not alias_ordner.is_dir():
            logger.warning("Alias-Ordner %s existiert nicht; alle Alias-Tabellen sind leer", alias_ordner)
        kunden_datei = alias_ordner / KUNDEN_DATEI
        lieferanten_datei = alias_ordner / LIEFERANTEN_DATEI
        artikel_datei = alias_ordner / ARTIKEL_DATEI

        kunden_roh = _lies_yaml(kunden_datei)
        lieferanten_roh = _lies_yaml(lieferanten_datei)
        artikel_roh = _lies_yaml(artikel_datei)

        tabellen = cls(
            kunden=_eintraege_aus(
                kunden_roh,
                wurzel="kunden",
                nummer_feld="kundennummer",
                listenfeld="aliase",
                art="alias",
                datei=kunden_datei,
            ),
            lieferanten=_eintraege_aus(
                lieferanten_roh,
                wurzel="lieferanten",
                nummer_feld="lieferantennummer",
                listenfeld="aliase",
                art="alias",
                datei=lieferanten_datei,
            ),
            produktmarken=_eintraege_aus(
                lieferanten_roh,
                wurzel="lieferanten",
                nummer_feld="lieferantennummer",
                listenfeld="produktmarken",
                art="produktmarke",
                datei=lieferanten_datei,
            ),
            artikel=_eintraege_aus(
                artikel_roh,
                wurzel="artikel",
                nummer_feld="artikelnummer",
                listenfeld="aliase",
                art="alias",
                datei=artikel_datei,
            ),
            ueberspringen=_ueberspringen_aus(artikel_roh, artikel_datei),
            ordner=alias_ordner,
        )
        logger.info("Aliase geladen aus %s: %r", alias_ordner, tabellen)
        return tabellen

    def __repr__(self) -> str:
        return (
            f"AliasTabellen({len(self.kunden_eintraege)} Kunden-, {len(self.lieferanten_eintraege)} Lieferanten-, "
            f"{len(self.marken_eintraege)} Marken-, {len(self.artikel_eintraege)} Artikel-Aliase, "
            f"{len(self.ueberspringen_eintraege)} Überspringen-Einträge)"
        )

    # -- Suche -----------------------------------------------------------------

    @staticmethod
    def _suche(eintraege: list[AliasEintrag], text: str | None, firma_id: str | None) -> list[AliasEintrag]:
        """Alle Einträge, deren Alias als Wortfolge im Text vorkommt; längster Alias zuerst,
        bei gleicher Länge in Dateireihenfolge."""
        text_norm = normalisiere_name(text)
        if not text_norm:
            return []
        treffer = [
            e
            for e in eintraege
            if (firma_id is None or e.firma_id == firma_id) and enthaelt_als_wort(text_norm, e.alias_norm)
        ]
        treffer.sort(key=lambda e: -len(e.alias_norm))
        return treffer

    @classmethod
    def _bester(
        cls, eintraege: list[AliasEintrag], text: str | None, firma_id: str | None, art: str
    ) -> AliasTreffer | None:
        treffer = cls._suche(eintraege, text, firma_id)
        if not treffer:
            return None
        bester = treffer[0]
        logger.debug(
            "%s-Alias %r in %r -> %s (Firma %s)", art, bester.alias, text, bester.nummer, bester.firma_id
        )
        return bester.treffer()

    @staticmethod
    def _eindeutig(treffer: list[AliasEintrag]) -> list[AliasTreffer]:
        gesehen: set[tuple[str, str]] = set()
        ergebnis: list[AliasTreffer] = []
        for e in treffer:
            schluessel = (e.firma_id, e.nummer)
            if schluessel in gesehen:
                continue
            gesehen.add(schluessel)
            ergebnis.append(e.treffer())
        return ergebnis

    def kunde(self, text: str | None, firma_id: str | None = None) -> AliasTreffer | None:
        """Bester Kunden-Alias im Text (optional auf eine Firma beschränkt)."""
        return self._bester(self.kunden_eintraege, text, firma_id, "Kunden")

    def kunden_treffer(self, text: str | None, firma_id: str | None = None) -> list[AliasTreffer]:
        """Alle Kunden (Nummer, Firma), deren Alias im Text vorkommt, längster Alias zuerst."""
        return self._eindeutig(self._suche(self.kunden_eintraege, text, firma_id))

    def lieferant(self, text: str | None, firma_id: str | None = None) -> AliasTreffer | None:
        """Bester Lieferanten-Alias im Text (optional auf eine Firma beschränkt)."""
        return self._bester(self.lieferanten_eintraege, text, firma_id, "Lieferanten")

    def lieferanten_treffer(self, text: str | None, firma_id: str | None = None) -> list[AliasTreffer]:
        return self._eindeutig(self._suche(self.lieferanten_eintraege, text, firma_id))

    def lieferant_fuer_marke(
        self, bezeichnung: str | None, firma_id: str | None = None
    ) -> AliasTreffer | None:
        """Lieferant anhand einer Produktmarke in der Artikelbezeichnung ("Villa Armellina ..." -> Cide)."""
        return self._bester(self.marken_eintraege, bezeichnung, firma_id, "Marken")

    def artikel(self, bezeichnung: str | None, firma_id: str | None = None) -> AliasTreffer | None:
        """Bester Artikel-Alias in der Bezeichnung (optional auf eine Firma beschränkt)."""
        return self._bester(self.artikel_eintraege, bezeichnung, firma_id, "Artikel")

    def ist_zu_ueberspringen(self, bezeichnung: str | None) -> bool:
        """``True``, wenn die Bezeichnung einen Eintrag der Überspringen-Liste enthält."""
        treffer = self._suche(self.ueberspringen_eintraege, bezeichnung, None)
        if treffer:
            logger.debug("Position %r steht auf der Überspringen-Liste (%r)", bezeichnung, treffer[0].alias)
            return True
        return False

    # -- Auskunft ----------------------------------------------------------------

    @staticmethod
    def _firmen_fuer(eintraege: list[AliasEintrag], nummer: str) -> list[str]:
        nummer = str(nummer).strip()
        firmen: list[str] = []
        for e in eintraege:
            if e.nummer == nummer and e.firma_id not in firmen:
                firmen.append(e.firma_id)
        return firmen

    def alle_firmen_fuer_kunde(self, nummer: str) -> list[str]:
        """Alle Firmen (Mandanten), in denen für diese Kundennummer Aliase definiert sind."""
        return self._firmen_fuer(self.kunden_eintraege, nummer)

    def alle_firmen_fuer_lieferant(self, nummer: str) -> list[str]:
        return self._firmen_fuer(self.lieferanten_eintraege + self.marken_eintraege, nummer)

    def aliase_fuer_kunde(self, nummer: str, firma_id: str | None = None) -> list[str]:
        nummer = str(nummer).strip()
        return [
            e.alias
            for e in self.kunden_eintraege
            if e.nummer == nummer and (firma_id is None or e.firma_id == firma_id)
        ]

    def aliase_fuer_artikel(self, nummer: str, firma_id: str | None = None) -> list[str]:
        nummer = str(nummer).strip()
        return [
            e.alias
            for e in self.artikel_eintraege
            if e.nummer == nummer and (firma_id is None or e.firma_id == firma_id)
        ]

    def firmen(self) -> set[str]:
        """Alle Firmen-IDs, die in irgendeiner Alias-Tabelle vorkommen."""
        return {
            e.firma_id
            for e in self.kunden_eintraege
            + self.lieferanten_eintraege
            + self.marken_eintraege
            + self.artikel_eintraege
        }


# ---------------------------------------------------------------------------
# YAML schreiben (Pflege aus der Freigabe-UI)
# ---------------------------------------------------------------------------


def _schreibe_yaml(pfad: Path, daten: Mapping[str, object], kopf: str) -> None:
    pfad.parent.mkdir(parents=True, exist_ok=True)
    text = kopf + yaml.safe_dump(
        dict(daten), allow_unicode=True, sort_keys=False, default_flow_style=False, width=100
    )
    temp = pfad.with_name(pfad.name + ".tmp")
    try:
        temp.write_text(text, encoding="utf-8")
        os.replace(temp, pfad)
    except OSError as exc:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
        raise AliasFehler(f"Alias-Datei {pfad} kann nicht geschrieben werden: {exc}") from exc


def _pruefe_text(wert: str | None, bezeichnung: str) -> str:
    text = _als_text(wert)
    if text is None:
        raise AliasFehler(f"{bezeichnung} darf nicht leer sein")
    return text


def _speichere_alias(
    pfad: Path,
    *,
    kopf: str,
    wurzel: str,
    nummer_feld: str,
    listenfeld: str,
    nummer: str,
    firma_id: str,
    alias: str,
    anzeigename: str | None,
    ueberschreiben: bool,
) -> Path:
    nummer = _pruefe_text(nummer, nummer_feld)
    firma_id = _pruefe_text(firma_id, "firma")
    alias = _pruefe_text(alias, "Alias")
    alias_norm = normalisiere_name(alias)
    if not alias_norm:
        raise AliasFehler(f"Alias {alias!r} enthält keine vergleichbaren Zeichen")

    daten = _lies_yaml(pfad, warnen_wenn_fehlt=False)
    liste = daten.get(wurzel)
    if liste is None:
        liste = []
        daten[wurzel] = liste
    if not isinstance(liste, list):
        raise AliasFehler(f"{pfad}: '{wurzel}' muss eine Liste sein")

    eigener: dict | None = None
    for element in liste:
        if not isinstance(element, dict):
            continue
        e_nummer = _als_text(element.get(nummer_feld))
        e_firma = _als_text(element.get("firma") or element.get("firma_id"))
        if e_firma != firma_id:
            continue
        if e_nummer == nummer:
            if eigener is None:
                eigener = element
            continue
        # anderer Eintrag derselben Firma: Konflikt?
        vorhandene = _als_liste(element.get(listenfeld))
        doppelte = [a for a in vorhandene if normalisiere_name(_als_text(a)) == alias_norm]
        if not doppelte:
            continue
        if not ueberschreiben:
            raise AliasKonflikt(
                f"Alias {alias!r} zeigt in Firma {firma_id} bereits auf {nummer_feld} {e_nummer} "
                f"(gewünscht: {nummer}); mit ueberschreiben=True verschieben"
            )
        element[listenfeld] = [a for a in vorhandene if a not in doppelte]
        logger.warning(
            "Alias %r in %s von %s %s nach %s verschoben (Firma %s)",
            alias,
            pfad.name,
            nummer_feld,
            e_nummer,
            nummer,
            firma_id,
        )

    if eigener is None:
        eigener = {nummer_feld: nummer, "firma": firma_id}
        if anzeigename:
            eigener["anzeigename"] = anzeigename
        eigener[listenfeld] = [alias]
        liste.append(eigener)
        logger.info(
            "Neuer Eintrag %s %s (Firma %s) mit Alias %r in %s",
            nummer_feld,
            nummer,
            firma_id,
            alias,
            pfad.name,
        )
    else:
        vorhandene = _als_liste(eigener.get(listenfeld))
        if anzeigename and not _als_text(eigener.get("anzeigename")):
            eigener["anzeigename"] = anzeigename
        if any(normalisiere_name(_als_text(a)) == alias_norm for a in vorhandene):
            logger.info(
                "Alias %r für %s %s (Firma %s) ist bereits vorhanden", alias, nummer_feld, nummer, firma_id
            )
            eigener[listenfeld] = vorhandene
        else:
            eigener[listenfeld] = [*vorhandene, alias]
            logger.info(
                "Alias %r für %s %s (Firma %s) in %s ergänzt", alias, nummer_feld, nummer, firma_id, pfad.name
            )

    _schreibe_yaml(pfad, daten, kopf)
    return pfad


def speichere_alias_kunde(
    alias_ordner: Path,
    kundennummer: str,
    firma_id: str,
    alias: str,
    *,
    anzeigename: str | None = None,
    ueberschreiben: bool = False,
) -> Path:
    """Ergänzt ``kunden.yaml`` um einen Alias für ``kundennummer`` in ``firma_id``.

    Zeigt der Alias in derselben Firma schon auf eine andere Kundennummer, wird ein
    :class:`AliasKonflikt` ausgelöst, es sei denn ``ueberschreiben=True`` (dann wird er verschoben).
    Liefert den Pfad der geschriebenen Datei.
    """
    return _speichere_alias(
        Path(alias_ordner) / KUNDEN_DATEI,
        kopf=_KOPF_KUNDEN,
        wurzel="kunden",
        nummer_feld="kundennummer",
        listenfeld="aliase",
        nummer=kundennummer,
        firma_id=firma_id,
        alias=alias,
        anzeigename=anzeigename,
        ueberschreiben=ueberschreiben,
    )


def speichere_alias_lieferant(
    alias_ordner: Path,
    lieferantennummer: str,
    firma_id: str,
    alias: str | None = None,
    *,
    produktmarke: str | None = None,
    anzeigename: str | None = None,
    ueberschreiben: bool = False,
) -> Path:
    """Ergänzt ``lieferanten.yaml`` um einen Alias und/oder eine Produktmarke für einen Lieferanten."""
    if alias is None and produktmarke is None:
        raise AliasFehler("Es muss ein Alias oder eine Produktmarke angegeben werden")
    pfad = Path(alias_ordner) / LIEFERANTEN_DATEI
    if alias is not None:
        _speichere_alias(
            pfad,
            kopf=_KOPF_LIEFERANTEN,
            wurzel="lieferanten",
            nummer_feld="lieferantennummer",
            listenfeld="aliase",
            nummer=lieferantennummer,
            firma_id=firma_id,
            alias=alias,
            anzeigename=anzeigename,
            ueberschreiben=ueberschreiben,
        )
    if produktmarke is not None:
        _speichere_alias(
            pfad,
            kopf=_KOPF_LIEFERANTEN,
            wurzel="lieferanten",
            nummer_feld="lieferantennummer",
            listenfeld="produktmarken",
            nummer=lieferantennummer,
            firma_id=firma_id,
            alias=produktmarke,
            anzeigename=anzeigename,
            ueberschreiben=ueberschreiben,
        )
    return pfad


def speichere_alias_artikel(
    alias_ordner: Path,
    artikelnummer: str,
    firma_id: str,
    alias: str,
    *,
    anzeigename: str | None = None,
    ueberschreiben: bool = False,
) -> Path:
    """Ergänzt ``artikel.yaml`` um einen Alias für ``artikelnummer`` in ``firma_id``."""
    return _speichere_alias(
        Path(alias_ordner) / ARTIKEL_DATEI,
        kopf=_KOPF_ARTIKEL,
        wurzel="artikel",
        nummer_feld="artikelnummer",
        listenfeld="aliase",
        nummer=artikelnummer,
        firma_id=firma_id,
        alias=alias,
        anzeigename=anzeigename,
        ueberschreiben=ueberschreiben,
    )


def speichere_ueberspringen(alias_ordner: Path, bezeichnung: str) -> Path:
    """Nimmt eine Bezeichnung in die Überspringen-Liste von ``artikel.yaml`` auf."""
    bezeichnung = _pruefe_text(bezeichnung, "Bezeichnung")
    norm = normalisiere_name(bezeichnung)
    if not norm:
        raise AliasFehler(f"Bezeichnung {bezeichnung!r} enthält keine vergleichbaren Zeichen")
    pfad = Path(alias_ordner) / ARTIKEL_DATEI
    daten = _lies_yaml(pfad, warnen_wenn_fehlt=False)
    liste = daten.get("ueberspringen")
    if liste is None:
        liste = []
        daten["ueberspringen"] = liste
    if not isinstance(liste, list):
        raise AliasFehler(f"{pfad}: 'ueberspringen' muss eine Liste sein")
    if any(normalisiere_name(_als_text(e)) == norm for e in liste):
        logger.info("%r steht bereits auf der Überspringen-Liste", bezeichnung)
    else:
        liste.append(bezeichnung)
        logger.info("%r in die Überspringen-Liste aufgenommen", bezeichnung)
    _schreibe_yaml(pfad, daten, _KOPF_ARTIKEL)
    return pfad
