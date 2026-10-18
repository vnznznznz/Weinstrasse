"""Verpackungsmengen (VerpackG) für Systembetreiber (z. B. Der Grüne Punkt) und Verpackungsregister LUCID.

Der Importeur ist "Hersteller" im Sinne des VerpackG für Verpackungen, die er erstmals
in Deutschland in Verkehr bringt. Gemeldet werden die systembeteiligungspflichtigen
Verkaufs-, Um- und Versandverpackungen, die typischerweise beim privaten Endverbraucher
(oder vergleichbaren Anfallstellen wie Gastronomie) anfallen – je Materialart in kg mit
drei Nachkommastellen (LUCID-Formular, Stand August 2026):
Glas, PPK, Eisenmetalle, Aluminium, Getränkekartonverpackungen, Sonstige Verbundverpackungen,
Kunststoffe, Sonstige Materialien.

Meldungen bei LUCID müssen mit der Meldung beim Systembetreiber übereinstimmen:
  * Planmenge (Initiale Planmengenmeldung) bis 31.12. für das Folgejahr,
  * Unterjährige Mengenmeldung je Vertrag (monatlich/quartalsweise/halbjährlich),
  * Jahresabschluss-Mengenmeldung (Ist) bis 15.05. beim Grünen Punkt, in LUCID spätestens bis 01.06.,
    danach nur noch als Nachtragsmengenmeldung (additiv).
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from auftragsautomat.config import Firma
from auftragsautomat.meldungen.modelle import (
    Belegtyp,
    Bewegung,
    Material,
    MaterialMenge,
    Meldemonat,
    VerpackungsMeldung,
    VerpackungsProfil,
)
from auftragsautomat.meldungen.quellen import Meldedaten
from auftragsautomat.models import Pruefcode, Pruefhinweis, Schweregrad

KG3 = Decimal("0.001")


def _summiere(
    bewegungen: list[Bewegung],
    firma: Firma,
    meldedaten: Meldedaten,
    profile: dict[str, VerpackungsProfil],
    hinweise: list[Pruefhinweis],
    *,
    hinweise_sammeln: bool,
) -> tuple[dict[Material, Decimal], Decimal]:
    gramm: dict[Material, Decimal] = {m: Decimal(0) for m in Material}
    flaschen_summe = Decimal(0)
    gemeldet: set[str] = set()
    for b in bewegungen:
        md = meldedaten.finde(firma.id, b.artikelnummer)
        if md is None or not md.verpackung_profil:
            if hinweise_sammeln and b.artikelnummer not in gemeldet:
                gemeldet.add(b.artikelnummer)
                hinweise.append(
                    Pruefhinweis(
                        code=Pruefcode.MELDEDATEN_FEHLEN,
                        schweregrad=Schweregrad.FEHLER,
                        text=(
                            f"Artikel {b.artikelnummer} ({b.bezeichnung or '?'}): kein Verpackungsprofil hinterlegt – "
                            "Verpackungsmenge unvollständig. config/meldungen/artikel_meldedaten.csv ergänzen."
                        ),
                        details={"artikel": b.artikelnummer},
                    )
                )
            continue
        profil = profile.get(md.verpackung_profil)
        if profil is None:
            if hinweise_sammeln and b.artikelnummer not in gemeldet:
                gemeldet.add(b.artikelnummer)
                hinweise.append(
                    Pruefhinweis(
                        code=Pruefcode.MELDEDATEN_FEHLEN,
                        schweregrad=Schweregrad.FEHLER,
                        text=f"Artikel {b.artikelnummer}: Verpackungsprofil {md.verpackung_profil!r} fehlt in verpackungen.yaml.",
                        details={"artikel": b.artikelnummer},
                    )
                )
            continue
        flaschen = b.menge * md.einheit_je_position
        flaschen_summe += flaschen
        for material, g in profil.gewicht_je_material_g().items():
            gramm[material] += g * flaschen
    return gramm, flaschen_summe


def _in_kg(gramm: dict[Material, Decimal]) -> list[MaterialMenge]:
    return [
        MaterialMenge(material=m, kg=(g / Decimal(1000)).quantize(KG3, rounding=ROUND_HALF_UP))
        for m, g in gramm.items()
    ]


def erstelle_verpackung(
    bewegungen: list[Bewegung],
    firma: Firma,
    monat: Meldemonat,
    meldedaten: Meldedaten,
    profile: dict[str, VerpackungsProfil],
) -> VerpackungsMeldung:
    konfig = firma.meldungen.verpackung
    meldung = VerpackungsMeldung(
        firma_id=firma.id,
        monat=monat,
        systembetreiber=konfig.systembetreiber,
        lucid_registrierungsnummer=konfig.lucid_registrierungsnummer,
    )

    def relevant(b: Bewegung) -> bool:
        if b.belegtyp not in (Belegtyp.VERKAUF, Belegtyp.GUTSCHRIFT_VERKAUF):
            return False
        return (not konfig.nur_inland) or b.partner_land.upper() == "DE"

    im_monat = [b for b in bewegungen if relevant(b) and monat.enthaelt(b.bewegungsdatum)]
    im_jahr = [
        b
        for b in bewegungen
        if relevant(b) and b.bewegungsdatum.year == monat.jahr and b.bewegungsdatum.month <= monat.monat
    ]
    gramm_monat, fl_monat = _summiere(
        im_monat, firma, meldedaten, profile, meldung.hinweise, hinweise_sammeln=True
    )
    gramm_jahr, fl_jahr = _summiere(
        im_jahr, firma, meldedaten, profile, meldung.hinweise, hinweise_sammeln=False
    )
    meldung.mengen_monat = _in_kg(gramm_monat)
    meldung.mengen_jahr_bis_monat = _in_kg(gramm_jahr)
    meldung.flaschen_monat = fl_monat
    meldung.flaschen_jahr_bis_monat = fl_jahr
    faktor = Decimal(12) / Decimal(monat.monat)
    meldung.hochrechnung_jahr = _in_kg({m: g * faktor for m, g in gramm_jahr.items()})

    if len(im_jahr) < len([b for b in bewegungen if relevant(b) and b.bewegungsdatum.year == monat.jahr]):
        pass  # spätere Monate im Export sind für die Jahressumme bis zum Meldemonat unerheblich
    if not konfig.lucid_registrierungsnummer:
        meldung.hinweise.append(
            Pruefhinweis(
                code=Pruefcode.MELDEDATEN_FEHLEN,
                schweregrad=Schweregrad.WARNUNG,
                text=f"{firma.name}: LUCID-Registrierungsnummer fehlt in config/firmen.yaml (meldungen.verpackung).",
            )
        )
    if not konfig.systembetreiber:
        meldung.hinweise.append(
            Pruefhinweis(
                code=Pruefcode.MELDEDATEN_FEHLEN,
                schweregrad=Schweregrad.WARNUNG,
                text=f"{firma.name}: Systembetreiber (z. B. Der Grüne Punkt) und Vertragsnummer fehlen in config/firmen.yaml.",
            )
        )
    return meldung
