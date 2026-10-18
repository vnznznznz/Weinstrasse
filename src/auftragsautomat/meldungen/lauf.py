"""Orchestrierung: Belegexport lesen, drei Meldungen berechnen, Dateien schreiben."""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

from auftragsautomat.config import Firma, Settings
from auftragsautomat.meldungen import ausgabe
from auftragsautomat.meldungen.fristen import fristen_fuer
from auftragsautomat.meldungen.intrastat import erstelle_intrastat
from auftragsautomat.meldungen.modelle import Bewegung, Meldemonat, Monatsmeldungen, Richtung
from auftragsautomat.meldungen.quellen import (
    Meldedaten,
    bewegungen_pfad,
    lade_bewegungen,
    lade_meldedaten,
    lade_verpackungsprofile,
)
from auftragsautomat.meldungen.schaumweinsteuer import erstelle_schaumweinsteuer
from auftragsautomat.meldungen.verpackung import erstelle_verpackung
from auftragsautomat.models import Pruefcode, Pruefhinweis, Schweregrad


def lade_bewegungen_jahr(
    firma: Firma, monat: Meldemonat, settings: Settings
) -> tuple[list[Bewegung], list[str]]:
    """Alle Monatsexporte des Jahres bis zum Meldemonat (für Jahressummen der Verpackung)."""
    bewegungen: list[Bewegung] = []
    fehlende: list[str] = []
    for mon in range(1, monat.monat + 1):
        pfad = bewegungen_pfad(firma, Meldemonat(jahr=monat.jahr, monat=mon), settings)
        if pfad.exists():
            bewegungen.extend(lade_bewegungen(pfad, firma))
        else:
            fehlende.append(str(pfad))
    # Dubletten über mehrere Exporte (gleicher Beleg, Artikel, Zeile) entfernen
    gesehen: set[tuple] = set()
    eindeutig: list[Bewegung] = []
    for b in bewegungen:
        k = (b.belegtyp, b.belegnummer, b.artikelnummer, b.menge, b.netto_eur, b.belegdatum)
        if k in gesehen:
            continue
        gesehen.add(k)
        eindeutig.append(b)
    return eindeutig, fehlende


def erstelle_monatsmeldungen(
    firma: Firma,
    monat: Meldemonat,
    settings: Settings,
    *,
    bewegungen: list[Bewegung] | None = None,
    meldedaten: Meldedaten | None = None,
) -> Monatsmeldungen:
    hinweise: list[Pruefhinweis] = []
    if bewegungen is None:
        bewegungen, fehlende = lade_bewegungen_jahr(firma, monat, settings)
        if str(bewegungen_pfad(firma, monat, settings)) in fehlende:
            hinweise.append(
                Pruefhinweis(
                    code=Pruefcode.MELDEDATEN_FEHLEN,
                    schweregrad=Schweregrad.FEHLER,
                    text=f"Belegexport für {monat} fehlt: {bewegungen_pfad(firma, monat, settings)}",
                )
            )
        vormonate = [Path(f).name for f in fehlende if f != str(bewegungen_pfad(firma, monat, settings))]
        if vormonate:
            hinweise.append(
                Pruefhinweis(
                    code=Pruefcode.MELDUNG_PRUEFEN,
                    schweregrad=Schweregrad.WARNUNG,
                    text=(
                        f"{len(vormonate)} Vormonats-Export(e) fehlen, Jahressummen der Verpackung sind unvollständig: "
                        + ", ".join(vormonate)
                    ),
                )
            )
    if meldedaten is None:
        meldedaten = lade_meldedaten(settings.meldungen.artikel_meldedaten)
    profile = lade_verpackungsprofile(settings.meldungen.verpackungen)

    return Monatsmeldungen(
        firma_id=firma.id,
        monat=monat,
        intrastat_eingang=erstelle_intrastat(
            bewegungen, firma, monat, Richtung.EINGANG, meldedaten, settings
        ),
        intrastat_versendung=erstelle_intrastat(
            bewegungen, firma, monat, Richtung.VERSENDUNG, meldedaten, settings
        ),
        schaumweinsteuer=erstelle_schaumweinsteuer(bewegungen, firma, monat, meldedaten, settings),
        verpackung=erstelle_verpackung(bewegungen, firma, monat, meldedaten, profile),
        fristen=fristen_fuer(monat, firma),
        hinweise=hinweise,
    )


def schreibe_monatsmeldungen(m: Monatsmeldungen, firma: Firma, settings: Settings) -> dict[str, Path | None]:
    ordner = ausgabe.ausgabe_ordner(settings, firma, m.monat)
    dateien: dict[str, Path | None] = {
        "idev_eingang": ausgabe.schreibe_idev_csv(m.intrastat_eingang, ordner, settings),
        "idev_versendung": ausgabe.schreibe_idev_csv(m.intrastat_versendung, ordner, settings),
        "xlsx": ausgabe.schreibe_xlsx(m, firma, ordner),
    }
    text = ausgabe.zusammenfassung(m, firma, dateien)
    zpfad = ordner / "zusammenfassung.md"
    zpfad.write_text(text, encoding="utf-8")
    dateien["zusammenfassung"] = zpfad
    (ordner / "meldungen.json").write_text(m.model_dump_json(indent=2), encoding="utf-8")
    _protokoll(m, firma, settings)
    return dateien


def _protokoll(m: Monatsmeldungen, firma: Firma, settings: Settings) -> None:
    settings.log_ordner.mkdir(parents=True, exist_ok=True)
    pfad = settings.log_ordner / "meldungen_protokoll.csv"
    neu = not pfad.exists()
    with pfad.open("a", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter=";")
        if neu:
            w.writerow(
                [
                    "zeitpunkt",
                    "firma",
                    "monat",
                    "intrastat_eingang_pos",
                    "intrastat_versendung_pos",
                    "schaumwein_liter",
                    "schaumwein_steuer_eur",
                    "verpackung_glas_kg",
                    "fehler",
                    "warnungen",
                ]
            )
        from auftragsautomat.meldungen.modelle import Material

        w.writerow(
            [
                datetime.now().isoformat(timespec="seconds"),
                firma.id,
                str(m.monat),
                len(m.intrastat_eingang.positionen),
                len(m.intrastat_versendung.positionen),
                str(m.schaumweinsteuer.liter_gesamt),
                str(m.schaumweinsteuer.steuer_gesamt_eur),
                str(m.verpackung.kg(Material.GLAS)),
                sum(1 for h in m.alle_hinweise if h.schweregrad == Schweregrad.FEHLER),
                sum(1 for h in m.alle_hinweise if h.schweregrad == Schweregrad.WARNUNG),
            ]
        )
