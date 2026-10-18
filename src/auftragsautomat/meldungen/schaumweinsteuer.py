"""Schaumwein- und Zwischenerzeugnissteuer (SchaumwZwStG) – monatliche Steueranmeldung.

Rechtsgrundlagen (gesetze-im-internet.de, Stand 2026):
  * § 1 Abs. 2: Schaumwein = Flasche mit Schaumweinstopfen und Haltevorrichtung ODER
    Überdruck >= 3 bar bei 20 °C, in den genannten KN-Positionen (2204 10, 2204 21 06-09, ...).
    Perlwein (Frizzante <= 2,5 bar, Schraub-/Kronkorken) ist KEIN Schaumwein.
  * § 2: 136 EUR/hl; unter 6 % vol 51 EUR/hl.
  * § 30 (Zwischenerzeugnisse): 153 EUR/hl; bis 15 % vol 102 EUR/hl; schäumende 136 EUR/hl.
  * § 15 / § 22b: monatliche Steueranmeldung bis zum 10. Tag des Folgemonats, Fälligkeit am
    5. Tag des zweiten auf die Entstehung folgenden Monats (zertifizierter Empfänger mit
    Dauererlaubnis, registrierter Empfänger, Steuerlager). Einzelfall: unverzüglich.
  * Formulare (zoll.de): 2401 monatliche Steueranmeldung Schaumwein (Anlage 2402),
    2404 Einzelfall Schaumwein; 2451 / 2452 / 2453 Zwischenerzeugnisse. Abgabe über das
    Zoll-Portal, ab 01.01.2027 verpflichtend elektronisch.

Steuerentstehung je Firma konfigurierbar: "eingang" (Empfang aus anderem Mitgliedstaat)
oder "entnahme" (Steuerlager: Entnahme in den freien Verkehr = Lieferung/Rechnung).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from auftragsautomat.config import Firma, Settings
from auftragsautomat.meldungen.modelle import (
    Belegtyp,
    Bewegung,
    Erzeugnisart,
    Meldemonat,
    Richtung,
    SchaumweinsteuerMeldung,
    SteuerBeleg,
    Steuerklasse,
    SteuerZeile,
)
from auftragsautomat.meldungen.quellen import Meldedaten
from auftragsautomat.models import Pruefcode, Pruefhinweis, Schweregrad

CENT = Decimal("0.01")


def ist_schaumwein(
    erzeugnisart: Erzeugnisart, ueberdruck_bar: Decimal | None, schaumweinstopfen: bool
) -> bool:
    """§ 1 Abs. 2 SchaumwZwStG: Stopfen mit Haltevorrichtung oder >= 3 bar."""
    if erzeugnisart == Erzeugnisart.SCHAUMWEIN:
        return True
    if erzeugnisart in (Erzeugnisart.STILLWEIN, Erzeugnisart.PERLWEIN):
        return schaumweinstopfen or (ueberdruck_bar is not None and ueberdruck_bar >= 3)
    return False


def steuerklasse(
    erzeugnisart: Erzeugnisart,
    alkohol_vol: Decimal | None,
    ueberdruck_bar: Decimal | None,
    schaumweinstopfen: bool,
) -> Steuerklasse | None:
    if erzeugnisart == Erzeugnisart.ZWISCHENERZEUGNIS:
        if schaumweinstopfen or (ueberdruck_bar is not None and ueberdruck_bar >= 3):
            return Steuerklasse.ZWISCHEN_SCHAEUMEND
        if alkohol_vol is not None and alkohol_vol > 15:
            return Steuerklasse.ZWISCHEN_UEBER_15
        return Steuerklasse.ZWISCHEN_BIS_15
    if ist_schaumwein(erzeugnisart, ueberdruck_bar, schaumweinstopfen):
        if alkohol_vol is not None and alkohol_vol < 6:
            return Steuerklasse.SCHAUMWEIN_UNTER_6
        return Steuerklasse.SCHAUMWEIN_AB_6
    return None


def berechne_steuer(liter: Decimal, satz_eur_je_hl: Decimal) -> Decimal:
    return (liter / Decimal(100) * satz_eur_je_hl).quantize(CENT, rounding=ROUND_HALF_UP)


def abgabefrist(monat: Meldemonat) -> date:
    """Steueranmeldung bis zum 10. Tag des Folgemonats (§ 15 Abs. 1 SchaumwZwStG)."""
    f = monat.folgemonat(1)
    return date(f.jahr, f.monat, 10)


def faelligkeit(monat: Meldemonat) -> date:
    """Fällig am 5. Tag des zweiten auf die Entstehung folgenden Monats."""
    f = monat.folgemonat(2)
    return date(f.jahr, f.monat, 5)


def erstelle_schaumweinsteuer(
    bewegungen: list[Bewegung],
    firma: Firma,
    monat: Meldemonat,
    meldedaten: Meldedaten,
    settings: Settings,
) -> SchaumweinsteuerMeldung:
    konfig = firma.meldungen.schaumwein
    saetze = settings.meldungen.steuersaetze_eur_je_hl
    meldung = SchaumweinsteuerMeldung(
        firma_id=firma.id, monat=monat, abgabe_bis=abgabefrist(monat), faellig_am=faelligkeit(monat)
    )
    if konfig.steuerentstehung == "eingang":
        relevante = [
            b
            for b in bewegungen
            if monat.enthaelt(b.bewegungsdatum) and b.richtung in (Richtung.EINGANG, Richtung.DRITTLAND)
        ]
    else:
        relevante = [
            b
            for b in bewegungen
            if monat.enthaelt(b.bewegungsdatum)
            and b.belegtyp in (Belegtyp.VERKAUF, Belegtyp.GUTSCHRIFT_VERKAUF)
        ]

    gruppen: dict[Steuerklasse, list[tuple[Bewegung, Decimal, Decimal]]] = defaultdict(list)
    for b in relevante:
        md = meldedaten.finde(firma.id, b.artikelnummer)
        if md is None:
            meldung.hinweise.append(
                Pruefhinweis(
                    code=Pruefcode.MELDEDATEN_FEHLEN,
                    schweregrad=Schweregrad.FEHLER,
                    text=(
                        f"Artikel {b.artikelnummer} ({b.bezeichnung or '?'}, Beleg {b.belegnummer}): keine Meldedaten – "
                        "Erzeugnisart/Alkohol/Überdruck unbekannt, steuerliche Einordnung nicht möglich."
                    ),
                    details={"artikel": b.artikelnummer, "beleg": b.belegnummer},
                )
            )
            continue
        klasse = steuerklasse(md.erzeugnisart, md.alkohol_vol, md.ueberdruck_bar, md.schaumweinstopfen)
        if (
            md.erzeugnisart in (Erzeugnisart.SCHAUMWEIN, Erzeugnisart.ZWISCHENERZEUGNIS)
            and md.alkohol_vol is None
        ):
            meldung.hinweise.append(
                Pruefhinweis(
                    code=Pruefcode.MELDEDATEN_FEHLEN,
                    schweregrad=Schweregrad.WARNUNG,
                    text=f"Artikel {b.artikelnummer}: Alkoholgehalt fehlt, Regelsteuersatz angenommen. Bitte ergänzen.",
                    details={"artikel": b.artikelnummer},
                )
            )
        if (
            md.erzeugnisart == Erzeugnisart.PERLWEIN
            and md.ueberdruck_bar is None
            and not md.schaumweinstopfen
        ):
            meldung.hinweise.append(
                Pruefhinweis(
                    code=Pruefcode.MELDUNG_PRUEFEN,
                    schweregrad=Schweregrad.INFO,
                    text=(
                        f"Artikel {b.artikelnummer} als Perlwein geführt (nicht steuerbar). Bitte Überdruck (<= 2,5 bar) "
                        "und Verschluss (kein Schaumweinstopfen) einmalig mit dem Produktdatenblatt belegen."
                    ),
                    details={"artikel": b.artikelnummer},
                )
            )
        if klasse is None:
            continue
        if md.volumen_cl is None:
            meldung.hinweise.append(
                Pruefhinweis(
                    code=Pruefcode.MELDEDATEN_FEHLEN,
                    schweregrad=Schweregrad.FEHLER,
                    text=f"Artikel {b.artikelnummer}: Volumen fehlt, Litermenge für Beleg {b.belegnummer} nicht berechenbar.",
                    details={"artikel": b.artikelnummer, "beleg": b.belegnummer},
                )
            )
            continue
        flaschen = b.menge * md.einheit_je_position
        liter = flaschen * md.volumen_cl / Decimal(100)
        gruppen[klasse].append((b, flaschen, liter))
        meldung.belege.append(
            SteuerBeleg(
                belegnummer=b.belegnummer,
                datum=b.bewegungsdatum,
                partnername=b.partnername,
                partner_land=b.partner_land,
                artikelnummer=b.artikelnummer,
                bezeichnung=b.bezeichnung,
                klasse=klasse,
                flaschen=flaschen,
                liter=liter,
                alkohol_vol=md.alkohol_vol,
                steuer_eur=berechne_steuer(liter, saetze[klasse.value]),
            )
        )
        if b.ist_gutschrift:
            meldung.hinweise.append(
                Pruefhinweis(
                    code=Pruefcode.MELDUNG_GUTSCHRIFT,
                    schweregrad=Schweregrad.WARNUNG,
                    text=(
                        f"Gutschrift/Rücksendung {b.belegnummer} ({abs(flaschen)} Fl.) mindert die Steuermenge. "
                        "Nur zulässig, wenn die Ware tatsächlich nicht in den freien Verkehr gelangt ist bzw. eine "
                        "Entlastung (Formular 2401, Entlastungsteil) beantragt wird – mit dem Hauptzollamt klären."
                    ),
                    details={"beleg": b.belegnummer},
                )
            )

    for klasse in Steuerklasse:
        eintraege = gruppen.get(klasse)
        if not eintraege:
            continue
        flaschen = sum((e[1] for e in eintraege), Decimal(0))
        liter = sum((e[2] for e in eintraege), Decimal(0))
        satz = saetze[klasse.value]
        meldung.zeilen.append(
            SteuerZeile(
                klasse=klasse,
                steuersatz_eur_je_hl=satz,
                flaschen=flaschen,
                liter=liter,
                steuer_eur=berechne_steuer(liter, satz),
                belegnummern=sorted({e[0].belegnummer for e in eintraege}),
            )
        )

    if meldung.zeilen and konfig.status == "keiner":
        meldung.hinweise.append(
            Pruefhinweis(
                code=Pruefcode.MELDUNG_PRUEFEN,
                schweregrad=Schweregrad.FEHLER,
                text=(
                    f"{firma.name} hat steuerpflichtigen Schaumwein bezogen, aber keinen verbrauchsteuerlichen Status "
                    "(zertifizierter Empfänger / registrierter Empfänger / Steuerlager) in config/firmen.yaml. Ohne Erlaubnis "
                    "des Hauptzollamts ist der gewerbliche Bezug nicht zulässig (§ 20a SchaumwZwStG)."
                ),
            )
        )
    if meldung.zeilen and not konfig.verbrauchsteuernummer:
        meldung.hinweise.append(
            Pruefhinweis(
                code=Pruefcode.MELDEDATEN_FEHLEN,
                schweregrad=Schweregrad.WARNUNG,
                text="Verbrauchsteuernummer/Erlaubnisnummer für die Steueranmeldung fehlt in config/firmen.yaml.",
            )
        )
    return meldung
