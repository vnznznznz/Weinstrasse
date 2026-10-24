"""Lokales Backend: klassisches Outlook und Excel per COM auf dem Windows-PC (``m365.backend: lokal``).

Grundlage: ``docs/recherche/excel_lokal_und_vorlagen.md`` (Teile A.2, B.1) und
``graph_excel_outlook.md`` (Abschnitt 5). Alles hier ist am echten System noch nicht ausgeführt;
COM-Member sind aus dem VBA-Objektmodell übernommen (pywin32 bildet sie 1:1 ab).

* :class:`OutlookComMailBackend` – Entwurf per ``CreateItem(0)``: To/CC/Subject setzen, Anhänge,
  ``Display(False)`` (Outlook fügt die Standardsignatur des Kontos ein), Vorlagentext direkt nach
  ``<body…>`` einfügen, ``Save()`` (neues Element -> Ordner "Entwürfe"), ``Close(0)`` (olSave),
  Rückgabe ``EntryID``. **Es wird nie gesendet.** Posteingang über ``Namespace("MAPI")``,
  ``GetDefaultFolder(6)`` und ``Items.Restrict`` mit DASL-Filter (``urn:schemas:httpmail:datereceived``).

  **Warnung Objektmodellschutz:** Ein per ``Dispatch`` erzeugtes Outlook gilt als *nicht
  vertrauenswürdig*. Das **Lesen** von ``To``, ``CC``, ``Body``, ``HTMLBody``, ``SenderEmailAddress``,
  ``Recipients`` löst die Adressbuch-Sicherheitsabfrage aus ("Ein Programm versucht, auf
  E-Mail-Adressinformationen zuzugreifen …"), sofern Windows-Sicherheitscenter kein aktuelles
  Antivirus meldet oder das Trust Center strenger steht; "Verweigern" ergibt einen COM-Fehler.
  ``Subject``, ``ReceivedTime``, ``EntryID``, ``Attachments.Add``/``SaveAsFile``, ``Save`` sind nicht
  geschützt. Deshalb: ``suche_mails`` liest nur ungeschützte Felder, wenn
  ``geschuetzte_felder_lesen=False``; ``lese_mail`` braucht die geschützten Felder zwangsläufig.
  Für den unbeaufsichtigten Tagelauf den Posteingang per Graph lesen. Das **neue Outlook** hat kein
  COM-Objektmodell – dann bleibt nur Graph bzw. ``.eml`` (``fake.py``).

* :class:`ExcelComBackend` – Excel speichert selbst (Kontrollkästchen, dynamische Arrays, x14-
  Erweiterungen bleiben erhalten; **nie openpyxl zum Speichern**). Datei muss lokal synchronisiert
  vorliegen (grüner Haken). Vor dem Schreiben ``Workbook.ReadOnly`` prüfen -> :class:`M365Konflikt`;
  ``RPC_E_CALL_REJECTED`` (Anwender tippt gerade / Dialog offen) wird mit Wartezeit wiederholt;
  Zellen mit Formel werden nie überschrieben; nach dem Schreiben ``Save()`` nur, wenn
  ``AutoSaveOn`` aus ist (bei Cloud-Dateien mischt AutoSpeichern die Änderung ins Co-Authoring).
  xlwings wird bevorzugt (hängt sich an die offene Mappe), sonst ``win32com``.

* :class:`LokalerDateiBackend` – Dateien über ``DateiRefKonfig.lokaler_pfad`` (OneDrive-Sync-Ordner);
  identisch zum Fake-Dateibackend.

Die Logik ist mit Fake-COM-Objekten testbar: ``dispatch`` (ProgID -> COM-Objekt) ist injizierbar;
ohne Injektion wird außerhalb von Windows :class:`NichtAufWindows` geworfen.
"""

from __future__ import annotations

import logging
import re
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from auftragsautomat.config import DateiRefKonfig
from auftragsautomat.m365.basis import (
    M365Fehler,
    M365Konflikt,
    M365NichtGefunden,
    Mail,
    MailAnhang,
    MailKopf,
    Zellbereich,
    parse_adresse,
)
from auftragsautomat.m365.fake import FakeDateiBackend
from auftragsautomat.models import EmailEntwurf

__all__ = [
    "ExcelComBackend",
    "LokalerDateiBackend",
    "NichtAufWindows",
    "OL_FOLDER_INBOX",
    "OL_FORMAT_HTML",
    "OL_MAIL_ITEM",
    "OL_SAVE",
    "OutlookComMailBackend",
    "RPC_E_CALL_REJECTED",
    "dasl_filter",
    "fuege_vor_signatur_ein",
    "html_rumpf",
    "ist_rpc_abgelehnt",
    "mit_wiederholung",
]

logger = logging.getLogger(__name__)

RPC_E_CALL_REJECTED = -2147418111  # 0x80010001 "Call was rejected by callee"
OL_MAIL_ITEM = 0  # OlItemType.olMailItem
OL_FOLDER_INBOX = 6  # OlDefaultFolders.olFolderInbox
OL_SAVE = 0  # OlInspectorClose.olSave
OL_FORMAT_HTML = 2  # OlBodyFormat.olFormatHTML
OL_CLASS_MAIL = 43  # OlObjectClass.olMail

Dispatch = Callable[[str], Any]


class NichtAufWindows(M365Fehler):
    """COM-Backend wurde außerhalb von Windows angefordert."""


def _standard_dispatch(prog_id: str) -> Any:
    if sys.platform != "win32":
        raise NichtAufWindows(
            f"Das lokale COM-Backend ({prog_id}) läuft nur unter Windows (hier: {sys.platform}). "
            "Bitte m365.backend auf 'graph' oder 'fake' stellen."
        )
    import pythoncom  # type: ignore[import-not-found]  # pragma: no cover - nur Windows
    import win32com.client  # type: ignore[import-not-found]  # pragma: no cover - nur Windows

    pythoncom.CoInitialize()  # pragma: no cover
    return win32com.client.Dispatch(prog_id)  # pragma: no cover


def ist_rpc_abgelehnt(exc: BaseException) -> bool:
    """``pywintypes.com_error`` mit HRESULT ``RPC_E_CALL_REJECTED`` (Excel/Outlook gerade beschäftigt)."""
    hresult = getattr(exc, "hresult", None)
    if hresult is None and exc.args:
        hresult = exc.args[0]
    return hresult == RPC_E_CALL_REJECTED


def mit_wiederholung(
    aktion: Callable[[], Any],
    *,
    versuche: int = 10,
    wartezeit: float = 1.5,
    schlaf: Callable[[float], Any] = time.sleep,
    was: str = "COM-Aufruf",
) -> Any:
    """Führt ``aktion`` aus und wiederholt bei ``RPC_E_CALL_REJECTED`` bis zu ``versuche`` Mal."""
    for versuch in range(1, max(1, versuche) + 1):
        try:
            return aktion()
        except Exception as exc:
            if not ist_rpc_abgelehnt(exc) or versuch >= versuche:
                raise
            logger.info("%s abgelehnt (Office beschäftigt) – Versuch %s/%s nach %.1f s", was, versuch + 1, versuche, wartezeit)
            schlaf(wartezeit)
    raise AssertionError("unerreichbar")


# ---------------------------------------------------------------------------
# HTML-Hilfen für den Outlook-Entwurf
# ---------------------------------------------------------------------------

_BODY_START_RE = re.compile(r"<body\b[^>]*>", re.IGNORECASE)
_BODY_ENDE_RE = re.compile(r"</body\s*>", re.IGNORECASE)


def html_rumpf(html: str) -> str:
    """Inhalt zwischen ``<body…>`` und ``</body>`` (sonst der ganze Text)."""
    start = _BODY_START_RE.search(html)
    if not start:
        return html.strip()
    ende = _BODY_ENDE_RE.search(html, start.end())
    return html[start.end() : ende.start() if ende else len(html)].strip()


def fuege_vor_signatur_ein(signatur_html: str, text_html: str) -> str:
    """Setzt den Vorlagentext direkt hinter ``<body…>`` des von Outlook erzeugten HTML (Signaturblock
    bleibt erhalten). Ohne ``<body>`` wird der Text vorangestellt (Ron-de-Bruin-Variante)."""
    rumpf = html_rumpf(text_html)
    treffer = _BODY_START_RE.search(signatur_html or "")
    if not treffer:
        return rumpf + "<br>" + (signatur_html or "")
    return signatur_html[: treffer.end()] + rumpf + signatur_html[treffer.end() :]


def _dasl_text(wert: str) -> str:
    return wert.replace("'", "''")


def dasl_filter(seit: datetime, *, absender: str | None = None, betreff_enthaelt: str | None = None) -> str:
    """DASL-Filter für ``Items.Restrict`` (Datum in UTC laut Outlook-Doku; Teilstringsuche im Betreff
    per ``ci_phrasematch``, weil Jet-Syntax kein "contains" kennt)."""
    if seit.tzinfo is None:
        seit = seit.astimezone()
    seit_utc = seit.astimezone(UTC)
    # TODO-PRUEFEN: Datumsformat des DASL-Vergleichs am PC des Anwenders prüfen ("MM/DD/YYYY HH:MM" in UTC
    # laut Outlook-Doku "Filtering Items Using a Date-time Comparison"); sonst Jet-Syntax "[ReceivedTime] >= '…'".
    teile = [f"\"urn:schemas:httpmail:datereceived\" >= '{seit_utc:%m/%d/%Y %H:%M}'"]
    if betreff_enthaelt:
        teile.append(f"\"urn:schemas:httpmail:subject\" ci_phrasematch '{_dasl_text(betreff_enthaelt)}'")
    if absender:
        # TODO-PRUEFEN: "urn:schemas:httpmail:fromemail" liefert bei Exchange-Absendern ggf. die X500-Adresse.
        teile.append(f"\"urn:schemas:httpmail:fromemail\" = '{_dasl_text(absender)}'")
    return "@SQL=" + " AND ".join(teile)


def _zu_datetime(wert: Any) -> datetime:
    """COM-Zeitwert (pywintypes.datetime) -> aware ``datetime`` in lokaler Zeit."""
    if isinstance(wert, datetime):
        ergebnis = datetime(wert.year, wert.month, wert.day, wert.hour, wert.minute, wert.second, tzinfo=wert.tzinfo)
    elif isinstance(wert, date):
        ergebnis = datetime(wert.year, wert.month, wert.day)
    else:
        try:
            ergebnis = datetime.fromisoformat(str(wert))
        except ValueError:
            ergebnis = datetime.now()
    return ergebnis.astimezone() if ergebnis.tzinfo is None else ergebnis


def _adressliste(text: Any) -> list[str]:
    return [t.strip() for t in str(text or "").replace(",", ";").split(";") if t.strip()]


# ---------------------------------------------------------------------------
# Outlook
# ---------------------------------------------------------------------------


class OutlookComMailBackend:
    """Entwürfe im klassischen Outlook anlegen, Posteingang lesen (siehe Modul-Docstring)."""

    def __init__(self, *, dispatch: Dispatch | None = None, geschuetzte_felder_lesen: bool = True) -> None:
        self._dispatch: Dispatch = dispatch or _standard_dispatch
        self.geschuetzte_felder_lesen = geschuetzte_felder_lesen
        self._outlook: Any = None

    def _anwendung(self) -> Any:
        if self._outlook is None:
            self._outlook = self._dispatch("Outlook.Application")
        return self._outlook

    def _namespace(self) -> Any:
        return self._anwendung().GetNamespace("MAPI")

    # -- Entwurf -----------------------------------------------------------------

    def erstelle_entwurf(self, entwurf: EmailEntwurf) -> str:
        fehlend = [str(p) for p in entwurf.anhaenge if not Path(p).is_file()]
        if fehlend:
            raise M365Fehler(f"Anhang nicht gefunden: {', '.join(fehlend)}")
        mail = self._anwendung().CreateItem(OL_MAIL_ITEM)
        mail.To = "; ".join(entwurf.an)
        mail.CC = "; ".join(entwurf.cc)
        mail.Subject = entwurf.betreff
        mail.BodyFormat = OL_FORMAT_HTML
        for pfad in entwurf.anhaenge:
            mail.Attachments.Add(str(Path(pfad).resolve()))
        # Display(False): nicht-modal anzeigen, damit Outlook die Standardsignatur des Kontos einfügt
        # (Ron de Bruin / Slipstick, [V-Dritt]); der Anwender soll den Entwurf ohnehin prüfen.
        # TODO-PRUEFEN: Variante ohne Anzeige (nur GetInspector) am PC testen; bei mehreren Konten
        # SendUsingAccount und konto-spezifische Signatur prüfen (Checkliste Teil B, Punkte 10/11).
        mail.Display(False)
        signatur_html = str(mail.HTMLBody or "")
        mail.HTMLBody = fuege_vor_signatur_ein(signatur_html, entwurf.text_html)
        mail.Save()
        mail.Close(OL_SAVE)
        entry_id = str(mail.EntryID or "")
        entwurf.speicherort = entry_id
        entwurf.erstellt_am = entwurf.erstellt_am or datetime.now().astimezone()
        logger.info("Outlook-Entwurf '%s' gespeichert (%s Anhänge)", entwurf.betreff, len(entwurf.anhaenge))
        return entry_id

    # -- Posteingang --------------------------------------------------------------

    def _ordner(self, ordner: str | None) -> Any:
        posteingang = self._namespace().GetDefaultFolder(OL_FOLDER_INBOX)
        if not ordner or ordner.lower() in ("inbox", "posteingang"):
            return posteingang
        try:
            return posteingang.Folders(ordner)
        except Exception as exc:
            raise M365NichtGefunden(f"Outlook-Ordner '{ordner}' unterhalb des Posteingangs nicht gefunden.") from exc

    def _absender(self, item: Any) -> tuple[str, str | None]:
        if not self.geschuetzte_felder_lesen:
            return "", None
        return str(item.SenderEmailAddress or ""), (str(item.SenderName) if getattr(item, "SenderName", None) else None)

    def _kopf(self, item: Any, ordner: str | None) -> MailKopf:
        adresse, name = self._absender(item)
        return MailKopf(
            id=str(item.EntryID),
            betreff=str(item.Subject or ""),
            absender=adresse,
            absender_name=name,
            empfangen=_zu_datetime(item.ReceivedTime),
            hat_anhaenge=int(item.Attachments.Count) > 0,
            ordner=ordner or "inbox",
        )

    def suche_mails(
        self,
        *,
        seit: datetime,
        absender: str | None = None,
        betreff_enthaelt: str | None = None,
        ordner: str | None = None,
        maximal: int = 50,
    ) -> list[MailKopf]:
        items = self._ordner(ordner).Items
        items.Sort("[ReceivedTime]", True)
        gefiltert = items.Restrict(dasl_filter(seit, absender=absender, betreff_enthaelt=betreff_enthaelt))
        ergebnis: list[MailKopf] = []
        for item in gefiltert:
            if getattr(item, "Class", OL_CLASS_MAIL) != OL_CLASS_MAIL:
                continue
            ergebnis.append(self._kopf(item, ordner))
            if len(ergebnis) >= maximal:
                break
        return ergebnis

    def lese_mail(self, mail_id: str) -> Mail:
        try:
            item = self._namespace().GetItemFromID(mail_id)
        except Exception as exc:
            raise M365NichtGefunden(f"Outlook-Element {mail_id!r} nicht gefunden.") from exc
        anhaenge: list[MailAnhang] = []
        for index in range(1, int(item.Attachments.Count) + 1):
            att = item.Attachments.Item(index)
            anhaenge.append(MailAnhang(id=str(index), name=str(att.FileName), groesse=getattr(att, "Size", None)))
        return Mail(
            kopf=self._kopf(item, None),
            text=str(item.Body or "") or None,
            html=str(item.HTMLBody or "") or None,
            an=_adressliste(item.To),
            cc=_adressliste(item.CC),
            anhaenge=anhaenge,
        )

    def speichere_anhang(self, mail_id: str, anhang_id: str, ziel: Path) -> Path:
        item = self._namespace().GetItemFromID(mail_id)
        try:
            att = item.Attachments.Item(int(anhang_id))
        except Exception as exc:  # ValueError (keine Zahl) oder COM-Fehler (Index ungültig)
            raise M365NichtGefunden(f"Anhang {anhang_id!r} in Nachricht {mail_id!r} nicht gefunden.") from exc
        ziel = Path(ziel)
        if ziel.is_dir():
            ziel = ziel / str(att.FileName)
        ziel.parent.mkdir(parents=True, exist_ok=True)
        att.SaveAsFile(str(ziel.resolve()))
        return ziel


# ---------------------------------------------------------------------------
# Excel
# ---------------------------------------------------------------------------


def _com_wert(wert: Any) -> Any:
    if isinstance(wert, datetime):
        return wert.replace(tzinfo=None) if wert.tzinfo else wert
    if isinstance(wert, date):
        return datetime(wert.year, wert.month, wert.day)
    if isinstance(wert, Decimal):
        return int(wert) if wert == wert.to_integral_value() else float(wert)
    return wert


def _python_wert(wert: Any) -> Any:
    if wert is None:
        return ""
    if isinstance(wert, datetime):
        return datetime(wert.year, wert.month, wert.day, wert.hour, wert.minute, wert.second)
    if isinstance(wert, float) and wert.is_integer():
        return int(wert)
    return wert


def _matrix(wert: Any, bereich: Zellbereich) -> list[list[Any]]:
    """``Range.Value`` (Skalar bei einer Zelle, sonst Tupel von Tupeln) -> Matrix mit ``""`` für leer."""
    if bereich.zeilen == 1 and bereich.spalten == 1 and not isinstance(wert, list | tuple):
        return [[_python_wert(wert)]]
    zeilen = list(wert or [])
    ergebnis: list[list[Any]] = []
    for zi in range(bereich.zeilen):
        zeile = list(zeilen[zi]) if zi < len(zeilen) and isinstance(zeilen[zi], list | tuple) else []
        ergebnis.append([_python_wert(zeile[si]) if si < len(zeile) else "" for si in range(bereich.spalten)])
    return ergebnis


class ExcelComBackend:
    """Excel-Desktop per xlwings/COM; Excel speichert, Datei muss lokal vorliegen (siehe Modul-Docstring)."""

    def __init__(
        self,
        *,
        dispatch: Dispatch | None = None,
        xlwings_nutzen: bool | None = None,
        versuche: int = 10,
        wartezeit: float = 1.5,
        schlaf: Callable[[float], Any] = time.sleep,
    ) -> None:
        self._dispatch: Dispatch = dispatch or _standard_dispatch
        self._xlwings_nutzen = xlwings_nutzen
        self.versuche = versuche
        self.wartezeit = wartezeit
        self._schlaf = schlaf
        self._offen: dict[str, tuple[Any, Callable[[], None]]] = {}

    # -- Öffnen / Schließen ----------------------------------------------------------

    @staticmethod
    def _pfad(datei: DateiRefKonfig) -> Path:
        if datei.lokaler_pfad is None:
            raise M365Fehler(
                "Für das lokale Excel-Backend braucht die Datei einen 'lokaler_pfad' (OneDrive-synchronisierter Ordner)."
            )
        pfad = Path(datei.lokaler_pfad)
        if not pfad.is_file():
            raise M365NichtGefunden(
                f"Excel-Datei {pfad} liegt nicht lokal vor (OneDrive: 'Immer auf diesem Gerät behalten' wählen)."
            )
        return pfad

    def _xlwings(self) -> Any | None:
        if self._xlwings_nutzen is False:
            return None
        try:
            import xlwings  # type: ignore[import-not-found]
        except ImportError:
            if self._xlwings_nutzen:
                raise M365Fehler("xlwings ist nicht installiert (pip install 'auftragsautomat[windows]').") from None
            return None
        return xlwings

    def _oeffne(self, pfad: Path) -> tuple[Any, Callable[[], None]]:
        """Liefert (COM-Workbook, Schließfunktion). Eine bereits offene Mappe wird weiterverwendet und
        nicht geschlossen; eine selbst geöffnete wird geschlossen (ohne erneutes Speichern)."""
        xw = self._xlwings()
        if xw is not None:  # pragma: no cover - xlwings nur unter Windows mit Excel
            bereits_offen = any(str(b.name).lower() == pfad.name.lower() for b in xw.books)
            buch = xw.Book(str(pfad))
            if bereits_offen:
                return buch.api, lambda: None

            def schliessen_xw() -> None:
                app = buch.app
                buch.close()
                if not app.books:
                    app.quit()

            return buch.api, schliessen_xw
        app = self._dispatch("Excel.Application")
        for mappe in app.Workbooks:
            if str(mappe.Name).lower() == pfad.name.lower():
                return mappe, lambda: None
        # TODO-PRUEFEN: benannte Argumente bei late-bound Dispatch (pywin32 ≥ 300 unterstützt sie); sonst
        # positional: Open(str(pfad), 0, False, None, None, None, True, None, None, False, False).
        mappe = mit_wiederholung(
            lambda: app.Workbooks.Open(str(pfad), UpdateLinks=0, ReadOnly=False, IgnoreReadOnlyRecommended=True, Notify=False),
            versuche=self.versuche, wartezeit=self.wartezeit, schlaf=self._schlaf, was="Workbooks.Open",
        )

        def schliessen_com() -> None:
            mappe.Close(SaveChanges=False)
            if int(app.Workbooks.Count) == 0:
                # TODO-PRUEFEN: Dispatch hängt sich an ein laufendes Excel; ohne offene Mappen beenden wir es
                # (sehr wahrscheinlich selbst gestartet). Falls der Anwender ein leeres Excel offen hat, stört das.
                app.Quit()

        return mappe, schliessen_com

    @contextmanager
    def sitzung(self, datei: DateiRefKonfig) -> Iterator[None]:
        """Hält die Arbeitsmappe über mehrere Zugriffe offen."""
        schluessel = str(self._pfad(datei))
        if schluessel in self._offen:
            yield
            return
        mappe, schliessen = self._oeffne(Path(schluessel))
        self._offen[schluessel] = (mappe, schliessen)
        try:
            yield
        finally:
            self._offen.pop(schluessel, None)
            schliessen()

    def _mit_mappe(self, datei: DateiRefKonfig, aktion: Callable[[Any], Any]) -> Any:
        pfad = self._pfad(datei)
        offen = self._offen.get(str(pfad))
        if offen is not None:
            return aktion(offen[0])
        mappe, schliessen = self._oeffne(pfad)
        try:
            return aktion(mappe)
        finally:
            schliessen()

    def _com(self, aktion: Callable[[], Any], was: str) -> Any:
        return mit_wiederholung(aktion, versuche=self.versuche, wartezeit=self.wartezeit, schlaf=self._schlaf, was=was)

    @staticmethod
    def _blatt(mappe: Any, blatt: str) -> Any:
        try:
            return mappe.Worksheets(blatt)
        except Exception as exc:
            raise M365NichtGefunden(f"Arbeitsblatt '{blatt}' nicht gefunden.") from exc

    # -- ExcelBackend -------------------------------------------------------------

    def blattnamen(self, datei: DateiRefKonfig) -> list[str]:
        return self._mit_mappe(datei, lambda wb: [str(ws.Name) for ws in wb.Worksheets])

    def lese_bereich(self, datei: DateiRefKonfig, blatt: str, adresse: str) -> list[list[Any]]:
        bereich = parse_adresse(adresse)

        def lesen(wb: Any) -> list[list[Any]]:
            ws = self._blatt(wb, blatt)
            wert = self._com(lambda: ws.Range(bereich.adresse).Value, "Range.Value")
            return _matrix(wert, bereich)

        return self._mit_mappe(datei, lesen)

    def lese_benutzten_bereich(self, datei: DateiRefKonfig, blatt: str) -> tuple[str, list[list[Any]]]:
        def lesen(wb: Any) -> tuple[str, list[list[Any]]]:
            ws = self._blatt(wb, blatt)
            benutzt = ws.UsedRange
            adresse = str(self._com(lambda: benutzt.Address(False, False), "UsedRange.Address"))
            bereich = parse_adresse(adresse)
            wert = self._com(lambda: benutzt.Value, "UsedRange.Value")
            return Zellbereich(bereich.zeile_von, bereich.spalte_von, bereich.zeile_bis, bereich.spalte_bis, blatt).mit_blatt(), _matrix(wert, bereich)

        return self._mit_mappe(datei, lesen)

    def schreibe_bereich(
        self,
        datei: DateiRefKonfig,
        blatt: str,
        adresse: str,
        werte: list[list[Any | None]],
        *,
        zahlenformat: list[list[str | None]] | None = None,
    ) -> None:
        bereich = parse_adresse(adresse)
        if len(werte) != bereich.zeilen or any(len(z) != bereich.spalten for z in werte):
            raise ValueError(f"werte passt nicht zum Bereich {adresse} ({bereich.zeilen}x{bereich.spalten}).")
        if zahlenformat is not None and (
            len(zahlenformat) != bereich.zeilen or any(len(z) != bereich.spalten for z in zahlenformat)
        ):
            raise ValueError(f"zahlenformat passt nicht zum Bereich {adresse}.")

        def schreiben(wb: Any) -> None:
            if bool(wb.ReadOnly):
                raise M365Konflikt(
                    f"{Path(str(wb.Name)).name} ist nur lesend geöffnet (von jemand anderem in Bearbeitung?) – nicht geschrieben.",
                    wartezeit_sekunden=600.0,
                )
            ws = self._blatt(wb, blatt)
            geschrieben = 0
            for zi, zeile in enumerate(werte):
                for si, wert in enumerate(zeile):
                    if wert is None:
                        continue
                    zeilennr, spaltennr = bereich.zeile_von + zi, bereich.spalte_von + si
                    zelle = ws.Cells(zeilennr, spaltennr)
                    if getattr(zelle, "HasFormula", False) is True:
                        raise M365Fehler(
                            f"Zelle {blatt}!{Zellbereich(zeilennr, spaltennr, zeilennr, spaltennr).adresse} enthält eine Formel "
                            "und wird nicht überschrieben."
                        )
                    fmt = zahlenformat[zi][si] if zahlenformat else None
                    if fmt:
                        self._com(lambda z=zelle, f=fmt: setattr(z, "NumberFormat", f), "NumberFormat")
                    if wert == "":
                        self._com(lambda z=zelle: z.ClearContents(), "ClearContents")
                    else:
                        com_wert = _com_wert(wert)
                        self._com(lambda z=zelle, w=com_wert: setattr(z, "Value", w), "Range.Value")
                    geschrieben += 1
            try:
                autosave = bool(wb.AutoSaveOn)
            except Exception:  # Eigenschaft fehlt bei älteren Excel-Versionen
                autosave = False
            # TODO-PRUEFEN: Bei Cloud-Dateien ist AutoSaveOn standardmäßig True (Excel mischt die Änderung ins
            # Co-Authoring); am PC prüfen, ob die Änderung ohne Save() innerhalb weniger Sekunden bei den
            # Kollegen erscheint (Checkliste Teil A, Punkte 5/6 der Recherche).
            if not autosave:
                self._com(wb.Save, "Workbook.Save")
            logger.info("Excel %s!%s: %s Zellen geschrieben (AutoSpeichern %s)", blatt, adresse, geschrieben, "an" if autosave else "aus")

        self._mit_mappe(datei, schreiben)


# ---------------------------------------------------------------------------
# Dateien
# ---------------------------------------------------------------------------


class LokalerDateiBackend(FakeDateiBackend):
    """Dateien im OneDrive-synchronisierten Ordner (``DateiRefKonfig.lokaler_pfad``); OneDrive lädt hoch."""
