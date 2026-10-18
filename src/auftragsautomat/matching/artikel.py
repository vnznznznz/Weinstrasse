"""Artikel-Matching: Dokumentposition -> Artikel des Lexware-Stamms (:class:`PositionsZuordnung`).

Regeln (docs/ARCHITEKTUR.md, Abschnitt ``matching/artikel.py``):

1. Bezeichnung auf der Überspringen-Liste (``aliase.ist_zu_ueberspringen``) oder Preis 0,00 ohne
   Treffer -> Status ``UEBERSPRUNGEN`` + ``Pruefhinweis(NULLPREIS_OHNE_ARTIKEL, WARNUNG)``.
2. Artikelnummer im Dokument (``roh.artikelnummer``) -> direkter Treffer (Quelle "artikelnummer").
3. Artikel-Alias (Firma muss passen) -> Treffer mit Score 100 (Quelle "alias"). Nennt das Dokument
   ein anderes Volumen / eine Holzkiste / einen anderen Jahrgang als der Alias-Artikel, wird die
   passende Variante per Fuzzy gesucht (der Alias-Artikel dient als Anker).
4. Fuzzy über :func:`~auftragsautomat.matching.volumen.normalisiere_bezeichnung` (Quelle "fuzzy")
   mit Zu-/Abschlägen: Lieferant/Warengruppe passt (+5) / widerspricht (-10); Jahrgang gleich (+5),
   abweichend (-10), Artikel ohne Jahrgang neutral; Volumen: Dokument nennt keins -> Standard
   (75 cl) bevorzugt, Sondervarianten -15; Dokument nennt eins -> gleich +5, abweichend -15;
   Holzkiste analog.
   **Sondervarianten (37,5 cl, 1,5 l, Holzkiste) werden NIE automatisch gewählt** – auch nicht
   per Alias oder Artikelnummer: sie bleiben Kandidat mit ``manuell_erforderlich=True``, Status
   ``UNSICHER``, ``Pruefhinweis(VARIANTE_MANUELL, WARNUNG)``.
5. Inaktive Artikel nur als Kandidat, nie automatisch.
6. Bei ``SICHER``: ``menge = roh.menge`` (None -> 0; den Hinweis MENGE_FEHLT erzeugt ``pruefung``),
   ``ek_preis = artikel.ek_preis``, ``vk_preis = artikel.vk_preis_1``, ``preis_dokument = roh.preis``.
   Preis 0,00 mit sicherem Treffer -> normal zuordnen, aber ``Pruefhinweis(PREIS_ABWEICHUNG, INFO)``
   "Preis 0,00 im Dokument".
   Bei ``UNSICHER``/``KEIN_TREFFER``: ``artikel = None``, Hinweis ``ARTIKEL_UNSICHER`` (WARNUNG)
   bzw. ``KEIN_ARTIKEL`` (FEHLER).

:func:`ordne_manuell` setzt in der Freigabe-UI einen vom Anwender gewählten Artikel
(Status ``MANUELL``) und entfernt die Matching-Hinweise.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal

from rapidfuzz import fuzz

from auftragsautomat.config import MatchingKonfig
from auftragsautomat.matching.aliase import AliasTabellen, aehnlichkeit, normalisiere_name
from auftragsautomat.matching.volumen import (
    ist_holzkiste,
    normalisiere_bezeichnung,
    parse_jahrgang,
    parse_volumen_cl,
)
from auftragsautomat.models import (
    Artikel,
    ArtikelKandidat,
    Lieferant,
    MatchStatus,
    PositionsZuordnung,
    Pruefcode,
    Pruefhinweis,
    RohPosition,
    Schweregrad,
)
from auftragsautomat.stammdaten.basis import Stammdaten

__all__ = [
    "ABSTAND_EINDEUTIG",
    "MAX_KANDIDATEN",
    "MATCHING_HINWEISCODES",
    "ArtikelMatcher",
    "ordne_manuell",
]

logger = logging.getLogger(__name__)

ABSTAND_EINDEUTIG = 5.0
MAX_KANDIDATEN = 5

BONUS_JAHRGANG = 5.0
MALUS_JAHRGANG = 10.0
BONUS_VOLUMEN = 5.0
MALUS_VOLUMEN = 15.0
BONUS_HOLZKISTE = 5.0
MALUS_HOLZKISTE = 15.0
BONUS_LIEFERANT = 5.0
MALUS_LIEFERANT = 10.0
MALUS_WARENGRUPPE_FREMD = 5.0
BONUS_ALIAS_VERWANDT = 15.0  # Variante desselben Artikels wie der Alias-Treffer
AEHNLICHKEIT_VERWANDT = 90.0

# Hinweise, die eine manuelle Zuordnung in der Freigabe-UI erledigt.
MATCHING_HINWEISCODES: frozenset[Pruefcode] = frozenset(
    {
        Pruefcode.KEIN_ARTIKEL,
        Pruefcode.ARTIKEL_UNSICHER,
        Pruefcode.VARIANTE_MANUELL,
        Pruefcode.NULLPREIS_OHNE_ARTIKEL,
    }
)


def _volumen_text(volumen_cl: Decimal | None, holzkiste: bool = False) -> str:
    teile: list[str] = []
    if volumen_cl is not None:
        v = volumen_cl.normalize()
        teile.append(f"{format(v, 'f').replace('.', ',')} cl")
    if holzkiste:
        teile.append("Holzkiste")
    return " / ".join(teile) if teile else "Standardvolumen"


@dataclass(frozen=True)
class _Merkmale:
    """Aus der Dokumentposition abgeleitete Vergleichsmerkmale."""

    norm: str
    jahrgang: int | None
    volumen_cl: Decimal | None  # None = Dokument nennt kein Volumen
    holzkiste: bool
    nullpreis: bool
    standard_volumen_cl: Decimal

    @property
    def variante_angefordert(self) -> bool:
        """Dokument nennt ausdrücklich eine Sondervariante (37,5 cl, 1,5 l, Holzkiste ...)."""
        if self.holzkiste:
            return True
        return self.volumen_cl is not None and self.volumen_cl != self.standard_volumen_cl

    def beschreibung(self) -> str:
        return _volumen_text(self.volumen_cl, self.holzkiste)


@dataclass
class _Bewertung:
    artikel: Artikel
    score: float  # 0..100 (Anzeige, Schwellen)
    score_roh: float  # ungedeckelt (Reihenfolge, Abstand)
    gruende: list[str] = field(default_factory=list)

    @property
    def grund(self) -> str:
        return ", ".join(self.gruende)

    def kandidat(self) -> ArtikelKandidat:
        return ArtikelKandidat(
            artikel=self.artikel,
            score=self.score,
            grund=self.grund,
            manuell_erforderlich=self.artikel.ist_sondervariante,
        )


class ArtikelMatcher:
    """Ordnet Dokumentpositionen Artikeln des Stamms einer Firma zu."""

    def __init__(
        self, stammdaten: Stammdaten, aliase: AliasTabellen, konfig: MatchingKonfig | None = None
    ) -> None:
        self.stammdaten = stammdaten
        self.aliase = aliase
        self.konfig = konfig or MatchingKonfig()
        self._norm: dict[str, str] = {
            a.artikelnummer: normalisiere_bezeichnung(a.bezeichnung) for a in stammdaten.artikel
        }
        self._matchcode_norm: dict[str, str] = {
            a.artikelnummer: normalisiere_name(a.matchcode) for a in stammdaten.artikel if a.matchcode
        }
        self._lieferanten_namen: dict[str, set[str]] = {}
        for lf in stammdaten.lieferanten:
            namen = {normalisiere_name(t) for t in (lf.name, lf.name2, lf.matchcode) if t}
            self._lieferanten_namen[lf.lieferantennummer] = {n for n in namen if n}

    # -- Merkmale des Dokuments --------------------------------------------------

    def _merkmale(self, roh: RohPosition) -> _Merkmale:
        volumen = roh.volumen_cl if roh.volumen_cl is not None else parse_volumen_cl(roh.bezeichnung)
        jahrgang = roh.jahrgang if roh.jahrgang is not None else parse_jahrgang(roh.bezeichnung)
        nullpreis = roh.preis is not None and roh.preis == 0
        return _Merkmale(
            norm=normalisiere_bezeichnung(roh.bezeichnung),
            jahrgang=jahrgang,
            volumen_cl=volumen,
            holzkiste=ist_holzkiste(roh.bezeichnung),
            nullpreis=nullpreis,
            standard_volumen_cl=self.konfig.standard_volumen_cl,
        )

    # -- Hilfen ------------------------------------------------------------------

    def _artikel_nach_nummer(self, nummer: str | None) -> Artikel | None:
        # TODO-LEXWARE: Artikelnummern werden als Text verglichen (auch ohne führende Nullen und in
        # Großschreibung); prüfen, wie Lexware Artikelnummern exportiert und ob Kunden sie so nennen.
        if not nummer:
            return None
        nummer = nummer.strip()
        for variante in (nummer, nummer.lstrip("0"), nummer.upper(), nummer.lstrip("0").upper()):
            if variante and variante in self.stammdaten.artikel_nach_nummer:
                return self.stammdaten.artikel_nach_nummer[variante]
        return None

    def _lieferant_passt(self, artikel: Artikel, lieferant: Lieferant | None) -> bool | None:
        # TODO-LEXWARE: Annahme, dass Artikel.lieferantennummer bzw. die Warengruppe das Weingut nennt.
        """True = passt, False = widerspricht, None = nicht beurteilbar."""
        if lieferant is None:
            return None
        if artikel.lieferantennummer:
            return artikel.lieferantennummer == lieferant.lieferantennummer
        if artikel.warengruppe:
            wg = normalisiere_name(artikel.warengruppe)
            if wg and wg in self._lieferanten_namen.get(lieferant.lieferantennummer, set()):
                return True
            treffer = self.aliase.lieferant(artikel.warengruppe, firma_id=self.stammdaten.firma_id)
            if treffer is not None:
                return treffer.nummer == lieferant.lieferantennummer
        return None

    @staticmethod
    def _variante_passt(artikel: Artikel, merkmale: _Merkmale) -> bool:
        """Volumen, Holzkiste und Jahrgang des Artikels widersprechen dem Dokument nicht."""
        if merkmale.holzkiste != artikel.holzkiste:
            return False
        if (
            merkmale.volumen_cl is not None
            and artikel.volumen_cl is not None
            and merkmale.volumen_cl != artikel.volumen_cl
        ):
            return False
        if (
            merkmale.jahrgang is not None
            and artikel.jahrgang is not None
            and merkmale.jahrgang != artikel.jahrgang
        ):
            return False
        return True

    def _hinweis(
        self, roh: RohPosition, code: Pruefcode, grad: Schweregrad, text: str, **details: str
    ) -> Pruefhinweis:
        return Pruefhinweis(
            code=code, schweregrad=grad, text=text, position_laufnr=roh.laufnr, details=details
        )

    def _nullpreis_hinweis(self, roh: RohPosition) -> Pruefhinweis:
        return self._hinweis(
            roh,
            Pruefcode.PREIS_ABWEICHUNG,
            Schweregrad.INFO,
            f"Position {roh.laufnr}: Preis 0,00 im Dokument ('{roh.bezeichnung}'); gebucht wird der Lexware-Preis",
            preis_dokument="0,00",
        )

    # -- Ergebnisse bauen -------------------------------------------------------

    @staticmethod
    def _basis(
        roh: RohPosition, status: MatchStatus, quelle: str | None, kandidaten: list[_Bewertung]
    ) -> PositionsZuordnung:
        return PositionsZuordnung(
            roh=roh,
            artikel=None,
            kandidaten=[b.kandidat() for b in kandidaten],
            status=status,
            quelle=quelle,
            menge=roh.menge if roh.menge is not None else Decimal(0),
            preis_dokument=roh.preis,
        )

    def _sicher(
        self,
        roh: RohPosition,
        merkmale: _Merkmale,
        bewertung: _Bewertung,
        quelle: str,
        kandidaten: list[_Bewertung],
    ) -> PositionsZuordnung:
        artikel = bewertung.artikel
        zuordnung = self._basis(roh, MatchStatus.SICHER, quelle, kandidaten)
        zuordnung.artikel = artikel
        zuordnung.ek_preis = artikel.ek_preis
        zuordnung.vk_preis = artikel.vk_preis_1
        if roh.menge is None:
            logger.debug(
                "Position %s: keine Menge im Dokument, Menge 0 (Hinweis folgt in der Prüfung)", roh.laufnr
            )
        if merkmale.nullpreis:
            zuordnung.hinweise.append(self._nullpreis_hinweis(roh))
        logger.info(
            "Position %s '%s' -> Artikel %s (%s) über %s, Score %.0f",
            roh.laufnr,
            roh.bezeichnung,
            artikel.artikelnummer,
            artikel.bezeichnung,
            quelle,
            bewertung.score,
        )
        return zuordnung

    def _ueberspringe(
        self, roh: RohPosition, merkmale: _Merkmale, grund: str, kandidaten: list[_Bewertung] | None = None
    ) -> PositionsZuordnung:
        zuordnung = self._basis(roh, MatchStatus.UEBERSPRUNGEN, None, kandidaten or [])
        zuordnung.hinweise.append(
            self._hinweis(
                roh,
                Pruefcode.NULLPREIS_OHNE_ARTIKEL,
                Schweregrad.WARNUNG,
                f"Position {roh.laufnr} '{roh.bezeichnung}' wird übersprungen: {grund}",
                grund=grund,
            )
        )
        logger.warning("Position %s '%s' übersprungen: %s", roh.laufnr, roh.bezeichnung, grund)
        return zuordnung

    def _kein_treffer(self, roh: RohPosition, merkmale: _Merkmale) -> PositionsZuordnung:
        if merkmale.nullpreis:
            return self._ueberspringe(roh, merkmale, "Preis 0,00 und kein Artikel im Stamm")
        zuordnung = self._basis(roh, MatchStatus.KEIN_TREFFER, None, [])
        zuordnung.hinweise.append(
            self._hinweis(
                roh,
                Pruefcode.KEIN_ARTIKEL,
                Schweregrad.FEHLER,
                f"Position {roh.laufnr}: kein Artikel im Stamm der Firma {self.stammdaten.firma_id} für '{roh.bezeichnung}'",
            )
        )
        logger.warning("Position %s '%s': kein Artikel gefunden", roh.laufnr, roh.bezeichnung)
        return zuordnung

    def _unsicher(
        self,
        roh: RohPosition,
        merkmale: _Merkmale,
        kandidaten: list[_Bewertung],
        quelle: str,
        *,
        nur_variante: bool = False,
        zusatz: str | None = None,
    ) -> PositionsZuordnung:
        zuordnung = self._basis(roh, MatchStatus.UNSICHER, quelle, kandidaten)
        bester = kandidaten[0]
        liste = "; ".join(f"{b.artikel.artikelnummer} ({b.score:.0f}: {b.grund})" for b in kandidaten)
        varianten = [b for b in kandidaten if b.artikel.ist_sondervariante]
        if nur_variante:
            zuordnung.hinweise.append(
                self._hinweis(
                    roh,
                    Pruefcode.VARIANTE_MANUELL,
                    Schweregrad.WARNUNG,
                    f"Position {roh.laufnr}: Dokument nennt {merkmale.beschreibung()}; Artikel {bester.artikel.artikelnummer} "
                    f"'{bester.artikel.bezeichnung}' ist eine Sondervariante und muss manuell bestätigt werden",
                    artikelnummer=bester.artikel.artikelnummer,
                )
            )
        else:
            text = f"Position {roh.laufnr}: Artikel für '{roh.bezeichnung}' nicht eindeutig"
            if zusatz:
                text += f" ({zusatz})"
            zuordnung.hinweise.append(
                self._hinweis(
                    roh, Pruefcode.ARTIKEL_UNSICHER, Schweregrad.WARNUNG, f"{text}; Kandidaten: {liste}"
                )
            )
            if varianten and (merkmale.variante_angefordert or bester.artikel.ist_sondervariante):
                nummern = ", ".join(b.artikel.artikelnummer for b in varianten)
                zuordnung.hinweise.append(
                    self._hinweis(
                        roh,
                        Pruefcode.VARIANTE_MANUELL,
                        Schweregrad.WARNUNG,
                        f"Position {roh.laufnr}: Sondervariante ({merkmale.beschreibung()}) nur manuell wählbar: {nummern}",
                        artikelnummern=nummern,
                    )
                )
        if merkmale.nullpreis:
            zuordnung.hinweise.append(self._nullpreis_hinweis(roh))
        logger.info("Position %s '%s' unsicher; Kandidaten: %s", roh.laufnr, roh.bezeichnung, liste)
        return zuordnung

    # -- Direkter Treffer (Artikelnummer / Alias) ---------------------------------

    def _direkt(
        self,
        roh: RohPosition,
        merkmale: _Merkmale,
        artikel: Artikel,
        quelle: str,
        grund: str,
        lieferant: Lieferant | None,
    ) -> PositionsZuordnung:
        bewertung = _Bewertung(artikel, 100.0, 100.0, [grund])
        zusatz_hinweise: list[Pruefhinweis] = []
        if lieferant is not None and self._lieferant_passt(artikel, lieferant) is False:
            # Kein Grund, den eindeutigen Treffer zu verwerfen: den Lieferantenkonflikt meldet der
            # LieferantenMatcher ("Positionen gehören zu mehreren Lieferanten"); hier nur ein Hinweis.
            bewertung.gruende.append(f"Lieferant {lieferant.lieferantennummer} widerspricht dem Artikel")
            logger.warning(
                "Position %s: Artikel %s (%s) gehört nicht zum Lieferanten %s",
                roh.laufnr,
                artikel.artikelnummer,
                grund,
                lieferant.lieferantennummer,
            )
            zusatz_hinweise.append(
                self._hinweis(
                    roh,
                    Pruefcode.ARTIKEL_UNSICHER,
                    Schweregrad.INFO,
                    f"Position {roh.laufnr}: Artikel {artikel.artikelnummer} gehört zu Lieferant "
                    f"{artikel.lieferantennummer or artikel.warengruppe}, der Beleg nennt Lieferant {lieferant.lieferantennummer}",
                    artikelnummer=artikel.artikelnummer,
                    lieferant_dokument=lieferant.lieferantennummer,
                )
            )
        if not artikel.aktiv:
            bewertung.gruende.append("Artikel inaktiv")
            weitere = [
                b
                for b in self._bewerte_alle(merkmale, lieferant, None)
                if b.artikel.artikelnummer != artikel.artikelnummer
            ]
            kandidaten = [bewertung, *weitere[: MAX_KANDIDATEN - 1]]
            ergebnis = self._unsicher(
                roh, merkmale, kandidaten, quelle, zusatz=f"Artikel {artikel.artikelnummer} ist inaktiv"
            )
        elif artikel.ist_sondervariante:
            bewertung.gruende.append(f"Sondervariante {_volumen_text(artikel.volumen_cl, artikel.holzkiste)}")
            ergebnis = self._unsicher(roh, merkmale, [bewertung], quelle, nur_variante=True)
        else:
            ergebnis = self._sicher(roh, merkmale, bewertung, quelle, [bewertung])
        ergebnis.hinweise.extend(zusatz_hinweise)
        return ergebnis

    # -- Fuzzy -------------------------------------------------------------------

    def _bewerte(
        self, artikel: Artikel, merkmale: _Merkmale, lieferant: Lieferant | None, anker_norm: str | None
    ) -> _Bewertung | None:
        art_norm = self._norm.get(artikel.artikelnummer, "")
        name_score = aehnlichkeit(merkmale.norm, art_norm)
        # TODO-LEXWARE: Artikel-Matchcode nur als Ganzes vergleichen; prüfen, was Lexware dort führt.
        matchcode = self._matchcode_norm.get(artikel.artikelnummer)
        if matchcode:
            name_score = max(name_score, round(float(fuzz.ratio(merkmale.norm, matchcode)), 1))
        if name_score <= 0:
            return None
        gruende = [f"Name {name_score:.0f}"]
        score = name_score

        if anker_norm and art_norm and aehnlichkeit(anker_norm, art_norm) >= AEHNLICHKEIT_VERWANDT:
            score += BONUS_ALIAS_VERWANDT
            gruende.append("Variante des Alias-Artikels")

        # Jahrgang
        if merkmale.jahrgang is not None and artikel.jahrgang is not None:
            if merkmale.jahrgang == artikel.jahrgang:
                score += BONUS_JAHRGANG
                gruende.append(f"Jahrgang {artikel.jahrgang} passt")
            else:
                score -= MALUS_JAHRGANG
                gruende.append(f"Jahrgang {artikel.jahrgang} statt {merkmale.jahrgang}")

        # Holzkiste
        if merkmale.holzkiste != artikel.holzkiste:
            score -= MALUS_HOLZKISTE
            gruende.append("Holzkiste" if artikel.holzkiste else "keine Holzkiste")
        elif merkmale.holzkiste:
            score += BONUS_HOLZKISTE
            gruende.append("Holzkiste passt")

        # Volumen
        standard = merkmale.standard_volumen_cl
        if merkmale.volumen_cl is None:
            if artikel.volumen_cl is not None and artikel.volumen_cl != standard:
                score -= MALUS_VOLUMEN
                gruende.append(
                    f"{_volumen_text(artikel.volumen_cl)} statt Standard {_volumen_text(standard)}"
                )
        elif artikel.volumen_cl is None:
            gruende.append("Volumen im Stamm unbekannt")
        elif artikel.volumen_cl == merkmale.volumen_cl:
            score += BONUS_VOLUMEN
            gruende.append(f"{_volumen_text(artikel.volumen_cl)} passt")
        else:
            score -= MALUS_VOLUMEN
            gruende.append(f"{_volumen_text(artikel.volumen_cl)} statt {_volumen_text(merkmale.volumen_cl)}")

        # Lieferant / Warengruppe
        passt = self._lieferant_passt(artikel, lieferant)
        if passt is True:
            score += BONUS_LIEFERANT
            gruende.append("Lieferant passt")
        elif passt is False:
            if artikel.lieferantennummer:
                score -= MALUS_LIEFERANT
            else:
                score -= MALUS_WARENGRUPPE_FREMD
            gruende.append(f"Lieferant {lieferant.lieferantennummer} passt nicht")  # type: ignore[union-attr]

        if not artikel.aktiv:
            gruende.append("Artikel inaktiv")
        if artikel.ist_sondervariante:
            gruende.append("Sondervariante, nur manuell")
        return _Bewertung(artikel, round(max(0.0, min(100.0, score)), 1), round(score, 1), gruende)

    def _bewerte_alle(
        self, merkmale: _Merkmale, lieferant: Lieferant | None, anker_norm: str | None
    ) -> list[_Bewertung]:
        if not merkmale.norm:
            return []
        bewertungen = [
            b
            for a in self.stammdaten.artikel
            if (b := self._bewerte(a, merkmale, lieferant, anker_norm)) is not None
        ]
        bewertungen = [b for b in bewertungen if b.score >= self.konfig.schwelle_kandidat]
        # bei gleichem Score aktive Artikel zuerst (inaktive Dublette blockiert nicht)
        bewertungen.sort(key=lambda b: (-b.score_roh, not b.artikel.aktiv, b.artikel.artikelnummer))
        return bewertungen

    def _fuzzy(
        self,
        roh: RohPosition,
        merkmale: _Merkmale,
        lieferant: Lieferant | None,
        *,
        anker: Artikel | None = None,
        zusatz_hinweise: list[Pruefhinweis] | None = None,
    ) -> PositionsZuordnung:
        anker_norm = self._norm.get(anker.artikelnummer) if anker is not None else None
        kandidaten = self._bewerte_alle(merkmale, lieferant, anker_norm)[:MAX_KANDIDATEN]
        if not kandidaten:
            ergebnis = self._kein_treffer(roh, merkmale)
            ergebnis.hinweise.extend(zusatz_hinweise or [])
            return ergebnis

        bester = kandidaten[0]
        zweiter = next((b for b in kandidaten[1:] if b.artikel.aktiv), None)
        eindeutig = zweiter is None or (bester.score_roh - zweiter.score_roh) >= ABSTAND_EINDEUTIG
        gut_genug = bester.score >= self.konfig.schwelle_sicher

        if gut_genug and eindeutig and bester.artikel.aktiv and not bester.artikel.ist_sondervariante:
            ergebnis = self._sicher(roh, merkmale, bester, "fuzzy", kandidaten)
        elif (
            gut_genug
            and eindeutig
            and bester.artikel.aktiv
            and bester.artikel.ist_sondervariante
            and merkmale.variante_angefordert
        ):
            ergebnis = self._unsicher(roh, merkmale, kandidaten, "fuzzy", nur_variante=True)
        else:
            zusatz = None
            if not bester.artikel.aktiv:
                zusatz = f"bester Kandidat {bester.artikel.artikelnummer} ist inaktiv"
            elif bester.artikel.ist_sondervariante and not merkmale.variante_angefordert:
                zusatz = f"bester Kandidat {bester.artikel.artikelnummer} ist eine Sondervariante, das Dokument nennt keine"
            elif not eindeutig and zweiter is not None:
                zusatz = (
                    f"{bester.artikel.artikelnummer} und {zweiter.artikel.artikelnummer} sind ähnlich gut"
                )
            elif not gut_genug:
                zusatz = f"Score {bester.score:.0f} unter {self.konfig.schwelle_sicher:.0f}"
            ergebnis = self._unsicher(roh, merkmale, kandidaten, "fuzzy", zusatz=zusatz)
        ergebnis.hinweise.extend(zusatz_hinweise or [])
        return ergebnis

    # -- Öffentlich --------------------------------------------------------------

    def finde(self, roh: RohPosition, lieferant: Lieferant | None = None) -> PositionsZuordnung:
        """Artikel zur Dokumentposition ``roh`` finden (siehe Modul-Docstring für die Regeln)."""
        merkmale = self._merkmale(roh)
        zusatz_hinweise: list[Pruefhinweis] = []

        # 1. Überspringen-Liste
        if self.aliase.ist_zu_ueberspringen(roh.bezeichnung):
            return self._ueberspringe(roh, merkmale, "steht auf der Überspringen-Liste")

        # 2. Artikelnummer im Dokument
        if roh.artikelnummer:
            artikel = self._artikel_nach_nummer(roh.artikelnummer)
            if artikel is not None:
                return self._direkt(
                    roh,
                    merkmale,
                    artikel,
                    "artikelnummer",
                    f"Artikelnummer {roh.artikelnummer} im Dokument",
                    lieferant,
                )
            logger.warning(
                "Position %s: Artikelnummer %r aus dem Dokument ist im Stamm der Firma %s unbekannt",
                roh.laufnr,
                roh.artikelnummer,
                self.stammdaten.firma_id,
            )
            zusatz_hinweise.append(
                self._hinweis(
                    roh,
                    Pruefcode.ARTIKEL_UNSICHER,
                    Schweregrad.INFO,
                    f"Position {roh.laufnr}: Artikelnummer {roh.artikelnummer} aus dem Dokument ist im Stamm unbekannt",
                    artikelnummer_dokument=roh.artikelnummer,
                )
            )

        # 3. Alias
        anker: Artikel | None = None
        treffer = self.aliase.artikel(roh.bezeichnung, firma_id=self.stammdaten.firma_id)
        if treffer is not None:
            artikel = self._artikel_nach_nummer(treffer.nummer)
            if artikel is None:
                logger.warning(
                    "Artikel-Alias %r zeigt auf Artikelnummer %s, die im Stamm der Firma %s fehlt",
                    treffer.alias,
                    treffer.nummer,
                    self.stammdaten.firma_id,
                )
            elif self._variante_passt(artikel, merkmale):
                ergebnis = self._direkt(
                    roh, merkmale, artikel, "alias", f"Alias '{treffer.alias}'", lieferant
                )
                ergebnis.hinweise.extend(zusatz_hinweise)
                return ergebnis
            else:
                logger.info(
                    "Position %s: Alias %r -> Artikel %s, aber Dokument nennt %s%s; suche passende Variante",
                    roh.laufnr,
                    treffer.alias,
                    artikel.artikelnummer,
                    merkmale.beschreibung(),
                    f", Jahrgang {merkmale.jahrgang}" if merkmale.jahrgang else "",
                )
                anker = artikel

        # 4. Fuzzy
        return self._fuzzy(roh, merkmale, lieferant, anker=anker, zusatz_hinweise=zusatz_hinweise)


def ordne_manuell(
    zuordnung: PositionsZuordnung, artikel: Artikel, *, menge: Decimal | None = None
) -> PositionsZuordnung:
    """Vom Anwender gewählten Artikel setzen (Freigabe-UI): Status ``MANUELL``, Preise aus dem Stamm,
    Matching-Hinweise (KEIN_ARTIKEL, ARTIKEL_UNSICHER, VARIANTE_MANUELL, NULLPREIS_OHNE_ARTIKEL) entfernt.
    Liefert eine neue :class:`PositionsZuordnung`; die übergebene bleibt unverändert."""
    neu = zuordnung.model_copy(deep=True)
    neu.artikel = artikel
    neu.status = MatchStatus.MANUELL
    neu.quelle = "manuell"
    neu.ek_preis = artikel.ek_preis
    neu.vk_preis = artikel.vk_preis_1
    neu.preis_dokument = zuordnung.roh.preis
    if menge is not None:
        neu.menge = menge
    elif neu.menge == 0 and zuordnung.roh.menge is not None:
        neu.menge = zuordnung.roh.menge
    neu.hinweise = [h for h in neu.hinweise if h.code not in MATCHING_HINWEISCODES]
    if not any(k.artikel.artikelnummer == artikel.artikelnummer for k in neu.kandidaten):
        neu.kandidaten.insert(
            0,
            ArtikelKandidat(
                artikel=artikel, score=100.0, grund="manuell gewählt", manuell_erforderlich=False
            ),
        )
    if not artikel.aktiv:
        neu.hinweise.append(
            Pruefhinweis(
                code=Pruefcode.ARTIKEL_UNSICHER,
                schweregrad=Schweregrad.WARNUNG,
                text=f"Position {zuordnung.roh.laufnr}: manuell gewählter Artikel {artikel.artikelnummer} ist inaktiv",
                position_laufnr=zuordnung.roh.laufnr,
                details={"artikelnummer": artikel.artikelnummer},
            )
        )
        logger.warning(
            "Position %s: manuell gewählter Artikel %s ist inaktiv",
            zuordnung.roh.laufnr,
            artikel.artikelnummer,
        )
    logger.info(
        "Position %s '%s' manuell -> Artikel %s (%s)%s",
        zuordnung.roh.laufnr,
        zuordnung.roh.bezeichnung,
        artikel.artikelnummer,
        artikel.bezeichnung,
        " (Sondervariante bestätigt)" if artikel.ist_sondervariante else "",
    )
    return neu
