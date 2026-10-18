"""Intrastat-Meldung (Intrahandelsstatistik, Destatis) aus den Belegpositionen eines Monats.

Quelle der Regeln: Leitfaden zur Intrahandelsstatistik 2026 (Statistisches Bundesamt).
  * Bezugszeitraum = Monat der physischen Warenbewegung (Lieferdatum, sonst Belegdatum).
  * Abgabe bis zum 10. Arbeitstag nach dem Bezugsmonat, nur über IDEV / eSTATISTIK.core.
  * Anmeldeschwellen: Eingang 3 Mio. EUR, Versendung 1 Mio. EUR (Vorjahr oder laufendes Jahr).
  * Ohne Warenverkehr ist eine Fehlanzeige abzugeben.
  * Zusammenfassung je Warennummer, Mitgliedstaat, Bundesland, Art des Geschäfts,
    Verkehrszweig, Ursprungsland (Versendung zusätzlich USt-IdNr. des Partners).
  * Eigenmasse in vollen kg (Masse ohne Umschließung), Besondere Maßeinheit bei Kapitel 2204
    in vollen Litern, Rechnungsbetrag in vollen Euro (unter 0,50 EUR -> 1), Statistischer Wert
    (frei deutsche Grenze) bei verbrauchsteuerpflichtigen Waren immer, ohne Verbrauchsteuer.
  * Negative Werte sind unzulässig: Gutschriften/Rücksendungen werden als Hinweis ausgewiesen.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal

from auftragsautomat.config import Firma, Settings
from auftragsautomat.meldungen.modelle import (
    EU_MITGLIEDSTAATEN,
    Bewegung,
    IntrastatMeldung,
    IntrastatPosition,
    Meldemonat,
    Richtung,
)
from auftragsautomat.meldungen.quellen import Meldedaten
from auftragsautomat.models import Pruefcode, Pruefhinweis, Schweregrad

KAPITEL_MIT_LITER = ("2204", "2205", "2206", "2208")  # Besondere Maßeinheit "l"


def _runde_euro(betrag: Decimal) -> int:
    """Volle Euro, kaufmännisch; Beträge unter 0,50 EUR werden auf 1 EUR aufgerundet."""
    if betrag <= 0:
        return 0
    if betrag < Decimal("0.50"):
        return 1
    return int(betrag.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _runde_ganz(wert: Decimal) -> int:
    return int(wert.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def erstelle_intrastat(
    bewegungen: list[Bewegung],
    firma: Firma,
    monat: Meldemonat,
    richtung: Richtung,
    meldedaten: Meldedaten,
    settings: Settings,
) -> IntrastatMeldung:
    konfig = firma.meldungen.intrastat
    mk = settings.meldungen
    meldung = IntrastatMeldung(firma_id=firma.id, monat=monat, richtung=richtung)
    meldung.meldepflichtig = (
        konfig.meldepflichtig_eingang if richtung == Richtung.EINGANG else konfig.meldepflichtig_versendung
    )
    if meldung.meldepflichtig is None:
        schwelle = (
            mk.intrastat_schwelle_eingang_eur
            if richtung == Richtung.EINGANG
            else mk.intrastat_schwelle_versendung_eur
        )
        meldung.hinweise.append(
            Pruefhinweis(
                code=Pruefcode.MELDUNG_SCHWELLE,
                schweregrad=Schweregrad.WARNUNG,
                text=(
                    f"Meldepflicht {richtung.value} für {firma.name} nicht hinterlegt. Schwelle: "
                    f"{schwelle:,.0f} EUR im Vorjahr oder laufenden Jahr. Bitte prüfen und in config/firmen.yaml "
                    f"unter meldungen.intrastat.meldepflichtig_{richtung.value} eintragen."
                ).replace(",", "."),
            )
        )

    relevante = [b for b in bewegungen if monat.enthaelt(b.bewegungsdatum) and b.richtung == richtung]
    gruppen: dict[tuple, list[tuple[Bewegung, Decimal, Decimal, Decimal]]] = defaultdict(list)

    for b in relevante:
        md = meldedaten.finde(firma.id, b.artikelnummer)
        if b.ist_gutschrift:
            meldung.hinweise.append(
                Pruefhinweis(
                    code=Pruefcode.MELDUNG_GUTSCHRIFT,
                    schweregrad=Schweregrad.WARNUNG,
                    text=(
                        f"Beleg {b.belegnummer} ({b.artikelnummer}) ist eine Gutschrift/Rücksendung über "
                        f"{abs(b.menge)} Fl. / {abs(b.netto_eur):.2f} EUR. Nicht in der Meldung enthalten: "
                        "Rücksendungen sind in der Gegenrichtung mit Art des Geschäfts 21 anzumelden, reine "
                        "Preisgutschriften gar nicht (Berichtigung nur bei > 5.000 EUR je Position)."
                    ),
                    details={"beleg": b.belegnummer, "artikel": b.artikelnummer},
                )
            )
            continue
        fehlend = []
        if md is None or not md.warennummer:
            fehlend.append("warennummer")
        if md is None or not md.ursprungsland:
            fehlend.append("ursprungsland")
        if md is None or md.volumen_cl is None:
            fehlend.append("volumen_cl")
        if fehlend:
            meldung.hinweise.append(
                Pruefhinweis(
                    code=Pruefcode.MELDEDATEN_FEHLEN,
                    schweregrad=Schweregrad.FEHLER,
                    text=(
                        f"Artikel {b.artikelnummer} ({b.bezeichnung or '?'}): Meldedaten fehlen "
                        f"({', '.join(fehlend)}). Beleg {b.belegnummer} kann nicht angemeldet werden. "
                        "Bitte in config/meldungen/artikel_meldedaten.csv ergänzen."
                    ),
                    details={"artikel": b.artikelnummer, "beleg": b.belegnummer},
                )
            )
            continue
        assert md is not None and md.volumen_cl is not None and md.warennummer and md.ursprungsland
        if len(md.warennummer) != 8 or not md.warennummer.isdigit():
            meldung.hinweise.append(
                Pruefhinweis(
                    code=Pruefcode.MELDEDATEN_FEHLEN,
                    schweregrad=Schweregrad.FEHLER,
                    text=f"Artikel {b.artikelnummer}: Warennummer {md.warennummer!r} ist nicht 8-stellig numerisch.",
                    details={"artikel": b.artikelnummer},
                )
            )
            continue
        flaschen = b.menge * md.einheit_je_position
        liter = flaschen * md.volumen_cl / Decimal(100)
        dichte = md.dichte_kg_je_l or mk.dichte_kg_je_l
        kg = liter * dichte
        partner = b.partner_land.upper()
        if partner not in EU_MITGLIEDSTAATEN:
            continue
        bundesland = konfig.bundesland if richtung == Richtung.EINGANG else "99"
        if richtung == Richtung.VERSENDUNG and md.ursprungsland.upper() == "DE":
            bundesland = konfig.bundesland
        schluessel = (
            md.warennummer,
            partner,
            md.ursprungsland.upper(),
            bundesland,
            konfig.art_des_geschaefts_standard,
            konfig.verkehrszweig_standard,
            (b.partner_ustid or "").replace(" ", "") if richtung == Richtung.VERSENDUNG else None,
        )
        gruppen[schluessel].append((b, flaschen, liter, kg))
        if richtung == Richtung.VERSENDUNG and not b.partner_ustid:
            meldung.hinweise.append(
                Pruefhinweis(
                    code=Pruefcode.MELDUNG_PRUEFEN,
                    schweregrad=Schweregrad.FEHLER,
                    text=f"Versendung {b.belegnummer} an {b.partnername or b.partnernummer}: USt-IdNr. des Kunden fehlt.",
                    details={"beleg": b.belegnummer},
                )
            )

    for schluessel, eintraege in sorted(gruppen.items(), key=lambda kv: tuple(str(x) for x in kv[0])):
        warennummer, partner, ursprung, bundesland, adg, vz, ustid = schluessel
        flaschen = sum((e[1] for e in eintraege), Decimal(0))
        liter = sum((e[2] for e in eintraege), Decimal(0))
        kg = sum((e[3] for e in eintraege), Decimal(0))
        netto = sum((e[0].netto_eur for e in eintraege), Decimal(0))
        fracht = sum((e[0].fracht_eur for e in eintraege), Decimal(0))
        statistischer_wert = None
        if konfig.statistischer_wert_angeben:
            zuschlag = netto * konfig.frachtzuschlag_prozent / Decimal(100) if fracht == 0 else fracht
            statistischer_wert = _runde_euro(netto + zuschlag)
        besondere = _runde_ganz(liter) if warennummer.startswith(KAPITEL_MIT_LITER) else None
        bezeichnungen = sorted({e[0].bezeichnung for e in eintraege if e[0].bezeichnung})
        meldung.positionen.append(
            IntrastatPosition(
                richtung=richtung,
                warennummer=warennummer,
                partner_mitgliedstaat=partner,
                ursprungsland=ursprung,
                bundesland=bundesland,
                art_des_geschaefts=adg,
                verkehrszweig=vz,
                partner_ustid=ustid or None,
                eigenmasse_kg=max(_runde_ganz(kg), 1) if kg > 0 else 0,
                besondere_masseinheit=besondere,
                rechnungsbetrag_eur=_runde_euro(netto),
                statistischer_wert_eur=statistischer_wert,
                flaschen=flaschen,
                liter=liter,
                belegnummern=sorted({e[0].belegnummer for e in eintraege}),
                warenbezeichnung="; ".join(bezeichnungen)[:80] or None,
            )
        )

    if meldung.fehlanzeige and meldung.meldepflichtig:
        meldung.hinweise.append(
            Pruefhinweis(
                code=Pruefcode.MELDUNG_PRUEFEN,
                schweregrad=Schweregrad.INFO,
                text=f"Keine {richtung.value}-Warenverkehre im {monat}: Fehlanzeige in IDEV abgeben (gesetzlich verpflichtend).",
            )
        )
    return meldung


def idev_csv_zeilen(meldung: IntrastatMeldung, settings: Settings) -> list[list[str]]:
    """Zeilen für die CSV-Datei zum Import ins IDEV-Formular (Spaltenreihenfolge konfigurierbar)."""
    mk = settings.meldungen
    spalten = mk.idev_spalten_eingang if meldung.richtung == Richtung.EINGANG else mk.idev_spalten_versendung
    zeilen: list[list[str]] = []
    if mk.idev_kopfzeile:
        zeilen.append(list(spalten))
    for p in meldung.positionen:
        daten = p.model_dump()
        zeile = []
        for s in spalten:
            w = daten.get(s)
            zeile.append("" if w is None else str(w))
        zeilen.append(zeile)
    return zeilen
