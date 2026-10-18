"""Kunden- und Lieferanten-Matching (Aliase > Kundennummer im Text > Fuzzy).

:class:`KundenMatcher` und :class:`LieferantenMatcher` arbeiten auf den Stammdaten **einer**
Firma (:class:`~auftragsautomat.stammdaten.basis.Stammdaten`) und den Alias-Tabellen
(:class:`~auftragsautomat.matching.aliase.AliasTabellen`). Aliase zählen nur, wenn ihre
``firma_id`` zur Firma der Stammdaten passt.

Scores sind 0..100 (:func:`~auftragsautomat.matching.aliase.aehnlichkeit`, Alias/Nummer = 100).
Entscheidung (``config.MatchingKonfig``):

* Score >= ``schwelle_sicher`` und eindeutig (Abstand zum zweitbesten aktiven Kandidaten
  >= :data:`ABSTAND_EINDEUTIG`) und Partner aktiv -> ``SICHER``;
* sonst Score >= ``schwelle_kandidat`` -> ``UNSICHER`` mit bis zu :data:`MAX_KANDIDATEN`
  Kandidaten (absteigend sortiert); inaktive Partner sind immer nur Kandidat;
* sonst ``KEIN_TREFFER``.

Entscheidung für den Fixture-Fall "Weinhandlung Müller" (Kunden 2100 "Weinhandlung Müller" und
2300 "Weinhandlung Müller-Lüdenscheid"): 2100 erhält 100, 2300 rund 90; der Abstand von ~10
ist >= 5, also wird 2100 SICHER zugeordnet. Nennt die Adresse "Lüdenscheid", bekommt 2300
den Ortsbonus, der Abstand schrumpft unter 5 und das Ergebnis wird UNSICHER (Anwender wählt).

Die Prüfhinweise ``KEIN_KUNDE``/``KUNDE_UNSICHER``/``KEIN_LIEFERANT``/``LIEFERANT_UNSICHER``
auf Belegebene erzeugt ``matching.pruefung`` aus dem Status; hier werden nur Zuordnungen gebaut.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from auftragsautomat.config import MatchingKonfig
from auftragsautomat.matching.aliase import AliasTabellen, aehnlichkeit, enthaelt_als_wort, normalisiere_name
from auftragsautomat.models import (
    Kunde,
    KundeKandidat,
    KundeZuordnung,
    Lieferant,
    LieferantKandidat,
    LieferantZuordnung,
    MatchStatus,
    PositionsZuordnung,
    RohPosition,
)
from auftragsautomat.stammdaten.basis import Stammdaten

__all__ = [
    "ABSTAND_EINDEUTIG",
    "BONUS_ORT",
    "BONUS_PLZ",
    "GRUND_LIEFERANTEN_KONFLIKT",
    "MAX_KANDIDATEN",
    "KundenMatcher",
    "LieferantenMatcher",
    "finde_kundennummer",
]

logger = logging.getLogger(__name__)

ABSTAND_EINDEUTIG = 5.0  # Mindestabstand zum zweitbesten aktiven Kandidaten für SICHER
MAX_KANDIDATEN = 5
BONUS_ORT = 5.0
BONUS_PLZ = 5.0
GRUND_LIEFERANTEN_KONFLIKT = "Positionen gehören zu mehreren Lieferanten"

# "Kd-Nr. 3441", "Kundennummer: 3441", "Kunden-Nr 3441", "Debitorennr. 3441", "Ihre Kundennummer bei uns: 3441"
_KUNDENNUMMER_MUSTER = re.compile(
    r"(?:kd|kdn|kunden|kunde|debitor(?:en)?)[\s.-]*(?:nr|nummer|no|number)\s*\.?\s*(?:bei\s+uns\s*)?[:.]?\s*"
    r"([A-Za-z]{0,2}-?\d{2,}[\w-]*)",
    re.IGNORECASE,
)


def finde_kundennummer(text: str | None) -> list[str]:
    """Alle im Text ausdrücklich als Kundennummer bezeichneten Nummern ("Kd-Nr. 3441")."""
    if not text:
        return []
    gefunden: list[str] = []
    for treffer in _KUNDENNUMMER_MUSTER.finditer(text):
        nummer = treffer.group(1).strip("-")
        if nummer and nummer not in gefunden:
            gefunden.append(nummer)
    return gefunden


def _textvarianten(text: str | None) -> list[str]:
    """Normalisierter Gesamttext und (bei mehrzeiligem Text) die erste nicht leere Zeile."""
    if not text:
        return []
    varianten: list[str] = []
    gesamt = normalisiere_name(text)
    if gesamt:
        varianten.append(gesamt)
    zeilen = [z.strip() for z in str(text).splitlines() if z.strip()]
    if len(zeilen) > 1:
        erste = normalisiere_name(zeilen[0])
        if erste and erste not in varianten:
            varianten.append(erste)
    return varianten


def _nummer_varianten(nummer: str) -> list[str]:
    # TODO-LEXWARE: Kunden-/Lieferantennummern werden als Text verglichen; prüfen, ob Lexware
    # führende Nullen oder Buchstaben in Nummern führt (Export vs. Anzeige im Beleg).
    nummer = nummer.strip()
    varianten = [nummer]
    ohne_nullen = nummer.lstrip("0")
    if ohne_nullen and ohne_nullen != nummer:
        varianten.append(ohne_nullen)
    if nummer.upper() != nummer:
        varianten.append(nummer.upper())
    return varianten


@dataclass
class _Bewertung:
    nummer: str
    score: float  # 0..100 (für Anzeige und Schwellen)
    score_roh: float  # ungedeckelt (für Reihenfolge und Abstand)
    grund: str
    aktiv: bool
    gruende: list[str] = field(default_factory=list)
    menge: Decimal = Decimal(0)  # Flaschen der Positionen, die auf diesen Partner zeigen (Lieferant)


def _entscheide(
    bewertungen: list[_Bewertung], konfig: MatchingKonfig
) -> tuple[MatchStatus, _Bewertung | None, list[_Bewertung]]:
    """Gemeinsame Entscheidungslogik: liefert (Status, sichere Bewertung oder None, Kandidaten)."""
    kandidaten = [b for b in bewertungen if b.score >= konfig.schwelle_kandidat]
    # bei gleichem Score aktive Partner zuerst (inaktive Dublette gleichen Namens blockiert nicht)
    kandidaten.sort(key=lambda b: (-b.score_roh, not b.aktiv, b.nummer))
    kandidaten = kandidaten[:MAX_KANDIDATEN]
    if not kandidaten:
        return MatchStatus.KEIN_TREFFER, None, []
    bester = kandidaten[0]
    zweiter = next((b for b in kandidaten[1:] if b.aktiv), None)
    eindeutig = zweiter is None or (bester.score_roh - zweiter.score_roh) >= ABSTAND_EINDEUTIG
    if bester.score >= konfig.schwelle_sicher and eindeutig and bester.aktiv:
        return MatchStatus.SICHER, bester, kandidaten
    return MatchStatus.UNSICHER, None, kandidaten


def _deckel(wert: float) -> float:
    return round(max(0.0, min(100.0, wert)), 1)


# ---------------------------------------------------------------------------
# Kunden
# ---------------------------------------------------------------------------


class KundenMatcher:
    """Ordnet den Kundentext eines Dokuments einem Kunden der Firma zu."""

    def __init__(
        self, stammdaten: Stammdaten, aliase: AliasTabellen, konfig: MatchingKonfig | None = None
    ) -> None:
        self.stammdaten = stammdaten
        self.aliase = aliase
        self.konfig = konfig or MatchingKonfig()
        self._namen: dict[str, list[str]] = {
            k.kundennummer: self._namensvarianten(k) for k in stammdaten.kunden
        }
        self._ort_norm: dict[str, str] = {
            k.kundennummer: normalisiere_name(k.ort, rechtsformen_entfernen=False)
            for k in stammdaten.kunden
            if k.ort
        }

    @staticmethod
    def _namensvarianten(kunde: Kunde) -> list[str]:
        varianten: list[str] = []
        for text in (
            kunde.name,
            kunde.name2,
            f"{kunde.name} {kunde.name2}" if kunde.name2 else None,
            kunde.matchcode,  # TODO-LEXWARE: prüfen, was im Lexware-Matchcode der Kunden steht
        ):
            norm = normalisiere_name(text)
            if norm and norm not in varianten:
                varianten.append(norm)
        return varianten

    def _kunde(self, nummer: str) -> Kunde | None:
        for variante in _nummer_varianten(nummer):
            kunde = self.stammdaten.kunden_nach_nummer.get(variante)
            if kunde is not None:
                return kunde
        return None

    def _zuordnung(
        self, status: MatchStatus, bester: _Bewertung | None, kandidaten: list[_Bewertung], quelle: str | None
    ) -> KundeZuordnung:
        kunde = self._kunde(bester.nummer) if bester is not None else None
        return KundeZuordnung(
            kunde=kunde if status == MatchStatus.SICHER else None,
            kandidaten=[
                KundeKandidat(kunde=k, score=b.score, grund=b.grund)
                for b in kandidaten
                if (k := self._kunde(b.nummer)) is not None
            ],
            status=status,
            quelle=quelle if status != MatchStatus.KEIN_TREFFER else None,
        )

    def finde(
        self, text: str | None, adresse: str | None = None, *, kundennummer: str | None = None
    ) -> KundeZuordnung:
        """Kunde zu ``text`` (Name wie im Dokument) finden; ``adresse`` liefert Ort/PLZ als Bonus,
        ``kundennummer`` eine im Dokument ausdrücklich genannte Kundennummer.

        Reihenfolge: 1. Alias (Firma muss passen), 2. Kundennummer (Parameter oder im Text:
        "Kd-Nr. 3441"), 3. Fuzzy über Name, Name2, Matchcode.
        """
        firma_id = self.stammdaten.firma_id
        if not (text and text.strip()) and not (adresse and adresse.strip()) and not kundennummer:
            logger.info("Firma %s: kein Kundentext im Dokument", firma_id)
            return KundeZuordnung()

        # 1. Alias
        for quelle_text in (text, adresse):
            treffer = self.aliase.kunde(quelle_text, firma_id=firma_id)
            if treffer is None:
                continue
            kunde = self._kunde(treffer.nummer)
            if kunde is None:
                logger.warning(
                    "Kunden-Alias %r zeigt auf Kundennummer %s, die im Stamm der Firma %s fehlt",
                    treffer.alias,
                    treffer.nummer,
                    firma_id,
                )
                continue
            grund = f"Alias '{treffer.alias}'"
            if not kunde.aktiv:
                logger.warning(
                    "Kunden-Alias %r -> %s, Kunde ist inaktiv; nur Kandidat",
                    treffer.alias,
                    kunde.kundennummer,
                )
                bewertung = _Bewertung(kunde.kundennummer, 100.0, 100.0, f"{grund}, Kunde inaktiv", False)
                return self._zuordnung(MatchStatus.UNSICHER, None, [bewertung], "alias")
            logger.info("Kunde %s (%s) über Alias %r", kunde.kundennummer, kunde.name, treffer.alias)
            bewertung = _Bewertung(kunde.kundennummer, 100.0, 100.0, grund, True)
            return self._zuordnung(MatchStatus.SICHER, bewertung, [bewertung], "alias")

        # 2. Kundennummer (Parameter oder im Text)
        nummern = [kundennummer] if kundennummer else []
        nummern += finde_kundennummer(text) + finde_kundennummer(adresse)
        for nummer in nummern:
            kunde = self._kunde(nummer)
            if kunde is None:
                logger.info(
                    "Kundennummer %r aus dem Dokument ist im Stamm der Firma %s unbekannt", nummer, firma_id
                )
                continue
            grund = f"Kundennummer {nummer} im Dokument"
            if not kunde.aktiv:
                bewertung = _Bewertung(kunde.kundennummer, 100.0, 100.0, f"{grund}, Kunde inaktiv", False)
                return self._zuordnung(MatchStatus.UNSICHER, None, [bewertung], "kundennummer")
            logger.info("Kunde %s (%s) über Kundennummer im Dokument", kunde.kundennummer, kunde.name)
            bewertung = _Bewertung(kunde.kundennummer, 100.0, 100.0, grund, True)
            return self._zuordnung(MatchStatus.SICHER, bewertung, [bewertung], "kundennummer")

        # 3. Fuzzy
        bewertungen = self._fuzzy(text, adresse)
        status, bester, kandidaten = _entscheide(bewertungen, self.konfig)
        if status == MatchStatus.SICHER and bester is not None:
            logger.info("Kunde %s über Fuzzy-Vergleich (%s) zu %r", bester.nummer, bester.grund, text)
        elif status == MatchStatus.UNSICHER:
            logger.info(
                "Kunde zu %r unsicher; Kandidaten: %s",
                text,
                ", ".join(f"{b.nummer} ({b.score:.0f})" for b in kandidaten),
            )
        else:
            logger.info("Kein Kunde zu %r in Firma %s gefunden", text, firma_id)
        return self._zuordnung(status, bester, kandidaten, "fuzzy")

    def _fuzzy(self, text: str | None, adresse: str | None) -> list[_Bewertung]:
        varianten = _textvarianten(text)
        if not varianten and adresse:
            varianten = _textvarianten(adresse)
        if not varianten:
            return []
        # Ort/PLZ: aus der Adresse, bei mehrzeiligem Text auch aus dem Text selbst.
        adress_teile = [adresse or ""]
        if text and len([z for z in text.splitlines() if z.strip()]) > 1:
            adress_teile.append(text)
        adresse_norm = normalisiere_name("\n".join(adress_teile), rechtsformen_entfernen=False)

        bewertungen: list[_Bewertung] = []
        for kunde in self.stammdaten.kunden:
            namen = self._namen.get(kunde.kundennummer) or []
            if not namen:
                continue
            name_score = max(aehnlichkeit(v, n) for v in varianten for n in namen)
            if name_score <= 0:
                continue
            gruende = [f"Name {name_score:.0f}"]
            bonus = 0.0
            if adresse_norm:
                if kunde.plz and enthaelt_als_wort(
                    adresse_norm, normalisiere_name(kunde.plz, rechtsformen_entfernen=False)
                ):
                    bonus += BONUS_PLZ
                    gruende.append(f"PLZ {kunde.plz} passt")
                ort_norm = self._ort_norm.get(kunde.kundennummer)
                if ort_norm and enthaelt_als_wort(adresse_norm, ort_norm):
                    bonus += BONUS_ORT
                    gruende.append(f"Ort {kunde.ort} passt")
            if not kunde.aktiv:
                gruende.append("Kunde inaktiv")
            roh = name_score + bonus
            bewertungen.append(
                _Bewertung(
                    kunde.kundennummer,
                    _deckel(roh),
                    roh,
                    "Fuzzy: " + ", ".join(gruende),
                    kunde.aktiv,
                    gruende,
                )
            )
        return bewertungen


# ---------------------------------------------------------------------------
# Lieferanten
# ---------------------------------------------------------------------------


class LieferantenMatcher:
    """Ordnet den Lieferanten (Weingut) eines Dokuments zu: aus dem Text, sonst aus den
    Produktmarken bzw. den zugeordneten Artikeln der Positionen."""

    def __init__(
        self, stammdaten: Stammdaten, aliase: AliasTabellen, konfig: MatchingKonfig | None = None
    ) -> None:
        self.stammdaten = stammdaten
        self.aliase = aliase
        self.konfig = konfig or MatchingKonfig()
        self._namen: dict[str, list[str]] = {
            lf.lieferantennummer: self._namensvarianten(lf) for lf in stammdaten.lieferanten
        }
        self._warengruppen_cache: dict[str, str | None] = {}

    @staticmethod
    def _namensvarianten(lieferant: Lieferant) -> list[str]:
        varianten: list[str] = []
        for text in (
            lieferant.name,
            lieferant.name2,
            f"{lieferant.name} {lieferant.name2}" if lieferant.name2 else None,
            lieferant.matchcode,  # TODO-LEXWARE: prüfen, was im Lexware-Matchcode der Lieferanten steht
        ):
            norm = normalisiere_name(text)
            if norm and norm not in varianten:
                varianten.append(norm)
        return varianten

    def _lieferant(self, nummer: str | None) -> Lieferant | None:
        if not nummer:
            return None
        for variante in _nummer_varianten(nummer):
            lieferant = self.stammdaten.lieferanten_nach_nummer.get(variante)
            if lieferant is not None:
                return lieferant
        return None

    def _zuordnung(
        self, status: MatchStatus, bester: _Bewertung | None, kandidaten: list[_Bewertung], quelle: str | None
    ) -> LieferantZuordnung:
        lieferant = self._lieferant(bester.nummer) if bester is not None else None
        return LieferantZuordnung(
            lieferant=lieferant if status == MatchStatus.SICHER else None,
            kandidaten=[
                LieferantKandidat(lieferant=lf, score=b.score, grund=b.grund)
                for b in kandidaten
                if (lf := self._lieferant(b.nummer)) is not None
            ],
            status=status,
            quelle=quelle if status != MatchStatus.KEIN_TREFFER else None,
        )

    def lieferant_fuer_warengruppe(self, warengruppe: str | None) -> str | None:
        # TODO-LEXWARE: Annahme, dass die Warengruppe im Artikelstamm dem Weingut/Lieferanten entspricht.
        """Lieferantennummer zu einer Warengruppe (= Weingut): Alias oder exakter Name/Name2/Matchcode."""
        if not warengruppe:
            return None
        if warengruppe in self._warengruppen_cache:
            return self._warengruppen_cache[warengruppe]
        nummer: str | None = None
        treffer = self.aliase.lieferant(warengruppe, firma_id=self.stammdaten.firma_id)
        per_alias = self._lieferant(treffer.nummer) if treffer is not None else None
        if per_alias is not None:
            nummer = per_alias.lieferantennummer
        else:
            norm = normalisiere_name(warengruppe)
            for lf in self.stammdaten.lieferanten:
                if norm and norm in self._namen.get(lf.lieferantennummer, []):
                    nummer = lf.lieferantennummer
                    break
        self._warengruppen_cache[warengruppe] = nummer
        return nummer

    def _stimme_fuer_position(self, position: PositionsZuordnung | RohPosition) -> tuple[str, str] | None:
        """(Lieferantennummer, Grund) für eine Position oder None."""
        if isinstance(position, PositionsZuordnung):
            if position.status == MatchStatus.UEBERSPRUNGEN:
                return None
            roh = position.roh
            artikel = position.artikel
        else:
            roh = position
            artikel = None
        if artikel is not None:
            # Zugeordneter Artikel: sein Lieferant/seine Warengruppe ist die verlässlichste Stimme
            per_artikel = self._lieferant(artikel.lieferantennummer)
            if per_artikel is not None:
                return per_artikel.lieferantennummer, f"Artikel {artikel.artikelnummer}"
            nummer = self.lieferant_fuer_warengruppe(artikel.warengruppe)
            if nummer is not None:
                return nummer, f"Warengruppe '{artikel.warengruppe}' (Artikel {artikel.artikelnummer})"
        if roh.durchgestrichen:
            return None
        marke = self.aliase.lieferant_fuer_marke(roh.bezeichnung, firma_id=self.stammdaten.firma_id)
        per_marke = self._lieferant(marke.nummer) if marke is not None else None
        if per_marke is not None:
            return per_marke.lieferantennummer, f"Produktmarke '{marke.alias}'"  # type: ignore[union-attr]
        return None

    @staticmethod
    def _menge(position: PositionsZuordnung | RohPosition) -> Decimal:
        if isinstance(position, PositionsZuordnung):
            return position.menge if position.menge is not None else Decimal(0)
        return position.menge if position.menge is not None else Decimal(0)

    def _aus_positionen(
        self, positionen: Sequence[PositionsZuordnung | RohPosition] | None
    ) -> list[_Bewertung]:
        """Stimmen der Positionen je Lieferant; Score = Anteil der Positionen in Prozent."""
        if not positionen:
            return []
        stimmen: dict[str, list[str]] = {}
        mengen: dict[str, Decimal] = {}
        for position in positionen:
            stimme = self._stimme_fuer_position(position)
            if stimme is None:
                continue
            nummer, grund = stimme
            stimmen.setdefault(nummer, []).append(grund)
            mengen[nummer] = mengen.get(nummer, Decimal(0)) + self._menge(position)
        gesamt = sum(len(g) for g in stimmen.values())
        if gesamt == 0:
            return []
        bewertungen: list[_Bewertung] = []
        for nummer, gruende in stimmen.items():
            lieferant = self._lieferant(nummer)
            if lieferant is None:
                continue
            anteil = 100.0 * len(gruende) / gesamt
            beispiele = ", ".join(dict.fromkeys(gruende))
            if len(stimmen) > 1:
                grund = f"{GRUND_LIEFERANTEN_KONFLIKT} ({len(gruende)} von {gesamt} Positionen: {beispiele})"
            else:
                grund = f"Positionen ({len(gruende)} von {gesamt}): {beispiele}"
            aktiv = lieferant.aktiv
            if not aktiv:
                grund += ", Lieferant inaktiv"
            bewertungen.append(
                _Bewertung(nummer, _deckel(anteil), anteil, grund, aktiv, gruende, mengen.get(nummer, Decimal(0)))
            )
        bewertungen.sort(key=lambda b: (-b.score_roh, -b.menge, not b.aktiv, b.nummer))
        return bewertungen

    def _hauptlieferant(
        self, aus_positionen: list[_Bewertung], fuzzy_status: MatchStatus, fuzzy_bester: _Bewertung | None
    ) -> LieferantZuordnung:
        """Positionen zeigen auf mehrere Weingüter (Kundenbestellung mit Weinen mehrerer Lieferanten).

        Jedes Weingut bekommt später eine eigene Einkauf-Bestellung (``matching.pruefung.gruppiere_nach_lieferant``,
        ``lexware.buchen``). Hier wird nur der **Hauptlieferant** bestimmt: der im Text genannte, sonst der mit den
        meisten Positionen, bei Gleichstand der mit den meisten Flaschen. Alle Beteiligten bleiben Kandidaten mit
        dem Grund :data:`GRUND_LIEFERANTEN_KONFLIKT`. Ein inaktiver Hauptlieferant bleibt UNSICHER.
        """
        firma_id = self.stammdaten.firma_id
        kandidaten = aus_positionen[:MAX_KANDIDATEN]
        haupt = kandidaten[0]
        if (
            fuzzy_status == MatchStatus.SICHER
            and fuzzy_bester is not None
            and any(b.nummer == fuzzy_bester.nummer for b in kandidaten)
        ):
            haupt = next(b for b in kandidaten if b.nummer == fuzzy_bester.nummer)
            haupt.grund += "; im Text genannt"
        logger.warning(
            "Firma %s: %s: %s; Hauptlieferant %s (je Weingut eine eigene Einkauf-Bestellung)",
            firma_id,
            GRUND_LIEFERANTEN_KONFLIKT,
            "; ".join(f"{b.nummer} ({len(b.gruende)} Pos., {b.score:.0f} %)" for b in aus_positionen),
            haupt.nummer,
        )
        if not haupt.aktiv:
            logger.warning("Hauptlieferant %s aus den Positionen ist inaktiv; nur Kandidat", haupt.nummer)
            return self._zuordnung(MatchStatus.UNSICHER, None, kandidaten, "positionen")
        return self._zuordnung(MatchStatus.SICHER, haupt, kandidaten, "positionen")

    def _fuzzy(self, text: str | None) -> list[_Bewertung]:
        varianten = _textvarianten(text)
        if not varianten:
            return []
        bewertungen: list[_Bewertung] = []
        for lieferant in self.stammdaten.lieferanten:
            namen = self._namen.get(lieferant.lieferantennummer) or []
            if not namen:
                continue
            score = max(aehnlichkeit(v, n) for v in varianten for n in namen)
            if score <= 0:
                continue
            gruende = [f"Name {score:.0f}"]
            if not lieferant.aktiv:
                gruende.append("Lieferant inaktiv")
            bewertungen.append(
                _Bewertung(
                    lieferant.lieferantennummer,
                    _deckel(score),
                    score,
                    "Fuzzy: " + ", ".join(gruende),
                    lieferant.aktiv,
                    gruende,
                )
            )
        return bewertungen

    def finde(
        self,
        text: str | None,
        positionen: Sequence[PositionsZuordnung | RohPosition] | None = None,
    ) -> LieferantZuordnung:
        """Lieferant zu ``text`` (Weingut wie im Dokument) und/oder den Positionen finden.

        Reihenfolge: 1. Alias im Text, 2. Produktmarken der Positionsbezeichnungen,
        3. Lieferantennummer/Warengruppe der bereits zugeordneten Artikel, 4. Fuzzy über die
        Lieferantennamen. Zeigen die Positionen auf mehrere Lieferanten (Kundenbestellung mit Weinen
        mehrerer Weingüter), ist das Ergebnis SICHER für den Hauptlieferanten (siehe
        :meth:`_hauptlieferant`) mit allen Beteiligten als Kandidaten (Grund "Positionen gehören zu
        mehreren Lieferanten"); die Prüfung meldet ``MEHRERE_LIEFERANTEN`` und die Buchung legt je
        Weingut eine eigene Einkauf-Bestellung an. Widersprechen sich Text und Positionen bei einem
        einzigen Positions-Lieferanten, ist das Ergebnis UNSICHER.
        """
        firma_id = self.stammdaten.firma_id

        # 1. Alias im Text
        treffer = self.aliase.lieferant(text, firma_id=firma_id) if text else None
        if treffer is not None:
            lieferant = self._lieferant(treffer.nummer)
            if lieferant is None:
                logger.warning(
                    "Lieferanten-Alias %r zeigt auf Lieferantennummer %s, die im Stamm der Firma %s fehlt",
                    treffer.alias,
                    treffer.nummer,
                    firma_id,
                )
            else:
                grund = f"Alias '{treffer.alias}'"
                if not lieferant.aktiv:
                    logger.warning(
                        "Lieferanten-Alias %r -> %s, Lieferant inaktiv; nur Kandidat",
                        treffer.alias,
                        lieferant.lieferantennummer,
                    )
                    bewertung = _Bewertung(
                        lieferant.lieferantennummer, 100.0, 100.0, f"{grund}, Lieferant inaktiv", False
                    )
                    return self._zuordnung(MatchStatus.UNSICHER, None, [bewertung], "alias")
                logger.info(
                    "Lieferant %s (%s) über Alias %r",
                    lieferant.lieferantennummer,
                    lieferant.name,
                    treffer.alias,
                )
                bewertung = _Bewertung(lieferant.lieferantennummer, 100.0, 100.0, grund, True)
                return self._zuordnung(MatchStatus.SICHER, bewertung, [bewertung], "alias")

        # 2./3. Positionen (Produktmarken, zugeordnete Artikel)
        aus_positionen = self._aus_positionen(positionen)
        # 4. Fuzzy über den Text (auch als Gegenprobe zu den Positionen)
        fuzzy = self._fuzzy(text)
        fuzzy_status, fuzzy_bester, fuzzy_kandidaten = _entscheide(fuzzy, self.konfig)

        if aus_positionen:
            if len(aus_positionen) > 1:
                return self._hauptlieferant(aus_positionen, fuzzy_status, fuzzy_bester)
            einziger = aus_positionen[0]
            if (
                fuzzy_status == MatchStatus.SICHER
                and fuzzy_bester is not None
                and fuzzy_bester.nummer != einziger.nummer
            ):
                logger.warning(
                    "Firma %s: Text nennt Lieferant %s, Positionen deuten auf %s; Anwender muss wählen",
                    firma_id,
                    fuzzy_bester.nummer,
                    einziger.nummer,
                )
                fuzzy_bester.grund = f"Text nennt diesen Lieferanten ({fuzzy_bester.grund}), Positionen deuten auf {einziger.nummer}"
                return self._zuordnung(MatchStatus.UNSICHER, None, [einziger, fuzzy_bester], "positionen")
            if not einziger.aktiv:
                logger.warning("Lieferant %s aus den Positionen ist inaktiv; nur Kandidat", einziger.nummer)
                return self._zuordnung(MatchStatus.UNSICHER, None, [einziger], "positionen")
            logger.info("Lieferant %s über die Positionen (%s)", einziger.nummer, einziger.grund)
            return self._zuordnung(MatchStatus.SICHER, einziger, [einziger], "positionen")

        if fuzzy_status == MatchStatus.SICHER and fuzzy_bester is not None:
            logger.info("Lieferant %s über Fuzzy-Vergleich zu %r", fuzzy_bester.nummer, text)
        elif fuzzy_status == MatchStatus.UNSICHER:
            logger.info(
                "Lieferant zu %r unsicher; Kandidaten: %s",
                text,
                ", ".join(f"{b.nummer} ({b.score:.0f})" for b in fuzzy_kandidaten),
            )
        else:
            logger.info("Kein Lieferant zu %r in Firma %s gefunden", text, firma_id)
        return self._zuordnung(fuzzy_status, fuzzy_bester, fuzzy_kandidaten, "fuzzy")
