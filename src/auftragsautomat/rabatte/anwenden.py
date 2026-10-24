"""Aktionen auf ein Belegpaket anwenden (Positionsrabatt in % im Verkauf, nie im Einkauf).

Geschäftsregel 16 (``docs/ARCHITEKTUR.md``) und ``docs/AUSBAUSTUFE_2.md`` Abschnitt 7:

* **Finden** (:func:`finde_aktionen`): Eine Aktion passt auf eine aktive Position, wenn

  - die Firma passt (``Aktion.firma_id`` leer, gleich ``paket.firma_id`` oder gleich dem Kürzel/Namen
    der Firma des Pakets),
  - der Kunde passt: ``kundennummern`` enthält die Lexware-Kundennummer, oder ``kunde_kurz``
    (normalisiert, ganzes Wort) trifft Matchcode, Name, Name 2, einen Kunden-Alias oder den
    Kundentext des Dokuments, oder ``kunde_kurz`` nennt die Kundengruppe laut Katalog
    (``paket.kundengruppe``); ohne Kundenangabe gilt die Aktion für alle Kunden,
  - der Lieferant passt (``lieferant_kurz`` gegen Warengruppe/Lieferantennummer des Artikels, den
    zugeordneten Lieferanten, Lieferanten-Aliase/Produktmarken oder den Lieferantentext des
    Dokuments; ohne Angabe alle),
  - der Artikel passt: ``artikelnummern`` exakt (Artikelnummer des Stamms oder des Dokuments), sonst
    ``artikel_text`` unscharf (rapidfuzz über ``normalisiere_bezeichnung``; ab
    ``matching.schwelle_sicher`` sicher, ab ``schwelle_kandidat`` unsicher); abweichender
    Jahrgang -> unsicher, abweichendes Volumen -> kein Treffer; ohne Angabe alle Artikel,
  - das Bezugsdatum im Zeitraum liegt. Bezug laut ``rabatte.bezug``: Liefertag (sonst
    Auftragsdatum, sonst heute) bzw. Auftragsdatum (sonst Liefertag, sonst heute). Liegt es nur
    innerhalb von ``rabatte.toleranz_tage`` außerhalb des Zeitraums oder stammt es nicht aus dem
    Dokument (heute), ist der Treffer unsicher.

  Mehrere sichere Aktionen mit unterschiedlichem Rabatt auf derselben Position werden alle unsicher
  (der höchste Rabatt steht vorn); mit gleichem Rabatt bleibt der erste sicher.
* **Umrechnen** (:func:`rabatt_prozent_aus`): Prozent direkt; EUR je Flasche und Aktionspreis werden
  auf den VK Preisgruppe 1 bezogen (zwei Nachkommastellen); ohne VK oder bei negativem Ergebnis
  ``None`` mit Hinweis im Log.
* **Anwenden** (:func:`wende_an`): sicherer Treffer und ``rabatte.automatisch_anwenden`` ->
  ``PositionsZuordnung.rabatt_prozent`` + ``rabatt_quelle = "aktion:<id>"`` + ``RABATT_VORSCHLAG``
  (INFO); ohne automatisches Anwenden nur der INFO-Vorschlag; unsicher -> ``RABATT_UNSICHER``
  (WARNUNG) ohne Eintrag; Rabatt über ``rabatte.max_rabatt_prozent`` -> ``RABATT_VORSCHLAG`` als
  FEHLER (blockiert). Ein vorhandener Rabatt mit anderer Quelle ("manuell", "dokument") wird nie
  überschrieben. Der VK bleibt Preisgruppe 1 (``vk_preis_rabattiert`` rechnet den Rabatt ein), der
  Einkauf (``ek_preis``/``netto_ek``) bleibt unberührt. ``paket.aktionen`` listet alle Treffer. Die
  Funktion ist idempotent: eigene Hinweise und eigene Rabatteinträge werden vorher entfernt.
* **Laden** (:func:`alle_aktionen`): Excel (nur wenn ``rabatte.aktionen_datei`` eine web_url,
  drive_id/item_id oder einen lokalen Pfad hat) plus gespeicherte Aktionen aus
  ``rabatte.gedaechtnis_datei``; jeder Fehler wird als Warnung protokolliert, der Rest geliefert;
  ohne Quellen eine leere Liste (Stufe 1 bleibt unverändert). Kurzer Cache je Konfiguration.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from auftragsautomat.config import Firma, Settings
from auftragsautomat.m365.basis import datei_schluessel
from auftragsautomat.matching.aliase import (
    AliasFehler,
    AliasTabellen,
    aehnlichkeit,
    enthaelt_als_wort,
    normalisiere_name,
)
from auftragsautomat.matching.volumen import normalisiere_bezeichnung, parse_jahrgang, parse_volumen_cl
from auftragsautomat.models import (
    Aktion,
    AktionTreffer,
    Belegpaket,
    Kundengruppe,
    PositionsZuordnung,
    Pruefcode,
    Pruefhinweis,
    Schweregrad,
)
from auftragsautomat.rabatte.aktionen_excel import lese_aktionen
from auftragsautomat.rabatte.rabattliste_pdf import RabattlisteFehler, lade_gespeicherte_aktionen

__all__ = [
    "CACHE_SEKUNDEN",
    "EIGENE_CODES",
    "QUELLE_PRAEFIX",
    "SCORE_TEILMENGE",
    "alle_aktionen",
    "bezugsdatum",
    "finde_aktionen",
    "leere_cache",
    "rabatt_prozent_aus",
    "wende_an",
]

logger = logging.getLogger(__name__)

#: Präfix der ``rabatt_quelle`` für automatisch eingetragene Aktionsrabatte.
QUELLE_PRAEFIX = "aktion:"
#: Hinweise, die :func:`wende_an` erzeugt und vor jedem Lauf entfernt.
EIGENE_CODES: frozenset[Pruefcode] = frozenset({Pruefcode.RABATT_VORSCHLAG, Pruefcode.RABATT_UNSICHER})
#: Wie lange :func:`alle_aktionen` ein Ergebnis je Konfiguration wiederverwendet.
CACHE_SEKUNDEN = 300.0
#: Score, wenn alle Wörter des Aktionstexts (mindestens zwei) in der Artikelbezeichnung vorkommen.
SCORE_TEILMENGE = 95.0

_ZWEI_STELLEN = Decimal("0.01")
_HUNDERT = Decimal(100)

#: Schreibweisen von Kundengruppen in der Aktionen-Excel (normalisiert) je Katalog-Gruppe.
# TODO-PRUEFEN: Welche Gruppen-/Regionsbegriffe die Kundenspalte der echten Excel benutzt (z. B. "LEH",
# "Fachhandel", Regionsnamen) und ob Lexware den Positionsrabatt mit zwei Nachkommastellen annimmt
# (sonst in rabatt_prozent_aus auf ganze Prozent runden).
_GRUPPEN_SYNONYME: dict[Kundengruppe, frozenset[str]] = {
    Kundengruppe.FACHHANDEL: frozenset({"fachhandel", "fh", "fachhaendler", "weinhandel"}),
    Kundengruppe.EIGENSTAENDIGER_LEH: frozenset(
        {"eigenstaendiger leh", "eigenstaendige leh", "seh", "selbstaendiger leh", "selbstaendige", "eigenstaendige", "leh"}
    ),
    Kundengruppe.REGION: frozenset({"region", "regionen", "einkaufsregion", "einkaufsregionen", "leh"}),
    Kundengruppe.KETTE: frozenset({"kette", "ketten", "supermarktkette", "handelskette", "leh"}),
    Kundengruppe.GROSSKUNDE: frozenset({"grosskunde", "grosskunden", "leh"}),
}


# ---------------------------------------------------------------------------
# Hilfen
# ---------------------------------------------------------------------------


def _nummer_norm(nummer: str | None) -> str:
    if not nummer:
        return ""
    return str(nummer).strip().upper().lstrip("0")


def _prozent_text(wert: Decimal | None) -> str:
    if wert is None:
        return "-"
    return f"{wert.normalize():f}".replace(".", ",") + " %"


def _geld(wert: Decimal | None) -> str:
    if wert is None:
        return "-"
    return f"{wert.quantize(_ZWEI_STELLEN):f}".replace(".", ",") + " €"


def _aktion_text(aktion: Aktion) -> str:
    name = f"'{aktion.bezeichnung}' " if aktion.bezeichnung else ""
    return f"Aktion {name}[{aktion.aktion_id}]"


def _passt_wort(text: str | None, norm: str) -> bool:
    """``norm`` (normalisiert) ist gleich ``text`` oder kommt als ganzes Wort darin vor (oder umgekehrt)."""
    t = normalisiere_name(text)
    if not t or not norm:
        return False
    return t == norm or enthaelt_als_wort(t, norm) or enthaelt_als_wort(norm, t)


def _firma_passt(aktion: Aktion, paket: Belegpaket, settings: Settings) -> bool:
    if not aktion.firma_id:
        return True
    if not paket.firma_id:
        return False
    if aktion.firma_id.casefold() == paket.firma_id.casefold():
        return True
    firma: Firma | None = settings.firmen.get(paket.firma_id)
    if firma is None:
        return False
    norm = normalisiere_name(aktion.firma_id)
    return bool(norm) and norm in {
        normalisiere_name(firma.kurz),
        normalisiere_name(firma.name),
        normalisiere_name(firma.lexware_firmenname),
    }


def bezugsdatum(paket: Belegpaket, settings: Settings, *, heute: date | None = None) -> tuple[date, str, bool]:
    """Bezugsdatum für den Zeitraumvergleich: ``(Datum, Beschreibung, aus dem Dokument?)``.

    ``rabatte.bezug = "liefertag"``: Liefertag, sonst Auftragsdatum, sonst heute;
    ``"auftragsdatum"``: Auftragsdatum, sonst Liefertag, sonst heute. Nur "heute" gilt nicht als aus
    dem Dokument (Treffer werden dann unsicher).
    """
    roh = paket.roh
    heute = heute or date.today()
    if settings.rabatte.bezug == "auftragsdatum":
        reihenfolge = (("Auftragsdatum", roh.datum), ("Liefertag", roh.liefertag))
    else:
        reihenfolge = (("Liefertag", roh.liefertag), ("Auftragsdatum", roh.datum))
    for name, wert in reihenfolge:
        if wert is not None:
            return wert, name, True
    return heute, "heute (Dokument ohne Liefertag und Datum)", False


# ---------------------------------------------------------------------------
# Kriterien
# ---------------------------------------------------------------------------


@dataclass
class _Kontext:
    paket: Belegpaket
    settings: Settings
    aliase: AliasTabellen | None

    def kunden_aliase(self) -> list[str]:
        kunde = self.paket.kunde.kunde
        if kunde is None or self.aliase is None:
            return []
        return self.aliase.aliase_fuer_kunde(kunde.kundennummer, kunde.firma_id)

    def lieferant_nummer_fuer(self, text: str) -> str | None:
        """Lieferantennummer zu einem Namen/Alias/einer Produktmarke (Aliase der Firma des Pakets)."""
        if self.aliase is None:
            return None
        treffer = self.aliase.lieferant(text, firma_id=self.paket.firma_id) or self.aliase.lieferant_fuer_marke(
            text, firma_id=self.paket.firma_id
        )
        return treffer.nummer if treffer is not None else None


def _kunde_passt(aktion: Aktion, kontext: _Kontext) -> str | None:
    """Grund, wenn der Kunde passt; ``None``, wenn nicht."""
    if not aktion.kundennummern and not aktion.kunde_kurz:
        return "alle Kunden"
    paket = kontext.paket
    kunde = paket.kunde.kunde
    if aktion.kundennummern and kunde is not None:
        nummern = {_nummer_norm(n) for n in aktion.kundennummern}
        if _nummer_norm(kunde.kundennummer) in nummern:
            return f"Kundennummer {kunde.kundennummer}"
    if not aktion.kunde_kurz:
        return None
    norm = normalisiere_name(aktion.kunde_kurz)
    if not norm:
        return None
    gruppe = paket.kundengruppe
    if gruppe is not None and gruppe is not Kundengruppe.UNBEKANNT:
        if norm == normalisiere_name(gruppe.value) or norm in _GRUPPEN_SYNONYME.get(gruppe, frozenset()):
            return f"Kundengruppe {gruppe.value}"
    kandidaten: list[tuple[str | None, str]] = []
    if kunde is not None:
        kandidaten.extend(
            [
                (kunde.matchcode, f"Matchcode {kunde.matchcode}"),
                (kunde.name, f"Kundenname {kunde.name}"),
                (kunde.name2, f"Kundenname {kunde.name2}"),
            ]
        )
        kandidaten.extend((alias, f"Kunden-Alias '{alias}'") for alias in kontext.kunden_aliase())
    kandidaten.append((paket.roh.kunde_text, f"Kundentext '{paket.roh.kunde_text}'"))
    for text, beschreibung in kandidaten:
        if _passt_wort(text, norm):
            return beschreibung
    return None


def _lieferant_passt(aktion: Aktion, position: PositionsZuordnung, kontext: _Kontext) -> str | None:
    """Grund, wenn der Lieferant der Position zur Aktion passt; ``None``, wenn nicht."""
    kurz = aktion.lieferant_kurz or ""
    norm = normalisiere_name(kurz)
    if not norm:
        return "alle Lieferanten"
    paket = kontext.paket
    artikel = position.artikel
    aktion_nummer = _nummer_norm(kontext.lieferant_nummer_fuer(kurz))
    if artikel is not None:
        if artikel.lieferantennummer:
            nummer = _nummer_norm(artikel.lieferantennummer)
            if nummer == _nummer_norm(kurz) or (aktion_nummer and nummer == aktion_nummer):
                return f"Lieferant {artikel.lieferantennummer} des Artikels"
        if _passt_wort(artikel.warengruppe, norm):
            return f"Warengruppe {artikel.warengruppe}"
        if artikel.lieferantennummer or artikel.warengruppe:
            # Der Artikel kennt sein Weingut, und es ist ein anderes (Kundenbestellung mit mehreren Weingütern).
            return None
    lieferant = paket.lieferant.lieferant
    if lieferant is not None:
        nummer = _nummer_norm(lieferant.lieferantennummer)
        if nummer == _nummer_norm(kurz) or (aktion_nummer and nummer == aktion_nummer):
            return f"Lieferant {lieferant.lieferantennummer}"
        for text in (lieferant.matchcode, lieferant.name, lieferant.name2):
            if _passt_wort(text, norm):
                return f"Lieferant {lieferant.name}"
    if artikel is None and kontext.aliase is not None:
        marke = kontext.aliase.lieferant_fuer_marke(position.roh.bezeichnung, firma_id=paket.firma_id)
        if marke is not None and aktion_nummer and _nummer_norm(marke.nummer) == aktion_nummer:
            return f"Produktmarke in '{position.roh.bezeichnung}'"
    if _passt_wort(paket.roh.lieferant_text, norm):
        return f"Lieferantentext '{paket.roh.lieferant_text}'"
    return None


def _artikel_score(ziel: str, text: str) -> float:
    """Ähnlichkeit Aktionstext <-> Artikelbezeichnung (beide über ``normalisiere_bezeichnung``).

    Grundlage ist :func:`~auftragsautomat.matching.aliase.aehnlichkeit`. Nennt die Aktion nur den
    Kern des Artikels ("Gloria Secco Frizzante" für "Villa Armellina Gloria Secco Frizzante"), kommen
    alle ihre Wörter (mindestens zwei) im Artikel vor; das zählt als :data:`SCORE_TEILMENGE`.
    Ein einzelnes Wort bleibt unscharf.
    """
    score = aehnlichkeit(ziel, text)
    ziel_woerter = ziel.split()
    if len(ziel_woerter) >= 2 and set(ziel_woerter) <= set(text.split()):
        score = max(score, SCORE_TEILMENGE)
    return score


def _artikel_passt(aktion: Aktion, position: PositionsZuordnung, kontext: _Kontext) -> tuple[bool, str] | None:
    """``(sicher, Grund)``, wenn Lieferant und Artikel der Position zur Aktion passen; sonst ``None``."""
    gruende: list[str] = []
    sicher = True
    if aktion.lieferant_kurz:
        grund = _lieferant_passt(aktion, position, kontext)
        if grund is None:
            return None
        gruende.append(grund)
    artikel = position.artikel
    roh = position.roh
    if aktion.artikelnummern:
        nummern = {_nummer_norm(n) for n in aktion.artikelnummern}
        for kandidat in (artikel.artikelnummer if artikel is not None else None, roh.artikelnummer):
            if kandidat and _nummer_norm(kandidat) in nummern:
                gruende.append(f"Artikelnummer {kandidat}")
                return True, "; ".join(gruende)
        if not aktion.artikel_text:
            return None
    if aktion.artikel_text:
        ziel = normalisiere_bezeichnung(aktion.artikel_text)
        if not ziel:
            return None
        texte = [t for t in ((artikel.bezeichnung if artikel is not None else None), roh.bezeichnung) if t]
        score = max((_artikel_score(ziel, normalisiere_bezeichnung(t)) for t in texte), default=0.0)
        konfig = kontext.settings.matching
        if score < konfig.schwelle_kandidat:
            return None
        volumen_aktion = parse_volumen_cl(aktion.artikel_text)
        volumen_artikel = artikel.volumen_cl if artikel is not None else (roh.volumen_cl or parse_volumen_cl(roh.bezeichnung))
        if volumen_aktion is not None and volumen_artikel is not None and volumen_aktion != volumen_artikel:
            return None
        if score >= konfig.schwelle_sicher:
            gruende.append(f"Artikeltext '{aktion.artikel_text}' passt ({score:.0f})")
        else:
            sicher = False
            gruende.append(f"Artikeltext '{aktion.artikel_text}' nur ähnlich ({score:.0f})")
        jahrgang_aktion = parse_jahrgang(aktion.artikel_text)
        jahrgang_artikel = artikel.jahrgang if artikel is not None else (roh.jahrgang or parse_jahrgang(roh.bezeichnung))
        if jahrgang_aktion is not None and jahrgang_artikel is not None and jahrgang_aktion != jahrgang_artikel:
            sicher = False
            gruende.append(f"Jahrgang {jahrgang_artikel} statt {jahrgang_aktion}")
        if volumen_aktion is None and artikel is not None and artikel.ist_sondervariante:
            sicher = False
            gruende.append("Artikel ist eine Sondervariante, die Aktion nennt kein Volumen")
        return sicher, "; ".join(gruende)
    gruende.append("alle Artikel des Lieferanten" if aktion.lieferant_kurz else "alle Artikel")
    return sicher, "; ".join(gruende)


def _zeitraum_passt(aktion: Aktion, datum: date, toleranz_tage: int) -> tuple[bool, str] | None:
    zeitraum = f"{aktion.von:%d.%m.%Y}–{aktion.bis:%d.%m.%Y}"
    if aktion.von <= datum <= aktion.bis:
        return True, f"im Zeitraum {zeitraum}"
    if toleranz_tage > 0:
        toleranz = timedelta(days=toleranz_tage)
        if aktion.von - toleranz <= datum <= aktion.bis + toleranz:
            abstand = (aktion.von - datum).days if datum < aktion.von else (datum - aktion.bis).days
            return False, f"{abstand} Tag(e) außerhalb des Zeitraums {zeitraum} (Toleranz {toleranz_tage} Tage)"
    return None


# ---------------------------------------------------------------------------
# Öffentlich: Umrechnung, Suche
# ---------------------------------------------------------------------------


def rabatt_prozent_aus(aktion: Aktion, vk_preis: Decimal | None) -> Decimal | None:
    """Prozentsatz einer Aktion für einen VK Preisgruppe 1 (zwei Nachkommastellen, kaufmännisch gerundet).

    Reihenfolge: ``rabatt_prozent`` direkt; sonst ``rabatt_eur`` -> ``eur / vk * 100``; sonst
    ``aktionspreis`` -> ``(vk - aktionspreis) / vk * 100``. Ohne VK (oder VK 0) bei EUR/Aktionspreis
    ``None``; ein Ergebnis von 0 oder darunter (Aktionspreis nicht unter dem VK) ebenfalls ``None``
    mit Hinweis im Log.
    """
    if aktion.rabatt_prozent is not None:
        prozent = aktion.rabatt_prozent
        if prozent <= 0:
            logger.warning("%s: Rabatt %s ist nicht positiv", _aktion_text(aktion), prozent)
            return None
        return prozent.quantize(_ZWEI_STELLEN, rounding=ROUND_HALF_UP)
    if aktion.rabatt_eur is None and aktion.aktionspreis is None:
        return None
    if vk_preis is None or vk_preis <= 0:
        logger.info("%s: EUR-Rabatt/Aktionspreis ohne VK nicht umrechenbar", _aktion_text(aktion))
        return None
    if aktion.rabatt_eur is not None:
        prozent = aktion.rabatt_eur / vk_preis * _HUNDERT
        art = f"Rabatt {_geld(aktion.rabatt_eur)}"
    else:
        prozent = (vk_preis - aktion.aktionspreis) / vk_preis * _HUNDERT  # type: ignore[operator]
        art = f"Aktionspreis {_geld(aktion.aktionspreis)}"
    prozent = prozent.quantize(_ZWEI_STELLEN, rounding=ROUND_HALF_UP)
    if prozent <= 0:
        logger.warning(
            "%s: %s ergibt bei VK %s keinen positiven Rabatt (%s) – nicht anwendbar",
            _aktion_text(aktion),
            art,
            _geld(vk_preis),
            _prozent_text(prozent),
        )
        return None
    if prozent > _HUNDERT:
        logger.warning("%s: %s übersteigt den VK %s – nicht anwendbar", _aktion_text(aktion), art, _geld(vk_preis))
        return None
    return prozent


def _aliase(settings: Settings, aliase: AliasTabellen | None) -> AliasTabellen | None:
    if aliase is not None:
        return aliase
    try:
        return AliasTabellen.lade(settings.alias_ordner)
    except AliasFehler as exc:
        logger.warning("Aliase für die Aktionssuche nicht ladbar: %s", exc)
        return None


def _markiere_mehrfachtreffer(treffer: list[AktionTreffer]) -> None:
    nach_position: dict[int | None, list[AktionTreffer]] = defaultdict(list)
    for t in treffer:
        nach_position[t.position_laufnr].append(t)
    for liste in nach_position.values():
        sichere = [t for t in liste if t.sicher and t.rabatt_prozent is not None]
        if len(sichere) < 2:
            continue
        saetze = {t.rabatt_prozent for t in sichere}
        ids = ", ".join(t.aktion.aktion_id for t in sichere)
        if len(saetze) == 1:
            for t in sichere[1:]:
                t.sicher = False
                t.grund += f"; gleicher Rabatt wie {sichere[0].aktion.aktion_id} (dort übernommen)"
            sichere[0].grund += f"; auch {', '.join(t.aktion.aktion_id for t in sichere[1:])} mit gleichem Rabatt"
            continue
        hoechster = max(sichere, key=lambda t: t.rabatt_prozent or Decimal(0))
        for t in sichere:
            t.sicher = False
            t.grund += (
                f"; mehrere Aktionen passen ({ids}) – höchster Rabatt {_prozent_text(hoechster.rabatt_prozent)} "
                f"aus {hoechster.aktion.aktion_id}, bitte wählen"
            )


def _sortierung(t: AktionTreffer) -> tuple[int, int, Decimal]:
    return (t.position_laufnr if t.position_laufnr is not None else -1, 0 if t.sicher else 1, -(t.rabatt_prozent or Decimal(0)))


def finde_aktionen(
    paket: Belegpaket,
    aktionen: list[Aktion],
    settings: Settings,
    *,
    heute: date | None = None,
    aliase: AliasTabellen | None = None,
) -> list[AktionTreffer]:
    """Alle Aktionen, die auf aktive Positionen des Pakets zutreffen (Kriterien siehe Modul-Docstring).

    ``heute`` ersetzt das Tagesdatum (Tests); ``aliase`` spart das Laden der Alias-Tabellen. Ergebnis
    nach Position sortiert, je Position sichere Treffer vor unsicheren, höchster Rabatt zuerst.
    """
    konfig = settings.rabatte
    if not konfig.aktiv:
        logger.debug("Rabatte sind deaktiviert (rabatte.aktiv = false)")
        return []
    aktive = [a for a in aktionen if a.aktiv]
    if not aktive or not paket.aktive_positionen():
        return []
    datum, bezug_text, aus_dokument = bezugsdatum(paket, settings, heute=heute)
    kontext = _Kontext(paket, settings, _aliase(settings, aliase))
    treffer: list[AktionTreffer] = []
    gesehen: set[str] = set()
    for aktion in aktive:
        if aktion.aktion_id in gesehen:
            logger.debug("Aktion %s doppelt – zweites Vorkommen ignoriert", aktion.aktion_id)
            continue
        gesehen.add(aktion.aktion_id)
        if not _firma_passt(aktion, paket, settings):
            continue
        kunde_grund = _kunde_passt(aktion, kontext)
        if kunde_grund is None:
            continue
        zeit = _zeitraum_passt(aktion, datum, konfig.toleranz_tage)
        if zeit is None:
            continue
        zeit_sicher, zeit_grund = zeit
        for position in paket.aktive_positionen():
            artikel = _artikel_passt(aktion, position, kontext)
            if artikel is None:
                continue
            artikel_sicher, artikel_grund = artikel
            rabatt = rabatt_prozent_aus(aktion, position.vk_preis)
            gruende = [kunde_grund, artikel_grund, f"{bezug_text} {datum:%d.%m.%Y} {zeit_grund}"]
            sicher = zeit_sicher and artikel_sicher and aus_dokument
            if rabatt is None:
                sicher = False
                gruende.append(
                    "Rabatt nicht berechenbar (VK fehlt oder Aktionspreis nicht unter dem VK)"
                    if aktion.rabatt_prozent is None
                    else "Rabatt ungültig"
                )
            treffer.append(
                AktionTreffer(
                    aktion=aktion,
                    position_laufnr=position.roh.laufnr,
                    rabatt_prozent=rabatt,
                    grund="; ".join(gruende),
                    sicher=sicher,
                )
            )
    _markiere_mehrfachtreffer(treffer)
    treffer.sort(key=_sortierung)
    if treffer:
        logger.info(
            "%s: %d Aktionstreffer (%d sicher) auf %d Position(en)",
            paket.roh.quelldatei.name,
            len(treffer),
            sum(1 for t in treffer if t.sicher),
            len({t.position_laufnr for t in treffer}),
        )
    return treffer


# ---------------------------------------------------------------------------
# Öffentlich: Anwenden
# ---------------------------------------------------------------------------


def _hinweis(
    code: Pruefcode, grad: Schweregrad, text: str, *, laufnr: int | None, **details: str
) -> Pruefhinweis:
    return Pruefhinweis(code=code, schweregrad=grad, text=text, position_laufnr=laufnr, details=details)


def _entferne_eigene(paket: Belegpaket) -> None:
    paket.hinweise = [h for h in paket.hinweise if h.code not in EIGENE_CODES]
    for position in paket.positionen:
        position.hinweise = [h for h in position.hinweise if h.code not in EIGENE_CODES]
        if position.rabatt_quelle and position.rabatt_quelle.startswith(QUELLE_PRAEFIX):
            position.rabatt_prozent = None
            position.rabatt_quelle = None


def _kandidaten_text(liste: list[AktionTreffer]) -> str:
    return "; ".join(f"{_aktion_text(t.aktion)} {_prozent_text(t.rabatt_prozent)} ({t.grund})" for t in liste)


def wende_an(paket: Belegpaket, treffer: list[AktionTreffer], settings: Settings) -> Belegpaket:
    """Trägt sichere Aktionsrabatte ein und erzeugt die Hinweise RABATT_VORSCHLAG/RABATT_UNSICHER
    (Regeln siehe Modul-Docstring). Mutiert ``paket`` und gibt es zurück."""
    konfig = settings.rabatte
    _entferne_eigene(paket)
    positionen = {p.roh.laufnr: p for p in paket.positionen}
    nach_position: dict[int | None, list[AktionTreffer]] = defaultdict(list)
    for t in treffer:
        t.angewendet = False
        nach_position[t.position_laufnr].append(t)

    for laufnr, liste in nach_position.items():
        liste.sort(key=_sortierung)
        if laufnr is None:
            paket.hinweise.append(
                _hinweis(
                    Pruefcode.RABATT_VORSCHLAG,
                    Schweregrad.INFO,
                    f"Aktionen auf Belegebene (nicht automatisch angewendet): {_kandidaten_text(liste)}",
                    laufnr=None,
                    kandidaten=",".join(t.aktion.aktion_id for t in liste),
                )
            )
            continue
        position = positionen.get(laufnr)
        if position is None or position.uebersprungen:
            continue
        bezeichnung = position.roh.bezeichnung
        if position.rabatt_prozent is not None or position.rabatt_quelle:
            quelle = position.rabatt_quelle or "ohne Quelle"
            position.hinweise.append(
                _hinweis(
                    Pruefcode.RABATT_VORSCHLAG,
                    Schweregrad.INFO,
                    f"Position {laufnr} '{bezeichnung}': Rabatt {_prozent_text(position.rabatt_prozent)} ({quelle}) "
                    f"bleibt; passende Aktionen: {_kandidaten_text(liste)}",
                    laufnr=laufnr,
                    rabatt_prozent=_prozent_text(position.rabatt_prozent),
                    rabatt_quelle=quelle,
                    kandidaten=",".join(t.aktion.aktion_id for t in liste),
                    angewendet="nein",
                )
            )
            continue
        sichere = [t for t in liste if t.sicher and t.rabatt_prozent is not None]
        if sichere:
            bester = sichere[0]
            rabatt = bester.rabatt_prozent
            assert rabatt is not None
            if rabatt > konfig.max_rabatt_prozent:
                logger.error(
                    "Position %s: Aktionsrabatt %s aus %s über der Grenze von %s",
                    laufnr,
                    _prozent_text(rabatt),
                    bester.aktion.aktion_id,
                    _prozent_text(konfig.max_rabatt_prozent),
                )
                position.hinweise.append(
                    _hinweis(
                        Pruefcode.RABATT_VORSCHLAG,
                        Schweregrad.FEHLER,
                        f"Position {laufnr} '{bezeichnung}': Aktionsrabatt {_prozent_text(rabatt)} "
                        f"({_aktion_text(bester.aktion)}) liegt über der Plausibilitätsgrenze von "
                        f"{_prozent_text(konfig.max_rabatt_prozent)} – Aktion prüfen oder Rabatt manuell setzen",
                        laufnr=laufnr,
                        aktion_id=bester.aktion.aktion_id,
                        rabatt_prozent=_prozent_text(rabatt),
                        max_rabatt_prozent=_prozent_text(konfig.max_rabatt_prozent),
                        angewendet="nein",
                    )
                )
                continue
            if konfig.automatisch_anwenden:
                position.rabatt_prozent = rabatt
                position.rabatt_quelle = f"{QUELLE_PRAEFIX}{bester.aktion.aktion_id}"
                bester.angewendet = True
                text = (
                    f"Position {laufnr} '{bezeichnung}': Aktionsrabatt {_prozent_text(rabatt)} übernommen "
                    f"({_aktion_text(bester.aktion)}: {bester.grund}) – VK bleibt Preisgruppe 1 "
                    f"{_geld(position.vk_preis)}, bitte prüfen"
                )
                logger.info(
                    "Position %s: Aktionsrabatt %s aus %s eingetragen", laufnr, _prozent_text(rabatt), bester.aktion.aktion_id
                )
            else:
                text = (
                    f"Position {laufnr} '{bezeichnung}': Aktionsrabatt {_prozent_text(rabatt)} vorgeschlagen "
                    f"({_aktion_text(bester.aktion)}: {bester.grund}) – nicht automatisch übernommen "
                    "(rabatte.automatisch_anwenden ist aus)"
                )
            position.hinweise.append(
                _hinweis(
                    Pruefcode.RABATT_VORSCHLAG,
                    Schweregrad.INFO,
                    text,
                    laufnr=laufnr,
                    aktion_id=bester.aktion.aktion_id,
                    rabatt_prozent=_prozent_text(rabatt),
                    quelle=bester.aktion.quelle,
                    angewendet="ja" if bester.angewendet else "nein",
                )
            )
            continue
        vorschlag = next((t.rabatt_prozent for t in liste if t.rabatt_prozent is not None), None)
        logger.warning("Position %s: Aktionen passen nur unscharf: %s", laufnr, _kandidaten_text(liste))
        position.hinweise.append(
            _hinweis(
                Pruefcode.RABATT_UNSICHER,
                Schweregrad.WARNUNG,
                f"Position {laufnr} '{bezeichnung}': Aktion passt nur unscharf, kein Rabatt eingetragen – "
                f"bitte entscheiden: {_kandidaten_text(liste)}",
                laufnr=laufnr,
                kandidaten=",".join(t.aktion.aktion_id for t in liste),
                rabatt_vorschlag=_prozent_text(vorschlag),
                angewendet="nein",
            )
        )

    paket.aktionen = list(treffer)
    return paket


# ---------------------------------------------------------------------------
# Öffentlich: alle Aktionen laden
# ---------------------------------------------------------------------------

_cache: dict[str, tuple[float, list[Aktion]]] = {}


def leere_cache() -> None:
    """Verwirft die zwischengespeicherten Aktionen (nach "Übernehmen" in der UI oder in Tests)."""
    _cache.clear()


def _cache_schluessel(settings: Settings) -> str:
    return f"{datei_schluessel(settings.rabatte.aktionen_datei)}|{settings.rabatte.aktionen_datei.blatt}|{settings.rabatte.gedaechtnis_datei}"


def alle_aktionen(settings: Settings, *, backends: Any | None = None, cache: bool = True) -> list[Aktion]:
    """Aktionen aus der Excel (falls konfiguriert) und dem Gedächtnis ``config/aktionen.yaml``.

    Die Excel wird nur gelesen, wenn ``rabatte.aktionen_datei`` eine ``web_url``, ``drive_id``/``item_id``
    oder einen ``lokaler_pfad`` hat; die Backends kommen aus ``backends`` (``m365.fabrik.Backends``
    oder ein Objekt mit ``excel``) oder aus :func:`~auftragsautomat.m365.fabrik.erzeuge_backends`.
    Jeder Fehler wird als Warnung protokolliert und der Rest geliefert; ohne Quellen ``[]``.
    Doppelte ``aktion_id`` (Excel vor Gedächtnis) werden einmal geliefert. Mit ``cache`` wird das
    Ergebnis :data:`CACHE_SEKUNDEN` lang je Konfiguration wiederverwendet.
    """
    konfig = settings.rabatte
    if not konfig.aktiv:
        return []
    schluessel = _cache_schluessel(settings)
    if cache:
        eintrag = _cache.get(schluessel)
        if eintrag is not None and time.monotonic() - eintrag[0] < CACHE_SEKUNDEN:
            return list(eintrag[1])

    aktionen: list[Aktion] = []
    ref = konfig.aktionen_datei
    if ref.web_url or (ref.drive_id and ref.item_id) or ref.lokaler_pfad is not None:
        try:
            if backends is None:
                from auftragsautomat.m365.fabrik import erzeuge_backends

                backends = erzeuge_backends(settings)
            excel = getattr(backends, "excel", backends)
            ergebnis = lese_aktionen(excel, ref, konfig, firmen=settings.firmen)
            for hinweis in ergebnis.fehler:
                logger.warning("Aktionen-Excel: %s", hinweis.text)
            aktionen.extend(ergebnis.aktionen)
        except Exception as exc:  # jede Störung der Excel-Quelle darf die Verarbeitung nicht stoppen
            logger.warning("Aktionen-Excel %s nicht lesbar: %s", datei_schluessel(ref), exc)
    else:
        logger.debug("Keine Aktionen-Excel konfiguriert (rabatte.aktionen_datei ohne web_url/IDs/lokaler_pfad)")

    try:
        aktionen.extend(lade_gespeicherte_aktionen(konfig.gedaechtnis_datei))
    except RabattlisteFehler as exc:
        logger.warning("%s", exc)

    eindeutig: dict[str, Aktion] = {}
    for aktion in aktionen:
        if aktion.aktion_id in eindeutig:
            logger.warning("Aktion %s mehrfach vorhanden – erste Fassung wird verwendet", aktion.aktion_id)
            continue
        eindeutig[aktion.aktion_id] = aktion
    ergebnis_liste = list(eindeutig.values())
    if cache:
        _cache[schluessel] = (time.monotonic(), list(ergebnis_liste))
    logger.info("%d Aktionen geladen (%s)", len(ergebnis_liste), schluessel)
    return ergebnis_liste
