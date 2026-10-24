"""Nachbereitung einer Buchung: Fixtermin, E-Mail-Entwürfe, Dossier und Ablage (docs/AUSBAUSTUFE_2.md).

:func:`nachbereiten` wird von :func:`auftragsautomat.lexware.buchen.buche` nach einer erfolgreichen
Buchung aufgerufen (Dry Run und scharf) und von der CLI bzw. der Freigabe-UI einzeln angesteuert.
Die Schritte sind voneinander unabhängig; jeder Fehler wird als Hinweis festgehalten, der Lauf geht
weiter und das Buchungsergebnis bleibt unverändert gültig:

1. **Backends** (:func:`erzeuge_backends_fuer`): Backend ``fake`` oder Dry Run -> Fake-Backends mit
   den ``logs``-Ordnern (``kommunikation.entwuerfe_ordner`` für ``.eml``-Entwürfe,
   ``dossier.lokaler_ordner`` für Dossiers). Ist für den Dry Run ein echtes Backend (``graph``/``lokal``)
   konfiguriert, wird die Excel nur **gelesen** (:class:`VorschauExcelBackend`), jede Schreibaktion landet
   im Protokoll und in der Vorschau-CSV ``<log_ordner>/fixtermine_vorschau.csv``; Entwürfe und Dossiers
   bleiben lokal. Scharf -> :func:`auftragsautomat.m365.fabrik.erzeuge_backends`.
2. **Kurznamen** (``termine.kurznamen``) aus ``config/aliase/kurznamen.yaml`` plus Stammdaten-Matchcodes,
   angeglichen an die Dropdown-Ressourcenliste der Fixterminüberwachung (nur lesen).
3. **Fixtermin** (:func:`fixtermin_eintragen`): nur mit ``roh.liefertag``; Dry Run -> Vorschau-CSV, kein
   Schreiben; scharf -> :meth:`FixterminRegister.trage_ein`.
4. **Kontaktliste** (:func:`lade_kontakte`): ``settings.m365.kontaktliste_datei``; fehlt sie, entstehen die
   Entwürfe ohne Empfänger (Hinweis).
5. **Entwürfe** (:func:`entwuerfe_erstellen`): :class:`Entwurfsfabrik.fuer_buchung` (die Prüfung der
   Auftragskopie auf verbotene Wörter greift dort) und Speichern über das Mail-Backend (nie senden).
6. **Dossier** (:func:`dossier_anlegen`): Quelldatei = ``paket.roh.quelldatei``; Lexware-PDFs aus dem
   Buchungsergebnis, übergeben oder im PDF-/Eingangsordner nach Hausmuster gefunden
   (:func:`finde_lexware_pdfs`, Namen laut Titulierung).
7. **Ablage** (:func:`ablegen`): :func:`auftragsautomat.ablage.lege_ab` – nur im scharfen Lauf wirksam.

Das Ergebnis (:class:`Nachbereitung`) wird als ``<name>.nachbereitung.json`` neben dem Paket im
Paketordner (``freigabe.zustand.paket_ordner``) gespeichert, damit CLI und Freigabe-UI es später anzeigen
und einzelne Schritte (Ablage, Dossier) wiederholen können.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from auftragsautomat import benennung
from auftragsautomat.config import DateiRefKonfig, Settings
from auftragsautomat.freigabe import zustand
from auftragsautomat.m365.basis import (
    ExcelBackend,
    M365Fehler,
    Mail,
    MailBackend,
    MailKopf,
    Zellbereich,
    datei_schluessel,
    parse_adresse,
    spaltenbuchstabe,
)
from auftragsautomat.m365.fabrik import Backends, erzeuge_backends
from auftragsautomat.m365.fake import FakeDateiBackend, FakeExcelBackend, FakeMailBackend, Schreibzugriff
from auftragsautomat.models import (
    AblageErgebnis,
    Belegpaket,
    BuchungsErgebnis,
    Dossier,
    EmailEntwurf,
    Fixtermin,
)

logger = logging.getLogger(__name__)

__all__ = [
    "KUERZEL_LEXWARE_PDFS",
    "NACHBEREITUNG_ENDUNG",
    "VORSCHAU_CSV",
    "LeseSchreibMailBackend",
    "Nachbereitung",
    "VorschauExcelBackend",
    "ablegen",
    "backends_fuer_ueberwachung",
    "dossier_anlegen",
    "dossier_verwaltung",
    "entwuerfe_erstellen",
    "erzeuge_backends_fuer",
    "finde_lexware_pdfs",
    "fixtermin_eintragen",
    "ist_konfiguriert",
    "lade_kontakte",
    "lade_kurznamen",
    "lade_nachbereitung",
    "nachbereiten",
    "nachbereitung_pfad",
    "speichere_nachbereitung",
    "weitere_weingueter",
    "zusammenfassung_nachbereitung",
]

#: Endung der Ergebnisdatei neben dem Paket (``<stamm>.nachbereitung.json``).
NACHBEREITUNG_ENDUNG = ".nachbereitung.json"
#: Vorschau der Excel-Änderungen im Dry Run (relativ zu ``settings.log_ordner``).
VORSCHAU_CSV = "fixtermine_vorschau.csv"
#: Dokumentkürzel der Lexware-PDFs, die Entwürfe und Dossier erwarten (Schlüssel in ``BuchungsErgebnis.pdfs``).
KUERZEL_LEXWARE_PDFS: tuple[str, ...] = ("Cde", "Speditionsauftrag", "Kopie", "Ls")
#: Name des gemischten Backends (lesen echt, schreiben als Vorschau).
BACKEND_VORSCHAU = "vorschau"
_DATUM_MARKE = date(2000, 1, 1)


# ---------------------------------------------------------------------------
# Ergebnis
# ---------------------------------------------------------------------------


class Nachbereitung(BaseModel):
    """Ergebnis der Nachbereitung einer Buchung (wird als JSON neben dem Paket gespeichert)."""

    fixtermin: Fixtermin | None = None
    weitere_fixtermine: list[Fixtermin] = Field(default_factory=list)  # je weiterem Weingut (Einkauf 2..n)
    dossier: Dossier | None = None
    entwuerfe: list[EmailEntwurf] = Field(default_factory=list)
    hinweise: list[str] = Field(default_factory=list)
    ablage: list[AblageErgebnis] = Field(default_factory=list)
    vorschau_csv: Path | None = None  # Dry Run: Vorschau der Excel-Änderungen
    dry_run: bool = True
    zeitpunkt: datetime | None = None
    ergebnis: BuchungsErgebnis | None = None  # Kopie des Buchungsergebnisses (für ablegen/dossier per CLI)
    speicherort: Path | None = None  # Pfad der JSON-Datei

    @property
    def kurzfassung(self) -> str:
        """Einzeiler für Protokoll, CLI und UI."""
        teile: list[str] = []
        if self.fixtermin is not None:
            text = f"Fixtermin {self.fixtermin.liefertag:%d.%m.%Y}"
            if self.fixtermin.zeile is not None:
                text += f" (Zeile {self.fixtermin.zeile}{', Vorschau' if self.dry_run else ''})"
            else:
                text += " (nicht eingetragen)"
            teile.append(text)
        else:
            teile.append("kein Fixtermin")
        gespeichert = sum(1 for e in self.entwuerfe if e.speicherort)
        teile.append(f"{len(self.entwuerfe)} Entwürfe ({gespeichert} gespeichert)")
        teile.append(f"Dossier {self.dossier.name}" if self.dossier is not None else "kein Dossier")
        if self.ablage:
            abgelegt = sum(1 for a in self.ablage if a.abgelegt)
            if all(a.dry_run for a in self.ablage):
                teile.append(f"Ablage {len(self.ablage)} Dateien nur protokolliert")
            else:
                teile.append(f"Ablage {abgelegt}/{len(self.ablage)} Dateien")
        if self.hinweise:
            teile.append(f"{len(self.hinweise)} Hinweise")
        return "; ".join(teile)


@dataclass
class _Lauf:
    """Zwischenstand eines Nachbereitungslaufs."""

    hinweise: list[str] = field(default_factory=list)

    def hinweis(self, text: str, *, fehler: bool = False) -> None:
        if text not in self.hinweise:
            self.hinweise.append(text)
        (logger.error if fehler else logger.info)("Nachbereitung: %s", text)


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------


def ist_konfiguriert(ref: DateiRefKonfig) -> bool:
    """``True``, wenn die Datei-Referenz auf etwas zeigt (``web_url``, IDs oder ``lokaler_pfad``)."""
    return bool(ref.web_url or (ref.drive_id and ref.item_id) or ref.lokaler_pfad is not None)


class VorschauExcelBackend:
    """Excel-Backend für den Dry Run: liest über ein echtes Backend, schreibt **nur ins Protokoll**.

    Jeder ``schreibe_bereich``-Aufruf wird wie beim :class:`FakeExcelBackend` als
    :class:`Schreibzugriff` festgehalten (``schreibprotokoll``) und kann mit :meth:`vorschau_csv`
    exportiert werden; die Datei in OneDrive/SharePoint bleibt unverändert.
    """

    def __init__(self, lesen: ExcelBackend, *, jetzt: Callable[[], datetime] | None = None) -> None:
        self.lesen = lesen
        self.schreibprotokoll: list[Schreibzugriff] = []
        self._jetzt = jetzt or (lambda: datetime.now().astimezone())

    def blattnamen(self, datei: DateiRefKonfig) -> list[str]:
        return self.lesen.blattnamen(datei)

    def lese_bereich(self, datei: DateiRefKonfig, blatt: str, adresse: str) -> list[list[Any]]:
        return self.lesen.lese_bereich(datei, blatt, adresse)

    def lese_benutzten_bereich(self, datei: DateiRefKonfig, blatt: str) -> tuple[str, list[list[Any]]]:
        return self.lesen.lese_benutzten_bereich(datei, blatt)

    def schreibe_bereich(
        self,
        datei: DateiRefKonfig,
        blatt: str,
        adresse: str,
        werte: list[list[Any | None]],
        *,
        zahlenformat: list[list[str | None]] | None = None,
    ) -> None:
        bereich: Zellbereich = parse_adresse(adresse)
        zugriff = Schreibzugriff(self._jetzt(), datei_schluessel(datei), blatt, bereich.adresse, [list(z) for z in werte], zahlenformat)
        for zi, zeile in enumerate(werte):
            for si, wert in enumerate(zeile):
                if wert is None:
                    continue
                fmt = zahlenformat[zi][si] if zahlenformat else None
                zugriff.zellen.append((f"{spaltenbuchstabe(bereich.spalte_von + si)}{bereich.zeile_von + zi}", wert, fmt))
        self.schreibprotokoll.append(zugriff)
        logger.info("Dry Run: Excel %s!%s nicht geschrieben (%d Zellen nur protokolliert)", blatt, bereich.adresse, len(zugriff.zellen))

    def vorschau_csv(self, pfad: Path, *, anhaengen: bool = False) -> Path:
        """Schreibt die protokollierten Zelländerungen als CSV (Format wie :meth:`FakeExcelBackend.vorschau_csv`)."""
        fake = FakeExcelBackend()
        fake.schreibprotokoll = self.schreibprotokoll
        return fake.vorschau_csv(pfad, anhaengen=anhaengen)


class LeseSchreibMailBackend:
    """Mail-Backend, das den Posteingang über ``lesen`` liest und Entwürfe über ``schreiben`` anlegt.

    Für den Überwachungslauf ohne ``--scharf``: echte Mails lesen (Graph/Outlook), Entwürfe aber nur
    als ``.eml`` über das Fake-Backend speichern.
    """

    def __init__(self, lesen: MailBackend, schreiben: MailBackend) -> None:
        self.lesen = lesen
        self.schreiben = schreiben

    def erstelle_entwurf(self, entwurf: EmailEntwurf) -> str:
        return self.schreiben.erstelle_entwurf(entwurf)

    def suche_mails(
        self,
        *,
        seit: datetime,
        absender: str | None = None,
        betreff_enthaelt: str | None = None,
        ordner: str | None = None,
        maximal: int = 50,
    ) -> list[MailKopf]:
        return self.lesen.suche_mails(seit=seit, absender=absender, betreff_enthaelt=betreff_enthaelt, ordner=ordner, maximal=maximal)

    def lese_mail(self, mail_id: str) -> Mail:
        return self.lesen.lese_mail(mail_id)

    def speichere_anhang(self, mail_id: str, anhang_id: str, ziel: Path) -> Path:
        return self.lesen.speichere_anhang(mail_id, anhang_id, ziel)

    @property
    def entwuerfe(self) -> list[EmailEntwurf]:
        """Gespeicherte Entwürfe des Schreib-Backends (Fake), falls es sie führt."""
        return list(getattr(self.schreiben, "entwuerfe", []))


def _fake_backends(settings: Settings) -> Backends:
    return Backends(
        excel=FakeExcelBackend(),
        mail=FakeMailBackend(Path(settings.kommunikation.entwuerfe_ordner)),
        datei=FakeDateiBackend(Path(settings.dossier.lokaler_ordner)),
        backend="fake",
    )


def erzeuge_backends_fuer(settings: Settings, *, dry_run: bool, hinweise: list[str] | None = None) -> Backends:
    """Backends für Nachbereitung/CLI.

    * Backend ``fake`` -> Fake-Backends mit den ``logs``-Ordnern (immer).
    * Dry Run mit ``graph``/``lokal`` -> Excel nur lesen (:class:`VorschauExcelBackend`), Entwürfe als
      ``.eml`` und Dossiers lokal (Fake). Lässt sich das echte Backend nicht erzeugen, komplett Fake (Hinweis).
    * scharf -> :func:`auftragsautomat.m365.fabrik.erzeuge_backends`.
    """
    if settings.m365.backend == "fake":
        return _fake_backends(settings)
    if not dry_run:
        return erzeuge_backends(settings)
    try:
        echt = erzeuge_backends(settings)
    except Exception as exc:  # noqa: BLE001 - z. B. unbekanntes Backend, fehlende Bibliothek
        text = f"Backend '{settings.m365.backend}' für den Dry Run nicht verfügbar ({type(exc).__name__}: {exc}); es werden Fake-Backends verwendet"
        logger.warning("%s", text)
        if hinweise is not None:
            hinweise.append(text)
        return _fake_backends(settings)
    fake = _fake_backends(settings)
    # TODO-PRUEFEN: Lesender Graph-Zugriff im Dry Run braucht ein gültiges Token (auftragsautomat m365-anmelden);
    # ohne Anmeldung melden die Schritte Fixtermin/Kontaktliste einen Hinweis und laufen ohne Excel weiter.
    return Backends(excel=VorschauExcelBackend(echt.excel), mail=fake.mail, datei=fake.datei, backend=BACKEND_VORSCHAU, anmeldung=echt.anmeldung)


def backends_fuer_ueberwachung(settings: Settings, *, scharf: bool = False, nur_lesen: bool = False) -> Backends:
    """Backends für den Überwachungslauf (CLI ``ueberwachen``, UI "Jetzt prüfen").

    ``nur_lesen`` -> ausschließlich Fake-Backends (kein Zugriff auf Graph/Outlook/Excel). ``scharf`` ->
    echte Backends. Sonst: Mails und Excel echt **lesen**, Entwürfe über das Fake-Backend als ``.eml``
    speichern (:class:`LeseSchreibMailBackend`), Excel-Schreibversuche nur protokollieren.
    """
    if nur_lesen or settings.m365.backend == "fake":
        return _fake_backends(settings)
    echt = erzeuge_backends(settings)
    if scharf:
        return echt
    # TODO-PRUEFEN: Am echten Postfach prüfen, dass der Dry Run des Überwachungslaufs die Mails über Graph nur liest
    # (suche_mails/lese_mail) und die Entwürfe als .eml unter kommunikation.entwuerfe_ordner landen, nicht im Postfach.
    fake = _fake_backends(settings)
    return Backends(
        excel=VorschauExcelBackend(echt.excel),
        mail=LeseSchreibMailBackend(echt.mail, fake.mail),
        datei=echt.datei,
        backend=BACKEND_VORSCHAU,
        anmeldung=echt.anmeldung,
    )


# ---------------------------------------------------------------------------
# Schritte
# ---------------------------------------------------------------------------


def lade_kurznamen(settings: Settings, excel: ExcelBackend | None = None, hinweise: list[str] | None = None) -> Any:
    """Kurznamen (``termine.kurznamen.Kurznamen``) laut ``kurznamen.yaml``, angeglichen an die
    Dropdown-Ressourcenliste der Fixterminüberwachung (nur lesen). ``None`` nur, wenn die YAML kaputt ist."""
    from auftragsautomat.termine.kurznamen import Kurznamen, KurznamenFehler, vorhandene_kurznamen

    try:
        kurznamen = Kurznamen.lade(settings=settings)
    except KurznamenFehler as exc:
        text = f"Kurznamen-Datei nicht lesbar ({exc}); es gelten Matchcodes und Namen"
        logger.warning("%s", text)
        if hinweise is not None:
            hinweise.append(text)
        return None
    ref = settings.m365.fixtermin_datei
    if excel is not None and ist_konfiguriert(ref):
        try:
            kurznamen.mit_vorhandenen(vorhandene_kurznamen(excel, ref))
        except Exception as exc:  # noqa: BLE001 - Ressourcenliste ist optional
            logger.info("Ressourcenliste der Fixterminüberwachung nicht lesbar (%s: %s)", type(exc).__name__, exc)
    return kurznamen


def fixtermin_eintragen(
    paket: Belegpaket,
    ergebnis: BuchungsErgebnis | None,
    settings: Settings,
    *,
    backends: Backends,
    dry_run: bool,
    kurznamen: Any | None = None,
    hinweise: list[str] | None = None,
    heute: date | None = None,
    weitere: list[Fixtermin] | None = None,
) -> tuple[Fixtermin | None, Path | None]:
    """Fixtermin aus dem Paket bilden und in die Fixterminüberwachung eintragen.

    Liefert ``(fixtermin, vorschau_csv)``: ohne Liefertag ``(None, None)``; im Dry Run wird nichts in
    die Excel geschrieben, die geplanten Zelländerungen stehen in der Vorschau-CSV. Scheitert das
    Eintragen (Datei nicht konfiguriert/lesbar, Tag ohne Zeile, alle Slots belegt), bleibt der
    Fixtermin ohne ``zeile`` erhalten und der Grund steht in ``hinweise``.

    Hat die Buchung weitere Einkauf-Bestellungen (``ergebnis.einkauf_weitere``, mehrere Weingüter), bekommt
    jedes weitere Weingut einen eigenen Eintrag mit seinem Kurznamen und der Belegnummer seiner Bestellung
    (Nachfrage, Häkchen und Zuordnung seiner Bestätigung); diese Einträge werden an ``weitere`` angehängt.
    """
    from auftragsautomat.termine.register import FixterminRegister, RegisterFehler, fixtermin_aus_paket

    lauf = _Lauf(hinweise if hinweise is not None else [])
    if paket.roh.liefertag is None:
        lauf.hinweis(f"Kein Liefertag in {Path(paket.roh.quelldatei).name}: kein Eintrag in der Fixterminüberwachung")
        return None, None
    try:
        fixtermin = fixtermin_aus_paket(paket, ergebnis, kurznamen, heute=heute)
    except RegisterFehler as exc:
        lauf.hinweis(f"Fixtermin nicht bestimmbar: {exc}")
        return None, None
    ref = settings.m365.fixtermin_datei
    if not ist_konfiguriert(ref) and not isinstance(backends.excel, FakeExcelBackend):
        lauf.hinweis("Fixterminüberwachung nicht konfiguriert (m365.fixtermin_datei): Fixtermin nicht eingetragen")
        return fixtermin, None
    register = FixterminRegister(backends.excel, ref, bundesland=settings.ueberwachung.bundesland)
    try:
        eingetragen = register.trage_ein(fixtermin)
    except (RegisterFehler, M365Fehler, ValueError) as exc:
        lauf.hinweis(f"Fixtermin {fixtermin.liefertag:%d.%m.%Y} nicht eingetragen: {exc}", fehler=not dry_run)
        return fixtermin, None
    except Exception as exc:  # noqa: BLE001 - COM-/Netzwerkfehler: Buchung bleibt gültig
        logger.exception("Fixtermin konnte nicht eingetragen werden")
        lauf.hinweis(f"Fixtermin {fixtermin.liefertag:%d.%m.%Y} nicht eingetragen ({type(exc).__name__}: {exc})", fehler=True)
        return fixtermin, None
    for teil in _weitere_fixtermine(paket, ergebnis, settings, kurznamen, heute, lauf):
        try:
            teil_eingetragen = register.trage_ein(teil)
        except Exception as exc:  # noqa: BLE001 - wie beim Hauptweingut: Buchung bleibt gültig, Grund im Hinweis
            logger.warning("Fixtermin des weiteren Weinguts %s nicht eingetragen: %s", teil.lieferant_kurz, exc)
            lauf.hinweis(
                f"Fixtermin {teil.liefertag:%d.%m.%Y} des weiteren Weinguts {teil.lieferant_kurz} nicht eingetragen "
                f"({type(exc).__name__}: {exc})",
                fehler=not dry_run,
            )
            continue
        lauf.hinweis(
            f"Weiteres Weingut: Fixtermin {teil_eingetragen.liefertag:%d.%m.%Y} ({teil_eingetragen.lieferant_kurz} / "
            f"{teil_eingetragen.kunde_kurz}) in Zeile {teil_eingetragen.zeile}" + (" (nur Vorschau)" if dry_run else "")
        )
        if weitere is not None:
            weitere.append(teil_eingetragen)
    vorschau: Path | None = None
    protokoll = getattr(backends.excel, "schreibprotokoll", None)
    if dry_run and protokoll is not None:
        try:
            # Anhängen: jede Dry-Run-Buchung behält ihre Vorschau (sonst überschreibt die nächste Buchung sie)
            vorschau = backends.excel.vorschau_csv(Path(settings.log_ordner) / VORSCHAU_CSV, anhaengen=True)  # type: ignore[union-attr]
        except OSError as exc:
            lauf.hinweis(f"Vorschau-CSV nicht schreibbar: {exc}")
        lauf.hinweis(
            f"Dry Run: Fixtermin {eingetragen.liefertag:%d.%m.%Y} ({eingetragen.lieferant_kurz} / {eingetragen.kunde_kurz}) "
            f"nur als Vorschau in Zeile {eingetragen.zeile}; nichts in der Excel geändert"
            + (f" – Vorschau: {vorschau}" if vorschau else "")
        )
    else:
        lauf.hinweis(
            f"Fixtermin {eingetragen.liefertag:%d.%m.%Y} ({eingetragen.lieferant_kurz} / {eingetragen.kunde_kurz}) "
            f"in Zeile {eingetragen.zeile} der Fixterminüberwachung eingetragen"
        )
    return eingetragen, vorschau


def _weitere_fixtermine(
    paket: Belegpaket,
    ergebnis: BuchungsErgebnis | None,
    settings: Settings,
    kurznamen: Any | None,
    heute: date | None,
    lauf: _Lauf,
) -> list[Fixtermin]:
    """Fixtermine der weiteren Weingüter (Teilpakete 2..n mit ihrer Einkauf-Bestellung, AB wie beim Hauptweingut)."""
    from auftragsautomat.termine.register import RegisterFehler, fixtermin_aus_paket

    if ergebnis is None or not ergebnis.einkauf_weitere:
        return []
    fixtermine: list[Fixtermin] = []
    for i, teil in enumerate(weitere_weingueter(paket, ergebnis, settings, hinweise=lauf.hinweise)):
        einkauf = ergebnis.einkauf_weitere[i] if i < len(ergebnis.einkauf_weitere) else None
        teil_ergebnis = ergebnis.model_copy(update={"einkauf": einkauf, "einkauf_weitere": [], "angebot": None})
        try:
            fixtermine.append(fixtermin_aus_paket(teil, teil_ergebnis, kurznamen, heute=heute))
        except RegisterFehler as exc:
            lauf.hinweis(f"Fixtermin des weiteren Weinguts nicht bestimmbar: {exc}")
    return fixtermine


def lade_kontakte(
    settings: Settings,
    backends: Backends,
    *,
    stammdaten: Mapping[str, Any] | None = None,
    hinweise: list[str] | None = None,
) -> Any | None:
    """Kontaktliste laden und als ``kontakte.Zuordner`` liefern; ``None`` mit Hinweis, wenn sie fehlt."""
    from auftragsautomat.kontakte.liste import KontakteFehler, kontaktliste_aus_settings
    from auftragsautomat.kontakte.zuordnung import Zuordner

    lauf = _Lauf(hinweise if hinweise is not None else [])
    if not ist_konfiguriert(settings.m365.kontaktliste_datei):
        lauf.hinweis("Kontaktliste nicht konfiguriert (m365.kontaktliste_datei): Entwürfe ohne Empfänger")
        return None
    try:
        liste = kontaktliste_aus_settings(settings, backends.excel)
    except (KontakteFehler, M365Fehler) as exc:
        lauf.hinweis(f"Kontaktliste nicht lesbar ({exc}): Entwürfe ohne Empfänger")
        return None
    except Exception as exc:  # noqa: BLE001 - z. B. NichtAufWindows beim COM-Backend
        logger.exception("Kontaktliste konnte nicht geladen werden")
        lauf.hinweis(f"Kontaktliste nicht lesbar ({type(exc).__name__}: {exc}): Entwürfe ohne Empfänger")
        return None
    if stammdaten is None:
        from auftragsautomat.stammdaten.laden import lade_alle

        try:
            stammdaten = lade_alle(settings)
        except Exception as exc:  # noqa: BLE001 - Stammdaten sind für die Kontakte optional
            logger.warning("Stammdaten für die Kontaktzuordnung nicht geladen (%s: %s)", type(exc).__name__, exc)
            stammdaten = None
    return Zuordner(liste, stammdaten, settings)


def _vorhandene_pdfs(ergebnis: BuchungsErgebnis | None, zusatz: Mapping[str, Path] | None) -> dict[str, Path]:
    pdfs: dict[str, Path] = {}
    if ergebnis is not None:
        for kuerzel, pfad in ergebnis.pdfs.items():
            if Path(pfad).is_file():
                pdfs[kuerzel] = Path(pfad)
    for kuerzel, pfad in (zusatz or {}).items():
        if Path(pfad).is_file():
            pdfs[kuerzel] = Path(pfad)
    return pdfs


def finde_lexware_pdfs(
    paket: Belegpaket,
    settings: Settings,
    *,
    datum: date | None = None,
    ordner: Iterable[Path] | None = None,
    kuerzel: Iterable[str] = KUERZEL_LEXWARE_PDFS,
    nicht_vor: datetime | None = None,
) -> dict[str, Path]:
    """Sucht die Lexware-PDFs eines Pakets nach Hausmuster (Titulierung) in ``ordner``.

    Standardordner: ``lexware.pdf_ausgabe_ordner``, der Ordner der Quelldatei und ``eingang_ordner``
    (rekursiv). Verglichen wird der Name laut :func:`benennung.name_fuer_paket` **ohne** den Datumsteil
    (die PDF kann an einem anderen Tag erzeugt worden sein); bei mehreren Treffern gewinnt die
    jüngste Datei. ``datum`` bestimmt nichts am Vergleich, nur Pakete ohne Firma liefern ``{}``.

    Gesucht wird nur, wenn der Name den Auftrag eindeutig kennzeichnet (Bestell- bzw. Auftragsnummer im
    Namen): ohne Nummer (z. B. WS ``Lieferant – Kunde – Kürzel – Datum`` oder ``ohne-Nr``) passt das
    Präfix auf jede ältere Datei desselben Kunden und Lieferanten. ``nicht_vor`` (z. B.
    ``ergebnis.zeitpunkt``) lässt nur Dateien zu, die danach geschrieben wurden.
    """
    if not paket.firma_id or paket.firma_id not in settings.firmen:
        return {}
    ohne_nummer = paket.model_copy(deep=True)
    ohne_nummer.roh.bestellnummer = None
    ohne_nummer.roh.belegnummer = None
    grenze = (nicht_vor - timedelta(seconds=2)).timestamp() if nicht_vor is not None else None
    suchordner: list[Path] = []
    if ordner is not None:
        suchordner = [Path(o) for o in ordner]
    else:
        suchordner = [Path(settings.lexware.pdf_ausgabe_ordner), Path(paket.roh.quelldatei).parent, Path(settings.eingang_ordner)]
    dateien: list[Path] = []
    gesehen: set[Path] = set()
    for o in suchordner:
        if not o.is_dir():
            continue
        try:
            kandidaten = o.rglob("*") if o == Path(settings.eingang_ordner) else o.iterdir()
            for p in kandidaten:
                if p.is_file() and p.suffix.lower() == ".pdf" and p.resolve() not in gesehen:
                    gesehen.add(p.resolve())
                    dateien.append(p)
        except OSError as exc:
            logger.debug("Ordner %s nicht lesbar: %s", o, exc)
    if not dateien:
        return {}
    trenner = settings.benennung.trenner
    marke = benennung.datum_text(_DATUM_MARKE, settings)
    gefunden: dict[str, Path] = {}
    for k in kuerzel:
        try:
            muster = benennung.name_fuer_paket(k, paket, settings, datum=_DATUM_MARKE, endung="")
        except (ValueError, KeyError) as exc:
            logger.debug("Kein Hausmuster für %s: %s", k, exc)
            continue
        try:
            eindeutig = muster != benennung.name_fuer_paket(k, ohne_nummer, settings, datum=_DATUM_MARKE, endung="")
        except (ValueError, KeyError):
            eindeutig = False
        if not eindeutig:
            logger.info("Lexware-PDF %s: Name '%s' enthält keine Auftragsnummer – keine Suche nach Hausmuster", k, muster)
            continue
        if muster.endswith(trenner + marke):
            praefix = muster[: -len(trenner + marke)] + trenner
        else:
            praefix = muster
        treffer = [p for p in dateien if p.stem.startswith(praefix) and (grenze is None or p.stat().st_mtime >= grenze)]
        if treffer:
            bestes = max(treffer, key=lambda p: p.stat().st_mtime)
            gefunden[k] = bestes
            logger.info("Lexware-PDF %s nach Hausmuster gefunden: %s", k, bestes)
    return gefunden


def weitere_weingueter(
    paket: Belegpaket,
    ergebnis: BuchungsErgebnis | None,
    settings: Settings,
    *,
    stammdaten: Any | None = None,
    hinweise: list[str] | None = None,
) -> list[Belegpaket]:
    """Teilpakete der weiteren Weingüter (ohne Hauptweingut) wie in :func:`lexware.buchen.einkaufs_teilpakete`.

    Nur, wenn die Buchung weitere Einkauf-Bestellungen angelegt hat (``ergebnis.einkauf_weitere``) bzw. PDFs
    "Cde (2)" … existieren; die Reihenfolge entspricht der Buchung. Stammdaten werden bei Bedarf geladen;
    sind sie nicht verfügbar, gibt es nur einen Hinweis (die weiteren Weingüter bekommen dann keinen Entwurf).
    """
    if not paket.einkauf_erforderlich or not paket.firma_id or paket.firma_id not in settings.firmen:
        return []
    erwartet = len(ergebnis.einkauf_weitere) if ergebnis is not None else 0
    if ergebnis is not None and not erwartet:
        erwartet = sum(1 for k in ergebnis.pdfs if k.startswith("Cde ("))
    if not erwartet:
        return []
    from auftragsautomat.lexware.buchen import einkaufs_teilpakete

    lauf = _Lauf(hinweise if hinweise is not None else [])
    if stammdaten is None:
        from auftragsautomat.stammdaten.laden import lade_stammdaten

        try:
            stammdaten = lade_stammdaten(settings.firma(paket.firma_id))
        except Exception as exc:  # noqa: BLE001 - ohne Stamm keine Aufteilung, nur Hinweis
            lauf.hinweis(f"Weitere Weingüter: Stammdaten nicht ladbar ({type(exc).__name__}: {exc}) – nur Entwürfe für das Hauptweingut")
            return []
    teile = einkaufs_teilpakete(paket, stammdaten)
    weitere = [teil for teil, _ in teile[1:]]
    if len(weitere) != erwartet:
        lauf.hinweis(f"Weitere Weingüter: {len(weitere)} Teilpakete, aber {erwartet} weitere Einkauf-Bestellungen gebucht – bitte Entwürfe prüfen")
    return weitere


def entwuerfe_erstellen(
    paket: Belegpaket,
    ergebnis: BuchungsErgebnis | None,
    settings: Settings,
    *,
    backends: Backends,
    fixtermin: Fixtermin | None,
    kontakte: Any | None,
    lexware_pdfs: Mapping[str, Path] | None = None,
    hinweise: list[str] | None = None,
    speichern: bool = True,
) -> list[EmailEntwurf]:
    """E-Mail-Entwürfe zur Buchung erzeugen und (``speichern``) über das Mail-Backend anlegen – nie senden."""
    from auftragsautomat.kommunikation.entwuerfe import Entwurfsfabrik
    from auftragsautomat.kommunikation.vorlagen import VorlagenFehler

    lauf = _Lauf(hinweise if hinweise is not None else [])
    if not paket.firma_id or paket.firma_id not in settings.firmen:
        lauf.hinweis("Keine Entwürfe: dem Paket ist keine bekannte Firma zugeordnet")
        return []
    firma = settings.firma(paket.firma_id)
    # Weinstrasse Agentur: das Angebot wird mit dem EN-Formular als Cde ans Weingut gedruckt (Abschnitt 5.2),
    # deshalb bekommt das Weingut auch ohne Einkauf-Bestellung eine Bestell-Mail mit diesem Anhang.
    # TODO-PRUEFEN: Mit dem Anwender bestätigen, dass die Agentur die "new order"-Mail ans Weingut selbst schickt
    # (sonst bestellung_ohne_einkauf=False lassen).
    bestellung_ohne_einkauf = not paket.einkauf_erforderlich and bool(firma.formulare.angebot_en)
    weitere = weitere_weingueter(paket, ergebnis, settings, hinweise=lauf.hinweise)
    try:
        fabrik = Entwurfsfabrik(settings, kontakte, firma, bestellung_ohne_einkauf=bestellung_ohne_einkauf)
        entwuerfe = fabrik.fuer_buchung(paket, ergebnis, fixtermin, lexware_pdfs, weitere_weingueter=weitere)
    except VorlagenFehler as exc:
        lauf.hinweis(f"Keine Entwürfe: {exc}", fehler=True)
        return []
    except Exception as exc:  # noqa: BLE001 - Vorlagenfehler, Kontaktfehler: Buchung bleibt gültig
        logger.exception("Entwürfe konnten nicht erzeugt werden")
        lauf.hinweis(f"Keine Entwürfe ({type(exc).__name__}: {exc})", fehler=True)
        return []
    for h in fabrik.hinweise:
        lauf.hinweis(h)
    if speichern and entwuerfe:
        try:
            fabrik.speichere(entwuerfe, backends.mail)
        except Exception as exc:  # noqa: BLE001 - speichere fängt je Entwurf; hier nur das Backend selbst
            logger.exception("Entwürfe konnten nicht gespeichert werden")
            lauf.hinweis(f"Entwürfe nicht gespeichert ({type(exc).__name__}: {exc})", fehler=True)
    for e in entwuerfe:
        for h in e.hinweise:
            lauf.hinweis(f"{e.typ.value}: {h}")
    if entwuerfe:
        lauf.hinweis(
            f"{len(entwuerfe)} Entwürfe erzeugt ({', '.join(e.typ.value for e in entwuerfe)})"
            + (" – als Entwurf gespeichert, nicht gesendet" if speichern else "")
        )
    return entwuerfe


def dossier_verwaltung(settings: Settings, *, dry_run: bool, backends: Backends | None = None) -> Any:
    """``DossierVerwaltung`` für den Modus: Dry Run/Fake -> lokaler Ordner ``dossier.lokaler_ordner``;
    scharf -> ``DossierVerwaltung.aus_settings`` mit dem Datei-Backend (OneDrive "vorangefertigte Dossiers")."""
    from auftragsautomat.dossier.erzeugung import DossierVerwaltung

    datei = backends.datei if backends is not None else None
    if dry_run or settings.m365.backend == "fake" or isinstance(datei, FakeDateiBackend):
        lokal = Path(settings.dossier.lokaler_ordner)
        return DossierVerwaltung(FakeDateiBackend(lokal), DateiRefKonfig(lokaler_pfad=lokal), settings)
    return DossierVerwaltung.aus_settings(settings, datei_backend=datei)


def dossier_anlegen(
    paket: Belegpaket,
    ergebnis: BuchungsErgebnis | None,
    settings: Settings,
    *,
    backends: Backends,
    dry_run: bool,
    entwuerfe: Iterable[EmailEntwurf] = (),
    fixtermin: Fixtermin | None = None,
    lexware_pdfs: Mapping[str, Path] | None = None,
    hinweise: list[str] | None = None,
) -> Dossier | None:
    """Dossier im Ordner "vorangefertigte Dossiers" anlegen bzw. fortschreiben (Dry Run: ``dossier.lokaler_ordner``).

    Kundenbestellung (Kd): nach dem scharfen Lauf liegt die Quelldatei im Archiv (``ergebnis.archiv``) –
    diese Kopie hat Vorrang vor dem (dann leeren) Eingangspfad, wie bei der Ablage.
    """
    lauf = _Lauf(hinweise if hinweise is not None else [])
    quelldatei = Path(paket.roh.quelldatei)
    if ergebnis is not None and ergebnis.archiv is not None and Path(ergebnis.archiv).is_file():
        quelldatei = Path(ergebnis.archiv)
    try:
        verwaltung = dossier_verwaltung(settings, dry_run=dry_run, backends=backends)
        dossier = verwaltung.erstelle_oder_aktualisiere(
            paket,
            ergebnis,
            quelldatei=quelldatei,
            lexware_pdfs=dict(lexware_pdfs or {}),
            entwuerfe=list(entwuerfe),
            fixtermin=fixtermin,
        )
    except Exception as exc:  # noqa: BLE001 - Dossier ist Zusatz; Buchung bleibt gültig
        logger.exception("Dossier konnte nicht angelegt werden")
        lauf.hinweis(f"Dossier nicht angelegt ({type(exc).__name__}: {exc})", fehler=True)
        return None
    lauf.hinweis(f"Dossier {dossier.name}: {len(dossier.dateien)} Dateien, {len(dossier.offene_punkte)} offene Punkte ({dossier.ordner})")
    return dossier


def ablegen(
    paket: Belegpaket,
    ergebnis: BuchungsErgebnis,
    settings: Settings,
    *,
    backends: Backends,
    dry_run: bool,
    hinweise: list[str] | None = None,
) -> list[AblageErgebnis]:
    """Fertige Dateien in den OneDrive-Ordner "Aufträge" ablegen (:func:`ablage.lege_ab`; nur scharf wirksam)."""
    from auftragsautomat.ablage import lege_ab

    lauf = _Lauf(hinweise if hinweise is not None else [])
    try:
        ergebnisse = lege_ab(paket, ergebnis, settings, datei_backend=backends.datei, dry_run=dry_run)
    except Exception as exc:  # noqa: BLE001 - Konfigurationsfehler der Ablage
        logger.exception("Ablage fehlgeschlagen")
        lauf.hinweis(f"Ablage nicht möglich ({type(exc).__name__}: {exc})", fehler=True)
        return []
    if not ergebnisse:
        lauf.hinweis("Ablage: keine Dateien")
    elif all(a.dry_run for a in ergebnisse):
        ziel = ergebnisse[0].zielordner or "(Wurzel)"
        lauf.hinweis(f"Ablage nur protokolliert ({len(ergebnisse)} Dateien nach '{ziel}'; Dry Run bzw. ablage.aktiv aus)")
    else:
        abgelegt = [a for a in ergebnisse if a.abgelegt]
        lauf.hinweis(f"Ablage: {len(abgelegt)} von {len(ergebnisse)} Dateien nach '{ergebnisse[0].zielordner}' abgelegt")
        for a in ergebnisse:
            if a.fehler:
                lauf.hinweis(f"Ablage {a.kuerzel} ({a.zielname}): {a.fehler}", fehler=True)
    return ergebnisse


# ---------------------------------------------------------------------------
# Gesamtlauf
# ---------------------------------------------------------------------------


def nachbereiten(
    paket: Belegpaket,
    ergebnis: BuchungsErgebnis,
    settings: Settings,
    *,
    backends: Backends | None = None,
    lexware_pdfs: dict[str, Path] | None = None,
    speichern: bool = True,
) -> Nachbereitung:
    """Nachbereitung einer Buchung (siehe Modul-Docstring); wirft keine fachlichen Exceptions.

    ``ergebnis.dry_run`` bestimmt den Modus. ``backends`` ersetzt die Backends laut Konfiguration
    (Tests); ``lexware_pdfs`` ergänzt ``ergebnis.pdfs`` (nur vorhandene Dateien zählen, fehlende
    werden nach Hausmuster gesucht). Mit ``speichern`` landet das Ergebnis als
    ``<name>.nachbereitung.json`` im Paketordner.
    """
    dry_run = bool(ergebnis.dry_run)
    lauf = _Lauf()
    nb = Nachbereitung(dry_run=dry_run, zeitpunkt=ergebnis.zeitpunkt)
    logger.info("Nachbereitung (%s) für %s", "Dry Run" if dry_run else "scharf", Path(paket.roh.quelldatei).name)
    if not paket.firma_id or paket.firma_id not in settings.firmen:
        lauf.hinweis("Nachbereitung übersprungen: dem Paket ist keine bekannte Firma zugeordnet", fehler=True)
        nb.hinweise = lauf.hinweise
        return nb
    if backends is None:
        backends = erzeuge_backends_fuer(settings, dry_run=dry_run, hinweise=lauf.hinweise)

    kurznamen = lade_kurznamen(settings, backends.excel, lauf.hinweise)
    nb.fixtermin, nb.vorschau_csv = fixtermin_eintragen(
        paket, ergebnis, settings, backends=backends, dry_run=dry_run, kurznamen=kurznamen, hinweise=lauf.hinweise,
        weitere=nb.weitere_fixtermine,
    )
    kontakte = lade_kontakte(settings, backends, hinweise=lauf.hinweise)

    pdfs = _vorhandene_pdfs(ergebnis, lexware_pdfs)
    fehlend = [k for k in KUERZEL_LEXWARE_PDFS if k not in pdfs]
    if fehlend:
        for k, pfad in finde_lexware_pdfs(paket, settings, kuerzel=fehlend, nicht_vor=ergebnis.zeitpunkt).items():
            pdfs[k] = pfad
            lauf.hinweis(f"PDF {k} nach Hausmuster gefunden: {pfad}")

    nb.entwuerfe = entwuerfe_erstellen(
        paket, ergebnis, settings, backends=backends, fixtermin=nb.fixtermin, kontakte=kontakte,
        lexware_pdfs=pdfs, hinweise=lauf.hinweise,
    )
    nb.dossier = dossier_anlegen(
        paket, ergebnis, settings, backends=backends, dry_run=dry_run, entwuerfe=nb.entwuerfe,
        fixtermin=nb.fixtermin, lexware_pdfs=pdfs, hinweise=lauf.hinweise,
    )
    nb.ablage = ablegen(paket, ergebnis, settings, backends=backends, dry_run=dry_run, hinweise=lauf.hinweise)

    nb.hinweise = lauf.hinweise
    nb.ergebnis = ergebnis.model_copy(deep=True)
    if speichern:
        try:
            nb.speicherort = speichere_nachbereitung(nb, paket, settings)
        except OSError as exc:
            logger.exception("Nachbereitung konnte nicht gespeichert werden")
            nb.hinweise.append(f"Nachbereitung nicht gespeichert: {exc}")
    logger.info("Nachbereitung fertig: %s", nb.kurzfassung)
    return nb


# ---------------------------------------------------------------------------
# Speichern / Laden / Anzeige
# ---------------------------------------------------------------------------


def nachbereitung_pfad(paket: Belegpaket | Path, settings: Settings, *, ordner: Path | None = None) -> Path:
    """``<Paketordner>/<stamm>.nachbereitung.json`` zu einem Paket oder einer ``*.paket.json``-Datei."""
    ordner = Path(ordner) if ordner is not None else zustand.paket_ordner(settings)
    if isinstance(paket, Belegpaket):
        name = zustand.paket_dateiname(paket)
    else:
        name = Path(paket).name
    stamm = name[: -len(zustand.PAKET_ENDUNG)] if name.endswith(zustand.PAKET_ENDUNG) else Path(name).stem
    return ordner / f"{stamm}{NACHBEREITUNG_ENDUNG}"


def speichere_nachbereitung(nb: Nachbereitung, paket: Belegpaket | Path, settings: Settings, *, ordner: Path | None = None) -> Path:
    """Schreibt die Nachbereitung atomar als JSON neben das Paket und liefert den Pfad."""
    ziel = nachbereitung_pfad(paket, settings, ordner=ordner)
    ziel.parent.mkdir(parents=True, exist_ok=True)
    kopie = nb.model_copy(update={"speicherort": ziel})
    temp = ziel.with_name(f"{ziel.name}.{os.getpid()}.tmp")
    try:
        temp.write_text(kopie.model_dump_json(indent=2), encoding="utf-8")
        os.replace(temp, ziel)
    finally:
        if temp.exists():
            temp.unlink(missing_ok=True)
    logger.info("Nachbereitung gespeichert: %s", ziel)
    return ziel


def lade_nachbereitung(paket: Belegpaket | Path, settings: Settings, *, ordner: Path | None = None) -> Nachbereitung | None:
    """Liest die gespeicherte Nachbereitung zu einem Paket; ``None``, wenn es keine (lesbare) gibt."""
    pfad = nachbereitung_pfad(paket, settings, ordner=ordner)
    if not pfad.is_file():
        return None
    try:
        nb = Nachbereitung.model_validate_json(pfad.read_text(encoding="utf-8"))
    except (OSError, ValidationError, ValueError) as exc:
        logger.warning("Nachbereitung %s nicht lesbar: %s", pfad, exc)
        return None
    nb.speicherort = pfad
    return nb


def zusammenfassung_nachbereitung(nb: Nachbereitung) -> str:
    """Mehrzeiliger Text für CLI und UI."""
    zeilen = [f"Nachbereitung ({'Dry Run' if nb.dry_run else 'scharf'}): {nb.kurzfassung}"]
    if nb.fixtermin is not None:
        ft = nb.fixtermin
        zeilen.append(
            f"  Fixtermin: {ft.liefertag:%d.%m.%Y} | {ft.lieferant_kurz} | {ft.kunde_kurz} | Auftrag {ft.auftragsdatum:%d.%m.%Y}"
            if ft.auftragsdatum
            else f"  Fixtermin: {ft.liefertag:%d.%m.%Y} | {ft.lieferant_kurz} | {ft.kunde_kurz}"
        )
        if ft.bemerkung:
            zeilen.append(f"    Bemerkung: {ft.bemerkung}")
        if ft.zeile is not None:
            zeilen.append(f"    Zeile {ft.zeile}" + (" (Vorschau, nicht geschrieben)" if nb.dry_run else ""))
    if nb.vorschau_csv is not None:
        zeilen.append(f"  Vorschau-CSV: {nb.vorschau_csv}")
    if nb.entwuerfe:
        zeilen.append("  Entwürfe (nicht gesendet):")
        for e in nb.entwuerfe:
            an = ", ".join(e.an) or "(kein Empfänger)"
            zeilen.append(f"    - {e.typ.value}: '{e.betreff}' an {an}" + (f" -> {e.speicherort}" if e.speicherort else " (nicht gespeichert)"))
            for h in e.hinweise:
                zeilen.append(f"        Hinweis: {h}")
    if nb.dossier is not None:
        zeilen.append(f"  Dossier: {nb.dossier.name} ({nb.dossier.ordner})")
        for punkt in nb.dossier.offene_punkte:
            zeilen.append(f"    offen: {punkt}")
    if nb.ablage:
        zeilen.append("  Ablage:")
        for a in nb.ablage:
            status = "nur protokolliert" if a.dry_run else ("abgelegt" if a.abgelegt else f"NICHT abgelegt: {a.fehler}")
            zeilen.append(f"    - {a.kuerzel}: {a.zielordner}/{a.zielname} ({status})")
    if nb.hinweise:
        zeilen.append("  Hinweise:")
        zeilen.extend(f"    - {h}" for h in nb.hinweise)
    return "\n".join(zeilen)
