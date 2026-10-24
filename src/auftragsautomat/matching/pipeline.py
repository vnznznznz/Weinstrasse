"""Matching-Pipeline: ``RohBestellung`` -> geprüftes ``Belegpaket``.

:func:`erstelle_belegpaket` orchestriert die Module des Matchings:

1. Aliase laden (``settings.alias_ordner``), sofern nicht übergeben.
2. Firma ermitteln (:func:`~auftragsautomat.matching.firma.ermittle_firma`: Eingangsordner,
   Firmen-Gedächtnis, Kunden-Alias, Firmenkatalog, Lieferanten-Alias, Stammdaten, Empfänger); eine
   übergebene ``firma_id`` gewinnt (Quelle ``"manuell"``), ebenso die Firma übergebener Stammdaten.
   ``Belegpaket.kundengruppe`` trägt die Einstufung laut Firmenkatalog.
3. Stammdaten der Firma laden (:func:`~auftragsautomat.stammdaten.laden.lade_stammdaten`, Cache).
4. Kunde zuordnen (:class:`~auftragsautomat.matching.partner.KundenMatcher`).
5. Positionen zuordnen (:class:`~auftragsautomat.matching.artikel.ArtikelMatcher`), zunächst
   ohne Lieferant.
6. Lieferant zuordnen (:class:`~auftragsautomat.matching.partner.LieferantenMatcher`) aus Text
   und Positionen.
7. Ist der Lieferant sicher, werden noch nicht sichere Positionen (UNSICHER, KEIN_TREFFER) erneut
   mit Lieferant gematcht; der Lieferantenbonus löst Mehrdeutigkeiten zwischen Weingütern auf.
   Ein zweites Ergebnis wird nur übernommen, wenn es nicht schlechter ist.
8. Durchgestrichene Positionen (Vision) werden als übersprungen markiert (Menge zählt nicht).
9. ``einkauf_erforderlich = Firma.einkauf_bestellung`` (Weinstrasse Agentur: False).
10. Aktionen/Rabatte (:mod:`~auftragsautomat.rabatte.anwenden`): die übergebenen ``aktionen`` (``None``
    = :func:`~auftragsautomat.rabatte.anwenden.alle_aktionen`, also Excel + ``config/aktionen.yaml``;
    ohne Quellen leer) werden gegen Kunde, Lieferant, Artikel und Zeitraum geprüft; sichere Treffer
    setzen ``rabatt_prozent``/``rabatt_quelle`` (Positionsrabatt im Verkauf, nie im Einkauf).
11. Belegfelder vorbelegen und prüfen (:func:`~auftragsautomat.matching.pruefung.pruefe`).

Ohne ermittelbare Firma entsteht ein Paket ohne Zuordnungen; die Prüfung blockiert es mit
``FIRMA_UNKLAR`` (FEHLER), die Freigabe-UI kann dann :func:`setze_firma` aufrufen.

:func:`pruefe_erneut` wiederholt nach Korrekturen in der Freigabe (manuell gewählte Artikel,
geänderte Mengen, neu gespeicherte Aliase) die Preise aus dem Artikelstamm, matcht offene Kunden,
Positionen und den Lieferanten erneut und prüft. :func:`setze_firma` matcht alles mit einer
anderen Firma neu.

Fehlende oder kaputte Stammdaten (``StammdatenFehler``) werden nicht verschluckt: ohne Stamm ist
kein Matching möglich, der Aufrufer (CLI, Watcher) meldet den Fehler je Datei.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from auftragsautomat.config import Settings
from auftragsautomat.matching.aliase import AliasTabellen
from auftragsautomat.matching.artikel import ArtikelMatcher
from auftragsautomat.matching.firma import QUELLE_MANUELL, StammdatenLader, ermittle_firma
from auftragsautomat.matching.firmenwahl import Firmenkatalog, merke_firma
from auftragsautomat.matching.partner import KundenMatcher, LieferantenMatcher
from auftragsautomat.matching.pruefung import pruefe
from auftragsautomat.models import (
    Aktion,
    Belegpaket,
    Firmenregel,
    Kundengruppe,
    MatchStatus,
    PositionsZuordnung,
    Pruefcode,
    RohBestellung,
)
from auftragsautomat.rabatte.anwenden import alle_aktionen, finde_aktionen, wende_an
from auftragsautomat.stammdaten import laden
from auftragsautomat.stammdaten.basis import Stammdaten

__all__ = ["erstelle_belegpaket", "merke_firma_fuer", "pruefe_erneut", "setze_firma"]

logger = logging.getLogger(__name__)

# Rangfolge der Positionsstatus für die Entscheidung, ob ein zweiter Matching-Durchlauf
# ein Ergebnis verbessert hat.
_RANG: dict[MatchStatus, int] = {
    MatchStatus.SICHER: 3,
    MatchStatus.MANUELL: 3,
    MatchStatus.UNSICHER: 2,
    MatchStatus.UEBERSPRUNGEN: 1,
    MatchStatus.KEIN_TREFFER: 0,
}
_OFFEN = (MatchStatus.UNSICHER, MatchStatus.KEIN_TREFFER)


# ---------------------------------------------------------------------------
# Hilfen
# ---------------------------------------------------------------------------


def _aliase(settings: Settings, aliase: AliasTabellen | None) -> AliasTabellen:
    if aliase is not None:
        return aliase
    return AliasTabellen.lade(settings.alias_ordner)


def _stammdaten(settings: Settings, firma_id: str | None, stammdaten: Stammdaten | None) -> Stammdaten | None:
    """Stammdaten der Firma: die übergebenen (Firma muss passen) oder aus dem Lader (Cache)."""
    if stammdaten is not None:
        if firma_id is not None and stammdaten.firma_id != firma_id:
            raise ValueError(
                f"Übergebene Stammdaten gehören zu Firma '{stammdaten.firma_id}', erwartet '{firma_id}'"
            )
        return stammdaten
    if firma_id is None:
        return None
    return laden.lade_stammdaten(settings.firma(firma_id))


def _kundengruppe_laut_katalog(roh: RohBestellung, settings: Settings) -> Kundengruppe | None:
    """Kundengruppe laut Firmenkatalog, auch wenn die Firma manuell vorgegeben ist (für Anzeige und
    Gedächtnis); ``None``, wenn kein Muster passt."""
    klassifikation = Firmenkatalog.aus_settings(settings).klassifiziere(
        roh.kunde_text, roh.kunde_adresse, roh.kundennummer_dokument, lieferant_text=roh.lieferant_text
    )
    if klassifikation.kundengruppe is Kundengruppe.UNBEKANNT:
        return None
    return klassifikation.kundengruppe


def _nicht_schlechter(alt: PositionsZuordnung, neu: PositionsZuordnung) -> PositionsZuordnung:
    """Zweites Matching-Ergebnis nur übernehmen, wenn es nicht schlechter ist; Mengenkorrekturen
    des Anwenders (Menge weicht von der Dokumentmenge ab) bleiben erhalten."""
    if _RANG[neu.status] < _RANG[alt.status]:
        logger.debug(
            "Position %s: zweiter Durchlauf (%s) schlechter als erster (%s); behalte erstes Ergebnis",
            alt.roh.laufnr,
            neu.status.value,
            alt.status.value,
        )
        return alt
    dokumentmenge = alt.roh.menge if alt.roh.menge is not None else Decimal(0)
    if alt.menge != dokumentmenge and not alt.uebersprungen:
        neu.menge = alt.menge
    return neu


def _markiere_durchgestrichen(position: PositionsZuordnung) -> PositionsZuordnung:
    """Durchgestrichene Position (Vision): bleibt sichtbar, zählt aber nicht (übersprungen, Menge 0)."""
    if position.roh.durchgestrichen and not position.uebersprungen:
        logger.info(
            "Position %s '%s' ist durchgestrichen und wird übersprungen",
            position.roh.laufnr,
            position.roh.bezeichnung,
        )
        position.status = MatchStatus.UEBERSPRUNGEN
        position.menge = Decimal(0)
    return position


def _matche(paket: Belegpaket, stammdaten: Stammdaten, aliase: AliasTabellen, settings: Settings) -> None:
    """Kunde, Positionen (zwei Durchläufe) und Lieferant für ``paket`` zuordnen."""
    roh = paket.roh
    konfig = settings.matching
    paket.kunde = KundenMatcher(stammdaten, aliase, konfig).finde(
        roh.kunde_text, roh.kunde_adresse, kundennummer=roh.kundennummer_dokument
    )

    artikel_matcher = ArtikelMatcher(stammdaten, aliase, konfig)
    paket.positionen = [artikel_matcher.finde(position) for position in roh.positionen]

    paket.lieferant = LieferantenMatcher(stammdaten, aliase, konfig).finde(
        roh.lieferant_text, paket.positionen
    )

    lieferant = paket.lieferant.lieferant
    if lieferant is not None:
        for i, position in enumerate(paket.positionen):
            if position.status in _OFFEN and not position.roh.durchgestrichen:
                neu = artikel_matcher.finde(position.roh, lieferant)
                paket.positionen[i] = _nicht_schlechter(position, neu)
                if paket.positionen[i] is neu:
                    logger.info(
                        "Position %s im zweiten Durchlauf mit Lieferant %s: %s",
                        position.roh.laufnr,
                        lieferant.lieferantennummer,
                        neu.status.value,
                    )
    paket.positionen = [_markiere_durchgestrichen(p) for p in paket.positionen]


def _rabatte_anwenden(
    paket: Belegpaket, settings: Settings, aktionen: list[Aktion] | None, aliase: AliasTabellen
) -> None:
    """Aktionen nach der Artikelzuordnung anwenden (``None`` = alle konfigurierten Quellen)."""
    if aktionen is None:
        aktionen = alle_aktionen(settings)
    treffer = finde_aktionen(paket, aktionen, settings, aliase=aliase)
    wende_an(paket, treffer, settings)


# ---------------------------------------------------------------------------
# Öffentlich
# ---------------------------------------------------------------------------


def erstelle_belegpaket(
    roh: RohBestellung,
    settings: Settings,
    *,
    firma_id: str | None = None,
    stammdaten: Stammdaten | None = None,
    aliase: AliasTabellen | None = None,
    stammdaten_lader: StammdatenLader | None = None,
    aktionen: list[Aktion] | None = None,
) -> Belegpaket:
    """Baut aus einer Rohbestellung das geprüfte Belegpaket (Ablauf siehe Modul-Docstring).

    ``firma_id`` erzwingt die Firma (Quelle "manuell"); ``stammdaten``/``aliase`` ersetzen das
    Laden; ``stammdaten_lader`` wird nur für die Firmen-Ermittlung über alle Stämme benutzt.
    ``aktionen`` sind die Aktionszeiträume für Positionsrabatte (``None`` = alle konfigurierten
    Quellen über :func:`~auftragsautomat.rabatte.anwenden.alle_aktionen`, ``[]`` = keine).
    Unbekannte ``firma_id`` -> ``KeyError`` (Konfigurationsfehler).
    """
    aliase = _aliase(settings, aliase)
    logger.info("Erstelle Belegpaket für %s", roh.quelldatei)

    if firma_id is None and stammdaten is not None:
        firma_id = stammdaten.firma_id
    if firma_id is not None:
        settings.firma(firma_id)  # KeyError bei unbekannter Firma
        paket = Belegpaket(
            roh=roh,
            firma_id=firma_id,
            firma_quelle=QUELLE_MANUELL,
            kundengruppe=_kundengruppe_laut_katalog(roh, settings),
        )
        logger.info("Firma %s manuell vorgegeben", firma_id)
    else:
        ergebnis = ermittle_firma(roh, settings, aliase, stammdaten_lader=stammdaten_lader)
        paket = Belegpaket(
            roh=roh,
            firma_id=ergebnis.firma_id,
            firma_quelle=ergebnis.quelle,
            hinweise=list(ergebnis.hinweise),
            kundengruppe=ergebnis.kundengruppe,
        )

    # Revised Order laut Dokument: im Paket sichtbar machen (Freigabe, Benennung "Neu"/"Neu Kopie", Dublettenprüfung)
    paket.ist_revision = roh.ist_revision
    paket.revision_von = roh.revision_von
    daten = _stammdaten(settings, paket.firma_id, stammdaten)
    if paket.firma_id is not None:
        paket.einkauf_erforderlich = settings.firma(paket.firma_id).einkauf_bestellung
    if daten is not None:
        _matche(paket, daten, aliase, settings)
    else:
        # Ohne Firma kein Stamm: Positionen unzugeordnet übernehmen, damit die UI sie zeigt.
        paket.positionen = [
            _markiere_durchgestrichen(
                PositionsZuordnung(
                    roh=position,
                    menge=position.menge if position.menge is not None else Decimal(0),
                    preis_dokument=position.preis,
                )
            )
            for position in roh.positionen
        ]
        logger.warning("Keine Firma für %s; Matching übersprungen", roh.quelldatei)

    _rabatte_anwenden(paket, settings, aktionen, aliase)
    return pruefe(paket, daten, settings)


def pruefe_erneut(
    paket: Belegpaket,
    settings: Settings,
    *,
    stammdaten: Stammdaten | None = None,
    aliase: AliasTabellen | None = None,
    aktionen: list[Aktion] | None = None,
) -> Belegpaket:
    """Nach Korrekturen in der Freigabe: Preise aus dem Artikelstamm neu setzen, offene Kunden-,
    Positions- und Lieferantenzuordnungen erneut versuchen (neue Aliase greifen), Aktionsrabatte
    neu anwenden (``aktionen`` wie bei :func:`erstelle_belegpaket`; manuell gesetzte Rabatte bleiben)
    und prüfen. Manuelle Entscheidungen (Status ``MANUELL``, gewählte Partner, korrigierte Mengen,
    gefüllte Belegfelder) bleiben erhalten. Mutiert ``paket`` und gibt es zurück."""
    aliase = _aliase(settings, aliase)
    daten = _stammdaten(settings, paket.firma_id, stammdaten)
    if daten is None:
        logger.warning("Erneute Prüfung ohne Firma/Stammdaten: nur die Prüfung läuft")
        _rabatte_anwenden(paket, settings, aktionen, aliase)
        return pruefe(paket, None, settings)

    konfig = settings.matching
    roh = paket.roh

    for position in paket.positionen:
        if position.artikel is not None:
            artikel = daten.artikel_nach_nummer.get(position.artikel.artikelnummer, position.artikel)
            if artikel is not position.artikel:
                logger.debug(
                    "Position %s: Artikel %s aus dem aktuellen Stamm übernommen",
                    position.roh.laufnr,
                    artikel.artikelnummer,
                )
            position.artikel = artikel
            position.ek_preis = artikel.ek_preis
            position.vk_preis = artikel.vk_preis_1
        position.preis_dokument = position.roh.preis

    if paket.kunde.kunde is None and paket.kunde.status != MatchStatus.MANUELL:
        paket.kunde = KundenMatcher(daten, aliase, konfig).finde(
            roh.kunde_text, roh.kunde_adresse, kundennummer=roh.kundennummer_dokument
        )

    lieferant = paket.lieferant.lieferant
    artikel_matcher = ArtikelMatcher(daten, aliase, konfig)
    for i, position in enumerate(paket.positionen):
        if position.artikel is None and position.status in _OFFEN and not position.roh.durchgestrichen:
            neu = artikel_matcher.finde(position.roh, lieferant)
            paket.positionen[i] = _nicht_schlechter(position, neu)

    if paket.lieferant.lieferant is None and paket.lieferant.status != MatchStatus.MANUELL:
        paket.lieferant = LieferantenMatcher(daten, aliase, konfig).finde(
            roh.lieferant_text, paket.positionen
        )
        if paket.lieferant.lieferant is not None:
            logger.info(
                "Lieferant %s nach Korrektur aus den Positionen abgeleitet",
                paket.lieferant.lieferant.lieferantennummer,
            )

    paket.positionen = [_markiere_durchgestrichen(p) for p in paket.positionen]
    _rabatte_anwenden(paket, settings, aktionen, aliase)
    return pruefe(paket, daten, settings)


def setze_firma(
    paket: Belegpaket,
    firma_id: str,
    settings: Settings,
    *,
    stammdaten: Stammdaten | None = None,
    aliase: AliasTabellen | None = None,
    merken: bool = False,
    benutzer: str | None = None,
) -> Belegpaket:
    """Ordnet das Paket einer anderen Firma zu und matcht alles neu (Quelle "manuell").

    Liefert ein **neues** Belegpaket aus ``paket.roh``; Belegfelder werden für die neue Firma
    vorbelegt, ``lieferschein_erstellen`` und die Kundengruppe laut Katalog werden übernommen.
    ``merken`` (Häkchen "für diesen Kunden und Lieferanten merken" in der Freigabe) setzt
    ``firma_merken`` am neuen Paket und schreibt die Wahl über
    :func:`~auftragsautomat.matching.firmenwahl.merke_firma` ins Firmen-Gedächtnis (``benutzer`` =
    "gelernt von"); Schreibfehler landen nur im Log. Die Freigabe muss danach erneut erfolgen.
    Unbekannte ``firma_id`` -> ``KeyError``.
    """
    settings.firma(firma_id)
    alte = [h for h in paket.hinweise if h.code == Pruefcode.FIRMA_UNKLAR]
    logger.info(
        "Firma für %s manuell auf %s gesetzt (vorher %s über %s)%s",
        paket.roh.quelldatei,
        firma_id,
        paket.firma_id or "-",
        paket.firma_quelle or "-",
        f"; {len(alte)} FIRMA_UNKLAR-Hinweis(e) verworfen" if alte else "",
    )
    neu = erstelle_belegpaket(paket.roh, settings, firma_id=firma_id, stammdaten=stammdaten, aliase=aliase)
    neu.lieferschein_erstellen = paket.lieferschein_erstellen
    if neu.kundengruppe is None:
        neu.kundengruppe = paket.kundengruppe
    neu.firma_merken = bool(merken)
    if merken:
        merke_firma_fuer(neu, settings, benutzer=benutzer)
    return neu


def merke_firma_fuer(paket: Belegpaket, settings: Settings, *, benutzer: str | None = None) -> Firmenregel | None:
    """Schreibt die Firma des Pakets ins Firmen-Gedächtnis (wenn ``paket.firma_merken`` und
    ``settings.firmenwahl.merken``); Schreibfehler blockieren die Freigabe nicht (nur Log)."""
    try:
        return merke_firma(paket, settings, benutzer=benutzer)
    except (OSError, ValueError) as exc:
        logger.error("Firma %s konnte nicht gemerkt werden: %s", paket.firma_id, exc)
        return None
