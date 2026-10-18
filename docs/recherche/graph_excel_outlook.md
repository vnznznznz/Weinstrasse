# Recherche: Excel in SharePoint/OneDrive und Outlook-Entwürfe aus einer lokalen Python-Anwendung (Microsoft Graph vs. COM)

Stand: 2026-10-18. Projekt „Auftrags-Automat für Lexware premium“, Ausbaustufe 2 (siehe `docs/AUSBAUSTUFE_2.md`,
Modul `m365/` mit den Backends `graph.py`, `lokal.py`, `fake.py`; Nutzer `termine/`, `kommunikation/`, `ueberwachung/`).

Umgebung des Anwenders: Windows-PC, Microsoft 365 (Exchange Online, OneDrive/SharePoint, Outlook Desktop),
Google Chrome, Python 3.11+. Zu schreibende Datei: `Fixterminüberwachung.xlsx` (SharePoint
`Buchhaltung/General/Aufträge/`, wird von mehreren Kollegen in Excel bearbeitet). Entwürfe im Postfach
`j.duenschede@…`.

Kennzeichnung in diesem Dokument:

* **[V]** = verifiziert (offizielle Dokumentation von Microsoft bzw. Quellcode/README des Herstellers; URL in „Quellen“).
* **[V-Dritt]** = verifiziert als *Aussage einer Drittquelle* (Community-Beitrag, Microsoft Q&A ohne offizielle Antwort,
  Blog) – kann abweichen, nicht als Garantie zu lesen.
* **[A]** = Annahme / Ableitung (mit Begründung). Muss am System geprüft werden.
* **[NF]** = nicht auffindbar / nicht dokumentiert; Prüfauftrag für den Anwender formuliert.

Alle Python-Beispiele sind **nicht ausgeführt** (in dieser Umgebung gibt es keinen Microsoft-365-Zugang). Endpunkte,
Header, Parameter und Bibliotheksfunktionen darin sind jeweils aus den genannten Quellen übernommen; wo eine Zeile auf
einer Annahme beruht, steht ein Kommentar `# [A]`.

---

## Zusammenfassung

1. **[V]** Die Excel-API von Microsoft Graph arbeitet serverseitig auf der Originaldatei in SharePoint/OneDrive for
   Business (Pfad `…/items/{id}/workbook/…`), unterstützt nur `.xlsx` (Office Open XML, kein `.xls`) und **nur
   delegierte Berechtigungen** (`Files.ReadWrite`); Application-Permissions werden für Workbook-Operationen
   ausdrücklich nicht unterstützt. Damit läuft alles unter dem angemeldeten Benutzer (einmalige Anmeldung, Token-Cache).
2. **[V]** Schreiben erfolgt per `PATCH …/worksheets/{name}/range(address='D5:K5')` mit `values`/`formulas`/
   `numberFormat`; `null` in der Matrix lässt eine Zelle unverändert, nicht gesendete Eigenschaften bleiben erhalten
   bzw. werden neu berechnet. Nachbarzellen mit Formeln (Spalten A–C) werden nicht angefasst, wenn sie nicht im
   Adressbereich liegen.
3. **[V]** Sessions: `createSession` mit `persistChanges: true`, Header `workbook-session-id`; persistente Session
   läuft nach ca. 5 Minuten Inaktivität ab (404), `closeSession` am Ende. Ohne Session werden Änderungen ebenfalls
   gespeichert, nur langsamer. Für wenige Schreibvorgänge pro Lauf ist beides zulässig.
4. **[V]** Die Graph-API kennt in v1.0 **keine** Objekte für Kontrollkästchen-Zellen (`Range.control` gibt es nur in
   Office.js/ExcelApi 1.18), keine bedingte Formatierung und keine Gliederung. Kontrollkästchen sind laut
   Office.js-Doku nur eine Darstellung boolescher Zellwerte; **[A]** ein per Graph geschriebenes `true`/`false` sollte
   das Kästchen setzen, ohne es zu entfernen – das ist nicht dokumentiert und muss an einer Kopie getestet werden.
5. **[V]** Konflikte mit gleichzeitig geöffneten Excel-Sitzungen melden sich als Fehlercodes `accessConflict`,
   `invalidSessionAccessConflict` (nicht wiederholen, bis der Konflikt gelöst ist) oder `invalidSessionUnsupportedWorkbook`;
   Microsoft rät von parallelen Schreibzugriffen mehrerer Clients auf dieselbe Datei ab. **[NF]** Wie Excel Desktop mit
   AutoSave Graph-Schreibzugriffe zusammenführt, ist nicht dokumentiert (Community-Bericht: Schreiben klappt, Zellen
   werden überschrieben) → Prüfauftrag.
6. **[V]** Authentifizierung: eigene App-Registrierung in Entra („Accounts in this organizational directory only“,
   Plattform „Mobile and desktop applications“ mit `http://localhost`, „Allow public client flows = Yes“ für
   Device-Code). Alle benötigten delegierten Rechte (`Files.ReadWrite`, `Sites.Read.All`, `Mail.ReadWrite`,
   `Mail.Read`, `User.Read`, `offline_access`) haben in der Referenz `AdminConsentRequired: No`; ob der Benutzer
   selbst zustimmen darf, hängt von der Consent-Richtlinie des Tenants ab (Standard: ja).
7. **[V]** `msal` 1.39 (`PublicClientApplication`, `acquire_token_silent` → `acquire_token_interactive` bzw.
   Device-Code) plus `msal-extensions` 1.3.1 (`build_encrypted_persistence` → auf Windows
   `FilePersistenceWithDataProtection`, DPAPI) liefern einen verschlüsselten Token-Cache mit Dateisperre.
8. **[V]** Die Client-ID `14d82eec-204b-4c2f-b7e8-296a70dab67e` ist von Microsoft als first-party-App
   „Microsoft Graph Command Line Tools“ dokumentiert; Microsoft beschreibt für eigene Skripte den Weg über eine
   **eigene** Registrierung („to isolate and limit the consent permissions“). **[NF]** Eine Freigabe dieser ID für
   Fremdskripte ist nirgends dokumentiert → nicht verwenden.
9. **[V]** Outlook per Graph: `POST /me/messages` legt einen Entwurf im Ordner „Entwürfe“ an (`isDraft: true`,
   `webLink` für Outlook im Web); Anhänge < 3 MB als `fileAttachment` mit `contentBytes` (Base64), 3–150 MB über
   `createUploadSession` + `PUT`-Chunks ≤ 4 MB ohne Authorization-Header. Mails suchen per `$filter=receivedDateTime ge …`
   oder `$search="from:…"`; Anhänge per `…/attachments/{id}/$value`. Kollegenpostfach: `Mail.Read.Shared` +
   Exchange-Freigabe. Limits: 10.000 Requests/10 min je App+Postfach, 4 gleichzeitige Requests.
10. **[V]** COM (pywin32) ist der „Objektmodellschutz“-Falle ausgesetzt: ein per `CreateObject`/`Dispatch` erzeugtes
    Outlook ist immer *nicht vertrauenswürdig*; Lese-Zugriffe auf `To`, `Body`, `HTMLBody`, `SenderEmailAddress`,
    `Recipients` lösen die Adressbuch-Warnung aus, sofern Windows-Sicherheitscenter kein „gutes“ Antivirus meldet oder
    Trust Center/GPO strenger steht. `Subject`, `Attachments.Add`, `Save` sind nicht geschützt. Excel per COM auf dem
    OneDrive-Pfad hat zusätzlich Sync-/Schreibschutz-Risiken (`Workbooks.Open … Notify`, `Workbook.ReadOnly`).
11. **Empfehlung:** Graph als Primärbackend für Excel **und** Mail; COM nur als optionales lokales Backend für das
    Anlegen von Entwürfen (nicht zum Lesen des Posteingangs); `fake.py` für Tests. Begründung in Abschnitt 6.

---

## 1. Microsoft Graph Excel-API

### 1.1 Adressierung der Arbeitsmappe

**Verifiziert**

* [V] Workbook-Ressource über die Drive-API: `https://graph.microsoft.com/v1.0/me/drive/items/{id}/workbook/` oder
  `…/me/drive/root:/{item-path}:/workbook/`. Die offiziellen SDK-Beispiele derselben Seiten verwenden
  `/drives/{drive-id}/items/{driveItem-id}/workbook/…`; diese Form ist für Dateien in SharePoint-Bibliotheken die
  passende (siehe Abschnitt 3). Quelle: „Working with Excel in Microsoft Graph“ (v1.0), Python-Snippets in
  „workbook: createSession“.
* [V] „The Excel REST API supports only Office Open XML file formatted workbooks. The `.xls` extension workbooks aren't
  supported.“ Nur Dateien auf der Business-Plattform (OneDrive for Business, SharePoint, Gruppen); OneDrive Consumer
  nicht.
* [V] Berechtigung für Excel-Operationen: Delegated (work or school) `Files.ReadWrite` (least privileged);
  **Application: Not supported**; persönliche Microsoft-Konten für `createSession`/`tables/rows` nicht unterstützt.
* [V] Arbeitsblätter: `GET …/workbook/worksheets` (Liste mit `id`, `name`, `position`, `visibility`), Zugriff per Name
  `…/worksheets/Sheet32243` oder per ID; IDs enthalten `{}` und müssen URL-kodiert werden (`%7B…%7D`).

**Annahmen**

* [A] Blattnamen mit Umlauten („Fixterminüberwachung“, „Ressourcen für Auto DropDown“) müssen im Pfadsegment
  UTF-8-prozentkodiert werden (Regel aus „Address resources in a drive“: „URL-invalid characters are encoded with a %
  and then the UTF-8 character code“). Sicherer: Blatt einmal per Liste ermitteln und die `id` verwenden.
* [A] Die Adresse im `range(address='…')` kann den Blattnamen enthalten (`'Fixterminüberwachung'!D5:K5`); die
  Doku-Beispiele nutzen `test!A1:B2`. Bei Leerzeichen im Blattnamen wie in Excel in einfache Anführungszeichen setzen.

### 1.2 Sessions (`persistChanges`)

**Verifiziert**

* [V] Drei Modi: persistente Session (Änderungen werden gespeichert; „most efficient“), nicht-persistente Session
  (temporäre Serverkopie, „When the Excel session expires, the changes are lost“), sessionless („Excel servers have to
  locate the server's copy of the workbook each time … suitable for making one off requests“).
* [V] „The session header is not required for an Excel API to work. … If you don't use a session header, changes made
  during the API call *are* persisted to the file.“
* [V] `POST …/workbook/createSession` mit Body `{ "persistChanges": true }` → `201 Created`, Antwort
  `{ "id": "{session-id}", "persistChanges": true }`; danach Header `workbook-session-id: {session-id}` bei jedem
  Aufruf. Abschluss: `POST …/workbook/closeSession` (Header mitsenden) → `204 No Content`.
* [V] Ablauf: „If the session id has expired, a `404` HTTP error code is returned on the session. … Typically the
  persistent session expires after about 5 minutes of inactivity. Non persistent session expires after about 7 minutes
  of inactivity.“
* [V] `createSession` kann gelegentlich `504` liefern: „The appropriate response to this error is to repeat the request.“
* [V] Long-running-Muster: Header `Prefer: respond-async` → ggf. `202 Accepted` mit `Location`-Header
  (`…/workbook/operations/{id}`), Status `notStarted | running | succeeded | failed`; Poll-Intervall ca. 30 s, höchstens
  4 Minuten; bei `succeeded` liefert `resourceLocation` (`…/workbook/sessionInfoResource(key='…')`) die Session.
* [V] Sessionful-Requests mit `502/badGateway` oder `503/serviceUnavailable` ohne bekannten Second-Level-Code: Session
  direkt neu anlegen.

**Annahmen**

* [A] Für unseren Fall (wenige Lese-/Schreibzugriffe pro Lauf, Sekunden Abstand) reicht: Session anlegen → lesen →
  schreiben → schließen; bei 404 auf die Session einmal neu anlegen und wiederholen.

### 1.3 Bereiche lesen und schreiben (`range`, `values`, `formulas`)

**Verifiziert**

* [V] `GET …/worksheets/{id|name}/range(address='A1:B2')` liefert `address`, `values`, `text`, `formulas`,
  `formulasLocal`, `formulasR1C1`, `numberFormat`, `valueTypes` (`Unknown`, `Empty`, `String`, `Integer`, `Double`,
  `Boolean`, `Error`), `rowCount`, `columnCount`, `rowIndex`, `columnIndex`.
* [V] `GET …/worksheets/{id|name}/usedRange` bzw. `usedRange(valuesOnly=true)` („Considers only cells with values as
  used cells (ignores formatting)“) – geeignet, um den belegten Bereich zu finden, ohne eine feste Zeilenzahl anzunehmen.
* [V] `PATCH …/worksheets/{id|name}/range(address='<address>')` (auch `…/names/{name}/range` und
  `…/tables/{id|name}/columns/{id|name}/range`), Header `Content-Type: application/json`, optional
  `Workbook-Session-Id`. Beschreibbare Eigenschaften: `values`, `formulas`, `formulasLocal` („the English
  '=SUM(A1, 1.5)' formula would become '=SUMME(A1; 1,5)' in German“), `formulasR1C1`, `numberFormat`, `rowHidden`,
  `columnHidden`.
* [V] „Existing properties that aren't included in the request body maintains their previous values or be recalculated
  based on changes to other property values. For best performance, you shouldn't include existing values that haven't
  changed.“ `null` in der 2-D-Matrix: „instruct the API to ignore the cell for that particular input“. `""` (leerer
  String) löscht den Inhalt bzw. setzt `numberFormat` auf `General`. `null` für die ganze Eigenschaft (`"values": null`)
  ist ungültig.
* [V] „Single input copy“: Passt die Eingabedimension nicht zum Zielbereich und ist es ein Einzelwert, wird er wie
  STRG+Enter auf den ganzen Bereich angewendet.
* [V] Unbegrenzte Bereiche (`C:C`, `2:2`) können nicht beschrieben werden; große Bereiche in mehreren kleineren
  Aufrufen schreiben. Lese-Limit 5.000.000 Zellen je Range; Excel im Web: Payload-Limit 5 MB je Request/Response
  (Office-Add-in-Doku, von der Graph-Fehlerdoku für `rangeExceedsLimit` referenziert).
* [V] Werte kommen als String, Zahl oder Boolean zurück; Fehlerzellen als Fehlerstring. Datumswerte erscheinen als
  Excel-Serienzahl in `values` (Tabellen-Beispiel: `42019`), formatiert in `text`.

**Verhalten bei Formeln in Nachbarzellen**

* [V] Ein `PATCH` wirkt nur auf den adressierten Bereich; nicht enthaltene Eigenschaften/Zellen „maintain their
  previous values or be recalculated“. Formeln in den Spalten A–C (KW/Datum/Tag) bleiben also unberührt, solange die
  Adresse bei `D` beginnt (z. B. `D5:K5`) – das entspricht der Regel „Spalten A–C nie beschreiben“ aus
  `AUSBAUSTUFE_2.md`.
* [A] Formeln auf dem zweiten Blatt („Ressourcen für Auto DropDown“) rechnen serverseitig nach; Graph gibt in `values`
  die neu berechneten Ergebnisse zurück. Die Doku beschreibt Neuberechnung nur indirekt („recalculated“); ein
  explizites `workbook/application/calculate` ist für normale Werteingaben nicht nötig.
* [A] Beim Schreiben von Texten, die wie Formeln aussehen (führendes `=`), in `values` interpretiert Excel sie als
  Formel; Bemerkungstexte daher ohne führendes `=` bzw. mit Apostroph-Logik prüfen. Nicht aus der Doku belegt → Test.

### 1.4 Tabellen (`tables/{id|name}/rows`)

**Verifiziert**

* [V] `POST …/workbook/tables/{id|name}/rows` (auch `…/worksheets/{id|name}/tables/{id|name}/rows`) mit Body
  `{ "values": [[…],[…]], "index": null }`; `index` optional (0-basiert; `null` = anhängen, darunterliegende Zeilen
  rutschen nach unten) → `201 Created` mit `workbookTableRow` (`index`, `values`). Die Best-Practice-Seite zeigt zusätzlich
  die Aktionsform `…/tables/{id|name}/rows/add` (Antwort `200 OK`). Beide Formen stehen in offizieller Doku.
* [V] „this API can accept multiple rows of data. Adding one row at a time can affect performance. The recommended
  approach is to batch the rows together in a single call“; gelegentlich `504` → wiederholen; asynchron mit
  `Prefer: respond-async` **nur** mit `Workbook-Session-Id`.
* [V] Weitere Tabellen-Operationen in der Übersicht: Liste `…/tables`, `rows`, `columns`, Zeile löschen
  `…/rows/$/itemAt(index=6)`, Spalte anfügen, Sortieren `…/sort/apply`, Filter `…/columns(id='2')/filter/apply`,
  `convertToRange`.

**Annahmen**

* [A] Berechnete Tabellenspalten werden beim Anfügen von Zeilen automatisch gefüllt (Excel-Standardverhalten); die
  Graph-Doku sagt dazu nichts → Test. Für die Kontaktliste (`tbl_Spedition_*`) ist das relevant, für
  `Fixterminüberwachung` (keine Tabelle, feste Slots je Tag) nicht.
* [A] Die 5-Slot-Struktur je Werktag (Zeilen ab 5, Spalten A–C per Formel) ist **kein** Tabellenobjekt; dort ist
  `range`-PATCH das richtige Werkzeug, `rows/add` nicht (es würde ans Tabellenende anfügen).

### 1.5 Kontrollkästchen-Zellen („Checkbox“, Excel 365)

**Verifiziert**

* [V] Office.js-Doku „Manage Excel range checkboxes“: „Checkboxes only display for cells that contain Boolean values such
  as `true` and `false`.“ Workflow: boolesche Werte in die Zellen schreiben, `Range.control = { type:
  Excel.CellControlType.checkbox }` setzen; „Write `true` or `false` to change the checkbox state“; Entfernen mit
  `Excel.CellControlType.empty` („remove the checkbox UI from a range and keep the underlying Boolean values“).
  „If **B3** is not already configured as a checkbox, the cell stores the Boolean value without showing a checkbox.“
  Voraussetzung ExcelApi 1.18 (`Excel.CheckboxCellControl`).
* [V] Die Graph-Ressource `workbookRange` (v1.0) hat **keine** Eigenschaft `control`/`cellControl`; Beziehungen sind nur
  `format`, `sort`, `worksheet`. In der Beta-Dokumentation wurde ebenfalls kein Checkbox-/CellControl-Objekt gefunden
  (Suche). Graph kann also Kontrollkästchen weder anlegen noch entfernen.
* [V] `values` nimmt JSON-Booleans an (`valueTypes` kennt `Boolean`); Excel zeigt sie im deutschen Gebietsschema als
  WAHR/FALSCH. Es ist also `true`/`false` zu senden, **nicht** der String `"WAHR"`.

**Annahmen / offen**

* [A] Da das Kontrollkästchen eine Zelleigenschaft (Cell Control) ist und Graph beim `PATCH` nur `values` ändert, sollte
  das Kästchen erhalten bleiben und den neuen Zustand zeigen. Nicht dokumentiert.
* [NF] Ob ein `PATCH values: [[true]]` über Graph bei einer Checkbox-Zelle das Kästchen erhält, ob ein String in eine
  Checkbox-Zelle die Steuerung entfernt und ob Excel Online/Graph Dateien mit Cell Controls überhaupt als „supported
  workbook“ behandelt (`unsupportedWorkbook`), ist zu testen (Checkliste, Punkt C3).

### 1.6 Bedingte Formatierung, Gliederung, Datenüberprüfung – was bleibt erhalten?

**Verifiziert**

* [V] Graph v1.0 bietet für `workbookRange` nur `format` (Schrift, Füllung, Rahmen, Ausrichtung) – keine Ressourcen für
  bedingte Formatierung, Gliederung/Gruppierung oder Datenüberprüfung (Dropdown). Man kann sie per Graph weder lesen
  noch ändern.
* [V] Fehlercode `invalidSessionUnsupportedWorkbook`/`unsupportedWorkbook`: „The workbook contains unsupported features
  or exceeds the size limit. Usually the unsupported factors are introduced by another client accessing the workbook“ –
  d. h. bestimmte Features einer Datei können die API-Nutzung blockieren; welche, ist nicht aufgelistet.
* [V] Hinweis aus der Excel-Online-Connector-Doku (gleiches Backend): Schon „read-only“-Aktionen erzeugen eine neue
  Version in der Versionsgeschichte („by design due to internal save mechanisms“).

**Annahmen / offen**

* [A] Die API arbeitet in der Excel-Online-Engine auf der Originaldatei; Features, die Excel Online anzeigt (bedingte
  Formatierung, Gliederung, Datenüberprüfung), bleiben beim Speichern durch dieselbe Engine erhalten. Das ist der
  gleiche Mechanismus wie „in Excel im Web eine Zelle ändern“. Keine offizielle Garantie gefunden.
* [NF] Erhalt von bedingter Formatierung, Gruppierung, Datenüberprüfung (Dropdown aus „Ressourcen für Auto DropDown“),
  Kontrollkästchen und Druckeinstellungen nach einem Graph-`PATCH` an einer **Kopie** prüfen (Checkliste C3).

### 1.7 Grenzen: Dateigröße, gleichzeitig geöffnete Datei, Sperren, Co-Authoring

**Verifiziert**

* [V] Keine Zahl für die maximale Dateigröße in der Graph-Excel-Doku; nur „exceeds the size limit“ in den Fehlercodes.
  Power-Automate-Connector „Excel Online (Business)“ (nutzt Graph): max. 25 MB je Datei, 5 MB je Request. Für
  `Fixterminüberwachung.xlsx` und die Kontaktliste irrelevant (Kilobyte-Bereich), solange niemand Bilder einbettet.
* [V] Fehlercodes mit Sperrbezug (Second-Level, case-insensitive):
  * `accessConflict`: „conflicts with other clients accessing the workbook (for example, another client has locked the
    workbook for edit). The Microsoft Graph client is not expected to resend the failed request until the conflict is
    resolved.“
  * `invalidSessionAccessConflict`: Session ungültig wegen Konflikt; nicht mit demselben `createSession` neu anlegen,
    bis der Konflikt gelöst ist.
  * `conflictUncategorized`, `insertDeleteConflict`, `filteredRangeConflict`, `accessDenied` („performing changes to
    locked cells“ – Blattschutz), `nonBlankCellOffSheet`.
* [V] Connector-Doku: „An Excel file may be locked for an update or delete up to 6 minutes since the last use of the
  connector.“ und „Users should avoid writing data to a single Excel file from multiple clients concurrently (Excel
  Desktop, Excel Web, Power Automate, LogicApps or Power Apps). This can cause possible merge conflicts and data
  inconsistency.“
* [V] Best Practice: „We don't recommend increasing concurrency … especially for write requests … for each workbook,
  only send the next request after receiving a successful response to the current request.“ Parallele Schreibzugriffe
  sind „often the cause of throttling, timeout …, merge conflict (when concurrent sessions are involved)“.
* [V-Dritt] Microsoft Q&A (2026, „Intermittent GenericFileOpenError“): Testtabelle des Fragestellers – Datei in Excel
  Desktop geöffnet während des Graph-Schreibens → Inhalt wurde überschrieben (vom Fragesteller als erwartet bewertet);
  mehrere Benutzer auf *demselben* Blatt → „clears data but doesn't write back“; außerdem `GenericFileOpenError`
  (`internalServerErrorUncategorized`, „The workbook cannot be opened“) und „invalid session“ sporadisch. Keine
  offizielle Erklärung im Thread; Microsoft verwies auf die Fehlerdoku und ein Support-Ticket.
* [V-Dritt] Microsoft Q&A: `PUT …/content` (Datei-Upload, **nicht** Workbook-API) scheitert mit `423 Locked`, wenn ein
  Benutzer die Datei in einer Office-Desktop-App geöffnet hat; Graph bietet keine API, um eine Sperre abzufragen.

**Annahmen / offen**

* [A] Die Workbook-API läuft in Excel Online und nimmt am Co-Authoring teil (anders als `PUT …/content`, das die ganze
  Datei ersetzt). Ein in Excel Desktop mit AutoSave geöffnetes Workbook sollte Graph-Änderungen innerhalb von Sekunden
  anzeigen. Belegt ist das nur durch den Community-Bericht oben.
* [NF] Verhalten bei (a) Datei in Excel Desktop geöffnet (AutoSave an/aus), (b) Kollege tippt gleichzeitig in dieselbe
  Zeile, (c) Datei nur über OneDrive-Sync geöffnet ohne Netz – an einer Kopie testen (Checkliste C4).
* [A] Umgang im Code: bei `accessConflict`/`invalidSessionAccessConflict` den Lauf für diese Datei abbrechen, Hinweis
  „Datei gesperrt – später erneut“ ausgeben und per Aufgabenplanung in z. B. 10 Minuten erneut versuchen; **nicht**
  sofort wiederholen (Doku: „not expected to resend“).

### 1.8 Throttling

**Verifiziert**

* [V] Excel-Service-Limits (Graph-Throttling-Seite): „Any“ Request – 5.000 Requests / 10 s je App über alle Tenants,
  1.500 Requests / 10 s je App je Tenant; gilt für `workbook*`-Ressourcen. Globales Limit 130.000 Requests / 10 s je App.
* [V] Excel-API-Throttling ist zusätzlich lastabhängig („isn't defined with simple and universal limit numbers“);
  Antworten können `Retry-After` (Sekunden) tragen: „the Microsoft Graph client is not expected to send any follow-up
  requests before the specified duration passes“. Codes: `tooManyRequestsUncategorized`, `serviceUnavailableUncategorized`,
  `transientFailure` (jeweils Cooldown abwarten), `gatewayTimeoutUncategorized`.
* [V] Throttling-Header allgemein: `x-ms-throttle-scope`, `x-ms-throttle-information`, `x-ms-throttle-limit-percentage`.

**Annahmen**

* [A] Unser Volumen (einige Dutzend Requests je Tagelauf) ist weit unter allen Limits; eine einfache Schleife
  „bei 429/503 `Retry-After` abwarten, höchstens 3 Versuche, sonst exponentiell 2/4/8 s“ genügt.

### 1.9 Fehlerstruktur (Graph Excel)

* [V] Regulär: HTTP-Status + `{ "error": { "code", "message", "innerError": { "code", "message", … } } }`;
  Long-running: `200 OK` mit `"status": "failed"` und `error`. Vorgehen laut Doku: 1) prüfen, ob Long-running-Fehler,
  2) Second-Level-Code (`innerError.code`) auswerten, 3) Top-Level-Code, 4) HTTP-Status, 5) `Retry-After` beachten.
  Nachrichten-Texte nur zur Diagnose, nicht zur Logik verwenden.
* [V] Wichtige Second-Level-Codes (Pflicht): `accessConflict`, `badRequestUncategorized`, `conflictUncategorized`,
  `forbiddenUncategorized`, `gatewayTimeoutUncategorized`, `internalServerErrorUncategorized` (Session danach nicht
  weiterverwenden), `invalidSessionAccessConflict`, `invalidSessionAuthentication`, `invalidSessionNotFound`,
  `invalidSessionReCreatable` (Session neu anlegen und weitermachen), `invalidSessionRestricted`,
  `invalidSessionUnexpected`, `invalidSessionUnsupportedWorkbook`, `methodNotAllowedUncategorized`,
  `notFoundUncategorized`, `notImplementedUncategorized`, `payloadTooLargeUncategorized`, `serviceUnavailableUncategorized`,
  `tooManyRequestsUncategorized`, `transientFailure`, `unauthorizedUncategorized`, `unsupportedWorkbook`. Optional:
  `accessDenied`, `filteredRangeConflict`, `generalException`, `insertDeleteConflict`, `invalidArgument`,
  `invalidReference`, `itemAlreadyExists`, `itemNotFound`, `methodNotAllowed`, `nonBlankCellOffSheet`,
  `rangeExceedsLimit`, `requestAborted`, `unsupportedOperation`.

### 1.10 Python-Beispiel (Graph, Excel) – aus den Quellen abgeleitet, nicht ausgeführt

```python
# m365/graph_excel.py – Beispiel; Endpunkte/Header laut Graph-Doku (v1.0).
import time
import urllib.parse
import requests

GRAPH = "https://graph.microsoft.com/v1.0"


class GraphFehler(RuntimeError):
    def __init__(self, status: int, code: str, inner: str, retry_after: int | None):
        super().__init__(f"HTTP {status} {code}/{inner}")
        self.status, self.code, self.inner, self.retry_after = status, code, inner, retry_after


def _pruefe(antwort: requests.Response) -> dict:
    if antwort.ok:
        return antwort.json() if antwort.content else {}
    try:
        err = antwort.json().get("error", {})
    except ValueError:
        err = {}
    inner = (err.get("innerError") or {}).get("code", "")
    ra = antwort.headers.get("Retry-After")
    raise GraphFehler(antwort.status_code, err.get("code", ""), inner, int(ra) if ra else None)


class ExcelMappe:
    """Eine Arbeitsmappe in SharePoint/OneDrive for Business, adressiert per drive-id/item-id."""

    def __init__(self, token: str, drive_id: str, item_id: str):
        self.basis = f"{GRAPH}/drives/{drive_id}/items/{item_id}/workbook"
        self.s = requests.Session()
        self.s.headers.update({"Authorization": f"Bearer {token}"})
        self.session_id: str | None = None

    # --- Sessions ----------------------------------------------------------
    def __enter__(self):
        r = self.s.post(f"{self.basis}/createSession", json={"persistChanges": True}, timeout=60)
        if r.status_code == 504:                      # Doku: "repeat the request"
            r = self.s.post(f"{self.basis}/createSession", json={"persistChanges": True}, timeout=60)
        self.session_id = _pruefe(r)["id"]
        self.s.headers["workbook-session-id"] = self.session_id
        return self

    def __exit__(self, *exc):
        try:
            self.s.post(f"{self.basis}/closeSession", json={}, timeout=30)   # 204
        finally:
            self.s.headers.pop("workbook-session-id", None)
            self.session_id = None

    # --- Lesen / Schreiben -------------------------------------------------
    def _blatt(self, name: str) -> str:
        return urllib.parse.quote(name, safe="")      # [A] UTF-8-Prozentkodierung für Umlaute

    def used_range(self, blatt: str) -> dict:
        r = self.s.get(f"{self.basis}/worksheets/{self._blatt(blatt)}/usedRange(valuesOnly=true)", timeout=60)
        return _pruefe(r)                              # enthält address, values, text, formulas …

    def lese(self, blatt: str, adresse: str) -> dict:
        r = self.s.get(f"{self.basis}/worksheets/{self._blatt(blatt)}/range(address='{adresse}')", timeout=60)
        return _pruefe(r)

    def schreibe_werte(self, blatt: str, adresse: str, werte: list[list]) -> dict:
        """werte: 2-D-Matrix; None = Zelle unverändert lassen, "" = Zelle leeren (Doku: null/"")."""
        r = self.s.patch(
            f"{self.basis}/worksheets/{self._blatt(blatt)}/range(address='{adresse}')",
            json={"values": werte},
            timeout=60,
        )
        return _pruefe(r)

    def tabellenzeilen_anfuegen(self, tabelle: str, zeilen: list[list]) -> dict:
        r = self.s.post(f"{self.basis}/tables/{urllib.parse.quote(tabelle, safe='')}/rows",
                        json={"values": zeilen}, timeout=120)
        if r.status_code == 504:                       # Doku: wiederholen
            r = self.s.post(f"{self.basis}/tables/{urllib.parse.quote(tabelle, safe='')}/rows",
                            json={"values": zeilen}, timeout=120)
        return _pruefe(r)


def mit_wiederholung(aktion, versuche: int = 3):
    """Throttling/Transientes nach Doku: Retry-After beachten; Konflikte NICHT wiederholen."""
    for i in range(versuche):
        try:
            return aktion()
        except GraphFehler as e:
            if e.inner.lower() in {"accessconflict", "invalidsessionaccessconflict", "conflictuncategorized"}:
                raise                                   # Datei gesperrt – später erneut, nicht sofort
            if e.status in (429, 503) or e.inner.lower() in {"transientfailure", "tooManyRequestsUncategorized".lower()}:
                time.sleep(e.retry_after or 2 ** (i + 1))
                continue
            raise
    raise RuntimeError("zu viele Versuche")


# Beispiel: Fixtermin in Slot-Zeile 7 eintragen (Spalten D–K; A–C bleiben unberührt)
# with ExcelMappe(token, drive_id, item_id) as m:
#     m.schreibe_werte("Fixterminüberwachung", "D7:K7",
#                      [["Cide", "Rheinberg", "2026-10-18", None, False, False, False, "AB 12345"]])
#     # Hinweis [A]: Datumszellen entweder als Excel-Serienzahl senden oder zusätzlich numberFormat setzen.
```

Hinweis zu Datumswerten: [A] Sendet man einen ISO-String in `values`, legt Excel ihn als Text ab, sofern die Zelle
nicht bereits ein Datumsformat hat; Doku-Beispiel schreibt `"1/1/2016"` mit `numberFormat: "m-ddd"`. Für die
Spalten „Auftragsdatum/Verladedatum“ daher entweder Serienzahl (Tage seit 1899-12-30) senden oder `numberFormat`
mitsenden – im Test festlegen (Checkliste C3).

---

## 2. Authentifizierung für eine Desktop-App

### 2.1 App-Registrierung in Entra ID

**Verifiziert**

* [V] Entra Admin Center → **Identity > Applications > App registrations > New registration**; mindestens die Rolle
  **Cloud Application Administrator**; „Supported account types“: **Accounts in this organizational directory only**
  (Single-Tenant/LOB). Nach der Registrierung stehen **Application (client) ID** und (unter Overview) die Tenant-ID.
* [V] Plattform: **Authentication > Add a platform > Mobile and desktop applications**; für Desktop-Apps mit
  Systembrowser empfiehlt Microsoft `http://localhost` (für eingebetteten Browser
  `https://login.microsoftonline.com/common/oauth2/nativeclient`). MSAL-Doku: „Set the redirect URI of your app to
  `http://localhost` … for your app registration.“ Für den optionalen Windows-Broker (WAM) zusätzlich
  `ms-appx-web://Microsoft.AAD.BrokerPlugin/{client-id}`.
* [V] Device-Code-Flow setzt voraus, dass die App als *public client* gilt: Troubleshooting-Artikel AADSTS7000218 –
  unter **Authentication > Advanced settings** „Enable the following mobile and desktop flows“ (UI-Bezeichnung
  „Allow public client flows“) auf **Yes**; sonst „The request body must contain … 'client_assertion' or
  'client_secret'“.
* [V] Die PowerShell-SDK-Doku beschreibt genau diese Registrierung („Public client/native“, `http://localhost`,
  WAM-URI) als Weg, „to isolate and limit the consent permissions“, und ergänzt: Enterprise App → **Assignment
  required? = Yes** und nur bestimmte Benutzer zuweisen. Das ist für unseren Zweck sinnvoll (nur `j.duenschede`).

### 2.2 Benötigte delegierte Berechtigungen und Admin-Consent

**Verifiziert** (Graph-Permissions-Referenz, Spalte „Delegated“):

| Berechtigung | Zweck | AdminConsentRequired (delegiert) |
|---|---|---|
| `User.Read` | Anmeldung, Profil | No |
| `Files.ReadWrite` | Excel-API (least privileged für `createSession`, `range` PATCH, `tables/rows`), `/shares` | No |
| `Files.ReadWrite.All` | „all files the signed-in user can access“ (höher privilegiert für `/shares`) | No |
| `Sites.Read.All` | `GET /sites/{hostname}:/{path}` (least privileged für Site-Auflösung) | No |
| `Sites.ReadWrite.All` | nur falls Site-Schreibrechte nötig wären – hier nicht | No |
| `Mail.ReadWrite` | Entwürfe anlegen, Anhänge hinzufügen, Upload-Session | No |
| `Mail.Read` | Mails lesen, Anhänge laden (least privileged für `attachments/{id}/$value`) | No |
| `Mail.ReadBasic` | `GET /me/messages` least privileged (ohne Body/Anhänge) | No |
| `Mail.Read.Shared` / `Mail.ReadWrite.Shared` | Freigegebene Ordner/Postfächer von Kollegen | No |
| `offline_access` | Refresh-Token | No |

* [V] Tenant-Richtlinie: „By default, all users are allowed to consent to applications for permissions that don't
  require administrator consent.“ Alternativen: „Allow user consent for apps from verified publishers, for selected
  permissions“ (`microsoft-user-default-low`, nur als *low impact* klassifizierte Rechte) oder Consent komplett
  deaktiviert; dann Admin-Consent bzw. Admin-Consent-Workflow. „Applications that require users to be assigned …
  must have their permissions consented by an administrator.“ Einstellung: **Enterprise apps > Consent and permissions
  > User consent settings** (Global Administrator).
* [V] Excel-Operationen gibt es nur delegiert; ein „Daemon“ mit Application-Permission ist für die Excel-API
  **nicht** möglich. Für Mail wären Application-Permissions möglich, wären aber admin-consent-pflichtig und würden
  alle Postfächer umfassen – nicht nötig.

**Annahmen**

* [A] Welche Rechte für `Fixterminüberwachung.xlsx` in der SharePoint-Site „Buchhaltung“ tatsächlich reichen: Die
  Excel-API-Seiten nennen nur `Files.ReadWrite`; für Dateien, die nicht im eigenen OneDrive liegen, kann die
  Auflösung über `/sites/…` `Sites.Read.All` erfordern. Empfehlung: `Files.ReadWrite` + `Sites.Read.All`
  beantragen; falls Zugriff verweigert wird, `Files.ReadWrite.All` ergänzen (alle drei ohne Admin-Consent laut Referenz).
* [A] Scopes werden in MSAL als Liste kurzer Graph-Scopes angegeben (`["Files.ReadWrite", "Sites.Read.All",
  "Mail.ReadWrite", "Mail.Read", "User.Read"]`); `offline_access`, `openid`, `profile` ergänzt MSAL selbst
  (so verhält sich MSAL Python; in den gelesenen Seiten nicht explizit belegt – bei `ValueError` zu reservierten Scopes
  diese aus der Liste entfernen).
* [NF] Ob im Tenant des Anwenders User-Consent erlaubt ist, zeigt der erste Anmeldeversuch (Meldung „Need admin
  approval“ → Admin-Consent einholen). Prüfauftrag A3.

### 2.3 Token-Erwerb mit MSAL (Python)

**Verifiziert**

* [V] `msal.PublicClientApplication(client_id, client_credential=None, *, enable_broker_on_windows=None, …, **kwargs)`;
  weitere Parameter des Basiskonstruktors: `authority`, `token_cache`. Aktuelle Version auf PyPI: msal 1.39.0.
* [V] Ablauf laut Doku: `accounts = app.get_accounts()` → `app.acquire_token_silent(scopes, account=accounts[0])`
  („finds a valid access token from cache, or a valid refresh token from cache and then automatically uses it to redeem a
  new access token“) → bei `None` interaktiv.
* [V] Interaktiv: `app.acquire_token_interactive(scopes, prompt=None, login_hint=None, timeout=None, port=None, …)`
  („It automatically provides PKCE protection“; Redirect `http://localhost`, Port systemvergeben oder `port=`).
  Optionaler Broker: `enable_broker_on_windows=True`, `pip install "msal[broker]>=1.33,<2"`, zusätzliche Redirect-URI,
  bei Konsolen-Apps `parent_window_handle=msal.PublicClientApplication.CONSOLE_WINDOW_HANDLE`.
* [V] Device-Code: `flow = app.initiate_device_flow(scopes=…)` → `flow["message"]` anzeigen (enthält
  `verification_uri` + `user_code`) → `result = app.acquire_token_by_device_flow(flow)` (blockiert bis Anmeldung;
  abbrechbar über `flow["expires_at"] = 0`). Protokoll: Benutzer hat 15 Minuten; Fehler `authorization_pending`,
  `authorization_declined`, `bad_verification_code`, `expired_token`; Refresh-Token nur mit `offline_access`.
* [V] Ergebnis: Dict mit `access_token` (Erfolg) bzw. `error`, `error_description`, `correlation_id`.
* [V] Token-Cache-Persistenz ist in MSAL nicht „out of the box“; für Desktop-Apps: Cache in Datei serialisieren
  (`SerializableTokenCache`) **mit Dateisperre**, oder `msal-extensions` verwenden.

### 2.4 Verschlüsselter Token-Cache mit `msal-extensions`

**Verifiziert** (README + Quellcode `msal_extensions/persistence.py`, `token_cache.py`, Version 1.3.1,
benötigt `msal>=1.29,<2`)

* [V] `build_encrypted_persistence(location)` wählt nach Plattform: Windows → `FilePersistenceWithDataProtection`
  (DPAPI, `is_encrypted = True`), macOS → `KeychainPersistence`, Linux → `LibsecretPersistence`; sonst `RuntimeError`.
  Unverschlüsselter Fallback: `FilePersistence(location)` (`is_encrypted = False`).
* [V] `PersistedTokenCache(persistence, lock_location=None)` erbt von `msal.SerializableTokenCache`, lädt bei Bedarf
  neu (`_reload_if_necessary`) und sperrt Schreibzugriffe mit `CrossPlatLock` (Dateisperre; `portalocker`-Extra
  optional). Übergabe an MSAL: `msal.PublicClientApplication(client_id, token_cache=cache)`.
* [V] README-Beispiel (sinngemäß, `fallback_to_plaintext` standardmäßig `False`):

```python
# m365/auth.py – msal 1.39 + msal-extensions 1.3.1 (API laut README/Quelle)
import os
import sys
import msal
from msal_extensions import build_encrypted_persistence, FilePersistence, PersistedTokenCache

CLIENT_ID = "<Application (client) ID der eigenen Registrierung>"
TENANT_ID = "<Directory (tenant) ID>"
AUTHORITY = f"https://login.microsoftonline.com/{TENANT_ID}"
SCOPES = ["Files.ReadWrite", "Sites.Read.All", "Mail.ReadWrite", "Mail.Read", "User.Read"]  # [A] Kurzform


def _cache_pfad() -> str:
    basis = os.environ.get("APPDATA") or os.path.expanduser("~")          # [A] Ablageort ist Designentscheidung
    ordner = os.path.join(basis, "Auftragsautomat")
    os.makedirs(ordner, exist_ok=True)
    return os.path.join(ordner, "msal_token_cache.bin")


def _persistence(location: str, fallback_to_plaintext: bool = False):
    try:
        return build_encrypted_persistence(location)                      # Windows: DPAPI
    except Exception:
        if not fallback_to_plaintext:
            raise
        return FilePersistence(location)                                  # Klartext – nur für Tests


def hole_token(interaktiv_erlaubt: bool = True, device_code: bool = False) -> str:
    cache = PersistedTokenCache(_persistence(_cache_pfad()))
    app = msal.PublicClientApplication(CLIENT_ID, authority=AUTHORITY, token_cache=cache)

    result = None
    accounts = app.get_accounts()
    if accounts:
        result = app.acquire_token_silent(SCOPES, account=accounts[0])
    if not result:
        if not interaktiv_erlaubt:
            raise RuntimeError("Anmeldung erforderlich – bitte 'auftragsautomat login' am PC ausführen")
        if device_code:
            flow = app.initiate_device_flow(scopes=SCOPES)
            if "user_code" not in flow:
                raise RuntimeError(f"Device-Flow fehlgeschlagen: {flow}")
            print(flow["message"]); sys.stdout.flush()
            result = app.acquire_token_by_device_flow(flow)
        else:
            result = app.acquire_token_interactive(SCOPES, login_hint="j.duenschede@weinstrasseadolph.de")
    if "access_token" not in result:
        raise RuntimeError(f"{result.get('error')}: {result.get('error_description')} ({result.get('correlation_id')})")
    return result["access_token"]
```

**Annahmen**

* [A] Täglicher Lauf per Aufgabenplanung: `acquire_token_silent` erneuert per Refresh-Token ohne Interaktion. Die
  Lebensdauer von Refresh-Tokens und Conditional-Access-Regeln (z. B. Anmeldehäufigkeit) sind tenantabhängig; der
  Lauf muss „interaction required“ sauber melden (Bericht + Erinnerung), nicht abstürzen. Prüfauftrag A5.
* [A] DPAPI bindet den Cache an das Windows-Benutzerkonto; der geplante Task muss unter demselben Konto laufen wie
  die interaktive Anmeldung.

### 2.5 Alternativen ohne eigene App-Registrierung (First-Party-Client-IDs)

**Verifiziert**

* [V] Microsoft listet `14d82eec-204b-4c2f-b7e8-296a70dab67e` als „Microsoft Graph Command Line Tools“ unter „Microsoft
  tenant-owned applications“ (Support-Artikel „Verify first-party Microsoft applications in sign-in reports“). Die
  Graph-PowerShell-Doku nennt die Standard-App und beschreibt ausdrücklich die eigene Registrierung („Use this approach
  if you need to isolate and limit the consent permissions granted for Microsoft Graph PowerShell usage“); in
  Sovereign Clouds ist die Standard-App gar nicht nutzbar.
* [V] Bekanntes Problem (Graph Known Issues): Die Consent-Seite der PowerShell/CLI-App zeigt „unverified publisher“;
  Microsofts Workaround ist eine eigene App-Registrierung mit eigener Publisher-Verifikation.
* [V-Dritt] Community-Modul „MgGraphCommunity“ nutzt 14d82eec… als Standard für Device-Code; Blogs (benroberts.io,
  office365itpros 2026) raten Admins, die Standard-App auf zugewiesene Benutzer zu beschränken, weil sich dort
  „permissions creep“ ansammelt; Conditional Access kann die App blockieren (Microsoft Q&A).
* [NF] Eine Microsoft-Aussage, dass Fremdskripte diese Client-ID (oder die Azure-CLI-ID `04b07795-…`) verwenden
  dürfen, wurde nicht gefunden; die Azure-CLI-ID steht nicht in der gelesenen Liste.

**Bewertung (Annahme/Empfehlung)**

* [A] Technisch funktioniert Device-Code mit 14d82eec… in vielen Tenants, aber: (1) nicht dokumentiert/supportet für
  Dritt-Skripte; (2) Consent landet auf einer App, die der Tenant nicht besitzt – Admins sperren sie zunehmend
  („Assignment required“), dann bricht unser Tool ohne Vorwarnung; (3) Audit-/Sign-in-Logs ordnen unsere Aktionen
  „Microsoft Graph Command Line Tools“ zu; (4) keine Kontrolle über Redirect-URIs/Broker. Die eigene Registrierung
  kostet etwa zehn Minuten und braucht nach Referenz keinen Admin-Consent. → **Eigene Registrierung; First-Party-IDs
  nicht verwenden.** Falls der Anwender keine Rechte zum Registrieren hat (Rolle Cloud Application Administrator oder
  „Users can register applications = Yes“), ist das der einzige Punkt, an dem der IT-Admin gebraucht wird.

---

## 3. Datei adressieren (Drive-ID/Item-ID aus der SharePoint-URL)

**Verifiziert**

* [V] Weg A – Freigabe-/Datei-URL kodieren (`/shares`): 1) URL Base64-kodieren, 2) in unpadded base64url wandeln
  (`=` am Ende entfernen, `/`→`_`, `+`→`-`), 3) `u!` voranstellen. `GET /shares/{encoded}/driveItem` liefert das
  `driveItem` der Datei; Header `Prefer: redeemSharingLink` (dauerhafter Zugriff) bzw. `redeemSharingLinkIfNecessary`
  (nur für diese Anfrage). Least privileged delegiert: `Files.ReadWrite`. Für OneDrive for Business/SharePoint ist
  immer Authentifizierung nötig.
* [V] Weg B – deterministisch über die Site: `GET /sites/{hostname}:/{server-relative-path}` (z. B.
  `/sites/contoso.sharepoint.com:/sites/Buchhaltung`) → `site.id`; `GET /sites/{siteId}/drives` → Dokumentbibliotheken
  (`id`, `name`, `driveType`, `webUrl`; System-Bibliotheken nur mit `$select=…,system`); `GET /drives/{drive-id}/root:/{item-path}`
  → `driveItem` (`id`, `eTag`, `cTag`, `webUrl`, `lastModifiedDateTime`); alternativ `GET /sites/{site-id}/drive/root:/{item-path}`
  für die Standardbibliothek. `GET /sites/root` bzw. `/sites/contoso.sharepoint.com` liefert die Root-Site.
* [V] ID-basierte Adressierung ist stabil: „Renaming or moving the item will not change the item's ID.“ Pfadbasierte
  Adressierung bricht bei Umbenennen/Verschieben.
* [V] Pfadkodierung: jedes Segment einzeln prozentkodieren (RFC 3986), nicht die ganze URL; `" "`→`%20`, `"#"`→`%23`;
  Nicht-ASCII-Zeichen als UTF-8-Prozentkodierung („% and then the UTF-8 character code“). In OneDrive for Business
  sind `/ \ * < > ? : | # %` in Datei-/Ordnernamen verboten; Ordnernamen enden nicht mit `.`, Namen beginnen nicht mit `~`.

**Annahmen**

* [A] Die Datei-URL aus dem Browser („Link kopieren“ oder Adresszeile, oft `…/:x:/r/sites/Buchhaltung/…` oder
  `…/Freigegebene%20Dokumente/General/Aufträge/Fixterminüberwachung.xlsx?web=1`) funktioniert in `/shares/u!…` in der
  Praxis meist, ist aber in der Doku als „sharing URL“ beschrieben; ein Tech-Community-Thread berichtet von Fehlern.
  Weg B ist robuster: `webUrl` der Datei kennt Hostname, Site-Pfad und Bibliotheksname.
* [A] Bibliotheksname: Die Standardbibliothek heißt in der URL „Shared Documents“/„Freigegebene Dokumente“, als Drive
  `name` meist „Documents“/„Dokumente“; daher `drives` auflisten und per `webUrl`-Präfix zuordnen statt zu raten.
* [A] `parentReference.driveId` und `id` des `driveItem` sind die beiden Werte, die in `config/settings.yaml`
  abgelegt werden sollten (einmal auflösen, dann nur noch IDs verwenden).

```python
# m365/adressen.py – Auflösung Web-URL -> (drive_id, item_id); Endpunkte laut Graph-Doku
import base64
import urllib.parse
import requests

GRAPH = "https://graph.microsoft.com/v1.0"


def share_token(url: str) -> str:
    b64 = base64.b64encode(url.encode("utf-8")).decode("ascii")
    return "u!" + b64.rstrip("=").replace("/", "_").replace("+", "-")


def ueber_shares(token: str, datei_url: str) -> tuple[str, str]:
    r = requests.get(f"{GRAPH}/shares/{share_token(datei_url)}/driveItem",
                     headers={"Authorization": f"Bearer {token}", "Prefer": "redeemSharingLinkIfNecessary"},
                     timeout=60)
    r.raise_for_status()
    item = r.json()
    return item["parentReference"]["driveId"], item["id"]       # [A] Feldnamen laut driveItem-Schema


def ueber_site(token: str, hostname: str, site_pfad: str, bibliothek_weburl_praefix: str,
               datei_pfad: str) -> tuple[str, str]:
    h = {"Authorization": f"Bearer {token}"}
    site = requests.get(f"{GRAPH}/sites/{hostname}:{site_pfad}", headers=h, timeout=60); site.raise_for_status()
    drives = requests.get(f"{GRAPH}/sites/{site.json()['id']}/drives", headers=h, timeout=60); drives.raise_for_status()
    drive = next(d for d in drives.json()["value"] if d["webUrl"].startswith(bibliothek_weburl_praefix))
    # Pfadsegmente einzeln kodieren (Umlaute -> UTF-8 %XX), Schrägstriche behalten:
    pfad = "/".join(urllib.parse.quote(seg, safe="") for seg in datei_pfad.split("/"))
    item = requests.get(f"{GRAPH}/drives/{drive['id']}/root:/{pfad}", headers=h, timeout=60); item.raise_for_status()
    return drive["id"], item.json()["id"]

# Beispiel:
# ueber_site(token, "weinstrasse.sharepoint.com", "/sites/Buchhaltung",
#            "https://weinstrasse.sharepoint.com/sites/Buchhaltung/Freigegebene%20Dokumente",
#            "General/Aufträge/Fixterminüberwachung.xlsx")      # [A] Hostname/Site-Pfad beim Anwender erfragen
```

---

## 4. Outlook per Microsoft Graph

### 4.1 Entwurf anlegen

**Verifiziert**

* [V] `POST /me/messages` (auch `/users/{id|upn}/messages`, `/me/mailFolders/{id}/messages`) mit JSON-`message`:
  `subject`, `body: { contentType: "HTML", content: "…" }`, `toRecipients: [{ emailAddress: { address } }]`,
  `ccRecipients`, `bccRecipients`, `importance`, optional `internetMessageHeaders` (Namen müssen mit `x-` beginnen; nur
  beim Erstellen setzbar). „By default, this operation saves the draft in the Drafts folder.“ Antwort `201 Created`
  mit `id`, `isDraft: true`, `webLink`, `conversationId`, `internetMessageId`. Berechtigung `Mail.ReadWrite`.
* [V] Alternativ MIME (Base64 im Body, `Content-Type: text/plain`); S/MIME-Payload ≤ 4 MB.
* [V] Spätere Änderung des Entwurfs: `PATCH /me/messages/{id}` („Update the draft later to add content to the body or
  change other message properties“); Senden wäre `POST …/send` – im Projekt ausdrücklich nicht genutzt.
* [V] Antworten/Weiterleiten als Entwurf: `createReply`, `createReplyAll`, `createForward` (Known Issue: der
  `comment`-Parameter landet nicht im Body des Entwurfs).

### 4.2 Anhänge

**Verifiziert**

* [V] < 3 MB: `POST /me/messages/{id}/attachments` mit
  `{ "@odata.type": "#microsoft.graph.fileAttachment", "name": "AB-….pdf", "contentBytes": "<base64>" }`
  (optional `contentType`); „This operation limits the size of the attachment you can add to under 3 MB.“ → `201`.
* [V] 3–150 MB: `POST /me/messages/{id}/attachments/createUploadSession` mit
  `{ "AttachmentItem": { "attachmentType": "file", "name": "…", "size": <Bytes> } }` → `201` mit `uploadUrl`
  (vorauthentifiziert, Domäne `outlook.office.com`), `expirationDateTime`, `nextExpectedRanges: ["0-"]`. Dann `PUT
  {uploadUrl}` je Chunk mit `Content-Type: application/octet-stream`, `Content-Length`, `Content-Range: bytes
  {start}-{end}/{total}`; **keinen** `Authorization`-Header senden; Chunks „less than 4 MB“ halten; Bytes in Reihenfolge.
  Letzter `PUT` → `201 Created` mit `Location`-Header (enthält die Attachment-ID). Abbruch: `DELETE {uploadUrl}` → `204`.
  Fehler `ErrorAttachmentSizeShouldNotBeLessThanMinimumSize`, wenn eine Upload-Session für < 3 MB versucht wird.
* [V] Known Issue: Mit delegierten Rechten funktioniert `createUploadSession` nur, wenn die Nachricht im Postfach des
  angemeldeten Benutzers liegt (nicht in freigegebenen/delegierten Postfächern).
* [V] Outlook-Limit: 150 MB Upload (PATCH/POST/PUT) je 5 Minuten je App+Postfach.

**Annahmen**

* [A] Unsere PDFs (Bestellung, AB, Speditionsauftrag, Lieferschein) liegen praktisch immer unter 3 MB → einfacher
  `fileAttachment`-POST; die Upload-Session nur als Fallback implementieren (Größe vorher per `os.path.getsize` prüfen).

### 4.3 Entwurf später öffnen (`webLink`)

**Verifiziert**

* [V] `webLink`: „The URL to open the message in Outlook on the web. You can append an `ispopout` argument … If
  `ispopout` is set to `0`, the browser shows the message in the Outlook on the web review pane. … This URL cannot be
  accessed from within an iFrame.“ Bei Delegation müssen beide Postfächer in derselben Datenbankregion liegen.
* [V] `id` einer Nachricht ändert sich beim Verschieben zwischen Ordnern, sofern nicht `Prefer: IdType="ImmutableId"`
  verwendet wird (für die Merkdatei `logs/nachfragen.json` relevant: ImmutableId verwenden oder `internetMessageId`
  speichern).

**Annahmen**

* [A] Es gibt keinen Graph-Deep-Link in Outlook **Desktop**; der Entwurf erscheint dort nach der Synchronisation im
  Ordner „Entwürfe“ (Exchange-Cache). In der Streamlit-Freigabe daher: Betreff + `webLink` (öffnet OWA) anzeigen und
  den Hinweis „in Outlook unter Entwürfe“. Per COM ließe sich ein Entwurf zusätzlich per `Display()` öffnen (dann
  `EntryID`-Zuordnung nötig) – nur im lokalen Backend.
* [A] Zur eindeutigen Wiedererkennung eigener Entwürfe beim Erstellen einen Header `x-auftragsautomat-vorgang: <id>`
  setzen (Feature verifiziert, Verhalten bei Entwürfen nicht ausdrücklich dokumentiert) **und** zusätzlich die
  `id`/`internetMessageId` in `logs/` speichern.

### 4.4 Mails suchen (`$filter`, `$search`), Anhänge laden

**Verifiziert**

* [V] `GET /me/messages` bzw. `GET /me/mailFolders/{id}/messages` (`inbox` als bekannter Ordnername über
  `mailFolders('Inbox')` in der Shared-Folder-Doku); Standard-Seitengröße 10, `$top` 1–1000, `@odata.nextLink` komplett
  weiterverwenden (nicht `$skip` manipulieren); `$select` für Antwortzeit; Body kommt nur als HTML, außer Header
  `Prefer: outlook.body-content-type="text"`. Least privileged `Mail.ReadBasic`, mit Body/Anhängen `Mail.Read`.
* [V] `$filter` + `$orderby` gemeinsam: Eigenschaften aus `$orderby` müssen auch in `$filter` stehen, in gleicher
  Reihenfolge und vor den übrigen – sonst `InefficientFilter` („The restriction or sort order is too complex“).
* [V] `$search` auf Nachrichten: Standardfelder `from`, `subject`, `body`; KQL-Eigenschaften `from`, `to`, `cc`,
  `participants`, `subject`, `body`, `attachment` (Dateiname), `hasAttachments`, `received` (Beispiel
  `received:07/23/2018`), `sent`, `importance`, `size`, `kind`; Ergebnis nach Sendedatum sortiert, bis zu 1.000 Treffer;
  z. B. `$search="from:def-order@hillebrandgori.com"`, `$search="subject:Auftragsbestätigung"`,
  `$search="from:adelev OR from:alexw"`.
* [V] Anhänge: `GET /me/messages/{id}/attachments` (Liste; `$filter` wird am `/attachments`-Endpunkt ignoriert) und
  `GET /me/messages/{id}/attachments/{id}` (Metadaten inkl. `contentBytes` Base64) bzw. `…/attachments/{id}/$value`
  (Rohbytes, `Content-Type` des Originals). `hasAttachments` zählt Inline-Bilder nicht mit.
* [V] Limits: 10.000 Requests / 10 Minuten je App+Postfach, 4 gleichzeitige Requests, JSON-Batch führt höchstens 4
  Teilanfragen parallel aus; `429` mit `Retry-After`.

**Annahmen**

* [A] Datumsbereiche: belegt ist nur `$filter=receivedDateTime ge 2026-10-11T00:00:00Z` (OData, UTC-Zeitstempel);
  KQL-Bereiche wie `received>=2026-10-11` sind KQL-Syntax und wurden auf der Graph-Seite nicht gezeigt. Für die
  7-Tage-Suche daher `$filter` verwenden, `$search` nur für Freitext.
* [A] `$search` lässt sich auf `messages` nicht mit `$filter`/`$orderby` kombinieren (allgemein bekannte Einschränkung,
  in den gelesenen Seiten nicht wörtlich belegt) → im Code getrennt halten.
* [A] Für Hillebrand-Bestätigungen reicht `$filter` auf `from/emailAddress/address eq '…'` plus
  `receivedDateTime ge …` und lokales Regex `Our Ref: (DEMB\d+)` im `subject`/`bodyPreview`.

### 4.5 Postfach eines Kollegen / Shared Mailbox

**Verifiziert**

* [V] Delegierte Rechte `Mail.Read.Shared` bzw. `Mail.ReadWrite.Shared`; Voraussetzung ist eine Outlook-/Exchange-
  Freigabe des Ordners oder Delegation des Postfachs („If Garth hasn't shared his Inbox with John, nor has he delegated
  his mailbox to John, specifying Garth's user ID … return an error“). Aufrufe:
  `GET /users/{upn}/mailFolders('Inbox')/messages`, `…/messages/{id}`, `…/mailFolders('Inbox')`.
* [V] Mit `.Shared`-Rechten keine Change Notifications; dafür wäre Application-`Mail.Read` nötig.

**Annahmen**

* [A] Für CC-Kopien an `b.mietzen@…` ist kein Shared-Zugriff nötig (Entwurf im eigenen Postfach). Shared-Rechte nur
  beantragen, wenn Bestätigungen in einem anderen Postfach (z. B. `info@`) eingehen.

### 4.6 Python-Beispiel (Graph, Mail) – aus den Quellen abgeleitet, nicht ausgeführt

```python
# m365/graph_mail.py
import base64
import os
import requests

GRAPH = "https://graph.microsoft.com/v1.0"
CHUNK = 3 * 1024 * 1024          # < 4 MB laut Doku


def erstelle_entwurf(token: str, betreff: str, html: str, an: list[str], cc: list[str] = (),
                     vorgang_id: str | None = None) -> dict:
    body = {
        "subject": betreff,
        "body": {"contentType": "HTML", "content": html},
        "toRecipients": [{"emailAddress": {"address": a}} for a in an],
        "ccRecipients": [{"emailAddress": {"address": a}} for a in cc],
    }
    if vorgang_id:
        body["internetMessageHeaders"] = [{"name": "x-auftragsautomat-vorgang", "value": vorgang_id}]  # [A]
    r = requests.post(f"{GRAPH}/me/messages", json=body,
                      headers={"Authorization": f"Bearer {token}"}, timeout=60)
    r.raise_for_status()
    return r.json()               # id, isDraft, webLink


def haenge_datei_an(token: str, message_id: str, pfad: str) -> None:
    groesse = os.path.getsize(pfad)
    name = os.path.basename(pfad)
    h = {"Authorization": f"Bearer {token}"}
    if groesse < 3 * 1024 * 1024:
        with open(pfad, "rb") as f:
            inhalt = base64.b64encode(f.read()).decode("ascii")
        r = requests.post(f"{GRAPH}/me/messages/{message_id}/attachments",
                          json={"@odata.type": "#microsoft.graph.fileAttachment", "name": name,
                                "contentBytes": inhalt}, headers=h, timeout=120)
        r.raise_for_status()
        return
    r = requests.post(f"{GRAPH}/me/messages/{message_id}/attachments/createUploadSession",
                      json={"AttachmentItem": {"attachmentType": "file", "name": name, "size": groesse}},
                      headers=h, timeout=60)
    r.raise_for_status()
    upload_url = r.json()["uploadUrl"]
    with open(pfad, "rb") as f:
        start = 0
        while start < groesse:
            teil = f.read(CHUNK)
            ende = start + len(teil) - 1
            p = requests.put(upload_url, data=teil, timeout=300,
                             headers={"Content-Type": "application/octet-stream",
                                      "Content-Length": str(len(teil)),
                                      "Content-Range": f"bytes {start}-{ende}/{groesse}"})   # kein Authorization!
            p.raise_for_status()
            start = ende + 1


def suche_mails(token: str, seit_iso_utc: str, absender: str | None = None, top: int = 50) -> list[dict]:
    flt = f"receivedDateTime ge {seit_iso_utc}"
    if absender:
        flt += f" and from/emailAddress/address eq '{absender}'"
    params = {"$filter": flt, "$orderby": "receivedDateTime desc", "$top": str(top),
              "$select": "id,subject,from,receivedDateTime,hasAttachments,bodyPreview,internetMessageId"}
    h = {"Authorization": f"Bearer {token}", "Prefer": 'IdType="ImmutableId"'}
    url = f"{GRAPH}/me/mailFolders/inbox/messages"
    treffer: list[dict] = []
    while url:
        r = requests.get(url, params=params, headers=h, timeout=60); r.raise_for_status()
        daten = r.json(); treffer += daten["value"]; url = daten.get("@odata.nextLink"); params = None
    return treffer


def lade_anhaenge(token: str, message_id: str, zielordner: str) -> list[str]:
    h = {"Authorization": f"Bearer {token}"}
    r = requests.get(f"{GRAPH}/me/messages/{message_id}/attachments?$select=id,name,contentType,size",
                     headers=h, timeout=60); r.raise_for_status()
    pfade = []
    for a in r.json()["value"]:
        if a.get("@odata.type") != "#microsoft.graph.fileAttachment":
            continue
        raw = requests.get(f"{GRAPH}/me/messages/{message_id}/attachments/{a['id']}/$value", headers=h, timeout=120)
        raw.raise_for_status()
        ziel = os.path.join(zielordner, a["name"]); open(ziel, "wb").write(raw.content); pfade.append(ziel)
    return pfade
```

---

## 5. Alternative lokal per COM (pywin32)

### 5.1 Outlook: Entwurf anlegen, Posteingang lesen

**Verifiziert** (VBA-Objektmodell; pywin32 bildet dieselben Member ab)

* [V] `MailItem.Save`: „Saves the Microsoft Outlook item to the current folder or, if this is a new item, to the Outlook
  default folder for the item type“ – für eine neue Mail also „Entwürfe“. Einschränkung: bei Inline-Antworten kann
  `Save` fehlschlagen.
* [V] `Items.Restrict(filter)`: Jet-Syntax `[ReceivedTime] >= '…'` (Datum als formatierter String) oder DASL
  `@SQL=…` mit Property-URIs, z. B. `"urn:schemas:httpmail:subject"` und Operatoren `ci_phrasematch`/`ci_startswith`
  (Teilstring-Suche im Betreff – Jet-Syntax kann kein „contains“). Nicht filterbar u. a. `Body`, `HTMLBody`, `EntryID`,
  `Saved`, `Sent`, `ConversationIndex`. `Restrict` ist bei großen Ordnern deutlich schneller als `Find`.
* [V] Objektmodellschutz („Ein Programm versucht, auf E-Mail-Adressinformationen zuzugreifen …“ = Address Book Warning):
  Ausgelöst, wenn **nicht vertrauenswürdiger** Code eine geschützte Eigenschaft **liest** oder geschützte Methoden ruft.
  Für `MailItem` geschützt: `Bcc`, `Body`, `Cc`, `HTMLBody`, `PropertyAccessor`, `ReceivedByName`,
  `ReceivedOnBehalfOfName`, `Recipients`, `ReplyRecipientNames`, `RTFBody`, `Sender`, `SenderEmailAddress`,
  `SenderEmailType`, `SenderName`, `SentOnBehalfOfName`, `To`; Methoden `SaveAs`, `Send` (Send-Warnung mit 5-s-Timer).
  `Recipient`/`Recipients`: jede Eigenschaft/Methode. **Nicht** in der Liste: `Subject`, `Save`, `Attachments.Add`,
  `Attachment.SaveAsFile`, `Items.Restrict`, `Folder.Items`, `ReceivedTime`, `EntryID`.
* [V] Trust-Regeln: In-Process-COM-Add-ins mit dem übergebenen `Application`-Objekt sind vertraut; „if you create a new
  **Application** object by using the **CreateObject** method, that object and any of its subordinate objects …
  are not trusted.“ Cross-Process-Aufrufer (unser Python-Prozess) laufen **ohne** Warnungen, wenn Windows-
  Sicherheitscenter ein Antivirus mit Status „Good“ meldet **und** Outlook Standard-Sicherheitseinstellungen oder eine
  GPO „warn when antivirus inactive/out of date“ verwendet. Trust Center → **Programmatic Access**: „Warn me … when my
  antivirus software is inactive or out-of-date (recommended)“ (Standard), „Always warn me“, „Never warn me (not
  recommended)“; nur lokale Administratoren können die Einstellung ändern; GPO oder Exchange-Sicherheitsformular
  können sie überschreiben (dann nur „prompt / allow / deny“).
* [V] Wird die Warnung mit **Deny** beantwortet, liefert Outlook `MAPI_E_NOT_SUPPORTED` (COM-Fehler in Python); **Allow**
  ohne Zeitraum erlaubt nur diesen einen Aufruf; **Allow** mit Zeitraum (Dropdown) gilt für alle Aufrufer bis Ablauf.
* [V-Dritt] Registry/GPO-Schlüssel (Slipstick, Q&A): `HKCU\Software\Policies\Microsoft\Office\16.0\Outlook\Security`
  `AdminSecurityMode=3`, `PromptOOMSend/PromptOOMAddressBookAccess…=2`, `ObjectModelGuard=2`; bei Click-to-Run anderer
  Pfad; Werte „verschwinden“ teils wieder. Nicht empfohlen (senkt Sicherheit); nur als Hinweis an den IT-Admin.

**Annahmen**

* [A] pywin32-Aufrufe entsprechen 1:1 dem VBA-Modell: `win32com.client.Dispatch("Outlook.Application")`,
  `.GetNamespace("MAPI")`, `.GetDefaultFolder(6)` (olFolderInbox = 6), `.CreateItem(0)` (olMailItem = 0),
  `.Attachments.Add(pfad)`, `.Save()`. Konstanten als Zahlen angeben (oder `win32com.client.constants` nach
  `gencache.EnsureDispatch`). Standardwissen, nicht aus den gelesenen Seiten.
* [A] **Setzen** von `To`/`CC`/`HTMLBody` beim Entwurf löst keine Warnung aus (die Doku spricht von „performs a get“);
  **Lesen** von `SenderEmailAddress`/`Body`/`HTMLBody` eingehender Mails (für `eingang.py`) löst sie ohne „gutes“
  Antivirus aus. Daher: lokales Backend nur für Entwürfe, nicht für den Posteingang – oder Posteingang per Graph.
* [A] **Neues Outlook (Monarch)** unterstützt das COM-Objektmodell nicht; nur das klassische Outlook. Beim Anwender
  prüfen (Checkliste D1). Nicht in den gelesenen Seiten belegt.
* [A] Outlook muss laufen oder wird durch `Dispatch` gestartet; bei nicht angemeldetem Profil erscheint ein
  Profil-Dialog. Für einen unbeaufsichtigten Tagelauf ungeeignet, sobald Outlook nicht offen ist.

```python
# m365/lokal.py – Outlook-Entwurf per COM (Member laut VBA-Doku; [A] pywin32-Abbildung); nur Windows
import win32com.client  # pywin32 312

OL_MAIL_ITEM, OL_FOLDER_INBOX = 0, 6


def erstelle_entwurf_com(betreff: str, html: str, an: list[str], cc: list[str], anhaenge: list[str]) -> str:
    outlook = win32com.client.Dispatch("Outlook.Application")        # CreateObject-Äquivalent -> "untrusted"
    mail = outlook.CreateItem(OL_MAIL_ITEM)
    mail.Subject = betreff                                            # nicht geschützt
    mail.To = "; ".join(an)                                           # Setzen; [A] keine Warnung beim Schreiben
    mail.CC = "; ".join(cc)
    mail.HTMLBody = html
    for p in anhaenge:
        mail.Attachments.Add(p)                                       # nicht geschützt
    mail.Save()                                                       # neue Mail -> Ordner "Entwürfe"
    return mail.EntryID                                               # [A] EntryID zum Wiederfinden/Display()


def hillebrand_bestaetigungen_com(seit: "datetime") -> list[dict]:
    ns = win32com.client.Dispatch("Outlook.Application").GetNamespace("MAPI")
    items = ns.GetDefaultFolder(OL_FOLDER_INBOX).Items
    items.Sort("[ReceivedTime]", True)
    # Jet-Syntax laut Doku: Datum als String im Outlook-Format; [A] Format "dd.MM.yyyy HH:mm" im deutschen Gebietsschema
    gefiltert = items.Restrict(f"[ReceivedTime] >= '{seit.strftime('%d.%m.%Y %H:%M')}'")
    # DASL-Teilstringsuche im Betreff (ci_phrasematch laut Doku):
    gefiltert = gefiltert.Restrict('@SQL="urn:schemas:httpmail:subject" ci_phrasematch \'Our Ref\'')
    treffer = []
    for it in gefiltert:
        # Subject/ReceivedTime ungeschützt; SenderEmailAddress/Body sind geschützt -> Warnung möglich
        treffer.append({"betreff": it.Subject, "empfangen": it.ReceivedTime, "entry_id": it.EntryID})
    return treffer
```

### 5.2 Excel per COM auf dem OneDrive-synchronisierten Pfad

**Verifiziert**

* [V] `Workbooks.Open(FileName, UpdateLinks, ReadOnly, …, Notify, …)`: `ReadOnly=True` öffnet schreibgeschützt;
  `Notify=True`: „If the file cannot be opened in read/write mode … Microsoft Excel will open the file as read-only,
  poll the file notification list, and then notify the user when the file becomes available. If this argument is
  False or omitted, no notification is requested, and any attempts to open an unavailable file will fail.“
  `UpdateLinks=0` unterdrückt die Link-Abfrage; Makros sind beim programmatischen Öffnen standardmäßig aktiv
  (`Application.AutomationSecurity`).
* [V] `Workbook.ReadOnly` ist `True`, wenn die Mappe schreibgeschützt geöffnet wurde → vor dem Schreiben prüfen.
* [V] Microsoft-Support: Eine Datei ist „locked for editing“, wenn nur ein Beteiligter eine Excel-Version ohne
  Co-Authoring nutzt oder die Datei an einem Ort liegt, der Co-Authoring nicht unterstützt (lokale Ordner, andere
  Sync-Dienste).
* [V-Dritt] xlwings-Doku/Q&A: Bei OneDrive/SharePoint-Dateien mit **AutoSave** liefern `Workbook.FullName`/`Path`
  eine `https://…`-URL statt des lokalen Pfads; `Workbooks.Open` erwartet den lokalen Sync-Pfad
  (`C:\Users\<Name>\<Tenant>\…`), keinen Browser-Link. Es gibt keinen offiziellen Schalter, der immer lokale Pfade liefert.

**Annahmen**

* [A] COM-Schreiben auf den synchronisierten Pfad geht durch den OneDrive-Client: Ist die Datei bei einem Kollegen in
  Excel Desktop geöffnet, greift Co-Authoring über den Client (Datei muss in Excel mit AutoSave geöffnet sein, damit
  Excel selbst zusammenführt). Ein per COM unsichtbar geöffnetes Excel verhält sich wie ein normaler Benutzer – inkl.
  möglicher Dialoge („Datei wird bereits bearbeitet“, Konflikt-Dialog, „Speichern unter“). Diese Dialoge blockieren
  einen unbeaufsichtigten Lauf.
* [A] Ohne laufenden OneDrive-Client oder offline schreibt COM nur lokal; der Sync erzeugt bei paralleler Änderung
  Konfliktkopien („…-<PC-Name>.xlsx“). Nicht dokumentiert belegt, aber typisches Sync-Verhalten.
* [A] Formeln, bedingte Formatierung, Gliederung und Kontrollkästchen bleiben bei COM erhalten (Excel speichert die
  Datei selbst) – das ist der eine Vorteil gegenüber Graph, falls Graph im Test Features entfernt.

```python
# m365/lokal.py – Excel per COM (Member laut VBA-Doku; [A] pywin32-Abbildung); nur Windows
import win32com.client

def schreibe_zellen_com(pfad_lokal: str, blatt: str, adresse: str, werte: list[list]) -> None:
    xl = win32com.client.DispatchEx("Excel.Application")   # [A] DispatchEx = eigene Instanz, nicht die des Benutzers
    xl.Visible = False
    xl.DisplayAlerts = False                                # [A] unterdrückt Dialoge, nicht alle
    wb = xl.Workbooks.Open(pfad_lokal, UpdateLinks=0, ReadOnly=False, Notify=False)
    try:
        if wb.ReadOnly:
            raise RuntimeError("Datei ist schreibgeschützt geöffnet (vermutlich von jemand anderem in Bearbeitung)")
        ws = wb.Worksheets(blatt)
        ws.Range(adresse).Value = werte                     # [A] 2-D-Liste -> Bereich (pywin32 wandelt in SAFEARRAY)
        wb.Save()
    finally:
        wb.Close(SaveChanges=False)
        xl.Quit()
```

---

## 6. Empfehlung: Graph vs. COM für unseren Fall

**Ausgangslage:** `Fixterminüberwachung.xlsx` wird von mehreren Kollegen in Excel bearbeitet (Co-Authoring über
SharePoint). Entwürfe sollen im Postfach `j.duenschede@…` entstehen; der tägliche Lauf soll Bestätigungen aus
eingehenden Mails erkennen, möglichst unbeaufsichtigt (Aufgabenplanung).

| Kriterium | Graph (empfohlen) | COM (lokal) |
|---|---|---|
| Excel: Datei gleichzeitig von Kollegen geöffnet | [V] Arbeitet in Excel Online auf der Originaldatei; Konflikte als klare Fehlercodes (`accessConflict`), keine Dialoge; [A] Co-Authoring-Merge wie bei Excel im Web | [A] Abhängig von OneDrive-Sync und Excel-Dialogen; Schreibschutz (`Workbook.ReadOnly`), Konfliktkopien; blockierende Dialoge im Tagelauf |
| Excel: Formeln A–C, Formatierungen | [V] `PATCH` auf `D5:K5` lässt A–C unberührt; Formate/Bedingte Formatierung nicht per API änderbar; [NF] Erhalt von Checkbox/bedingter Formatierung/Gliederung testen | [A] bleibt erhalten (Excel speichert selbst) |
| Excel: Kontrollkästchen | [V] Boolean schreiben möglich; [NF] Erhalt der Steuerung testen | [A] `Range.Value = True` setzt Kästchen (Excel-eigene Engine) |
| Voraussetzungen | App-Registrierung (einmalig, i. d. R. ohne Admin-Consent), Anmeldung einmal pro PC, Internet | Windows + installiertes Excel/klassisches Outlook, OneDrive-Client läuft, Outlook offen |
| Unbeaufsichtigter Tagelauf | [V] Silent-Token aus DPAPI-Cache; läuft ohne offene Office-Programme | [A] Outlook/Excel müssen startbar sein; Sicherheits- und Dateidialoge können hängen |
| Mail: Entwurf mit Anhängen | [V] `POST /me/messages` + `fileAttachment`; Entwurf in „Entwürfe“, `webLink` (OWA) | [V] `CreateItem(0)` + `Save()` → „Entwürfe“, sofort in Outlook sichtbar, `Display()` möglich |
| Mail: Posteingang lesen (Hillebrand, Lieferanten) | [V] `$filter`/`$search`, `$value`-Download, 10.000 Req/10 min | [V] `Items.Restrict`; Lesen von `SenderEmailAddress`/`Body` ist geschützt → Warnungen ohne „gutes“ Antivirus oder bei strengem Trust Center |
| Kollegenpostfach | [V] `Mail.Read.Shared` + Exchange-Freigabe | [A] über `GetSharedDefaultFolder` möglich, gleiche Prompt-Problematik |
| Neues Outlook (Monarch) | unabhängig | [A] kein COM-Objektmodell |
| Tests/Linux-Dry-Run | HTTP-Mocks einfach (`fake.py`) | nur auf Windows |

**Empfehlung**

1. **Graph als Primärbackend** für Excel (Fixtermine, Kontaktliste) und Mail (Entwürfe, Posteingang) – eigene
   App-Registrierung, delegierte Rechte `User.Read`, `Files.ReadWrite`, `Sites.Read.All`, `Mail.ReadWrite`,
   `Mail.Read` (+ `Mail.Read.Shared` nur bei Bedarf), Token-Cache per `msal-extensions` (DPAPI) unter `%APPDATA%`.
   Interaktive Anmeldung (`acquire_token_interactive`, Systembrowser) als Standard; Device-Code als Fallback, wenn der
   Browser-Redirect auf `localhost` im Netz des Anwenders blockiert ist.
2. **Schreibdisziplin für Excel:** eine persistente Session je Lauf, strikt sequenziell, nur Adressen ab Spalte D,
   `null` für unveränderte Zellen, Werte vor dem Schreiben erneut lesen (optimistische Prüfung: Slot noch leer?),
   bei `accessConflict`/`invalidSessionAccessConflict` abbrechen und später erneut, `Retry-After` beachten.
3. **COM nur als optionales lokales Backend** (`m365.backend: lokal`) für das Anlegen von Entwürfen (kein Lesen des
   Posteingangs) und als Notnagel für Excel, falls der Graph-Test (C3) zeigt, dass Kontrollkästchen oder bedingte
   Formatierung verloren gehen. Vorher am PC prüfen: klassisches Outlook, Trust-Center-Einstellung, Antivirus-Status.
4. **Nicht** die First-Party-Client-ID von Microsoft verwenden (Abschnitt 2.5).
5. Alle Autonomie-Grenzen aus `AUSBAUSTUFE_2.md` bleiben: nie senden (`/send` nicht implementieren), Terminänderungen
   und Kontaktänderungen nur als Vorschlag.

---

## Checkliste für den Anwender

**A – Entra/App-Registrierung (einmalig, ca. 10 Minuten; ggf. IT-Admin)**

* A1. Entra Admin Center → App registrations → New registration: Name „Auftragsautomat (Weinstrasse)“, „Accounts in
  this organizational directory only“. Application (client) ID und Directory (tenant) ID notieren → `config/settings.yaml`.
* A2. Authentication → Add a platform → „Mobile and desktop applications“ → `http://localhost`; Advanced settings →
  „Allow public client flows“ = **Yes**. Optional WAM-URI `ms-appx-web://Microsoft.AAD.BrokerPlugin/<client-id>`.
* A3. API permissions → Microsoft Graph → Delegated: `User.Read`, `Files.ReadWrite`, `Sites.Read.All`,
  `Mail.ReadWrite`, `Mail.Read`, `offline_access` (optional `Mail.Read.Shared`). Beim ersten Login prüfen, ob die
  Zustimmung durchgeht; bei „Need admin approval“ Admin-Consent durch den Admin (oder Freigabe über den
  Admin-Consent-Workflow) einholen.
* A4. Enterprise applications → die neue App → Properties → „Assignment required“ = Yes und nur `j.duenschede`
  zuweisen (Empfehlung aus der PowerShell-Doku; verhindert Nutzung durch andere).
* A5. Nach dem ersten Login: PC-Neustart, dann `auftragsautomat ueberwachen` ohne Browser-Fenster ausführen; prüfen,
  dass der Silent-Token greift. Nach ~1 Woche erneut prüfen (Refresh-Token-/Conditional-Access-Verhalten des Tenants).
* A6. Sitzung unter dem Konto ausführen, das sich angemeldet hat (DPAPI-Cache ist kontogebunden); geplanter Task mit
  „Nur ausführen, wenn Benutzer angemeldet ist“ beginnen.

**B – Datei-Adressen**

* B1. Web-URL von `Fixterminüberwachung.xlsx` und `Ansprechpartner Abwicklung.xlsx` aus SharePoint kopieren
  (Adresszeile), Hostname (`<tenant>.sharepoint.com`) und Site-Pfad (`/sites/<Name>`) nennen.
* B2. Mit `ueber_site()`/`ueber_shares()` einmalig `drive_id`/`item_id` ermitteln und in `settings.yaml` ablegen;
  anschließend `GET …/workbook/worksheets` – exakte Blattnamen („Fixterminüberwachung“, „Ressourcen für Auto DropDown“)
  und Tabellennamen (`tbl_Spedition_*`) bestätigen.

**C – Excel-Verhalten (an einer KOPIE der Datei, gleiche Bibliothek)**

* C1. Kopie anlegen; `usedRange(valuesOnly=true)` lesen; Zeile 5 ff. mit `range(address='A5:K9')` lesen – Werte,
  `valueTypes`, Datumsdarstellung (Serienzahl vs. Text) notieren.
* C2. `PATCH D7:K7` mit Testwerten (Strings, Datum, `true/false`); prüfen: Spalten A–C unverändert (Formeln noch da?),
  Dropdown-Blatt rechnet weiter, Versionsgeschichte zeigt den Benutzer.
* C3. Nach dem PATCH in Excel Desktop öffnen: Kontrollkästchen in „Bestätigung des Lieferanten/Spedition/Abgeholt“
  noch vorhanden und gesetzt? Bedingte Formatierung, Gliederung/Gruppierung, Datenüberprüfung (Dropdown),
  Druckbereich erhalten? Falls etwas fehlt → COM-Backend für Excel oder Werte nur über Excel-Formeln/Bemerkungsspalte.
* C4. Konflikttest: (a) Kopie in Excel Desktop (AutoSave an) geöffnet lassen und per Graph schreiben – erscheint der
  Wert nach wenigen Sekunden? (b) Kollege tippt in dieselbe Zeile während des Schreibens – welche Seite gewinnt,
  welcher Fehlercode kommt? (c) Datei nur lokal (OneDrive offline) geöffnet – Fehler `accessConflict`?
* C5. Datumswerte: entscheiden, ob Serienzahl oder `numberFormat` mitgesendet wird (Ergebnis in `termine/register.py`
  festhalten).

**D – Outlook**

* D1. Klassisches oder neues Outlook? (Datei → Office-Konto bzw. Schalter „Neues Outlook testen“). Neues Outlook →
  COM-Backend entfällt.
* D2. Graph-Entwurf anlegen (ohne Anhang, dann mit PDF < 3 MB); in Outlook Desktop unter „Entwürfe“ sichtbar? `webLink`
  öffnet OWA? Absender/Signatur korrekt (HTML-Body)?
* D3. Nur wenn COM gewünscht: Trust Center → Programmatic Access ansehen (Standard: „warn when antivirus out of date“)
  und Antivirus-Status im Windows-Sicherheitscenter prüfen; Testskript `erstelle_entwurf_com` ausführen – erscheint
  eine Sicherheitsabfrage? Dann `SenderEmailAddress` einer Inbox-Mail lesen – Abfrage?
* D4. Posteingang: Hillebrand-Bestätigungen der letzten 7 Tage per `$filter` abrufen; Betreffmuster
  `Our Ref: (DEMB\d+)` bestätigen; Beispielmail als `.eml`/Export für `tests/fixtures` bereitstellen (anonymisiert).
* D5. Falls Bestätigungen in ein anderes Postfach laufen (`info@`, Kollege): Exchange-Freigabe einrichten und
  `Mail.Read.Shared` ergänzen.

**E – Sonstiges**

* E1. Dateigröße der beiden Excel-Dateien (< 25 MB?) und ob Bilder/Makros enthalten sind (`.xlsm` wäre zu prüfen;
  Graph-Doku nennt nur Office Open XML; der Connector unterstützt `.xlsx`/`.xlsb`).
* E2. Zeitzone/Format der Fixtermine (Spalte „Datum“ Serienzahl?) – siehe C1.

---

## Quellen

Microsoft Graph – Excel

* Working with Excel in Microsoft Graph (v1.0): https://learn.microsoft.com/en-us/graph/api/resources/excel?view=graph-rest-1.0
* workbook: createSession (v1.0): https://learn.microsoft.com/en-us/graph/api/workbook-createsession?view=graph-rest-1.0
* Manage sessions and persistence in Excel: https://learn.microsoft.com/en-us/graph/excel-manage-sessions
* Best practices for working with the Excel API: https://learn.microsoft.com/en-us/graph/workbook-best-practice
* Error handling for Excel APIs: https://learn.microsoft.com/en-us/graph/workbook-error-handling
* Update range (v1.0): https://learn.microsoft.com/en-us/graph/api/range-update?view=graph-rest-1.0
* workbookRange resource type (v1.0): https://learn.microsoft.com/en-us/graph/api/resources/workbookrange?view=graph-rest-1.0
* Worksheet: UsedRange (v1.0): https://learn.microsoft.com/en-us/graph/api/worksheet-usedrange?view=graph-rest-1.0
* Create TableRow (v1.0): https://learn.microsoft.com/en-us/graph/api/table-post-rows?view=graph-rest-1.0
* Known issues with Microsoft Graph APIs: https://learn.microsoft.com/en-us/graph/known-issues
* Microsoft Graph service-specific throttling limits: https://learn.microsoft.com/en-us/graph/throttling-limits
* Resource limits and performance optimization for Office Add-ins (Excel: 5 MB Payload, 5.000.000 Zellen): https://learn.microsoft.com/en-us/office/dev/add-ins/concepts/resource-limits-and-performance-optimization
* Manage Excel range checkboxes with the JavaScript API (Office.js): https://learn.microsoft.com/en-us/office/dev/add-ins/excel/excel-add-ins-ranges-checkboxes
* Excel Online (Business) Connector – Known issues and limitations (25 MB, 6-Minuten-Sperre, parallele Clients): https://learn.microsoft.com/en-us/connectors/excelonlinebusiness/
* [V-Dritt] Microsoft Q&A: Intermittent GenericFileOpenError … writing batched data to Excel workbooks: https://learn.microsoft.com/en-us/answers/questions/5866259/graph-api-issue-intermittent-genericfileopenerror
* [V-Dritt] Microsoft Q&A: Need API to determine if OneDrive/SharePoint document is open or locked (423 bei PUT /content): https://learn.microsoft.com/en-au/answers/questions/5654593/need-api-to-determine-if-onedrive-sharepoint-docum
* Microsoft Support: Excel file is locked for editing: https://support.microsoft.com/en-us/office/excel-file-is-locked-for-editing-6fa93887-2c2c-45f0-abcc-31b04aed68b3

Authentifizierung

* Register an application with the Microsoft identity platform: https://learn.microsoft.com/en-us/graph/auth-register-app-v2
* OAuth 2.0 device authorization grant: https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-device-code
* Troubleshooting AADSTS7000218 („Allow public client flows“): https://learn.microsoft.com/en-us/troubleshoot/entra/entra-id/app-integration/confidential-client-application-authentication-error-aadsts7000218
* Microsoft Graph permissions reference (Quelle der AdminConsentRequired-Angaben; Rohdatei): https://learn.microsoft.com/en-us/graph/permissions-reference bzw. https://raw.githubusercontent.com/microsoftgraph/microsoft-graph-docs-contrib/main/concepts/permissions-reference.md
* Configure how users consent to applications: https://learn.microsoft.com/en-us/entra/identity/enterprise-apps/configure-user-consent
* MSAL Python – Acquire tokens: https://learn.microsoft.com/en-us/entra/msal/python/getting-started/acquiring-tokens
* MSAL Python – PublicClientApplication (API-Referenz): https://learn.microsoft.com/en-us/python/api/msal/msal.application.publicclientapplication
* MSAL Python – Custom token cache serialization: https://learn.microsoft.com/en-us/entra/msal/python/advanced/msal-python-token-cache-serialization
* msal-extensions (README, `persistence.py`, `token_cache.py`, Branch `dev`; PyPI 1.3.1): https://github.com/AzureAD/microsoft-authentication-extensions-for-python
* PyPI-Versionen (abgefragt 2026-10-18): msal 1.39.0, msal-extensions 1.3.1, pywin32 312
* Use Microsoft Graph PowerShell authentication commands (eigene App-Registrierung, Assignment required): https://learn.microsoft.com/en-us/powershell/microsoftgraph/authentication-commands
* Verify first-party Microsoft applications in sign-in reports (14d82eec… = Microsoft Graph Command Line Tools): https://learn.microsoft.com/en-us/troubleshoot/entra/entra-id/governance/verify-first-party-apps-sign-in
* [V-Dritt] Interactive Graph Sessions with Custom Entra ID Apps (office365itpros, 2026-09-24): https://office365itpros.com/2026/09/24/interactive-graph-sessions-apps/
* [V-Dritt] Why securing Microsoft Graph Command Line Tools matters: https://benroberts.io/why-securing-microsoft-graph-command-line-tools-matters/

Datei adressieren

* Access shared items (`/shares`, Kodierung `u!`): https://learn.microsoft.com/en-us/graph/api/shares-get?view=graph-rest-1.0
* Address resources in a drive on OneDrive (Pfadkodierung): https://learn.microsoft.com/en-us/graph/onedrive-addressing-driveitems
* Get a SharePoint site (`/sites/{hostname}:/{path}`): https://learn.microsoft.com/en-us/graph/api/site-get?view=graph-rest-1.0
* List drives (`/sites/{siteId}/drives`): https://learn.microsoft.com/en-us/graph/api/drive-list?view=graph-rest-1.0
* Get driveItem (`/drives/{drive-id}/root:/{item-path}`): https://learn.microsoft.com/en-us/graph/api/driveitem-get?view=graph-rest-1.0
* [V-Dritt] Tech Community: Unable to get a driveItem of a file URL from Graph: https://techcommunity.microsoft.com/discussions/officegraph/unable-to-get-a-driveitem-of-a-file-url-from-graph/2596151

Outlook per Graph

* Create message (Entwurf): https://learn.microsoft.com/en-us/graph/api/user-post-messages?view=graph-rest-1.0
* Add attachment (< 3 MB): https://learn.microsoft.com/en-us/graph/api/message-post-attachments?view=graph-rest-1.0
* Attach large files to Outlook messages or events (Upload-Session): https://learn.microsoft.com/en-us/graph/outlook-large-attachments
* List messages: https://learn.microsoft.com/en-us/graph/api/user-list-messages?view=graph-rest-1.0
* Use the $search query parameter: https://learn.microsoft.com/en-us/graph/search-query-parameter
* Get attachment (`/$value`): https://learn.microsoft.com/en-us/graph/api/attachment-get?view=graph-rest-1.0
* Get Outlook messages in a shared or delegated folder: https://learn.microsoft.com/en-us/graph/outlook-share-messages-folders
* message resource type (`webLink`, `isDraft`, ImmutableId): https://learn.microsoft.com/en-us/graph/api/resources/message?view=graph-rest-1.0

COM / Office-Objektmodell

* Security Behavior of the Outlook Object Model: https://learn.microsoft.com/en-us/office/vba/outlook/how-to/security/security-behavior-of-the-outlook-object-model
* Outlook Object Model Security Warnings: https://learn.microsoft.com/en-us/office/vba/outlook/how-to/security/outlook-object-model-security-warnings
* Protected Properties and Methods: https://learn.microsoft.com/en-us/office/vba/outlook/how-to/security/protected-properties-and-methods
* MailItem.Save: https://learn.microsoft.com/en-us/office/vba/api/outlook.mailitem.save
* Items.Restrict (Jet/DASL-Filter): https://learn.microsoft.com/en-us/office/vba/api/outlook.items.restrict
* Workbooks.Open: https://learn.microsoft.com/en-us/office/vba/api/excel.workbooks.open
* Workbook.ReadOnly: https://learn.microsoft.com/en-us/office/vba/api/excel.workbook.readonly
* [V-Dritt] xlwings-Doku „OneDrive and SharePoint“ (FullName liefert URL bei AutoSave): https://docs.xlwings.org/en/latest/onedrive_sharepoint.html
* [V-Dritt] Microsoft Q&A: Outlook Programmatic Access in Office 365 Pro Plus & RDS 2019 (Registry-Pfade): https://learn.microsoft.com/en-us/answers/questions/410934/outlook-programmatic-access-in-office-365-pro-plus
* [V-Dritt] Slipstick: Change Programmatic Access Options: https://www.slipstick.com/developer/change-programmatic-access-options/
