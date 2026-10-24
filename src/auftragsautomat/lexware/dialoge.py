"""Erkennung und Behandlung bekannter Lexware-Dialoge (Sicherheitsnetz).

Empfehlung aus der Recherche: Die Abfragen nach dem Speichern (Druck, E-Mail, eRechnung,
Poststelle, Internetmarke, Festschreiben) und die USt-IdNr.-Hinweise in allen vier Firmen unter
``Extras -> Optionen -> Auftragsbearbeitung / E-Mail-Versand`` abschalten. Dieses Modul fängt sie
trotzdem ab, falls sie doch erscheinen:

* "per E-Mail / Ausdruck"            -> "Überspringen" (nichts drucken, nichts mailen)
* "USt-IdNr. des Lieferanten prüfen?" -> "Nein"
* "Die Anwendung kann nur einmal gestartet werden" -> Fehler (nie eine zweite Instanz)
* "Änderungen verwerfen?"             -> "Ja" NUR im Kontext des Dry-Run-Abbruchs
* "Datei existiert bereits, ersetzen?" -> "Nein" (PDF-Ausgabe überschreibt nie)
* alles andere                        -> Screenshot + Fehler, NIE blind bestätigen

Die PDF-Ausgabe (Druckdialog, Dateinamen-Dialog des PDF-Druckers, Export-Dialog) bedient
``ui_automation.UiSchreiber`` selbst; ``behandle_dialoge`` wird dort erst aufgerufen, wenn diese
Dialoge geschlossen sind (Kontext ``PDF_AUSGABE``).

Die Regeln sind Daten (``BEKANNTE_DIALOGE``); Wortlaute sind großteils nicht belegt
(``# TODO-LEXWARE``) und müssen am System aufgenommen werden.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from auftragsautomat.lexware.ui_automation import Fenster, LexwareBackend

logger = logging.getLogger(__name__)

__all__ = [
    "BEKANNTE_DIALOGE",
    "DialogAktion",
    "DialogFehler",
    "DialogKontext",
    "DialogRegel",
    "LexwareDoppelstart",
    "UnbekannterDialog",
    "behandle_dialoge",
    "finde_regel",
]

MAX_RUNDEN = 6


class DialogFehler(Exception):
    """Ein Dialog verhindert den weiteren Ablauf; ``screenshot`` zeigt auf das Beweisbild."""

    def __init__(self, text: str, *, titel: str = "", dialogtext: str = "", screenshot: Path | None = None) -> None:
        super().__init__(text)
        self.titel = titel
        self.dialogtext = dialogtext
        self.screenshot = screenshot


class UnbekannterDialog(DialogFehler):
    """Dialog, für den es keine Regel gibt: wird nie blind bestätigt."""


class LexwareDoppelstart(DialogFehler):
    """'Die Anwendung kann nur einmal gestartet werden' – es läuft bereits ein Framework.exe."""


class DialogKontext(StrEnum):
    START = "start"  # beim Andocken/Starten von Lexware
    ASSISTENT = "assistent"  # während der Erfassung
    NACH_SPEICHERN = "nach_speichern"  # Druck-/E-Mail-/USt-IdNr.-Abfragen
    DRY_RUN_ABBRUCH = "dry_run_abbruch"  # Rückfrage nach "Abbrechen"
    PDF_AUSGABE = "pdf_ausgabe"  # nach der PDF-Ausgabe (Druckdialog/Dateidialog sind dann schon geschlossen)


class DialogAktion(StrEnum):
    KLICKEN = "klicken"  # Button (Control-Bezeichner) klicken
    FEHLER = "fehler"  # Abbruch mit Fehler und Screenshot


@dataclass(frozen=True)
class DialogRegel:
    name: str
    aktion: DialogAktion
    titel_regex: str | None = None
    text_regex: str | None = None
    control: str | None = None  # Bezeichner aus LexwareControls (z. B. "DIALOG_NEIN")
    nur_kontext: frozenset[DialogKontext] = field(default_factory=frozenset)  # leer = überall
    beschreibung: str = ""
    warnung: str | None = None  # wird ins Protokoll übernommen

    def passt(self, titel: str, text: str) -> bool:
        if self.titel_regex and re.search(self.titel_regex, titel or "", re.I | re.S):
            return True
        return bool(self.text_regex and re.search(self.text_regex, text or "", re.I | re.S))


# Reihenfolge ist wichtig: die erste passende Regel gewinnt. Alle Wortlaute: TODO-LEXWARE.
BEKANNTE_DIALOGE: list[DialogRegel] = [
    DialogRegel(
        name="einmalstart",
        aktion=DialogAktion.FEHLER,
        titel_regex=r"nur einmal gestartet",
        text_regex=r"nur\s+einmal\s+gestartet|bereits\s+(gestartet|geöffnet)",
        beschreibung="Lexware läuft bereits – nie eine zweite Instanz starten",
    ),
    DialogRegel(
        name="aenderungen_verwerfen",
        aktion=DialogAktion.KLICKEN,
        control="DIALOG_JA",
        text_regex=r"verwerfen|ohne\s+zu\s+speichern|wirklich\s+(abbrechen|verlassen)|Eingaben\s+gehen\s+verloren",
        nur_kontext=frozenset({DialogKontext.DRY_RUN_ABBRUCH}),
        beschreibung="Rückfrage nach 'Abbrechen' – nur im Dry-Run-Abbruch mit 'Ja' beantworten",
    ),
    DialogRegel(
        name="datei_ersetzen",
        aktion=DialogAktion.KLICKEN,
        control="DIALOG_NEIN",
        text_regex=r"existiert\s+bereits|already\s+exists|ersetzen\?|überschreiben\?|replace\s+it",
        beschreibung="Datei existiert bereits -> Nein (PDF-Ausgabe überschreibt nie; freier Name wird vorher gewählt)",
        warnung="Eine vorhandene Datei wurde nicht überschrieben (Abfrage mit 'Nein' beantwortet)",
    ),
    DialogRegel(
        name="druck_email",
        aktion=DialogAktion.KLICKEN,
        control="DIALOG_UEBERSPRINGEN",
        titel_regex=r"drucken|ausgabe|versand",
        text_regex=r"ausdruck|drucken|e-?mail|versenden|verschicken|frankier",
        nur_kontext=frozenset({DialogKontext.NACH_SPEICHERN, DialogKontext.ASSISTENT, DialogKontext.PDF_AUSGABE}),
        beschreibung="Druck-/E-Mail-Abfrage nach dem Speichern: nichts drucken, nichts mailen",
    ),
    DialogRegel(
        name="ust_idnr",
        aktion=DialogAktion.KLICKEN,
        control="DIALOG_NEIN",
        titel_regex=r"ust[-.\s]*id",
        text_regex=r"ust[-.\s]*id|umsatzsteuer[-\s]*id",
        beschreibung="USt-IdNr. prüfen? -> Nein (Geschäftsregel)",
    ),
    DialogRegel(
        name="erechnung_poststelle_internetmarke",
        aktion=DialogAktion.KLICKEN,
        control="DIALOG_NEIN",
        text_regex=r"e-?rechnung|poststelle|internetmarke|xrechnung|zugferd",
        nur_kontext=frozenset({DialogKontext.NACH_SPEICHERN}),
        beschreibung="Abfragen eRechnung/Poststelle/Internetmarke -> Nein",
    ),
    DialogRegel(
        name="festschreiben",
        aktion=DialogAktion.KLICKEN,
        control="DIALOG_NEIN",
        text_regex=r"festschreiben|festgeschrieben",
        nur_kontext=frozenset({DialogKontext.NACH_SPEICHERN}),
        beschreibung="Festschreiben? -> Nein (bleibt dem Anwender überlassen)",
        warnung="Beleg wurde nicht festgeschrieben (Abfrage mit 'Nein' beantwortet)",
    ),
    DialogRegel(
        name="auftrag_buchen",
        aktion=DialogAktion.KLICKEN,
        control="DIALOG_NEIN",
        text_regex=r"auftrag\s+(jetzt\s+)?buchen|gebucht\s+werden",
        nur_kontext=frozenset({DialogKontext.NACH_SPEICHERN}),
        beschreibung="Auftrag buchen? (nur mit Lexware buchhaltung) -> Nein",
        warnung="Auftrag wurde nicht in die Buchhaltung gebucht (Abfrage mit 'Nein' beantwortet)",
    ),
    DialogRegel(
        name="lieferstopp",
        aktion=DialogAktion.FEHLER,
        text_regex=r"lieferstopp|liefersperre",
        beschreibung="Lieferstopp am Kunden – Entscheidung des Anwenders",
    ),
    DialogRegel(
        name="kreditrahmen",
        aktion=DialogAktion.FEHLER,
        text_regex=r"kreditrahmen|kreditlimit",
        beschreibung="Kreditrahmen überschritten – Entscheidung des Anwenders",
    ),
    DialogRegel(
        name="datensatz_gesperrt",
        aktion=DialogAktion.FEHLER,
        text_regex=r"anderen\s+benutzer\s+bearbeitet|gesperrt",
        beschreibung="Datensatz wird von einem anderen Benutzer bearbeitet",
    ),
]


def finde_regel(titel: str, text: str, regeln: list[DialogRegel] | None = None) -> DialogRegel | None:
    for regel in regeln if regeln is not None else BEKANNTE_DIALOGE:
        if regel.passt(titel, text):
            return regel
    return None


def _dialogtext(backend: LexwareBackend, dialog: Fenster) -> str:
    try:
        return backend.lese_text(dialog, "DIALOG_TEXT") or ""
    except Exception as exc:  # noqa: BLE001
        logger.debug("Dialogtext von %s nicht lesbar: %s", dialog, exc)
        return ""


def _schluessel(dialog: Fenster, text: str) -> str:
    return f"{dialog.titel}\x00{text}"


def behandle_dialoge(
    backend: LexwareBackend,
    kontext: DialogKontext | str = DialogKontext.ASSISTENT,
    *,
    screenshot: Callable[[str], Path | None] | None = None,
    screenshots_ordner: Path | None = None,
    regeln: list[DialogRegel] | None = None,
    max_runden: int = MAX_RUNDEN,
) -> list[str]:
    """Behandelt alle offenen Dialoge laut Regeln; liefert das Protokoll (eine Zeile je Aktion).

    Unbekannte Dialoge und FEHLER-Regeln werfen ``UnbekannterDialog`` bzw. ``DialogFehler``
    (``LexwareDoppelstart`` für die Einmalstart-Meldung) mit Screenshot-Pfad. Ein Dialog, der sich
    nach dem Klick nicht schließt, wird nach ``max_runden`` als Fehler gemeldet.
    """
    kontext = DialogKontext(kontext)
    regeln = BEKANNTE_DIALOGE if regeln is None else regeln
    protokoll: list[str] = []
    gesehen: dict[str, int] = {}

    def bild(name: str) -> Path | None:
        try:
            if screenshot is not None:
                return screenshot(name)
            if screenshots_ordner is not None:
                pfad = Path(screenshots_ordner) / f"{datetime.now():%Y%m%d_%H%M%S}_{name}.png"
                return backend.screenshot(pfad)
        except Exception as exc:  # noqa: BLE001
            logger.error("Screenshot für Dialog fehlgeschlagen: %s", exc)
        return None

    for _ in range(max_runden):
        dialoge = backend.offene_dialoge()
        if not dialoge:
            return protokoll
        for dialog in dialoge:
            text = _dialogtext(backend, dialog)
            schluessel = _schluessel(dialog, text)
            gesehen[schluessel] = gesehen.get(schluessel, 0) + 1
            kurz = " ".join(text.split())[:160]
            regel = finde_regel(dialog.titel, text, regeln)
            if regel is None:
                pfad = bild("unbekannter_dialog")
                logger.error("Unbekannter Dialog %r: %r (Screenshot %s)", dialog.titel, kurz, pfad)
                raise UnbekannterDialog(
                    f"Unbekannter Dialog {dialog.titel!r}: {kurz!r} – nicht bestätigt, Abbruch",
                    titel=dialog.titel,
                    dialogtext=text,
                    screenshot=pfad,
                )
            if regel.aktion is DialogAktion.FEHLER:
                pfad = bild(f"dialog_{regel.name}")
                logger.error("Dialog %r (%s): %r -> Abbruch (Screenshot %s)", dialog.titel, regel.name, kurz, pfad)
                klasse = LexwareDoppelstart if regel.name == "einmalstart" else DialogFehler
                raise klasse(
                    f"Dialog {dialog.titel!r} ({regel.beschreibung}): {kurz!r}",
                    titel=dialog.titel,
                    dialogtext=text,
                    screenshot=pfad,
                )
            if regel.nur_kontext and kontext not in regel.nur_kontext:
                pfad = bild(f"dialog_{regel.name}_kontext")
                logger.error("Dialog %r (%s) im Kontext %s nicht erlaubt (Screenshot %s)", dialog.titel, regel.name, kontext, pfad)
                raise UnbekannterDialog(
                    f"Dialog {dialog.titel!r} ({regel.name}) ist im Kontext '{kontext}' nicht vorgesehen: {kurz!r}",
                    titel=dialog.titel,
                    dialogtext=text,
                    screenshot=pfad,
                )
            if gesehen[schluessel] > 2:
                pfad = bild(f"dialog_{regel.name}_bleibt")
                raise DialogFehler(
                    f"Dialog {dialog.titel!r} ({regel.name}) bleibt nach {gesehen[schluessel] - 1} Klicks auf "
                    f"{regel.control} bestehen",
                    titel=dialog.titel,
                    dialogtext=text,
                    screenshot=pfad,
                )
            assert regel.control is not None
            backend.klicke(dialog, regel.control)
            zeile = f"Dialog {dialog.titel!r} ({regel.name}) mit {backend.controls.titel(regel.control)!r} beantwortet"
            logger.info("%s: %r", zeile, kurz)
            protokoll.append(zeile)
            if regel.warnung:
                protokoll.append(regel.warnung)
    if backend.offene_dialoge():
        pfad = bild("dialoge_bleiben")
        raise DialogFehler(
            f"Nach {max_runden} Runden sind noch Dialoge offen: "
            + ", ".join(repr(d.titel) for d in backend.offene_dialoge()),
            screenshot=pfad,
        )
    return protokoll
