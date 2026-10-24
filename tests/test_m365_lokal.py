"""Tests für m365.lokal mit Fake-COM-Objekten (kein Windows, kein Outlook/Excel nötig)."""

from __future__ import annotations

import sys
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from auftragsautomat.config import DateiRefKonfig
from auftragsautomat.m365 import lokal
from auftragsautomat.m365.basis import M365Fehler, M365Konflikt, M365NichtGefunden
from auftragsautomat.m365.lokal import (
    RPC_E_CALL_REJECTED,
    ExcelComBackend,
    LokalerDateiBackend,
    NichtAufWindows,
    OutlookComMailBackend,
    dasl_filter,
    fuege_vor_signatur_ein,
    html_rumpf,
    ist_rpc_abgelehnt,
    mit_wiederholung,
)
from auftragsautomat.models import EmailEntwurf, EntwurfTyp

# ---------------------------------------------------------------- Fake-COM: Outlook


class ComError(Exception):
    def __init__(self, hresult: int, text: str = "Call was rejected by callee") -> None:
        super().__init__(hresult, text)
        self.hresult = hresult


class FakeAttachment:
    def __init__(self, pfad: str) -> None:
        self.pfad = pfad
        self.FileName = Path(pfad).name
        self.Size = 123
        self.gespeichert: list[str] = []

    def SaveAsFile(self, ziel: str) -> None:
        self.gespeichert.append(ziel)
        Path(ziel).write_bytes(b"anhang")


class FakeAttachments:
    def __init__(self) -> None:
        self.liste: list[FakeAttachment] = []

    def Add(self, pfad: str) -> FakeAttachment:
        att = FakeAttachment(pfad)
        self.liste.append(att)
        return att

    @property
    def Count(self) -> int:
        return len(self.liste)

    def Item(self, index: int) -> FakeAttachment:
        return self.liste[index - 1]


SIGNATUR_HTML = (
    "<html><head><meta charset='utf-8'></head><body lang=DE style='word-wrap:break-word'>"
    "<p class=MsoNormal><o:p>&nbsp;</o:p></p><div id='Signature'><p>Mit freundlichen Grüßen<br>Frau Beispiel</p></div>"
    "</body></html>"
)


class FakeMailItem:
    def __init__(self, entry_id: str = "ENTRY-1", **felder: Any) -> None:
        self.To = ""
        self.CC = ""
        self.Subject = ""
        self.BodyFormat = 1
        self.HTMLBody = ""
        self.Body = ""
        self.Attachments = FakeAttachments()
        self.EntryID = entry_id
        self.Class = 43
        self.SenderEmailAddress = "absender@example.org"
        self.SenderName = "Frau Beispiel"
        self.ReceivedTime = datetime(2026, 10, 18, 10, 30)
        self.ereignisse: list[tuple[str, Any]] = []
        for name, wert in felder.items():
            setattr(self, name, wert)

    def Display(self, modal: bool = False) -> None:
        self.ereignisse.append(("Display", modal))
        self.HTMLBody = SIGNATUR_HTML  # Outlook fügt beim Anzeigen die Signatur ein

    def Save(self) -> None:
        self.ereignisse.append(("Save", None))

    def Close(self, modus: int) -> None:
        self.ereignisse.append(("Close", modus))


class FakeItems:
    def __init__(self, items: list[Any]) -> None:
        self.items = items
        self.sortierung: tuple[str, bool] | None = None
        self.filter: str | None = None

    def Sort(self, feld: str, absteigend: bool) -> None:
        self.sortierung = (feld, absteigend)

    def Restrict(self, filter: str) -> FakeItems:
        neu = FakeItems(list(self.items))
        neu.filter = filter
        neu.sortierung = self.sortierung
        return neu

    def __iter__(self):
        return iter(self.items)


class FakeFolder:
    def __init__(self, items: list[Any], unterordner: dict[str, FakeFolder] | None = None) -> None:
        self.Items = FakeItems(items)
        self._unterordner = unterordner or {}

    def Folders(self, name: str) -> FakeFolder:
        if name not in self._unterordner:
            raise ComError(-2147221233, "Ordner nicht gefunden")
        return self._unterordner[name]


class FakeNamespace:
    def __init__(self, inbox: FakeFolder, items: dict[str, FakeMailItem]) -> None:
        self.inbox = inbox
        self.items = items

    def GetDefaultFolder(self, nummer: int) -> FakeFolder:
        assert nummer == 6
        return self.inbox

    def GetItemFromID(self, entry_id: str) -> FakeMailItem:
        if entry_id not in self.items:
            raise ComError(-2147221233, "Element nicht gefunden")
        return self.items[entry_id]


class FakeOutlook:
    def __init__(self, inbox: FakeFolder | None = None) -> None:
        self.erzeugt: list[FakeMailItem] = []
        items = {m.EntryID: m for m in (inbox.Items.items if inbox else []) if isinstance(m, FakeMailItem)}
        self.namespace = FakeNamespace(inbox or FakeFolder([]), items)

    def CreateItem(self, typ: int) -> FakeMailItem:
        assert typ == 0
        mail = FakeMailItem(entry_id=f"ENTRY-{len(self.erzeugt) + 1}")
        self.erzeugt.append(mail)
        return mail

    def GetNamespace(self, name: str) -> FakeNamespace:
        assert name == "MAPI"
        return self.namespace


def dispatch_fuer(objekt: Any):
    aufrufe: list[str] = []

    def dispatch(prog_id: str) -> Any:
        aufrufe.append(prog_id)
        return objekt

    dispatch.aufrufe = aufrufe  # type: ignore[attr-defined]
    return dispatch


def entwurf(*anhaenge: Path) -> EmailEntwurf:
    return EmailEntwurf(
        typ=EntwurfTyp.AUFTRAGSKOPIE_KUNDE,
        an=["kunde@example.org", "zweiter@example.org"],
        cc=["kollegin@example.org"],
        betreff="Ihre Bestellung B-4711 – Auftragskopie zur Information",
        text_html="<html><body><p>Hallo Frau Beispiel,</p><p>anbei die Kopie.</p></body></html>",
        text_plain="Hallo Frau Beispiel,\n\nanbei die Kopie.",
        anhaenge=list(anhaenge),
    )


# ---------------------------------------------------------------- Hilfsfunktionen


def test_html_rumpf_und_einfuegen_vor_signatur():
    assert html_rumpf("<html><body class='x'>\n<p>Text</p>\n</body></html>") == "<p>Text</p>"
    assert html_rumpf("<p>ohne body</p>") == "<p>ohne body</p>"
    ergebnis = fuege_vor_signatur_ein(SIGNATUR_HTML, "<html><body><p>Hallo</p></body></html>")
    assert ergebnis.index("<p>Hallo</p>") < ergebnis.index("id='Signature'")
    assert ergebnis.startswith("<html><head>") and ergebnis.count("<body") == 1
    assert ergebnis.index("<body") < ergebnis.index("<p>Hallo</p>")
    assert fuege_vor_signatur_ein("", "<p>Nur Text</p>") == "<p>Nur Text</p><br>"
    assert fuege_vor_signatur_ein("<p>Sig</p>", "<p>Text</p>") == "<p>Text</p><br><p>Sig</p>"


def test_dasl_filter():
    seit = datetime(2026, 10, 11, 8, 0, tzinfo=UTC)
    assert dasl_filter(seit) == "@SQL=\"urn:schemas:httpmail:datereceived\" >= '10/11/2026 08:00'"
    f = dasl_filter(seit, absender="a@example.org", betreff_enthaelt="Our Ref: DEMB'00000")
    assert "\"urn:schemas:httpmail:subject\" ci_phrasematch 'Our Ref: DEMB''00000'" in f
    assert "\"urn:schemas:httpmail:fromemail\" = 'a@example.org'" in f
    assert f.count(" AND ") == 2
    naive = datetime(2026, 10, 11, 8, 0)
    assert dasl_filter(naive).endswith(f"'{naive.astimezone(UTC):%m/%d/%Y %H:%M}'")


def test_ist_rpc_abgelehnt_und_wiederholung():
    assert ist_rpc_abgelehnt(ComError(RPC_E_CALL_REJECTED))
    assert not ist_rpc_abgelehnt(ComError(-2147221233))
    assert not ist_rpc_abgelehnt(ValueError("x"))
    versuche = {"n": 0}
    pausen: list[float] = []

    def aktion() -> str:
        versuche["n"] += 1
        if versuche["n"] < 3:
            raise ComError(RPC_E_CALL_REJECTED)
        return "fertig"

    assert mit_wiederholung(aktion, versuche=5, wartezeit=0.5, schlaf=pausen.append) == "fertig"
    assert pausen == [0.5, 0.5]
    versuche["n"] = -10
    with pytest.raises(ComError):
        mit_wiederholung(aktion, versuche=3, wartezeit=0.1, schlaf=pausen.append)
    with pytest.raises(ValueError):
        mit_wiederholung(lambda: (_ for _ in ()).throw(ValueError("anderer Fehler")), schlaf=pausen.append)


def test_ohne_windows_wirft_nicht_auf_windows():
    assert sys.platform != "win32"
    with pytest.raises(NichtAufWindows, match="nur unter Windows"):
        OutlookComMailBackend().suche_mails(seit=datetime.now(UTC))
    with pytest.raises(NichtAufWindows):
        lokal._standard_dispatch("Excel.Application")


# ---------------------------------------------------------------- Outlook


def test_outlook_entwurf_mit_signatur_und_anhaengen(tmp_path: Path):
    pdf = tmp_path / "Beispiel – WIK – B-4711 – Kopie – 18.10.2026.pdf"
    pdf.write_bytes(b"%PDF")
    outlook = FakeOutlook()
    backend = OutlookComMailBackend(dispatch=dispatch_fuer(outlook))
    e = entwurf(pdf)
    assert backend.erstelle_entwurf(e) == "ENTRY-1"
    mail = outlook.erzeugt[0]
    assert mail.To == "kunde@example.org; zweiter@example.org" and mail.CC == "kollegin@example.org"
    assert mail.Subject == e.betreff and mail.BodyFormat == 2
    assert [a.pfad for a in mail.Attachments.liste] == [str(pdf.resolve())]
    assert [ev[0] for ev in mail.ereignisse] == ["Display", "Save", "Close"]
    assert mail.ereignisse[0] == ("Display", False) and mail.ereignisse[-1] == ("Close", 0)
    assert "<p>Hallo Frau Beispiel,</p>" in mail.HTMLBody and "id='Signature'" in mail.HTMLBody
    assert mail.HTMLBody.index("Hallo Frau Beispiel") < mail.HTMLBody.index("id='Signature'")
    assert mail.HTMLBody.count("<html") == 1
    assert e.speicherort == "ENTRY-1" and e.erstellt_am is not None
    assert backend._dispatch.aufrufe == ["Outlook.Application"]  # type: ignore[attr-defined]


def test_outlook_entwurf_fehlender_anhang(tmp_path: Path):
    outlook = FakeOutlook()
    with pytest.raises(M365Fehler, match="Anhang nicht gefunden"):
        OutlookComMailBackend(dispatch=dispatch_fuer(outlook)).erstelle_entwurf(entwurf(tmp_path / "fehlt.pdf"))
    assert outlook.erzeugt == []


def _posteingang() -> FakeFolder:
    m1 = FakeMailItem("E1", Subject="Auftragsbestätigung; Your Ref: B-4711; Our Ref: DEMB00000", ReceivedTime=datetime(2026, 10, 18, 9, 0))
    m1.Attachments.Add("/tmp/Bestätigung.pdf")
    m1.Body = "Guten Tag,\nOur Ref: DEMB00000"
    m1.HTMLBody = "<html><body><p>Guten Tag,</p><p>Our Ref: DEMB00000</p></body></html>"
    m1.To = "logistik@example.org; kollegin@example.org"
    m1.CC = "chef@example.org"
    m2 = FakeMailItem("E2", Subject="Zweite", ReceivedTime=datetime(2026, 10, 17, 9, 0))
    termin = FakeMailItem("E3", Subject="Besprechung", Class=26)  # kein MailItem
    spedition = FakeFolder([FakeMailItem("E4", Subject="Im Unterordner")])
    return FakeFolder([m1, m2, termin], {"Spedition": spedition})


def test_outlook_suche_mails_restrict_und_sortierung():
    outlook = FakeOutlook(_posteingang())
    backend = OutlookComMailBackend(dispatch=dispatch_fuer(outlook))
    seit = datetime(2026, 10, 11, tzinfo=UTC)
    koepfe = backend.suche_mails(seit=seit, betreff_enthaelt="Our Ref", maximal=10)
    assert [k.id for k in koepfe] == ["E1", "E2"]  # Termin übersprungen; Filterung macht Outlook (Restrict)
    assert koepfe[0].absender == "absender@example.org" and koepfe[0].absender_name == "Frau Beispiel"
    assert koepfe[0].hat_anhaenge and not koepfe[1].hat_anhaenge and koepfe[0].ordner == "inbox"
    assert koepfe[0].empfangen.tzinfo is not None and koepfe[0].empfangen.replace(tzinfo=None) == datetime(2026, 10, 18, 9, 0)
    items = outlook.namespace.inbox.Items
    assert items.sortierung == ("[ReceivedTime]", True)
    assert backend.suche_mails(seit=seit, maximal=1)[0].id == "E1"
    unter = backend.suche_mails(seit=seit, ordner="Spedition")
    assert [k.id for k in unter] == ["E4"] and unter[0].ordner == "Spedition"
    with pytest.raises(M365NichtGefunden):
        backend.suche_mails(seit=seit, ordner="Gibt es nicht")


def test_outlook_suche_mails_ohne_geschuetzte_felder():
    backend = OutlookComMailBackend(dispatch=dispatch_fuer(FakeOutlook(_posteingang())), geschuetzte_felder_lesen=False)
    koepfe = backend.suche_mails(seit=datetime(2026, 10, 11, tzinfo=UTC))
    assert koepfe[0].absender == "" and koepfe[0].absender_name is None and koepfe[0].betreff.startswith("Auftragsbestätigung")


def test_outlook_lese_mail_und_anhang(tmp_path: Path):
    outlook = FakeOutlook(_posteingang())
    backend = OutlookComMailBackend(dispatch=dispatch_fuer(outlook))
    mail = backend.lese_mail("E1")
    assert mail.kopf.id == "E1" and mail.text.startswith("Guten Tag") and mail.html.startswith("<html>")
    assert mail.an == ["logistik@example.org", "kollegin@example.org"] and mail.cc == ["chef@example.org"]
    assert [(a.id, a.name, a.groesse) for a in mail.anhaenge] == [("1", "Bestätigung.pdf", 123)]
    with pytest.raises(M365NichtGefunden):
        backend.lese_mail("E99")
    ordner = tmp_path / "anh"
    ordner.mkdir()
    pfad = backend.speichere_anhang("E1", "1", ordner)
    assert pfad == ordner / "Bestätigung.pdf" and pfad.read_bytes() == b"anhang"
    att = outlook.namespace.items["E1"].Attachments.Item(1)
    assert att.gespeichert == [str((ordner / "Bestätigung.pdf").resolve())]
    datei = backend.speichere_anhang("E1", "1", tmp_path / "neu" / "b.pdf")
    assert datei.exists()
    with pytest.raises(M365NichtGefunden):
        backend.speichere_anhang("E1", "7", ordner)
    with pytest.raises(M365NichtGefunden):
        backend.speichere_anhang("E1", "abc", ordner)


# ---------------------------------------------------------------- Fake-COM: Excel


class FakeCell:
    def __init__(self, blatt: FakeWorksheet, zeile: int, spalte: int) -> None:
        self.blatt, self.zeile, self.spalte = blatt, zeile, spalte
        self.NumberFormat = "General"

    @property
    def Value(self) -> Any:
        return self.blatt.daten.get((self.zeile, self.spalte))

    @Value.setter
    def Value(self, wert: Any) -> None:
        if self.blatt.ablehnungen > 0:
            self.blatt.ablehnungen -= 1
            raise ComError(RPC_E_CALL_REJECTED)
        self.blatt.daten[(self.zeile, self.spalte)] = wert
        self.blatt.schreibvorgaenge.append((self.zeile, self.spalte, wert))

    @property
    def HasFormula(self) -> bool:
        return (self.zeile, self.spalte) in self.blatt.formeln

    def ClearContents(self) -> None:
        self.blatt.daten.pop((self.zeile, self.spalte), None)
        self.blatt.schreibvorgaenge.append((self.zeile, self.spalte, None))


class FakeRange:
    def __init__(self, blatt: FakeWorksheet, adresse: str) -> None:
        from auftragsautomat.m365.basis import parse_adresse

        self.blatt = blatt
        self.bereich = parse_adresse(adresse)

    @property
    def Value(self) -> Any:
        b = self.bereich
        if b.zeilen == 1 and b.spalten == 1:
            return self.blatt.daten.get((b.zeile_von, b.spalte_von))
        return tuple(
            tuple(self.blatt.daten.get((z, s)) for s in range(b.spalte_von, b.spalte_bis + 1))
            for z in range(b.zeile_von, b.zeile_bis + 1)
        )

    def Address(self, zeile_absolut: bool, spalte_absolut: bool) -> str:
        return self.bereich.adresse


class FakeWorksheet:
    def __init__(self, name: str, daten: dict[tuple[int, int], Any], formeln: set[tuple[int, int]] | None = None) -> None:
        self.Name = name
        self.daten = dict(daten)
        self.formeln = formeln or set()
        self.schreibvorgaenge: list[tuple[int, int, Any]] = []
        self.ablehnungen = 0

    def Range(self, adresse: str) -> FakeRange:
        return FakeRange(self, adresse)

    def Cells(self, zeile: int, spalte: int) -> FakeCell:
        return FakeCell(self, zeile, spalte)

    @property
    def UsedRange(self) -> FakeRange:
        from auftragsautomat.m365.basis import Zellbereich

        zeilen = [z for z, _ in self.daten] or [1]
        spalten = [s for _, s in self.daten] or [1]
        return FakeRange(self, Zellbereich(1, 1, max(zeilen), max(spalten)).adresse)


class FakeWorksheets:
    def __init__(self, blaetter: list[FakeWorksheet]) -> None:
        self.blaetter = blaetter

    def __call__(self, name: str) -> FakeWorksheet:
        for ws in self.blaetter:
            if ws.Name == name:
                return ws
        raise ComError(-2147352565, "Index ungültig")

    def __iter__(self):
        return iter(self.blaetter)


class FakeWorkbook:
    def __init__(self, name: str, blaetter: list[FakeWorksheet], *, read_only: bool = False, autosave: bool = False) -> None:
        self.Name = name
        self.FullName = f"https://contoso.sharepoint.com/sites/Buchhaltung/{name}"
        self.ReadOnly = read_only
        self.AutoSaveOn = autosave
        self.Worksheets = FakeWorksheets(blaetter)
        self.ereignisse: list[tuple[str, Any]] = []
        self.app: FakeExcelApp | None = None

    def Save(self) -> None:
        self.ereignisse.append(("Save", None))

    def Close(self, SaveChanges: bool = True) -> None:
        self.ereignisse.append(("Close", SaveChanges))
        if self.app is not None:
            self.app.Workbooks.liste.remove(self)


class FakeWorkbooks:
    def __init__(self, app: FakeExcelApp) -> None:
        self.app = app
        self.liste: list[FakeWorkbook] = []
        self.geoeffnet: list[tuple[str, dict[str, Any]]] = []

    def __iter__(self):
        return iter(list(self.liste))

    @property
    def Count(self) -> int:
        return len(self.liste)

    def Open(self, pfad: str, **kw: Any) -> FakeWorkbook:
        self.geoeffnet.append((pfad, kw))
        mappe = self.app.vorrat.pop(Path(pfad).name)
        mappe.app = self.app
        self.liste.append(mappe)
        return mappe


class FakeExcelApp:
    def __init__(self, vorrat: dict[str, FakeWorkbook]) -> None:
        self.vorrat = vorrat  # Mappen "auf der Platte"
        self.Workbooks = FakeWorkbooks(self)
        self.beendet = False

    def Quit(self) -> None:
        self.beendet = True


FIX_DATEN = {(4, 1): "KW", (4, 2): "Datum", (4, 4): "Lieferant", (5, 1): 43, (5, 2): datetime(2026, 10, 19), (5, 4): "Cide", (5, 5): "Beispiel", (5, 8): True}


@pytest.fixture()
def xlsx(tmp_path: Path) -> Path:
    pfad = tmp_path / "Fixterminüberwachung.xlsx"
    pfad.write_bytes(b"PK\x03\x04 nur ein Platzhalter")
    return pfad


def excel_backend(app: FakeExcelApp, **kw: Any) -> ExcelComBackend:
    pausen: list[float] = []
    backend = ExcelComBackend(dispatch=dispatch_fuer(app), xlwings_nutzen=False, wartezeit=0.2, schlaf=pausen.append, **kw)
    backend.pausen = pausen  # type: ignore[attr-defined]
    return backend


def test_excel_pfad_pruefungen(tmp_path: Path):
    backend = excel_backend(FakeExcelApp({}))
    with pytest.raises(M365Fehler, match="lokaler_pfad"):
        backend.blattnamen(DateiRefKonfig(web_url="https://x"))
    with pytest.raises(M365NichtGefunden, match="liegt nicht lokal vor"):
        backend.blattnamen(DateiRefKonfig(lokaler_pfad=tmp_path / "fehlt.xlsx"))


def test_excel_lesen_oeffnet_und_schliesst(xlsx: Path):
    blatt = FakeWorksheet("Fixterminüberwachung", FIX_DATEN, formeln={(5, 1), (5, 2)})
    mappe = FakeWorkbook(xlsx.name, [blatt, FakeWorksheet("Ressourcen für Auto DropDown", {})])
    app = FakeExcelApp({xlsx.name: mappe})
    backend = excel_backend(app)
    ref = DateiRefKonfig(lokaler_pfad=xlsx, blatt="Fixterminüberwachung")
    assert backend.blattnamen(ref) == ["Fixterminüberwachung", "Ressourcen für Auto DropDown"]
    assert app.Workbooks.geoeffnet[0][0] == str(xlsx)
    assert app.Workbooks.geoeffnet[0][1] == {"UpdateLinks": 0, "ReadOnly": False, "IgnoreReadOnlyRecommended": True, "Notify": False}
    assert mappe.ereignisse == [("Close", False)] and app.beendet  # selbst geöffnet -> schließen, Excel beenden
    app.vorrat[xlsx.name] = mappe
    assert backend.lese_bereich(ref, "Fixterminüberwachung", "A5:E5") == [[43, datetime(2026, 10, 19), "", "Cide", "Beispiel"]]
    app.vorrat[xlsx.name] = mappe
    assert backend.lese_bereich(ref, "Fixterminüberwachung", "D5") == [["Cide"]]
    app.vorrat[xlsx.name] = mappe
    adresse, werte = backend.lese_benutzten_bereich(ref, "Fixterminüberwachung")
    assert adresse == "'Fixterminüberwachung'!A1:H5" and werte[4][7] is True and werte[0] == [""] * 8
    app.vorrat[xlsx.name] = mappe
    with pytest.raises(M365NichtGefunden, match="Arbeitsblatt"):
        backend.lese_bereich(ref, "Gibt es nicht", "A1")


def test_excel_bereits_offene_mappe_wird_weiterverwendet(xlsx: Path):
    mappe = FakeWorkbook(xlsx.name, [FakeWorksheet("Blatt", {(1, 1): "x"})])
    app = FakeExcelApp({})
    app.Workbooks.liste.append(mappe)
    backend = excel_backend(app)
    assert backend.lese_bereich(DateiRefKonfig(lokaler_pfad=xlsx), "Blatt", "A1") == [["x"]]
    assert app.Workbooks.geoeffnet == [] and mappe.ereignisse == [] and not app.beendet


def test_excel_schreiben_zellenweise_mit_none_und_formaten(xlsx: Path):
    blatt = FakeWorksheet("Fixterminüberwachung", FIX_DATEN, formeln={(5, 1), (5, 2)})
    mappe = FakeWorkbook(xlsx.name, [blatt], autosave=False)
    app = FakeExcelApp({xlsx.name: mappe})
    backend = excel_backend(app)
    ref = DateiRefKonfig(lokaler_pfad=xlsx)
    backend.schreibe_bereich(
        ref, "Fixterminüberwachung", "D6:K6",
        [["Bollina", None, date(2026, 10, 18), datetime(2026, 10, 19, 8, 0, tzinfo=UTC), True, Decimal("2"), Decimal("1.5"), ""]],
        zahlenformat=[[None, None, "TT.MM.JJJJ", None, None, None, "0.0", None]],
    )
    assert blatt.schreibvorgaenge == [
        (6, 4, "Bollina"), (6, 6, datetime(2026, 10, 18)), (6, 7, datetime(2026, 10, 19, 8, 0)), (6, 8, True), (6, 9, 2), (6, 10, 1.5), (6, 11, None),
    ]
    assert (6, 5) not in blatt.daten  # None = unverändert
    assert FakeCell(blatt, 6, 6).NumberFormat == "General"  # Fake merkt Formate nicht je Zelle, Setter lief ohne Fehler
    assert mappe.ereignisse == [("Save", None), ("Close", False)]  # AutoSave aus -> Save()


def test_excel_schreiben_mit_autosave_speichert_nicht_selbst(xlsx: Path):
    blatt = FakeWorksheet("Blatt", {})
    mappe = FakeWorkbook(xlsx.name, [blatt], autosave=True)
    backend = excel_backend(FakeExcelApp({xlsx.name: mappe}))
    backend.schreibe_bereich(DateiRefKonfig(lokaler_pfad=xlsx), "Blatt", "A1:B1", [["a", "b"]])
    assert ("Save", None) not in mappe.ereignisse and blatt.daten == {(1, 1): "a", (1, 2): "b"}


def test_excel_schreiben_schreibgeschuetzt_und_formel(xlsx: Path):
    blatt = FakeWorksheet("Blatt", {(1, 1): "=A2"}, formeln={(1, 1)})
    mappe = FakeWorkbook(xlsx.name, [blatt], read_only=True)
    backend = excel_backend(FakeExcelApp({xlsx.name: mappe}))
    ref = DateiRefKonfig(lokaler_pfad=xlsx)
    with pytest.raises(M365Konflikt, match="nur lesend"):
        backend.schreibe_bereich(ref, "Blatt", "B1", [["x"]])
    assert blatt.schreibvorgaenge == [] and mappe.ereignisse == [("Close", False)]
    mappe.ReadOnly = False
    backend2 = excel_backend(FakeExcelApp({xlsx.name: mappe}))
    with pytest.raises(M365Fehler, match="Formel"):
        backend2.schreibe_bereich(ref, "Blatt", "A1:B1", [["x", "y"]])
    assert blatt.schreibvorgaenge == []
    with pytest.raises(ValueError):
        backend2.schreibe_bereich(ref, "Blatt", "A1:B1", [["x"]])
    with pytest.raises(ValueError):
        backend2.schreibe_bereich(ref, "Blatt", "A1:B1", [["x", "y"]], zahlenformat=[["0"]])


def test_excel_rpc_call_rejected_wird_wiederholt(xlsx: Path):
    blatt = FakeWorksheet("Blatt", {})
    blatt.ablehnungen = 2
    mappe = FakeWorkbook(xlsx.name, [blatt])
    backend = excel_backend(FakeExcelApp({xlsx.name: mappe}), versuche=5)
    backend.schreibe_bereich(DateiRefKonfig(lokaler_pfad=xlsx), "Blatt", "A1", [["x"]])
    assert blatt.daten == {(1, 1): "x"} and backend.pausen == [0.2, 0.2]  # type: ignore[attr-defined]
    blatt.ablehnungen = 5
    backend3 = excel_backend(FakeExcelApp({xlsx.name: mappe}), versuche=3)
    with pytest.raises(ComError):
        backend3.schreibe_bereich(DateiRefKonfig(lokaler_pfad=xlsx), "Blatt", "A2", [["y"]])
    assert backend3.pausen == [0.2, 0.2]  # type: ignore[attr-defined]


def test_excel_sitzung_haelt_mappe_offen(xlsx: Path):
    blatt = FakeWorksheet("Blatt", {(1, 1): 1})
    mappe = FakeWorkbook(xlsx.name, [blatt])
    app = FakeExcelApp({xlsx.name: mappe})
    backend = excel_backend(app)
    ref = DateiRefKonfig(lokaler_pfad=xlsx)
    with backend.sitzung(ref):
        with backend.sitzung(ref):
            assert backend.lese_bereich(ref, "Blatt", "A1") == [[1]]
        backend.schreibe_bereich(ref, "Blatt", "A2", [[2]])
        assert mappe.ereignisse == [("Save", None)]
    assert len(app.Workbooks.geoeffnet) == 1 and mappe.ereignisse[-1] == ("Close", False) and app.beendet


def test_excel_xlwings_erzwungen_ohne_paket(xlsx: Path):
    backend = ExcelComBackend(dispatch=dispatch_fuer(FakeExcelApp({})), xlwings_nutzen=True)
    with pytest.raises(M365Fehler, match="xlwings"):
        backend.blattnamen(DateiRefKonfig(lokaler_pfad=xlsx))


def test_lokaler_dateibackend(tmp_path: Path):
    backend = LokalerDateiBackend(tmp_path / "wurzel")
    ref = DateiRefKonfig(lokaler_pfad=tmp_path / "sync")
    assert backend.ordner_anlegen(ref, "Aufträge ohne Provision") == str(tmp_path / "sync" / "Aufträge ohne Provision")
    assert backend.existiert(ref, "Aufträge ohne Provision") and (tmp_path / "sync" / "Aufträge ohne Provision").is_dir()
