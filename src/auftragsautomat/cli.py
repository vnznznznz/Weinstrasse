"""Kommandozeile des Auftrags-Automaten (typer).

Derzeit: ``auftragsautomat meldungen ...`` (Intrastat, Schaumweinsteuer, Verpackung).
Weitere Befehle (extrahieren, pruefen, buchen, freigabe, watch) folgen laut docs/ARCHITEKTUR.md.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from auftragsautomat.config import Settings, lade_settings
from auftragsautomat.meldungen.lauf import (
    erstelle_monatsmeldungen,
    lade_bewegungen_jahr,
    schreibe_monatsmeldungen,
)
from auftragsautomat.meldungen.modelle import Material, Meldemonat
from auftragsautomat.meldungen.quellen import (
    QuellenFehler,
    bewegungen_pfad,
    lade_bewegungen,
    lade_meldedaten,
    lade_verpackungsprofile,
    schreibe_meldedaten_vorlage,
)
from auftragsautomat.models import Schweregrad

app = typer.Typer(help="Auftrags-Automat für Lexware premium.", no_args_is_help=True)
meldungen_app = typer.Typer(
    help="Monatliche Meldungen: Intrastat, Schaumweinsteuer, Verpackung.", no_args_is_help=True
)
app.add_typer(meldungen_app, name="meldungen")
console = Console()


def _settings(stamm: Path | None) -> Settings:
    return lade_settings(stamm)


def _monat(text: str | None) -> Meldemonat:
    if text:
        return Meldemonat.parse(text)
    heute = date.today()
    return Meldemonat(jahr=heute.year, monat=heute.month).folgemonat(-1)  # Standard: Vormonat


def _firmen(settings: Settings, firma: str | None) -> list[str]:
    if firma:
        settings.firma(firma)
        return [firma]
    return [f.id for f in settings.firmen.values() if f.meldungen.aktiv]


@meldungen_app.command("erstellen")
def erstellen(
    monat: str | None = typer.Option(None, "--monat", "-m", help="Bezugsmonat JJJJ-MM (Standard: Vormonat)."),
    firma: str | None = typer.Option(
        None, "--firma", "-f", help="Firmen-ID; ohne Angabe alle aktiven Firmen."
    ),
    stamm: Path | None = typer.Option(
        None, "--stamm", help="Projektordner (Standard: Repo/AUFTRAGSAUTOMAT_HOME)."
    ),
) -> None:
    """Berechnet alle drei Meldungen und schreibt CSV/XLSX/Zusammenfassung nach meldungen/<firma>/<monat>/."""
    settings = _settings(stamm)
    mm = _monat(monat)
    fehler_gesamt = 0
    for fid in _firmen(settings, firma):
        f = settings.firma(fid)
        try:
            m = erstelle_monatsmeldungen(f, mm, settings)
        except QuellenFehler as exc:
            console.print(f"[red]{f.name}: {exc}[/red]")
            fehler_gesamt += 1
            continue
        dateien = schreibe_monatsmeldungen(m, f, settings)
        fehler = [h for h in m.alle_hinweise if h.schweregrad == Schweregrad.FEHLER]
        warnungen = [h for h in m.alle_hinweise if h.schweregrad == Schweregrad.WARNUNG]
        fehler_gesamt += len(fehler)
        t = Table(title=f"{f.name} – {mm}")
        t.add_column("Meldung")
        t.add_column("Ergebnis")
        ie, iv, st, vp = m.intrastat_eingang, m.intrastat_versendung, m.schaumweinsteuer, m.verpackung
        t.add_row(
            "Intrastat Eingang",
            "Fehlanzeige" if ie.fehlanzeige else f"{len(ie.positionen)} Pos., {ie.summe_rechnungsbetrag} EUR",
        )
        t.add_row(
            "Intrastat Versendung",
            "Fehlanzeige" if iv.fehlanzeige else f"{len(iv.positionen)} Pos., {iv.summe_rechnungsbetrag} EUR",
        )
        t.add_row(
            "Schaumweinsteuer",
            f"{st.liter_gesamt} l, {st.steuer_gesamt_eur} EUR (Abgabe bis {st.abgabe_bis})",
        )
        t.add_row(
            "Verpackung Glas",
            f"{vp.kg(Material.GLAS)} kg (Monat), {vp.kg(Material.GLAS, jahr=True)} kg (Jahr)",
        )
        t.add_row("Hinweise", f"[red]{len(fehler)} Fehler[/red], [yellow]{len(warnungen)} Warnungen[/yellow]")
        console.print(t)
        for h in fehler:
            console.print(f"  [red]FEHLER[/red] {h.text}")
        console.print(f"  Dateien: {dateien['zusammenfassung']}")
    raise typer.Exit(code=1 if fehler_gesamt else 0)


@meldungen_app.command("pruefen")
def pruefen(
    monat: str | None = typer.Option(None, "--monat", "-m"),
    firma: str | None = typer.Option(None, "--firma", "-f"),
    stamm: Path | None = typer.Option(None, "--stamm"),
    vorlage: bool = typer.Option(
        False, "--vorlage", help="Fehlende Artikel als Leerzeilen in artikel_meldedaten.csv anlegen."
    ),
) -> None:
    """Prüft Belegexport und Meldedaten auf Vollständigkeit, ohne Dateien zu schreiben."""
    settings = _settings(stamm)
    mm = _monat(monat)
    meldedaten = lade_meldedaten(settings.meldungen.artikel_meldedaten)
    profile = lade_verpackungsprofile(settings.meldungen.verpackungen)
    console.print(
        f"Meldedaten: {len(meldedaten)} Artikel, Verpackungsprofile: {', '.join(profile) or 'keine'}"
    )
    probleme = 0
    for fid in _firmen(settings, firma):
        f = settings.firma(fid)
        pfad = bewegungen_pfad(f, mm, settings)
        if not pfad.exists():
            console.print(f"[red]{f.name}: Belegexport fehlt: {pfad}[/red]")
            probleme += 1
            continue
        try:
            bewegungen = lade_bewegungen(pfad, f)
        except QuellenFehler as exc:
            console.print(f"[red]{f.name}: {exc}[/red]")
            probleme += 1
            continue
        artikel = {(b.firma_id, b.artikelnummer, b.bezeichnung) for b in bewegungen}
        fehlend = sorted(a for a in artikel if meldedaten.finde(a[0], a[1]) is None)
        unvollstaendig = []
        for a in artikel:
            md = meldedaten.finde(a[0], a[1])
            if md is None:
                continue
            luecken = [
                n
                for n, w in (
                    ("warennummer", md.warennummer),
                    ("ursprungsland", md.ursprungsland),
                    ("volumen_cl", md.volumen_cl),
                    ("verpackung_profil", md.verpackung_profil),
                )
                if not w
            ]
            if md.verpackung_profil and md.verpackung_profil not in profile:
                luecken.append(f"profil '{md.verpackung_profil}' unbekannt")
            if luecken:
                unvollstaendig.append((a[1], a[2], luecken))
        console.print(
            f"[bold]{f.name}[/bold]: {len(bewegungen)} Positionen in {pfad.name}, {len(artikel)} Artikel; "
            f"ohne Meldedaten: {len(fehlend)}, unvollständig: {len(unvollstaendig)}"
        )
        for a in fehlend:
            console.print(f"  [red]fehlt[/red] {a[1]} {a[2] or ''}")
        for nr, bez, luecken in sorted(unvollstaendig):
            console.print(f"  [yellow]unvollständig[/yellow] {nr} {bez or ''}: {', '.join(luecken)}")
        probleme += len(fehlend) + len(unvollstaendig)
        if vorlage and fehlend:
            n = schreibe_meldedaten_vorlage(settings.meldungen.artikel_meldedaten, fehlend)
            console.print(
                f"  {n} Leerzeilen in {settings.meldungen.artikel_meldedaten} angelegt – bitte ausfüllen."
            )
    raise typer.Exit(code=1 if probleme else 0)


@meldungen_app.command("fristen")
def fristen(
    monat: str | None = typer.Option(None, "--monat", "-m"),
    firma: str | None = typer.Option(None, "--firma", "-f"),
    stamm: Path | None = typer.Option(None, "--stamm"),
) -> None:
    """Zeigt die Abgabefristen des Bezugsmonats."""
    from auftragsautomat.meldungen.fristen import fristen_fuer

    settings = _settings(stamm)
    mm = _monat(monat)
    for fid in _firmen(settings, firma):
        f = settings.firma(fid)
        t = Table(title=f"Fristen {f.name} – Bezugsmonat {mm}")
        t.add_column("Datum")
        t.add_column("Meldung")
        t.add_column("Empfänger")
        for fr in fristen_fuer(mm, f):
            t.add_row(fr.datum.isoformat(), fr.bezeichnung, fr.empfaenger)
        console.print(t)


@meldungen_app.command("jahr")
def jahr(
    jahr: int = typer.Option(..., "--jahr", "-j"),
    firma: str | None = typer.Option(None, "--firma", "-f"),
    stamm: Path | None = typer.Option(None, "--stamm"),
) -> None:
    """Jahressummen (Verpackung Ist-Mengen, Intrastat-Schwellen) aus allen Monatsexporten eines Jahres."""
    settings = _settings(stamm)
    for fid in _firmen(settings, firma):
        f = settings.firma(fid)
        mm = Meldemonat(jahr=jahr, monat=12)
        bewegungen, fehlende = lade_bewegungen_jahr(f, mm, settings)
        m = erstelle_monatsmeldungen(f, mm, settings, bewegungen=bewegungen)
        from auftragsautomat.meldungen.modelle import Richtung

        eingang = sum(
            b.netto_eur
            for b in bewegungen
            if b.richtung == Richtung.EINGANG and b.bewegungsdatum.year == jahr
        )
        versendung = sum(
            b.netto_eur
            for b in bewegungen
            if b.richtung == Richtung.VERSENDUNG and b.bewegungsdatum.year == jahr
        )
        t = Table(title=f"{f.name} – Jahr {jahr} ({len(fehlende)} Monatsexporte fehlen)")
        t.add_column("Kennzahl")
        t.add_column("Wert")
        t.add_row("Innergemeinschaftliche Eingänge EUR (Schwelle 3 Mio.)", f"{eingang:,.2f}")
        t.add_row("Innergemeinschaftliche Versendungen EUR (Schwelle 1 Mio.)", f"{versendung:,.2f}")
        for mat in Material:
            t.add_row(f"Verpackung {mat.value} kg (Ist, Inland)", f"{m.verpackung.kg(mat, jahr=True)}")
        console.print(t)


if __name__ == "__main__":
    app()
