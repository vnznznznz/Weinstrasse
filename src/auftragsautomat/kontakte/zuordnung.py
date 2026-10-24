"""Kontakte zuordnen: Lieferant/Kunde aus Lexware -> Ansprechpartner, Spedition je Kunde,
Postfächer der Spedition nach Zweck, Anrede und Sprache, CC-Empfänger
(docs/AUSBAUSTUFE_2.md, Abschnitt 2.3).

Alle Firmennamen werden mit :func:`~auftragsautomat.matching.aliase.normalisiere_name`
verglichen (Rechtsformen, Umlaute, Satzzeichen egal) und unscharf bewertet
(:func:`~auftragsautomat.matching.aliase.aehnlichkeit`, 0..100). Mehrfachfirmen wie
``"Cantina Alpha & Cantina Beta"`` werden an ``&``, ``+``, ``/``, ``;`` zerlegt, jeder Teil zählt.
Ein Treffer gilt ab ``settings.matching.schwelle_sicher`` und nur, wenn kein zweiter Kandidat
gleichauf liegt; unsichere Treffer werden nur protokolliert, nie geraten (ein falscher
Empfänger wäre schlimmer als kein Empfänger).

Postfächer der Spedition sind **nicht** fest kodiert: ``postfach_spedition`` findet sie über den
Positions-/Namenstext der Kontaktliste ("Neue Aufträge", "Sendungsnachfragen|Status", "EMCS|Zoll",
"Buchhaltung"); gibt es keins, wird der erste Disponent genommen, sonst ``None``.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from auftragsautomat.benennung import normalisiere
from auftragsautomat.config import Settings
from auftragsautomat.kontakte.liste import Kontaktliste, bereinige_speditionsname, sprache_fuer_land
from auftragsautomat.matching.aliase import aehnlichkeit, enthaelt_als_wort, normalisiere_name
from auftragsautomat.models import Kontakt, Kontaktgruppe, Kunde, Lieferant
from auftragsautomat.stammdaten.basis import Stammdaten

__all__ = [
    "ABSTAND_EINDEUTIG",
    "ZWECKE",
    "Geschlecht",
    "Person",
    "Zuordner",
    "Zweck",
    "firmenscore",
    "firmenvarianten",
    "person_aus_name",
    "positionsrang",
]

logger = logging.getLogger(__name__)

Zweck = Literal["neue_auftraege", "status", "zoll", "buchhaltung"]
ZWECKE: tuple[str, ...] = ("neue_auftraege", "status", "zoll", "buchhaltung")
Geschlecht = Literal["w", "m", "unbekannt", "neutral"]

#: Zwei Firmen mit Score-Abstand unter diesem Wert gelten als mehrdeutig (kein Treffer).
ABSTAND_EINDEUTIG = 1.0

# Positions-/Namenstexte (normalisiert: casefold, ae/oe/ue) je Zweck eines Speditionspostfachs.
_ZWECK_MUSTER: dict[str, re.Pattern[str]] = {
    "neue_auftraege": re.compile(
        r"neue?[ -]?auftr|new[ -]?orders?|auftrags?[ -]?postfach|auftragsannahme|auftragseingang|bestellung|\border"
    ),
    "status": re.compile(
        r"sendungsnachfrage|nachfrage|status|info[ -]?postfach|tracking|sendungsverfolgung|\binfo\b|kundenservice|customer[ -]?service"
    ),
    "zoll": re.compile(r"emcs|zoll|customs|verbrauchsteuer|steuerlager|excise"),
    "buchhaltung": re.compile(r"buchhaltung|rechnung|accounting|invoice|debitor|kreditor|finanz|abrechnung"),
}
_DISPO_RE = re.compile(r"dispo|disposition|dispatch|disponent")
_POSTFACH_RE = re.compile(r"postfach|mailbox|zentrale|sammel")
_ORDERS_RE = re.compile(r"\border|bestell|auftr|ordini|commande|pedido")
_ALLGEMEIN_RE = re.compile(r"^(allgemein|alle|general|gesamt|alles|-|–)$")
_TRENNER_FIRMEN = re.compile(r"\s*(?:&|\+|/|;|\|)\s*")
_ABTEILUNG_RE = re.compile(
    r"postfach|mailbox|buchhaltung|zentrale|\bteam\b|abteilung|\boffice\b|\binfo\b|service|support|dispo|"
    r"rechnung|einkauf|verkauf|vertrieb|logistik|export|import|\borders?\b|ordini|commandes?|accounting|"
    r"\bsales\b|department|kundenservice|sekretariat|empfang|@"
)
_TITEL_RE = re.compile(
    r"^(?:dr|prof|dipl|ing|mag|mba|msc|bsc|ma|ba|med|jur|rer|nat|phil|h\.c|dott|dott\.ssa|dottssa|avv|"
    r"dipl\.-ing|dipl-ing|rae|ra|stb|wp)\.?,?$",
    re.IGNORECASE,
)
_ANREDE_PRAEFIX: dict[str, Geschlecht] = {
    "frau": "w",
    "fr": "w",
    "herr": "m",
    "herrn": "m",
    "hr": "m",
    "mr": "m",
    "mister": "m",
    "mrs": "w",
    "ms": "w",
    "miss": "w",
    "sig": "m",
    "signor": "m",
    "signora": "w",
    "sigra": "w",
    "sig.ra": "w",
    "m": "m",
    "mme": "w",
    "mlle": "w",
    "madame": "w",
    "monsieur": "m",
    "sr": "m",
    "sra": "w",
    "señor": "m",
    "señora": "w",
    "senor": "m",
    "senora": "w",
}
_PARTIKEL = {"von", "van", "de", "di", "da", "del", "della", "der", "den", "du", "la", "le", "zu", "zur", "vom", "dos", "das"}

# Geläufige Vornamen (DE/IT/FR/ES/EN) für die Anrede-Heuristik; keine Personendaten, nur Namenslisten.
_VORNAMEN_W = {
    "anna", "anne", "angela", "angelika", "anja", "anke", "antje", "barbara", "beate", "birgit",
    "brigitte", "carmen", "caroline", "carolin", "christa", "christiane", "christina", "christine", "claudia",
    "cornelia", "dagmar", "daniela", "diana", "doris", "elena", "elisabeth", "elke", "erika", "eva", "franziska",
    "gabriele", "gisela", "gudrun", "hannah", "hanna", "heike", "helga", "ilona", "inge", "ingrid", "irene", "iris",
    "jana", "janina", "jasmin", "jennifer", "jessica", "johanna", "julia", "jutta", "karin", "karina", "katharina",
    "kathrin", "katja", "katrin", "kerstin", "kirsten", "klara", "kristina", "laura", "lea", "lena", "lisa", "luisa",
    "manuela", "maren", "margit", "maria", "marianne", "marie", "marion", "martina", "melanie", "michaela", "monika",
    "nadine", "nicole", "nina", "petra", "regina", "renate", "rita", "sabine", "sabrina", "sandra", "sarah", "silke",
    "silvia", "simone", "sonja", "stefanie", "stephanie", "susanne", "svenja", "tanja", "ulrike", "ursula", "ute",
    "vanessa", "vera", "verena", "yvonne", "alessandra", "alessia", "beatrice", "chiara", "cristina", "elisa",
    "federica", "francesca", "giulia", "giovanna", "ilaria", "lucia", "marta", "paola", "roberta", "sara",
    "serena", "simona", "stefania", "valentina", "valeria", "agnès", "agnes", "amélie", "amelie", "camille",
    "céline", "celine", "charlotte", "chloé", "claire", "élodie", "elodie", "émilie", "emilie", "hélène", "helene",
    "isabelle", "julie", "lucie", "manon", "mathilde", "nathalie", "pauline", "sophie", "sylvie", "valérie", "valerie",
    "ana", "beatriz", "carla", "carolina", "dolores", "isabel", "lucía", "marina", "mercedes",
    "montserrat", "nuria", "pilar", "rosa", "teresa", "alice", "amy", "emma", "jane", "joanne", "karen", "kate",
    "linda", "lucy", "mary", "rachel", "rebecca", "susan", "victoria",
}
_VORNAMEN_M = {
    "alexander", "andreas", "andrea", "armin", "axel", "benjamin", "bernd", "bernhard", "björn", "bjoern", "carsten",
    "christian", "christoph", "daniel", "david", "dennis", "dieter", "dirk", "dominik", "erik", "fabian", "felix",
    "florian", "frank", "franz", "friedrich", "georg", "gerd", "gerhard", "guido", "günter", "guenter", "hans",
    "harald", "heiko", "heinz", "helmut", "henning", "holger", "horst", "ingo", "jan", "jens", "joachim", "jochen",
    "johannes", "jonas", "jörg", "joerg", "josef", "jürgen", "juergen", "kai", "karl", "karsten", "klaus", "lars",
    "lukas", "lutz", "manfred", "marcel", "marco", "mario", "markus", "martin", "mathias", "matthias", "max",
    "maximilian", "michael", "moritz", "nils", "norbert", "olaf", "oliver", "patrick", "paul", "peter", "philipp",
    "rainer", "ralf", "reinhard", "rené", "rene", "robert", "roland", "rolf", "rüdiger", "ruediger", "sascha",
    "sebastian", "simon", "stefan", "steffen", "stephan", "sven", "thomas", "thorsten", "tim", "tobias", "torsten",
    "udo", "ulrich", "uwe", "volker", "walter", "werner", "wolfgang", "alberto", "alessandro", "antonio",
    "carlo", "claudio", "davide", "diego", "emanuele", "enrico", "fabio", "federico", "filippo", "francesco",
    "gianluca", "giorgio", "giovanni", "giuseppe", "jacopo", "lorenzo", "luca", "luciano", "luigi", "massimo", "matteo", "mattia", "michele", "nicola", "paolo", "pietro", "riccardo", "roberto", "salvatore",
    "stefano", "tommaso", "umberto", "vincenzo", "vittorio", "alain", "antoine", "arnaud", "benoît",
    "benoit", "bruno", "christophe", "cédric", "cedric", "didier", "dominique", "éric", "eric", "étienne", "etienne",
    "fabrice", "françois", "francois", "frédéric", "frederic", "guillaume", "hervé", "herve", "jacques", "jean",
    "jérôme", "jerome", "julien", "laurent", "marc", "mathieu", "matthieu", "michel", "nicolas", "olivier",
    "pascal", "philippe", "pierre", "sébastien", "sebastien", "stéphane", "stephane", "thierry", "vincent",
    "xavier", "yves", "alejandro", "álvaro", "alvaro", "carlos", "enrique", "fernando", "francisco", "javier",
    "joaquín", "joaquin", "jorge", "josé", "jose", "juan", "luis", "manuel", "miguel", "pablo", "pedro", "rafael",
    "ramón", "ramon", "raúl", "raul", "sergio", "adam", "andrew", "anthony", "brian", "charles", "chris",
    "christopher", "edward", "george", "henry", "jack", "james", "john", "joseph", "mark", "matthew", "nick",
    "richard", "steve", "steven", "william",
}
# Mehrdeutige Vornamen (deutsch weiblich, italienisch männlich: "Andrea", "Simone") stehen in keiner
# Liste; "Andrea", "Luca", "Nicola" gelten als männlich (romanische Namen auf -a).
for _mehrdeutig in ("andrea", "simone", "simone_it"):
    _VORNAMEN_W.discard(_mehrdeutig)
    _VORNAMEN_M.discard(_mehrdeutig)
_MAENNLICH_AUF_A = {"luca", "andrea", "nicola", "mattia", "elia", "joshua", "sascha", "micha", "jona", "noah", "ezra"}


# ---------------------------------------------------------------------------
# Personen und Anrede
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Person:
    """Aus einem Namensfeld abgeleitete Person (oder Abteilung/Postfach, dann ``neutral``)."""

    vorname: str | None
    nachname: str | None
    geschlecht: Geschlecht
    voller_name: str | None

    @property
    def ist_person(self) -> bool:
        return self.geschlecht != "neutral" and bool(self.voller_name)


def _geschlecht_aus_vorname(vorname: str | None) -> Geschlecht:
    if not vorname:
        return "unbekannt"
    v = vorname.casefold().strip(".")
    if v in _MAENNLICH_AUF_A:
        return "m"
    if v in _VORNAMEN_W:
        return "w"
    if v in _VORNAMEN_M:
        return "m"
    if len(v) >= 4 and v.endswith("a"):
        return "w"
    if len(v) >= 4 and v.endswith("o"):
        return "m"
    return "unbekannt"


def person_aus_name(name: str | None, *, anrede: str | None = None) -> Person:
    """Zerlegt "Frau Beispiel", "Dr. Giulia Esempio", "Auftrags-Postfach" in Vorname/Nachname/Geschlecht.

    Abteilungen und Postfächer (Buchhaltung, Zentrale, Team …) ergeben ``neutral``. Das Geschlecht
    kommt aus ``anrede`` ("Frau"/"Herr"/"Ms"/"Mr"), aus einem Namenspräfix, aus einer Vornamensliste
    oder aus der Endung (-a weiblich, -o männlich bei romanischen Namen); sonst ``unbekannt``.
    """
    text = re.sub(r"\s+", " ", (name or "").replace(",", " ")).strip()
    if not text:
        return Person(None, None, "neutral", None)
    if _ABTEILUNG_RE.search(normalisiere(text)):
        return Person(None, None, "neutral", text)
    geschlecht: Geschlecht = "unbekannt"
    if anrede:
        geschlecht = _ANREDE_PRAEFIX.get(normalisiere(anrede).replace("dear ", "").strip(" ."), "unbekannt")
    tokens = text.split(" ")
    erstes = tokens[0].casefold().rstrip(".")
    if erstes in _ANREDE_PRAEFIX and len(tokens) > 1:
        if geschlecht == "unbekannt":
            geschlecht = _ANREDE_PRAEFIX[erstes]
        tokens = tokens[1:]
    tokens = [t for t in tokens if not _TITEL_RE.match(t)]
    if not tokens:
        return Person(None, None, "neutral", text)
    if len(tokens) == 1:
        einzel = tokens[0]
        if geschlecht != "unbekannt":
            return Person(None, einzel, geschlecht, einzel)
        g = _geschlecht_aus_vorname(einzel)
        if g != "unbekannt":
            return Person(einzel, None, g, einzel)
        return Person(None, einzel, "unbekannt", einzel)
    vorname = tokens[0]
    rest = tokens[1:]
    nachname = " ".join(rest)
    if geschlecht == "unbekannt":
        geschlecht = _geschlecht_aus_vorname(vorname)
    return Person(vorname, nachname, geschlecht, " ".join(tokens))


def _anrede_text(person: Person, sprache: str) -> str:
    if person.geschlecht == "neutral" or not person.voller_name:
        return "Dear Sir or Madam" if sprache == "en" else "Sehr geehrte Damen und Herren"
    nachname = person.nachname or person.voller_name
    if sprache == "en":
        if person.geschlecht == "w":
            return f"Dear Ms {nachname}"
        if person.geschlecht == "m":
            return f"Dear Mr {nachname}"
        return f"Dear {person.voller_name}"
    if person.geschlecht == "w":
        return f"Sehr geehrte Frau {nachname}"
    if person.geschlecht == "m":
        return f"Sehr geehrter Herr {nachname}"
    return f"Guten Tag {person.voller_name}"


def _anrede_kurz(person: Person, sprache: str) -> str | None:
    """Wert für ``Kontakt.anrede``: "Frau"/"Herr" bzw. "Ms"/"Mr", sonst ``None``."""
    if person.geschlecht == "w":
        return "Ms" if sprache == "en" else "Frau"
    if person.geschlecht == "m":
        return "Mr" if sprache == "en" else "Herr"
    return None


# ---------------------------------------------------------------------------
# Rangfolgen
# ---------------------------------------------------------------------------


def positionsrang(position: str | None, name: str | None = None) -> int:
    """Reihenfolge für Kundenkontakte: Dispo/Logistik (0) vor Einkauf/Orders (1) vor Sonstigen (2)
    vor Buchhaltung (3)."""
    t = normalisiere(f"{position or ''} {name or ''}")
    if _DISPO_RE.search(t) or re.search(r"logistik|versand|wareneingang|warenannahme", t):
        return 0
    if re.search(r"einkauf|\border|bestell|category|sortiment|purchas|ordini|commande|pedido", t):
        return 1
    if re.search(r"buchhaltung|rechnung|accounting|finanz|debitor|kreditor|invoice", t):
        return 3
    return 2


def _ist_allgemein(produkt: str | None) -> bool:
    return not produkt or bool(_ALLGEMEIN_RE.match(normalisiere(produkt)))


# ---------------------------------------------------------------------------
# Unscharfer Firmenvergleich
# ---------------------------------------------------------------------------


def firmenvarianten(firma: str) -> list[str]:
    """Normalisierte Vergleichsformen einer Firma: ganzer Name plus jeder Teil einer Mehrfachfirma."""
    normen = [normalisiere_name(firma)]
    for teil in _TRENNER_FIRMEN.split(firma):
        n = normalisiere_name(teil)
        if n and n not in normen:
            normen.append(n)
    return [n for n in normen if n]


#: Gattungswörter, die allein nie als Wort-Treffer zählen ("Weingut" trifft nicht jedes Weingut).
_GENERISCHE_WOERTER = {
    "weingut", "winery", "cantina", "cantine", "domaine", "chateau", "azienda", "tenuta", "bodega",
    "bodegas", "vini", "vino", "wines", "wine", "vins", "spedition", "logistik", "logistics", "weinhandlung",
    "weinhaus", "vinothek", "markt", "handel", "agentur", "gruppe", "group", "import", "export",
}


def firmenscore(text_norm: str, varianten: Sequence[str]) -> float:
    """Bester Score 0..100 eines Texts gegen die Varianten; ein ganzes Wort bzw. eine ganze Wortfolge
    (ab 4 Zeichen, kein Gattungswort) als Teil der anderen Seite zählt mindestens 95."""
    if not text_norm:
        return 0.0
    bester = 0.0
    for v in varianten:
        s = aehnlichkeit(text_norm, v)
        kurz, lang = (text_norm, v) if len(text_norm) <= len(v) else (v, text_norm)
        if len(kurz) >= 4 and kurz not in _GENERISCHE_WOERTER and enthaelt_als_wort(lang, kurz):
            s = max(s, 95.0)
        bester = max(bester, s)
    return bester


def _texte(partner: Kunde | Lieferant | str | None) -> list[str]:
    if partner is None:
        return []
    if isinstance(partner, str):
        return [partner] if partner.strip() else []
    texte = [partner.matchcode, partner.name, partner.name2]
    if partner.name and partner.name2:
        texte.append(f"{partner.name} {partner.name2}")
    return [t for t in texte if t and t.strip()]


# ---------------------------------------------------------------------------
# Zuordner
# ---------------------------------------------------------------------------


class Zuordner:
    """Zuordnung zwischen Kontaktliste, Lexware-Stammdaten und Einstellungen.

    ``stammdaten`` (Firmen-ID -> :class:`Stammdaten`) ist optional; damit werden die Kontakte der
    Lieferanten/Kunden mit Lexware-Nummern verknüpft (``Kontakt.lexware_nummer``; bei mehreren
    Firmen die erste in alphabetischer Reihenfolge, Details über :meth:`nummer_fuer`).
    ``anrede_regeln`` (E-Mail -> ``{"anrede": …, "sprache": …}``) überschreibt die Heuristik,
    z. B. aus ``config/anrede.yaml`` des Kommunikationsmoduls.
    """

    def __init__(
        self,
        kontaktliste: Kontaktliste,
        stammdaten: Mapping[str, Stammdaten] | None,
        settings: Settings,
        *,
        anrede_regeln: Mapping[str, Mapping[str, str]] | None = None,
    ) -> None:
        self.liste = kontaktliste
        self.settings = settings
        self.stammdaten: dict[str, Stammdaten] = dict(stammdaten or {})
        self.anrede_regeln = {k.strip().lower(): dict(v) for k, v in (anrede_regeln or {}).items()}
        self._lieferanten_varianten = {firma: firmenvarianten(firma) for firma in kontaktliste.lieferanten}
        self._kunden_varianten = {firma: firmenvarianten(firma) for firma in kontaktliste.kunden}
        # Kontaktfirma -> {firma_id: Lexware-Nummer} und Rückweg (firma_id, nummer) -> Kontaktfirma
        self.nummern_lieferanten: dict[str, dict[str, str]] = {}
        self.nummern_kunden: dict[str, dict[str, str]] = {}
        self._firma_fuer_lieferantennummer: dict[tuple[str, str], str] = {}
        self._firma_fuer_kundennummer: dict[tuple[str, str], str] = {}
        if self.stammdaten:
            self._verknuepfe_stammdaten()

    # -- Stammdaten -------------------------------------------------------------------

    @property
    def _sicher(self) -> float:
        return self.settings.matching.schwelle_sicher

    @property
    def _kandidat(self) -> float:
        return self.settings.matching.schwelle_kandidat

    def _bester_partner(self, varianten: Sequence[str], partner: Iterable[Kunde | Lieferant]) -> tuple[str | None, float]:
        bester, score, zweiter = None, 0.0, 0.0
        for p in partner:
            s = max((firmenscore(normalisiere_name(t), varianten) for t in _texte(p)), default=0.0)
            if s > score:
                bester, score, zweiter = p, s, score
            elif s > zweiter:
                zweiter = s
        if bester is None or score < self._sicher:
            return None, score
        if zweiter >= self._sicher and score - zweiter < ABSTAND_EINDEUTIG:
            return None, score
        nummer = bester.lieferantennummer if isinstance(bester, Lieferant) else bester.kundennummer
        return nummer, score

    def _verknuepfe_stammdaten(self) -> None:
        for firma_id in sorted(self.stammdaten):
            stamm = self.stammdaten[firma_id]
            for firma, varianten in self._lieferanten_varianten.items():
                nummer, _ = self._bester_partner(varianten, stamm.lieferanten)
                if nummer:
                    self.nummern_lieferanten.setdefault(firma, {})[firma_id] = nummer
                    self._firma_fuer_lieferantennummer[(firma_id, nummer)] = firma
            for firma, varianten in self._kunden_varianten.items():
                nummer, _ = self._bester_partner(varianten, stamm.kunden)
                if nummer:
                    self.nummern_kunden.setdefault(firma, {})[firma_id] = nummer
                    self._firma_fuer_kundennummer[(firma_id, nummer)] = firma
        for firma, nummern in self.nummern_lieferanten.items():
            erste = nummern[sorted(nummern)[0]]
            for kontakt in self.liste.lieferanten.get(firma, []):
                kontakt.lexware_nummer = kontakt.lexware_nummer or erste
        for firma, nummern in self.nummern_kunden.items():
            erste = nummern[sorted(nummern)[0]]
            for kontakt in self.liste.kunden.get(firma, []):
                kontakt.lexware_nummer = kontakt.lexware_nummer or erste
        logger.info(
            "Kontaktliste mit Lexware verknüpft: %d Lieferanten, %d Kunden mit Nummer",
            len(self.nummern_lieferanten),
            len(self.nummern_kunden),
        )

    def nummer_fuer(self, kontakt: Kontakt, firma_id: str) -> str | None:
        """Lexware-Nummer des Kontakts (Lieferant oder Kunde) in der Firma ``firma_id``."""
        if kontakt.gruppe == Kontaktgruppe.LIEFERANT:
            return self.nummern_lieferanten.get(kontakt.firma, {}).get(firma_id)
        if kontakt.gruppe == Kontaktgruppe.KUNDE:
            return self.nummern_kunden.get(kontakt.firma, {}).get(firma_id)
        return None

    # -- Firma finden -------------------------------------------------------------------

    def _finde_firma(self, varianten_je_firma: Mapping[str, Sequence[str]], texte: Sequence[str], was: str) -> str | None:
        if not texte or not varianten_je_firma:
            return None
        texte_norm = [normalisiere_name(t) for t in texte]
        bewertungen = sorted(
            ((max(firmenscore(t, v) for t in texte_norm), firma) for firma, v in varianten_je_firma.items()),
            key=lambda b: (-b[0], b[1]),
        )
        score, firma = bewertungen[0]
        zweiter = bewertungen[1][0] if len(bewertungen) > 1 else 0.0
        if score >= self._sicher:
            if zweiter >= self._sicher and score - zweiter < ABSTAND_EINDEUTIG:
                logger.warning(
                    "%s '%s' in der Kontaktliste mehrdeutig: '%s' und '%s' (je %.0f) – kein Kontakt zugeordnet.",
                    was,
                    texte[0],
                    firma,
                    bewertungen[1][1],
                    score,
                )
                return None
            return firma
        if score >= self._kandidat:
            logger.warning(
                "%s '%s' in der Kontaktliste nur unsicher gefunden ('%s', %.0f < %.0f) – kein Kontakt zugeordnet.",
                was,
                texte[0],
                firma,
                score,
                self._sicher,
            )
        else:
            logger.info("%s '%s' nicht in der Kontaktliste.", was, texte[0])
        return None

    def firma_fuer_lieferant(self, lieferant: Lieferant | str | None) -> str | None:
        """Schlüssel in ``Kontaktliste.lieferanten`` für einen Lexware-Lieferanten oder einen Text."""
        if isinstance(lieferant, Lieferant):
            direkt = self._firma_fuer_lieferantennummer.get((lieferant.firma_id, lieferant.lieferantennummer))
            if direkt:
                return direkt
        return self._finde_firma(self._lieferanten_varianten, _texte(lieferant), "Lieferant")

    def firma_fuer_kunde(self, kunde: Kunde | str | None) -> str | None:
        """Schlüssel in ``Kontaktliste.kunden`` für einen Lexware-Kunden oder einen Text."""
        if isinstance(kunde, Kunde):
            direkt = self._firma_fuer_kundennummer.get((kunde.firma_id, kunde.kundennummer))
            if direkt:
                return direkt
        return self._finde_firma(self._kunden_varianten, _texte(kunde), "Kunde")

    # -- Lieferant ----------------------------------------------------------------------

    def kontakte_fuer_lieferant(self, lieferant: Lieferant | str | None) -> list[Kontakt]:
        """Alle Kontakte des Weinguts, Position "Orders" zuerst, danach in Listenreihenfolge."""
        firma = self.firma_fuer_lieferant(lieferant)
        if firma is None:
            return []
        kontakte = self.liste.lieferanten.get(firma, [])
        return sorted(kontakte, key=lambda k: (0 if _ORDERS_RE.search(normalisiere(k.position)) else 1))

    def kontakt_fuer_lieferant(self, lieferant: Lieferant | str | None) -> Kontakt | None:
        """Der Ansprechpartner für Bestellungen ans Weingut (Position "Orders" bevorzugt), sonst ``None``."""
        kontakte = self.kontakte_fuer_lieferant(lieferant)
        return kontakte[0] if kontakte else None

    # -- Kunde ----------------------------------------------------------------------------

    def _produkt_passt(self, produkt: str | None, lieferant_norm: str, lieferant_tokens: list[str]) -> bool:
        if not lieferant_norm or _ist_allgemein(produkt):
            return False
        pn = normalisiere_name(produkt)
        if not pn:
            return False
        if aehnlichkeit(pn, lieferant_norm) >= self._kandidat:
            return True
        return any(len(t) >= 4 and enthaelt_als_wort(pn, t) for t in lieferant_tokens)

    def kontakte_fuer_kunde(self, kunde: Kunde | str | None, lieferant: Lieferant | str | None = None) -> list[Kontakt]:
        """Kontakte des Kunden in Empfängerreihenfolge: Produktzeile passend zum Lieferanten vor
        "Allgemein", innerhalb Dispo vor Einkauf vor Sonstigen vor Buchhaltung. Zeilen anderer
        Produkte entfallen, wenn ein Lieferant angegeben ist und passende Zeilen existieren."""
        firma = self.firma_fuer_kunde(kunde)
        if firma is None:
            return []
        kontakte = self.liste.kunden.get(firma, [])
        lieferant_texte = _texte(lieferant)
        lieferant_norm = normalisiere_name(lieferant_texte[0]) if lieferant_texte else ""
        lieferant_tokens = lieferant_norm.split()

        def produktrang(k: Kontakt) -> int:
            if _ist_allgemein(k.produkt):
                return 1
            if self._produkt_passt(k.produkt, lieferant_norm, lieferant_tokens):
                return 0
            return 2

        bewertet = [(produktrang(k), positionsrang(k.position, k.name), i, k) for i, k in enumerate(kontakte)]
        if lieferant_norm:
            passend = [b for b in bewertet if b[0] < 2]
            if passend:
                bewertet = passend
        return [b[3] for b in sorted(bewertet, key=lambda b: (b[0], b[1], b[2]))]

    def spedition_fuer_kunde(self, kunde: Kunde | str | None, lieferant: Lieferant | str | None = None) -> str:
        """Standard-Spedition des Kunden aus der Spalte "Spedition" (Klammerzusatz entfernt, Schreibweise
        des Speditionsblatts), sonst ``settings.kontakte.standard_spedition``."""
        for kontakt in self.kontakte_fuer_kunde(kunde, lieferant):
            name = bereinige_speditionsname(kontakt.spedition)
            if name:
                return self.liste.finde_spedition(name) or name
        return self.settings.kontakte.standard_spedition

    # -- Spedition --------------------------------------------------------------------------

    def postfach_spedition(self, spedition: str | None, zweck: Zweck) -> Kontakt | None:
        """Postfach/Ansprechpartner der Spedition für einen Zweck ("neue_auftraege", "status", "zoll",
        "buchhaltung") aus dem Positions-/Namenstext; Postfächer vor Personen. Ohne Treffer der erste
        Disponent, sonst ``None``. Nichts ist fest kodiert."""
        if zweck not in _ZWECK_MUSTER:
            raise ValueError(f"Unbekannter Zweck '{zweck}'. Erlaubt: {', '.join(ZWECKE)}")
        kontakte = self.liste.kontakte_der_spedition(spedition)
        if not kontakte:
            logger.warning("Spedition '%s' nicht in der Kontaktliste.", spedition)
            return None
        muster = _ZWECK_MUSTER[zweck]
        treffer = [k for k in kontakte if muster.search(normalisiere(f"{k.name or ''} {k.position or ''}"))]
        if treffer:
            treffer.sort(key=lambda k: 0 if _POSTFACH_RE.search(normalisiere(f"{k.name or ''} {k.position or ''}")) else 1)
            return treffer[0]
        for k in kontakte:
            if _DISPO_RE.search(normalisiere(f"{k.name or ''} {k.position or ''}")):
                logger.info("Spedition '%s': kein Postfach für '%s', nehme Disponent %s.", spedition, zweck, k.name)
                return k
        logger.warning("Spedition '%s': weder Postfach für '%s' noch Disponent in der Kontaktliste.", spedition, zweck)
        return None

    # -- Anrede ------------------------------------------------------------------------------

    def sprache_fuer(self, kontakt: Kontakt) -> Literal["de", "en"]:
        """Regel aus ``anrede_regeln``, sonst Land (Lieferanten außerhalb DE/AT/CH englisch), sonst
        ``Kontakt.sprache``."""
        regel = self.anrede_regeln.get((kontakt.email or "").lower())
        if regel and regel.get("sprache") in ("de", "en"):
            return regel["sprache"]  # type: ignore[return-value]
        if kontakt.land:
            return sprache_fuer_land(kontakt.land, standard=kontakt.sprache)
        return kontakt.sprache

    def anrede_fuer(self, kontakt: Kontakt) -> tuple[str, Literal["de", "en"]]:
        """Anredezeile ohne Satzzeichen und Sprache, z. B. ``("Sehr geehrte Frau Beispiel", "de")``,
        ``("Dear Ms Esempio", "en")``, für Postfächer ``("Sehr geehrte Damen und Herren", "de")``."""
        sprache = self.sprache_fuer(kontakt)
        regel = self.anrede_regeln.get((kontakt.email or "").lower())
        if regel and regel.get("anrede"):
            return regel["anrede"], sprache
        person = person_aus_name(kontakt.name, anrede=kontakt.anrede)
        return _anrede_text(person, sprache), sprache

    def ergaenze_anrede(self, kontakt: Kontakt) -> tuple[str, Literal["de", "en"]]:
        """Wie :meth:`anrede_fuer`, setzt zusätzlich ``Kontakt.anrede`` ("Frau"/"Herr"/"Ms"/"Mr")
        und ``Kontakt.sprache`` am Kontakt."""
        anrede, sprache = self.anrede_fuer(kontakt)
        kontakt.sprache = sprache
        if not kontakt.anrede:
            kontakt.anrede = _anrede_kurz(person_aus_name(kontakt.name), sprache)
        return anrede, sprache

    # -- CC ----------------------------------------------------------------------------------

    def empfaenger_cc(self, settings: Settings | None = None) -> list[str]:
        """CC-Empfänger: ``kommunikation.cc_standard`` plus interne Kontakte der Liste, ohne Dubletten."""
        s = settings or self.settings
        ergebnis: list[str] = []
        gesehen: set[str] = set()
        for adresse in list(s.kommunikation.cc_standard) + [k.email for k in self.liste.je_gruppe(Kontaktgruppe.INTERN)]:
            if not adresse:
                continue
            norm = adresse.strip().lower()
            if norm and norm not in gesehen:
                gesehen.add(norm)
                ergebnis.append(adresse.strip())
        return ergebnis
