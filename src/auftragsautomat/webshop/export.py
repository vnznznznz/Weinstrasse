"""Schreibt die Daten des Frontends: ``produkte.json`` (Sortiment mit Preisen aus dem Artikelstamm)
und ``lieferung.json`` (Liefergebiet, Zeitfenster, Versandkosten, Laden)."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from auftragsautomat.models import Artikel
from auftragsautomat.webshop.konfig import DARSTELLUNG, SortimentEintrag, WebshopKonfig

logger = logging.getLogger(__name__)

_CENT = Decimal("0.01")


def brutto(netto: Decimal, mwst_prozent: Decimal) -> Decimal:
    """Netto -> brutto, kaufmännisch auf Cent gerundet."""
    return (netto * (Decimal(1) + mwst_prozent / Decimal(100))).quantize(_CENT, rounding=ROUND_HALF_UP)


@dataclass
class ExportErgebnis:
    produkte: list[dict] = field(default_factory=list)
    aus_stamm: int = 0
    beispielpreise: int = 0
    uebersprungen: list[str] = field(default_factory=list)


def produkt_aus_eintrag(
    eintrag: SortimentEintrag, artikel: Artikel | None, mwst_prozent: Decimal
) -> dict | None:
    """Ein Sortimentseintrag plus (optional) Stammartikel -> Produkt fürs Frontend. None = nicht
    anbietbar (kein Preis oder Artikel gesperrt)."""
    if artikel is not None and not artikel.aktiv:
        return None
    preis: Decimal | None = None
    beispielpreis = False
    if artikel is not None and artikel.vk_preis_1 is not None and artikel.vk_preis_1 > 0:
        preis = brutto(artikel.vk_preis_1, mwst_prozent)
    elif eintrag.preis_brutto is not None:
        preis = eintrag.preis_brutto.quantize(_CENT)
        beispielpreis = True
    if preis is None:
        return None
    volumen = eintrag.volumen_cl
    if volumen is None and artikel is not None and artikel.volumen_cl is not None:
        volumen = artikel.volumen_cl
    if volumen is None:
        volumen = Decimal(75)
    liter = (volumen / Decimal(100)).quantize(Decimal("0.001")).normalize()
    jahrgang = eintrag.jahrgang if eintrag.jahrgang is not None else (artikel.jahrgang if artikel else None)
    farbe, hintergrund = DARSTELLUNG[eintrag.typ]
    return {
        "id": eintrag.artikelnummer,
        "name": eintrag.name or (artikel.bezeichnung if artikel else eintrag.artikelnummer),
        "untertitel": eintrag.untertitel,
        "typ": eintrag.typ,
        "land": eintrag.land,
        "badge": eintrag.badge,
        "text": eintrag.text,
        "preis": float(preis),
        "liter": float(liter),
        "grundpreis_liter": float((preis / liter).quantize(_CENT, rounding=ROUND_HALF_UP)),
        "jahrgang": jahrgang,
        "beispielpreis": beispielpreis,
        "farbe": farbe,
        "bg": hintergrund,
    }


def baue_produkte(konfig: WebshopKonfig, artikel_nach_nummer: dict[str, Artikel]) -> ExportErgebnis:
    ergebnis = ExportErgebnis()
    for eintrag in konfig.sortiment:
        artikel = artikel_nach_nummer.get(eintrag.artikelnummer)
        produkt = produkt_aus_eintrag(eintrag, artikel, konfig.mwst_prozent)
        if produkt is None:
            logger.warning("Sortiment: %s ohne Preis oder gesperrt, übersprungen", eintrag.artikelnummer)
            ergebnis.uebersprungen.append(eintrag.artikelnummer)
            continue
        if produkt["beispielpreis"]:
            ergebnis.beispielpreise += 1
        else:
            ergebnis.aus_stamm += 1
        ergebnis.produkte.append(produkt)
    return ergebnis


def lieferung_daten(konfig: WebshopKonfig) -> dict:
    lf = konfig.lieferung
    return {
        "mindestflaschen_lastenrad": lf.mindestflaschen_lastenrad,
        "bestellschluss_uhr": lf.bestellschluss_uhr,
        "tage_voraus": lf.tage_voraus,
        "versandkosten": float(lf.versandkosten),
        "versandkostenfrei_ab": float(lf.versandkostenfrei_ab),
        "zonen": [{"id": z.id, "name": z.name, "gebuehr": float(z.gebuehr), "plz": z.plz} for z in lf.zonen],
        "zeitfenster": [{"id": f.id, "von": f.von, "bis": f.bis, "tage": f.tage} for f in lf.zeitfenster],
        "laden": {
            "name": konfig.laden.name,
            "strasse": konfig.laden.strasse,
            "plz": konfig.laden.plz,
            "ort": konfig.laden.ort,
            "email": konfig.laden.email,
            "oeffnungszeiten": {str(k): list(v) for k, v in konfig.laden.oeffnungszeiten.items()},
        },
    }


def _schreibe_json(ziel: Path, daten: object) -> None:
    ziel.parent.mkdir(parents=True, exist_ok=True)
    ziel.write_text(json.dumps(daten, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def exportiere(konfig: WebshopKonfig, artikel_nach_nummer: dict[str, Artikel]) -> ExportErgebnis:
    """Beide Dateien in ``konfig.ausgabe_ordner`` schreiben."""
    ergebnis = baue_produkte(konfig, artikel_nach_nummer)
    stand = datetime.now(UTC).replace(microsecond=0).isoformat()
    _schreibe_json(konfig.ausgabe_ordner / "produkte.json", {"stand": stand, "produkte": ergebnis.produkte})
    _schreibe_json(konfig.ausgabe_ordner / "lieferung.json", {"stand": stand, **lieferung_daten(konfig)})
    return ergebnis
