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
* ``beispiel-pdfs [ORDNER]``                – Beispiel-PDFs erzeugen (scripts/beispiel_pdfs_erzeugen.py)
* ``version``

Alle Meldungen sind deutsch; Fehler führen zu Exit-Code ≠ 0. ``--home`` (oder die Umgebungs-
variable ``AUFTRAGSAUTOMAT_HOME``) wählt den Projektstamm mit ``config/``.
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
    console.print(zusammenfassung(paket, ergebnis))
    console.print(f"Paket aktualisiert: {ziel}")
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
    console.print(zusammenfassung(paket, ergebnis))
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


def main() -> None:  # pragma: no cover - Einstiegspunkt
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
