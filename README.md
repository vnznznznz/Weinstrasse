# Auftrags-Automat für Lexware premium

## Zweck

Der Auftrags-Automat nimmt Kundenaufträge (PDF, Scan, E-Mail) aus dem OneDrive-Ordner "Eingang"
entgegen, gleicht sie gegen den Lexware-Stamm ab, prüft sie und zeigt sie zur Freigabe an. Nach der
Freigabe legt er in Lexware premium die Belege an (Einkauf-Bestellung an das Weingut, Angebot,
Auftragsbestätigung, optional Lieferschein), trägt den Fixtermin in die Fixterminüberwachung ein,
erzeugt E-Mail-Entwürfe, ein Dossier je Auftrag und legt die fertigen Dateien in OneDrive ab. Ein
täglicher Überwachungslauf prüft offene Fixtermine, Bestätigungen und Sendungsstatus.

Vier Firmen (Lexware-Mandanten), konfiguriert in `config/firmen.yaml`:

| ID | Firma | Kürzel |
|---|---|---|
| `weinstrasse_agentur` | Weinstrasse Agentur | WS |
| `wik_fh_seh` | Wein-Import-Kreis FH & SEH | WIK |
| `wik_leh` | Wein-Import-Kreis LEH | WIK |
| `woe_leh` | Wine-of-Excellence LEH | WOE |

Grundsätze: Standard ist der Dry Run. E-Mails entstehen nur als Entwurf und werden nie gesendet.
Es wird nichts gedruckt. Die Auftragsbestätigung heißt gegenüber Kunden nur "Auftragskopie".

## Einrichtung am Windows-PC

Schritt-für-Schritt-Anleitung für den Lexware-PC (Programme installieren, Projekt holen, Claude Code
starten, Lexware-Kalibrierung mit Kalibrier-Protokoll): `docs/EINRICHTUNG_WINDOWS.md`. Die
Einrichtung der Python-Umgebung erledigt `scripts/windows/einrichten.ps1`
(`powershell -ExecutionPolicy Bypass -File scripts\windows\einrichten.ps1`). Die Arbeitsanweisung
und Sicherheitsregeln für die Claude-Code-Sitzung am PC stehen in `CLAUDE.md`.

## Installation unter Windows

Voraussetzungen: Windows-PC mit Lexware premium, Python 3.11 oder neuer (entwickelt mit 3.13),
für Microsoft 365 ein Konto mit Zugriff auf die OneDrive-/SharePoint-Ordner.

```bat
cd C:\Pfad\zum\Projektordner
py -3.13 -m venv .venv
.venv\Scripts\activate
pip install -e ".[windows]"
```

Extras laut `pyproject.toml`:

| Extra | Inhalt | Wann nötig |
|---|---|---|
| `windows` | pyodbc, pywinauto, extract-msg, pywin32, xlwings | immer auf dem Lexware-PC (UI-Automation, ODBC, `.msg`, Outlook/Excel per COM) |
| `tracking` | playwright | nur für die öffentliche Sendungsverfolgung bzw. das Portal als Rückfall |
| `dev` | pytest, pytest-cov, ruff | Tests und Lint |

Mit Sendungsverfolgung über den Browser:

```bat
pip install -e ".[windows,tracking]"
playwright install chromium
```

Ohne Playwright meldet die Tracking-Kette die Quelle `public_tracking` als nicht verfügbar und
arbeitet mit Mail (und API, falls eingerichtet) weiter. Der Portal-Rückfall nutzt das installierte
Google Chrome mit einem eigenen Profil (`auftragsautomat tracking-einrichten`).

Umgebungsvariablen (nie in Dateien ablegen):

* `ANTHROPIC_API_KEY` – nur für Scans ohne Textebene und Rabattlisten ohne Textebene (Claude Vision).
  Ohne Schlüssel meldet die CLI eine einzeilige Fehlermeldung.
* `HILLEBRAND_API_KEY`, `HILLEBRAND_API_SECRET`, `HILLEBRAND_API_BENUTZER`, `HILLEBRAND_API_PASSWORT` –
  nur, wenn ein API-Zugang der Spedition vorliegt (Namen änderbar unter `tracking.api_*_env`).
* `AUFTRAGSAUTOMAT_HOME` – optional: Projektstamm mit `config/` (sonst der Projektordner).

Täglicher Überwachungslauf: in der Windows-Aufgabenplanung `auftragsautomat ueberwachen` (bzw.
`auftragsautomat ueberwachen --scharf`, siehe "Dry Run und scharfer Betrieb") werktags morgens starten.

## Konfiguration

Alle Dateien liegen in `config/`. Pfade sind relativ zum Projektordner, sofern nicht absolut.

### `settings.yaml`
Allgemeine Einstellungen: Ordner (`eingang_ordner`, `archiv_ordner`, `log_ordner`,
`xml_ausgabe_ordner`), `dry_run` (Standard `true`), Schwellen des Matchings, Claude-Modell,
Lexware (`exe_pfad`, `fenster_titel_regex`, `backend`, `controls_datei`, `pdf_ausgabe`
keine|drucker|export, `pdf_drucker`, `pdf_ausgabe_ordner`, `firmenwechsel_erlaubt`) sowie die
Abschnitte `m365`, `kommunikation`, `tracking`, `ueberwachung`, `kontakte`, `dossier`, `benennung`,
`rabatte`, `firmenwahl`, `eingang`, `ablage`. Ohne `lexware.pdf_ausgabe` (Standard `keine`) entstehen
keine Beleg-PDFs; Entwürfe haben dann keinen Anhang und die Ablage enthält nur das Eingangsdokument.

### `firmen.yaml`
Je Firma: `name`, `lexware_firmenname` (exakt wie in der Lexware-Firmenauswahl), `kuerzel`,
`einkauf_bestellung`, `verkauf_weg` (`angebot_weiterfuehren` | `angebot_wandeln` | `direkt_ab`),
`lieferdatum_in_ab_loeschen`, `ab_text_loeschen`, `bestellnummer_in_nachbemerkung`,
`lieferschein_erlaubt`, `formulare` (`einkauf_en`, `angebot_en`, `verkauf_ab`, `lieferschein`,
`speditionsauftrag_kopien`), `kundengruppen`, `eingang_unterordner`, `stammdaten` (`csv` mit Pfaden
und Spaltenzuordnung der Lexware-Exporte oder `odbc`, nur lesend), `opentrans` (`import_ordner`,
`lizenziert`) und `belegfelder_standard`. Die Stammdaten-Exporte liegen unter
`daten/<firma>/*.csv` und gehören nicht ins Repository.

### `firmenkatalog.yaml` und `firmen_gedaechtnis.yaml`
`firmenkatalog.yaml` ordnet Kunden über Textmuster einer Kundengruppe zu (`fachhandel`,
`eigenstaendiger_leh`, `region`, `kette`, `grosskunde`) und legt je Gruppe die möglichen Firmen und
die Entscheidung fest (`eindeutig`, `lieferant`, `fragen`). Abschnitt `lieferanten` bestimmt im
Fachhandel die Firma je Lieferant. `firmen_gedaechtnis.yaml` schreibt das Tool selbst (gelernte
Antworten je Kunde + Lieferant). Details: "Firmenwahl und Gedächtnis".

### Aliase (`config/aliase/`)
* `kunden.yaml` – Schreibweisen im Dokument -> Kundennummer und Firma.
* `lieferanten.yaml` – Schreibweisen und Produktmarken -> Lieferantennummer und Firma.
* `artikel.yaml` – Bezeichnungen -> Artikelnummer; `ueberspringen` für Positionen ohne Stammartikel.
* `kurznamen.yaml` – Kurznamen für die Fixterminüberwachung, wenn der Lexware-Matchcode nicht passt.

Ein Alias schlägt das unscharfe Matching. Aliase lassen sich in der Freigabe-Oberfläche speichern.

### Microsoft 365 (`m365` in `settings.yaml`)
* `backend`: `fake` (Standard; Entwürfe als `.eml`, Excel nur Vorschau), `graph` (empfohlen) oder
  `lokal` (Outlook/Excel per COM, klassisches Outlook nötig).
* Für `graph`: eigene App-Registrierung in Microsoft Entra (Single-Tenant, Plattform "Mobile and
  desktop applications", "Allow public client flows" = Yes, delegierte Rechte laut `m365.bereiche`),
  dann `tenant_id` und `client_id` eintragen und einmal `auftragsautomat m365-anmelden` ausführen.
* Dateien: `fixtermin_datei`, `kontaktliste_datei`, `dossier_ordner` je mit `web_url` (Adresszeile
  aus dem Browser) oder `lokaler_pfad` (OneDrive-synchronisierter Pfad); `postfach` für ein
  Funktionspostfach, sonst das angemeldete Konto.
* Prüfen: `auftragsautomat m365-pruefen` (liest nur).

```yaml
m365:
  backend: graph
  tenant_id: "<Directory-ID>"
  client_id: "<Application-ID>"
  fixtermin_datei:
    web_url: "https://<tenant>.sharepoint.com/sites/<site>/<pfad>/Fixterminüberwachung.xlsx"
    blatt: "Fixterminüberwachung"
```

### Signaturen, Anrede und Vorlagen
* `kommunikation` in `settings.yaml`: `absender_name`, `absender_email` (z. B.
  `auftraege@example.org`), `cc_standard`, `entwuerfe_ordner`, `verbotene_woerter_auftragskopie`.
* `signaturen.yaml`: Signatur je Firma (Variante `persoenlich` oder `team`); die Beispielwerte vor dem
  scharfen Betrieb ersetzen.
* `anrede.yaml`: Anrede und Grußformel je Sprache ("Hallo Frau Beispiel!", "Dear Ms Example,"),
  feste Regeln je Adresse.
* `vorlagen/*.html.j2` und `*.txt.j2`: Texte der Entwürfe (`bestellung_weingut`, `auftragskopie_kunde`,
  `spedition_auftrag`, `lieferschein`, `nachfrage_lieferant`, `nachfrage_spedition`,
  `erinnerung_intern`).

### Ablage (`ablage` in `settings.yaml`)
`aktiv` (Standard `false`), `auftraege_ordner` (OneDrive-Ordner, der "Aufträge mit Provision" und
"Aufträge ohne Provision" enthält), `ordner_mit_provision`, `ordner_ohne_provision`,
`firmen_mit_provision` (Standard `[weinstrasse_agentur]`), `struktur` (Standard `"{provision}"`,
weitere Platzhalter `{firma}`, `{jahr}`, `{monat}`, `{lieferant}`, `{kunde}`), `nur_scharf`,
`vorhandene_ueberschreiben` (Standard `false`), `kundenbestellung_ablegen`.

### Eingang (`eingang` und `eingang_ordner` in `settings.yaml`)
`eingang_ordner` auf den lokal synchronisierten OneDrive-Ordner "Eingang" setzen, z. B.
`"C:/Users/<benutzer>/OneDrive - <organisation>/Eingang"`. Weitere Schalter: `upload_erlaubt`,
`upload_max_mb`, `upload_dateitypen`, `upload_in_firmen_unterordner`, `als_kundenbestellung_ablegen`.
`eingang.onedrive_ordner` ist vorgesehen, wird aber noch nicht ausgewertet; maßgeblich ist der
synchronisierte Pfad.

### Weitere Dateien
`ueberwachung.yaml` (Ampel-Feinheiten und Texte des Überwachungslaufs), `tracking_hillebrand.yaml`
(Muster, URLs und Selektoren der Sendungsverfolgung), `lexware_controls.yaml` (Control-IDs der
Lexware-Oberfläche, am echten System zu prüfen), `aktionen.yaml` (Aktionen aus Rabattlisten, legt
das Tool an).

## Bedienung

1. Auftrag in den OneDrive-Ordner "Eingang" legen oder in der Freigabe-Oberfläche hochladen.
2. `auftragsautomat freigabe` starten (Browser, Port 8501) oder `auftragsautomat watch` laufen lassen.
   Neue Dateien werden extrahiert, geprüft und als Paket unter `logs/pakete/` gespeichert.
3. In der Freigabe Firma, Kunde, Lieferant, Positionen, Mengen, Preise, Rabatte und Belegfelder prüfen,
   korrigieren, freigeben und buchen. Blockierte Pakete (Fehler) lassen sich nicht buchen.
4. Nach dem Buchen zeigt die Oberfläche Belegnummern, Dateinamen, Fixtermin, Entwürfe, Dossier und
   Ablage. Entwürfe in Outlook prüfen und selbst senden.
5. Täglich `auftragsautomat ueberwachen`; Ergebnis auf der Seite "Heute" oder mit `auftragsautomat heute`.

## CLI-Befehle

Aufruf `auftragsautomat [--home PFAD] [--log-level STUFE] BEFEHL …`. Ohne `--scharf` ist jeder Befehl
ein Dry Run.

| Befehl | Kurzbeschreibung |
|---|---|
| `version` | Versionsnummer ausgeben |
| `extrahieren DATEI [--json]` | Datei nur extrahieren und die Rohbestellung anzeigen |
| `pruefen DATEI [--firma ID] [--json] [--paket-ordner P]` | extrahieren, abgleichen, prüfen, Paket speichern |
| `buchen PAKET [--scharf] [--lieferschein] [--freigeben] [--ja]` | gespeichertes Paket buchen; scharf nur nach Sicherheitsabfrage (`--ja` überspringt sie) |
| `lieferschein PAKET --ab NR [--scharf] [--ja]` | nur den Lieferschein zu einer vorhandenen AB anlegen |
| `freigabe [--port 8501] [--kein-browser]` | Freigabe-Oberfläche (Streamlit) starten |
| `watch [--einmalig] [--paket-ordner P]` | Eingang beobachten: extrahieren, prüfen, Paket speichern; bucht nie |
| `stammdaten-pruefen [--firma ID]` | Stammdaten laden und Datenqualität berichten |
| `odbc-erkunden --firma ID [--ziel DATEI] [--praefix X]` | Lexware-ODBC-Schema als Bericht (nur lesend) |
| `lexware-erkunden [--ziel PFAD] [--tiefe N] [--alle-fenster] [--backend uia\|win32\|beide] [--abgleich] [--aus-datei BAUM.JSON]` | Control-Baum des laufenden Lexware nach `logs/lexware_erkundung/<JJJJ-MM-TT_HHMMSS>/` (nur lesend, startet Lexware nie); mit `--abgleich` Vergleich mit `config/lexware_controls.yaml` (`abgleich.md`) |
| `beispiel-pdfs [ORDNER]` | synthetische Beispiel-PDFs erzeugen (Standard `tests/fixtures/pdfs`) |
| `m365-anmelden [--device-code] [--nur-status]` | Microsoft-365-Anmeldung bzw. Status |
| `m365-pruefen` | nur lesen: Fixterminüberwachung, Kontaktliste, Dossier-Ordner |
| `fixtermin-eintragen PAKET [--scharf]` | Fixtermin eintragen; ohne `--scharf` nur `logs/fixtermine_vorschau.csv` |
| `entwuerfe PAKET [--scharf]` | E-Mail-Entwürfe erzeugen; ohne `--scharf` als `.eml` unter `logs/entwuerfe` |
| `dossier PAKET [--scharf]` | Dossier anlegen/fortschreiben; ohne `--scharf` unter `logs/dossiers` |
| `ablegen PAKET [--scharf]` | fertige Dateien nach "Aufträge mit/ohne Provision"; ohne `--scharf` nur Protokoll |
| `benennen PAKET --kuerzel K [--datum TT.MM.JJJJ]` | Dateinamen laut Titulierung anzeigen |
| `firma-merken PAKET --firma ID` | Firma setzen, neu prüfen und je Kunde + Lieferant merken |
| `ueberwachen [--tage N] [--scharf] [--nur-lesen]` | täglicher Überwachungslauf mit Bericht |
| `heute` | letzten Überwachungsbericht anzeigen |
| `tracking REFERENZ [--kundenreferenz] [--spedition S]` | Sendungsstatus über die Tracking-Kette |
| `tracking-einrichten` | Portal-Login einmalig im eigenen Chrome-Profil einrichten |
| `kontakte-pruefen` | Kontaktliste laden und zusammenfassen |
| `rabatte-einlesen PDF [--uebernehmen]` | Rabattlisten-PDF einlesen; mit `--uebernehmen` nach `config/aktionen.yaml` |
| `aktionen [--firma ID] [--datum TT.MM.JJJJ] [--alle]` | gültige Aktionen anzeigen |

`PAKET` ist eine gespeicherte Paketdatei aus `logs/pakete/`.

## Dry Run und scharfer Betrieb

Scharf geschrieben wird nur, wenn alle Sicherungen offen sind:

1. `dry_run: false` in `config/settings.yaml`,
2. `--scharf` auf der Kommandozeile (bei `buchen` zusätzlich die Sicherheitsabfrage) bzw. der
   ausgeschaltete Dry-Run-Schalter mit Bestätigung in der Freigabe-Oberfläche,
3. für Lexware: Windows und eine erreichbare Lexware-Instanz.

| Situation | Lexware | Fixtermin | Entwürfe | Dossier | Ablage |
|---|---|---|---|---|---|
| `settings.dry_run: true` (Standard) | nur Simulation (DryRunSchreiber), openTRANS-XML nach `logs/opentrans` | Vorschau-CSV | `.eml` unter `logs/entwuerfe` | `logs/dossiers` | nur Protokoll |
| `settings.dry_run: false`, ohne `--scharf` | UI-Automation bis kurz vor "Speichern", dann Abbrechen | Vorschau-CSV | `.eml` | `logs/dossiers` | nur Protokoll |
| `settings.dry_run: false`, mit `--scharf` | Belege werden gespeichert | Eintrag in der Excel | Entwürfe im Postfach (nie gesendet) | OneDrive "vorangefertigte Dossiers" | Kopie nach OneDrive, wenn `ablage.aktiv` |

Die Spalten Fixtermin, Entwürfe und Dossier setzen für den scharfen Lauf `m365.backend: graph` oder
`lokal` voraus; mit `fake` bleibt alles lokal unter `logs/`. Die UI-Automation ohne `--scharf` läuft
nur unter Windows mit erreichbarem Lexware, sonst simuliert der DryRunSchreiber.

Ist `settings.dry_run` an, läuft auch ein Befehl mit `--scharf` als Dry Run (mit Hinweis). Die
Quelldatei wird nur nach einer scharfen, erfolgreichen Buchung nach `archiv/<firma>/<jahr>/`
verschoben. Jede Buchung (auch Dry Run) steht in `logs/protokoll.csv`.

## Sicherheitsregeln

* **Nie senden:** Kein Backend hat eine Sendefunktion. E-Mails entstehen nur als Entwurf (Outlook,
  Graph oder `.eml`); gesendet wird von Hand.
* **Nie drucken:** PDFs nur über PDF-Drucker oder Export (`lexware.pdf_ausgabe`); der Lexware-Dialog
  "per E-Mail/Ausdruck" wird mit "Überspringen", "USt-IdNr. prüfen?" mit "Nein" beantwortet.
* **Auftragskopie:** Gegenüber Kunden heißt die AB nur "Auftragskopie". Enthalten Betreff, Text oder
  Dateiname eines der Wörter aus `kommunikation.verbotene_woerter_auftragskopie` (u. a.
  "Auftragsbestätigung", "bestätigen", "verbindlich"), wird der Entwurf nicht erzeugt. Der Titel des
  Lexware-Formulars muss in Lexware ebenfalls auf "Auftragskopie" lauten.
* **Excel:** In der Fixterminüberwachung schreibt das Tool nur die Datenspalten D..K einer Zeile, nie
  die Spalten A–C (Formeln) und nie das Blatt "Ressourcen für Auto DropDown". Ist kein Slot frei,
  meldet es einen Fehler statt zu überschreiben. Excel-Dateien werden nie mit openpyxl gespeichert.
  Die Aktionszeiträume-Excel wird nur gelesen; in die Kontaktliste schreibt das Tool nur eine vom
  Anwender freigegebene Kontaktänderung (`kontakte.automatisch_uebernehmen`, Standard aus).
* **Vorschläge statt automatischer Änderungen:** Terminänderungen, Kontaktänderungen, unsichere
  Rabatte, die Firmenwahl bei Region/Kette/Großkunde, Sondervarianten (37,5 cl, 1,5 l, Holzkiste) und
  Preisabweichungen werden angezeigt, aber erst nach Entscheidung des Anwenders übernommen.
  Lexware-Stammdaten ändert das Tool nie (nur Hinweis).
* **Lexware-Datenbank:** ODBC nur lesend (`SELECT`); Belege entstehen ausschließlich über die
  Oberfläche bzw. den openTRANS-Import.
* **Buchen nur nach Freigabe:** Blockierte Pakete werden nie gebucht; Dubletten werden unmittelbar
  vor dem Schreiben erneut geprüft; der Watcher bucht nie.
* **Dateien:** Ablage und Dossiers kopieren nur; das Tool löscht nichts, verschiebt nichts in der
  Auftragsablage oder aus "vorangefertigte Dossiers" und überschreibt keine vorhandenen Dateien
  (Namenskollision -> " (2)"). Einzige Verschiebung: Nach einer scharfen, erfolgreichen Buchung wird
  die Quelldatei aus dem Eingang nach `archiv_ordner` verschoben; liegt der Eingang in OneDrive,
  verschwindet sie dort aus "Eingang" (als "Kd" liegt sie in der Ablage, wenn diese aktiv ist).
* **Datenschutz:** Im Repository stehen nur synthetische Beispiele (Domain `example.org`,
  "Frau Beispiel", Referenz `DEMB00000`).

## Ablauf je Firma

**WIK FH & SEH, WIK LEH, WOE** (Kürzel WIK/WOE):

1. Einkauf "Bestellung schreiben" je Weingut: Lieferant = Weingut, Preise = EK, Feld "Customer" =
   Endkunde, Belegfelder (Spedition, Weingut, Shipment, Kunde); Kundenbestellnummer in die
   Nachbemerkung. Formular "WIK EN" zweimal als PDF: Cde (an das Weingut) und Speditionsauftrag.
   Enthält ein Auftrag Weine mehrerer Weingüter, entsteht je Weingut eine Bestellung.
2. Verkauf: Angebot (VK Preisgruppe 1, Positionsrabatte aus Aktionen) -> "Weiterführen" (bzw.
   "Auftrag wandeln") -> Auftragsbestätigung; Lieferdatum und AB-Text löschen. Formular "WIK AB_LS" als
   PDF = Auftragskopie (Kopie).
3. Lieferschein nur, wenn in der Freigabe gewählt (PDF Ls).
4. Revised Order: Cde und AB werden neu angelegt, nicht geändert (Kürzel "Neu" / "Neu Kopie").

**Weinstrasse Agentur** (Kürzel WS): keine Einkauf-Bestellung, kein Lieferschein. Verkauf: Angebot
(Lieferdatum löschen, Rabatte) -> Formular "WS_EN" zweimal als PDF (Cde ans Weingut und
Speditionsauftrag) -> "Weiterführen" -> AB mit Formular "WS_AB" = Auftragskopie.

Ist für eine Firma `opentrans.lizenziert: true`, wird die Verkauf-AB per openTRANS-Import angelegt
(ohne Angebot); den Lieferschein zieht dann `auftragsautomat lieferschein PAKET --ab NR` nach.

Nach jeder erfolgreichen Buchung (alle Firmen): Fixtermin, E-Mail-Entwürfe (Bestellung ans Weingut,
Auftragskopie an den Kunden, Speditionsauftrag an das Postfach "Neue Aufträge" der Spedition laut
Kontaktliste, ggf. Lieferschein), Dossier, Ablage.

## Benennung der Dateien

Alle Dateinamen kommen aus `src/auftragsautomat/benennung.py` (Titulierung). Trenner " – ",
Datum `TT.MM.JJJJ`; verbotene Zeichen werden ersetzt, die Länge ist begrenzt.

| Kürzel | Bedeutung |
|---|---|
| Kd | Kundenbestellung (Eingangsdokument) |
| Cde | Bestellung an das Weingut |
| Kopie | Auftragskopie an den Kunden |
| Oc | Bestätigung des Weinguts |
| Neu / Neu Kopie | Revised Order: neue Bestellung / neue Auftragskopie |
| Rekl | Reklamation |
| Ls | Lieferschein |
| Speditionsauftrag / Speditionsbestätigung | Auftrag an bzw. Bestätigung der Spedition |

Schemata mit Beispielen (synthetisch):

* WS: `Lieferant – Kunde – Kürzel – Datum`, z. B. `Beispielgut – Musterhandel – Cde – 26.10.2026.pdf`;
  für Kunden aus `benennung.kunden_mit_auftragsnummer` zusätzlich die Auftragsnummer:
  `Beispielgut – Beispielkette – 4500000000 – Kopie – 26.10.2026.pdf`.
* WIK/WOE: `Lieferant – WIK|WOE – Bestell-Nr – Kürzel – Datum`, z. B.
  `Beispielgut – WIK – B-0001 – Cde – 26.10.2026.pdf`.
* Spedition: `Spedition – Speditionsauftrag|Speditionsbestätigung|Lieferschein – Lieferant –
  Kunde|Bestell-Nr – Datum`, z. B. `Hillebrand – Speditionsauftrag – Beispielgut – B-0001 – 26.10.2026.pdf`.

Lieferant und Kunde sind Kurznamen (Lexware-Matchcode, sonst Name, sonst Text des Dokuments).
Vorschau: `auftragsautomat benennen PAKET --kuerzel Kopie`.

## Aktionen und Rabatte

* Quelle 1: Aktionszeiträume-Excel in OneDrive (`rabatte.aktionen_datei`, Blatt "Aktionen"; Spalten
  laut `rabatte.spalten`, Überschriften tolerant). Wird nur gelesen.
* Quelle 2: Rabattlisten-PDF, hochgeladen auf der Seite "Rabatte" oder per
  `auftragsautomat rabatte-einlesen PDF`. Nach Prüfung der erkannten Tabelle werden die Aktionen in
  `config/aktionen.yaml` übernommen.
* Anwendung beim Prüfen: Eine Aktion passt, wenn Firma, Kunde (Kundennummer, Name, Alias oder
  Kundengruppe), Lieferant bzw. Artikel und das Bezugsdatum (`rabatte.bezug`: Liefertag, sonst
  Auftragsdatum) passen. Sichere Treffer setzen den Positionsrabatt in % (Hinweis `RABATT_VORSCHLAG`,
  gelb); unscharfe Treffer bleiben Vorschlag (`RABATT_UNSICHER`); weicht der Rabatt im Dokument ab,
  meldet das Tool `RABATT_ABWEICHUNG` (rot). Rabatte über `max_rabatt_prozent` blockieren.
* Der VK bleibt immer Preisgruppe 1; der Rabatt ist ein Positionsrabatt im Angebot/in der AB, nie im
  Einkauf. EUR-Rabatte und Aktionspreise werden in Prozent umgerechnet. Manuelle Rabatte in der
  Freigabe werden nie überschrieben.
* Anzeigen: `auftragsautomat aktionen [--firma ID] [--datum TT.MM.JJJJ]`.

## Firmenwahl und Gedächtnis

Reihenfolge der Firmen-Ermittlung: Unterordner im Eingang -> Firmen-Gedächtnis -> Kunden-Alias ->
Firmenkatalog -> Lieferanten-Alias -> Stammdaten -> Empfänger im Dokument.

* Fachhandel -> Weinstrasse Agentur oder WIK FH & SEH, je nach Lieferant (Katalog, Abschnitt
  `lieferanten`).
* Eigenständige Lebensmittel-Einzelhändler -> WIK FH & SEH.
* Einkaufsregionen, große Ketten, Großkunden -> WIK LEH oder WOE: das Tool fragt in der Freigabe
  (`FIRMA_UNKLAR`, blockiert bis zur Wahl).

Die Antwort wird je Kunde + Lieferant in `config/firmen_gedaechtnis.yaml` gemerkt (Häkchen "für
diesen Kunden und Lieferanten merken", Standard an; CLI `firma-merken`). Beim nächsten Auftrag
übernimmt das Tool die gelernte Firma und kennzeichnet sie (`FIRMA_GELERNT`, "gelernt am … von …").

## Ablage und Eingang

* **Eingang:** der OneDrive-Ordner "Eingang". Kunden- und Verkäuferaufträge liegen dort;
  `eingang_ordner` zeigt auf seinen synchronisierten Pfad. Unterordner mit dem Namen aus
  `Firma.eingang_unterordner` geben die Firma vor. Der Upload in der Freigabe-Oberfläche speichert
  dorthin (atomar, ohne Überschreiben) und prüft sofort; gebucht wird erst nach Freigabe. Der
  Rabattlisten-Ordner `eingang/rabatte` wird nicht als Auftrag verarbeitet.
* **Ablage fertiger Dokumente:** nach Freigabe und erfolgreichem scharfem Lauf kopiert das Tool Kd,
  Cde, Kopie, Speditionsauftrag, Ls (bei Revision Neu / Neu Kopie) in OneDrive:
  * Weinstrasse Agentur -> "Aufträge mit Provision",
  * alle anderen Firmen (WIK FH & SEH, WIK LEH, WOE) -> "Aufträge ohne Provision".
* Voraussetzung: `ablage.aktiv: true` und `ablage.auftraege_ordner`. Im Dry Run, bei
  `ablage.aktiv: false` oder nach einer Dry-Run-Buchung wird nur protokolliert. Nichts wird verschoben
  oder gelöscht; bei Namensgleichheit wird " (2)" angehängt.
* Dossiers entstehen getrennt davon im Ordner "vorangefertigte Dossiers" (`m365.dossier_ordner`,
  im Dry Run `logs/dossiers`); das Einsortieren übernimmt der Mitarbeiter.

## Ordnerstruktur

```
config/                 Konfiguration (settings, firmen, firmenkatalog, aliase/, vorlagen/, signaturen …)
daten/<firma>/          Stammdaten-Exporte aus Lexware (nicht im Repository)
eingang/                Eingang (Standard; im Betrieb der synchronisierte OneDrive-Ordner "Eingang")
archiv/<firma>/<jahr>/  archivierte Quelldateien nach scharfer Buchung
logs/                   pakete/, protokoll.csv, berichte/, opentrans/, pdf/, entwuerfe/, dossiers/,
                        ablage/, ueberwachung/, fixtermine_vorschau.csv, nachfragen.json, screenshots/,
                        lexware_erkundung/
beispiele/              Platz für anonymisierte Beispielbelege (siehe beispiele/README.md)
docs/                   ARCHITEKTUR.md, AUSBAUSTUFE_2.md, WAS_ICH_BRAUCHE.md, EINRICHTUNG_WINDOWS.md, recherche/
scripts/                beispiel_pdfs_erzeugen.py, windows/einrichten.ps1 (Einrichtung am Windows-PC)
src/auftragsautomat/    Quellcode (extraktion, stammdaten, matching, rabatte, lexware, freigabe, m365,
                        termine, kontakte, kommunikation, tracking, ueberwachung, dossier, ablage,
                        benennung.py, eingang.py, protokoll.py, watcher.py, cli.py)
tests/                  pytest-Tests und synthetische Fixtures
```

## Tests

```bat
pip install -e ".[dev]"
pytest
ruff check src tests scripts
```

Die Tests laufen unter Linux und Windows ohne Lexware, ohne Netzwerk und ohne API-Schlüssel
(Claude-Client, pywinauto, Graph, COM und Playwright werden gemockt). Die Fixture `settings` in
`tests/conftest.py` leitet alle schreibenden Pfade in ein temporäres Verzeichnis um; Tests schreiben
nichts ins Repository. `tests/test_datenschutz.py` prüft, dass Doku und Konfiguration keine
persönlichen E-Mail-Adressen und keine echten Sendungsreferenzen enthalten.

## Weitere Dokumentation

* `docs/ARCHITEKTUR.md` – Module, öffentliche Schnittstellen, Geschäftsregeln 1–20.
* `docs/AUSBAUSTUFE_2.md` – realer Ablauf, Fixtermine, Kontakte, Entwürfe, Tracking, Dossiers,
  Lexware-Ablauf je Firma, Benennung, Rabatte, Firmenwahl, Eingang und Ablage.
* `docs/WAS_ICH_BRAUCHE.md` – offene Zuarbeiten (Exporte, Lexware-Details, Entscheidungen).
* `docs/recherche/` – Recherche zu openTRANS, ODBC, UI-Automation, Graph/Excel/Outlook, Tracking.
