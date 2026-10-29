# Einrichtung am Windows-PC und Lexware-Kalibrierung

Diese Anleitung ist für Sie als Anwender. Sie richten den Auftrags-Automaten auf dem Windows-PC
ein, auf dem Lexware premium läuft, und starten dort eine Claude-Code-Sitzung. Claude gleicht dann
die Lexware-Anbindung am echten Programm ab ("Kalibrierung"): Fenster- und Feldnamen, Stammdaten,
Ablauf von Bestellung, Angebot → Auftragsbestätigung und Lieferschein in allen vier Firmen.

Die Arbeitsanweisung für Claude steht in `CLAUDE.md` im Projektordner. Am Ende dieser Datei steht
das **Kalibrier-Protokoll**, das Claude nach jeder Phase fortschreibt.

---

## 1. Voraussetzungen

* Windows 10 oder Windows 11.
* Lexware premium ist installiert, und Sie können damit arbeiten.
* Ein eigenes Claude-Konto (Anmeldung auf claude.ai), das Claude Code nutzen darf.
* Zugang zum GitHub-Projekt `github.com/weinstrasse/auftragsautomat` (privat; Anmeldung mit dem
  GitHub-Konto der Weinstrasse).
* Administratorrechte brauchen Sie **nur** für die Installation der Programme in Abschnitt 2.
  Für alles Weitere nicht.

---

## 2. Programme installieren (einmalig)

Sie brauchen drei Programme. Wählen Sie für jedes **entweder** den Download **oder** den
`winget`-Befehl (Eingabeaufforderung oder PowerShell, als Administrator geöffnet).

1. **Git for Windows** (holt das Projekt von GitHub; Claude Code braucht es außerdem für seine
   Befehle).
   * Download: git-scm.com → "Download for Windows", Installation mit den Standard-Einstellungen.
   * oder: `winget install Git.Git`
2. **Python 3.13 (64 Bit)**.
   * Download: python.org → Downloads → Windows → "Windows installer (64-bit)" für Python 3.13.
     Im ersten Fenster des Installers unbedingt **"Add python.exe to PATH"** ankreuzen.
   * oder: `winget install Python.Python.3.13`
3. **Claude** – eine der beiden Varianten:
   * **Claude Desktop-App** (empfohlen): von claude.ai/download herunterladen und installieren,
     mit Ihrem Claude-Konto anmelden. Darin den Bereich für Claude Code ("Code") verwenden.
   * **Claude Code im Terminal**: in PowerShell `irm https://claude.ai/install.ps1 | iex`
     ausführen; danach startet der Befehl `claude` die Sitzung. Beim ersten Start melden Sie sich
     mit Ihrem Claude-Konto an.

Nach der Installation alle offenen Eingabeaufforderungen/PowerShell-Fenster schließen und neu
öffnen, damit die Programme gefunden werden.

---

## 3. Projekt holen (einmalig)

1. Einen Ordner wählen, **nicht** in OneDrive (die Synchronisierung stört bei vielen kleinen
   Dateien und der Python-Umgebung). Empfehlung: `C:\Auftragsautomat`.
2. PowerShell öffnen (ohne Administrator) und eingeben:

   ```powershell
   cd C:\
   git clone https://github.com/weinstrasse/auftragsautomat.git Auftragsautomat
   cd C:\Auftragsautomat
   ```

   Fragt Git nach einer Anmeldung, öffnet sich ein Browserfenster für GitHub; dort anmelden.
3. Ihre Lexware-Exporte (Artikel, Kunden, Lieferanten, Belege) je Firma nach
   `C:\Auftragsautomat\daten\<firma>\` legen, falls Sie sie schon haben (siehe
   `docs/WAS_ICH_BRAUCHE.md`, Abschnitt A1). Der Ordner `daten` wird nie zu GitHub hochgeladen.

---

## 4. Einrichtung ausführen

Das erledigt Claude in Phase 0 selbst. Wenn Sie es vorher selbst ausprobieren möchten:

```powershell
cd C:\Auftragsautomat
powershell -ExecutionPolicy Bypass -File scripts\windows\einrichten.ps1
```

Das Skript prüft Python, legt die Python-Umgebung im Ordner `.venv` an, installiert dort alle
Pakete, führt die Tests aus, prüft, ob Lexware läuft, und nennt die nächsten Schritte. Es
installiert nichts außerhalb des Projektordners und ändert keine Windows-Einstellungen. Man kann es
gefahrlos mehrmals ausführen.

Schalter:

* `-OhneTests` – Tests überspringen (schneller).
* `-MitBrowser` – zusätzlich den Browser für die Sendungsverfolgung laden (nur nötig, wenn Sie die
  Sendungsverfolgung nutzen; landet im Benutzerprofil unter `%LOCALAPPDATA%\ms-playwright`).

---

## 5. Vor jeder Sitzung mit Claude

1. **Lexware selbst starten – nur einmal.** Claude startet Lexware nie; ein zweiter Start bringt
   die Meldung "Anwendung kann nur einmal gestartet werden".
2. Die gewünschte **Firma öffnen** (Claude sagt Ihnen, welche).
3. **Alle offenen Belege und Assistenten schließen**, damit nichts Halbfertiges im Weg ist.
4. **Lexware normal starten, nicht "Als Administrator ausführen".** Falls Lexware bei Ihnen immer
   als Administrator läuft, muss auch die Claude-App bzw. das Terminal als Administrator laufen
   (siehe Fehlerbehebung).
5. Bildschirm entsperrt lassen, Bildschirmschoner/Sperre für die Dauer der Sitzung nicht auslösen.
   Keine Fernwartungs-Sitzung (RDP) minimieren.

---

## 6. Claude Code starten

1. **Desktop-App:** Claude öffnen → Bereich "Code" → als Ordner `C:\Auftragsautomat` wählen.
   **Terminal:** PowerShell öffnen, `cd C:\Auftragsautomat`, dann `claude`.
2. Diesen Text einfügen und absenden:

   > Lies CLAUDE.md und starte die Lexware-Kalibrierung mit Phase 0.

   Bei späteren Sitzungen:

   > Lies CLAUDE.md und das Kalibrier-Protokoll in docs/EINRICHTUNG_WINDOWS.md und mach mit der
   > nächsten offenen Phase weiter.

3. Claude Code fragt vor Befehlen um Erlaubnis ("Darf ich … ausführen?"). Lesen Sie diese
   Rückfragen. Wählen Sie bei Befehlen mit `--scharf` **nie** "immer erlauben".

---

## 7. Was Claude darf – und was nie

Claude **darf**:

* Programme im Projektordner ausführen, Tests laufen lassen, Dateien im Projekt ändern und die
  Änderungen auf GitHub sichern (Arbeitszweig `main`).
* Lexware-Fenster **lesen** (Fenster- und Feldnamen), Assistenten öffnen, Felder probeweise füllen
  und **mit "Abbrechen" wieder schließen**.
* Ihre Stammdaten-Exporte lesen.

Claude tut **nie** (ohne Ihr ausdrückliches "Ja" im Chat für genau diesen einen Schritt):

* in Lexware **speichern**, **buchen**, einen Beleg festschreiben,
* **drucken** oder **E-Mails senden** (das passiert überhaupt nie, auch nicht mit Ja),
* Lexware-Einstellungen oder Stammdaten ändern,
* Lexware starten, beenden oder die Firma wechseln.

Kundendaten (Namen, Adressen, Preise, Kundennummern) gelangen nie in den Programmcode oder nach
GitHub. Erkundungsdateien und Bildschirmfotos bleiben im Ordner `logs` auf Ihrem PC.

---

## 8. Während Claude Lexware bedient

* **Maus und Tastatur nicht benutzen**, solange Claude Lexware steuert. Claude sagt vorher Bescheid
  ("Ich bediene jetzt Lexware, bitte nicht eingreifen") und danach ("fertig").
* Claude bittet Sie manchmal, ein bestimmtes Fenster zu öffnen (z. B. "Verkauf → Angebot neu").
  Öffnen Sie es, lassen Sie es offen und schreiben Sie "offen" in den Chat.
* **Abbrechen jederzeit:** in Claude Code die Taste **Esc** drücken. Danach in Lexware einen offenen
  Assistenten mit "Abbrechen" schließen (nicht speichern).

---

## 9. Fehlerbehebung

| Problem | Lösung |
|---|---|
| `python` oder `py` wird nicht gefunden | Python 3.13 neu installieren und "Add python.exe to PATH" ankreuzen; danach PowerShell neu öffnen. Öffnet `python` den Microsoft Store: Einstellungen → Apps → Erweiterte App-Einstellungen → App-Ausführungsaliase → "python.exe" und "python3.exe" ausschalten. |
| "Die Ausführung von Skripts ist auf diesem System deaktiviert" | In derselben PowerShell `Set-ExecutionPolicy -Scope Process Bypass` eingeben (gilt nur für dieses Fenster) oder das Skript mit `powershell -ExecutionPolicy Bypass -File scripts\windows\einrichten.ps1` starten. |
| Fehler beim Installieren von `pywinauto`/`pywin32` | Einrichtung erneut ausführen. Hilft das nicht: `.venv\Scripts\python -m pip install --force-reinstall pywin32 pywinauto` und die Meldung an Claude geben. |
| `.venv` hat eine andere Python-Version | Den Ordner `.venv` umbenennen (z. B. `.venv_alt`) und die Einrichtung erneut ausführen. |
| Claude "sieht" die Lexware-Fenster nicht oder findet kein Hauptfenster | Läuft Lexware als Administrator? Dann auch die Claude-App bzw. PowerShell mit "Als Administrator ausführen" starten – sonst blendet Windows die Fenster für die Automatisierung aus. Außerdem: Bildschirm entsperrt? Lexware wirklich gestartet (Task-Manager)? |
| "Anwendung kann nur einmal gestartet werden" | Lexware lief schon. Meldung schließen, das vorhandene Lexware-Fenster verwenden. Claude startet Lexware nie selbst. |
| Umlaute erscheinen als Zeichensalat oder `UnicodeEncodeError` | In der PowerShell vor dem Start `$env:PYTHONUTF8 = "1"` setzen. |
| `git clone` fragt nach Benutzer/Passwort | Im erscheinenden Browserfenster bei GitHub anmelden. Ohne Zugriff auf das Projekt bitte den Projektinhaber um Freigabe bitten. |

---

## Kalibrier-Protokoll

Claude schreibt diesen Abschnitt nach jeder Phase fort (Datum, Ergebnis, offene Punkte) und sichert
ihn mit einem Commit. Hier stehen nur Fenster-/Feldnamen, Versionsstände und Entscheidungen – **keine
Kundendaten**, keine Belegnummern echter Kunden, keine Preise.

### Phase 0 – Umgebung

| Punkt | Ergebnis |
|---|---|
| Datum / Windows-Version | offen |
| Lexware-Version (Hilfe → Info) | offen |
| Python-Version und Bitness (32/64) | offen |
| Prozessname Lexware (Task-Manager → Details) und Plattform (32/64 Bit) | offen (erwartet `Framework.exe`) |
| Lexware als Administrator gestartet? | offen |
| Einrichtung (`einrichten.ps1`) und Tests | offen |

### Phase 1 – Stammdaten

| Firma | Quelle (CSV/ODBC) | `stammdaten-pruefen` | Spaltenänderungen in `config/firmen.yaml` | offen |
|---|---|---|---|---|
| wik_fh_seh | offen | offen | – | – |
| wik_leh | offen | offen | – | – |
| woe_leh | offen | offen | – | – |
| weinstrasse_agentur | offen | offen | – | – |

### Phase 2 – Oberfläche

| Fenster | Titel (wörtlich, Kundendaten durch `…` ersetzt) | Backend | erledigt |
|---|---|---|---|
| Hauptfenster | offen | offen | nein |
| Verkauf – Angebot neu | offen | offen | nein |
| Auftragsbestätigung | offen | offen | nein |
| Einkauf – Bestellung neu | offen | offen | nein |
| Lieferschein | offen | offen | nein |
| Formularauswahl | offen | offen | nein |
| Druck-/E-Mail-Abfrage | offen | offen | nein |

Entscheidung Backend (uia/win32) und Begründung: offen.
Anzahl `TODO-LEXWARE` in `config/lexware_controls.yaml` vorher/nachher: offen.

### Phase 3 – Trockenlauf (ohne Speichern)

| Firma | Bestellung (Einkauf) | Angebot | Angebot → AB | Lieferschein | Anwender bestätigt |
|---|---|---|---|---|---|
| wik_fh_seh | offen | offen | offen | offen | nein |
| wik_leh | offen | offen | offen | offen | nein |
| woe_leh | offen | offen | offen | offen | nein |
| weinstrasse_agentur | entfällt | offen | offen | entfällt | nein |

### Phase 4 – erster scharfer Beleg

| Datum | Firma | Belegart | Testkunde/Testfirma? | Summen/PDF geprüft | Storno nötig? |
|---|---|---|---|---|---|
| offen | | | | | |

### Phase 5 – Betrieb aller vier Firmen

| Firma | scharfer Lauf | Lieferschein-Nachzug | Revised Order | offen |
|---|---|---|---|---|
| wik_fh_seh | offen | offen | offen | |
| wik_leh | offen | offen | offen | |
| woe_leh | offen | offen | offen | |
| weinstrasse_agentur | offen | entfällt | offen | |

### Phase 6 – Aufräumen

Verbleibende `TODO-LEXWARE` (Datei: Zeile – was fehlt): offen.
