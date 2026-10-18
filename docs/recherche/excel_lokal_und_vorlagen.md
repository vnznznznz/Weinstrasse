# Recherche: Excel-Dateien lokal sicher bearbeiten (Fallback ohne Graph) und E-Mail-Entwürfe mit korrekter Signatur

Stand: 2026-10-18. Projekt „Auftrags-Automat für Lexware premium“, Ausbaustufe 2 (siehe `docs/AUSBAUSTUFE_2.md`,
Module `m365/lokal.py`, `m365/fake.py`, `termine/register.py`, `kommunikation/entwuerfe.py`, `config/vorlagen/`,
`config/signaturen.yaml`).

Umgebung des Anwenders: Windows-PC, Microsoft 365 (Exchange Online, OneDrive/SharePoint, Outlook Desktop),
Google Chrome, Python 3.11+. Betroffene Datei: `Fixterminüberwachung.xlsx` (SharePoint `Buchhaltung/General/Aufträge/`,
von Kollegen gleichzeitig in Excel geöffnet, enthält LET/SORT/UNIQUE-Formeln, Excel-365-Kontrollkästchen in Zellen
und Gliederungsgruppen).

Kennzeichnung in diesem Dokument:

* **[V]** = verifiziert (offizielle Dokumentation von Microsoft, openpyxl, xlwings, LibreOffice, Python bzw. Quellcode
  des Herstellers; URL in „Quellen“).
* **[V-Test]** = in dieser Umgebung selbst nachgestellt (openpyxl 3.1.5 / XlsxWriter 3.2.9 / Jinja2 3.1.6 /
  Python 3.13; Testskript im Anhang). Gilt für genau diese Versionen.
* **[V-Dritt]** = verifiziert als *Aussage einer Drittquelle* (Community-Beitrag, Microsoft Q&A ohne offizielle
  Antwort, Blog, Issue-Tracker) – kann abweichen, nicht als Garantie zu lesen.
* **[A]** = Annahme / Ableitung (mit Begründung). Muss am System geprüft werden.
* **[NF]** = nicht auffindbar / nicht dokumentiert; Prüfauftrag für den Anwender formuliert.

Python-Beispiele für Outlook-COM, Excel-COM/xlwings und Graph sind **nicht ausgeführt** (hier kein Windows, kein
Office, kein Microsoft-365-Zugang). Das openpyxl-Roundtrip-Experiment (Teil A) und das Jinja2/`.eml`-Beispiel (Teil B)
**wurden ausgeführt**. Wo eine Zeile auf einer Annahme beruht, steht ein Kommentar `# [A]`.

---

## Zusammenfassung

1. **[V-Test]** openpyxl 3.1.5 schreibt beim Speichern die gesamte Datei aus seinem Objektmodell neu. Dabei
   verschwinden aus einer Testdatei: `xl/featurePropertyBag/featurePropertyBag.xml` (Träger der Excel-365-
   **Kontrollkästchen**), der Zellstil mit `xfpb:xfComplement` (die Zelle wird zu einer nackten `t="b"`-Zelle → Excel
   zeigt WAHR/FALSCH statt Kästchen), `xl/metadata.xml` samt dem Zellattribut `cm="1"` (Marker für **dynamische
   Array-Formeln**), alle `<extLst>`-Erweiterungen auf Blattebene (Warnung
   „Data Validation extension is not supported and will be removed“; gleicher Code-Pfad für bedingte Formatierung)
   sowie `xr:revisionPtr`/`xmlns:xr` (Co-Authoring-/Revisionsmetadaten) in `workbook.xml`.
2. **[V-Test]** Erhalten bleiben: Formeltexte (inkl. `_xlfn._xlws.SORT(_xlfn.UNIQUE(…))`, `_xlfn.XLOOKUP`),
   `<f t="array" ref="…">`, Standard-Datenüberprüfungen, Standard-bedingte-Formatierung, Gliederungsebenen
   (`outlineLevel`) und Tabellen-Objekte (`xl/tables/table1.xml`, `tablePart`). Zwischengespeicherte Formelwerte
   (`<v>`) werden geleert; Excel rechnet beim Öffnen neu.
3. **[A]** Konsequenz für `Fixterminüberwachung.xlsx`: Nach einem openpyxl-Speichern werden die LET/SORT/UNIQUE-
   Formeln des Blatts „Ressourcen für Auto DropDown“ von Excel als Legacy-Array-Formeln mit **fester Größe** (die
   `ref` der Ankerzelle) gelesen – Microsoft dokumentiert, dass Legacy-Array-Formeln nicht „spillen“ **[V]**. Die
   Dropdown-Quellen würden auf einen Wert zusammenfallen, die Kontrollkästchen zu WAHR/FALSCH werden, und
   Excel-Listen-Validierungen mit Bezug auf ein anderes Blatt (von Excel häufig als `x14:dataValidation` in
   `extLst` geschrieben) würden entfernt. **openpyxl darf die Originaldatei nie speichern** – nur lesen.
4. **[V]** xlwings steuert Excel-Desktop per COM; gespeichert wird von Excel selbst, daher bleibt alles erhalten.
   Seit v0.27.4 funktionieren lokal synchronisierte OneDrive-/SharePoint-Dateien (nur mit grünem Haken, nicht
   „nur Cloud“); bei eingeschaltetem AutoSpeichern liefert `FullName` eine URL, wofür xlwings die Einstellungen
   `ONEDRIVE_COMMERCIAL_WIN`/`SHAREPOINT_WIN` kennt. `xw.Book(pfad)` gibt die bereits geöffnete Mappe zurück,
   statt einen Fehler zu werfen.
5. **[V]** Excel-COM direkt (pywin32): `Workbooks.Open(…, ReadOnly, Notify, IgnoreReadOnlyRecommended)`;
   cloud-gehostete Mappen haben `AutoSaveOn = True`, dann werden auch fremde Änderungen automatisch in die lokale
   Kopie gemischt. Microsoft (Power Automate) warnt: „OneDrive and SharePoint aren't fully compatible with Excel
   instances launched through COM“ und empfiehlt als Workaround lokale Kopie oder Anheften an eine sichtbare
   Excel-Instanz. Der COM-Fehler `RPC_E_CALL_REJECTED (0x80010001)` tritt auf, wenn der Anwender gerade eine Zelle
   bearbeitet oder ein Dialog offen ist → Wiederholen.
6. **[V]** Sperren: Die Besitzerdatei `~$Name.xlsx` legt Excel beim Öffnen an; sie enthält den Benutzernamen, kann
   verwaist zurückbleiben und muss dann gelöscht werden. „Gesperrt zur Bearbeitung durch einen anderen Benutzer“
   kommt auch von einem selbst (anderes Gerät, nicht sauber geschlossene Sitzung), von Excel-Versionen ohne
   Co-Authoring (2016/2019/2021 LTSC), von der Legacy-Funktion „Arbeitsmappe freigeben“ oder von Speicherorten ohne
   Co-Authoring. Co-Authoring verlangt Microsoft-365-Excel, `.xlsx/.xlsm/.xlsb`, OneDrive/SharePoint Online und
   AutoSpeichern. **[NF]** Wie Excel/OneDrive reagieren, wenn ein Fremdprogramm die Datei auf der Platte ersetzt,
   während Kollegen sie co-authoren, ist nicht dokumentiert (Drittquelle: „immer Risiko einer Konfliktkopie“).
7. **[V]** LibreOffice headless (`soffice --headless --convert-to xlsx --outdir … Datei`) kennt FILTER, SORT, UNIQUE
   und LET seit 24.8, aber Excel-365-Zellkontrollkästchen (`featurePropertyBag`) sind nirgends als unterstützt
   dokumentiert **[NF]**, und die Bug-Historie zum `_xlfn.`-Präfix beim Speichern **[V-Dritt]** spricht gegen einen
   Roundtrip dieser Datei.
8. **Empfehlung Teil A:** Reihenfolge der Backends für Schreibzugriffe: (1) Microsoft Graph (bereits beschlossen),
   (2) Excel-Desktop per COM/xlwings auf dem PC des Anwenders (Excel speichert, AutoSpeichern mischt), (3) openpyxl
   **ausschließlich lesend** (`load_workbook(read_only=True, data_only=True)`) und als Dry-Run-Vorschau. Ein
   „openpyxl schreibt die Originaldatei“-Pfad wird nicht gebaut; stattdessen bei fehlendem Graph/COM eine
   Import-Datei (`logs/fixtermine_import_<datum>.xlsx`/`.csv`) erzeugen, die der Anwender in Excel einfügt.
9. **[V]/[V-Dritt]** Signatur per COM (nur **klassisches** Outlook; das neue Outlook hat laut Microsoft-Moderator
   kein COM/VBA/MAPI): `mail.Display()` lässt Outlook die Standardsignatur des Kontos einfügen; danach
   `mail.HTMLBody = text + mail.HTMLBody` (voranstellen), `mail.Save()` legt den Entwurf im Standardordner
   (Entwürfe) ab, `mail.Close(0)` (olSave). Bilder in der Signatur werden dabei von Outlook selbst eingebettet.
   Der `GetInspector`-Trick ohne Anzeige ist Community-Wissen mit widersprüchlichen Aussagen → testen. Das Lesen
   von `%APPDATA%\Microsoft\Signatures\*.htm` ist seit „Roaming Signatures“ (Outlook-Build ≥ 2303, Signaturen im
   Postfach) unzuverlässig; Bilder müssten als Anhang mit `PR_ATTACH_CONTENT_ID` (`0x3712`, PT_UNICODE →
   `…/proptag/0x3712001F`) und `cid:`-Verweis eingebettet werden.
10. **[V]** Signatur per Graph: Graph fügt **keine** Signatur ein (die `message`-Ressource hat keine
    Signatur-Eigenschaft; es gibt keine Graph-API für Signaturen **[V-Dritt]**). Die Signatur muss als HTML aus
    `config/signaturen.yaml` in den `body` geschrieben werden; Inline-Bilder als `fileAttachment` mit
    `isInline: true` und `contentId`, im HTML `<img src="cid:…">`. Anhänge < 3 MB per `POST …/attachments`,
    3–150 MB per `createUploadSession` + `PUT`-Chunks (`Content-Range: bytes a-b/gesamt`, < 4 MB je PUT, kein
    Authorization-Header, Abschluss `201` + `Location`).
11. **[V]/[V-Test]** Jinja2: `select_autoescape(enabled_extensions=("html", "html.j2"),
    disabled_extensions=("txt", "txt.j2"), default=True)` (Abgleich per `endswith`), Zeilenumbrüche über einen
    eigenen `nl2br`-Filter (erst `escape`, dann `Markup("<br>").join`), Signatur als `Markup` übergeben;
    `trim_blocks`/`lstrip_blocks` gegen Leerzeilen. `*.md.j2` aus `AUSBAUSTUFE_2.md` → besser `*.html.j2` und
    `*.txt.j2`.
12. **[V]** Lokales Entwurfsformat: `.msg` ohne Outlook nur mit kommerziellen Bibliotheken schreibbar
    (Open-Source-Python liest nur: python-oxmsg „no support for … creating“, extract-msg); mit Outlook per
    `MailItem.SaveAs(pfad, 3)` (olMSG) bzw. `.oft` (olTemplate = 2) und `CreateItemFromTemplate` (öffnet immer im
    Verfassen-Modus). `.eml` erzeugt Pythons `email`-Modul (`EmailMessage`, `add_attachment`, `add_related`,
    `as_bytes(policy=SMTP)`). Klassisches Outlook öffnet `.eml` seit Build 2109 nur noch mit Kopfzeile
    `X-Unsent: 1` als bearbeitbaren Entwurf **[V-Dritt]**; im neuen Outlook zeitweise gar nicht, zuletzt nur mit
    gültiger `Message-ID` **[V-Dritt]**. → `fake.py` schreibt `.eml` mit `X-Unsent: 1` und `Message-ID`; produktiv
    bleibt Graph (Entwurf im Postfach), Fallback COM.

---

## Teil A – Excel-Dateien lokal sicher bearbeiten (Fallback ohne Graph)

### A.1 Was openpyxl beim Laden/Speichern verliert

#### A.1.1 Was die openpyxl-Dokumentation selbst sagt

* **[V]** Tutorial, Abschnitt „Loading from a file“: *„openpyxl does currently not read all possible items in an
  Excel file so shapes will be lost from existing files if they are opened and saved with the same name.“*
  Flags von `load_workbook`: `data_only` (Formel oder zuletzt von Excel gespeicherter Wert), `keep_vba`
  („preserved … still not editable“), `read_only` („not all features are available (charts, images, etc.)“),
  `rich_text` (Standard `False`), `keep_links` (Cache externer Mappen). `copy_worksheet` kopiert nur Zellen und
  einige Blatt-Attribute, keine Bilder/Diagramme. [Q1]
* **[V]** „Simple formulae“: *„openpyxl never evaluates formula“*; Formeln sind Strings mit englischen
  Funktionsnamen; unbekannte Funktionen (nicht in `openpyxl.utils.FORMULAE`) *„must be prefixed with `_xlfn.` to
  work“*; Array-Formeln über `ArrayFormula("E2:E11", "=…")`, sichtbar nur in der linken oberen Zelle; Datentabellen
  werden nur *erhalten*. Dynamische Array-Formeln (Spill) werden auf der Seite nicht erwähnt. [Q2]
  **[V-Test]** `LET`, `SORT`, `UNIQUE`, `FILTER`, `XLOOKUP` sind **nicht** in `FORMULAE` (3.1.5); `IFERROR` schon.
* **[V]** Datenüberprüfung: *„Validations without any cell ranges will be ignored when saving a workbook.“* Die
  Doku-Seite erwähnt die Erweiterungs-Problematik nicht. [Q3]
* **[V]** Tabellen: `ws.tables` ist ein dict-artiges Objekt aller Tabellen; Tabellennamen müssen mappenweit
  eindeutig sein; Kopfzeilen müssen Strings sein; ein Druckbereich auf einen Tabellennamen kann openpyxl nicht
  auflösen (Warnung). [Q4]
* **[V]** Gliederung: `RowDimension(outlineLevel=…, collapsed=…)`, `DimensionHolder.group(start, end,
  outline_level, hidden)`. [Q5]

#### A.1.2 Quelltext openpyxl 3.1.5 (aus dem PyPI-Paket)

* **[V]** `openpyxl/worksheet/_reader.py`, `parse_extensions()`: jede `<ext>` in einer Blatt-`extLst` erzeugt
  `warn("{0} extension is not supported and will be removed")` (Typ aus `EXT_TYPES`, sonst „Unknown“). Die
  Erweiterungen werden nicht ins Modell übernommen, also beim Schreiben nicht ausgegeben. Betroffen sind damit
  alle x14-Erweiterungen auf Blattebene: Datenüberprüfungen (`x14:dataValidations`), bedingte Formatierungen
  (`x14:conditionalFormattings`, z. B. Datenbalken-2010, Symbolsätze mit Erweiterung, Regeln mit Bezug auf andere
  Blätter), Sparklines, Slicer-Listen usw. [Q6]
* **[V]** `openpyxl/writer/excel.py`: der Writer schreibt feste Teile (`docProps`, `theme`, `styles`, `workbook`,
  Blätter, Tabellen, Diagramme, Bilder, Kommentare, externe Links, Pivot-Caches) aus dem Objektmodell. Nur mit
  `keep_vba=True` werden Teile aus dem Original-Archiv kopiert, und zwar ausschließlich die, die auf das
  VBA-Muster passen (`xl/vbaProject.bin`, `xl/ctrlProps/…`, `xl/drawings/vmlDrawing…` usw.). Alles andere, was
  das Modell nicht kennt (`xl/metadata.xml`, `xl/featurePropertyBag/…`, `xl/revisions/…`, `customXml/…`,
  Slicer-/Timeline-Teile), fällt weg. [Q6]
* **[V]** `openpyxl/cell/_writer.py`: Zeichenketten werden als `t="inlineStr"` geschrieben (kein
  `sharedStrings.xml`); Zellattribute außer `r`, `s`, `t` (also auch `cm`, `vm`) werden nicht ausgegeben. [Q6]
* **[V-Dritt]** Ein offener Merge-Request „Add support for metadata.xml“ (MR 439) bestätigt die Lücke bei
  dynamischen Arrays (`cm`/`vm`-Attribute, `metadata.xml`); Namespaces und `extLst` gingen dort noch verloren; er
  verweist auf Issue #1898. Status nicht einsehbar (Seite durch Bot-Schutz blockiert). [Q10]
* **[V-Dritt]** Issue #1745 „Possibility for pass-through on non-standard data validation?“: Excel selbst schreibt
  eine Datenüberprüfungs-Erweiterung, die openpyxl nicht kennt; Bitte um Durchreichen unveränderter Blätter –
  Status unbekannt. Issue #1458 „List-Data-Validation lost from source file“ (Laden + Speichern ohne Änderung
  entfernt eine Listenvalidierung). Issue #1895 „conditional formatting gets altered / removed on read“: Regeln vom
  Typ „enthält Text“ bzw. mit Bezug auf ein anderes Blatt gingen verloren, „cell value equal to“ blieb. [Q7–Q9]

#### A.1.3 Eigener Roundtrip-Test (openpyxl 3.1.5, Skript im Anhang)

Testdatei mit XlsxWriter 3.2.9 erzeugt: Blatt „Fixterminüberwachung“ mit Kontrollkästchen-Zellen
(`insert_checkbox`), Gliederungsgruppe (`outlineLevel=1`), Listen-Datenüberprüfung mit Bezug auf das Blatt
„Ressourcen“, zwei bedingte Formatierungen (Zellwert, Datenbalken), Tabelle `tbl_Spedition_DE`; Blatt
„Ressourcen“ mit dynamischen Array-Formeln `=SORT(UNIQUE(A2:A5))`, `=FILTER(…)`, `=LET(x,A2:A5,UNIQUE(x))`,
`=XLOOKUP(…)`. In einer zweiten Variante wurden zusätzlich eine `x14:dataValidation` in `extLst` (so schreibt Excel
Listen mit Bezug auf ein anderes Blatt) und ein `xr:revisionPtr` in `workbook.xml` eingefügt. openpyxl hat die
Datei geladen, `D5`/`E5` beschrieben (unser Schreibfall: Slot füllen) und gespeichert.

| Merkmal | Quelle (XlsxWriter) | nach openpyxl `load_workbook` + `save` | Bewertung |
|---|---|---|---|
| ZIP-Teile | 15 Teile inkl. `xl/featurePropertyBag/featurePropertyBag.xml`, `xl/metadata.xml`, `xl/sharedStrings.xml` | 12 Teile; **fehlen: featurePropertyBag.xml, metadata.xml**, sharedStrings.xml (Strings inline) | **[V-Test]** Kontrollkästchen- und Dynamic-Array-Metadaten weg |
| Kontrollkästchen-Zelle F2 | `<c r="F2" s="1" t="b">`; Stil 1 trägt `<extLst><ext uri="{C7286773-…}"><xfpb:xfComplement i="0"/>` | `<c r="F2" t="b"><v>0</v></c>`; Stil ohne `extLst` | **[V-Test]** Darstellung als Kästchen verloren, Wert bleibt boolesch; Excel zeigt FALSCH **[A]** (Kästchen = boolescher Zellwert + Zellsteuerelement/Format **[V, Q11/Q13]**) |
| Dynamische Array-Formel C1 | `<c r="C1" cm="1"><f t="array" ref="C1">_xlfn._xlws.SORT(_xlfn.UNIQUE(A2:A5))</f>` | `<c r="C1"><f t="array" ref="C1">…</f><v />` | **[V-Test]** `cm="1"` weg → Legacy-Array über feste `ref` (eine Zelle) **[A]**; Formeltext inkl. `_xlfn.` unverändert |
| `_xlfn.`/`_xlws.`-Präfixe | 5 Vorkommen | 5 Vorkommen | **[V-Test]** erhalten |
| Gliederung | 4× `outlineLevel="1"` | 4× `outlineLevel="1"` | **[V-Test]** erhalten |
| Datenüberprüfung Standard | `<dataValidation type="list" … sqref="D2:D100">` | vorhanden (Attributreihenfolge anders, `showDropDown="0"` ergänzt) | **[V-Test]** erhalten |
| Datenüberprüfung `x14` in `extLst` | 1 Block | **entfernt**, `UserWarning: Data Validation extension is not supported and will be removed` | **[V-Test]** verloren |
| Bedingte Formatierung (cellIs, dataBar Standard) | 2 Blöcke | 2 Blöcke | **[V-Test]** erhalten (x14-Varianten laut Quelltext verloren) |
| Tabelle | `xl/tables/table1.xml`, `<tablePart r:id="rId1"/>` | beides vorhanden | **[V-Test]** erhalten |
| `xr:revisionPtr`, `xmlns:xr`, `coauthVersion…` | vorhanden | **entfernt** | **[V-Test]** Co-Authoring-Metadaten weg |
| Zwischengespeicherte Formelwerte `<v>` | `0` | leer | **[V-Test]** Excel rechnet beim Öffnen neu; Programme, die nur Cache lesen (`data_only=True`, Graph `values` vor dem nächsten Excel-Öffnen), sehen leer **[A]** |

Hinweis zur Testdatei: XlsxWriter 3.2.9 hat `LET` **nicht** mit `_xlfn.` und seine Parameter nicht mit `_xlpm.`
präfixiert (`LET(x,A2:A5,_xlfn.UNIQUE(x))`); Excel selbst schreibt `_xlfn.LET(_xlpm.x,…)` **[V-Dritt, Q12/Q15]**.
Für openpyxl ist das unerheblich, weil es den Formeltext unverändert durchreicht **[V-Test]**. Wer mit openpyxl
*selbst* solche Formeln schreibt, muss die Präfixe setzen **[V, Q2]** – in unserem Design werden die Formelspalten
A–C und das Ressourcen-Blatt nie beschrieben.

#### A.1.4 Was das für `Fixterminüberwachung.xlsx` bedeutet

* **[A]** Das Blatt „Ressourcen für Auto DropDown“ „füllt sich per Formel“ (LET/SORT/UNIQUE). Nach einem
  openpyxl-Speichern liest Excel diese Zellen als Legacy-Array-Formeln mit der gespeicherten `ref` (bei
  Spill-Formeln ist das die Ankerzelle allein). Microsoft: *„Older array formulas, known as legacy array formulas,
  always return a fixed-size result“*, Spilling gilt für sie nicht **[V, Q14]**. Die Dropdown-Quellen fallen also
  auf je einen Wert zusammen. Ob Excel die Formel zusätzlich mit `@` anzeigt oder als `{…}`-CSE-Formel, ist nicht
  dokumentiert → am Testexemplar prüfen.
* **[A]** Die Kontrollkästchen in „Bestätigung des Lieferanten/der Spedition/Abgeholt“ werden zu WAHR/FALSCH-Text.
  Der boolesche Wert bleibt, die Kollegen verlieren aber die Klick-Bedienung; Wiederherstellen in Excel nur manuell
  (Einfügen → Kontrollkästchen).
* **[A]** Falls die Kurznamen-Dropdowns (Lieferant/Kunde) als Listen-Validierung auf das Ressourcen-Blatt zeigen,
  liegen sie mit hoher Wahrscheinlichkeit als `x14:dataValidation` in `extLst` (Issue #1745/#1458 beschreiben genau
  diesen Fall) → sie würden mit Warnung entfernt. Prüfung: Datei als ZIP öffnen, in `xl/worksheets/sheet1.xml`
  nach `x14:dataValidation` suchen (Checkliste).
* **[A]** Weil openpyxl die Datei vollständig neu schreibt, überschreibt ein Speichern **alle** Änderungen, die
  Kollegen seit unserem `load_workbook` gemacht haben und die noch nicht lokal synchronisiert waren (klassisches
  Lost-Update). Dazu kommen die entfernten Revisionsmetadaten (`xr:revisionPtr`); wie Excel-Sitzungen mit
  AutoSpeichern darauf reagieren, ist nicht dokumentiert **[NF]** – Microsoft nennt als Folgen nicht unterstützter
  Features generell „Refresh recommended“ / „Upload failed“ **[V, Q28]**, eine Drittquelle „immer Risiko einer
  Konfliktkopie“ bei Fremdprogrammen **[V-Dritt, Q30]**.

**Schluss:** openpyxl ist für diese Datei nur als **Leser** geeignet (`load_workbook(pfad, read_only=True,
data_only=True)`), z. B. für `offene(ab, bis)` und für die Dry-Run-Vorschau in `fake.py`. Ein Schreibpfad über
openpyxl auf die Originaldatei wird nicht implementiert.

### A.2 Alternativen

#### A.2.1 xlwings (steuert Excel-Desktop, Excel speichert)

* **[V]** `xw.Book(fullname=…, update_links=…, read_only=…, …, mode=…)`; Standard `mode="i"` („Excel needs to be
  installed“); `mode="r"` liest ohne Excel, aber nur mit xlwings PRO. `Book.save(path=None, password=None)`
  („If a path is provided, this works like SaveAs()“), `Book.close()` („Closes the book without saving it“),
  `Book.fullname`, `Book.app`. [Q18]
* **[V]** `xw.books.open(fullname, update_links, read_only, …, notify, …)`: *„If it is already open, it doesn't raise
  an exception but simply returns the Book object.“* `xw.Book(...)` sucht in allen Excel-Instanzen und meldet einen
  Fehler, wenn dieselbe Mappe in mehreren Instanzen offen ist. `App` als Kontextmanager verwenden. [Q17, Q19]
* **[V]** OneDrive/SharePoint: *„Since v0.27.4, xlwings works with locally synced files on OneDrive, OneDrive for
  Business, and SharePoint.“* Nur Dateien mit grünem Haken (lokal vorhanden), *„a cloud icon will not work“*.
  Viele Funktionen hängen an `FullName`, das bei AutoSpeichern eine URL statt des lokalen Pfads liefert; Standard-
  Setups funktionieren „out-of-the-box“, sonst `ONEDRIVE_COMMERCIAL_WIN` bzw. `SHAREPOINT_WIN` (z. B.
  `%USERPROFILE%\My Company LLC`) setzen; bei gleichnamigen Dateien unter dem SharePoint-Stamm Fehlermeldung →
  Unterordner in `SHAREPOINT_WIN` aufnehmen. Workaround bei Problemen: `ADD_WORKBOOK_TO_PYTHONPATH` abschalten.
  [Q16]
* **Bewertung:** Für `lokal.py` ist xlwings die bequemste Variante über COM (Pfad → Book, Zellen setzen, `save()`).
  Es ersetzt pywin32 nicht, sondern setzt darauf auf; die COM-Fehlerbehandlung aus A.2.2 gilt genauso. Da die Datei
  „von Kollegen gleichzeitig in Excel offen“ ist, läuft Excel auf dem Anwender-PC ohnehin; xlwings hängt sich an
  die offene Mappe (Rückgabe des vorhandenen `Book`) und Excel/AutoSpeichern mischt die Änderung ins Co-Authoring
  **[A]** (Mechanismus laut `AutoSaveOn`-Doku: *„their changes will also be automatically merged into the user's
  local copy when AutoSaveOn is True“* **[V, Q21]**; dass das auch für programmatische Zellschreibungen gilt, ist
  nicht gesondert dokumentiert).

#### A.2.2 Excel-COM direkt (pywin32)

* **[V]** `Workbooks.Open(FileName, UpdateLinks, ReadOnly, Format, Password, WriteResPassword,
  IgnoreReadOnlyRecommended, Origin, Delimiter, Editable, Notify, Converter, AddToMru, Local, CorruptLoad)`.
  `Notify=True`: *„If the file cannot be opened in read/write mode … Microsoft Excel will open the file as
  read-only, poll the file notification list, and then notify the user when the file becomes available. If this
  argument is False or omitted, no notification is requested, and any attempts to open an unavailable file will
  fail.“* `UpdateLinks=0` verhindert den Links-Dialog; Makros sind beim programmatischen Öffnen standardmäßig
  aktiv (`AutomationSecurity`). [Q20]
* **[V]** `Workbook.AutoSaveOn`: bei cloud-gehosteten Mappen standardmäßig `True`; *„If a cloud-hosted workbook is
  shared with other users, their changes will also be automatically merged into the user's local copy when
  AutoSaveOn is True.“* Setzen auf `True/False` ist bei deaktiviertem Schalter ein Fehler. [Q21] Microsoft-
  Leitfaden „How AutoSave impacts add-ins and macros“: bei AutoSpeichern feuern Save-Ereignisse periodisch; Code,
  der das Speichern selbst kontrollieren muss, setzt `AutoSaveOn = False`. Für uns: AutoSpeichern **eingeschaltet
  lassen**, damit die Änderung sofort hochgeladen und gemischt wird; `Save()` zusätzlich ist unschädlich **[A]**.
  [Q22]
* **[V]** Microsoft (Power Automate Desktop, nutzt COM): *„Power Automate for desktop utilizes COM objects for Excel
  interactions. OneDrive and SharePoint aren't fully compatible with Excel instances launched through COM.“*
  Mögliche Folge „file not found“; Workaround 1: lokale Kopie bearbeiten und zurückkopieren; Workaround 2: Excel
  als Anwendung starten (Add-ins laden lassen) und sich an den Prozess anhängen. [Q23]
* **[V]** Fehlerbild `RPC_E_CALL_REJECTED (0x80010001)` „Call was rejected by callee“: *„Ensure that the target
  Excel file isn't being edited (for example, a cell is actively being edited at the time of the action run) or
  blocked by a pop-up dialog.“* `RPC_E_DISCONNECTED (0x80010108)`: Instanz offen halten. [Q24] COM sieht dafür
  `IMessageFilter::RetryRejectedCall` vor (Wiederholen, erst nach ca. 30 s Dialog). [Q25] In Python praktikabel:
  `pywintypes.com_error` mit HRESULT `-2147418111` abfangen und mit Wartezeit wiederholen **[A]**.
* **[V]** Prüfen, ob die Mappe nur lesend geöffnet wurde: `Workbook.ReadOnly` ist die dafür vorgesehene
  Eigenschaft (Doku-Seite in dieser Recherche nicht gesondert abgerufen → **[A]**, Standard-Eigenschaft).

Skizze für `m365/lokal.py` (pywin32, **nicht ausgeführt**):

```python
# m365/lokal.py – Excel-Teil (Windows, klassisches Excel). Nicht ausgeführt.
import time
from pathlib import Path
import pythoncom
import pywintypes
import win32com.client

RPC_E_CALL_REJECTED = -2147418111  # 0x80010001  [V: Power Automate-Doku / IMessageFilter]

class ExcelLokal:
    def __init__(self, pfad: Path):
        self.pfad = Path(pfad)

    def _sperrdatei(self) -> Path:
        # Besitzerdatei "~$Name.xlsx" legt Excel beim Öffnen zum Bearbeiten an [V: Locked-File-Post]
        return self.pfad.with_name("~$" + self.pfad.name)

    def schreibe_bereich(self, blatt: str, adresse: str, werte: list[list]) -> None:
        pythoncom.CoInitialize()
        app = win32com.client.Dispatch("Excel.Application")     # hängt sich an laufendes Excel oder startet eins
        # Workbooks.Open: ReadOnly=False, IgnoreReadOnlyRecommended=True, Notify=False  [V: Workbooks.Open]
        # Ist die Mappe im selben Excel bereits offen, liefert Workbooks(Name) sie; sonst Open.  [A]
        wb = None
        for offen in app.Workbooks:
            if offen.FullName.lower().endswith(self.pfad.name.lower()):   # [A] FullName kann URL sein (AutoSave)
                wb = offen
                break
        if wb is None:
            wb = app.Workbooks.Open(str(self.pfad), 0, False, None, None, None, True, None, None, False, False)
        if wb.ReadOnly:                                          # [A] Standard-Eigenschaft Workbook.ReadOnly
            raise RuntimeError("Fixterminüberwachung.xlsx ist nur lesend geöffnet – nicht geschrieben.")
        ws = wb.Worksheets(blatt)
        for versuch in range(10):
            try:
                ws.Range(adresse).Value = werte                  # 2-D-Liste → Bereich
                break
            except pywintypes.com_error as e:
                if e.hresult == RPC_E_CALL_REJECTED and versuch < 9:
                    time.sleep(1.5)                              # Anwender bearbeitet gerade eine Zelle [V]
                    continue
                raise
        if not wb.AutoSaveOn:                                   # [V] cloud-gehostet: standardmäßig True
            wb.Save()
```

#### A.2.3 LibreOffice headless

* **[V]** Kommandozeile: `--headless` („Starts in headless mode which allows using the application without user
  interface“), `--convert-to OutputFileExtension[:OutputFilterName[:OutputFilterParams]] [--outdir output_dir]`,
  z. B. `soffice --headless --convert-to xlsx --outdir C:\tmp Datei.xlsx`. [Q32]
* **[V]** Funktionsumfang: FILTER, UNIQUE und LET *„available since LibreOffice 24.8“* (Namensräume
  `COM.MICROSOFT.FILTER` usw., nicht Teil von ODF 1.3). [Q33–Q35]
* **[V-Dritt]** Bug-Historie: Calc fügte beim Speichern als OOXML das `_xlfn.`-Präfix für neuere Funktionen zeitweise
  nicht hinzu bzw. entfernte es nicht beim Laden (→ `#NAME?` in Excel); Formular-Kontrollkästchen gingen beim
  xlsx-Export verloren. Die Berichte sind älter und teils behoben, zeigen aber das Risiko. [Q36]
* **[NF]** Unterstützung für Excel-365-**Zell**-Kontrollkästchen (`featurePropertyBag`) und für das Erhalten von
  Co-Authoring-Metadaten ist nirgends dokumentiert.
* **Bewertung:** Für Konvertierungen (z. B. `.xls` → `.xlsx`, PDF-Export) brauchbar, als Schreib-Backend für
  `Fixterminüberwachung.xlsx` nicht – derselbe Roundtrip-Verlust wie bei openpyxl ist zu erwarten, nur weniger
  vorhersehbar **[A]**.

### A.3 Verhalten mit OneDrive-synchronisierten Dateien

* **[V]** Besitzerdatei: *„the owner file is not deleted from the directory upon closing the file. The file looks
  like ~file name.xlsx and contains the user information for who had the file open. … This can happen if something
  interrupts the process and breaks Excels connection with the file.“* Lösung: verwaiste `~$`-Datei löschen. Bei
  der Legacy-„Arbeitsmappe freigeben“-Funktion gibt es trotzdem „file is locked“-Meldungen beim gleichzeitigen
  Öffnen/Speichern. [Q27]
* **[V]** „Excel-Datei ist zur Bearbeitung gesperrt“: *„Sometimes the file is locked by another user, and other
  times the 'another user' is actually you“* (Datei auf anderem Gerät offen, Sitzung nicht sauber beendet);
  *„Excel 2016, 2019, and 2021 LTSC do not support co-authoring“* – *„If just one person uses a version of Excel
  that doesn't support co-authoring“* bekommen alle die Sperrmeldung; Legacy „Shared Workbook“ abschalten;
  Speicherorte wie Dropbox/Box/Google Drive oder lokale Ordner können nicht co-authored werden; Sync-App aktuell
  halten und nicht pausieren; Formate `.xlsx/.xlsm/.xlsb`, kein „Strict Open XML“. [Q26]
* **[V]** Co-Authoring-Voraussetzungen: Microsoft-365-Abonnement und aktuelles Excel, Datei in OneDrive/OneDrive
  for Business/SharePoint Online, AutoSpeichern an; Störungen: *„someone uses an Excel feature that is not fully
  supported in Excel co-authoring yet“* → „Refresh recommended“ / „Upload failed“; Offline-Änderungen werden erst
  nach Wiederverbindung gemischt. [Q28]
* **[V-Dritt]** Microsoft Q&A „File is open in another application“: Excel öffnet eine Datei im lokalen
  OneDrive-Ordner nur lesend, solange der OneDrive-Client sie gerade synchronisiert; ohne OneDrive-Client öffnet
  sie normal. Keine Microsoft-Ursache benannt. [Q29] Tech Community: Files-On-Demand-Dateien halten den Sync-Client
  beschäftigt; *„Opening a OneDrive file locally with a non-Microsoft application always risks a conflict copy“*
  (Konfliktkopien tragen den Computernamen im Dateinamen, z. B. `Budget-Desktop-PC-1.xlsx`). [Q30] Deutsche
  Admin-Foren bestätigen verwaiste `~$`-Dateien und falsche Benutzernamen in der Sperrmeldung. [Q31]
* **[NF]** Ob OneDrive `~$`-Besitzerdateien synchronisiert (und sie damit auf dem PC des Anwenders sichtbar sind,
  wenn eine Kollegin die Datei an ihrem PC offen hat), ist in den gelesenen Quellen nicht belegt. Beim Co-Authoring
  über SharePoint Online liegt die Sperre serverseitig; die lokale `~$`-Datei zeigt nur, dass **auf diesem PC**
  jemand die Datei in Excel offen hat **[A]**. Prüfauftrag in der Checkliste.
* **[NF]** Nicht dokumentiert: Was passiert, wenn ein Fremdprogramm (openpyxl) die Datei im synchronisierten Ordner
  ersetzt, während Kollegen sie per Co-Authoring bearbeiten. Erwartung **[A]**: OneDrive lädt die neue Version hoch,
  die Excel-Sitzungen der Kollegen melden Konflikt/„Upload failed“ oder erzeugen eine Konfliktkopie; eigene
  nicht-synchronisierte Änderungen gehen verloren.

### A.4 Empfehlung für `Fixterminüberwachung.xlsx`

1. **Schreiben nur über Excel selbst oder Graph.** Rangfolge in `config/settings.yaml` → `m365.backend`:
   `graph` (Standard) → `lokal` (COM/xlwings, Excel auf dem Anwender-PC, AutoSpeichern bleibt an) → `fake`.
   openpyxl erhält in `lokal.py`/`fake.py` nur Leserechte (`read_only=True, data_only=True`), und zwar auf einer
   **Kopie** der Datei (OneDrive-Dateien beim Lesen nicht sperren: `shutil.copy2` nach `logs/tmp/`, dann laden) **[A]**.
2. **Vor jedem COM-Schreibzugriff prüfen:** (a) Datei lokal vorhanden (kein Cloud-Platzhalter; xlwings-Regel
   „grüner Haken“) **[V]**; (b) `wb.ReadOnly` → Abbruch mit `Pruefhinweis(FEHLER, "nur lesend geöffnet")` **[A]**;
   (c) `RPC_E_CALL_REJECTED` → bis 10× wiederholen **[V]/[A]**; (d) nach dem Schreiben Zellinhalt zurücklesen und
   im Bericht protokollieren **[A]**.
3. **Spalten A–C, das Ressourcen-Blatt und Zellen mit Formeln nie beschreiben** (Design bleibt); vor dem Schreiben
   `Range.HasFormula` prüfen **[A]** (Excel-Eigenschaft, nicht gesondert verifiziert).
4. **Kein Graph, kein Excel erreichbar:** statt eines openpyxl-Schreibzugriffs auf das Original eine Import-Datei
   `logs/fixtermine_import_<datum>.xlsx` (neu erzeugt, nur Werte) plus Bericht schreiben; der Anwender fügt die
   Zeilen per Kopieren in die Fixterminüberwachung ein. Das ist der einzige openpyxl-Schreibpfad.
5. **Testplan vor Inbetriebnahme** (Checkliste): Kopie der echten Datei durch `load_workbook`/`save` schicken, in
   Excel öffnen und Kontrollkästchen, Dropdowns, Spill-Bereiche prüfen – das dokumentiert den Schaden konkret und
   begründet die Backend-Regel gegenüber den Kollegen.

---

## Teil B – E-Mail-Entwürfe mit korrekter Signatur

### B.1 Signatur per COM (klassisches Outlook)

#### B.1.1 Grundlagen aus der Microsoft-Doku

* **[V]** `MailItem.Display(Modal)`: *„Displays a new Inspector object for the item.“* [Q37]
* **[V]** `MailItem.HTMLBody`: *„Returns or sets a String representing the HTML body … Setting the HTMLBody property
  will always update the Body property immediately.“* Beispiel setzt `BodyFormat = olFormatHTML` und `.HTMLBody`
  vor `.Display`. [Q38]
* **[V]** `MailItem.GetInspector`: *„Returns an Inspector object that represents an inspector initialized to
  contain the specified item … If an Inspector object already exists for the item, the GetInspector property will
  return that Inspector object instead of creating a new one.“* Beispiel nutzt `GetInspector.WordEditor`, um per
  Word-Objektmodell Text einzufügen. Von einer automatischen Signatur steht dort nichts. [Q39]
* **[V]** `MailItem.Save`: *„Saves the Microsoft Outlook item to the current folder or, if this is a new item, to the
  Outlook default folder for the item type“* → Entwürfe. [Q40] `MailItem.Close(SaveMode)` mit `OlInspectorClose`:
  `olSave = 0`, `olDiscard = 1`, `olPromptForSave = 2`. [Q41]
* **[V]** Formatverlust (KB 4020759): Wird `HtmlBody` mit CSS-haltigem HTML gesetzt, *„the formatting that's dictated
  by the configured CSS styles may disappear, or the paragraph styles may be replaced by the MSONormal class“*,
  weil Word als Editor das HTML beim Senden validiert. Empfehlung: `GetInspector.WordEditor` (Word-Objektmodell)
  statt `HtmlBody`. Für unsere schlichten Vorlagen (Absätze, `<br>`, `<b>`, einfache Tabelle) ist `HTMLBody`
  vertretbar **[A]**; CSS-Klassen vermeiden, Inline-Styles sparsam. [Q47]
* **[V]** Neues Outlook: Microsoft-Moderator (Q&A, 2026-07-20): *„The New Outlook for Windows does not support COM,
  VBA, or MAPI automation.“* Empfehlung Graph bzw. Office.js; Übergang: Schalter zurück zu Outlook Classic. [Q53]
  → `lokal.py` setzt das **klassische** Outlook voraus; Erkennung im Code über erfolgreiches
  `Dispatch("Outlook.Application")` + Namespace-Zugriff, sonst Fallback Graph/`.eml` **[A]**.

#### B.1.2 Wie die Standardsignatur in den Entwurf kommt

| Methode | Beleg | Bewertung |
|---|---|---|
| **(1) `Display()` → Outlook fügt Signatur ein → eigenen Text voranstellen** | Ron de Bruin: `.HTMLBody = strbody & "<br>" & .HTMLBody` nach `.Display`; *„we must display the mail for a short time“*; Beispiel 1 funktioniert auch mit Bild in der Signatur **[V-Dritt, Q49]**; Slipstick ebenso **[V-Dritt, Q50]**; deutsches Herber-Forum: *„.GetInspector.Display“* nötig **[V-Dritt, Q52]** | **Empfohlen.** Für Entwürfe ist das Anzeigen kein Nachteil (der Anwender soll ohnehin prüfen). Signatur des Kontos, das Outlook für das neue Element wählt (Standardkonto/-signatur) **[A]** |
| **(2) `GetInspector` anfassen ohne Anzeige** | Dmitry Streblechenko (2007): Signatur wird eingefügt, *„You do not need to actually display the message“*; Sue Mosher: nur wenn `Display` zuerst aufgerufen wird **[V-Dritt, widersprüchlich, Q51]**; AutoIt-/PowerAutomate-Nutzer berichten Erfolg **[V-Dritt]** | Testen; wenn es im Build des Anwenders klappt, ist es die flackerfreie Variante |
| **(3) Signaturdateien lesen** `%APPDATA%\Microsoft\Signatures\<Name>.htm` (+ `<Name>_files\` für Bilder) | Ron de Bruin `GetBoiler` (FileSystemObject) **[V-Dritt, Q49]**; Beispiel 2 *„can't handle a signature with a picture in it“* | **Nur Notnagel.** Seit Roaming Signatures (unten) kann die Standardsignatur lokal fehlen oder veraltet sein; Bilder müssen manuell eingebettet werden (B.1.3) |
| **(4) WordEditor / Lesezeichen `_MailAutoSig`** | Slipstick: Lesezeichen `_MailAutoSig` auswählen/löschen, Signatur wird danach nicht mehr automatisch gewechselt **[V-Dritt, Q50]**; Microsoft empfiehlt WordEditor zum Bearbeiten **[V, Q47]** | Für gezieltes Einfügen *über* der Signatur (`wdDoc.Range(0,0).InsertBefore`) sinnvoll; braucht Word-Objektmodell |

* **[V]** Roaming Signatures: *„Email signatures in Outlook for Windows now roam across devices“*, *„signatures will
  be stored in the user's mailbox“*, ab *„Outlook build 2303 or higher“*; bestehende Signaturen werden nicht
  überschrieben, nicht als Standard gesetzte bleiben lokal verfügbar; Signatur-Add-ins *„will no longer work when
  this feature is enabled“*; Abschalten: `HKEY_CURRENT_USER\Software\Microsoft\Office\16.0\Outlook\Setup`,
  DWORD `DisableRoamingSignatures = 1` (Richtlinienpfad `…\Policies\Microsoft\Office\16.0\Outlook\Setup`). [Q48]
  Folge: Methode (3) ist nicht mehr verlässlich; (1)/(2) nutzen Outlooks eigene Logik und bleiben richtig **[A]**.

#### B.1.3 Inline-Bilder (Logo in der Signatur) per COM

* **[V]** MAPI-Eigenschaft `PidTagAttachContentId` (`PR_ATTACH_CONTENT_ID`): Identifier `0x3712`, Typ
  `PT_STRING8`/`PT_UNICODE`; *„Contains the content identification header of a MIME message attachment … used for
  MHTML support.“* [Q46] Per `PropertyAccessor.SetProperty(SchemaName, Value)` mit Namensraum
  `http://schemas.microsoft.com/mapi/proptag/0x3712001F` (0x3712 + Typ 0x001F = PT_UNICODE) setzbar; Eigenschaften
  werden mit dem expliziten `Save` des Elements gespeichert. [Q45]
* **[V-Dritt]** Vorgehen aus Python-/VBA-Tutorials: `att = mail.Attachments.Add(pfad)`;
  `att.PropertyAccessor.SetProperty("http://schemas.microsoft.com/mapi/proptag/0x3712001F", "logo")`; im HTML
  `<img src="cid:logo">`; Reihenfolge: Anhang + CID setzen, **dann** `HTMLBody` zuweisen. [Q73, Q50 (Oliv)]
* Bei Methode (1) erledigt Outlook das Einbetten der Signaturbilder selbst **[V-Dritt, Q49]**; (B.1.3) braucht man
  nur für Methode (3) oder für eigene Bilder in der Vorlage.

#### B.1.4 Skizze `m365/lokal.py` – Outlook-Teil (pywin32, nicht ausgeführt)

```python
# m365/lokal.py – Outlook-Teil (klassisches Outlook, Windows). Nicht ausgeführt.
import pythoncom
import win32com.client

OL_MAIL_ITEM = 0       # OlItemType.olMailItem  [V: Outlook-Objektmodell]
OL_SAVE = 0            # OlInspectorClose.olSave [V]
OL_FORMAT_HTML = 2     # OlBodyFormat.olFormatHTML [A: Standardwert, nicht gesondert abgerufen]

def erstelle_entwurf(an: list[str], cc: list[str], betreff: str, html_text: str,
                     anhaenge: list[str], inline_bilder: dict[str, str] | None = None) -> str:
    """Legt einen Entwurf im Ordner 'Entwürfe' an und gibt die EntryID zurück. Sendet nie."""
    pythoncom.CoInitialize()
    outlook = win32com.client.Dispatch("Outlook.Application")
    mail = outlook.CreateItem(OL_MAIL_ITEM)
    mail.To = "; ".join(an)
    mail.CC = "; ".join(cc)
    mail.Subject = betreff
    mail.BodyFormat = OL_FORMAT_HTML
    for pfad in anhaenge:
        mail.Attachments.Add(pfad)
    for cid, pfad in (inline_bilder or {}).items():          # nur für eigene Bilder in der Vorlage
        att = mail.Attachments.Add(pfad)
        att.PropertyAccessor.SetProperty(
            "http://schemas.microsoft.com/mapi/proptag/0x3712001F", cid)   # PR_ATTACH_CONTENT_ID [V]
    mail.Display()                       # Outlook fügt die Standardsignatur des Kontos ein [V-Dritt]
    signatur_html = mail.HTMLBody        # enthält <html>…<body>…Signatur…</body></html>
    mail.HTMLBody = _vor_signatur_einfuegen(signatur_html, html_text)   # [A] siehe unten
    mail.Save()                          # neues Element → Standardordner 'Entwürfe' [V]
    mail.Close(OL_SAVE)                  # Inspector schließen, Änderungen behalten [V]
    return mail.EntryID

def _vor_signatur_einfuegen(signatur_html: str, html_text: str) -> str:
    """Fügt den Vorlagentext direkt nach <body…> ein, damit Outlooks Signaturblock erhalten bleibt. [A]"""
    i = signatur_html.lower().find("<body")
    j = signatur_html.find(">", i) + 1 if i >= 0 else 0
    return signatur_html[:j] + html_text + signatur_html[j:]
```

Hinweise: Ron de Bruin verkettet schlicht `strbody & "<br>" & .HTMLBody` **[V-Dritt]**, was Outlook ebenfalls
akzeptiert (es erzeugt dann ein zweites `<html>`-Gerüst). Die Variante „nach `<body>` einfügen“ ist sauberer, aber
eigene Ableitung **[A]**. Wenn der Anwender mehrere Konten/Signaturen hat (Logistik-Postfach vs. persönliches
Konto), bestimmt `MailItem.SendUsingAccount` das Konto; ob Outlook dabei die konto-spezifische Signatur beim
`Display` nachzieht, ist nicht dokumentiert **[NF]** → Prüfauftrag.

### B.2 Signatur und Anhänge per Microsoft Graph

* **[V]** `POST /me/messages` legt einen Entwurf an (JSON oder MIME), Standardordner „Drafts“, Antwort enthält
  `isDraft: true` und `webLink`; Berechtigung `Mail.ReadWrite`. [Q54] Die `message`-Ressource hat **keine**
  Signatur-Eigenschaft (Eigenschaftenliste vollständig gelesen); `hasAttachments` ist bei nur-Inline-Anhängen
  `false`, Inline-Bilder erkennt man am `src`, z. B. `<IMG src="cid:image001.jpg@01D26CD8.6C05F070">`. [Q55]
  Die Konzeptseite „Automate creating, sending, and processing messages“ erwähnt Signaturen nicht. [Q59]
* **[V-Dritt]** Microsoft Q&A: *„Microsoft Graph API doesn't have support for Signature“*; `mailboxSettings` enthält
  keine Signatur; auch EWS-Parameter `SignatureHtml/SignatureText` funktionieren bei aktivierten Roaming
  Signatures nicht. [Q60] → Die Signatur **muss** vom Automaten selbst in den HTML-Body geschrieben werden
  (`config/signaturen.yaml` je Firma/Absender, wie in `AUSBAUSTUFE_2.md` vorgesehen). **[NF]** Ob das klassische
  Outlook beim späteren Öffnen eines per Graph erzeugten Entwurfs *zusätzlich* seine Signatur einfügt, ist nicht
  dokumentiert; Erwartung **[A]**: nein (Signatur-Automatik greift beim Verfassen neuer Elemente in Outlook).
  Prüfauftrag.
* **[V]** Inline-Bild: `fileAttachment` mit `"@odata.type": "#microsoft.graph.fileAttachment"`, `name`,
  `contentBytes` (Base64), `contentType`, `isInline: true`, `contentId` (*„The ID of the attachment in the Exchange
  store“*); `contentLocation` *„Don't use this property as it isn't supported.“* [Q56] Anhänge per
  `POST /me/messages/{id}/attachments` sind auf **unter 3 MB** begrenzt. [Q57]
* **[V]** Große Anhänge (3–150 MB): `POST /me/messages/{id}/attachments/createUploadSession` mit Body
  `{"AttachmentItem": {"attachmentType": "file", "name": "…", "size": <Bytes>}}` → `201` mit `uploadUrl`
  (vor-authentifiziert, Domäne `outlook.office.com`), `expirationDateTime`, `nextExpectedRanges: ["0-"]`. Dann
  `PUT uploadUrl` je Teilstück mit Headern `Content-Type: application/octet-stream`, `Content-Length`,
  `Content-Range: bytes {start}-{end}/{total}`; **kein** Authorization-Header; je PUT unter 4 MB; Teilstücke in
  Reihenfolge; Zwischenantwort `200` mit `nextExpectedRanges`; letzte Antwort `201 Created` mit `Location`-Header
  (Anhang-ID). Abbruch per `DELETE uploadUrl` → `204`. Fehler bei < 3 MB:
  `ErrorAttachmentSizeShouldNotBeLessThanMinimumSize`. Berechtigung `Mail.ReadWrite`. Bekanntes Problem bei
  geteilten/delegierten Postfächern (Known Issue 13644). [Q58]

Skizze für `m365/graph.py` (mit `requests`, Token aus `msal` wie in `graph_excel_outlook.md`; **nicht ausgeführt**):

```python
# m365/graph.py – Entwurf mit Signatur, Inline-Logo und Anhängen. Nicht ausgeführt.
import base64, mimetypes
from pathlib import Path
import requests

GRAPH = "https://graph.microsoft.com/v1.0"
KLEIN_MAX = 3 * 1024 * 1024          # [V] unter 3 MB: einfacher POST
CHUNK = 3 * 320 * 1024               # [A] 960 KiB; Doku verlangt nur "< 4 MB je PUT" und Reihenfolge

def erstelle_entwurf(token: str, an: list[str], cc: list[str], betreff: str,
                     html_text: str, signatur_html: str, logo: Path | None, anhaenge: list[Path]) -> dict:
    h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    body = {
        "subject": betreff,
        "body": {"contentType": "HTML", "content": html_text + signatur_html},   # Signatur selbst einfügen [V]
        "toRecipients": [{"emailAddress": {"address": a}} for a in an],
        "ccRecipients": [{"emailAddress": {"address": c}} for c in cc],
    }
    if logo is not None:                                   # Inline-Bild: isInline + contentId, HTML nutzt cid:logo
        body["attachments"] = [{
            "@odata.type": "#microsoft.graph.fileAttachment",
            "name": logo.name,
            "contentType": mimetypes.guess_type(logo.name)[0] or "application/octet-stream",
            "contentBytes": base64.b64encode(logo.read_bytes()).decode("ascii"),
            "isInline": True,
            "contentId": "logo",
        }]
    r = requests.post(f"{GRAPH}/me/messages", headers=h, json=body, timeout=30)
    r.raise_for_status()
    msg = r.json()                                         # enthält id, isDraft=true, webLink [V]
    for pfad in anhaenge:
        daten = pfad.read_bytes()
        if len(daten) < KLEIN_MAX:
            requests.post(f"{GRAPH}/me/messages/{msg['id']}/attachments", headers=h, timeout=60, json={
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": pfad.name,
                "contentType": mimetypes.guess_type(pfad.name)[0] or "application/octet-stream",
                "contentBytes": base64.b64encode(daten).decode("ascii"),
            }).raise_for_status()
        else:
            _grosser_anhang(h, msg["id"], pfad, daten)
    return msg

def _grosser_anhang(h: dict, msg_id: str, pfad: Path, daten: bytes) -> str:
    r = requests.post(f"{GRAPH}/me/messages/{msg_id}/attachments/createUploadSession", headers=h, timeout=30,
                      json={"AttachmentItem": {"attachmentType": "file", "name": pfad.name, "size": len(daten)}})
    r.raise_for_status()
    upload_url = r.json()["uploadUrl"]                      # vor-authentifiziert, KEIN Authorization-Header [V]
    gesamt = len(daten)
    start = 0
    while start < gesamt:
        ende = min(start + CHUNK, gesamt) - 1
        teil = daten[start:ende + 1]
        r = requests.put(upload_url, data=teil, timeout=120, headers={
            "Content-Type": "application/octet-stream",
            "Content-Length": str(len(teil)),
            "Content-Range": f"bytes {start}-{ende}/{gesamt}",
        })
        r.raise_for_status()
        if r.status_code == 201:                            # fertig: Location-Header enthält Anhang-URL/-ID [V]
            return r.headers.get("Location", "")
        start = int(r.json()["nextExpectedRanges"][0].split("-")[0])   # Reihenfolge einhalten [V]
    return ""
```

### B.3 HTML-Vorlagen mit Jinja2

* **[V]** `Environment(autoescape=…)`: `True` schaltet XML/HTML-Autoescaping ein; `select_autoescape(
  enabled_extensions, disabled_extensions, default_for_string, default)`; im Template `{% autoescape %}`-Block;
  Optionen `trim_blocks` (*„the first newline after a block is removed (block, not variable tag!)“*),
  `lstrip_blocks`, `keep_trailing_newline`, `newline_sequence` (`'\r'`, `'\n'` oder `'\r\n'`). [Q61]
  **[V-Test]** `select_autoescape` vergleicht `template_name.lower().endswith(…)` mit `"." + ext`; mit
  `enabled_extensions=("html", "html.j2")` ist `ab_kunde.html.j2` autoescaped, `nachfrage.txt.j2` mit
  `disabled_extensions=("txt", "txt.j2")` nicht; `None` (String-Template) → `default_for_string`.
* **[V]** Template-Designer: *„When automatic escaping is enabled, everything is escaped by default except for
  values explicitly marked as safe“* (im Kontext als `markupsafe.Markup`, im Template per `|safe`); bereits
  escapte, nicht als sicher markierte Werte werden doppelt escaped; String-Literale im Template gelten als unsicher;
  Makros/`super()` liefern sichere Daten. Whitespace: `-`-Modifikatoren (`{%- … -%}`), ein einzelner abschließender
  Zeilenumbruch wird standardmäßig entfernt. [Q62]
* **[V-Test]** Fallstrick Zeilenumbrüche: `{{ text | e | replace('\n', '<br>') | safe }}` liefert `&lt;br&gt;`, weil
  `Markup.replace` seine Argumente escapt. Lösung: eigener Filter `nl2br` (erst `escape`, dann
  `Markup("<br>").join(...)`). Die Signatur aus `config/signaturen.yaml` als `Markup` übergeben (sie enthält HTML
  und `<img src="cid:…">`).
* **[V]** Zeilenenden: Outlook/Graph erzeugen selbst `\r\n` im Body (Graph-Antwortbeispiel zeigt `\r\n`); für
  `.eml` erzwingt `email.policy.SMTP` RFC-konformes `\r\n` (*„Like default, but with linesep set to \r\n“*). [Q54,
  Q64] Für Text-Vorlagen (`*.txt.j2`) reicht `\n`; die Umwandlung erledigt die Serialisierung.
* **Empfehlung:** Vorlagen als `config/vorlagen/<name>.html.j2` (HTML, autoescape an) und `<name>.txt.j2`
  (Klartext-Alternative, autoescape aus) statt `*.md.j2`; ein `Environment` mit `trim_blocks=True,
  lstrip_blocks=True, keep_trailing_newline=False`; Variablen: `anrede`, `name`, `bestellnr`, `ab_nummer`,
  `kunde`, `lieferant`, `liefertag`, `our_ref`, `signatur_html` (Markup).

Ausgeführtes Beispiel (Jinja2 3.1.6, Python 3.13; Ausgabe geprüft):

```python
from pathlib import Path
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup, escape

env = Environment(
    loader=FileSystemLoader(Path("config/vorlagen")),
    autoescape=select_autoescape(enabled_extensions=("html", "html.j2"),
                                 disabled_extensions=("txt", "txt.j2"),
                                 default_for_string=True, default=True),
    trim_blocks=True, lstrip_blocks=True, keep_trailing_newline=False,
)

def nl2br(wert) -> Markup:
    """Escapt den Text und ersetzt Zeilenumbrüche durch <br>; Ergebnis ist als sicher markiert."""
    return Markup("<br>").join(escape(str(wert)).splitlines())

env.filters["nl2br"] = nl2br

# ab_kunde.html.j2:
#   <p>Hallo {{ anrede }} {{ name }}!</p>
#   <p>Bitte finden Sie anbei die Auftragsbestätigung zu Ihrer Bestellung {{ bestellnr }}.</p>
#   {% if hinweis %}<p>{{ hinweis | nl2br }}</p>{% endif %}
#   {{ signatur_html }}
signatur = Markup('<p>Mit freundlichen Grüßen<br><b>Weinstrasse Logistik</b><br><img src="cid:logo" alt="Logo"></p>')
html = env.get_template("ab_kunde.html.j2").render(
    anrede="Frau", name="Müller <Dispo>", bestellnr="4711 & 4712",
    hinweis="Zeile 1\nZeile 2", signatur_html=signatur)
# Ergebnis (geprüft):
# <p>Hallo Frau Müller &lt;Dispo&gt;!</p>
# <p>Bitte finden Sie anbei die Auftragsbestätigung zu Ihrer Bestellung 4711 &amp; 4712.</p>
# <p>Zeile 1<br>Zeile 2</p><p>Mit freundlichen Grüßen<br><b>Weinstrasse Logistik</b><br><img src="cid:logo" alt="Logo"></p>
```

### B.4 Anhänge > 3 MB per Graph

Siehe B.2 (Upload-Session). Praktische Grenzen für unsere PDFs: Lexware-Belege sind in der Regel deutlich unter
3 MB; der Upload-Session-Pfad ist trotzdem zu implementieren, weil gescannte Lieferscheine (Menrath-Scan) größer
sein können **[A]**. Die Teilstückgröße ist frei unter 4 MB; Vielfache von 320 KiB sind in der Outlook-Doku **nicht**
gefordert (das ist eine OneDrive-Regel) – im Beispiel trotzdem 960 KiB gewählt **[A]**.

### B.5 `.msg` vs. `.eml` als lokales Entwurfsformat

#### B.5.1 `.msg`

* **[V]** Format: [MS-OXMSG] „Outlook Item (.msg) File Format“, Revision 18.0 (20.05.2025); repräsentiert
  Nachrichten, Termine, Kontakte usw. im Dateisystem (OLE-Compound-Datei mit MAPI-Eigenschaftsströmen). [Q65]
* **[V-Dritt]** Ohne Outlook schreiben: Open-Source-Python liest nur – `python-oxmsg` 0.0.2 (MIT): *„There is no
  support for modifying messages or creating them from scratch“*; `extract-msg` (bereits in `pyproject.toml` für
  das Lesen) ebenso. Schreibende Bibliotheken sind kommerziell: Independentsoft „MSG PY“ (`pip install
  independentsoft.msg`, 30-Tage-Evaluation) und Aspose.Email (Java-Brücke). [Q66–Q68]
* **[V]** Mit Outlook: `MailItem.SaveAs(Path, Type)` – ohne Typ `.msg`; `OlSaveAsType`: `olMSG = 3`,
  `olMSGUnicode = 9`, `olTemplate = 2` (.oft), `olHTML = 5`, `olTXT = 0`, `olMHTML = 10`; `.eml` ist **kein**
  Zieltyp. [Q42] `NameSpace.OpenSharedItem(Path)` öffnet `.msg`, `.ics`, `.vcf` (Rückgabetyp entspricht dem
  gespeicherten Element); `.eml` wird dort nicht genannt. [Q44] `Application.CreateItemFromTemplate(TemplatePath,
  InFolder)` erzeugt aus `.oft` ein neues Element: *„New items will always open in compose mode, as opposed to
  read mode, regardless of the mode in which the items were saved to disk“*; mit `InFolder =
  GetDefaultFolder(olFolderDrafts)` + `Save` landet es direkt in Entwürfen. [Q43]

#### B.5.2 `.eml`

* **[V]** Python `email`: `EmailMessage`, `set_content`, `add_alternative(html, subtype="html")`,
  `get_payload()[1].add_related(bytes, "image", "png", cid=…)` (CID aus `make_msgid()`, im HTML ohne `<>`),
  `add_attachment(bytes, maintype=…, subtype=…, filename=…)` mit `mimetypes.guess_type`; Datei schreiben mit
  `open(path, "wb").write(msg.as_bytes(policy=SMTP))`. [Q63] `email.policy.SMTP`: `linesep = "\r\n"`; `default`
  nutzt `\n`; `max_line_length` Standard 78. [Q64]
* **[V-Dritt]** Klassisches Outlook: Bis Version 2109 (Build 16.0.14430.20256) öffnete eine per Dateizuordnung
  gestartete `.eml` als neuer Entwurf; seither nur noch mit Kopfzeile **`X-Unsent: 1`** als bearbeitbarer Entwurf,
  sonst wie eine empfangene Mail (nur Antworten/Weiterleiten). Outlook für Mac ignoriert den Header. [Q69]
* **[V-Dritt]** Neues Outlook: Mai 2024 Microsoft-Moderator *„at this moment it isn't feasible in New Outlook for
  Windows“*; Juli 2024 Entwickler: `.eml` mit `X-Unsent: 1` in der ersten Zeile öffnet als Entwurf; Herbst 2025
  Fehler „This message can't be saved right now“, Ursache fehlende `Message-ID` (Q&A: *„Currently, Outlook requires
  a valid Message-ID in .eml files to save them as drafts“*), Fragesteller meldet Fix in Version 1.2026.105.100.
  [Q70–Q72]
* **[V-Test]** Erzeugtes Beispiel (siehe Anhang): Kopfzeilen `Subject` (RFC-2047-codiert), `From`, `To`, `Cc`,
  `Date`, `Message-ID` (`make_msgid(domain=…)`), `X-Unsent: 1`, `multipart/mixed` → `multipart/alternative`
  (text/plain + text/html mit `cid:logo`) + PDF-Anhang.

#### B.5.3 Empfehlung

| Zweck | Format/Weg | Begründung |
|---|---|---|
| Produktiv, Standard | **Graph-Entwurf im Postfach** (B.2) | funktioniert mit klassischem **und** neuem Outlook, kein lokales Dateiformat nötig, `webLink` zum Öffnen **[V]** |
| Produktiv, Fallback am PC | **COM-Entwurf** (B.1) mit `Display` → Signatur → `Save` | nur klassisches Outlook **[V]**, dafür echte Outlook-Signatur ohne eigene Pflege |
| Dry-Run / Linux / Tests (`fake.py`) | **`.eml`** nach `logs/entwuerfe/` mit `X-Unsent: 1` und `Message-ID` | mit Bordmitteln erzeugbar **[V]**; in klassischem Outlook per Doppelklick als Entwurf nutzbar **[V-Dritt]**; HTML + Anhänge + Inline-Logo vollständig abbildbar |
| Nicht bauen | `.msg` ohne Outlook | nur kommerziell; mit Outlook ist der direkte Entwurf (B.1) einfacher als SaveAs/OpenSharedItem |
| Optional | `.oft`-Vorlagen je Mailtyp, `CreateItemFromTemplate(…, Entwürfe)` | wenn der Anwender Vorlagen lieber in Outlook pflegt; öffnet immer im Verfassen-Modus **[V]** |

---

## Checkliste für den Anwender

Teil A – Excel

1. **Schadensnachweis openpyxl (10 Minuten):** Kopie von `Fixterminüberwachung.xlsx` anlegen, auf dem PC
   `python -c "import openpyxl; wb=openpyxl.load_workbook('Kopie.xlsx'); wb.save('Kopie_openpyxl.xlsx')"` ausführen,
   Warnungen notieren, `Kopie_openpyxl.xlsx` in Excel öffnen: Zeigen die Kontrollkästchen WAHR/FALSCH? Sind die
   Dropdown-Listen (Lieferant/Kunde) noch da? Zeigt „Ressourcen für Auto DropDown“ noch alle Werte oder nur einen?
   Ergebnis an die Entwicklung (bestätigt A.1.4).
2. **Datenüberprüfung im Original prüfen:** Kopie mit `.zip` umbenennen, `xl/worksheets/sheet1.xml` öffnen, nach
   `x14:dataValidation` suchen. Treffer = Dropdowns liegen in der Erweiterung (openpyxl würde sie entfernen).
3. **OneDrive-Status der Datei:** Im Explorer grüner Haken (lokal verfügbar)? Falls Wolkensymbol: Rechtsklick →
   „Immer auf diesem Gerät behalten“ (xlwings/COM brauchen die lokale Datei).
4. **Sperrdatei-Verhalten:** Kollegin öffnet die Datei an ihrem PC; prüfen, ob in Ihrem lokalen Ordner eine
   `~$Fixterminüberwachung.xlsx` erscheint (ausgeblendete Dateien anzeigen). Ergebnis bestimmt, ob die Sperrdatei
   als Signal taugt.
5. **COM-Schreibtest bei offener Datei:** Datei bei Ihnen **und** bei einer Kollegin in Excel geöffnet
   (AutoSpeichern an); Test-Skript (A.2.2-Skizze) schreibt eine Bemerkung in einen leeren Slot; prüfen: erscheint
   die Änderung innerhalb weniger Sekunden bei der Kollegin? Keine Meldung „Upload failed“/„Konfliktkopie“?
6. **AutoSpeichern-Eigenschaft:** In Excel VBA-Direktfenster `?ActiveWorkbook.AutoSaveOn` und
   `?ActiveWorkbook.ReadOnly` ausführen, Werte notieren.
7. **Excel-Version der Kollegen:** Alle auf Microsoft-365-Excel (nicht 2016/2019/2021 LTSC)? Sonst blockiert
   Co-Authoring für alle.
8. **Pfad-Einstellungen für xlwings:** Falls `xw.Book(pfad)` mit „Couldn't find your local OneDrive/SharePoint
   file“ abbricht: `SHAREPOINT_WIN` bzw. `ONEDRIVE_COMMERCIAL_WIN` in `xlwings.conf` setzen (lokaler Stammordner
   der Synchronisation, z. B. `C:\Users\<Name>\<Firma>`).

Teil B – Outlook

9. **Outlook-Variante:** Klassisches Outlook oder „Neues Outlook“ (Schalter rechts oben)? Build (Datei → Office-
   Konto)? ≥ 2303 bedeutet Roaming Signatures aktiv.
10. **Signaturtest COM:** Test-Skript (B.1.4-Skizze) ausführen: Enthält der Entwurf in „Entwürfe“ die richtige
    Signatur inkl. Logo? Variante ohne `Display()` (nur `GetInspector`) ebenfalls testen (flackerfrei?).
11. **Mehrere Konten:** Wenn aus dem Logistik-Postfach gesendet wird: Welches Konto wählt Outlook für
    `CreateItem`? Wird die konto-spezifische Signatur gesetzt oder die des Standardkontos? Ggf.
    `SendUsingAccount` nötig.
12. **Graph-Entwurf in Outlook öffnen:** Einen per Graph erzeugten Entwurf (mit eigener Signatur) in Outlook
    öffnen: Fügt Outlook eine zweite Signatur ein? Werden Inline-Logo (`cid:`) und Anhänge angezeigt?
13. **Signatur-HTML liefern:** Die Signaturen (je Firma/Absender) als HTML aus Outlook kopieren (Datei → Optionen →
    E-Mail → Signaturen, oder aus einer gesendeten Mail „Quelltext anzeigen“) und Logo-Datei bereitstellen →
    `config/signaturen.yaml`.
14. **`.eml`-Test:** Datei `logs/entwuerfe/<name>.eml` aus dem Dry-Run doppelklicken: Öffnet sie als bearbeitbarer
    Entwurf (Senden-Schaltfläche aktiv)? Bleiben Anhänge und Logo erhalten?
15. **Größte PDF-Anhänge:** Typische Größe der Lieferschein-Scans prüfen (> 3 MB? dann Upload-Session-Pfad
    relevant).

---

## Anhang: Testskript (ausgeführt in dieser Umgebung)

Pakete: `openpyxl 3.1.5`, `xlsxwriter 3.2.9` (per `uv pip install --target …`), Python 3.13. Das Skript erzeugt die
Testdatei, lässt openpyxl laden/schreiben/speichern und vergleicht die ZIP-Teile und XML-Attribute.

```python
import sys, warnings, zipfile, re
import xlsxwriter, openpyxl

src, dst = "quelle.xlsx", "openpyxl_roundtrip.xlsx"
wb = xlsxwriter.Workbook(src)
ws = wb.add_worksheet("Fixterminüberwachung")
ws.write_row(0, 0, ["KW", "Datum", "Tag", "Lieferant", "Kunde", "Lieferant bestätigt", "Spedition bestätigt"])
for i, z in enumerate([[44, "2026-10-26", "Mo", "Cide", "Rheinberg"], [44, "2026-10-26", "Mo", "Bollina", "Spar"],
                       [44, "2026-10-27", "Di", "Cide", "Norma"], [44, "2026-10-27", "Di", "", ""]], start=1):
    ws.write_row(i, 0, z)
    ws.insert_checkbox(i, 5, i % 2 == 0)          # Excel-365-Kontrollkästchen (featurePropertyBag)
    ws.insert_checkbox(i, 6, False)
    ws.set_row(i, None, None, {"level": 1})       # Gliederung
ws.data_validation("D2:D100", {"validate": "list", "source": "=Ressourcen!$A$2:$A$50"})
ws.conditional_format("F2:F5", {"type": "cell", "criteria": "==", "value": "TRUE",
                                "format": wb.add_format({"bg_color": "#C6EFCE"})})
ws.conditional_format("A2:A5", {"type": "data_bar"})
ws.add_table("I1:J4", {"name": "tbl_Spedition_DE", "columns": [{"header": "Name"}, {"header": "E-Mail"}],
                       "data": [["Hillebrand", "a@b.de"], ["Stark", "c@d.de"], ["Bouché", "e@f.de"]]})
ws2 = wb.add_worksheet("Ressourcen")
ws2.write_column(0, 0, ["Lieferant", "Cide", "Bollina", "Cide", "Zorzettig"])
ws2.write_dynamic_array_formula("C1:C1", "=SORT(UNIQUE(A2:A5))")        # cm="1" + metadata.xml
ws2.write_dynamic_array_formula("E1:E1", '=FILTER(A2:A5,A2:A5<>"Cide")')
ws2.write_dynamic_array_formula("G1:G1", "=LET(x,A2:A5,UNIQUE(x))")
ws2.write_formula("I1", '=XLOOKUP("Cide",A2:A5,A2:A5)')
wb.close()

with warnings.catch_warnings(record=True) as w:
    warnings.simplefilter("always")
    owb = openpyxl.load_workbook(src)
    owb["Fixterminüberwachung"]["D5"] = "Bollina"   # unser Schreibfall
    owb.save(dst)
print([str(x.message) for x in w])
a = {n: zipfile.ZipFile(src).read(n) for n in zipfile.ZipFile(src).namelist()}
b = {n: zipfile.ZipFile(dst).read(n) for n in zipfile.ZipFile(dst).namelist()}
print("nur in Quelle:", sorted(set(a) - set(b)))
for nm in ("xl/worksheets/sheet1.xml", "xl/worksheets/sheet2.xml"):
    for lab, d in (("QUELLE", a), ("ZIEL", b)):
        s = d[nm].decode()
        print(lab, nm, {k: len(re.findall(p, s)) for k, p in {
            'cm="1"': r'cm="1"', "t=array": r't="array"', "outlineLevel": r'outlineLevel=',
            "dataValidation": r"<dataValidation\b", "condFmt": r"<conditionalFormatting\b",
            "extLst": r"<extLst>", "tablePart": r"<tablePart\b", "_xlfn": r"_xlfn\."}.items()})
print(re.findall(r'<c r="F2"[^>]*>', a["xl/worksheets/sheet1.xml"].decode()),
      re.findall(r'<c r="F2"[^>]*>', b["xl/worksheets/sheet1.xml"].decode()))
```

Ergebnis (gekürzt): Warnungen beim ersten Lauf: keine (Standard-DV/CF); `nur in Quelle:
['xl/featurePropertyBag/featurePropertyBag.xml', 'xl/metadata.xml', 'xl/sharedStrings.xml']`; Blatt 2:
`cm="1"` 4 → 0, `t="array"` 4 → 4, `_xlfn.` 5 → 5; Blatt 1: `outlineLevel` 4 → 4, `dataValidation` 1 → 1,
`conditionalFormatting` 2 → 2, `tablePart` 1 → 1; F2: `<c r="F2" s="1" t="b">` → `<c r="F2" t="b">`. Zweiter Lauf
mit eingefügter `x14:dataValidation` und `xr:revisionPtr`: Warnung „Data Validation extension is not supported and
will be removed“; `x14:dataValidation` 4 → 0, `xr:revisionPtr` 1 → 0.

---

## Quellen

openpyxl, XlsxWriter, Excel-Dateiformat

* [Q1] openpyxl Tutorial (Flags von `load_workbook`, Warnung zu Shapes, `copy_worksheet`):
  https://openpyxl.readthedocs.io/en/stable/tutorial.html
* [Q2] openpyxl „Simple formulae“ (nie ausgewertet, `FORMULAE`, `_xlfn.`, `ArrayFormula`):
  https://openpyxl.readthedocs.io/en/stable/simple_formulae.html
* [Q3] openpyxl „Validating cells“: https://openpyxl.readthedocs.io/en/stable/validation.html
* [Q4] openpyxl „Worksheet Tables“: https://openpyxl.readthedocs.io/en/stable/worksheet_tables.html
* [Q5] openpyxl `worksheet.dimensions` (outline_level, group):
  https://openpyxl.readthedocs.io/en/3.1/api/openpyxl.worksheet.dimensions.html
* [Q6] openpyxl 3.1.5 Quelltext (PyPI-Paket): `openpyxl/worksheet/_reader.py` (`parse_extensions`),
  `openpyxl/writer/excel.py` (`write_data`, VBA-Kopie), `openpyxl/cell/_writer.py` (`inlineStr`, Attribute),
  `openpyxl/utils/formulas.py` (`FORMULAE`). https://pypi.org/project/openpyxl/3.1.5/
* [Q7] openpyxl Issue #1745 „Possibility for pass-through on non-standard data validation?“ (Seite blockiert, nur
  Titel/Suchauszug): https://foss.heptapod.net/openpyxl/openpyxl/-/issues/1745
* [Q8] openpyxl Issue #1458 „List-Data-Validation lost from source file“:
  https://foss.heptapod.net/openpyxl/openpyxl/-/issues/1458
* [Q9] openpyxl Issue #1895 „conditional formatting gets altered / removed on read operation from xlsx“:
  https://foss.heptapod.net/openpyxl/openpyxl/-/issues/1895
* [Q10] openpyxl Merge Request 439 „Add support for metadata.xml“ (Seite blockiert, nur Suchauszug):
  https://foss.heptapod.net/openpyxl/openpyxl/-/merge_requests/439
* [Q11] XlsxWriter `worksheet.insert_checkbox()` (Kontrollkästchen als Boolean + Format, Excel 2024+):
  https://xlsxwriter.readthedocs.io/worksheet.html
* [Q12] XlsxWriter „Working with Formulas“ (`_xlfn.`, `_xlws.`, `_xlpm.`, `use_future_functions`, dynamische
  Arrays): https://xlsxwriter.readthedocs.io/working_with_formulas.html
* [Q13] Office.js `Excel.CellControl` / `CheckboxCellControl` (ExcelApi 1.18):
  https://learn.microsoft.com/en-us/javascript/api/excel/excel.cellcontrol
* [Q14] Microsoft „Dynamic array formulas and spilled array behavior“ (Legacy-Array-Formeln fester Größe):
  https://support.microsoft.com/en-us/topic/205c6b06-03ba-4151-89a1-87a7eb36e531
* [Q15] Exceljet „_xlfn prefix“ (Drittquelle): https://exceljet.net/glossary/xlfn

xlwings, Excel-COM, AutoSpeichern

* [Q16] xlwings „OneDrive and SharePoint“: https://docs.xlwings.org/en/latest/onedrive_sharepoint.html
* [Q17] xlwings „Connect to a Book“: https://docs.xlwings.org/en/latest/connect_to_workbook.html
* [Q18] xlwings API `Book`: https://docs.xlwings.org/en/latest/api/book.html
* [Q19] xlwings API `Books.open`: https://docs.xlwings.org/en/latest/api/books.html
* [Q20] Excel VBA `Workbooks.Open`: https://learn.microsoft.com/en-us/office/vba/api/excel.workbooks.open
* [Q21] Excel VBA `Workbook.AutoSaveOn`: https://learn.microsoft.com/en-us/office/vba/api/excel.workbook.autosaveon
* [Q22] „How AutoSave impacts add-ins and macros“:
  https://learn.microsoft.com/en-us/office/vba/library-reference/concepts/how-autosave-impacts-addins-and-macros
* [Q23] Power Automate „Excel actions“, Abschnitt „Using Excel files synchronized through OneDrive or SharePoint“:
  https://learn.microsoft.com/en-us/power-automate/desktop-flows/actions-reference/excel#using-excel-files-synchronized-through-onedrive-or-sharepoint
* [Q24] Power Automate „Errors when running desktop flows that include Excel actions“ (RPC_E_CALL_REJECTED u. a.):
  https://learn.microsoft.com/en-us/troubleshoot/power-platform/power-automate/desktop-flows/office-automation/excel/troubleshoot-excel-errors
* [Q25] `IMessageFilter::RetryRejectedCall`:
  https://learn.microsoft.com/en-us/windows/win32/api/objidl/nf-objidl-imessagefilter-retryrejectedcall

Sperren, OneDrive, Co-Authoring

* [Q26] Microsoft „Excel file is locked for editing“ (deutsch: „Excel-Datei ist für die Bearbeitung gesperrt“):
  https://support.microsoft.com/en-us/office/excel-file-is-locked-for-editing-6fa93887-2c2c-45f0-abcc-31b04aed68b3 ,
  https://support.microsoft.com/de-DE/Excel/excel-file-is-locked-for-editing
* [Q27] Microsoft Excel Support Team Blog (archiviert) „The Definitive 'Locked File' Post“ (Besitzerdatei `~$`):
  https://learn.microsoft.com/en-us/archive/blogs/the_microsoft_excel_support_team_blog/the-definitive-locked-file-post-updated-772014
* [Q28] Microsoft „Collaborate on Excel workbooks at the same time with co-authoring“:
  https://support.microsoft.com/en-us/office/collaborate-on-excel-workbooks-at-the-same-time-with-co-authoring-7152aa8b-b791-414c-a3bb-3024e46fb104
* [Q29] Microsoft Q&A „File is open in another application“ (OneDrive, Drittquelle):
  https://learn.microsoft.com/en-us/answers/questions/4976710/file-is-open-in-another-application-error
* [Q30] Microsoft Tech Community „Limitations of OneDrive & Excel“ (Drittquelle):
  https://techcommunity.microsoft.com/discussions/onedriveforbusiness/limitations-of-onedrive-excel/1553063
* [Q31] administrator.de (deutsch, Drittquelle): „Excel löscht Besitzerdatei nicht“
  https://administrator.de/forum/excel-loescht-besitzerdatei-nicht-346250.html ; „Excel Datei durch anderen
  Benutzer gesperrt“ https://administrator.de/forum/excel-datei-durch-anderen-benutzer-gesperrt-591676.html

LibreOffice

* [Q32] LibreOffice Hilfe „Starting the Software With Parameters“ (`--headless`, `--convert-to`, `--outdir`):
  https://help.libreoffice.org/latest/en-US/text/shared/guide/start_parameters.html
* [Q33] LibreOffice Hilfe FILTER (seit 24.8): https://help.libreoffice.org/latest/en-US/text/scalc/01/func_filter.html
* [Q34] LibreOffice Hilfe LET (seit 24.8): https://help.libreoffice.org/latest/en-US/text/scalc/01/func_let.html
* [Q35] LibreOffice Hilfe UNIQUE (seit 24.8): https://help.libreoffice.org/latest/en-US/text/scalc/01/func_unique.html
* [Q36] LibreOffice Bugzilla (Drittquelle): `_xlfn.`-Präfix beim OOXML-Speichern
  https://bugs.libreoffice.org/show_bug.cgi?id=106013 ; Kontrollkästchen beim xlsx-Export verloren
  https://www.libreoffice.org/bugzilla/show_bug.cgi?id=142209

Outlook-Objektmodell (COM)

* [Q37] `MailItem.Display`: https://learn.microsoft.com/en-us/office/vba/api/outlook.mailitem.display
* [Q38] `MailItem.HTMLBody`: https://learn.microsoft.com/en-us/office/vba/api/outlook.mailitem.htmlbody
* [Q39] `MailItem.GetInspector`: https://learn.microsoft.com/en-us/office/vba/api/outlook.mailitem.getinspector
* [Q40] `MailItem.Save`: https://learn.microsoft.com/en-us/office/vba/api/outlook.mailitem.save
* [Q41] `MailItem.Close` und `OlInspectorClose`:
  https://learn.microsoft.com/en-us/office/vba/api/outlook.mailitem.close(method) ,
  https://learn.microsoft.com/en-us/office/vba/api/outlook.olinspectorclose
* [Q42] `MailItem.SaveAs` und `OlSaveAsType`: https://learn.microsoft.com/en-us/office/vba/api/outlook.mailitem.saveas ,
  https://learn.microsoft.com/en-us/office/vba/api/outlook.olsaveastype
* [Q43] `Application.CreateItemFromTemplate`:
  https://learn.microsoft.com/en-us/office/vba/api/outlook.application.createitemfromtemplate
* [Q44] `NameSpace.OpenSharedItem`: https://learn.microsoft.com/en-us/office/vba/api/outlook.namespace.openshareditem
* [Q45] `PropertyAccessor.SetProperty`:
  https://learn.microsoft.com/en-us/office/vba/api/outlook.propertyaccessor.setproperty
* [Q46] MAPI `PidTagAttachContentId` (0x3712, PT_UNICODE):
  https://learn.microsoft.com/en-us/office/client-developer/outlook/mapi/pidtagattachcontentid-canonical-property
* [Q47] „Formatting lost when editing HtmlBody“ (KB 4020759):
  https://learn.microsoft.com/en-us/troubleshoot/outlook/user-interface/formatting-lost-when-editing-the-htmlbody-property
* [Q48] Microsoft „Outlook roaming signatures“: https://support.microsoft.com/en-US/Outlook/outlook-roaming-signatures
* [Q49] Ron de Bruin „Insert Outlook Signature in mail“ (Drittquelle):
  https://jkp-ads.com/rdb/win/s1/outlook/signature.htm
* [Q50] Slipstick „Insert Outlook Signature in VBA“ (Drittquelle):
  https://www.slipstick.com/developer/code-samples/insert-signature-vba
* [Q51] Slipstick-Forum „the signature is also inserted if you touch the MailItem.GetInspector“ (Drittquelle,
  widersprüchlich): https://forums.slipstick.com/threads/43819-re-the-signature-is-also-inserted-if-you-touch-the-mailitem
* [Q52] Herber-Forum (deutsch, Drittquelle) „Email aus Excel Signatur ohne Bilder“:
  https://xf2023.herber.de/forum/archiv/1996to2000/1996509_Email_aus_Excel_Signatur_ohne_Bilder.html
* [Q53] Microsoft Q&A „need a support in new outlook automation“ (Moderator: kein COM/VBA/MAPI im neuen Outlook):
  https://learn.microsoft.com/en-in/answers/questions/5951650/need-a-support-in-new-outlook-automation
* [Q73] israel-dryer „Outlook-Python-Tutorial“, Wiki „Attachments“ (Drittquelle, Python/pywin32, cid):
  https://github.com/israel-dryer/Outlook-Python-Tutorial/wiki/Attachments

Microsoft Graph

* [Q54] „Create message“ (`POST /me/messages`): https://learn.microsoft.com/en-us/graph/api/user-post-messages
* [Q55] `message` resource type: https://learn.microsoft.com/en-us/graph/api/resources/message
* [Q56] `fileAttachment` resource type: https://learn.microsoft.com/en-us/graph/api/resources/fileattachment
* [Q57] „Add attachment“ (< 3 MB): https://learn.microsoft.com/en-us/graph/api/message-post-attachments
* [Q58] „Attach large files to Outlook messages or events“ (Upload-Session):
  https://learn.microsoft.com/en-us/graph/outlook-large-attachments
* [Q59] „Automate creating, sending, and processing messages using the Outlook mail API“:
  https://learn.microsoft.com/en-us/graph/outlook-create-send-messages
* [Q60] Microsoft Q&A (Drittquelle): „Roaming Signatures … No MS Graph API exist“
  https://learn.microsoft.com/en-us/answers/questions/890305/roaming-signatures-in-owa-already-done-and-ews-is ;
  „Get email signature saved to an Outlook account via API“ https://learn.microsoft.com/en-us/answers/a/1410790

Jinja2, Python email, .msg/.eml

* [Q61] Jinja2 API (Autoescaping, `select_autoescape`, Environment-Optionen):
  https://jinja.palletsprojects.com/en/stable/api/
* [Q62] Jinja2 Template Designer (HTML Escaping, Whitespace Control):
  https://jinja.palletsprojects.com/en/stable/templates/
* [Q63] Python „email: Examples“ (Anhänge, HTML mit Inline-Bild, Datei schreiben):
  https://docs.python.org/3/library/email.examples.html
* [Q64] Python `email.policy` (`SMTP`, `linesep`): https://docs.python.org/3/library/email.policy.html
* [Q65] [MS-OXMSG] Outlook Item (.msg) File Format:
  https://learn.microsoft.com/en-us/openspecs/exchange_server_protocols/ms-oxmsg/b046868c-9fbf-41ae-9ffb-8de2bd4eec82
* [Q66] python-oxmsg (PyPI, nur Lesen): https://pypi.org/project/python-oxmsg/
* [Q67] Independentsoft „MSG PY“ (kommerziell, Drittquelle): https://www.independentsoft.de/msgpy/
* [Q68] Aspose „Create MSG/EML in Python“ (kommerziell, Drittquelle): https://blog.aspose.com/tag/create-msg-eml-in-python
* [Q69] Microsoft Q&A „EML files no longer opening as a draft e-mail to send“ (X-Unsent, Build 2109):
  https://learn.microsoft.com/en-us/answers/questions/4562029/eml-files-no-longer-opening-as-a-draft-e-mail-to-s
* [Q70] Microsoft Q&A „Header X-Unsent for EML files is not recognized in New Outlook“:
  https://learn.microsoft.com/en-us/answers/questions/4635248/header-x-unsent-for-eml-files-is-not-recognized-in
* [Q71] Microsoft Q&A „New EML files will not save to draft folder“ (Message-ID):
  https://learn.microsoft.com/en-us/answers/questions/5594291/new-eml-files-will-not-save-to-draft-folder-(fixed
* [Q72] Microsoft Q&A „Creating and Sending an Email through New Outlook from a Desktop Application“:
  https://learn.microsoft.com/en-us/answers/questions/1640434/creating-and-sending-an-email-through-new-outlook
