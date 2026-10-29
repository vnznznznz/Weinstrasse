"""Kommandozeile des Auftrags-Automaten (``auftragsautomat …``, typer).

Befehle:

* ``extrahieren DATEI [--json]``            – nur Extraktion, Rohbestellung anzeigen
* ``pruefen DATEI [--firma] [--json]``      – Extraktion + Matching + Prüfung, Paket speichern
* ``buchen PAKET_JSON [--scharf] [--lieferschein] [--freigeben] [--ja]`` – gespeichertes Paket buchen
  (ohne ``--scharf`` immer Dry Run; scharf nur nach Sicherheitsabfrage)
* ``lieferschein PAKET_JSON --ab AB-NR [--scharf] [--ja]`` – nur den Lieferschein zu einer vorhandenen AB
  anlegen (nach dem openTRANS-Import, wenn die AB-Nummer bekannt ist)
* ``freigabe [--port]``                     – Freigabe-Oberfläche (Streamlit) starten
* ``watch [--einmalig]``                    – Eingangsordner beobachten (extrahieren + prüfen, NIE buchen)
* ``stammdaten-pruefen [--firma]``          – Stammdaten laden und Datenqualität berichten
* ``odbc-erkunden --firma [--ziel]``        – Lexware-ODBC-Schema erkunden (nur lesend)
* ``lexware-erkunden [--ziel] [--tiefe] [--alle-fenster] [--backend] [--abgleich] [--aus-datei]`` – Control-Baum
  des laufenden Lexware nur lesend auslesen und mit config/lexware_controls.yaml abgleichen
* ``beispiel-pdfs [ORDNER]``                – Beispiel-PDFs erzeugen (scripts/beispiel_pdfs_erzeugen.py)
* ``version``

Ausbaustufe 2 (Fixtermine, Entwürfe, Dossiers, Überwachung, Tracking, Rabatte, Firmenwahl, Ablage):

* ``m365-anmelden [--device-code] [--nur-status]`` – Microsoft-365-Anmeldung (Backend graph) bzw. Status
* ``m365-pruefen``                          – nur lesen: Blätter der Fixterminüberwachung, Kontakte zählen
* ``fixtermin-eintragen PAKET_JSON [--scharf]`` – Fixtermin in die Fixterminüberwachung (sonst Vorschau-CSV)
* ``entwuerfe PAKET_JSON [--scharf]``       – E-Mail-Entwürfe (nie senden; ohne --scharf als .eml)
* ``dossier PAKET_JSON [--scharf]``         – Dossier anlegen/fortschreiben (ohne --scharf lokal)
* ``ablegen PAKET_JSON [--scharf]``         – fertige Dateien nach OneDrive "Aufträge" (ohne --scharf nur Protokoll)
* ``benennen PAKET_JSON --kuerzel K``       – Dateiname laut Titulierung anzeigen
* ``firma-merken PAKET_JSON --firma ID``    – Firma setzen und je Kunde + Lieferant merken
* ``ueberwachen [--tage N] [--scharf] [--nur-lesen]`` – täglicher Überwachungslauf, Bericht schreiben
* ``heute``                                 – letzten Überwachungsbericht anzeigen
* ``tracking REFERENZ [--kundenreferenz]``  – Sendungsstatus über die Tracking-Kette
* ``tracking-einrichten``                   – Portal-Login im eigenen Chrome-Profil einrichten
* ``kontakte-pruefen``                      – Kontaktliste laden und zusammenfassen
* ``rabatte-einlesen PDF [--uebernehmen]``  – Rabattlisten-PDF einlesen, optional ins Gedächtnis
* ``aktionen [--firma] [--datum]``          – gültige Aktionen anzeigen

Alle Meldungen sind deutsch; Fehler führen zu Exit-Code ≠ 0. ``--home`` (oder die Umgebungs-
variable ``AUFTRAGSAUTOMAT_HOME``) wählt den Projektstamm mit ``config/``. Ohne ``--scharf`` ist jeder
Befehl ein Dry Run.
"""

from __future__ import annotations

import getpass
import importlib.util
import logging
import os
import subprocess
import sys
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from types import ModuleType
from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from auftragsautomat import __version__
from auftragsautomat.config import DEFAULT_PROJEKT_STAMM, Settings, lade_settings, projekt_stamm
from auftragsautomat.models import Belegpaket, MatchStatus, Pruefhinweis, RohBestellung, Schweregrad
from auftragsautomat.protokoll import formatiere_betrag, formatiere_datum, formatiere_menge

__all__ = ["app", "main"]

logger = logging.getLogger(__name__)

app = typer.Typer(
    help="Auftrags-Automat für Lexware premium: Bestellungen einlesen, prüfen, freigeben und buchen.",
    no_args_is_help=True,
    add_completion=False,
)

_BEISPIELSKRIPT = "beispiel_pdfs_erzeugen.py"
_APP_DATEI = Path(__file__).resolve().parent / "freigabe" / "app.py"
_STATUS_SYMBOL = {
    MatchStatus.SICHER: "OK",
    MatchStatus.MANUELL: "OK (manuell)",
    MatchStatus.UNSICHER: "? unsicher",
    MatchStatus.KEIN_TREFFER: "!! kein Treffer",
    MatchStatus.UEBERSPRUNGEN: "- übersprungen",
}
_log_handler: logging.Handler | None = None


# ---------------------------------------------------------------------------
# Hilfen
# ---------------------------------------------------------------------------


def _konfiguriere_logging(stufe: str) -> None:
    """Root-Logger auf stderr; ein früher installierter Handler wird ersetzt (wiederholte Aufrufe,
    Tests mit umgeleitetem stderr)."""
    global _log_handler
    wurzel = logging.getLogger()
    if _log_handler is not None:
        wurzel.removeHandler(_log_handler)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    wurzel.addHandler(handler)
    _log_handler = handler
    try:
        wurzel.setLevel(stufe.upper())
    except ValueError:
        wurzel.setLevel(logging.INFO)
        logger.warning("Unbekannte Log-Stufe %r, verwende INFO", stufe)


def _lade_settings() -> Settings:
    """Settings aus ``config/`` des Projektstamms (Testhaken: monkeypatchbar)."""
    return lade_settings()


def _console() -> Console:
    console = Console(highlight=False)
    if not console.is_terminal:
        # In Dateien/Tests nicht auf 80 Zeichen umbrechen
        console = Console(highlight=False, width=160)
    return console


def _fehler(text: str, code: int = 1) -> typer.Exit:
    typer.secho(f"Fehler: {text}", err=True, fg=typer.colors.RED)
    return typer.Exit(code=code)


def _bekannte_fehler() -> tuple[type[BaseException], ...]:
    """Fachliche Fehler der Extraktion/Prüfung, die ohne Traceback als eine Zeile gemeldet werden
    (Datei fehlt oder unbekannt, Vision ohne API-Schlüssel/abgelehnt, Stammdaten nicht ladbar)."""
    from auftragsautomat.stammdaten.csv_quelle import StammdatenFehler

    fehler: list[type[BaseException]] = [FileNotFoundError, ValueError, RuntimeError, StammdatenFehler]
    try:
        from auftragsautomat.extraktion.vision import VisionFehler
    except ImportError:  # anthropic/Pillow nicht installiert: Vision meldet sich als ExtraktorNichtVerfuegbar
        pass
    else:
        fehler.append(VisionFehler)
    return tuple(fehler)


def _hinweis_zeile(h: Pruefhinweis) -> str:
    pos = f" Pos. {h.position_laufnr}:" if h.position_laufnr is not None else ""
    return f"[{h.schweregrad.value.upper()}] {h.code.value}{pos} {h.text}"


def _zeige_hinweise(console: Console, hinweise: Sequence[Pruefhinweis]) -> None:
    if not hinweise:
        console.print("Keine Hinweise.")
        return
    reihenfolge = {Schweregrad.FEHLER: 0, Schweregrad.WARNUNG: 1, Schweregrad.INFO: 2}
    farben = {Schweregrad.FEHLER: "red", Schweregrad.WARNUNG: "yellow", Schweregrad.INFO: "cyan"}
    console.print("Hinweise:")
    for h in sorted(hinweise, key=lambda x: (reihenfolge[x.schweregrad], x.position_laufnr or 0)):
        console.print(f"  {_hinweis_zeile(h)}", style=farben[h.schweregrad])


def _tabelle_roh(roh: RohBestellung) -> Table:
    tabelle = Table(title=f"Rohbestellung {Path(roh.quelldatei).name} (Typ {roh.eingangstyp.value}, {roh.extraktionsmethode.value})")
    for spalte, rechts in (("Pos", True), ("Art.-Nr.", False), ("Bezeichnung", False), ("Jg", True), ("Menge", True), ("Einh.", False), ("Preis", True), ("Vol. cl", True), ("Hinweis", False)):
        tabelle.add_column(spalte, justify="right" if rechts else "left")
    for p in roh.positionen:
        tabelle.add_row(
            str(p.laufnr),
            p.artikelnummer or "",
            p.bezeichnung + (" (durchgestrichen)" if p.durchgestrichen else ""),
            str(p.jahrgang) if p.jahrgang else "",
            formatiere_menge(p.menge),
            p.einheit or "",
            formatiere_betrag(p.preis),
            formatiere_menge(p.volumen_cl),
            p.hinweis or "",
        )
    return tabelle


def _zeige_roh(console: Console, roh: RohBestellung) -> None:
    console.print(f"Kunde:        {roh.kunde_text or '-'}")
    console.print(f"Lieferant:    {roh.lieferant_text or '-'}")
    console.print(f"Belegnr.:     {roh.belegnummer or '-'}    Bestellnr.: {roh.bestellnummer or '-'}")
    console.print(f"Datum:        {formatiere_datum(roh.datum)}    Liefertag: {formatiere_datum(roh.liefertag)}")
    console.print(f"Lieferadr.:   {roh.lieferadresse or '-'}")
    console.print(_tabelle_roh(roh))
    if roh.summenvermerke:
        console.print("Summenvermerke: " + "; ".join(f"{formatiere_menge(s.wert)} {s.einheit or ''} ({s.rohtext})" for s in roh.summenvermerke))
    console.print(f"Gesamtmenge laut Dokument: {formatiere_menge(roh.gesamtmenge)}")
    _zeige_hinweise(console, roh.hinweise)


def _tabelle_paket(paket: Belegpaket) -> Table:
    tabelle = Table(title=f"Belegpaket {Path(paket.roh.quelldatei).name} – Firma {paket.firma_id or '?'}")
    for spalte, rechts in (("Pos", True), ("Dokument", False), ("Artikelnr", False), ("Artikel (Lexware)", False), ("Menge", True), ("EK", True), ("VK", True), ("Preis Dok.", True), ("Abw.", True), ("Status", False)):
        tabelle.add_column(spalte, justify="right" if rechts else "left")
    for p in paket.positionen:
        abweichung = p.preis_abweichung
        abw_text = ""
        stil = ""
        if abweichung is not None and abweichung != 0 and not p.uebersprungen:
            abw_text = ("+" if abweichung > 0 else "") + formatiere_betrag(abweichung)
            stil = "red"
        kandidaten = ""
        if p.artikel is None and p.kandidaten:
            kandidaten = " (Kandidaten: " + ", ".join(k.artikel.artikelnummer for k in p.kandidaten[:3]) + ")"
        tabelle.add_row(
            str(p.roh.laufnr),
            p.roh.bezeichnung,
            p.artikel.artikelnummer if p.artikel else "-",
            (p.artikel.bezeichnung if p.artikel else "-") + kandidaten,
            formatiere_menge(p.menge),
            formatiere_betrag(p.ek_preis),
            formatiere_betrag(p.vk_preis),
            formatiere_betrag(p.preis_dokument),
            abw_text,
            _STATUS_SYMBOL.get(p.status, p.status.value),
            style=stil or None,
        )
    return tabelle


def _zeige_paket(console: Console, paket: Belegpaket) -> None:
    from auftragsautomat.matching.pruefung import zusammenfassung_hinweise

    console.print(_tabelle_paket(paket))
    console.print(zusammenfassung_hinweise(paket))


def _freigeben(paket: Belegpaket, quelle: str) -> None:
    paket.freigegeben = True
    paket.freigegeben_am = datetime.now()
    paket.freigegeben_von = _benutzername()
    logger.info("Paket freigegeben (%s) von %s", quelle, paket.freigegeben_von)


def _benutzername() -> str:
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001 - getuser kann auf exotischen Systemen scheitern
        return os.environ.get("USERNAME") or os.environ.get("USER") or "unbekannt"


def _lade_beispielskript() -> ModuleType:
    """``scripts/beispiel_pdfs_erzeugen.py`` ist kein Paket -> per importlib aus dem Projektstamm laden."""
    kandidaten = [projekt_stamm() / "scripts" / _BEISPIELSKRIPT, DEFAULT_PROJEKT_STAMM / "scripts" / _BEISPIELSKRIPT]
    for pfad in kandidaten:
        if pfad.is_file():
            spec = importlib.util.spec_from_file_location("beispiel_pdfs_erzeugen", pfad)
            if spec is None or spec.loader is None:
                continue
            modul = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(modul)
            return modul
    raise FileNotFoundError(
        f"Skript {_BEISPIELSKRIPT} nicht gefunden (gesucht: {', '.join(str(k) for k in kandidaten)})"
    )


def _streamlit_kommando(port: int, *, kein_browser: bool) -> list[str]:
    kommando = [sys.executable, "-m", "streamlit", "run", str(_APP_DATEI), "--server.port", str(port)]
    if kein_browser:
        kommando += ["--server.headless", "true"]
    kommando += ["--browser.gatherUsageStats", "false"]
    return kommando


def _starte_prozess(kommando: list[str], umgebung: dict[str, str]) -> int:
    """Startet den Streamlit-Prozess und wartet (Testhaken: monkeypatchbar)."""
    return subprocess.run(kommando, env=umgebung, check=False).returncode


# ---------------------------------------------------------------------------
# Befehle
# ---------------------------------------------------------------------------


@app.callback()
def haupt(
    home: Annotated[
        Path | None, typer.Option("--home", help="Projektstamm mit config/ (sonst AUFTRAGSAUTOMAT_HOME bzw. Repo).")
    ] = None,
    log_level: Annotated[str, typer.Option("--log-level", help="Log-Stufe (DEBUG, INFO, WARNING, ERROR).")] = "INFO",
) -> None:
    """Auftrags-Automat für Lexware premium."""
    if home is not None:
        os.environ["AUFTRAGSAUTOMAT_HOME"] = str(Path(home).resolve())
    _konfiguriere_logging(log_level)


@app.command()
def version() -> None:
    """Versionsnummer ausgeben."""
    typer.echo(f"auftragsautomat {__version__}")


@app.command()
def extrahieren(
    datei: Annotated[Path, typer.Argument(help="PDF, Bild, .eml/.msg/.txt")],
    json_ausgabe: Annotated[bool, typer.Option("--json", help="Rohbestellung als JSON ausgeben.")] = False,
) -> None:
    """Datei nur extrahieren (ohne Matching) und die Rohbestellung anzeigen."""
    from auftragsautomat.extraktion.erkennung import extrahiere

    settings = _lade_settings()
    try:
        roh = extrahiere(datei, settings)
    except _bekannte_fehler() as exc:
        raise _fehler(f"{exc}") from exc
    if json_ausgabe:
        typer.echo(roh.model_dump_json(indent=2))
        return
    _zeige_roh(_console(), roh)


@app.command()
def pruefen(
    datei: Annotated[Path, typer.Argument(help="Eingangsdatei (PDF, Bild, .eml/.msg/.txt)")],
    firma: Annotated[str | None, typer.Option("--firma", help="Firma erzwingen (sonst automatisch ermitteln).")] = None,
    json_ausgabe: Annotated[bool, typer.Option("--json", help="Belegpaket als JSON ausgeben.")] = False,
    paket_ordner: Annotated[
        Path | None, typer.Option("--paket-ordner", help="Ordner für das Paket (Standard: logs/pakete).")
    ] = None,
) -> None:
    """Datei extrahieren, gegen den Lexware-Stamm abgleichen, prüfen und das Paket speichern."""
    from auftragsautomat.extraktion.erkennung import extrahiere
    from auftragsautomat.freigabe import zustand
    from auftragsautomat.matching.pipeline import erstelle_belegpaket

    settings = _lade_settings()
    if firma is not None and firma not in settings.firmen:
        raise _fehler(f"Unbekannte Firma '{firma}'. Bekannt: {', '.join(settings.firmen) or '-'}")
    try:
        roh = extrahiere(datei, settings)
        paket = erstelle_belegpaket(roh, settings, firma_id=firma)
    except _bekannte_fehler() as exc:
        raise _fehler(f"{exc}") from exc
    except Exception as exc:  # noqa: BLE001 - unerwartete Fehler: Ursache klar melden, Traceback ins Log
        logger.exception("Prüfung von %s fehlgeschlagen", datei)
        raise _fehler(f"{type(exc).__name__}: {exc}") from exc
    ziel = zustand.speichere_paket(paket, paket_ordner if paket_ordner is not None else zustand.paket_ordner(settings))
    if json_ausgabe:
        typer.echo(paket.model_dump_json(indent=2))
        typer.echo(f"Paket gespeichert: {ziel}", err=True)
        return
    console = _console()
    _zeige_paket(console, paket)
    console.print(f"Paket gespeichert: {ziel}")
    if paket.blockiert:
        console.print("Status: BLOCKIERT – vor dem Buchen in der Freigabe korrigieren.", style="red")
    else:
        console.print("Status: freigabefähig – buchen mit: auftragsautomat buchen <paket> --freigeben", style="green")


@app.command()
def buchen(
    paket_json: Annotated[Path, typer.Argument(help="Gespeichertes Belegpaket (*.paket.json)")],
    scharf: Annotated[bool, typer.Option("--scharf", help="Wirklich in Lexware schreiben (sonst Dry Run).")] = False,
    lieferschein: Annotated[
        bool, typer.Option("--lieferschein", help="Zusätzlich Lieferschein (Auftrag weiterführen).")
    ] = False,
    freigeben: Annotated[
        bool, typer.Option("--freigeben", help="Paket als freigegeben markieren (nach Prüfung).")
    ] = False,
    ja: Annotated[bool, typer.Option("--ja", help="Sicherheitsabfrage bei --scharf überspringen.")] = False,
) -> None:
    """Gespeichertes Paket buchen. Ohne --scharf immer Dry Run (nichts wird in Lexware gespeichert)."""
    from auftragsautomat.freigabe import zustand
    from auftragsautomat.lexware.buchen import buche
    from auftragsautomat.protokoll import zusammenfassung

    settings = _lade_settings()
    try:
        paket = zustand.lade_paket(paket_json)
    except (FileNotFoundError, zustand.PaketFehler) as exc:
        raise _fehler(str(exc)) from exc
    if lieferschein:
        paket.lieferschein_erstellen = True

    dry_run = not scharf
    if scharf:
        if settings.dry_run:
            typer.secho(
                "Hinweis: settings.dry_run ist aktiv – der Lauf wird trotz --scharf als Dry Run ausgeführt.",
                err=True,
                fg=typer.colors.YELLOW,
            )
        if not ja:
            typer.secho(
                f"SCHARF buchen: Firma {paket.firma_id or '?'}, Datei {Path(paket.roh.quelldatei).name}, "
                f"{len(paket.aktive_positionen())} Positionen, Netto EK {formatiere_betrag(paket.summe_netto_ek)} €, "
                f"Netto VK {formatiere_betrag(paket.summe_netto_vk)} €.",
                fg=typer.colors.YELLOW,
            )
            if not typer.confirm("Wirklich in Lexware schreiben?", default=False):
                typer.echo("Abgebrochen, nichts gebucht.")
                raise typer.Exit(code=1)
            _freigeben(paket, "Bestätigung auf der Kommandozeile")
    if freigeben and not paket.freigegeben:
        _freigeben(paket, "--freigeben")
    if not paket.freigegeben:
        raise _fehler(
            "Paket ist nicht freigegeben. Nach Prüfung mit --freigeben (oder in der Freigabe-Oberfläche) freigeben.",
            code=2,
        )
    if paket.blockiert:
        fehler = [h for h in paket.alle_hinweise if h.schweregrad == Schweregrad.FEHLER]
        typer.secho(f"Paket ist blockiert ({len(fehler)} Fehler):", err=True, fg=typer.colors.RED)
        for h in fehler:
            typer.secho(f"  {_hinweis_zeile(h)}", err=True, fg=typer.colors.RED)
        raise _fehler("Blockierte Pakete können nicht gebucht werden; bitte in der Freigabe korrigieren.", code=2)

    ergebnis = buche(paket, settings, dry_run=dry_run)
    ziel = zustand.speichere_paket(paket, paket_json.parent, dateiname=paket_json.name)
    console = _console()
    console.print(zusammenfassung(paket, ergebnis, firma=settings.firmen.get(paket.firma_id or "")))
    console.print(f"Paket aktualisiert: {ziel}")
    _zeige_nachbereitung(console, paket, settings)
    if ergebnis.erfolgreich:
        console.print(f"Buchung {'simuliert (Dry Run)' if ergebnis.dry_run else 'ERFOLGREICH'}.", style="green")
    else:
        console.print("Buchung FEHLGESCHLAGEN / NICHT GEBUCHT – Details siehe oben.", style="red")
        raise typer.Exit(code=1)


@app.command()
def lieferschein(
    paket_json: Annotated[Path, typer.Argument(help="Gespeichertes Belegpaket (*.paket.json)")],
    ab: Annotated[str, typer.Option("--ab", help="Belegnummer der in Lexware vorhandenen Auftragsbestätigung.")],
    scharf: Annotated[bool, typer.Option("--scharf", help="Wirklich in Lexware schreiben (sonst Dry Run).")] = False,
    ja: Annotated[bool, typer.Option("--ja", help="Sicherheitsabfrage bei --scharf überspringen.")] = False,
) -> None:
    """Nur den Lieferschein zu einer vorhandenen AB anlegen (Auftrag weiterführen) – z. B. nach dem openTRANS-Import."""
    from auftragsautomat.freigabe import zustand
    from auftragsautomat.lexware.buchen import lieferschein_nachziehen
    from auftragsautomat.protokoll import zusammenfassung

    settings = _lade_settings()
    try:
        paket = zustand.lade_paket(paket_json)
    except (FileNotFoundError, zustand.PaketFehler) as exc:
        raise _fehler(str(exc)) from exc
    paket.lieferschein_erstellen = True
    if not paket.freigegeben:
        raise _fehler("Paket ist nicht freigegeben – Lieferschein nur nach Freigabe und Buchung der AB.", code=2)
    if scharf and not ja:
        typer.secho(
            f"SCHARF: Lieferschein zu AB {ab} für Firma {paket.firma_id or '?'}, Datei {Path(paket.roh.quelldatei).name}.",
            fg=typer.colors.YELLOW,
        )
        if not typer.confirm("Wirklich in Lexware schreiben?", default=False):
            typer.echo("Abgebrochen, nichts gebucht.")
            raise typer.Exit(code=1)
    ergebnis = lieferschein_nachziehen(paket, ab, settings, dry_run=not scharf)
    ziel = zustand.speichere_paket(paket, paket_json.parent, dateiname=paket_json.name)
    console = _console()
    console.print(zusammenfassung(paket, ergebnis, firma=settings.firmen.get(paket.firma_id or "")))
    console.print(f"Paket aktualisiert: {ziel}")
    if ergebnis.erfolgreich:
        console.print(f"Lieferschein {'simuliert (Dry Run)' if ergebnis.dry_run else 'ERFOLGREICH'}.", style="green")
    else:
        console.print("Lieferschein NICHT angelegt – Details siehe oben.", style="red")
        raise typer.Exit(code=1)


@app.command()
def freigabe(
    port: Annotated[int, typer.Option("--port", help="Port der Streamlit-Oberfläche.")] = 8501,
    kein_browser: Annotated[bool, typer.Option("--kein-browser", help="Browser nicht automatisch öffnen.")] = False,
) -> None:
    """Freigabe-Oberfläche starten (streamlit run app.py)."""
    if not _APP_DATEI.is_file():
        raise _fehler(f"Freigabe-App nicht gefunden: {_APP_DATEI}")
    umgebung = dict(os.environ)
    umgebung["AUFTRAGSAUTOMAT_HOME"] = str(projekt_stamm())
    quellordner = str(Path(__file__).resolve().parents[1])
    umgebung["PYTHONPATH"] = quellordner + (os.pathsep + umgebung["PYTHONPATH"] if umgebung.get("PYTHONPATH") else "")
    kommando = _streamlit_kommando(port, kein_browser=kein_browser)
    typer.echo(f"Starte Freigabe-Oberfläche auf Port {port} (beenden mit Strg+C) …")
    logger.info("Streamlit-Kommando: %s", " ".join(kommando))
    try:
        rueckgabe = _starte_prozess(kommando, umgebung)
    except FileNotFoundError as exc:
        raise _fehler(f"Streamlit konnte nicht gestartet werden: {exc}") from exc
    except KeyboardInterrupt:
        typer.echo("Freigabe-Oberfläche beendet.")
        return
    if rueckgabe != 0:
        raise _fehler(f"Streamlit wurde mit Code {rueckgabe} beendet.", code=rueckgabe)


@app.command()
def watch(
    einmalig: Annotated[bool, typer.Option("--einmalig", help="Vorhandene Dateien verarbeiten und beenden.")] = False,
    paket_ordner: Annotated[
        Path | None, typer.Option("--paket-ordner", help="Ordner für die Pakete (Standard: logs/pakete).")
    ] = None,
) -> None:
    """Eingangsordner beobachten: neue Dateien extrahieren, prüfen und als Paket speichern. Es wird NICHT gebucht."""
    from auftragsautomat import watcher

    settings = _lade_settings()
    verarbeite = watcher.verarbeite_standard(settings, paket_ordner=paket_ordner)
    typer.echo(f"Beobachte {settings.eingang_ordner} ({'einmalig' if einmalig else 'bis Strg+C'}) – es wird nicht automatisch gebucht.")
    try:
        anzahl = watcher.starte_watch(settings, verarbeite, einmalig=einmalig)
    except OSError as exc:
        raise _fehler(f"Watcher konnte nicht starten: {exc}") from exc
    typer.echo(f"{anzahl} Datei(en) verarbeitet.")


@app.command("stammdaten-pruefen")
def stammdaten_pruefen(
    firma: Annotated[str | None, typer.Option("--firma", help="Nur diese Firma (sonst alle).")] = None,
) -> None:
    """Stammdaten (CSV/ODBC) laden und einen Bericht zur Datenqualität ausgeben."""
    from auftragsautomat.stammdaten.laden import lade_stammdaten, stammdaten_bericht

    settings = _lade_settings()
    if firma is not None and firma not in settings.firmen:
        raise _fehler(f"Unbekannte Firma '{firma}'. Bekannt: {', '.join(settings.firmen) or '-'}")
    firmen = [settings.firma(firma)] if firma else list(settings.firmen.values())
    if not firmen:
        raise _fehler("Keine Firmen konfiguriert (config/firmen.yaml).")
    fehlgeschlagen: list[str] = []
    for f in firmen:
        typer.echo(f"=== {f.id} ({f.name}, Quelle {f.stammdaten.typ}) ===")
        try:
            daten = lade_stammdaten(f, cache=False)
        except Exception as exc:  # noqa: BLE001 - jede Ladeursache wird gemeldet (StammdatenFehler, ImportError pyodbc …)
            logger.exception("Stammdaten für %s nicht ladbar", f.id)
            typer.secho(f"  FEHLER: {type(exc).__name__}: {exc}", err=True, fg=typer.colors.RED)
            fehlgeschlagen.append(f.id)
            continue
        typer.echo(stammdaten_bericht(daten))
    if fehlgeschlagen:
        raise _fehler(f"Stammdaten nicht ladbar für: {', '.join(fehlgeschlagen)}")


@app.command("odbc-erkunden")
def odbc_erkunden(
    firma: Annotated[str, typer.Option("--firma", help="Firma mit ODBC-Konfiguration.")],
    ziel: Annotated[
        Path | None, typer.Option("--ziel", help="Zieldatei des Berichts (Standard: logs/odbc_schema_<firma>.md).")
    ] = None,
    praefix: Annotated[str | None, typer.Option("--praefix", help="Nur Tabellen mit diesem Namenspräfix.")] = None,
) -> None:
    """Lexware-ODBC-Schema (Tabellen, Spalten) erkunden und als Markdown-Bericht schreiben. Nur lesend."""
    from auftragsautomat.stammdaten import odbc_quelle

    settings = _lade_settings()
    try:
        f = settings.firma(firma)
    except KeyError as exc:
        raise _fehler(str(exc.args[0])) from exc
    konfig = f.stammdaten.odbc
    if konfig is None:
        raise _fehler(f"Firma '{firma}' hat keine ODBC-Konfiguration (stammdaten.odbc in config/firmen.yaml).")
    ziel = ziel if ziel is not None else Path(settings.log_ordner) / f"odbc_schema_{firma}.md"
    try:
        pfad = odbc_quelle.schreibe_schema_bericht(konfig, ziel, praefix=praefix)
    except Exception as exc:  # noqa: BLE001 - pyodbc fehlt, DSN falsch, Treiberfehler: alles klar melden
        logger.exception("ODBC-Erkundung für %s fehlgeschlagen", firma)
        raise _fehler(f"ODBC-Erkundung fehlgeschlagen: {type(exc).__name__}: {exc}") from exc
    typer.echo(f"Schema-Bericht geschrieben: {pfad}")


def _liegt_unter(pfad: Path, basis: Path) -> bool:
    try:
        Path(pfad).resolve().relative_to(Path(basis).resolve())
    except ValueError:
        return False
    return True


@app.command("lexware-erkunden")
def lexware_erkunden(
    ziel: Annotated[
        Path | None,
        typer.Option("--ziel", help="Zielordner (Standard: logs/lexware_erkundung/<JJJJ-MM-TT_HHMMSS>; mit --aus-datei: Ordner der Datei)."),
    ] = None,
    tiefe: Annotated[int, typer.Option("--tiefe", min=0, help="Maximale Tiefe des Control-Baums (Fenster = Ebene 0).")] = 12,
    alle_fenster: Annotated[
        bool, typer.Option("--alle-fenster", help="Auch offene Dialoge/Unterfenster des Lexware-Prozesses lesen.")
    ] = False,
    backend: Annotated[
        str | None, typer.Option("--backend", help="uia | win32 | beide (Standard: settings.lexware.backend).")
    ] = None,
    abgleich: Annotated[
        bool, typer.Option("--abgleich", help="config/lexware_controls.yaml mit dem gefundenen Baum abgleichen (abgleich.md).")
    ] = False,
    aus_datei: Annotated[
        Path | None,
        typer.Option("--aus-datei", help="Abgleich offline aus baum.json einer früheren Erkundung (ohne Lexware)."),
    ] = None,
    max_elemente: Annotated[
        int, typer.Option("--max-elemente", min=1, help="Obergrenze der gelesenen Elemente insgesamt.")
    ] = 20000,
) -> None:
    """Lexware-Oberfläche NUR LESEND erkunden: Control-Baum nach baum.json/baum.txt, optional Abgleich der Control-Namen.

    Dockt nur an das laufende Lexware an (startet es nie), klickt nichts, tippt nichts, setzt keinen Fokus.
    """
    from auftragsautomat.lexware import erkundung as ek
    from auftragsautomat.lexware.instanz import LexwareNichtGestartet
    from auftragsautomat.lexware.ui_automation import (
        BackendNichtVerfuegbar,
        NichtAufWindows,
        lade_controls,
        standard_controls_datei,
    )

    settings = _lade_settings()
    if backend is not None and backend not in ("uia", "win32", "beide"):
        raise _fehler(f"Unbekanntes Backend '{backend}' (erlaubt: uia, win32, beide).")

    quelle: Path | None = None
    if aus_datei is not None:
        try:
            erk = ek.lade_erkundung(aus_datei)
        except ek.ErkundungsFehler as exc:
            raise _fehler(str(exc)) from exc
        quelle = Path(aus_datei)
        ordner = Path(ziel) if ziel is not None else quelle.parent
        backends = list(erk.backends)
        if backend is not None:
            gewuenscht = ["uia", "win32"] if backend == "beide" else [backend]
            backends = [b for b in gewuenscht if erk.fenster_von(b)]
            if not backends:
                raise _fehler(f"{aus_datei} enthält keinen Baum für Backend {backend} (vorhanden: {', '.join(erk.backends) or '-'}).")
        abgleich = True
        dateien: list[Path] = []
    else:
        name = backend or str(settings.lexware.backend or "uia")
        backends = ["uia", "win32"] if name == "beide" else [name]
        try:
            erk = ek.erkunde(settings, backends=backends, tiefe=tiefe, alle_fenster=alle_fenster, max_elemente=max_elemente)
        except (NichtAufWindows, BackendNichtVerfuegbar) as exc:
            raise _fehler(
                f"{exc} Die Erkundung läuft nur auf dem Windows-PC mit Lexware; den Abgleich einer vorhandenen "
                "baum.json gibt es überall mit --aus-datei."
            ) from exc
        except LexwareNichtGestartet as exc:
            raise _fehler(str(exc)) from exc
        ordner = Path(ziel) if ziel is not None else ek.standard_zielordner(Path(settings.log_ordner))
        try:
            dateien = [ek.schreibe_baum_json(erk, ordner / "baum.json"), ek.schreibe_baum_txt(erk, ordner / "baum.txt")]
        except OSError as exc:
            raise _fehler(f"Ergebnis nicht schreibbar ({ordner}): {exc}") from exc

    bilanzen: list[str] = []
    if abgleich:
        datei = settings.lexware.controls_datei
        controls_datei = Path(datei) if datei else standard_controls_datei()
        try:
            controls = lade_controls(controls_datei if controls_datei.exists() else None)
        except (ValueError, OSError) as exc:
            raise _fehler(f"Control-Datei {controls_datei} nicht lesbar: {exc}") from exc
        abgleiche = [
            ek.gleiche_ab(
                erk, controls, backend=b, controls_datei=controls_datei if controls_datei.exists() else None,
                fenster_titel_regex=settings.lexware.fenster_titel_regex,
            )
            for b in backends
        ]
        try:
            dateien.append(
                ek.schreibe_abgleich_md(
                    erk, abgleiche, ordner / "abgleich.md",
                    controls_datei=controls_datei if controls_datei.exists() else None, quelle=quelle,
                )
            )
        except OSError as exc:
            raise _fehler(f"abgleich.md nicht schreibbar ({ordner}): {exc}") from exc
        bilanzen = [f"Abgleich ({a.backend}): {a.bilanz_text()}" for a in abgleiche]

    typer.echo(f"Lexware-Erkundung ({'aus Datei' if quelle else 'nur lesend'}): {ordner}")
    for pfad in dateien:
        typer.echo(f"  {pfad}")
    typer.echo(
        f"Fenster: {len(erk.fenster)}, Elemente: {erk.anzahl_elemente}, Fehler beim Lesen: {erk.fehler}"
        + (f" – ABGESCHNITTEN bei {erk.max_elemente} Elementen" if erk.abgeschnitten else "")
    )
    if erk.aktive_firma:
        typer.echo(f"Aktive Firma: {erk.aktive_firma}" + (f" ({erk.firma_id})" if erk.firma_id else ""))
    for name, text in erk.backend_fehler.items():
        typer.secho(f"Backend {name}: {text}", fg=typer.colors.YELLOW)
    for zeile in bilanzen:
        typer.echo(zeile)
    if not _liegt_unter(ordner, Path(settings.log_ordner)):
        typer.secho(
            f"WARNUNG: {ordner} liegt nicht unter {settings.log_ordner} – die Dateien nicht ins Git aufnehmen.",
            fg=typer.colors.YELLOW,
        )
    typer.secho(f"HINWEIS: {ek.DATENSCHUTZ_HINWEIS}", fg=typer.colors.YELLOW)


@app.command("beispiel-pdfs")
def beispiel_pdfs(
    ordner: Annotated[Path | None, typer.Argument(help="Zielordner (Standard: tests/fixtures/pdfs).")] = None,
) -> None:
    """Beispiel-PDFs für Tests und Demos erzeugen."""
    try:
        skript = _lade_beispielskript()
        dateien = skript.erzeuge_alle(ordner)
    except FileNotFoundError as exc:
        raise _fehler(str(exc)) from exc
    except ImportError as exc:
        raise _fehler(f"Abhängigkeit für die PDF-Erzeugung fehlt (reportlab/Pillow): {exc}") from exc
    for datei in dateien:
        typer.echo(f"erzeugt: {datei}")
    typer.echo(f"{len(dateien)} Beispiel-PDF(s) erzeugt.")


# ---------------------------------------------------------------------------
# Ausbaustufe 2: Hilfen
# ---------------------------------------------------------------------------


def _lade_paket_oder_fehler(paket_json: Path) -> Belegpaket:
    from auftragsautomat.freigabe import zustand

    try:
        return zustand.lade_paket(paket_json)
    except (FileNotFoundError, zustand.PaketFehler) as exc:
        raise _fehler(str(exc)) from exc


def _effektiver_dry_run(settings: Settings, scharf: bool) -> bool:
    """Ohne ``--scharf`` Dry Run; mit ``--scharf`` nur dann scharf, wenn ``settings.dry_run`` aus ist (Hinweis)."""
    if not scharf:
        return True
    if settings.dry_run:
        typer.secho(
            "Hinweis: settings.dry_run ist aktiv – der Lauf wird trotz --scharf als Dry Run ausgeführt.",
            err=True,
            fg=typer.colors.YELLOW,
        )
        return True
    return False


def _backends_oder_fehler(settings: Settings, *, dry_run: bool, hinweise: list[str] | None = None):
    from auftragsautomat.lexware import nachbereitung

    try:
        return nachbereitung.erzeuge_backends_fuer(settings, dry_run=dry_run, hinweise=hinweise)
    except Exception as exc:  # noqa: BLE001 - M365Fehler, fehlende Bibliothek, NichtAufWindows
        logger.exception("Backends konnten nicht erzeugt werden")
        raise _fehler(f"Backend '{settings.m365.backend}' nicht verfügbar: {type(exc).__name__}: {exc}") from exc


def _zeige_nachbereitung(console: Console, paket: Belegpaket, settings: Settings) -> None:
    from auftragsautomat.lexware import nachbereitung

    nb = nachbereitung.lade_nachbereitung(paket, settings)
    if nb is None:
        return
    console.print(nachbereitung.zusammenfassung_nachbereitung(nb))


def _zeige_hinweisliste(console: Console, hinweise: Sequence[str]) -> None:
    if not hinweise:
        return
    console.print("Hinweise:")
    for h in hinweise:
        console.print(f"  - {escape(str(h))}")


def _modus_text(dry_run: bool) -> str:
    return "Dry Run" if dry_run else "SCHARF"


def _firma_pruefen(settings: Settings, firma: str | None) -> None:
    if firma is not None and firma not in settings.firmen:
        raise _fehler(f"Unbekannte Firma '{firma}'. Bekannt: {', '.join(settings.firmen) or '-'}")


# ---------------------------------------------------------------------------
# Ausbaustufe 2: Microsoft 365
# ---------------------------------------------------------------------------


@app.command("m365-anmelden")
def m365_anmelden(
    device_code: Annotated[
        bool, typer.Option("--device-code", help="Anmeldung per Gerätecode (ohne Browser auf diesem PC).")
    ] = False,
    nur_status: Annotated[bool, typer.Option("--nur-status", help="Nur den Anmeldestatus anzeigen, nicht anmelden.")] = False,
) -> None:
    """Bei Microsoft 365 anmelden (Backend graph) bzw. den Anmeldestatus anzeigen. Öffnet ohne --device-code den Browser."""
    from auftragsautomat.m365.fabrik import anmelden, pruefe_anmeldung

    settings = _lade_settings()
    status = pruefe_anmeldung(settings)
    if nur_status or settings.m365.backend != "graph":
        typer.echo(status)
        return
    typer.echo(f"Status vorher: {status}")
    try:
        typer.echo(anmelden(settings, device_code=device_code, ausgabe=typer.echo))
    except Exception as exc:  # noqa: BLE001 - msal/Netzwerk/Konfiguration: eine Zeile statt Traceback
        logger.exception("Microsoft-365-Anmeldung fehlgeschlagen")
        raise _fehler(f"Anmeldung fehlgeschlagen: {type(exc).__name__}: {exc}") from exc


@app.command("m365-pruefen")
def m365_pruefen() -> None:
    """Nur lesen: Blattnamen der Fixterminüberwachung, Kontakte der Kontaktliste und den Dossier-Ordner prüfen."""
    from auftragsautomat.kontakte.liste import KontakteFehler, kontaktliste_aus_settings
    from auftragsautomat.lexware.nachbereitung import ist_konfiguriert
    from auftragsautomat.m365.fabrik import pruefe_anmeldung

    settings = _lade_settings()
    typer.echo(f"Backend: {settings.m365.backend} – {pruefe_anmeldung(settings)}")
    backends = _backends_oder_fehler(settings, dry_run=True)
    probleme: list[str] = []
    erfolge = 0

    ref = settings.m365.fixtermin_datei
    if ist_konfiguriert(ref):
        try:
            blaetter = backends.excel.blattnamen(ref)
            typer.echo(f"Fixterminüberwachung: {len(blaetter)} Blätter: {', '.join(blaetter)}")
            erfolge += 1
        except Exception as exc:  # noqa: BLE001 - jede Ursache melden
            probleme.append(f"Fixterminüberwachung nicht lesbar: {type(exc).__name__}: {exc}")
    else:
        typer.echo("Fixterminüberwachung: nicht konfiguriert (m365.fixtermin_datei).")

    if ist_konfiguriert(settings.m365.kontaktliste_datei):
        try:
            liste = kontaktliste_aus_settings(settings, backends.excel)
            typer.echo(
                f"Kontaktliste: {len(liste.kontakte)} Kontakte ({len(liste.lieferanten)} Lieferanten, "
                f"{len(liste.speditionen)} Speditionen, {len(liste.kunden)} Kunden)"
            )
            erfolge += 1
        except (KontakteFehler, Exception) as exc:  # noqa: BLE001
            probleme.append(f"Kontaktliste nicht lesbar: {type(exc).__name__}: {exc}")
    else:
        typer.echo("Kontaktliste: nicht konfiguriert (m365.kontaktliste_datei).")

    if ist_konfiguriert(settings.m365.dossier_ordner):
        try:
            eintraege = backends.datei.liste(settings.m365.dossier_ordner)
            typer.echo(f"Dossier-Ordner: {sum(1 for e in eintraege if e.ist_ordner)} Unterordner")
            erfolge += 1
        except Exception as exc:  # noqa: BLE001
            probleme.append(f"Dossier-Ordner nicht lesbar: {type(exc).__name__}: {exc}")
    else:
        typer.echo(f"Dossier-Ordner: nicht konfiguriert (m365.dossier_ordner); Dry Run nutzt {settings.dossier.lokaler_ordner}.")

    for p in probleme:
        typer.secho(f"  {p}", err=True, fg=typer.colors.RED)
    if probleme and not erfolge:
        raise _fehler("Keine der konfigurierten Dateien konnte gelesen werden.")


# ---------------------------------------------------------------------------
# Ausbaustufe 2: Fixtermin, Entwürfe, Dossier, Ablage, Benennung, Firma
# ---------------------------------------------------------------------------


@app.command("fixtermin-eintragen")
def fixtermin_eintragen(
    paket_json: Annotated[Path, typer.Argument(help="Gespeichertes Belegpaket (*.paket.json)")],
    scharf: Annotated[bool, typer.Option("--scharf", help="Wirklich in die Fixterminüberwachung schreiben (sonst Vorschau-CSV).")] = False,
) -> None:
    """Fixtermin des Pakets in die Fixterminüberwachung eintragen. Ohne --scharf nur Vorschau (logs/fixtermine_vorschau.csv)."""
    from auftragsautomat.lexware import nachbereitung

    settings = _lade_settings()
    paket = _lade_paket_oder_fehler(paket_json)
    dry_run = _effektiver_dry_run(settings, scharf)
    hinweise: list[str] = []
    backends = _backends_oder_fehler(settings, dry_run=dry_run, hinweise=hinweise)
    kurznamen = nachbereitung.lade_kurznamen(settings, backends.excel, hinweise)
    gespeichert = nachbereitung.lade_nachbereitung(paket, settings)
    ergebnis = gespeichert.ergebnis if gespeichert is not None else None
    fixtermin, vorschau = nachbereitung.fixtermin_eintragen(
        paket, ergebnis, settings, backends=backends, dry_run=dry_run, kurznamen=kurznamen, hinweise=hinweise
    )
    console = _console()
    if fixtermin is None:
        _zeige_hinweisliste(console, hinweise)
        raise _fehler("Kein Fixtermin eingetragen (siehe Hinweise).")
    console.print(f"Fixtermin ({_modus_text(dry_run)}): {fixtermin.liefertag:%d.%m.%Y} | {fixtermin.lieferant_kurz} | {fixtermin.kunde_kurz}")
    console.print(
        f"  Auftragsdatum {formatiere_datum(fixtermin.auftragsdatum)}, Spedition {fixtermin.spedition or '-'}, "
        f"Best.-Nr {fixtermin.bestellnummer or '-'}, AB {fixtermin.ab_nummer or '-'}, Kundenbestellnr. {fixtermin.kundenbestellnummer or '-'}"
    )
    if fixtermin.zeile is not None:
        console.print(f"  Zeile {fixtermin.zeile}" + (" (Vorschau, nicht geschrieben)" if dry_run else " eingetragen"))
    if vorschau is not None:
        console.print(f"  Vorschau-CSV: {vorschau}")
    _zeige_hinweisliste(console, hinweise)
    if fixtermin.zeile is None:
        raise typer.Exit(code=1)


@app.command()
def entwuerfe(
    paket_json: Annotated[Path, typer.Argument(help="Gespeichertes Belegpaket (*.paket.json)")],
    scharf: Annotated[bool, typer.Option("--scharf", help="Entwürfe im echten Postfach anlegen (sonst .eml unter logs/entwuerfe).")] = False,
) -> None:
    """E-Mail-Entwürfe zur Buchung erzeugen (Bestellung ans Weingut, Auftragskopie, Speditionsauftrag). Es wird nie gesendet."""
    from auftragsautomat.lexware import nachbereitung

    settings = _lade_settings()
    paket = _lade_paket_oder_fehler(paket_json)
    dry_run = _effektiver_dry_run(settings, scharf)
    hinweise: list[str] = []
    backends = _backends_oder_fehler(settings, dry_run=dry_run, hinweise=hinweise)
    gespeichert = nachbereitung.lade_nachbereitung(paket, settings)
    ergebnis = gespeichert.ergebnis if gespeichert is not None else None
    fixtermin = gespeichert.fixtermin if gespeichert is not None else None
    kontakte = nachbereitung.lade_kontakte(settings, backends, hinweise=hinweise)
    pdfs = nachbereitung.finde_lexware_pdfs(paket, settings, nicht_vor=ergebnis.zeitpunkt if ergebnis is not None else None)
    liste = nachbereitung.entwuerfe_erstellen(
        paket, ergebnis, settings, backends=backends, fixtermin=fixtermin, kontakte=kontakte, lexware_pdfs=pdfs, hinweise=hinweise
    )
    console = _console()
    console.print(f"{len(liste)} Entwürfe ({_modus_text(dry_run)}, nie gesendet):")
    for e in liste:
        console.print(escape(f"  - {e.typ.value}: '{e.betreff}' an {', '.join(e.an) or '(kein Empfänger)'}"))
        if e.cc:
            console.print(f"      CC: {', '.join(e.cc)}")
        for a in e.anhaenge:
            console.print(f"      Anhang: {Path(a).name}")
        console.print(f"      Gespeichert: {e.speicherort or '-'}")
    _zeige_hinweisliste(console, hinweise)
    if not liste:
        raise typer.Exit(code=1)


@app.command()
def dossier(
    paket_json: Annotated[Path, typer.Argument(help="Gespeichertes Belegpaket (*.paket.json)")],
    scharf: Annotated[bool, typer.Option("--scharf", help="Dossier im OneDrive-Ordner 'vorangefertigte Dossiers' (sonst logs/dossiers).")] = False,
) -> None:
    """Dossier zum Paket anlegen oder fortschreiben (Deckblatt, Belege, Entwürfe, Fixtermin, Index)."""
    from auftragsautomat.lexware import nachbereitung

    settings = _lade_settings()
    paket = _lade_paket_oder_fehler(paket_json)
    dry_run = _effektiver_dry_run(settings, scharf)
    hinweise: list[str] = []
    backends = _backends_oder_fehler(settings, dry_run=dry_run, hinweise=hinweise)
    gespeichert = nachbereitung.lade_nachbereitung(paket, settings)
    ergebnis = gespeichert.ergebnis if gespeichert is not None else None
    fixtermin = gespeichert.fixtermin if gespeichert is not None else None
    entwuerfe_alt = gespeichert.entwuerfe if gespeichert is not None else []
    pdfs = nachbereitung.finde_lexware_pdfs(paket, settings, nicht_vor=ergebnis.zeitpunkt if ergebnis is not None else None)
    if ergebnis is not None:
        for kuerzel, pfad in ergebnis.pdfs.items():
            if Path(pfad).is_file():
                pdfs.setdefault(kuerzel, Path(pfad))
    ergebnis_dossier = nachbereitung.dossier_anlegen(
        paket, ergebnis, settings, backends=backends, dry_run=dry_run, entwuerfe=entwuerfe_alt,
        fixtermin=fixtermin, lexware_pdfs=pdfs, hinweise=hinweise,
    )
    console = _console()
    if ergebnis_dossier is None:
        _zeige_hinweisliste(console, hinweise)
        raise _fehler("Dossier konnte nicht angelegt werden (siehe Hinweise).")
    console.print(f"Dossier ({_modus_text(dry_run)}): {ergebnis_dossier.name}")
    console.print(f"  Ordner: {ergebnis_dossier.ordner}")
    console.print(f"  Zielordner-Vorschlag: {ergebnis_dossier.vorgeschlagener_zielordner or '-'}")
    console.print(f"  Dateien ({len(ergebnis_dossier.dateien)}):")
    for d in ergebnis_dossier.dateien:
        console.print(f"    - {escape(d.name)} ({d.typ})")
    if ergebnis_dossier.offene_punkte:
        console.print(f"  Offene Punkte ({len(ergebnis_dossier.offene_punkte)}):")
        for p in ergebnis_dossier.offene_punkte:
            console.print(f"    - {escape(p)}")
    _zeige_hinweisliste(console, hinweise)


@app.command()
def ablegen(
    paket_json: Annotated[Path, typer.Argument(help="Gespeichertes Belegpaket (*.paket.json)")],
    scharf: Annotated[bool, typer.Option("--scharf", help="Wirklich nach OneDrive 'Aufträge' kopieren (sonst nur Protokoll).")] = False,
) -> None:
    """Fertige Dateien (Kd, Cde, Kopie, Speditionsauftrag, Ls) in den OneDrive-Ordner 'Aufträge mit/ohne Provision' ablegen."""
    from auftragsautomat.lexware import nachbereitung
    from auftragsautomat.models import BuchungsErgebnis

    settings = _lade_settings()
    paket = _lade_paket_oder_fehler(paket_json)
    dry_run = _effektiver_dry_run(settings, scharf)
    hinweise: list[str] = []
    backends = _backends_oder_fehler(settings, dry_run=dry_run, hinweise=hinweise)
    gespeichert = nachbereitung.lade_nachbereitung(paket, settings)
    if gespeichert is not None and gespeichert.ergebnis is not None:
        ergebnis = gespeichert.ergebnis
        nicht_vor: datetime | None = ergebnis.zeitpunkt
    else:
        hinweise.append(
            "Kein Buchungsergebnis gespeichert (zuerst 'buchen'); abgelegt werden nur Dateien aus einer Buchung – "
            "scharf ablegen ist erst nach einer erfolgreichen Buchung möglich"
        )
        ergebnis = BuchungsErgebnis(
            firma_id=paket.firma_id or "unbekannt", quelldatei=Path(paket.roh.quelldatei), dry_run=True, zeitpunkt=datetime.now(),
        )
        nicht_vor = None
    # Nach Hausmuster gefundene Dateien nur als Vorschlag nennen, nie unter dem Namen des Auftrags ablegen
    for kuerzel, pfad in nachbereitung.finde_lexware_pdfs(paket, settings, nicht_vor=nicht_vor).items():
        if kuerzel not in ergebnis.pdfs or not Path(ergebnis.pdfs[kuerzel]).is_file():
            hinweise.append(f"Vorschlag (nicht abgelegt): PDF {kuerzel} nach Hausmuster gefunden: {pfad}")
    ergebnisse = nachbereitung.ablegen(paket, ergebnis, settings, backends=backends, dry_run=dry_run, hinweise=hinweise)
    console = _console()
    tabelle = Table(title=f"Ablage ({_modus_text(dry_run)})")
    for spalte in ("Kürzel", "Quelle", "Zielordner", "Zielname", "Status"):
        tabelle.add_column(spalte)
    for a in ergebnisse:
        status = "nur protokolliert" if a.dry_run else ("abgelegt" if a.abgelegt else f"NICHT abgelegt: {a.fehler}")
        if a.dry_run and a.fehler:
            status += f" ({a.fehler})"
        tabelle.add_row(a.kuerzel, Path(a.datei).name, a.zielordner, a.zielname, status)
    console.print(tabelle)
    _zeige_hinweisliste(console, hinweise)
    if not ergebnisse:
        raise typer.Exit(code=1)


@app.command()
def benennen(
    paket_json: Annotated[Path, typer.Argument(help="Gespeichertes Belegpaket (*.paket.json)")],
    kuerzel: Annotated[str, typer.Option("--kuerzel", help="Dokumentkürzel laut Titulierung (Kd, Cde, Kopie, Oc, Neu, Ls, Speditionsauftrag …).")],
    datum: Annotated[str | None, typer.Option("--datum", help="Datum im Namen (TT.MM.JJJJ; Standard heute).")] = None,
) -> None:
    """Dateinamen laut Benennungsschema (Titulierung) für ein Paket anzeigen."""
    from auftragsautomat import benennung
    from auftragsautomat.extraktion.zahlen import parse_datum
    from auftragsautomat.models import DOKUMENTKUERZEL

    settings = _lade_settings()
    paket = _lade_paket_oder_fehler(paket_json)
    tag = None
    if datum:
        tag = parse_datum(datum)
        if tag is None:
            raise _fehler(f"'{datum}' ist kein Datum (erwartet TT.MM.JJJJ).")
    try:
        name = benennung.name_fuer_paket(kuerzel, paket, settings, datum=tag)
    except KeyError as exc:
        raise _fehler(str(exc.args[0]) if exc.args else str(exc)) from exc
    except ValueError as exc:
        raise _fehler(f"{exc} – bekannte Kürzel: {', '.join(DOKUMENTKUERZEL)}") from exc
    typer.echo(name)
    if kuerzel in ("Kopie", "Neu Kopie"):
        verstoesse = benennung.pruefe_auftragskopie_name(name, settings)
        if verstoesse:
            typer.secho(f"Achtung: verbotene Wörter im Namen der Auftragskopie: {', '.join(verstoesse)}", err=True, fg=typer.colors.RED)
            raise typer.Exit(code=1)


@app.command("firma-merken")
def firma_merken(
    paket_json: Annotated[Path, typer.Argument(help="Gespeichertes Belegpaket (*.paket.json)")],
    firma: Annotated[str, typer.Option("--firma", help="Firmen-ID aus config/firmen.yaml.")],
) -> None:
    """Firma für das Paket festlegen, neu zuordnen und die Wahl je Kunde + Lieferant im Firmen-Gedächtnis merken."""
    from auftragsautomat.freigabe import zustand
    from auftragsautomat.matching.pipeline import merke_firma_fuer, setze_firma

    settings = _lade_settings()
    _firma_pruefen(settings, firma)
    paket = _lade_paket_oder_fehler(paket_json)
    try:
        neu = setze_firma(paket, firma, settings)
    except Exception as exc:  # noqa: BLE001 - Stammdaten/Matching: Ursache klar melden
        logger.exception("Firma %s konnte nicht gesetzt werden", firma)
        raise _fehler(f"Firma konnte nicht gesetzt werden: {type(exc).__name__}: {exc}") from exc
    neu.firma_merken = True
    regel = merke_firma_fuer(neu, settings, benutzer=_benutzername())
    ziel = zustand.speichere_paket(neu, paket_json.parent, dateiname=paket_json.name)
    console = _console()
    console.print(f"Firma {firma} gesetzt; Paket neu zugeordnet und geprüft ({'BLOCKIERT' if neu.blockiert else 'freigabefähig'}).")
    if regel is None:
        console.print(
            "Nicht gemerkt: firmenwahl.merken ist aus oder Kunde unbekannt (siehe Log).", style="yellow"
        )
    else:
        console.print(
            f"Gemerkt: Kunde '{regel.kunde_kurz}', Lieferant '{regel.lieferant_kurz or 'alle'}' -> {regel.firma_id} "
            f"(gelernt von {regel.gelernt_von or '-'}) in {settings.firmenwahl.gedaechtnis_datei}"
        )
    console.print(f"Paket aktualisiert: {ziel}")


# ---------------------------------------------------------------------------
# Ausbaustufe 2: Überwachung, Tracking, Kontakte
# ---------------------------------------------------------------------------


@app.command()
def ueberwachen(
    tage: Annotated[int | None, typer.Option("--tage", help="Fixtermine so viele Tage voraus betrachten (Standard: ueberwachung.tage_voraus).")] = None,
    scharf: Annotated[bool, typer.Option("--scharf", help="Häkchen/Referenzen in die Excel schreiben, Entwürfe im Postfach anlegen.")] = False,
    nur_lesen: Annotated[bool, typer.Option("--nur-lesen", help="Keine Zugriffe auf Graph/Outlook/Excel: nur Fake-Backends (Test).")] = False,
) -> None:
    """Täglicher Überwachungslauf: Fixtermine prüfen, Bestätigungen aus Mails, Nachfragen als Entwürfe, Bericht schreiben."""
    from auftragsautomat.lexware import nachbereitung
    from auftragsautomat.ueberwachung.lauf import ueberwachen as lauf_ausfuehren

    settings = _lade_settings()
    if tage is not None:
        if tage < 0:
            raise _fehler("--tage muss 0 oder größer sein.")
        settings.ueberwachung.tage_voraus = tage
    dry_run = _effektiver_dry_run(settings, scharf)
    try:
        backends = nachbereitung.backends_fuer_ueberwachung(settings, scharf=not dry_run, nur_lesen=nur_lesen)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Backends für die Überwachung konnten nicht erzeugt werden")
        raise _fehler(f"Backend '{settings.m365.backend}' nicht verfügbar: {type(exc).__name__}: {exc}") from exc
    try:
        bericht, pfad = lauf_ausfuehren(settings, dry_run=dry_run, backends=backends)
    except Exception as exc:  # noqa: BLE001 - z. B. keine Firma konfiguriert
        logger.exception("Überwachungslauf fehlgeschlagen")
        raise _fehler(f"Überwachungslauf fehlgeschlagen: {type(exc).__name__}: {exc}") from exc
    console = _console()
    console.print(_bericht_tabelle(bericht))
    console.print(
        f"Überwachung ({_modus_text(dry_run)}{', nur lesen' if nur_lesen else ''}) bis {bericht.zeitraum_bis:%d.%m.%Y}: "
        f"{len(bericht.punkte)} Fixtermine, {len(bericht.entwuerfe)} Entwürfe, {len(bericht.terminaenderungen)} Terminänderungen, "
        f"{len(bericht.kontaktaenderungen)} Kontaktänderungen, {len(bericht.fehler)} Fehler"
    )
    for e in bericht.entwuerfe:
        console.print(escape(f"  Entwurf {e.typ.value}: '{e.betreff}' -> {e.speicherort or '-'}"))
    for f in bericht.fehler:
        console.print(f"  Fehler: {escape(f)}", style="red")
    console.print(f"Bericht: {pfad}")


def _bericht_tabelle(bericht) -> Table:  # noqa: ANN001 - Ueberwachungsbericht (lazy import)
    from auftragsautomat.ueberwachung.bericht import AMPEL_REIHENFOLGE, AMPEL_TEXT

    tabelle = Table(title=f"Überwachung vom {bericht.zeitpunkt:%d.%m.%Y %H:%M}")
    for spalte in ("Ampel", "Liefertag", "Lieferant", "Kunde", "Lief. best.", "Sped. best.", "Abgeholt", "Grund", "Maßnahmen"):
        tabelle.add_column(spalte)
    farben = {"rot": "red", "gelb": "yellow", "gruen": "green", "grau": "dim"}
    rang = {a: i for i, a in enumerate(AMPEL_REIHENFOLGE)}
    for p in sorted(bericht.punkte, key=lambda x: (rang.get(x.ampel, 9), x.fixtermin.liefertag)):
        ft = p.fixtermin
        tabelle.add_row(
            AMPEL_TEXT.get(p.ampel, p.ampel),
            ft.liefertag.strftime("%d.%m.%Y"),
            ft.lieferant_kurz,
            ft.kunde_kurz,
            "ja" if ft.lieferant_bestaetigt else "nein",
            "ja" if ft.spedition_bestaetigt else "nein",
            "ja" if ft.abgeholt else "nein",
            p.grund,
            "; ".join(p.aktionen),
            style=farben.get(p.ampel),
        )
    return tabelle


@app.command()
def heute() -> None:
    """Den letzten Überwachungsbericht anzeigen (Ampel je Fixtermin, Entwürfe, Vorschläge)."""
    from auftragsautomat.ueberwachung.bericht import lade_letzten_bericht_mit_zusatz

    settings = _lade_settings()
    ordner = Path(settings.ueberwachung.berichte_ordner)
    geladen = lade_letzten_bericht_mit_zusatz(ordner)
    if geladen is None:
        typer.echo(f"Kein Überwachungsbericht in {ordner} – zuerst 'auftragsautomat ueberwachen' ausführen.")
        return
    bericht, hinweise, protokoll = geladen
    console = _console()
    console.print(_bericht_tabelle(bericht))
    if bericht.entwuerfe:
        console.print("Entwürfe (nicht gesendet):")
        for e in bericht.entwuerfe:
            console.print(escape(f"  - {e.typ.value}: '{e.betreff}' an {', '.join(e.an) or '(kein Empfänger)'} -> {e.speicherort or '-'}"))
    if bericht.terminaenderungen:
        console.print("Vorschläge Terminänderung:")
        for t in bericht.terminaenderungen:
            console.print(
                f"  - {t.fixtermin.lieferant_kurz} / {t.fixtermin.kunde_kurz}: {t.fixtermin.liefertag:%d.%m.%Y} -> "
                f"{t.neues_datum:%d.%m.%Y} ({t.grund}){' – übernommen' if t.uebernommen else ''}"
            )
    if bericht.kontaktaenderungen:
        console.print("Vorschläge Kontaktänderung:")
        for k in bericht.kontaktaenderungen:
            wer = f"{k.kontakt.firma} / {k.kontakt.name or '-'}" if k.kontakt is not None else "neuer Kontakt"
            console.print(f"  - {wer}: {k.feld} '{k.alt or ''}' -> '{k.neu}' ({k.quelle}){' – übernommen' if k.uebernommen else ''}")
    for f in bericht.fehler:
        console.print(f"Fehler: {escape(f)}", style="red")
    _zeige_hinweisliste(console, hinweise)
    if protokoll:
        console.print("Änderungen:")
        for p in protokoll:
            console.print(f"  - {escape(p)}")


@app.command()
def tracking(
    referenz: Annotated[str, typer.Argument(help="Referenz der Spedition (z. B. DEMB00000) oder unsere Bestellnummer.")],
    kundenreferenz: Annotated[bool, typer.Option("--kundenreferenz", help="REFERENZ ist unsere eigene Referenz (Your Ref), nicht die der Spedition.")] = False,
    spedition: Annotated[str | None, typer.Option("--spedition", help="Spedition (Standard: kontakte.standard_spedition).")] = None,
) -> None:
    """Sendungsstatus über die Tracking-Kette (Mail, API, öffentliche Sendungsverfolgung, Portal) abfragen."""
    from auftragsautomat.models import Sendung
    from auftragsautomat.tracking.kette import erzeuge_kette

    settings = _lade_settings()
    referenz = referenz.strip()
    if not referenz:
        raise _fehler("Referenz fehlt.")
    sendung = Sendung(
        spedition=spedition or settings.kontakte.standard_spedition,
        referenz=None if kundenreferenz else referenz,
        kundenreferenz=referenz if kundenreferenz else None,
    )
    try:
        kette = erzeuge_kette(settings)
        aktuell = kette.aktualisiere(sendung)
    except Exception as exc:  # noqa: BLE001 - Konfiguration/Netzwerk/Browser
        logger.exception("Tracking fehlgeschlagen")
        raise _fehler(f"Tracking fehlgeschlagen: {type(exc).__name__}: {exc}") from exc
    console = _console()
    console.print(f"Sendung {aktuell.referenz or '-'} (unsere Ref. {aktuell.kundenreferenz or '-'}), Spedition {aktuell.spedition}")
    console.print(f"  Status: {aktuell.status.value} (Quelle {aktuell.quelle}; Kette: {', '.join(kette.namen) or '-'})")
    if aktuell.puenktlich is not None:
        console.print(f"  Pünktlich: {'ja' if aktuell.puenktlich else 'nein'}")
    console.print(f"  Geplante Abholung: {formatiere_datum(aktuell.geplante_abholung)}; geplante Zustellung: {formatiere_datum(aktuell.geplante_zustellung)}")
    if aktuell.meilensteine:
        console.print("  Meilensteine:")
        for m in aktuell.meilensteine:
            zeit = m.zeitpunkt.strftime("%d.%m.%Y %H:%M") if m.zeitpunkt else "-"
            console.print(escape(f"    - {zeit} {m.code}: {m.text}{' (geplant)' if m.geplant else ''}{f' – {m.ort}' if m.ort else ''}"))
    _zeige_hinweisliste(console, aktuell.hinweise)


@app.command("tracking-einrichten")
def tracking_einrichten() -> None:
    """Portal-Login (myHillebrandGori) einmalig im eigenen Chrome-Profil einrichten (sichtbarer Browser, Enter zum Fortfahren)."""
    from auftragsautomat.tracking.basis import lade_hillebrand_konfig
    from auftragsautomat.tracking.hillebrand_portal import PortalQuelle

    settings = _lade_settings()
    try:
        quelle = PortalQuelle(lade_hillebrand_konfig(settings.tracking.hillebrand_konfig), settings)
        if not quelle.verfuegbar():
            raise _fehler("Playwright/Chrome nicht verfügbar – 'pip install playwright' und 'playwright install chromium' bzw. Chrome installieren.")
        typer.echo(f"Chrome-Profil: {quelle.profil()}")
        angemeldet = quelle.einrichten()
    except typer.Exit:
        raise
    except Exception as exc:  # noqa: BLE001 - Browser/Playwright-Fehler klar melden
        logger.exception("Portal-Einrichtung fehlgeschlagen")
        raise _fehler(f"Einrichtung fehlgeschlagen: {type(exc).__name__}: {exc}") from exc
    if angemeldet:
        typer.echo("Portal eingerichtet: die Anmeldung ist im Automations-Profil gespeichert.")
    else:
        raise _fehler("Portal nicht eingerichtet: die Seite zeigt weiterhin die Anmeldung.")


@app.command("kontakte-pruefen")
def kontakte_pruefen() -> None:
    """Kontaktliste (Ansprechpartner Abwicklung) laden und zusammenfassen: Lieferanten, Speditionen mit Postfächern, Kunden."""
    from auftragsautomat.kontakte.liste import KontakteFehler, kontaktliste_aus_settings
    from auftragsautomat.kontakte.zuordnung import Zuordner

    settings = _lade_settings()
    backends = _backends_oder_fehler(settings, dry_run=True)
    try:
        liste = kontaktliste_aus_settings(settings, backends.excel)
    except KontakteFehler as exc:
        raise _fehler(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("Kontaktliste nicht lesbar")
        raise _fehler(f"Kontaktliste nicht lesbar: {type(exc).__name__}: {exc}") from exc
    zuordner = Zuordner(liste, None, settings)
    console = _console()
    console.print(f"Kontaktliste: {liste.quelle or '-'} – {len(liste.kontakte)} Kontakte")
    console.print(f"Lieferanten ({len(liste.lieferanten)}):")
    for firma, kontakte in liste.lieferanten.items():
        orders = [k for k in kontakte if k.position and "order" in k.position.lower()]
        console.print(f"  - {firma}: {len(kontakte)} Kontakte" + (f", Orders: {orders[0].email or '-'}" if orders else ", kein Orders-Kontakt"))
    console.print(f"Speditionen ({len(liste.speditionen)}):")
    for name, kontakte in liste.speditionen.items():
        neue = zuordner.postfach_spedition(name, "neue_auftraege")
        status = zuordner.postfach_spedition(name, "status")
        console.print(
            f"  - {name}: {len(kontakte)} Kontakte; Neue Aufträge: {neue.email if neue else '-'}; "
            f"Sendungsnachfragen: {status.email if status else '-'}"
        )
    console.print(f"Kunden ({len(liste.kunden)}):")
    for kunde, kontakte in liste.kunden.items():
        console.print(f"  - {kunde}: {len(kontakte)} Kontakte; Spedition: {zuordner.spedition_fuer_kunde(kunde)}")
    _zeige_hinweisliste(console, liste.hinweise)


# ---------------------------------------------------------------------------
# Ausbaustufe 2: Rabatte / Aktionen
# ---------------------------------------------------------------------------


def _aktionen_tabelle(aktionen, titel: str) -> Table:  # noqa: ANN001 - list[Aktion] (lazy import)
    tabelle = Table(title=titel)
    for spalte in ("ID", "Aktion", "Firma", "Kunde", "Lieferant", "Artikel", "Von", "Bis", "Rabatt %", "Rabatt €", "Aktionspreis", "Quelle"):
        tabelle.add_column(spalte)
    for a in aktionen:
        artikel = ", ".join(a.artikelnummern) or (a.artikel_text or "")
        tabelle.add_row(
            a.aktion_id, a.bezeichnung or "", a.firma_id or "alle", a.kunde_kurz or "alle", a.lieferant_kurz or "alle", artikel,
            a.von.strftime("%d.%m.%Y"), a.bis.strftime("%d.%m.%Y"),
            formatiere_menge(a.rabatt_prozent), formatiere_betrag(a.rabatt_eur), formatiere_betrag(a.aktionspreis), a.quelle,
        )
    return tabelle


@app.command("rabatte-einlesen")
def rabatte_einlesen(
    pdf: Annotated[Path, typer.Argument(help="Rabattlisten-PDF (Text-PDF; ohne Textebene Claude Vision)")],
    uebernehmen: Annotated[bool, typer.Option("--uebernehmen", help="Aktionen in das Gedächtnis (rabatte.gedaechtnis_datei) übernehmen.")] = False,
) -> None:
    """Rabattlisten-PDF einlesen und die erkannten Aktionen anzeigen; mit --uebernehmen in config/aktionen.yaml speichern."""
    from auftragsautomat.rabatte import RabattlisteFehler, leere_cache, lese_rabattliste, speichere_aktionen

    settings = _lade_settings()
    try:
        liste = lese_rabattliste(pdf, settings)
    except (FileNotFoundError, RabattlisteFehler) as exc:
        raise _fehler(str(exc)) from exc
    except _bekannte_fehler() as exc:
        raise _fehler(f"{exc}") from exc
    console = _console()
    console.print(_aktionen_tabelle(liste.aktionen, f"Rabattliste {pdf.name} ({liste.extraktionsmethode.value}, {len(liste.aktionen)} Aktionen)"))
    _zeige_hinweise(console, liste.hinweise)
    if not liste.aktionen:
        raise _fehler("Keine Aktionen erkannt – Datei prüfen (siehe Hinweise).")
    if uebernehmen:
        ziel = speichere_aktionen(liste.aktionen, Path(settings.rabatte.gedaechtnis_datei))
        leere_cache()
        console.print(f"{len(liste.aktionen)} Aktionen übernommen nach {ziel}", style="green")
    else:
        console.print("Nur angezeigt; mit --uebernehmen in das Gedächtnis speichern.")


@app.command()
def aktionen(
    firma: Annotated[str | None, typer.Option("--firma", help="Nur Aktionen dieser Firma (ID) oder für alle Firmen.")] = None,
    datum: Annotated[str | None, typer.Option("--datum", help="Stichtag TT.MM.JJJJ (Standard heute).")] = None,
    alle: Annotated[bool, typer.Option("--alle", help="Auch abgelaufene/künftige Aktionen anzeigen.")] = False,
) -> None:
    """Gültige Aktionen (Aktionszeiträume-Excel + Gedächtnis aus Rabattlisten) anzeigen."""
    from datetime import date

    from auftragsautomat.extraktion.zahlen import parse_datum
    from auftragsautomat.rabatte import alle_aktionen

    settings = _lade_settings()
    _firma_pruefen(settings, firma)
    tag = date.today()
    if datum:
        geparst = parse_datum(datum)
        if geparst is None:
            raise _fehler(f"'{datum}' ist kein Datum (erwartet TT.MM.JJJJ).")
        tag = geparst
    liste = alle_aktionen(settings, cache=False)
    kurz = settings.firma(firma).kurz if firma else None
    gefiltert = []
    for a in liste:
        if firma and a.firma_id not in (None, firma, kurz):
            continue
        if not alle and not a.gilt_am(tag):
            continue
        gefiltert.append(a)
    gefiltert.sort(key=lambda a: (a.von, a.bis, a.aktion_id))
    titel = f"Aktionen {'(alle)' if alle else f'gültig am {tag:%d.%m.%Y}'}" + (f" für {firma}" if firma else "") + f": {len(gefiltert)} von {len(liste)}"
    _console().print(_aktionen_tabelle(gefiltert, titel))
    if not liste:
        typer.echo("Keine Aktionsquellen: rabatte.aktionen_datei nicht konfiguriert und kein Gedächtnis (rabatte.gedaechtnis_datei).")


def main() -> None:  # pragma: no cover - Einstiegspunkt
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
