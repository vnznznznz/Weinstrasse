# CLAUDE.md – Arbeitsanweisung für die Claude-Code-Sitzung am Lexware-PC

## Projekt

Der Auftrags-Automat liest Kundenaufträge (PDF, Scan, E-Mail), gleicht sie gegen den Lexware-Stamm
ab und zeigt sie zur Freigabe an. Nach der Freigabe legt er in Lexware premium die Belege an
(Einkauf-Bestellung, Angebot → Auftragsbestätigung, optional Lieferschein) für vier Firmen:
`weinstrasse_agentur`, `wik_fh_seh`, `wik_leh`, `woe_leh`. Der Lexware-Teil
(`src/auftragsautomat/lexware/`) wurde nie am echten Programm getestet; deine Aufgabe in dieser
Sitzung ist die **Kalibrierung am echten Lexware** nach dem Plan unten.

Lies vor der Arbeit:

* `README.md` – Bedienung, CLI-Befehle, Dry Run, Sicherheitsregeln.
* `docs/ARCHITEKTUR.md` – Module, Schnittstellen, Geschäftsregeln (nie abschwächen).
* `docs/AUSBAUSTUFE_2.md` – Lexware-Ablauf je Firma, Formulare, Benennung.
* `docs/EINRICHTUNG_WINDOWS.md` – Anleitung des Anwenders; am Ende das **Kalibrier-Protokoll**
  (dort steht, welche Phase erledigt ist).
* `docs/recherche/ui_automation_lexware.md` (Abschnitte 7–9) und `docs/recherche/odbc_lexware.md`.
* `config/lexware_controls.yaml` – Control-Namen; `TODO-LEXWARE` = am echten System prüfen.
* `config/settings.yaml` (Abschnitt `lexware`) und `config/firmen.yaml`.

Lexware-Code: `lexware/ui_automation.py` (`PywinautoBackend`, `LexwareControls`/`ControlSpec`,
`lade_controls`), `lexware/instanz.py` (nie starten, nur andocken; Prozess `Framework.exe`),
`lexware/dialoge.py`, `lexware/buchen.py`, `lexware/opentrans.py`.

## Umgebung und Befehle

* Windows; Projekt in `C:\Auftragsautomat` (o. ä.), Python 3.13 in `.venv`.
* Einrichtung: `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/windows/einrichten.ps1`
  (Schalter `-OhneTests`, `-MitBrowser`).
* Jeder Shell-Aufruf ist eine neue Shell; `activate` wirkt nicht fort. Rufe deshalb direkt auf
  (funktioniert in Git Bash und PowerShell):
  * `.venv/Scripts/python -m pytest -q`
  * `.venv/Scripts/ruff check src tests scripts`
  * `.venv/Scripts/auftragsautomat <befehl> …`
  * Interaktiv für den Anwender: `.venv\Scripts\activate`, dann `python -m pytest -q` und
    `ruff check src tests scripts`.
* Bei Umlautfehlern (`UnicodeEncodeError`) die Umgebungsvariable `PYTHONUTF8=1` setzen.
* Log-Ausgaben gehen nach stderr; für Läufe an Lexware `--log-level DEBUG` vor den Befehl setzen
  (`auftragsautomat --log-level DEBUG buchen …`) und stderr nach `logs/` umleiten.

## Git

* Arbeitszweig: `claude/fervent-galileo-urj1wl`. Nicht auf andere Zweige wechseln.
* Commits mit klaren deutschen Nachrichten ("Lexware-Controls: Angebotsassistent Seite 1 kalibriert").
  Danach `git push origin claude/fervent-galileo-urj1wl`. **Nie Force-Push**, nie History umschreiben.
* Dateien einzeln hinzufügen (`git add <datei>`), vor jedem Commit `git status` und `git diff --cached`
  lesen. Nie committen: `logs/`, `daten/`, `eingang/`, `archiv/`, Erkundungs- und Screenshot-Dateien,
  `config/settings.yaml` mit `dry_run: false`.
* Vor jedem Commit: `pytest -q` grün und `ruff check src tests scripts` sauber.

## Sicherheitsregeln (immer, ohne Ausnahme)

1. **Lexware nie starten**, nie beenden. Läuft `Framework.exe` nicht, bittet der Anwender, es
   **einmal** zu starten. `lexware.exe_pfad` bleibt `null` (dann kann das Tool Lexware nicht starten).
2. **Nie "Speichern", "Buchen", "Festschreiben", "Drucken", "E-Mail senden"** ohne ausdrückliches
   "Ja" des Anwenders im Chat **für genau diesen einen Schritt**. Ein früheres Ja gilt nicht für den
   nächsten Beleg. Drucken auf Papier und E-Mail-Versand aus Lexware gibt es überhaupt nicht.
3. **Dry Run ist Standard.** `--scharf` (und das zugehörige `--ja`) nur nach diesem Ja. `config/settings.yaml`
   behält im Repository `dry_run: true`.
4. Dialoge: "per E-Mail/Ausdruck" → **Überspringen**; "USt-IdNr. prüfen?" → **Nein**; unbekannter
   Dialog → stoppen, Screenshot, Anwender fragen.
5. **Vor jeder Aktion, die Lexware-Daten verändern könnte, fragen** (Speichern, Weiterführen mit
   Speichern, Stammdaten, Optionen unter Extras → Optionen, Freifelder, Firmenwechsel).
   `lexware.firmenwechsel_erlaubt` bleibt `false`; die Firma wechselt der Anwender.
6. **Vor jeder Bedienung von Lexware** dem Anwender sagen: "Ich bediene jetzt Lexware, bitte Maus und
   Tastatur nicht benutzen." Danach "fertig" melden. Abbruch: Esc in Claude Code, offenen Assistenten
   mit "Abbrechen" schließen. Bedient wird Lexware nur über die Befehle des Projekts (`buchen`,
   `lieferschein`) ab Phase 3; Erkunden nur mit `auftragsautomat lexware-erkunden` (nur lesend).
   Eigene Ad-hoc-Skripte dürfen Lexware nur lesen (z. B. `print_control_identifiers`), nie klicken,
   tippen, Fokus setzen oder Fenster schließen.
7. Erkundungs-, Log- und Screenshot-Dateien bleiben unter `logs/` und werden **nie committet**.
8. **Keine echten Kundendaten** in Code, Tests, Doku, Commits oder im Kalibrier-Protokoll: keine Namen,
   Adressen, E-Mail-Adressen, Telefonnummern, Preise, Kunden-/Belegnummern. Erlaubt: Control-Namen,
   Fenstertitel (kunden- oder belegspezifische Teile durch `.*` bzw. `…` ersetzen), Firmennamen der vier
   eigenen Firmen. Beispiele nur synthetisch (`example.org`, "Frau Beispiel").
9. ODBC nur lesend (`SELECT`). Passwörter nur über Umgebungsvariablen (`passwort_env`), nie in Dateien.
10. Geschäftsregeln aus `docs/ARCHITEKTUR.md` nicht abschwächen, Tests nicht löschen, um grün zu werden.

## Kalibrierplan

Jede Phase endet mit (a) einem kurzen Bericht an den Anwender (was geprüft, was geändert, was offen),
(b) dem Fortschreiben des Kalibrier-Protokolls in `docs/EINRICHTUNG_WINDOWS.md` und (c) einem Commit.
Beginne mit der ersten Phase, die im Protokoll nicht erledigt ist. Abbruchkriterium in allen Phasen:
unerwarteter Dialog, falsche Firma im Titel, Lexware reagiert nicht, Anwender sagt Stopp → anhalten,
Screenshot nach `logs/screenshots/`, berichten.

### Phase 0 – Umgebung

1. `scripts/windows/einrichten.ps1` ausführen; Tests müssen grün sein.
2. Prozessname prüfen (`tasklist /FO CSV /NH`, Anwender: Task-Manager → Details, Spalte Plattform
   32/64 Bit). Stimmt `Framework.exe`, den Kommentar `TODO-LEXWARE` an `PROZESSNAME_STANDARD` in
   `lexware/instanz.py` und an `PROZESSNAME` in `config/lexware_controls.yaml` durch "bestätigt
   <Datum>" ersetzen; sonst beide Werte korrigieren.
3. Prüfen, ob Lexware als Administrator läuft; wenn ja, muss Claude/Terminal ebenfalls erhöht laufen.
4. Python-Bitness notieren. 64 Bit ist Standard; nur wenn das `win32`-Backend in Phase 2 an der
   Bitness scheitert, eine 32-Bit-`.venv` vorschlagen (Anwender entscheidet).

Dokumentieren: Windows-, Lexware-, Python-Version, Prozessname, Bitness, Admin ja/nein.
Abbruch: Tests rot, pywinauto nicht importierbar, Lexware-Prozess nicht auffindbar.

### Phase 1 – Stammdaten lesen

1. Weg wählen: **CSV-Export** (Anwender: Lexware → Datei → Export, Dateien nach `daten/<firma>/`,
   siehe `docs/WAS_ICH_BRAUCHE.md` A1) und/oder **ODBC** (`pyodbc.drivers()` prüfen; Lexware 2026
   nutzt PostgreSQL, ein Treiber fehlt vermutlich – siehe `docs/recherche/odbc_lexware.md`).
2. Je Firma `auftragsautomat stammdaten-pruefen --firma <id>`; bei ODBC zuerst einen
   `stammdaten.odbc`-Block in `config/firmen.yaml` anlegen und
   `auftragsautomat odbc-erkunden --firma <id>` (Bericht unter `logs/`, nicht committen).
3. Spaltenzuordnung in `config/firmen.yaml` (Anker `x_lexware_export`, `spalten_*`) an die echten
   Kopfzeilen anpassen; `lexware_firmenname` je Firma mit dem Titel in der Lexware-Firmenauswahl
   abgleichen. Bei Fragen zur Mandantenstruktur (`docs/WAS_ICH_BRAUCHE.md`) den Anwender fragen.

Dokumentieren: Quelle je Firma, Anzahl Artikel/Kunden/Lieferanten/Belege (nur Zahlen), geänderte
Spaltennamen. Abbruch: Export unlesbar oder Pflichtspalten fehlen → Anwender um neuen Export bitten.

### Phase 2 – Oberfläche erkunden

1. Den Anwender nacheinander öffnen lassen (offen lassen, nichts eintippen): Hauptfenster,
   Verkauf – Angebot neu, Auftragsbestätigung, Einkauf – Bestellung neu, Lieferschein,
   Formularauswahl-Dialog, Druck-/E-Mail-Abfrage. Danach schließt der Anwender jeweils mit "Abbrechen".
   Formularauswahl: an einem vorhandenen Beleg nur "Vorschau" wählen (nie "Drucken") und den Dialog
   vor der Ausgabe abbrechen. Die Druck-/E-Mail-Abfrage erscheint erst nach dem Speichern; ist sie
   nicht anders erreichbar, in Phase 4 beim ersten scharfen Beleg erkunden (dann "Überspringen").
   Ebenso die Rückfrage beim "Abbrechen" eines gefüllten Assistenten ("Änderungen verwerfen?"): der
   Anwender tippt in einem neuen Assistenten ein Zeichen in ein Feld, klickt "Abbrechen", lässt die
   Rückfrage offen, Claude erkundet, dann beantwortet der Anwender sie mit "Nein"/"Verwerfen".
2. Für jedes Fenster: `auftragsautomat lexware-erkunden --alle-fenster --abgleich`
   (weitere Optionen: `--ziel PFAD`, Standard `logs/lexware_erkundung/<JJJJ-MM-TT_HHMMSS>`;
   `--tiefe N`, Standard 12; `--backend uia|win32|beide`, Standard aus `settings.lexware.backend`).
   Mindestens einmal `--backend beide` für Hauptfenster und Bestellungs-Assistent.
   Nach Änderungen an `config/lexware_controls.yaml` den Abgleich ohne Lexware wiederholen:
   `auftragsautomat lexware-erkunden --aus-datei logs/lexware_erkundung/<ordner>/baum.json`.
   Die Ausgabe unter `logs/` kann Kundendaten enthalten (Listenzeilen, Feldtexte): nie committen,
   nie wörtlich ins Protokoll übernehmen.
3. **Backend entscheiden** (uia oder win32): das, in dem Seitenliste, Felder, Positionsliste und
   Schaltflächen mit stabilen Namen/`auto_id` sichtbar sind. In `config/settings.yaml` `lexware.backend` setzen.
4. `config/lexware_controls.yaml` mit den echten Namen füllen, jedes `TODO-LEXWARE` abarbeiten
   (ersetzt durch "[V-System <Datum>]"), Standardwerte in `LexwareControls` nur ändern, wenn nötig.
   Vorschläge aus `abgleich.md` nie ungeprüft übernehmen: erst in `baum.txt` nachsehen, ob es wirklich
   dasselbe Control ist. **Nie** einen Bezeichner auf ein Control legen, das speichert, druckt, mailt,
   löscht, bucht oder weiterführt, wenn der Bezeichner nicht genau diese Aktion meint (z. B. `WEITER`
   nie auf "Speichern"/"Weiterführen", `ABBRECHEN` nie auf etwas anderes als "Abbrechen").
   `ABBRECHEN`, `SPEICHERN` und die Rückfrage beim Abbrechen müssen eindeutig "gefunden" sein und vom
   Anwender am Bildschirm bestätigt werden, bevor Phase 3 beginnt (der Trockenlauf endet mit "Abbrechen").
   Unter `win32` kennt pywinauto meist kein `control_type`; meldet der Abgleich "passt nur ohne
   control_type", gilt das nur für das gewählte Backend.
   Tests (`tests/test_ui_automation*.py`, `tests/test_dialoge.py`) anpassen, bis alles grün ist.
5. Ist ein Control per UIA nicht erreichbar, Alternativen **in dieser Reihenfolge** prüfen:
   `win32`-Backend → Tastaturkürzel aus der Lexware-Hilfe → openTRANS-Import für den Verkauf
   (`lexware/opentrans.py`, `opentrans.lizenziert` in `config/firmen.yaml`). Nie Pixel-Klicks.

Dokumentieren: Fenstertitel wörtlich (ohne Kundendaten), Backend-Entscheidung mit Begründung,
Anzahl `TODO-LEXWARE` vorher/nachher. Abbruch: Fenster nur als leerer Rahmen sichtbar in beiden
Backends → Anwender informieren, Alternative vorschlagen.

### Phase 3 – Trockenlauf je Beleg und Firma (nichts speichern)

Voraussetzung: Phase 2 erledigt, `ABBRECHEN`/`SPEICHERN` bestätigt (siehe Phase 2, Schritt 4).
Den ersten Lauf je Belegart beobachtet der Anwender (Hand an Esc). Für die UI-Automation ohne
Speichern muss `dry_run` in `config/settings.yaml` **lokal** auf `false` stehen und der Befehl **ohne** `--scharf` laufen – dann
füllt der Automat den Assistenten und bricht vor "Speichern" ab. `lexware.pdf_ausgabe` bleibt `keine`.
Nach der Phase `dry_run: true` zurücksetzen; diese Änderung nie committen.

1. Beispielauftrag: eine Bestellung, die der Anwender auswählt (Datei unter `eingang/`, bleibt lokal),
   oder ein vom Anwender genannter Testkunde. `auftragsautomat pruefen <datei> --firma <id>` → Paket
   unter `logs/pakete/`.
2. Anwender öffnet die richtige Firma. `auftragsautomat --log-level DEBUG buchen <paket> --freigeben`
   (ohne `--scharf`; mit `--lieferschein`, wo erlaubt).
3. Prüfen, Beleg für Beleg: Partner (Kunde/Lieferant), Positionen per Artikelnummer, Mengen,
   **VK Preisgruppe 1** und Positionsrabatte, Belegfelder (Spedition, Weingut, Shipment, Kunde/Customer),
   Nachbemerkung (Kundenbestellnummer), Lieferdatum/AB-Text gelöscht, Formular-Auswahl laut
   `config/firmen.yaml`, Summe gegen das Paket – **Abbruch vor Speichern**. Fehler-Screenshots unter
   `logs/screenshots/` ansehen; der Anwender beobachtet den Bildschirm und bestätigt im Chat.
4. Reihenfolge: zuerst **WIK FH & SEH** (Einkauf + Verkauf), dann **WIK LEH**, **WOE LEH**, zuletzt
   **Weinstrasse Agentur** (nur Angebot → AB; kein Einkauf, kein Lieferschein).

Dokumentieren: je Firma und Belegart ok/Fehler, gefundene Abweichungen und deren Korrektur.
Abbruch: falsche Firma im Titel, Summe weicht ab, Feld nicht gefunden, unerwarteter Dialog.

### Phase 4 – erster scharfer Beleg

Nur mit ausdrücklichem Ja des Anwenders, möglichst mit einem **Testkunden** bzw. in einer
**Testfirma** (dann `lexware_firmenname` nur lokal anpassen, nicht committen). Vorher Zusammenfassung
zeigen (Firma, Belegart, Positionen, Summe). Dann `auftragsautomat buchen <paket> --scharf --ja` bzw.
`lieferschein <paket> --ab NR --scharf --ja` (`dry_run: false` lokal). Danach mit dem Anwender
prüfen: Belegnummer in der Lexware-Auftragsliste, Summen, ggf. PDF. Ein Storno macht der Anwender
selbst. Dokumentieren: Datum, Firma, Belegart, Prüfergebnis (ohne Kundendaten).
Abbruch: jede Abweichung → keine weiteren scharfen Belege, bis geklärt.

### Phase 5 – alle vier Firmen scharf

Wie Phase 4 für jede Firma, jeder Beleg einzeln mit Ja. Dazu: Lieferschein-Nachzug
(`auftragsautomat lieferschein <paket> --ab NR`) und Revised Order (`ist_revision`, neue Cde/AB
statt Änderung). Dokumentieren: je Firma ok/offen. Abbruch wie Phase 4.

### Phase 6 – Aufräumen

`dry_run: true` in `config/settings.yaml` prüfen. Verbleibende `TODO-LEXWARE` auflisten
(`git grep -n TODO-LEXWARE`) und ins Protokoll übernehmen. README (Abschnitte Lexware,
Konfiguration) an den kalibrierten Stand anpassen. Abschluss-Commit und Bericht.
