"""Prüfung eines Belegpakets (Preise, Summen, Mengen, Partner, Dubletten, Belegfelder, Firma).

:func:`pruefe` ergänzt ``paket.hinweise`` und die Hinweise der Positionen um die Prüf-Hinweise und
liefert das (mutierte) Paket zurück. Sie ist **idempotent**: vorher werden alle Hinweise entfernt,
die die Prüfung selbst erzeugt (Codes in :data:`PRUEF_CODES`), so dass sie nach Korrekturen in der
Freigabe beliebig oft wiederholt werden kann. Hinweise der Extraktion (``roh.hinweise``) und des
Matchings (``KEIN_ARTIKEL``, ``ARTIKEL_UNSICHER``, ``VARIANTE_MANUELL``, ``NULLPREIS_OHNE_ARTIKEL``)
bleiben erhalten; ebenso zwei Hinweise fremder Herkunft, die zwar Prüf-Codes tragen, aber nicht
aus der Prüfung stammen: ``PREIS_ABWEICHUNG`` als INFO ("Preis 0,00 im Dokument", Artikel-Matching)
und ``FIRMA_UNKLAR`` als WARNUNG (Ordner widerspricht Alias bzw. mehrdeutige Kandidaten,
``matching.firma``).

Regeln (docs/ARCHITEKTUR.md, Geschäftsregeln 1, 5, 6, 8, 9):

* Firma: ``firma_id`` leer oder unbekannt -> ``FIRMA_UNKLAR`` (FEHLER); Kunde/Lieferant/Artikel
  aus einer anderen Firma -> ``FIRMA_UNKLAR`` (FEHLER).
* Kunde fehlt -> ``KEIN_KUNDE`` (FEHLER); bei Kandidaten zusätzlich ``KUNDE_UNSICHER`` (WARNUNG).
* Lieferant fehlt und Einkauf erforderlich -> ``KEIN_LIEFERANT`` (FEHLER), bei Kandidaten
  zusätzlich ``LIEFERANT_UNSICHER`` (WARNUNG). Ohne Einkauf (Weinstrasse Agentur) entfällt beides.
* Positionen mehrerer Weingüter (Artikel mit anderem Lieferanten als dem Hauptlieferanten,
  :func:`gruppiere_nach_lieferant`) -> ``MEHRERE_LIEFERANTEN`` (WARNUNG): je Weingut legt die
  Buchung eine eigene Einkauf-Bestellung an (Geschäftsregel 2: Lieferant = Weingut). Gehört keine
  Position zum zugeordneten Lieferanten -> ``MEHRERE_LIEFERANTEN`` (FEHLER).
* Je aktiver (nicht übersprungener) Position: Menge fehlt/0 -> ``MENGE_FEHLT`` (FEHLER);
  EK fehlt (nur bei Einkauf) -> ``EK_FEHLT`` (FEHLER); VK fehlt -> ``VK_FEHLT`` (FEHLER);
  ``|vk_preis - preis_dokument| > preis_toleranz_eur`` -> ``PREIS_ABWEICHUNG`` (WARNUNG, rot;
  Betrag und Prozent in ``details``). Positionen ohne Artikel (UNSICHER) erhalten einen
  ``VK_FEHLT``-Blocker, bis der Anwender einen Kandidaten wählt; KEIN_TREFFER blockiert bereits
  über ``KEIN_ARTIKEL`` aus dem Matching.
* Summenvermerke ("180 R", "Gesamt 72 Fl."): jeder Vermerk gegen ``paket.gesamtmenge``; Einheit
  "R", Flaschen, Stück oder unbekannt = Flaschen; Kartons/Kisten/VE -> nicht vergleichbar (INFO).
  Abweichung -> ``SUMME_ABWEICHUNG`` (WARNUNG); stimmt der Vermerk mit der Dokumentmenge
  einschließlich übersprungener Positionen überein, nur INFO.
* Dubletten (:func:`pruefe_dubletten`): Belegnummer oder Kundenbestellnummer des Dokuments gleich
  Belegnummer oder Kundenbestellnummer eines vorhandenen Belegs (normalisiert: Groß/Klein,
  Leerraum/Trennzeichen, führende Nullen) und Partner gleich (oder im Beleg leer) -> ``DUBLETTE`` (FEHLER).
* Belegfelder (Spedition, Weingut, Shipment, Kunde): leere Felder werden **vor** der Prüfung
  vorbelegt (:func:`belegfelder_vorbelegen`); danach je fehlendem Feld ``BELEGFELD_FEHLT`` (FEHLER).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal

from auftragsautomat.config import Firma, Settings
from auftragsautomat.matching.aliase import normalisiere_name
from auftragsautomat.models import (
    Belegart,
    Belegfelder,
    Belegpaket,
    Lieferant,
    MatchStatus,
    PositionsZuordnung,
    Pruefcode,
    Pruefhinweis,
    Schweregrad,
    Summenvermerk,
)
from auftragsautomat.stammdaten.basis import Stammdaten

__all__ = [
    "BELEGFELD_NAMEN",
    "EINHEITEN_FLASCHEN",
    "EINHEITEN_GEBINDE",
    "PRUEF_CODES",
    "LieferantenGruppe",
    "belegfelder_vorbelegen",
    "entferne_pruefhinweise",
    "gruppiere_nach_lieferant",
    "ist_pruefhinweis",
    "lieferant_der_position",
    "normalisiere_belegnummer",
    "pruefe",
    "pruefe_dubletten",
    "zusammenfassung_hinweise",
]

logger = logging.getLogger(__name__)

#: Hinweis-Codes, die diese Prüfung erzeugt und vor jedem Lauf entfernt (Idempotenz).
PRUEF_CODES: frozenset[Pruefcode] = frozenset(
    {
        Pruefcode.PREIS_ABWEICHUNG,
        Pruefcode.SUMME_ABWEICHUNG,
        Pruefcode.MENGE_FEHLT,
        Pruefcode.EK_FEHLT,
        Pruefcode.VK_FEHLT,
        Pruefcode.KEIN_KUNDE,
        Pruefcode.KUNDE_UNSICHER,
        Pruefcode.KEIN_LIEFERANT,
        Pruefcode.LIEFERANT_UNSICHER,
        Pruefcode.MEHRERE_LIEFERANTEN,
        Pruefcode.DUBLETTE,
        Pruefcode.BELEGFELD_FEHLT,
        Pruefcode.FIRMA_UNKLAR,
    }
)

#: Reihenfolge der Belegfelder, wie Lexware sie zeigt (Spedition, Weingut, Shipment, Kunde).
BELEGFELD_NAMEN: tuple[str, ...] = ("spedition", "weingut", "shipment", "kunde")
#: Trenner im Belegfeld Weingut, wenn eine Kundenbestellung Weine mehrerer Weingüter enthält.
WEINGUT_TRENNER = " / "

#: Einheiten von Summenvermerken, die Flaschen meinen ("R" = Reihen/Flaschen in den Handschriften).
EINHEITEN_FLASCHEN: frozenset[str] = frozenset(
    {
        "",
        "r",
        "fl",
        "fla",
        "flasche",
        "flaschen",
        "stk",
        "st",
        "stueck",
        "stück",
        "btl",
        "bt",
        "bottle",
        "bottles",
        "bott",
    }
)
#: Einheiten, die Gebinde meinen und sich nicht mit der Flaschenmenge vergleichen lassen.
EINHEITEN_GEBINDE: frozenset[str] = frozenset(
    {
        "kt",
        "ktn",
        "karton",
        "kartons",
        "kiste",
        "kisten",
        "ve",
        "colli",
        "collo",
        "ctn",
        "case",
        "cases",
        "pal",
        "palette",
        "paletten",
        "pallet",
        "pallets",
        "ohk",
        "hk",
        "holzkiste",
        "holzkisten",
    }
)

_MAX_KANDIDATEN_TEXT = 5


# ---------------------------------------------------------------------------
# Hilfen
# ---------------------------------------------------------------------------


def _geld(wert: Decimal | None) -> str:
    if wert is None:
        return "-"
    return f"{wert.quantize(Decimal('0.01')):f}".replace(".", ",")


def _menge(wert: Decimal | None) -> str:
    if wert is None:
        return "-"
    wert = wert.normalize()
    if wert == wert.to_integral_value():
        return str(int(wert))
    return f"{wert:f}".replace(".", ",")


def _prozent(abweichung: Decimal, basis: Decimal) -> str:
    if basis == 0:
        return "-"
    return f"{(abs(abweichung) / abs(basis) * 100).quantize(Decimal('0.1')):f}".replace(".", ",")


def _hinweis(
    code: Pruefcode,
    grad: Schweregrad,
    text: str,
    *,
    laufnr: int | None = None,
    **details: str,
) -> Pruefhinweis:
    return Pruefhinweis(code=code, schweregrad=grad, text=text, position_laufnr=laufnr, details=details)


def _firma(settings: Settings, firma_id: str | None) -> Firma | None:
    if not firma_id:
        return None
    return settings.firmen.get(firma_id)


def ist_pruefhinweis(hinweis: Pruefhinweis) -> bool:
    """``True``, wenn der Hinweis von :func:`pruefe` stammt (und vor einem neuen Lauf entfernt wird).

    Ausgenommen: ``PREIS_ABWEICHUNG`` mit INFO (Artikel-Matching, "Preis 0,00 im Dokument") und
    ``FIRMA_UNKLAR`` mit WARNUNG (Firmen-Ermittlung: Ordner widerspricht Alias / Kandidaten).
    """
    if hinweis.code not in PRUEF_CODES:
        return False
    if hinweis.code == Pruefcode.PREIS_ABWEICHUNG and hinweis.schweregrad == Schweregrad.INFO:
        return False
    if hinweis.code == Pruefcode.FIRMA_UNKLAR and hinweis.schweregrad == Schweregrad.WARNUNG:
        return False
    return True


def entferne_pruefhinweise(paket: Belegpaket) -> int:
    """Entfernt alle Hinweise der Prüfung aus Paket und Positionen; liefert die Anzahl."""
    vorher = len(paket.hinweise) + sum(len(p.hinweise) for p in paket.positionen)
    paket.hinweise = [h for h in paket.hinweise if not ist_pruefhinweis(h)]
    for position in paket.positionen:
        position.hinweise = [h for h in position.hinweise if not ist_pruefhinweis(h)]
    entfernt = vorher - len(paket.hinweise) - sum(len(p.hinweise) for p in paket.positionen)
    if entfernt:
        logger.debug("%d alte Prüfhinweise entfernt", entfernt)
    return entfernt


# ---------------------------------------------------------------------------
# Belegfelder
# ---------------------------------------------------------------------------


def belegfelder_vorbelegen(
    paket: Belegpaket, settings: Settings, stammdaten: Stammdaten | None = None
) -> Belegfelder:
    """Füllt leere Belegfelder: Weingut = Lieferantenname (bei Positionen mehrerer Weingüter alle Namen,
    Hauptlieferant zuerst, mit " / " getrennt), Kunde = Kundenname, Shipment = Liefertag (dd.mm.yyyy),
    danach alles noch Leere aus ``Firma.belegfelder_standard`` (z. B. Spedition). Bereits gefüllte
    Felder (etwa vom Anwender) bleiben unverändert; nur eine frühere Weingut-Vorbelegung wird erneuert."""
    felder = paket.belegfelder
    firma = _firma(settings, paket.firma_id)

    lieferant = paket.lieferant.lieferant
    if lieferant is not None:
        aktuell = felder.weingut or ""
        # Nur eine (frühere) Vorbelegung wird erneuert, damit nach Korrekturen (manuell gewählter Artikel
        # eines weiteren Weinguts) alle Weingüter im Feld stehen; ein abweichender Anwendertext bleibt.
        vorbelegt = not aktuell or aktuell == lieferant.name or aktuell.startswith(lieferant.name + WEINGUT_TRENNER)
        if vorbelegt:
            namen = [lieferant.name]
            for gruppe in gruppiere_nach_lieferant(paket, stammdaten):
                if not gruppe.haupt and gruppe.lieferant.name not in namen:
                    namen.append(gruppe.lieferant.name)
            felder.weingut = WEINGUT_TRENNER.join(namen)
    elif not felder.weingut and not paket.einkauf_erforderlich and (paket.roh.lieferant_text or "").strip():
        # Firma ohne Einkauf-Bestellung (Weinstrasse Agentur): kein Lieferant im Stamm nötig,
        # das Weingut laut Dokument füllt das Belegfeld
        felder.weingut = paket.roh.lieferant_text.strip()  # type: ignore[union-attr]
    if not felder.kunde and paket.kunde.kunde is not None:
        felder.kunde = paket.kunde.kunde.name
    if not felder.shipment and paket.roh.liefertag is not None:
        # TODO-LEXWARE: prüfen, ob das Belegfeld "Shipment" den Liefertag (dd.mm.yyyy) oder etwas anderes erwartet.
        felder.shipment = paket.roh.liefertag.strftime("%d.%m.%Y")

    if firma is not None:
        for name, wert in firma.belegfelder_standard.items():
            schluessel = name.strip().lower()
            if schluessel not in BELEGFELD_NAMEN:
                logger.warning("Firma %s: belegfelder_standard enthält unbekanntes Feld %r", firma.id, name)
                continue
            if not getattr(felder, schluessel) and wert:
                setattr(felder, schluessel, str(wert))
    logger.debug("Belegfelder nach Vorbelegung: %s", felder.model_dump())
    return felder


def _pruefe_belegfelder(paket: Belegpaket) -> list[Pruefhinweis]:
    hinweise: list[Pruefhinweis] = []
    for feld in paket.belegfelder.fehlende():
        hinweise.append(
            _hinweis(
                Pruefcode.BELEGFELD_FEHLT,
                Schweregrad.FEHLER,
                f"Belegfeld '{feld}' ist leer (Spedition, Weingut, Shipment und Kunde müssen gefüllt sein)",
                feld=feld,
            )
        )
    return hinweise


# ---------------------------------------------------------------------------
# Lieferanten der Positionen (mehrere Weingüter in einer Kundenbestellung)
# ---------------------------------------------------------------------------


@dataclass
class LieferantenGruppe:
    """Aktive Positionen, die zu einem Weingut gehören (je Gruppe eine Einkauf-Bestellung)."""

    lieferant: Lieferant
    positionen: list[PositionsZuordnung] = field(default_factory=list)
    haupt: bool = False  # Gruppe des in ``paket.lieferant`` zugeordneten Hauptlieferanten

    @property
    def laufnummern(self) -> list[int]:
        return [p.roh.laufnr for p in self.positionen]

    @property
    def beschreibung(self) -> str:
        return f"{self.lieferant.lieferantennummer} {self.lieferant.name}"


def _nummer_varianten(nummer: str) -> list[str]:
    roh = str(nummer).strip()
    varianten = [roh, roh.upper(), roh.lstrip("0"), roh.upper().lstrip("0")]
    return [v for i, v in enumerate(varianten) if v and v not in varianten[:i]]


def _lieferant_nach_nummer(stammdaten: Stammdaten, nummer: str | None) -> Lieferant | None:
    if not nummer:
        return None
    for variante in _nummer_varianten(nummer):
        lieferant = stammdaten.lieferanten_nach_nummer.get(variante)
        if lieferant is not None:
            return lieferant
    return None


def lieferant_der_position(position: PositionsZuordnung, stammdaten: Stammdaten | None) -> Lieferant | None:
    """Weingut eines zugeordneten Artikels: ``Artikel.lieferantennummer``, sonst die Warengruppe
    (= Weingut) über Name/Name2/Matchcode der Lieferanten; ``None``, wenn nicht ermittelbar."""
    artikel = position.artikel
    if artikel is None or stammdaten is None:
        return None
    lieferant = _lieferant_nach_nummer(stammdaten, artikel.lieferantennummer)
    if lieferant is not None:
        return lieferant
    # TODO-LEXWARE: Annahme, dass die Warengruppe im Artikelstamm dem Weingut/Lieferanten entspricht.
    norm = normalisiere_name(artikel.warengruppe)
    if not norm:
        return None
    for lf in stammdaten.lieferanten:
        if norm in {normalisiere_name(lf.name), normalisiere_name(lf.name2), normalisiere_name(lf.matchcode)}:
            return lf
    return None


def gruppiere_nach_lieferant(paket: Belegpaket, stammdaten: Stammdaten | None) -> list[LieferantenGruppe]:
    """Teilt die aktiven Positionen nach Weingut auf (Geschäftsregel 2: Lieferant = Weingut).

    Die Gruppe des Hauptlieferanten (``paket.lieferant.lieferant``) steht zuerst (``haupt=True``) und
    enthält auch Positionen ohne ermittelbaren Lieferanten; weitere Gruppen folgen nach
    Lieferantennummer. Ohne Hauptlieferant oder Stammdaten wird nicht aufgeteilt (leere Liste).
    Eine leere Hauptgruppe wird nicht geliefert (die Prüfung meldet das als Fehler).
    """
    haupt = paket.lieferant.lieferant
    if haupt is None or stammdaten is None:
        return []
    haupt_gruppe = LieferantenGruppe(lieferant=haupt, haupt=True)
    weitere: dict[str, LieferantenGruppe] = {}
    for position in paket.aktive_positionen():
        lieferant = lieferant_der_position(position, stammdaten)
        if lieferant is None or lieferant.lieferantennummer == haupt.lieferantennummer:
            haupt_gruppe.positionen.append(position)
            continue
        weitere.setdefault(lieferant.lieferantennummer, LieferantenGruppe(lieferant=lieferant)).positionen.append(
            position
        )
    gruppen = [haupt_gruppe] if haupt_gruppe.positionen else []
    gruppen.extend(weitere[nummer] for nummer in sorted(weitere, key=lambda n: (len(n), n)))
    if weitere:
        logger.debug(
            "Positionen gehören zu %d Weingütern: %s",
            len(weitere) + (1 if haupt_gruppe.positionen else 0),
            "; ".join(f"{g.beschreibung} (Pos. {', '.join(map(str, g.laufnummern))})" for g in gruppen),
        )
    return gruppen


def _pruefe_weitere_lieferanten(paket: Belegpaket, stammdaten: Stammdaten | None) -> list[Pruefhinweis]:
    if not paket.einkauf_erforderlich or paket.lieferant.lieferant is None:
        return []
    gruppen = gruppiere_nach_lieferant(paket, stammdaten)
    weitere = [g for g in gruppen if not g.haupt]
    if not weitere:
        return []
    haupt = paket.lieferant.lieferant
    beschreibung = "; ".join(f"{g.beschreibung} (Pos. {', '.join(map(str, g.laufnummern))})" for g in weitere)
    details = {
        "hauptlieferant": haupt.lieferantennummer,
        "weitere": ",".join(g.lieferant.lieferantennummer for g in weitere),
        "positionen": ";".join(
            f"{g.lieferant.lieferantennummer}:{','.join(map(str, g.laufnummern))}" for g in gruppen
        ),
    }
    if not any(g.haupt for g in gruppen):
        return [
            _hinweis(
                Pruefcode.MEHRERE_LIEFERANTEN,
                Schweregrad.FEHLER,
                f"Keine Position gehört zum zugeordneten Lieferanten {haupt.lieferantennummer} {haupt.name}; "
                f"die Artikel gehören zu: {beschreibung}. Bitte den Lieferanten entsprechend wählen",
                **details,
            )
        ]
    haupt_gruppe = next(g for g in gruppen if g.haupt)
    logger.warning(
        "%s: Positionen gehören zu %d Weingütern (Hauptlieferant %s; weitere: %s) – je Weingut eine Einkauf-Bestellung",
        paket.roh.quelldatei.name,
        len(gruppen),
        haupt_gruppe.beschreibung,
        beschreibung,
    )
    return [
        _hinweis(
            Pruefcode.MEHRERE_LIEFERANTEN,
            Schweregrad.WARNUNG,
            f"Positionen gehören zu mehreren Weingütern – je Weingut wird eine eigene Einkauf-Bestellung "
            f"angelegt: {haupt_gruppe.beschreibung} (Pos. {', '.join(map(str, haupt_gruppe.laufnummern))}, "
            f"Hauptlieferant); {beschreibung}",
            **details,
        )
    ]


# ---------------------------------------------------------------------------
# Firma, Kunde, Lieferant
# ---------------------------------------------------------------------------


def _pruefe_firma(paket: Belegpaket, settings: Settings) -> list[Pruefhinweis]:
    hinweise: list[Pruefhinweis] = []
    if not paket.firma_id:
        kandidaten = next(
            (h.details.get("kandidaten", "") for h in paket.hinweise if h.code == Pruefcode.FIRMA_UNKLAR),
            "",
        )
        text = "Firma (Lexware-Mandant) nicht ermittelbar; ohne eindeutige Firma keine Buchung"
        if kandidaten:
            text += f" (Kandidaten: {kandidaten})"
        hinweise.append(_hinweis(Pruefcode.FIRMA_UNKLAR, Schweregrad.FEHLER, text, kandidaten=kandidaten))
        return hinweise
    if paket.firma_id not in settings.firmen:
        hinweise.append(
            _hinweis(
                Pruefcode.FIRMA_UNKLAR,
                Schweregrad.FEHLER,
                f"Firma '{paket.firma_id}' ist nicht konfiguriert (bekannt: {', '.join(settings.firmen) or '-'})",
                firma_id=paket.firma_id,
            )
        )
        return hinweise

    fremde: list[str] = []
    if paket.kunde.kunde is not None and paket.kunde.kunde.firma_id != paket.firma_id:
        fremde.append(f"Kunde {paket.kunde.kunde.kundennummer} ({paket.kunde.kunde.firma_id})")
    if paket.lieferant.lieferant is not None and paket.lieferant.lieferant.firma_id != paket.firma_id:
        fremde.append(
            f"Lieferant {paket.lieferant.lieferant.lieferantennummer} ({paket.lieferant.lieferant.firma_id})"
        )
    for position in paket.aktive_positionen():
        if position.artikel is not None and position.artikel.firma_id != paket.firma_id:
            fremde.append(
                f"Artikel {position.artikel.artikelnummer} in Position {position.roh.laufnr} ({position.artikel.firma_id})"
            )
    if fremde:
        hinweise.append(
            _hinweis(
                Pruefcode.FIRMA_UNKLAR,
                Schweregrad.FEHLER,
                f"Zuordnungen gehören nicht zur Firma {paket.firma_id}: {'; '.join(fremde)}",
                firma_id=paket.firma_id,
            )
        )
    return hinweise


def _pruefe_kunde(paket: Belegpaket) -> list[Pruefhinweis]:
    zuordnung = paket.kunde
    if zuordnung.kunde is not None:
        return []
    hinweise: list[Pruefhinweis] = []
    kandidaten = ", ".join(
        f"{k.kunde.kundennummer} {k.kunde.name} ({k.score:.0f})"
        for k in zuordnung.kandidaten[:_MAX_KANDIDATEN_TEXT]
    )
    kunde_text = paket.roh.kunde_text or "-"
    if zuordnung.status == MatchStatus.UNSICHER and kandidaten:
        hinweise.append(
            _hinweis(
                Pruefcode.KUNDE_UNSICHER,
                Schweregrad.WARNUNG,
                f"Kunde '{kunde_text}' nicht eindeutig; Kandidaten: {kandidaten}",
                kunde_text=kunde_text,
                kandidaten=kandidaten,
            )
        )
        text = f"Kein Kunde zugeordnet: '{kunde_text}' ist nicht eindeutig, bitte Kandidaten wählen"
    else:
        text = f"Kein Kunde zugeordnet: '{kunde_text}' im Kundenstamm nicht gefunden"
    hinweise.append(_hinweis(Pruefcode.KEIN_KUNDE, Schweregrad.FEHLER, text, kunde_text=kunde_text))
    return hinweise


def _pruefe_lieferant(paket: Belegpaket) -> list[Pruefhinweis]:
    if not paket.einkauf_erforderlich:
        logger.debug("Kein Einkauf erforderlich; Lieferantenprüfung entfällt")
        return []
    zuordnung = paket.lieferant
    if zuordnung.lieferant is not None:
        return []
    hinweise: list[Pruefhinweis] = []
    kandidaten = ", ".join(
        f"{k.lieferant.lieferantennummer} {k.lieferant.name} ({k.score:.0f})"
        for k in zuordnung.kandidaten[:_MAX_KANDIDATEN_TEXT]
    )
    lieferant_text = paket.roh.lieferant_text or "-"
    if zuordnung.status == MatchStatus.UNSICHER and kandidaten:
        gruende = "; ".join(dict.fromkeys(k.grund for k in zuordnung.kandidaten[:_MAX_KANDIDATEN_TEXT]))
        hinweise.append(
            _hinweis(
                Pruefcode.LIEFERANT_UNSICHER,
                Schweregrad.WARNUNG,
                f"Lieferant (Weingut) nicht eindeutig; Kandidaten: {kandidaten}. {gruende}",
                lieferant_text=lieferant_text,
                kandidaten=kandidaten,
            )
        )
        text = "Kein Lieferant zugeordnet (Einkauf-Bestellung erforderlich): nicht eindeutig, bitte Kandidaten wählen"
    else:
        text = (
            f"Kein Lieferant zugeordnet (Einkauf-Bestellung erforderlich): '{lieferant_text}' nicht gefunden"
        )
    hinweise.append(
        _hinweis(Pruefcode.KEIN_LIEFERANT, Schweregrad.FEHLER, text, lieferant_text=lieferant_text)
    )
    return hinweise


# ---------------------------------------------------------------------------
# Positionen
# ---------------------------------------------------------------------------


def _pruefe_position(
    position: PositionsZuordnung, paket: Belegpaket, toleranz: Decimal
) -> list[Pruefhinweis]:
    hinweise: list[Pruefhinweis] = []
    laufnr = position.roh.laufnr
    bezeichnung = position.roh.bezeichnung
    artikel = position.artikel
    artikel_text = f"Artikel {artikel.artikelnummer}" if artikel is not None else "kein Artikel zugeordnet"

    if position.menge <= 0:
        hinweise.append(
            _hinweis(
                Pruefcode.MENGE_FEHLT,
                Schweregrad.FEHLER,
                f"Position {laufnr} '{bezeichnung}': Menge fehlt oder ist 0",
                laufnr=laufnr,
                menge=_menge(position.menge),
            )
        )

    if artikel is None:
        # Ohne Artikel gibt es keine Preise. KEIN_TREFFER blockiert schon über KEIN_ARTIKEL (Matching);
        # UNSICHER trägt nur eine WARNUNG und braucht hier einen Blocker, bis der Anwender wählt.
        if not any(h.schweregrad == Schweregrad.FEHLER for h in position.hinweise):
            hinweise.append(
                _hinweis(
                    Pruefcode.VK_FEHLT,
                    Schweregrad.FEHLER,
                    f"Position {laufnr} '{bezeichnung}': kein Artikel zugeordnet, EK/VK fehlen; "
                    "bitte einen Kandidaten wählen",
                    laufnr=laufnr,
                    status=position.status.value,
                )
            )
    else:
        if paket.einkauf_erforderlich and position.ek_preis is None:
            hinweise.append(
                _hinweis(
                    Pruefcode.EK_FEHLT,
                    Schweregrad.FEHLER,
                    f"Position {laufnr} '{bezeichnung}': EK-Preis fehlt im Artikelstamm ({artikel_text})",
                    laufnr=laufnr,
                    artikelnummer=artikel.artikelnummer,
                )
            )
        if position.vk_preis is None:
            hinweise.append(
                _hinweis(
                    Pruefcode.VK_FEHLT,
                    Schweregrad.FEHLER,
                    f"Position {laufnr} '{bezeichnung}': VK-Preis (Preisgruppe 1) fehlt im Artikelstamm ({artikel_text})",
                    laufnr=laufnr,
                    artikelnummer=artikel.artikelnummer,
                )
            )

    abweichung = position.preis_abweichung  # vk_preis - preis_dokument, None wenn eines fehlt
    if abweichung is not None and abs(abweichung) > toleranz:
        vk = position.vk_preis or Decimal(0)
        hinweise.append(
            _hinweis(
                Pruefcode.PREIS_ABWEICHUNG,
                Schweregrad.WARNUNG,
                f"Position {laufnr} '{bezeichnung}': Preis laut Dokument {_geld(position.preis_dokument)} € weicht vom "
                f"VK laut Lexware {_geld(position.vk_preis)} € ab ({'+' if abweichung < 0 else '-'}{_geld(abs(abweichung))} €, "
                f"{_prozent(abweichung, vk)} %); gebucht wird der Lexware-Preis",
                laufnr=laufnr,
                vk_preis=_geld(position.vk_preis),
                preis_dokument=_geld(position.preis_dokument),
                abweichung_eur=_geld(abweichung),
                abweichung_prozent=_prozent(abweichung, vk),
            )
        )
    return hinweise


def _pruefe_positionen(paket: Belegpaket, settings: Settings) -> None:
    toleranz = settings.matching.preis_toleranz_eur
    if not paket.aktive_positionen():
        paket.hinweise.append(
            _hinweis(
                Pruefcode.MENGE_FEHLT,
                Schweregrad.FEHLER,
                "Keine buchbare Position: alle Positionen fehlen oder wurden übersprungen",
            )
        )
    for position in paket.positionen:
        if position.uebersprungen:
            continue
        position.hinweise.extend(_pruefe_position(position, paket, toleranz))


# ---------------------------------------------------------------------------
# Summenvermerke
# ---------------------------------------------------------------------------


def _einheit_norm(einheit: str | None) -> str:
    return (einheit or "").strip().strip(".").casefold()


def _pruefe_summenvermerk(vermerk: Summenvermerk, paket: Belegpaket) -> Pruefhinweis | None:
    einheit = _einheit_norm(vermerk.einheit)
    gesamt = paket.gesamtmenge
    details = {
        "vermerk": vermerk.rohtext,
        "wert": _menge(vermerk.wert),
        "einheit": vermerk.einheit or "",
        "gesamtmenge": _menge(gesamt),
    }
    if einheit in EINHEITEN_GEBINDE:
        return _hinweis(
            Pruefcode.SUMME_ABWEICHUNG,
            Schweregrad.INFO,
            f"Summenvermerk '{vermerk.rohtext}' nennt Gebinde ({vermerk.einheit}) und ist mit der "
            f"Flaschenmenge ({_menge(gesamt)}) nicht vergleichbar",
            **details,
        )
    angenommen = einheit not in EINHEITEN_FLASCHEN
    if angenommen:
        logger.info(
            "Summenvermerk %r: Einheit %r unbekannt, als Flaschen angenommen",
            vermerk.rohtext,
            vermerk.einheit,
        )
    if vermerk.wert == gesamt:
        logger.info("Summenvermerk %r stimmt mit der Gesamtmenge %s überein", vermerk.rohtext, _menge(gesamt))
        return None
    zusatz = f" (Einheit '{vermerk.einheit}' als Flaschen angenommen)" if angenommen else ""
    dokumentmenge = paket.roh.gesamtmenge
    if vermerk.wert == dokumentmenge and dokumentmenge != gesamt:
        return _hinweis(
            Pruefcode.SUMME_ABWEICHUNG,
            Schweregrad.INFO,
            f"Summenvermerk '{vermerk.rohtext}' entspricht der Dokumentmenge {_menge(dokumentmenge)} "
            f"einschließlich übersprungener Positionen; gebucht werden {_menge(gesamt)} Flaschen{zusatz}",
            dokumentmenge=_menge(dokumentmenge),
            **details,
        )
    return _hinweis(
        Pruefcode.SUMME_ABWEICHUNG,
        Schweregrad.WARNUNG,
        f"Summenvermerk '{vermerk.rohtext}' ({_menge(vermerk.wert)} Flaschen) weicht von der Gesamtmenge "
        f"der Positionen ({_menge(gesamt)}) ab{zusatz}",
        **details,
    )


def _pruefe_summenvermerke(paket: Belegpaket) -> list[Pruefhinweis]:
    hinweise: list[Pruefhinweis] = []
    for vermerk in paket.roh.summenvermerke:
        hinweis = _pruefe_summenvermerk(vermerk, paket)
        if hinweis is not None:
            hinweise.append(hinweis)
    return hinweise


# ---------------------------------------------------------------------------
# Dubletten
# ---------------------------------------------------------------------------


def normalisiere_belegnummer(text: str | None) -> str:
    """Belegnummer vergleichbar machen: Großschreibung, ohne Leerraum und Trennzeichen (- . / _),
    ohne führende Nullen ("b - 4711" -> "B4711", "000123" -> "123")."""
    if text is None:
        return ""
    norm = "".join(z for z in str(text).upper() if z.isalnum())
    return norm.lstrip("0")


def _partnernummer_norm(nummer: str | None) -> str:
    return normalisiere_belegnummer(nummer)


def pruefe_dubletten(
    paket: Belegpaket, stammdaten: Stammdaten | None, *, eintragen: bool = False
) -> list[Pruefhinweis]:
    """Sucht in ``stammdaten.belege`` nach Belegen mit gleicher Belegnummer/Kundenbestellnummer.

    Ein vorhandener Beleg gilt als Dublette, wenn seine Belegnummer oder Kundenbestellnummer der
    Belegnummer oder Kundenbestellnummer des Dokuments entspricht (normalisiert) **und** seine
    Partnernummer leer ist oder zum zugeordneten Partner passt (Kunde; bei Einkaufs-Belegen der
    Lieferant). Ist noch kein Partner zugeordnet, zählt der Nummerntreffer allein.

    Liefert die ``DUBLETTE``-Hinweise (FEHLER); leer = keine Dublette. Mit ``eintragen=True`` werden
    sie außerdem in ``paket.hinweise`` eingetragen (alte DUBLETTE-Hinweise vorher entfernt), so
    dass die Funktion unmittelbar vor dem Buchen erneut genutzt werden kann.
    """
    hinweise: list[Pruefhinweis] = []
    nummern: dict[str, str] = {}
    for feld, wert in (
        ("Belegnummer", paket.roh.belegnummer),
        ("Kundenbestellnummer", paket.roh.bestellnummer),
    ):
        norm = normalisiere_belegnummer(wert)
        if norm:
            nummern.setdefault(norm, f"{feld} '{wert}'")

    if stammdaten is None or not stammdaten.belege:
        logger.info(
            "Dublettenprüfung: keine Belege verfügbar (Stammdaten %s)",
            "fehlen" if stammdaten is None else "ohne Belege",
        )
    elif not nummern:
        logger.info("Dublettenprüfung: Dokument nennt weder Belegnummer noch Bestellnummer")
    else:
        kunde = paket.kunde.kunde
        lieferant = paket.lieferant.lieferant
        kunden_nr = _partnernummer_norm(kunde.kundennummer) if kunde else ""
        lieferanten_nr = _partnernummer_norm(lieferant.lieferantennummer) if lieferant else ""
        for beleg in stammdaten.belege:
            treffer_feld: str | None = None
            beleg_feld = ""
            beleg_norm = normalisiere_belegnummer(beleg.belegnummer)
            bestell_norm = normalisiere_belegnummer(beleg.kundenbestellnummer)
            if beleg_norm and beleg_norm in nummern:
                treffer_feld, beleg_feld = nummern[beleg_norm], "Belegnummer"
            elif bestell_norm and bestell_norm in nummern:
                treffer_feld, beleg_feld = nummern[bestell_norm], "Kundenbestellnummer"
            if treffer_feld is None:
                continue
            # TODO-LEXWARE: Partnernummer im Belegexport = Kundennummer (Verkauf) bzw. Lieferantennummer
            # (Bestellung); prüfen, wie Lexware die Spalte exportiert (Nummer vs. Matchcode).
            partner_norm = _partnernummer_norm(beleg.partnernummer)
            erwartet = lieferanten_nr if beleg.belegart == Belegart.BESTELLUNG else kunden_nr
            if partner_norm and erwartet and partner_norm != erwartet:
                logger.debug(
                    "Beleg %s trifft %s, gehört aber zu Partner %s (nicht %s); keine Dublette",
                    beleg.belegnummer,
                    treffer_feld,
                    beleg.partnernummer,
                    erwartet,
                )
                continue
            partner_text = (
                f"Partner {beleg.partnernummer}" if partner_norm else "Partner im Beleg nicht angegeben"
            )
            if partner_norm and not erwartet:
                partner_text += " (Partner im Paket noch nicht zugeordnet)"
            text = (
                f"Dublette: {treffer_feld} des Dokuments entspricht der {beleg_feld} des vorhandenen "
                f"{beleg.belegart.value}-Belegs {beleg.belegnummer}"
                f"{f' vom {beleg.datum:%d.%m.%Y}' if beleg.datum else ''} ({partner_text})"
            )
            logger.error("%s", text)
            hinweise.append(
                _hinweis(
                    Pruefcode.DUBLETTE,
                    Schweregrad.FEHLER,
                    text,
                    belegnummer=beleg.belegnummer,
                    belegart=beleg.belegart.value,
                    kundenbestellnummer=beleg.kundenbestellnummer or "",
                    partnernummer=beleg.partnernummer or "",
                    dokument=treffer_feld,
                )
            )
    if eintragen:
        paket.hinweise = [h for h in paket.hinweise if h.code != Pruefcode.DUBLETTE]
        paket.hinweise.extend(hinweise)
    return hinweise


# ---------------------------------------------------------------------------
# Gesamtprüfung
# ---------------------------------------------------------------------------


def pruefe(paket: Belegpaket, stammdaten: Stammdaten | None, settings: Settings) -> Belegpaket:
    """Führt alle Prüfungen aus (siehe Modul-Docstring), mutiert ``paket`` und gibt es zurück.

    ``stammdaten`` darf ``None`` sein (Firma unbekannt); dann entfällt nur die Dublettenprüfung.
    """
    entferne_pruefhinweise(paket)
    belegfelder_vorbelegen(paket, settings, stammdaten)

    paket.hinweise.extend(_pruefe_firma(paket, settings))
    paket.hinweise.extend(_pruefe_kunde(paket))
    paket.hinweise.extend(_pruefe_lieferant(paket))
    paket.hinweise.extend(_pruefe_weitere_lieferanten(paket, stammdaten))
    _pruefe_positionen(paket, settings)
    paket.hinweise.extend(_pruefe_summenvermerke(paket))
    paket.hinweise.extend(pruefe_dubletten(paket, stammdaten))
    paket.hinweise.extend(_pruefe_belegfelder(paket))

    fehler = sum(1 for h in paket.alle_hinweise if h.schweregrad == Schweregrad.FEHLER)
    warnungen = sum(1 for h in paket.alle_hinweise if h.schweregrad == Schweregrad.WARNUNG)
    logger.info(
        "Prüfung %s (Firma %s): %d Fehler, %d Warnungen -> %s",
        paket.roh.quelldatei.name,
        paket.firma_id or "-",
        fehler,
        warnungen,
        "BLOCKIERT" if paket.blockiert else "freigabefähig",
    )
    return paket


# ---------------------------------------------------------------------------
# Zusammenfassung
# ---------------------------------------------------------------------------

_GRAD_REIHENFOLGE = (Schweregrad.FEHLER, Schweregrad.WARNUNG, Schweregrad.INFO)
_GRAD_TEXT = {Schweregrad.FEHLER: "FEHLER ", Schweregrad.WARNUNG: "WARNUNG", Schweregrad.INFO: "INFO   "}


def zusammenfassung_hinweise(paket: Belegpaket) -> str:
    """Mehrzeiliger Text für CLI/UI: Kopf (Firma, Kunde, Lieferant, Positionen, Summen, Status) und
    alle Hinweise nach Schweregrad (FEHLER, WARNUNG, INFO)."""
    roh = paket.roh
    kunde = paket.kunde.kunde
    lieferant = paket.lieferant.lieferant
    kunde_text = f"{kunde.kundennummer} {kunde.name}" if kunde else f"- ('{roh.kunde_text or '-'}')"
    lieferant_text = (
        f"{lieferant.lieferantennummer} {lieferant.name}"
        if lieferant
        else f"- ('{roh.lieferant_text or '-'}')"
    )
    aktive = paket.aktive_positionen()
    zugeordnet = sum(1 for p in aktive if p.artikel is not None)
    uebersprungen = len(paket.positionen) - len(aktive)
    hinweise = paket.alle_hinweise
    anzahl = {grad: sum(1 for h in hinweise if h.schweregrad == grad) for grad in _GRAD_REIHENFOLGE}
    if paket.blockiert:
        status = "BLOCKIERT"
    elif anzahl[Schweregrad.WARNUNG]:
        status = "freigabefähig, bitte Warnungen prüfen"
    else:
        status = "freigabefähig"
    liefertag = roh.liefertag.strftime("%d.%m.%Y") if roh.liefertag else "-"
    felder_text = ", ".join(f"{n}={getattr(paket.belegfelder, n) or '-'}" for n in BELEGFELD_NAMEN)

    zeilen = [
        f"Belegpaket {roh.quelldatei.name} – Firma {paket.firma_id or '-'} ({paket.firma_quelle or 'nicht ermittelt'})"
        f"{' – ohne Einkauf-Bestellung' if not paket.einkauf_erforderlich else ''}",
        f"Kunde: {kunde_text} [{paket.kunde.status.value}] | Lieferant: {lieferant_text} [{paket.lieferant.status.value}]"
        f" | Belegnr. {roh.belegnummer or '-'} | Bestellnr. {roh.bestellnummer or '-'} | Liefertag {liefertag}",
        f"Positionen: {len(paket.positionen)} ({zugeordnet} zugeordnet, {len(aktive) - zugeordnet} offen, "
        f"{uebersprungen} übersprungen) | Gesamtmenge {_menge(paket.gesamtmenge)} Fl."
        f" | Netto EK {_geld(paket.summe_netto_ek)} € | Netto VK {_geld(paket.summe_netto_vk)} €",
        f"Belegfelder: {felder_text}",
        f"Status: {status} – {anzahl[Schweregrad.FEHLER]} Fehler, {anzahl[Schweregrad.WARNUNG]} Warnungen, "
        f"{anzahl[Schweregrad.INFO]} Hinweise",
    ]
    for grad in _GRAD_REIHENFOLGE:
        for h in hinweise:
            if h.schweregrad != grad:
                continue
            pos = f" Pos. {h.position_laufnr}:" if h.position_laufnr is not None else ""
            zeilen.append(f"  [{_GRAD_TEXT[grad]}] {h.code.value}{pos} {h.text}")
    return "\n".join(zeilen)
