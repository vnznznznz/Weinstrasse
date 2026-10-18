# Was ich von dir brauche

Stand: 18.10.2026. Das Grundgerüst (Extraktion, Matching, Prüfung, Freigabe-Ansicht,
Dry-Run-Buchung, openTRANS-Export, UI-Automation-Gerüst) ist gebaut und mit
synthetischen Daten getestet. Um es an **deine** Belege und **dein** Lexware anzupassen,
brauche ich die folgenden Dinge. Die Reihenfolge entspricht dem Vorgehen aus deiner
Spezifikation: erst Extraktion + Matching + Freigabe an echten PDFs, dann Lexware im
Dry Run, erst danach scharf.

Kurzfassung der drei wichtigsten Punkte:

1. **Beispiel-Belege** (echte PDFs/E-Mails/Scans, gern anonymisiert) in `beispiele/`.
2. **Stammdaten-Export** aus Lexware je Firma (Artikel, Kunden, Lieferanten, Belege) in `daten/<firma>/`.
3. **Antworten auf die Verständnisfragen** in Abschnitt C (Firmenzuordnung, Belegfelder, "180 R", "11+1").

---

## A. Für Phase 1: Extraktion, Matching, Freigabe an echten Daten

### A1. Beispiel-Belege (mindestens 3 bis 5 je Eingangstyp)

| Typ | Was | Worauf es ankommt |
|---|---|---|
| A | Kundenbestellungen als Text-PDF | möglichst verschiedene Kunden und Layouts (Tabelle, Fließtext, mit/ohne Jahrgang, mit "11+1", mit 0,00-€-Positionen wie "DAA Neptun", mit abweichender Lieferadresse) |
| B | Eigene Bestellungen an Weingüter | als `.msg` oder `.eml` direkt aus Outlook gespeichert (Datei → Speichern unter), gern auch mit PDF-Anhang; falls nur als PDF vorhanden, auch gut |
| C | Eingescannte Preislisten mit handschriftlichen Mengen | als PDF oder JPG/PNG, inklusive Beispielen mit durchgestrichenen Zeilen/Preisen und mit Summenvermerk ("180 R") |

Ablage: Ordner `beispiele/A_kundenbestellung/`, `beispiele/B_eigene_bestellung/`,
`beispiele/C_scan_preisliste/` im Repository. Anonymisieren ist in Ordnung (Namen und
Adressen schwärzen oder ersetzen), aber **Layout, Spaltenaufbau und Formulierungen
müssen original bleiben**, sonst lerne ich die falschen Muster. Zu jedem Beleg hilft
mir ein Satz "Sollergebnis" (Kunde, Lieferant, Firma, Positionen → Artikelnummern),
damit ich die Erkennung automatisch dagegen testen kann.

### A2. Stammdaten-Export aus Lexware, je Firma

Für jede der vier Firmen (Weinstrasse Agentur, Wein-Import-Kreis FH & SEH,
Wein-Import-Kreis LEH, Wine-of-Excellence LEH) je ein Export:

| Datei | Pflichtfelder | Hilfreich zusätzlich |
|---|---|---|
| `artikel.csv` | Artikelnummer, Bezeichnung, Warengruppe, Lieferantennummer, EK-Preis, VK-Preis Preisgruppe 1 | Inhalt/Volumen, Einheit, Jahrgang, Matchcode, aktiv/gesperrt |
| `kunden.csv` | Kundennummer, Name, PLZ, Ort | Name 2, Straße, Preisgruppe, Matchcode |
| `lieferanten.csv` | Lieferantennummer, Name | Ort, Matchcode |
| `belege.csv` | Belegart, Belegnummer, Kundenbestellnummer, Kunden-/Lieferantennummer, Datum | Netto (für die Dublettenprüfung reichen die letzten 12 Monate) |

Ablage: `daten/<firma>/` (`weinstrasse_agentur`, `wik_fh_seh`, `wik_leh`, `woe_leh`).
Dieser Ordner ist per `.gitignore` vom Repository ausgeschlossen, echte Stammdaten
landen also nicht auf GitHub. Format: CSV mit Semikolon, wie Lexware es exportiert.
Die Spaltennamen des Exports dürfen abweichen, ich bilde sie in `config/firmen.yaml`
(`spalten_artikel` usw.) auf meine Feldnamen ab. Dafür brauche ich einmalig die
**Kopfzeile jeder Exportdatei**.

Wie der Export in Lexware geht, steht nach der Recherche in
`docs/recherche/odbc_lexware.md` (Abschnitt CSV-Export). Wenn du den Export lieber
per ODBC ersetzen willst, siehe B2.

### A3. Alias-Tabellen bestätigen

Die Tabellen in `config/aliase/` enthalten die Beispiele aus deiner Spezifikation:

* Kunden: Weinmarkt Kirchhellen → 3441 (Hegmann/Bottrop-Kirchhellen), Weinhaus Menrath → 1392
* Lieferanten: Colosi → 87, Bollina srl → 138, Cide → 72 (Villa Armellina läuft über Cide)
* Artikel: Villa Armellina Gloria Secco Frizzante → 1643, Rosato Secco Frizzante → 957
* Überspringen: "DAA Neptun"

Ich habe alle vorläufig der Firma `wik_leh` zugeordnet (mit `# TODO prüfen` markiert).
**Bitte sag mir, in welcher Firma (Mandant) diese Nummern jeweils gelten.** Weitere
Aliase kannst du jederzeit selbst in den YAML-Dateien ergänzen, das Format ist dort erklärt.

### A4. Exakte Firmennamen

Die Namen in der Lexware-Firmenauswahl (Datei → Firma wechseln) müssen exakt in
`config/firmen.yaml` unter `lexware_firmenname` stehen, sonst verweigert die
UI-Automation die Buchung (Schutz gegen die falsche Firma). Bitte die vier Namen
buchstabengetreu nennen oder einen Screenshot der Firmenauswahl schicken.

---

## B. Für Phase 2: Lexware-Anbindung (Dry Run)

### B1. Version und Module

* Lexware → Hilfe → Info: genaue Produktbezeichnung, Version, Build (Screenshot).
* Ist der Menüpunkt **eBusiness → Standard-Shopschnittstelle** vorhanden? Falls ja:
  Screenshot des Import-Dialogs und der Einstellungen (Importverzeichnis, Kundenabgleich).
  Falls nein: dann läuft auch die Verkaufs-AB über die UI-Automation, das ist eingeplant.
* Welche Belege sind im Einkauf vorhanden (Bestellung, Wareneingang, Eingangsrechnung)?

### B2. Datenbankzugriff (optional, sonst bleibt es beim CSV-Export)

* Ist in Lexware ein **ODBC-/Datenbankzugriff** freigeschaltet (Extras → Optionen bzw.
  Lexware Info Center)? Wenn ja: DSN-Name und Benutzer (nur lesend) nennen; das Passwort
  nicht per Mail schicken, sondern auf dem PC als Umgebungsvariable hinterlegen
  (`config/firmen.yaml` → `passwort_env`).
* Mit `auftragsautomat odbc-erkunden --firma <id>` schreibe ich dann eine Liste aller
  Tabellen und Spalten, aus der ich die richtigen Abfragen ableite.

### B3. Einblick in die Lexware-Oberfläche

Für die UI-Automation brauche ich die echten Bezeichner der Fenster und Felder. Bitte
auf dem Lexware-PC (Python 3.11+ und `pip install -e ".[windows]"` vorausgesetzt):

1. `auftragsautomat lexware-inspizieren` ausführen, während der Assistent
   **"Bestellung schreiben"** in Schritt 1 offen ist; dann erneut in Schritt 2
   (Positionen) und Schritt 3 (Summe). Das Skript schreibt die Steuerelemente nach
   `logs/inspektion/`.
2. Dasselbe für **"Auftrag schreiben" / Auftragsbestätigung** im Verkauf.
3. Screenshots der Dialoge nach dem Speichern ("per E-Mail/Ausdruck", "USt-IdNr. prüfen")
   und des Kontextmenüs **"Auftrag weiterführen"**.
4. Screenshot eines fertigen Einkaufsbelegs, auf dem die Felder **Spedition, Weingut,
   Shipment und Kunde/Customer** sichtbar sind (siehe C2).

### B4. Testumgebung

* Gibt es einen **Test-Mandanten** oder darf ich vor dem ersten scharfen Lauf eine
  Datensicherung (Lexware → Datei → Datensicherung) einplanen? Ich empfehle beides.
* Der Dry Run bricht vor "Speichern" ab, erzeugt aber in Lexware sichtbare Aktivität
  (geöffnete Assistenten). Bitte einen Zeitpunkt nennen, zu dem niemand sonst in Lexware
  arbeitet.

### B5. Claude-API-Zugang für die Scans

* API-Schlüssel aus der Anthropic Console, auf dem PC als Umgebungsvariable
  `ANTHROPIC_API_KEY` hinterlegen (nie in Dateien im Repository).
* Kosten: pro gescannter Seite grob im Cent-Bereich; ich protokolliere die
  Token-Nutzung je Aufruf im Log.

### B6. Später: Outlook-Anhänge

* Welches Postfach und welcher Ordner (z. B. "Bestellungen")? Lokales Outlook auf dem
  PC oder Exchange Online? Sollen verarbeitete Mails verschoben werden?

---

## C. Verständnisfragen, die das Verhalten bestimmen

1. **Firma je Eingang:** Ich schlage vor, dass der Eingangsordner Unterordner je Firma
   hat (`eingang/wik_leh/…`). Dann ist die Firma eindeutig. Alternativ ermittle ich sie
   über Kunde/Lieferant, das ist aber bei Kunden, die in mehreren Firmen existieren, nicht
   eindeutig. Einverstanden mit Unterordnern?
2. **Belegfelder "Spedition, Weingut, Shipment, Kunde":** Sind das Lexware-Zusatzfelder
   am Beleg, Textfelder im Kopf oder Freifelder? Was genau soll in "Shipment" stehen
   (Liefertermin, Speditions-Referenz, Abholdatum)? Woher kommt "Spedition" (fest je
   Firma, je Kunde oder je Bestellung)?
3. **"180 R":** Was bedeutet das R im Summenvermerk (Flaschen, Kartons, "Rechnung")?
   Davon hängt ab, wogegen ich die Gesamtmenge prüfe.
4. **"11+1":** Wie wird das in Lexware gebucht: 12 Flaschen mit Preis für 11, eine
   Gratisposition zu 0,00 €, oder Rabatt auf der Position? Gilt es im Einkauf und im
   Verkauf?
5. **Preisgruppe:** Gilt für die Verkaufs-AB immer Preisgruppe 1, auch wenn der Kunde in
   Lexware eine andere Preisgruppe hat? Oder soll ich die Kunden-Preisgruppe nehmen und
   nur gegen den PDF-Preis warnen?
6. **Jahrgang ohne Angabe:** Wenn das PDF keinen Jahrgang nennt und es mehrere Jahrgänge
   im Stamm gibt: immer nachfragen (aktuell so) oder den neuesten Jahrgang vorschlagen?
7. **Einkauf bei Weinstrasse Agentur:** Dort entfällt die Bestellung. Entfällt dann auch
   der Lieferant, oder soll er trotzdem in der AB bzw. in den Belegfeldern stehen?
8. **Lieferschein:** Nur auf Knopfdruck in der Freigabe (so gebaut), nie automatisch.
   Richtig?

---

## D. Was du dafür von mir bekommst

* Lokale Freigabe-Ansicht (`auftragsautomat freigabe`): Tabelle mit Zuordnung, Menge,
  EK/VK aus Lexware, PDF-Preis, Abweichungen rot, Buttons "Buchen" und "Korrigieren".
* Protokoll je Lauf in `logs/protokoll.csv` plus Kurzbericht je Beleg.
* Dry Run als Standard; scharf nur mit `--scharf` und Bestätigung.
* Alle Annahmen über Lexware sind im Code mit `TODO-LEXWARE` markiert und in
  `docs/recherche/` begründet, damit wir sie gemeinsam am System abhaken können.
