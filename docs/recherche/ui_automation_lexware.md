# Recherche: UI-Automation von Lexware premium 2026 (Warenwirtschaft) mit Python/pywinauto

Stand: 2026-10-18. Projekt „Auftrags-Automat für Lexware premium“ (siehe `docs/ARCHITEKTUR.md`,
Module `lexware/ui_automation.py`, `lexware/dialoge.py`, `lexware/instanz.py`).
Umgebung des Anwenders: Windows-PC, Lexware premium 2026 (Warenwirtschaft premium), vier Firmen
(Mandanten), Python 3.11+.

Methodik: 47 Suchanfragen (deutsch und englisch), gelesen wurden vor allem die offiziellen
Handbücher (Benutzerhandbuch Warenwirtschaft pro/premium, Ausgabe März 2026, Seitenangaben unten
beziehen sich auf dieses Handbuch; Technisches Handbuch Einzelplatz 2023 und Mehrplatz 2026;
Schnelleinstieg Warenwirtschaft pro, Januar 2026), Lexware-FAQ-Seiten, die openTRANS-Spezifikation
von Lexware, ein Fachbuch (New Earth Publishing, „Lexware 2017 warenwirtschaft/pro/premium“,
Leseprobe), ein zweites Fachbuch (W. Seeger, „Lexware warenwirtschaft pro“, 2012, Auszug),
Forenbeiträge (lex-forum.net, administrator.de), lexwiki.de, processlibrary.com sowie die
pywinauto- und Microsoft-Dokumentation. Es wurde **kein** Lexware-System bedient; alle Aussagen zu
Fenstertiteln, Control-Namen und Dialogtexten sind daher nur so weit belegt, wie eine Quelle sie
nennt.

Kennzeichnung in diesem Dokument:

* **[V]** = verifiziert (Lexware-Handbuch, Lexware-FAQ, Hersteller-Doku; Quelle/Seite und Zitat genannt).
* **[V-Dritt]** = verifiziert als *Aussage einer Drittquelle* (Fachbuch, Forum, Partner, processlibrary) –
  nicht von Lexware bestätigt, kann für die konkrete Installation abweichen.
* **[A]** = Annahme / unsicher (mit Begründung). Muss am System geprüft werden.
* **[NF]** = nicht auffindbar; Prüfauftrag für den Anwender formuliert.

---

## Zusammenfassung (10 Zeilen)

1. **[V-Dritt]** Der Hauptprozess von Lexware professional/premium heißt **`Framework.exe`** (historischer Pfad
   `…\Lexware\Lexware professional\Gemeinsame Dateien\Framework\Framework.exe`; Lexware selbst nennt in einer FAQ
   „den Prozess framework.exe an den Clientrechnern“). Einen Prozess `LxWin.exe` oder `Lexware.exe` gibt es nach allen
   gefundenen Quellen nicht. Das Programm liegt unter `C:\Program Files (x86)\Lexware\[Programmreihe]\[Jahr]` → 32-Bit-Anwendung.
2. **[A]** Die UI-Technologie ist nirgends dokumentiert. Indizien (Modulnamen `Framework.Interfaces.dll`,
   `Framework.Controls.ProgressBar.dll`, `Haufe.Components.LexwareService.exe`, Pflicht „.NET Framework 4.8“) sprechen für
   einen .NET-Rahmen (WinForms) mit nativen Modulen (`lxframe.dll`, `stammmodule.dll`, `lxtab10.dll`); Delphi ist
   **nicht** belegt. Eine „neue Oberfläche“ 2025/2026 ist nicht belegt; neu ist 2026 nur die Datenbank (PostgreSQL).
3. **Empfehlung:** pywinauto mit **`backend="uia"` als Erstwahl**, `win32` als Fallback; vor dem Codieren die
   Hierarchie **beider** Backends mit `print_control_identifiers()` bzw. Inspect/Accessibility Insights aufnehmen.
   32-Bit-Python verwenden oder mindestens testen (pywinauto empfiehlt gleiche Bitness, nur relevant für `win32`).
4. **[V]** Der **Auftragsassistent** hat drei Seiten (Kopfdaten → Positionen → Konditionen/Summen) und ist für Einkauf
   (Bestellung) und Verkauf (Auftragsbestätigung) derselbe. Aufruf: `Strg+N` (Verkauf) / `Strg+M` (Einkauf) öffnet
   **immer** den Assistenten; `Strg+Shift+N/M` die Schnellerfassung. Buttons: Weiter, Zurück, Speichern, Abbrechen, Hilfe.
5. **[V]** Positionen: Positionsart wählen, Artikel per Nummer/Matchcode/AutoSuggest, Eingabe mit **grünem Häkchen**
   bestätigen (sonst kein Speichern möglich). Belegnummer und Vorgangsnummer werden **erst beim Speichern** vergeben →
   „Abbrechen statt Speichern“ (Dry Run) verbraucht keine Nummer.
6. **[V]** Nach dem Speichern erscheinen **optionale Abfragen** (Druck, E-Mail, eRechnung, Poststelle, Internetmarke,
   Festschreiben) und Hinweise zur USt-IdNr.-Prüfung – **alle** sind unter `Extras → Optionen` abschaltbar. Das ist der
   robusteste Weg, statt Dialoge wegzuklicken. Der Button-Text „Überspringen“ ist nicht belegt [NF].
7. **[V]** Eine automatische Bestellung *aus einer Auftragsbestätigung* gibt es nicht als Funktion; es gibt
   (a) **„Auftrag wandeln“** (Positionen eines Verkaufsbelegs in eine Bestellung übernehmen; EK nur, wenn der Lieferant
   am Artikel als Bezugsquelle hinterlegt ist, sonst Preis 0) und (b) **Extras → Bestellwesen** (Bestellvorschlag je
   Lieferant, bestandsbasiert, nicht auftragsbezogen). Streckengeschäft wird **nicht** unterstützt [V-Dritt].
8. **[V]** **Auftrag weiterführen**: Auftrag markieren → `Bearbeiten → Aufträge → Weiterführen` (oder Kontextmenü);
   Auftragsart anpassen; Reservierungen aus der AB werden beim Weiterführen in Lieferschein/Rechnung aufgehoben;
   Status „W“ am Ursprungsbeleg; zusätzliche Abfragen sind in den Firmenangaben konfigurierbar.
9. **[V]** Firmenwechsel: `Datei → Wechseln` (Doppelklick auf die Firma; alle Fenster der alten Firma werden
   geschlossen) oder Hauptnavigation (`F10`). **Die aktive Firma steht in der Titelleiste.** Kommandozeilenparameter
   für Firma/Benutzer: **[NF]**. Einmalstart: Meldung „Die Anwendung kann nur einmal gestartet werden“ ist nur in
   einem alten Forenfall (zwei `Framework.exe`) belegt [V-Dritt] → immer an die laufende Instanz andocken, nie neu starten.
10. **[V]** Zusatzfelder: je Bereich bis zu **sechs Auftrags-Freifelder** (`Verwaltung → Einstellungen → Freifelder`),
    sie stehen auf **Seite 1** des Auftragsassistenten. **[A]** „Spedition/Weingut/Shipment/Customer“ sind sehr
    wahrscheinlich solche umbenannten Freifelder. Ein Feld **Bestellnummer** (Kundenbestellnummer) und **Lieferdatum**
    gibt es auf Seite 1 des Verkaufsbelegs; **Lieferart** auf Seite 3.

---

## 1. UI-Technologie, Prozessname, pywinauto-Backend

### 1.1 Prozessname und Pfade

* **[V-Dritt]** processlibrary.com: `framework.exe` gehört zu „framework Application from Lexware GmbH & Co. KG“,
  Standardpfad `c:\programme\lexware\lexware professional\gemeinsame dateien\framework`. Weitere Module am selben Ort:
  `lxframe.dll`, `stammmodule.dll`; außerdem gelistet: `framework.dll`, `framework.controls.progressbar.dll`,
  `framework.interfaces.dll`, `framework.interfacesui.dll`, `framework.outputview.dll`, `frameworkui.dll`,
  `frameworkservice.exe`.
* **[V-Dritt]** administrator.de (2008): ein Anwender startet Lexware professional per Batch mit
  `start "Framework starten" "c:\Programme\Lexware\Lexware professional\Gemeinsame Dateien\Framework\Framework.exe"`
  – ohne weitere Parameter.
* **[V]** Lexware-FAQ 000003321 („Meldung am Client: Fehler -100 Der Datenbankserver ist noch nicht gestartet!“):
  Das Programm benötigt „die ungehinderte Kommunikation zwischen dem Prozess framework.exe an den Clientrechnern und
  dem Datenbankserverprozess dbsrv17.exe am Server“ (bis Version 2025, SQL Anywhere; Port 2638, System-DSN `LXSYDSN`).
* **[V]** Lexware-FAQ 000003364: Programmverzeichnis „Bei 64 bit-Windows: C:\Program Files (x86)\Lexware\[Programmreihe]\[Jahr]“,
  gemeinsame Komponenten unter „Common Files\Lexware“. → `Program Files (x86)` = **32-Bit-Programm** [A, abgeleitet].
* **[V]** Technisches Handbuch 2026: Dienste `Dbsrv16.exe` (Lexware Datenbank Server), `Haufe.Components.LexwareService.exe`
  (Lexware Postgresql Monitor), `pg_ctl.exe` (lexware_postgresql_pro), `LxInstallerService.exe`; Internetzugriff für
  `LxUpdateManager.exe`, `Lisa.LisaRunner.exe`, `LxWebaccess.exe`. Der Programmstart erfolgt „in zwei Schritten“: erst das
  Rahmenprogramm, dann „die installierten Programm-Module wie bspw. die Buchhaltung“ (Technisches Handbuch Einzelplatz 2023,
  „Das Programm starten“).
* **[V]** Lexware-FAQ 000003245 (Firewall) nennt für die *Standard/Plus*-Reihe die Programmdateien `lxoffice.exe`,
  `Pcfk32.exe` (faktura+auftrag), `Pcbh32.exe`, `loge32.exe`; für pro/premium nennt sie **keine** Programmdatei.
* **[NF]** `LxWin.exe` kommt in keiner Quelle vor. **Prüfauftrag:** Task-Manager → Registerkarte „Details“ bei laufendem
  Lexware: Name des Hauptprozesses, Pfad (Rechtsklick → „Dateipfad öffnen“) und Spalte „Plattform“ (32/64 Bit) notieren;
  außerdem Eigenschaften der Desktop-Verknüpfung („Ziel“).

### 1.2 Technologie der Oberfläche (Delphi? .NET?)

* **[NF]** Keine Quelle (Handbuch, FAQ, Stellenanzeigen, Foren) nennt die Entwicklungsplattform. Suchen nach
  Delphi-Artefakten (madExcept/EurekaLog-Dialoge, „Zugriffsverletzung bei Adresse … in Modul …“, DevExpress-Klassen,
  `ahk_class TForm…`) in Verbindung mit Lexware pro/premium lieferten nichts.
* **[V]** Systemvoraussetzungen (lexware.de): „Mindestens .NET Framework 4.8 ist für die Installation erforderlich.“
* **[A]** Die Modulnamen in Punkt-Notation (`Framework.Interfaces.dll`, `Framework.Controls.ProgressBar.dll`,
  `Lexware.Lisa.LisaRunner.exe`, `Haufe.Components.LexwareService.exe`, `Lexware.myCenter.Service.exe`) sind typische
  .NET-Assembly-Namen. Daneben stehen native Namen (`lxframe.dll`, `lxbase.dll`, `lxtab10.dll`, `stammmodule.dll`,
  `modulemanager.dll`). Plausible Architektur: .NET-Rahmen (WinForms), Fachmodule teils nativ (C++/Delphi) – **nicht belegt**.
  Für die Automation ist entscheidend, was Inspect/UIA pro Fenster liefert, nicht die Sprache.
* **[V]** „Neue Oberfläche 2025/2026“: nicht belegt. Neu in 2026 ist die Datenbank: „Ab der Version 2026 nutzt Lexware
  nicht mehr SAP SQL Anywhere, sondern PostgreSQL“ (siehe `odbc_lexware.md`); sichtbare Unterschiede laut Lexware-vor-Ort
  2025 nur in Aboutbox, Sicherheitszentrale, Toast-Messages und Datensicherungszentrale. eBanking stellt auf „DDBAC.net“ um
  („vorerst keine sichtbare Veränderung“).

### 1.3 pywinauto-Backend: Dokumentationslage

* **[V]** pywinauto Getting Started: „Win32 API (backend="win32") – a default backend for now“ für „MFC, VB6, VCL, simple
  WinForms controls and most of the old legacy apps“; „MS UI Automation (backend="uia")“ für „WinForms, WPF, Store apps, Qt5,
  browsers“. Entscheidung: „if Spy++ can show all the controls the "win32" backend is what you need“; Inspect.exe im
  UIA-Modus: „If it can show more controls and their properties than Spy++, probably the "uia" backend is your choice.“
  „Switching between available backends can show you a difference in hierarchies.“ Für UIA: „Custom properties and
  controls are not supported because of comtypes Python library restrictions.“
* **[V]** pywinauto Changelog 0.5.0: „64-bit Python and 64-bit apps support (but 32-bit Python is recommended for 32-bit
  apps)“; 0.5.1: „Warn user about mismatched Python/application bitness“.
* **[V]** Inspect.exe (Microsoft Learn): „Inspect is a legacy tool. We recommend Accessibility Insights instead.“ Inspect
  liegt im Windows SDK unter `\bin\<version>\<platform>`, „You don't typically need to run it as administrator“; zeigt
  UIA-Eigenschaften und Control Patterns sowie MSAA; Modi „Raw View / Control View / Content View“.

### 1.4 Erfahrungsberichte zur Automation von Lexware

* **[NF]** Keine GitHub-Projekte, AutoIt-/AutoHotkey-Skripte, UiPath-/Power-Automate-Berichte oder Forenbeiträge gefunden,
  die Lexware pro/premium per UI-Automation bedienen. Die GitHub-Code-Suche war in dieser Umgebung gesperrt
  (nur repository-gebundene Endpunkte erlaubt); die Websuche fand nichts.
* **[V-Dritt]** Die am Markt existierenden Zusatztools (lexwiki.de: Woo2LX, KisLX Pro, AnschriftManagerLX Pro,
  AuftragStatusLX Pro, ArtikelRückstandLX Pro) arbeiten als Partner-Programme am Datenbestand, nicht über die UI.
* **[V-Dritt]** Forum: Zusatzprogramme lassen sich mit der neuen Datenbank „nicht mehr wie gewohnt anbinden“ (siehe
  `odbc_lexware.md`), d. h. der UI-Weg bleibt für Nicht-Partner der einzige Schreibweg neben dem openTRANS-Import.

### 1.5 Bekannte Stolperfallen – was die Dokumentation dazu hergibt

Die vom Anwender genannten Beobachtungen (verzögerte Reaktion auf „Weiter“, Doppelklick springt weiter, Mengenfeld
nimmt Werte nur bei Einzeltastendruck an, Artikelsuche per Textfeld unzuverlässig) sind in keiner Quelle beschrieben
[NF]; es gibt aber dokumentierte Mechanismen, die sie erklären können:

* **[V]** AutoSuggest: „Sobald Sie eine Eingabe machen und passende Daten dazu gefunden werden, wird eine Auswahl von
  Einträgen angeboten … Mit jeder weiteren Eingabe passt sich die Auswahl an“ (S. 100). Ein per `set_text()`/`SetWindowText`
  gesetzter Wert löst diese Tastatur-Ereignisse nicht aus → Werte wirken nur bei echten Tastendrücken (`type_keys`).
  Erklärt die Beobachtung beim Mengenfeld und beim Artikelfeld. AutoSuggest ist abschaltbar: `Extras → Optionen →
  Auftragsbearbeitung → Autosuggest deaktivieren` (S. 204).
* **[V]** „Optimierte Mausbedienung“ (Option): „Mehrfacher Mausklick auf denselben Datensatz erhöht die Positionsmenge.
  Das Klicken auf einen anderen Artikel bewirkt das Übernehmen des vorherigen Artikels in die Positionsliste“ (S. 203)
  → Doppelklicks in der Artikelleiste verändern Mengen; nie doppelklicken.
* **[V]** Auswahl in der AutoSuggest-Liste „mit Doppelklick bzw. Eingabetaste übernehmen“ (S. 100); Enter in einer Liste
  bedeutet in Lexware generell „Datensatz bearbeiten“ (Abkürzungstasten, S. 213) → Enter nur gezielt senden.
* **[V]** Positionen: „Beachten Sie, dass zunächst die neuen Einträge bestätigt oder verworfen werden müssen, bevor Sie
  die Daten durch Speichern in die Datenbank ablegen können“ (S. 105) → grünes Häkchen ist Pflicht.
* **[V]** Tab-Reihenfolge der Positionserfassung ist **konfigurierbar** (`Extras → Optionen → Positionserfassung`:
  „Bestimmen Sie selbst, auf welchen Feldern der Positionserfassung der Cursor stoppen soll“, S. 205) → nie auf
  Tab-Sequenzen bauen, Felder immer direkt adressieren.
* **[V]** `Strg+E` wechselt vom Assistenten (Seite 1) in die Schnellerfassung, `Alt+E` zurück (S. 213) → bei
  `type_keys` auf Seite 1 keine Buchstaben mit Modifiern senden; `Strg+Return` ist Zeilenumbruch in Textfeldern.
* **[V]** Belegdatum = Systemdatum; Belegnummer „wird erst beim Speichern des Auftrags vergeben“ (S. 99); Vorgangsnummer
  „wird mit dem Speichern von neuen Aufträgen automatisch erzeugt“ (S. 105).
* **[A]** Verzögerte Reaktion auf „Weiter“: plausibel durch Datenbankzugriffe (Preisermittlung, Lagerverfügbarkeit,
  USt-IdNr.-Strukturprüfung) beim Seitenwechsel. Konsequenz für den Code: nach jedem „Weiter“ aktiv auf die neue Seite
  warten (Seitenliste links, siehe 1.6), nicht auf Zeit.

### 1.6 Aufbau der Assistenten (allgemein, verifiziert)

* **[V]** „Im linken Bereich des Assistenten werden alle Seiten des Assistenten aufgeführt. Die Seite, die aktuell
  bearbeitet wird, erkennen Sie an der blauen Markierung und am Rahmen um die Seitenbezeichnung. Die Seiten, die bereits
  abgearbeitet sind, sind durch die grüne Nummerierung markiert“ (S. 22). → Kandidat für die Erkennung des aktiven
  Schritts, falls UIA die Seitenliste als Liste/Tree/Tab liefert [A].
* **[V]** „Titelleiste: Im Kopf des Fensters steht, für welchen Vorgang dieser Assistent verwendet wird: In dem Beispiel
  finden Sie die Vorgangsbeschreibung Lohnleistung neu erfassen“ (S. 22). Die Titel der Auftragsassistenten sind nicht
  wörtlich belegt [NF]; „Verkaufsauftrag neu anlegen“ / „Einkaufs-Auftrag neu anlegen“ sind die Menüfunktionen
  (S. 30, Schnelleinstieg S. 40).
* **[V]** Buttons: „Über die Schaltfläche Weiter werden Sie Seite für Seite durch die Eingaben geführt … Zurück …
  Spätestens auf der letzten Seite gibt es anstelle der Schaltfläche Weiter die Schaltfläche Speichern. Mit dem Klick auf
  diese Schaltfläche wird die gesamte Eingabe gespeichert und der Assistent automatisch verlassen“ (S. 23). Bei manchen
  Assistenten ist Speichern schon auf früheren Seiten möglich (S. 23).
* **[V]** Im Infobereich der Kopfzeile des Auftragsassistenten stehen Belegnummer, Adressdaten und Vorgangsnummer (S. 105).

---

## 2. Assistent „Bestellung“ (Einkauf) und „Auftragsbestätigung“ (Verkauf)

### 2.1 Aufruf

* **[V]** `Bearbeiten → Aufträge → Verkaufs-/Einkaufs-Auftrag neu` startet die Auftragserfassung (S. 48); alternativ
  Symbol „Neuer Auftrag“ in der Symbolleiste → „Einkaufs-Auftrag neu anlegen“ (Schnelleinstieg S. 40).
* **[V]** Abkürzungstasten (S. 213): `Strg+N` Verkaufsauftrag neu im Auftragsassistenten, `Strg+M` Einkaufsauftrag neu im
  Auftragsassistenten, `Strg+Shift+N` / `Strg+Shift+M` dasselbe in der Schnellerfassung, `Strg+E` Wechsel Assistent → Schnellerfassung,
  `Alt+E` zurück, `F3` Auswahlliste öffnen, `F4` Kalender in Datumsfeldern, `F5` Ansicht aktualisieren, `F10` Hauptnavigation,
  `Alt+F4` Warenwirtschaft schließen, `Strg+F6` Daten nach Excel, `Return` in Ansicht = Datensatz bearbeiten, `Entf` = löschen,
  `Strg+Return` Zeilenumbruch, `#` Textbausteine/Variablen in Textfeldern.
* **[V]** „Um den Auftragsassistenten zu öffnen, halten Sie die Taste Strg gedrückt, während Sie die Funktion
  Einkaufs-/Verkaufs-Auftrag neu anlegen aufrufen“; mit `Strg+Shift` die Schnellerfassung (S. 30). Welche Maske ohne Modifier
  kommt, hängt von `Extras → Optionen → Auftragsbearbeitung → Aufträge bevorzugt im Schnellerfassungsdialog öffnen` ab (S. 203).
  → Der Automat sollte `Strg+N`/`Strg+M` verwenden und anschließend den Fenstertitel prüfen.
* **[V]** Die voreingestellte Auftragsart beim Aufruf ist unter `Extras → Optionen` (Standardwerte: Bearbeiter, Auftrags- und
  Positionsart, Warengruppe für manuelle Positionen, in premium Standardlager) festgelegt (S. 48, S. 204 f.).
* **[V]** „Die nachfolgende Beschreibung bezieht sich auf einen Verkaufsauftrag; für einkaufsseitige Aufträge gilt
  sinngemäß dasselbe“ (S. 99) – Einkauf und Verkauf nutzen denselben Assistenten.

### 2.2 Seite 1 – Kopfdaten („Rahmenbedingungen definieren, Adressdaten eingeben“)

Belegte Felder/Elemente (S. 99–102, Schnelleinstieg S. 18–19, 40):

| Element | Beleg | Hinweis |
|---|---|---|
| Auftragsart (Liste) | [V] | Verkauf: Angebot, Auftragsbestätigung, Lieferschein, Rechnung …; Einkauf: Bestellanfrage, Bestellung, Wareneingang, Eingangsrechnung, Gutschrift … (S. 50, S. 187) |
| Belegnummer | [V] | automatisch, erst beim Speichern; überschreibbar |
| Belegdatum (= Systemdatum), Lieferdatum, Leistungsdatum, Leistungszeitraum | [V] | Lieferdatum optional im Infofeld der Dokumente ausgebbar |
| Kundennummer / Lieferantennummer, Matchcode (gelb = AutoSuggest), Lupe, `F3` | [V] | „Wenn Sie die entsprechende Kundennummer kennen, geben Sie diese ein; die Angaben aus der Kundenverwaltung werden dann … übernommen“ |
| Projektnummer, Kostenstelle/Kostenträger | [V] | |
| Bearbeiter | [V] | Schnelleinstieg S. 19 („Im Feld Bearbeiter kann der Name des Sachbearbeiters eingegeben oder aus der Liste ausgewählt werden“); Standard aus Optionen |
| Wiedervorlage (Option + Datum), Notiz (Schaltfläche), Nullsteuersatz anwenden | [V] | |
| Auftragsbeschreibung (Text, Lupe = Textbausteine, Stift = Textbearbeitung) | [V] | Zeilenumbruch `Strg+Enter` |
| **Auftragsbezogene Freifelder** (bis 6) | [V] | „Im Auftragsassistenten stehen Freifelder zur Verfügung, wenn diese über Verwaltung → Einstellungen → Freifelder… definiert wurden“ – im Handbuch innerhalb des Abschnitts „Assistent Seite 1“ (S. 101) |
| Provisionsempfänger (nur premium, nur wenn aktiviert) | [V] | |
| **Bestellnummer** (Kundenbestellnummer) | [V] | Existenz belegt: E-Mail-Variable „Bestellnummer“ neben Auftragsnummer/Lieferdatum (S. 173); openTRANS-Spezifikation: `ORDER_ID` → „Auftrag-Bestellnummer“, in der erzeugten AB „auf der Seite ‚Kunde‘ … Bestellnummer und Datum wurden übernommen“. Genaue Beschriftung und Position im Assistenten **[A]** (vermutlich Seite 1) |
| TAM-Schaltfläche (Termin- und Aufgabenmanager) | [V] | S. 108 |

* **[V]** Seitenname: Die openTRANS-Doku nennt die erste Seite der AB „Seite ‚Kunde‘“; für Einkaufsbelege ist „Lieferant“
  naheliegend **[A]**.
* **[V]** Meldungen auf Seite 1 möglich: Lieferstopp („So werden Sie immer informiert, wenn Sie einen neuen Auftrag an diesen
  Kunden erfassen möchten“), Kreditrahmen-Überschreitung (S. 126) – beide ignorierbar, aber als Dialog einzuplanen.
* **[V]** Bei Bestellungen ist der Lieferant per Lupe aus der Lieferantenliste wählbar (Schnelleinstieg S. 40).

### 2.3 Seite 2 – Positionen

* **[V]** „Die zweite Seite des Auftragsassistenten ist in zwei unterschiedliche Bereiche geteilt. Im oberen Teil des
  Fensters finden sich die Eingabefelder, die sich je nach gewählter Positionsart entsprechend anpassen … In der unteren
  Hälfte sehen Sie die Liste der bereits erfassten Positionen“ (S. 102–104).
* **[V]** Positionsarten (Auswahlliste): Artikel (Stammartikel, Katalogartikel), Leistung (Lohnleistung, Nebenleistung),
  Manuelle Position, Alternativpositionen, Texte (Textposition, Kommentar, Zwischensumme), Sonstige (Titel, Titelsumme,
  Seitenumbruch) (S. 110). Standard-Positionsart je Einkauf/Verkauf unter `Extras → Optionen → Auftragsbearbeitung`.
* **[V]** Stammartikel: Suchkriterium wählbar – „die Nummer, die Bezeichnung (Kurztext), den Matchcode, Alle (AutoSuggest)
  und Bestellnummer (Autosuggest) direkt per Tastatur eingeben“ (S. 110). Gesperrte Artikel werden in der AutoSuggest-Suche
  nicht angezeigt (S. 104). → Für den Automaten: Suchkriterium **Nummer** einstellen, Artikelnummer tippen.
* **[V]** Über dem Feld Preis zeigt eine Schaltfläche den aktiven Preistyp; Klick öffnet ein gelbes Infofenster, das sich
  beim nächsten Mausklick schließt (S. 105). Warengruppe bestimmt den Steuersatz (Schnelleinstieg S. 19).
* **[V]** Abschluss einer Position: „Die Eingabe einer Position wird durch Klick auf das grüne Häkchen abgeschlossen. Damit
  wird die Position in die Positionsliste übernommen“ (Schnelleinstieg S. 19); daneben ein Symbol zum Verwerfen (S. 105).
* **[V]** Positionsliste: Varianten über Pfeil-Schaltfläche links unten; Positionen können markiert, geändert, umsortiert
  werden (S. 105). Bei Sammelrechnungen ist die Positionsliste nicht bearbeitbar.
* **[V]** Lagerbezug: Bei Auftragsbestätigungen kann „direkt bei der Positionserfassung“ entschieden werden, welche Artikel
  reserviert werden (S. 150) → ggf. Abfrage je Position **[A]**; „Warnhinweise unterdrücken“ reduziert Hinweise, aber „der
  Hinweis auf den verfügbaren Lagerbestand“ bleibt (S. 203).
* **[A]** Mengenfeld: Nach Handbuch wird nur per Tab/Maus zwischen den Feldern navigiert; die Beobachtung „nur Einzeltastendruck“
  passt zum AutoSuggest-/Validierungsverhalten. Empfehlung: Feld fokussieren, Inhalt mit `Strg+A`/`Entf` leeren, Ziffern
  einzeln mit `type_keys(..., with_spaces=True, pause=0.05)` senden, danach Wert zurücklesen.

### 2.4 Seite 3 – Konditionen / „Summen“

* **[V]** „Auf dieser Seite legen Sie die Zahlungs- und Lieferkonditionen fest“: Zahlungsbedingung, Zahlungsart, Lieferart
  (aus den Stammdaten vorbelegt, änderbar), Beträge, Gesamtrabatt (Betrag oder Prozent), i-Symbol (Netto/Brutto/Steuer),
  Summenzeichen (Titelzusammenstellung), Nachbemerkung (bis 2000 Zeichen) (S. 106–107). Der Rohgewinn wird nur mit Recht
  und Option angezeigt. Im Handbuch heißt die Seite „Seite Summen“ (S. 106), im Fachbuch 2017 „Summenseite“.
* **[V]** „Über die Schaltfläche Speichern wird der erfasste Auftrag in die Auftragsliste geschrieben. Möchten Sie Änderungen
  an den vorherigen Seiten vornehmen, klicken Sie auf Zurück. Zum Verwerfen der Eingaben gehen Sie auf Abbrechen“ (S. 107).
* **[V]** Eine vierte „Infoseite“ (Erfasst/Geändert, Beleghistorie) erscheint nur beim Öffnen gespeicherter Aufträge (S. 107).
* **[V]** Zahlungsbedingung/Lieferart erscheinen im Druck nur, wenn im Formular-Assistenten (Schlusstexte) aktiviert.

### 2.5 Nach dem Speichern: Dialoge und Hinweise

* **[V]** Schnelleinstieg 2026 (S. 19): „Klicken Sie auf die Schaltfläche Speichern. Nun werden Sie gefragt, ob Sie das Angebot
  ausdrucken oder per E-Mail verschicken möchten. Die Option Drucken öffnet den Druckdialog.“ Fachbuch 2017: „ANGEBOT DRUCKEN.
  Setzen Sie ein Häkchen für die gewünschte Option“ (Druck, E-Mail, ggf. Frankieren); Fachbuch 2012: „Es folgt die Festlegung
  einer Ausgabeart der Auftragsbestätigung … Soll weder gedruckt noch per E-Mail versendet werden, wird der entsprechende
  Button gewählt“ – die Button-Beschriftung („Überspringen“?) ist in keiner Quelle wörtlich belegt **[NF]**.
* **[V]** Diese Abfragen sind Optionen (`Extras → Optionen → Auftragsbearbeitung`, S. 204; `E-Mail-Versand`, S. 205):
  * „Nach Auftragsneuerfassung erscheint die Druckabfrage“
  * „Nach Auftragsneuerfassung erscheint die Abfrage nach der Verarbeitung als eRechnung“
  * „Nach Auftragsneuerfassung erscheint die Abfrage nach der Verarbeitung über die Poststelle“
  * „Nach Auftragsneuerfassung erscheint die Abfrage frankieren mit der Internetmarke“
  * „Nach Auftragsneuerfassung erscheint die Abfrage festschreiben“ (buchungsrelevante Belege)
  * „Nach Auftragsneuerfassung in der Erfassungsmaske verbleiben“
  * „Nach Auftragsneuerfassung abfragen, ob der Auftrag per E-Mail versendet werden soll“
  * „Hinweise zur USt-IdNr. Prüfung anzeigen – Falls Sie die Anfragen zur USt-IdNr. beim Bundeszentralamt außerhalb der
    Warenwirtschaft verwalten, können Sie durch die Deaktivierung dieser Option, die Anzeige von Meldungen der USt-IdNr.
    Prüfung abschalten.“
  * „Warnhinweise unterdrücken“, „Autosuggest deaktivieren“
* **[V]** USt-IdNr.: Bei Eingabe findet eine Strukturprüfung statt; die Online-Prüfung (einfach/qualifiziert, BZSt) ist nur für
  ausländische USt-IdNr. und wird über „Prüfen“ im Kunden-/Lieferanten-Assistenten (Seite Rechnungsstellung), über
  `Bearbeiten → Ausl. USt-IdNr prüfen` oder die Aktionsleiste gestartet (S. 197). Der vom Anwender beobachtete Dialog
  „USt-IdNr. des Lieferanten prüfen?“ beim Beleg ist damit sehr wahrscheinlich der über „Hinweise zur USt-IdNr. Prüfung
  anzeigen“ steuerbare Hinweis **[A]**; Wortlaut und Buttons **[NF]**.
* **[V]** Weitere mögliche Abfragen beim Speichern: „Auftrag buchen“ (nur wenn Lexware buchhaltung installiert: „Nachdem Sie
  einen zu buchenden Auftrag erfasst haben, erfolgt eine Abfrage, ob dieser Auftrag gebucht werden soll“, S. 51; in den
  Firmenangaben abschaltbar); Lagerabgänge bestätigen beim erneuten Speichern bearbeiteter Belege (S. 49); Wiedervorlage-Datum
  (Fachbuch 2017); Netzwerk-Meldung „Datensatz gerade von einem anderen Benutzer bearbeitet“ (S. 186).
* **[V]** Bearbeitbarkeit nach Druck/E-Mail ist in den Firmenangaben einstellbar („ob bereits gedruckte oder per E-Mail
  verschickte Aufträge noch bearbeitet werden können“, S. 43).

### 2.6 Schritt-für-Schritt-Ablauf für den Automaten (Einkauf „Bestellung“)

Belegte Schritte sind mit [V] markiert, der Rest ist Annahme und im Code mit `# TODO-LEXWARE` zu kennzeichnen.

1. [V] Prüfen, dass `Framework.exe` läuft; an die Instanz andocken (nie zweite Instanz starten, Abschnitt 5).
2. [V] Aktive Firma aus der **Titelleiste** lesen; bei Abweichung `Datei → Wechseln` (Abschnitt 5) und erneut prüfen.
3. [V] `Strg+M` → Auftragsassistent (Einkauf). [A] Fenstertitel enthält vermutlich „Einkaufsauftrag“/„Bestellung“ – zu belegen.
4. [V] Seite 1: Auftragsart „Bestellung“ wählen; Lieferantennummer in das Nummernfeld tippen (Stammdaten werden übernommen);
   Belegdatum belassen; Lieferdatum = Liefertag; [A] Freifelder „Spedition/Weingut/Shipment/Customer“ füllen (Abschnitt 6);
   [A] evtl. Dialoge Lieferstopp/Kreditrahmen gibt es nur im Verkauf.
5. [V] „Weiter“ → auf Seite 2 warten (Seitenliste links / Felder der Positionserfassung sichtbar).
6. [V] Je Position: Positionsart „Stammartikel“, Suchkriterium „Nummer“, Artikelnummer tippen, Auswahl per Enter/Übernehmen,
   Menge tippen (einzeln, Abschnitt 2.3), Preis = EK aus Stamm (nur kontrollieren, nicht überschreiben), **grünes Häkchen**;
   danach in der Positionsliste prüfen, dass die Zeile vorhanden ist (Menge, Artikelnummer).
7. [V] „Weiter“ → Seite 3 („Summen“): Gesamtbetrag netto/brutto lesen (i-Symbol zeigt Aufschlüsselung) und mit
   `paket.summe_netto_ek` vergleichen; Lieferart/Zahlungsbedingung nur lesen.
8. Dry Run: [V] „Abbrechen“ („verlassen Sie den Auftragsassistenten, ohne die Daten zu speichern“) → keine Belegnummer verbraucht.
   Scharf: „Speichern“ → [V] Belegnummer/Vorgangsnummer werden vergeben; Belegnummer aus Infobereich oder Auftragsliste lesen
   ([A] am sichersten aus der Einkaufsauftragsliste, sortiert nach Belegnummer).
9. [V] Nachgelagerte Abfragen (Druck/E-Mail/eRechnung/Poststelle/Internetmarke/festschreiben/USt-IdNr./Buchen) **vorher in
   den Optionen abschalten**; `dialoge.py` behandelt sie trotzdem defensiv (Titel/Text per UIA lesen, „Nein“/Abbrechen/„nicht
   drucken“ wählen; niemals „Drucken“ oder „Senden“).

Verkauf „Auftragsbestätigung“: identisch mit `Strg+N`, Auftragsart „Auftragsbestätigung“, Kundennummer, Bestellnummer
(Kundenbestellnummer), Lieferdatum, Preise = VK laut Preisgruppe (Lexware zieht sie selbst aus dem Kundenstamm; nur
kontrollieren); [V] bei AB kann je Position eine Reservierung abgefragt werden (S. 150).

---

## 3. Bestellvorschlag, auftragsbezogene Bestellung, Streckengeschäft

### 3.1 Was Lexware kann

* **[V]** „Auftrag wandeln – Die Funktion Auftrag wandeln ermöglicht es, die Positionslisten von Verkaufsaufträgen in
  Einkaufsaufträge und umgekehrt zu übernehmen … Von/in Verkauf: Angebot, Auftragsbestätigung, Lieferschein und Rechnung.
  In/von Einkauf: Bestellanfrage und Bestellung. Grundsätzlich werden die im Auftrag vorhandenen Positionen mit den jeweiligen
  Mengen übernommen. Wird ein Verkaufsauftrag in einen Einkaufsbeleg gewandelt, können die entsprechenden Einkaufspreise nur
  gezogen werden, wenn der ausgewählte Lieferant in den Artikelstammdaten als Bezugsquelle (auf der Seite Lieferanten)
  zugeordnet wurde. Andernfalls werden die Preise auf ‚0‘ gesetzt“ (S. 50). Menüort: im Kapitel „Das Menü Bearbeiten“
  unmittelbar nach „Auftrag weiterführen“ (S. 50); exakter Menüpfad/Kontextmenüeintrag **[A]** (vermutlich
  `Bearbeiten → Aufträge → Wandeln` bzw. Kontextmenü der Auftragsliste).
* **[V]** Bestellwesen (`Extras → Bestellwesen → Bestellanfrage/Bestellung/Bestelleingang`, S. 186–187): zwei Schritte –
  (1) Lieferanten und Artikel auswählen: Option „grundsätzlich alle Artikel“ oder „nur diejenigen, die den Mindestbestand
  unterschritten haben“ (dann zusätzlich „bereits bestellte Ware berücksichtigen“); „In der Listenansicht werden nur die
  Lieferanten angezeigt, die bei relevanten Artikeln hinterlegt sind“; Lieferant markieren, „Weiter“. (2) Bestellvorschlag:
  „Die Warenwirtschaft ermittelt auf Grundlage des aktuellen Bestands und verschiedener weiterer Kriterien den
  entsprechenden Bestellvorschlagswert. Setzen Sie in der Spalte Anfragen bzw. Bestellen einen Haken … Über die Schaltfläche
  Alle Anfragen bzw. Alle Bestellen werden alle Positionen vorgemerkt … Zuletzt speichern Sie die Angaben und verlassen damit
  den Assistenten. Die erstellte … Bestellung finden Sie in der Einkaufsauftragsliste wieder.“ Vorschlagsmenge = die im
  Artikelstamm je Lieferant hinterlegte Bestellmenge (Schnelleinstieg S. 42).
* **[V]** Voraussetzung: Lieferanten am Artikel (Artikel-Assistent, Seite Lieferanten: Bestellnummer, Bestellmenge, Lieferzeit,
  Preis pro, Einkaufspreis; beliebig viele Bezugsquellen): „Die hier hinterlegten Lieferanten sind eine Voraussetzung für das
  Bestellwesen: Nur so kann eine automatische Bestellung dieses Stammartikels erzeugt werden“ (S. 145).
* **[V]** Auftragsbezug im Bestellwesen nur indirekt: „Der Lieferrückstand informiert Sie über die offenen Auftragsmengen …
  aus allen offenen Auftragsbestätigungen, bei denen ein Lieferdatum vorhanden ist … Der Lieferrückstand kann bei der
  Ermittlung der Bestellmengen über Extras → Bestellwesen berücksichtigt werden“ (S. 142).
* **[V]** Bestelleingang: Bestellungen (aus Assistent oder Bestellwesen) werden über `Extras → Bestellwesen → Bestelleingang`
  in Wareneingänge weitergeführt; ein manuell per Auftragsassistent erfasster Wareneingang hat keinen Bezug zur Bestellung (S. 187).

### 3.2 Was Lexware nicht kann

* **[V-Dritt]** lex-forum.net (Wawi Pro 2017, Streckengeschäft/Baustoffhandel): mit Bordmitteln nicht umsetzbar; „Eine direkte
  Verbindung zwischen Verkaufs- und Einkaufsauftrag besteht derzeit“ nicht; Workaround: Endkundenanschrift als **Textposition**
  in der Positionsliste (bleibt beim Wandeln erhalten, ist per Recherche findbar); Auftrags-Freifelder gehen für den Abholer,
  werden aber nicht in Sammelrechnungen übernommen.
* **[V-Dritt]** lexwiki.de: „AnschriftManagerLX Pro“ (LexSHOP) erlaubt abweichende Anschriften auf Aufträgen und übernimmt
  sie in Folgebelege; Standard-Lexware unterstützt abweichende Lieferanschriften nur am Lieferschein.
* **Bewertung für das Projekt:** Eine automatische Bestellung *aus der AB* gibt es nicht. Zwei realistische Wege:
  (a) **Bestellung per Auftragsassistent** (Plan aus `ARCHITEKTUR.md`, voll kontrolliert: Mengen, EK, Freifelder) oder
  (b) **AB per openTRANS importieren, dann „Auftrag wandeln“ in eine Bestellung** – spart die Positionserfassung in der UI,
  setzt aber voraus, dass das Weingut als Bezugsquelle am Artikel hinterlegt ist (sonst EK = 0) und die Freifelder trotzdem
  nachgepflegt werden. Weg (b) ist eine lohnende Prüfaufgabe (Abschnitt 9).

---

## 4. „Auftrag weiterführen“ (AB → Lieferschein → Rechnung)

* **[V]** „Das ist die Funktion, mit der Sie aus Ihrem Angebot eine Auftragsbestätigung, daraus einen Lieferschein und hieraus
  wiederum die Rechnung erstellen – oder Sie erstellen die Rechnung ohne Zwischenschritte direkt aus dem Angebot.
  1. Markieren Sie den bereits vorliegenden Auftrag in der Auftragsliste. 2. Wählen Sie den Menüpunkt Bearbeiten → Aufträge →
  Weiterführen. 3. Passen Sie bei Bedarf die Auftragsart entsprechend an. 4. Hat sich der Auftrag zwischenzeitlich verändert,
  können Sie die einzelnen Positionen auch hier entsprechend anpassen“ (S. 50).
* **[V]** Kontextmenü: In der Detailansicht Aufträge (Kundenliste) „bietet Ihnen das Kontextmenü … Weiterführen oder Duplizieren“
  (S. 26); „Alle relevanten Menüpunkte erhalten Sie … wenn Sie in der geöffneten Bildschirmliste die rechte Maustaste anklicken“
  (S. 48). Fachbuch 2017: Lieferschein markieren, „rechte Maustaste und wählen weiterführen. Als Auswahl wird an dieser Stelle als
  Vorgangsart nur noch die Rechnung angeboten“; Fachbuch 2012: nach „Weiterführen“ wird die Auftragsart automatisch auf die
  nächste Stufe gesetzt, alternativ Aufruf über die Aktionsleiste rechts.
* **[V]** Nach dem Weiterführen öffnet sich der Auftragsassistent mit dem Folgebeleg (Positionen übernommen) – gleicher Ablauf
  wie bei Neuerfassung inklusive Speichern und nachgelagerter Abfragen [A, aus Fachbuch 2017 „RECHNUNG – POSITIONEN. Übernehmen Sie
  die beiden Positionen unverändert und drucken Sie die Rechnung“ abgeleitet].
* **[V]** Auswirkungen: Ursprungsbeleg erhält Status **W (Weitergeführt)**; Lieferschein in Rechnung → **F (Fakturiert)**;
  Lagerbuchung **LB**, Geliefert **LE**, Gedruckt **D**, per E-Mail **V**, Storniert **O**, Obsolet **OS**, Gebucht **B** (S. 89).
  Beleghistorie auf der Infoseite zeigt Vorgänger/Nachfolger (S. 107). „Beim Weiterführen in eine/n lagerrelevante/n
  Lieferschein/Rechnung wird die in der dazugehörigen Auftragsbestätigung reservierte Menge freigegeben“ (S. 150); welche
  Belegarten im Lager buchen, steht in den Firmenangaben (Seite Artikel, „Bestandsführung Verkauf/Einkauf“, S. 43).
* **[V]** Konfigurierbare Zusatzabfragen: Firmenangaben → Aufträge: „Nutzen Sie die Funktion Auftrag weiterführen, können Sie
  verschiedene Abfragen aktivieren“ (S. 43); Preise: „Beim Weiterführen und Duplizieren von Aufträgen können Sie die Gültigkeit
  der Sonderpreise prüfen lassen“ (S. 44); Texte: `Verwaltung → Texte → Standardtexte` legt fest, „ob die Auftragsbeschreibungen
  und Nachbemerkungen aus dem ursprünglichen Auftrag erhalten bleiben sollen“ (S. 50). Alternativpositionen gibt es nur in
  Angeboten. Bei OSS-Belegen Preise beim Weiterführen prüfen (S. 103).
* **[V]** Nicht weiterführbar: bereits in Sammelrechnungen fakturierte Lieferscheine (S. 120); während einer Inventur sind
  lagerrelevante Aufträge nur anzeigbar (S. 50).
* **[NF]** Wortlaut der Auftragsart-Auswahl beim Weiterführen (Dialog oder Listenfeld auf Seite 1?) – Prüfauftrag.

---

## 5. Einmalstart-Sperre, Firmenwechsel, aktive Firma, Kommandozeile

### 5.1 Einmalstart

* **[V-Dritt]** administrator.de (2008, Lexware Reisekosten Pro): Meldung „Die Anwendung kann nur einmal gestartet werden“;
  im Task-Manager „zwei Prozesse mit dem Namen Framework.exe“; Ursache laut Antwort: Reste einer alten Version. Für die
  Warenwirtschaft 2026 ist der Wortlaut nicht belegt, der Anwender beobachtet ihn aber selbst.
* **[V]** Lexware: „Zwei Datenbankserver im selben Netzwerk können nicht gleichzeitig laufen“ (FAQ 000003258); das Programm
  startet „in zwei Schritten“ (Rahmen, dann Module). Nach Absturz können Startmeldungen erscheinen (Datensicherung/Datenbank-
  Update am Client: „warten Sie einfach die Fertigstellung … ab“, Support-Seite Installationsunterstützung).
* **[V-Dritt]** kb.cmo.de (Partner-Wissensdatenbank, Seite derzeit nicht abrufbar, nur Such-Snippet): bei „Lexware bereits
  geöffnet“ hilft Ab-/Anmelden; Dienste „Lexware Update Service, Lexware Task Service, Datenbankdienste premium/professional“.
* **Konsequenz (`instanz.py`):** `lexware_laeuft()` = Prozess `Framework.exe` vorhanden (Name per Prüfauftrag bestätigen);
  `stelle_sicher_dass_lexware_laeuft()` startet **nur**, wenn kein `Framework.exe` läuft, und wartet dann auf das Hauptfenster;
  sonst `Application(backend=...).connect(path=<exe>)`. Beim Erscheinen von „nur einmal gestartet“ → Abbruch mit Screenshot.

### 5.2 Firmenwechsel und aktive Firma

* **[V]** „Wechseln – Unter diesem Menüpunkt bietet die Warenwirtschaft die Möglichkeit, zwischen den angelegten Firmen (bzw.
  Mandanten) zu wechseln. **Die jeweils aktuelle Firma wird am oberen Bildrand in der Titelleiste angezeigt.** Sie erhalten eine
  Auswahl der bereits angelegten Firmen und wechseln durch einen Doppelklick in die ausgewählte Firma. Alle noch aktiven Fenster
  der zuvor geöffneten Firma werden automatisch geschlossen“ (Menü Datei, S. 44).
* **[V]** Hauptnavigation (`F10` oder Maus darüber): „in eine andere Firma wechseln, in eine andere Applikation der Lexware
  Anwendung wechseln“ (S. 76). `Datei → Neu → Firma` legt Firmen an; `Datei → Schließen` schließt die Applikationen; `Datei →
  Beenden`/`Alt+F4` beendet das Programm (S. 41, 47).
* **[V]** Benutzer: Ohne Benutzerverwaltung „erscheint kein Anmeldedialog“; mit aktiver Verwaltung „wird bei jedem Programmaufruf
  automatisch der Benutzername und ggf. das zugehörende Kennwort abgefragt“; Option „Anmeldung mit letzter Kennung“: „Ist für
  diesen Benutzer kein Kennwort hinterlegt, startet das Programm direkt“ (Technisches Handbuch 2026, S. 59). Benutzerrechte
  können den Zugriff auf bestimmte Firmen einschränken; `Alt+.` im Benutzernamen-Feld trägt den Supervisor ein (Technik 2023, S. 70).
* **[NF]** Exakter Fenstertitel (Format „Lexware premium – Firmenname – Warenwirtschaft“?) und ob der Firmenname zusätzlich in der
  Statusleiste steht. **Prüfauftrag** (Abschnitt 9).

### 5.3 Kommandozeile

* **[NF]** Kein Handbuch und keine FAQ nennt Startparameter für `Framework.exe` (Firma, Benutzer, Modul). Der einzige belegte Aufruf
  ist ohne Parameter. **Prüfauftrag:** `"C:\…\Framework.exe" /?` bzw. `-?` in einer Eingabeaufforderung ausführen; „Ziel“ der
  Desktop-Verknüpfung notieren; ggf. Lexware-Hotline fragen.
* **[V]** Terminalserver-Betrieb: „Für diese Art der Nutzung bieten wir keinen direkten Support an“ (Systemvoraussetzungen);
  Client und Server dürfen „nicht über ein Virtual Private Network (VPN) verbunden sein“.

---

## 6. Zusatzfelder am Beleg (Spedition, Weingut, Shipment, Kunde/Customer, Kundenbestellnummer)

* **[V]** Freifelder: „Über Verwaltung → Einstellungen → Freifelder werden die Freifelder in der Warenwirtschaft verwaltet. Für alle
  Stammdatenbereiche und die Auftragsverwaltung können jeweils bis zu max. sechs benutzerdefinierte Felder hinterlegt werden. Um die
  Freifelder zu verwalten, ist das eigene Benutzerrecht Verwaltung → Einstellungen erforderlich“ (S. 175). Auftragsfreifelder
  stehen im Auftragsassistenten (Seite 1), können als Spalten der Auftragsliste eingeblendet, sortiert und im Formular-Assistenten
  (Seite Freifelder) in Infofeld/Betreff/Schlusstext gedruckt werden (S. 101–102); E-Mail-Variablen „Auftragsfreifelder 1–6“ (S. 173).
  Es gibt sie auch für Kunden, Lieferanten (S. 130, 137) und Artikel („rein informativ“, S. 142).
* **[A]** „Spedition“, „Weingut“, „Shipment“, „Customer“ auf dem Einkaufsbeleg sind mit hoher Wahrscheinlichkeit die vom Anwender
  benannten **Auftrags-Freifelder** (englische Bezeichnungen sprechen für individuelle Benennung). Alternativen, die geprüft werden
  müssen: Lieferart (Seite 3, Liste aus `Verwaltung → Lieferarten`), Auftragsbeschreibung/Nachbemerkung (Freitext), Textposition.
* **[V]** Kundenbestellnummer: Feld **„Bestellnummer“** auf Auftragsebene (E-Mail-Variable; openTRANS `ORDER_ID` → „Auftrag-
  Bestellnummer“, in der AB auf der Seite „Kunde“ sichtbar). Nicht zu verwechseln mit der **Artikel-Bestellnummer** je Lieferant
  (Artikelstamm, Seite Lieferanten) und dem Suchkriterium „Bestellnummer (AutoSuggest)“ in der Positionserfassung (S. 104, 110).
* **[V]** Liefertermin: Feld **Lieferdatum** (Seite 1), zusätzlich Leistungsdatum/Leistungszeitraum (S. 99).
* **[V]** Lieferanschrift: Kunden können mehrere Lieferanschriften haben (Hauptlieferanschrift mit Paket-Symbol, S. 126); abweichende
  Lieferanschrift am Lieferanten für Rücksendungen (S. 135).
* **[V-Dritt]** Freifelder werden nicht in Sammelrechnungen übernommen (lex-forum).
* **[NF]** Welche sechs Namen in den vier Firmen tatsächlich vergeben sind und ob die Einstellung je Firma gilt (das Handbuch spricht
  bei Kundenoptionen von „für die aktive Firma verbindlich“, S. 176 – für Freifelder nicht ausdrücklich). Prüfauftrag.

---

## 7. Empfehlung: Backend und Strategie

1. **Backend:** `backend="uia"` als Erstwahl (Pflicht .NET 4.8 und .NET-Assembly-Namen sprechen für WinForms-Anteile, die UIA gut
   abbildet; UIA ist bitness-unabhängig und liefert `automation_id`/`control_type`). `win32` als Fallback für Teilbereiche, in denen
   UIA keine Kinder zeigt (native Module, Grids). **Entscheidung erst nach Inspektion beider Bäume** (Abschnitt 9) – pywinauto:
   „Switching between available backends can show you a difference in hierarchies“.
2. **Python-Bitness:** 32-Bit-Python (Programm liegt unter `Program Files (x86)`); pywinauto: „32-bit Python is recommended for
   32-bit apps“ (betrifft `win32`-Nachrichten wie ListView/TreeView; UIA funktioniert auch gemischt [A]). pywinauto warnt bei
   Mismatch.
3. **Identifikation:** nur `title`/`auto_id`/`control_type`/`class_name` + `found_index`, nie Koordinaten; vor jedem Schritt
   `wait('visible enabled')`, nach „Weiter“ auf das Erscheinen eines Elements der Folgeseite warten (z. B. Positionsart-Liste),
   nie `sleep`.
4. **Eingabe:** Tastatur statt `set_text` (AutoSuggest-Felder reagieren auf Tastenereignisse); Ziffern einzeln; nach jeder Eingabe
   Wert zurücklesen (`window_text()`/ValuePattern); Doppelklicks vermeiden; keine Tab-Sequenzen (konfigurierbar).
5. **Dialoge vermeiden statt abfangen:** vor dem Produktivbetrieb in jeder der vier Firmen `Extras → Optionen` setzen: Druckabfrage,
   E-Mail-Abfrage, eRechnung-, Poststelle-, Internetmarke-, Festschreiben-Abfrage aus; „Hinweise zur USt-IdNr. Prüfung anzeigen“ aus;
   „in der Erfassungsmaske verbleiben“ aus; „Aufträge bevorzugt im Schnellerfassungsdialog“ aus; ggf. „Auftrag buchen“-Abfrage in den
   Firmenangaben aus. Das ändert das Verhalten auch für Kollegen → mit dem Anwender abstimmen. `dialoge.py` bleibt als Sicherheitsnetz
   (Fenster mit `control_type="Window"` ohne Assistenten-Titel → Text lesen, loggen, Screenshot, „Nein“/„Abbrechen“ wählen; Bei
   unbekanntem Dialog: Abbruch).
6. **Dry Run:** bis Seite 3, Summen lesen, „Abbrechen“ (keine Nummernvergabe, kein Datenbankschreiben belegt). Scharf: „Speichern“,
   dann Belegnummer in der Auftragsliste (Filter auf Belegdatum = heute und Partner) verifizieren und Dublettenprüfung erneut.
7. **Firma:** Titelleiste vor jedem Beleg prüfen; Firmenwechsel über `Datei → Wechseln` (Liste, Doppelklick) und anschließende
   Titelprüfung; nie parallel mit einem Menschen arbeiten (der Wechsel schließt alle Fenster).
8. **Alternative für die Bestellung prüfen:** AB (openTRANS) → „Auftrag wandeln“ → Bestellung; reduziert UI-Schritte auf Lieferantenwahl
   plus Freifelder, setzt Bezugsquelle am Artikel voraus.

Skelett für die Erkundung (nicht ausgeführt, Namen sind Platzhalter):

```python
# [A] Alle Bezeichner sind nach print_control_identifiers() zu ersetzen.
from pywinauto import Application, Desktop
app = Application(backend="uia").connect(path="Framework.exe")   # [V-Dritt] Prozessname
haupt = app.top_window()                                          # Titel enthält Firma [V]
print(haupt.window_text())
haupt.type_keys("^m")                                             # Strg+M: Einkaufsauftrag im Assistenten [V]
assistent = Desktop(backend="uia").window(title_re=".*(Einkauf|Bestellung).*")  # [A]
assistent.wait("visible", timeout=30)
assistent.print_control_identifiers(depth=6, filename="assistent_seite1_uia.txt")
```

---

## 8. Risiken

* UI-Technologie unbekannt; Grids/Positionsliste könnten für UIA unsichtbar sein (dann Fallback `win32` oder Lesen über
  „Export nach Excel“ der Auftragsliste statt aus dem Assistenten).
* Optionen/Abfragen sind benutzer- oder firmenabhängig konfigurierbar; Abweichungen zwischen den vier Firmen und zwischen Benutzern
  → Ablauf je Firma testen.
* Keine Kommandozeilenparameter belegt → Start/Anmeldung/Firmenwahl nur über UI; Benutzerverwaltung kann einen Anmeldedialog erzeugen.
* Einmalstart-Sperre: zweiter Start blockiert; nach Absturz bleiben Prozesse/Meldungen (Datensicherung, DB-Update) zurück.
* Firmenwechsel schließt alle Fenster – Kollision mit manueller Arbeit am selben PC.
* Netzwerk-Sperren („Datensatz gerade von einem anderen Benutzer bearbeitet“), Lieferstopp/Kreditrahmen-Hinweise, Lagerabfragen,
  Reservierungsabfragen bei AB.
* AutoSuggest/Tastatursemantik (Enter, Doppelklick) kann Mengen/Positionen unbeabsichtigt verändern; Mehrfachklick erhöht Mengen.
* Versionsupdates (unterjährig, premium) können Dialoge und Control-Bäume ändern → Identifier in Konfiguration auslagern, Smoke-Test
  nach jedem Update.
* Terminalserver/RDP-Sitzungen: Lexware ohne Support; UIA braucht eine aktive, entsperrte Desktop-Sitzung.

---

## 9. Checkliste für den Anwender

Bitte am laufenden System (eine Testfirma oder Musterfirma, nichts speichern) erzeugen und ins Repository unter
`docs/recherche/lexware_ui/` legen:

1. **Prozess:** Task-Manager → Details: Name, Pfad, „Plattform“ (32/64 Bit) des Lexware-Hauptprozesses; Screenshot. „Ziel“ der
   Desktop-Verknüpfung (Eigenschaften). In `cmd`: `"<Pfad>\Framework.exe" /?` – Ausgabe/Reaktion notieren.
2. **Titelleiste:** Screenshot des Hauptfensters mit sichtbarer Titelleiste in jeder der vier Firmen (`Datei → Wechseln`); Screenshot
   des Firmenauswahl-Dialogs; ob die Statusleiste Firma/Benutzer zeigt.
3. **Inspect/Accessibility Insights:** Windows SDK installieren (Inspect.exe) oder Accessibility Insights for Windows; je ein
   Screenshot des Baums (UIA-Modus, Control View) für: Hauptfenster, Firmenwahl, Auftragsassistent Seite 1/2/3 (Verkauf) und
   Seite 1/2/3 (Einkauf, Auftragsart Bestellung), Positionsliste, Druck-/E-Mail-Abfrage nach Speichern, USt-IdNr.-Hinweis.
   Alternativ Spy++ für dieselben Fenster (Vergleich win32).
4. **pywinauto:** in 32-Bit- und 64-Bit-Python je `print_control_identifiers(depth=8, filename=...)` mit `backend="uia"` **und**
   `backend="win32"` für Hauptfenster und Assistent Seite 1–3 (Dateien `*_uia.txt`, `*_win32.txt`). Script:
   `from pywinauto import Desktop; Desktop(backend="uia").window(title_re=".*Lexware.*").print_control_identifiers(depth=8, filename="haupt_uia.txt")`.
5. **Fenstertitel wörtlich** notieren: Hauptfenster, Auftragsassistent (Verkauf/Einkauf, neu und bearbeiten), Weiterführen-Dialog,
   Druckabfrage (alle Button-Beschriftungen, insbesondere der „weder drucken noch mailen“-Button), USt-IdNr.-Hinweis (Text + Buttons),
   Meldung „Anwendung kann nur einmal gestartet werden“ (Screenshot, falls reproduzierbar: zweites Mal starten).
6. **Optionen:** Screenshots von `Extras → Optionen → Auftragsbearbeitung`, `Positionserfassung`, `E-Mail-Versand`, `Benutzerverwaltung`;
   `Bearbeiten → Firmenangaben → Aufträge` (Abfragen beim Weiterführen, Bearbeitung nach Druck) und `→ Artikel` (Bestandsführung) –
   je Firma, falls unterschiedlich.
7. **Freifelder:** `Verwaltung → Einstellungen → Freifelder` → Registerkarte Aufträge (alle sechs Namen) je Firma; Seite 1 eines
   vorhandenen Einkaufsbelegs mit gefüllten Feldern Spedition/Weingut/Shipment/Customer (wo genau stehen sie?); Seite 1 einer AB mit
   Feld Bestellnummer/Lieferdatum (Beschriftung).
8. **Positionserfassung:** Screenshot Seite 2 mit Positionsart-Liste, Suchkriterium-Liste (Nummer/Matchcode/…), Mengenfeld, grünem Häkchen;
   kurzer Test: Menge per Zwischenablage einfügen vs. tippen – was passiert?
9. **Weiterführen:** AB in der Verkaufsauftragsliste markieren → Kontextmenü (Screenshot der Einträge) → Weiterführen → Screenshot der
   Auftragsart-Auswahl; danach Abbrechen.
10. **Auftrag wandeln:** Menü-/Kontextmenüeintrag suchen (Bearbeiten → Aufträge …); Screenshot des Dialogs (Lieferantenwahl?); Abbrechen.
    Prüfen, ob die Weingüter am Artikel als Lieferant hinterlegt sind (Artikel-Assistent, Seite Lieferanten).
11. **Bestellwesen:** `Extras → Bestellwesen → Bestellung` Seite 1 und 2 (Screenshots), Abbrechen.
12. **Benutzer:** Ist die Benutzerverwaltung aktiv (Anmeldedialog beim Start)? Welche Benutzer haben Rechte auf welche Firmen?

---

## 10. Quellen

Lexware (Hersteller):
* Benutzerhandbuch Warenwirtschaft pro/premium, Ausgabe 13.03.2026 – https://lex-blog.de/wp-content/uploads/2026/03/20260313_handbuch_warenwirtschaft_pro.pdf
  (identisch: https://www.lexware.de/fileadmin/support/handbuecher/2026/handbuch_warenwirtschaft_pro.pdf)
* Schnelleinstieg Warenwirtschaft pro, 03.01.2026 – https://lex-blog.de/wp-content/uploads/2026/03/20260103_schnelleinstieg_warenwirtschaft_pro.pdf
* Technisches Handbuch Mehrplatz 2026 – https://lex-blog.de/wp-content/uploads/2026/03/20260103_handbuch_technik_mehrplatz.pdf
* Technisches Handbuch Einzelplatz 2023 – https://lex-blog.de/wp-content/uploads/2023/02/handbuch_technik_einzelplatz_2023.pdf
* Spezifikation Bestellformat openTRANS pro (Lexware) – https://lexwiki.de/wp-content/uploads/Spezifikationen-Bestellformat-openTRANS-pro.pdf
* FAQ 000003321 „Meldung am Client: Fehler -100 …“ (framework.exe ↔ dbsrv17.exe) – https://www.lexware.de/support/faq/faq-beitrag/000003321
* FAQ 000003258 „Meldung am Einzelplatz: Fehler -100 …“ – https://www.lexware.de/support/faq/faq-beitrag/000003258/
* FAQ 000003364 „Client-Server-Installationen in einem Windows Netzwerk“ – https://www.lexware.de/support/faq/faq-beitrag/000003364/
* FAQ 000036354 „Version 2026: Client-Server-Installationen (PostgreSQL-Datenbank)“ – https://www.lexware.de/support/faq/faq-beitrag/000036354/
* FAQ 000003245 „Firewall – Benötigte Freigaben“ – https://www.lexware.de/support/faq/faq-beitrag/000003245/
* FAQ 000003319 „Die Anwendung kann nicht gestartet werden, da die Schreibrechte …“ – https://www.lexware.de/support/faq/faq-beitrag/000003319/
* FAQ 000028027 „Besonderheiten bei der Prüfung der USt-IdNr.“ – https://www.lexware.de/support/faq/faq-beitrag/000028027/
* FAQ 000023299 „Freifelder verwalten, anzeigen und drucken (faktura+auftrag)“ – https://www.lexware.de/support/faq/produkt/financial-office-plus/faq-beitrag/000023299/
* Systemvoraussetzungen – https://www.lexware.de/systemvoraussetzungen/
* Videoübersicht Fakturierung/Warenwirtschaft (Akademie) – https://www.lexware.de/support/videos-uebersicht-fakturierung/
* Lexware vor Ort 2025, Technik (TAS) – https://lex-blog.de/wp-content/uploads/2025/12/LvO2025_technik.pdf

Drittquellen:
* New Earth Publishing, „Lexware 2017 warenwirtschaft/pro/premium“, Leseprobe – https://content.e-bookshelf.de/media/reading/L-8487021-4f835bc43a.pdf
* W. Seeger, „Lexware warenwirtschaft pro“ (2012), Auszug – https://silo.tips/download/lexware-warenwirtschaft-pro-werner-seeger-1-ausgabe-mrz-2012
* lex-forum.net „Lieferanschrift auf Bestellungen“ (Streckengeschäft, 2017) – https://lex-forum.net/viewtopic.php?t=4327
* lexwiki.de Tools Warenwirtschaft – https://lexwiki.de/warenwirtschaft/tools-warenwirtschaft
* administrator.de „Lexware Reisekosten Pro Fehler – Die Anwendung kann nur einmal gestartet werden“ (2008) – https://administrator.de/forum/lexware-reisekosten-pro-fehler-die-anwendung-kann-nur-einmal-gestartet-werden-89771.html
* administrator.de „Start befehl“ (Framework.exe per Batch, 2008) – https://administrator.de/forum/start-befehl-94643.html
* administrator.de „Lexware und Terminal Server“ – https://administrator.de/forum/lexware-und-terminal-server-92360233975.html
* processlibrary.com framework.exe – https://www.processlibrary.com/en/directory/files/framework/444895/ ; lxframe – https://www.processlibrary.com/en/directory/files/lxframe/466456/
* kb.cmo.de „Lexware kann nur einmal gestartet werden“ (nur Such-Snippet, Seite 404) – https://kb.cmo.de/wissensdatenbank/lexware-kann-nur-einmal-gestartet-werden/

Werkzeuge:
* pywinauto Getting Started (Backends, Spy/Inspect, print_control_identifiers) – https://pywinauto.readthedocs.io/en/latest/getting_started.html
* pywinauto Change Log (Bitness 0.5.0/0.5.1) – https://pywinauto.readthedocs.io/en/stable/HISTORY.html
* Microsoft Learn „Accessibility tools – Inspect“ – https://learn.microsoft.com/en-us/windows/win32/winauto/inspect-objects
* Accessibility Insights for Windows – https://accessibilityinsights.io/

Nicht ergiebig (geprüft): Suchen nach `LxWin.exe`, Delphi/Borland/Embarcadero + Lexware, madExcept/EurekaLog + Lexware, `ahk_class`
+ Lexware, AutoIt/AutoHotkey/pywinauto/UiPath/FlaUI + Lexware, Startparameter/Kommandozeile + Lexware, „Überspringen“-Dialog,
Haufe-Stellenanzeigen (nur C#/Java/Web).
