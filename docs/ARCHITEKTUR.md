# Architektur des Auftrags-Automaten

Python-Paket `auftragsautomat` (Quellcode in `src/auftragsautomat/`). Läuft auf dem
Windows-PC mit Lexware premium 2026; Extraktion, Matching, Prüfung und Freigabe-UI
sind plattformunabhängig und werden unter Linux/Windows getestet. Nur die
Lexware-UI-Automation (pywinauto) und ODBC laufen ausschließlich unter Windows.

## Datenfluss

```
eingang/<firma>/*.pdf|*.eml|*.msg|*.txt
        │
        ▼
  extraktion.erkennung.extrahiere(datei, settings)      -> RohBestellung
        │   A) Text-PDF           -> extraktion.text_pdf   (pdfplumber)
        │   B) eigene E-Mail      -> extraktion.email_bestellung
        │   C) Scan/Handschrift   -> extraktion.vision     (Claude API, Konfidenzen)
        ▼
  matching.pipeline.erstelle_belegpaket(roh, settings)  -> Belegpaket
        │   Firma ermitteln -> Stammdaten laden (CSV/ODBC, nur lesend)
        │   Kunde / Lieferant / Artikel zuordnen (Aliase > Fuzzy)
        │   matching.pruefung.pruefe(...)  (Preise, Summen, 0,00 €, Dubletten, Belegfelder)
        ▼
  freigabe (Streamlit)  – Tabelle, rote Abweichungen, Buttons "Buchen" / "Korrigieren"
        ▼
  lexware.buchen.buche(paket, settings, dry_run)        -> BuchungsErgebnis
        │   Verkauf-AB:   lexware.opentrans (XML-Import)  oder  lexware.ui_automation
        │   Einkauf-Best: lexware.ui_automation ("Bestellung schreiben"-Assistent)
        │   Lieferschein: optional, "Auftrag weiterführen", nur nach Freigabe
        ▼
  protokoll.schreibe_protokoll(...)  -> logs/protokoll.csv + Zusammenfassung
```

Die Modelle in `models.py` sind der Vertrag zwischen den Modulen. **Modelle nicht
umbenennen oder Felder entfernen**; neue optionale Felder sind erlaubt.

## Konventionen

* Deutsch für Bezeichner, Docstrings, Log- und UI-Texte. Englisch nur für Bibliotheksbegriffe.
* Geld und Mengen als `Decimal`, niemals `float`. Volumen in cl (`Decimal("75")`).
* pydantic v2. Eingaben validieren, nie stillschweigend raten: fehlende Felder ergeben
  einen `Pruefhinweis`, keine Exception (Exceptions nur bei kaputten Dateien/Konfig).
* **Kein Schreiben in die Lexware-Datenbank.** ODBC ausschließlich `SELECT`.
* **Nichts drucken, nichts mailen.** Dialog "per E-Mail/Ausdruck" -> "Überspringen",
  Dialog "USt-IdNr. des Lieferanten prüfen?" -> "Nein".
* Dry Run ist Standard (`settings.dry_run = True`). Scharf nur per expliziter Option.
* Tests mit pytest in `tests/`, Fixtures aus `tests/conftest.py` (`settings`, `fixtures_ordner`).
  Jedes Modul bringt eigene Tests mit. `ruff check src tests` muss sauber sein.
* Keine Netzwerk- oder Lexware-Zugriffe in Tests: Claude-Client und pywinauto werden gemockt.
* Code, der Lexware-Details annimmt, die noch nicht am echten System verifiziert sind,
  bekommt einen Kommentar `# TODO-LEXWARE: ...` mit dem, was zu prüfen ist.

## Modulübersicht und öffentliche Schnittstellen

### `config.py` (vorhanden)
`lade_settings(stamm) -> Settings`; `Settings.firma(id) -> Firma`; `Firma.stammdaten.csv|odbc`,
`Firma.opentrans`, `Firma.einkauf_bestellung`, `Firma.belegfelder_standard`,
`Settings.matching` (Schwellen), `Settings.claude`, `Settings.lexware`, `Settings.alias_ordner`.

### `models.py` (vorhanden)
`Eingangstyp`, `RohPosition`, `RohBestellung`, `Artikel`, `Kunde`, `Lieferant`, `Beleg`,
`Pruefhinweis`/`Pruefcode`/`Schweregrad`, `PositionsZuordnung`, `KundeZuordnung`,
`LieferantZuordnung`, `Belegfelder`, `Belegpaket`, `Belegergebnis`, `BuchungsErgebnis`.

### `extraktion/`
* `basis.py` (vorhanden): Protocol `Extraktor` mit `kann_verarbeiten(datei)`, `extrahiere(datei)`.
* `erkennung.py`: `erkenne_eingangstyp(datei: Path) -> Eingangstyp` (PDF mit Textebene = A;
  PDF ohne Textebene = C; .eml/.msg/.txt = B); `extrahiere(datei: Path, settings: Settings,
  *, claude_client=None) -> RohBestellung` (Dispatcher). Firma aus `eingang/<unterordner>/`
  wird NICHT hier, sondern in `matching.firma` ermittelt.
* `text_pdf.py`: `ist_text_pdf(datei) -> bool`; `TextPdfExtraktor(settings)`. Liest mit
  pdfplumber Text + Tabellen. Erkennt Kunde, Lieferant, Belegnr., Bestellnr., Datum,
  Liefertag, Lieferadresse, Positionen [Bezeichnung, Jahrgang, Menge, Einheit, Preis,
  Volumen, Hinweis wie "11+1"], Summenvermerke. Deutsche Zahlen ("1.234,50"). Robust
  gegen unterschiedliche Layouts (Regex + Tabellen-Heuristik), unbekanntes -> `None`
  + `Pruefhinweis(FELD_FEHLT, WARNUNG)`.
* `vision.py`: `VisionExtraktor(settings, client: anthropic.Anthropic | None = None)`.
  Sendet das PDF (Dokument-Block, base64) bzw. Bilder an die Claude API mit
  `client.messages.parse(..., output_format=<pydantic-Modell>)`, Modell aus
  `settings.claude.modell` (`claude-opus-5-5`), `output_config={"effort": ...}`.
  Ergebnis: Positionen mit `konfidenz_menge`/`konfidenz_preis`, `durchgestrichen`,
  Summenvermerke ("180 R"). Konfidenz < Schwelle -> `Pruefhinweis(KONFIDENZ_NIEDRIG, WARNUNG)`,
  durchgestrichen -> `Pruefhinweis(DURCHGESTRICHEN, WARNUNG)` und Position bleibt mit
  `durchgestrichen=True` erhalten (Menge zählt nicht). `stop_reason == "refusal"` abfangen.
  Tests mit gemocktem Client (kein echter API-Aufruf).
* `email_bestellung.py`: `EmailExtraktor(settings)` für `.eml` (stdlib `email`), `.txt`
  und optional `.msg` (`extract_msg`, nur wenn installiert). Eigene Bestellung an das
  Weingut: Lieferant = Empfänger/Anrede, Endkunde aus Betreff/Text ("für Menrath"),
  Positionen aus dem Text; PDF-Anhänge werden extrahiert und an `text_pdf` weitergereicht.
* `zahlen.py`: `parse_dezimal("1.234,50") -> Decimal`, `parse_datum("15.09.2026") -> date`,
  `parse_jahrgang(text) -> int | None`.

### `stammdaten/`
* `basis.py` (vorhanden): Protocol `StammdatenQuelle`, Klasse `Stammdaten` (Indizes).
* `csv_quelle.py`: `CsvQuelle(firma_id, konfig: CsvQuellen)`. Spalten laut Fixtures
  (`tests/fixtures/stammdaten/*/*.csv`, Trennzeichen `;`, cp1252, deutsche Dezimalzahlen,
  `aktiv`/`holzkiste` als 0/1). Spaltenzuordnung über `konfig.spalten_*` (unser Name ->
  Exportspalte), unbekannte Spalten ignorieren, fehlende Pflichtspalten -> klare Fehlermeldung.
  `volumen_cl` leer -> aus der Bezeichnung ableiten (`matching.volumen.parse_volumen_cl`),
  `holzkiste` leer -> `matching.volumen.ist_holzkiste(bezeichnung)`.
* `odbc_quelle.py`: `OdbcQuelle(firma_id, konfig: OdbcQuelle)`. `pyodbc` nur bei Bedarf
  importieren (Linux-Tests ohne pyodbc müssen laufen). Ausschließlich SELECT; die SQL kommt
  aus der Konfiguration (`sql_artikel` usw.), mit dokumentierten Standardabfragen als
  Vorschlag (`# TODO-LEXWARE`). Verbindung mit `readonly=True` öffnen, `autocommit=False`,
  niemals `commit()`. `erkunde_schema(konfig) -> list[dict]` listet Tabellen/Spalten
  (für die Erkundung am echten System).
* `laden.py`: `lade_stammdaten(firma: Firma) -> Stammdaten` (wählt csv/odbc) mit
  einfachem Cache pro Prozess; `lade_alle(settings) -> dict[str, Stammdaten]`.

### `matching/`
* `aliase.py`: `AliasTabellen.lade(alias_ordner: Path) -> AliasTabellen` liest
  `kunden.yaml`, `lieferanten.yaml`, `artikel.yaml` (Format siehe `config/aliase/`).
  Methoden: `kunde(text) -> AliasTreffer | None`, `lieferant(text)`,
  `lieferant_fuer_marke(bezeichnung)`, `artikel(bezeichnung)`,
  `ist_zu_ueberspringen(bezeichnung) -> bool`. `AliasTreffer(nummer, firma_id, anzeigename)`.
  Vergleich normalisiert (Kleinschreibung, Umlaute, Satzzeichen, Mehrfach-Leerzeichen),
  Alias-Treffer auch als Teilstring ("Weinmarkt Kirchhellen GmbH & Co. KG").
* `volumen.py`: `parse_volumen_cl(text) -> Decimal | None` ("0,75 l", "75 cl", "750 ml",
  "0,375", "1,5 l", "Magnum", "halbe Flasche"); `ist_holzkiste(text) -> bool`
  ("Holzkiste", "HK", "OHK", "Kiste"); `normalisiere_bezeichnung(text) -> str` (entfernt
  Volumen, Jahrgang, Einheit, Satzzeichen).
* `partner.py`: `KundenMatcher(stammdaten, aliase, konfig).finde(text, adresse=None)
  -> KundeZuordnung`; `LieferantenMatcher(stammdaten, aliase, konfig).finde(text,
  positionen) -> LieferantZuordnung` (Lieferant aus Text, sonst aus Produktmarken der
  Positionen, sonst aus Warengruppe der zugeordneten Artikel). rapidfuzz, Score 0..100,
  Alias = 100. Mehrere gleich gute Kandidaten -> UNSICHER.
* `artikel.py`: `ArtikelMatcher(stammdaten, aliase, konfig).finde(roh: RohPosition,
  lieferant: Lieferant | None) -> PositionsZuordnung`. Regeln:
  1. `aliase.ist_zu_ueberspringen` oder (Preis 0,00 und kein Treffer) -> Status
     UEBERSPRUNGEN + `Pruefhinweis(NULLPREIS_OHNE_ARTIKEL, WARNUNG)`.
  2. Artikelnummer im Dokument (`roh.artikelnummer`) -> direkter Treffer.
  3. Alias -> Treffer (Score 100).
  4. Fuzzy über `normalisiere_bezeichnung` + Warengruppe (= Lieferant) + Jahrgang + Volumen.
     Standardvolumen 75 cl. **Sondervarianten (37,5 cl, 1,5 l, Holzkiste;
     `Artikel.ist_sondervariante`) werden NIE automatisch gewählt**, auch wenn das PDF
     sie nennt: sie erscheinen als Kandidat mit `manuell_erforderlich=True`, Status UNSICHER,
     `Pruefhinweis(VARIANTE_MANUELL, WARNUNG)`.
  5. Inaktive Artikel nur als Kandidat, nie automatisch.
  6. `menge = roh.menge`, `ek_preis = artikel.ek_preis`, `vk_preis = artikel.vk_preis_1`,
     `preis_dokument = roh.preis`.
* `firma.py`: `ermittle_firma(roh, datei, settings, aliase) -> tuple[str | None, str | None]`:
  1. Unterordner von `eingang/` (`Firma.eingang_unterordner`), 2. Kunden-Alias, 3. Lieferanten-
  Alias, 4. Suche in allen Stämmen (eindeutiger Kundentreffer), sonst `(None, None)` ->
  `Pruefhinweis(FIRMA_UNKLAR, FEHLER)`.
* `pruefung.py`: `pruefe(paket: Belegpaket, stammdaten: Stammdaten, settings) -> Belegpaket`:
  * Preisabweichung `|vk_preis - preis_dokument| > preis_toleranz_eur` -> `PREIS_ABWEICHUNG`
    (WARNUNG, rot in der UI; **nie stillschweigend übernehmen**, immer im Bericht).
  * Summenvermerk vs. Gesamtmenge -> `SUMME_ABWEICHUNG` (WARNUNG).
  * Menge fehlt/0 bei aktiver Position -> `MENGE_FEHLT` (FEHLER).
  * EK fehlt (bei Einkauf erforderlich) -> `EK_FEHLT` (FEHLER); VK fehlt -> `VK_FEHLT` (FEHLER).
  * Kein Kunde -> `KEIN_KUNDE` (FEHLER); kein Lieferant bei Einkauf -> `KEIN_LIEFERANT` (FEHLER).
  * Dubletten: `roh.belegnummer`/`roh.bestellnummer` in `stammdaten.belege`
    (Belegnummer oder Kundenbestellnummer, Partner gleich) -> `DUBLETTE` (FEHLER).
  * Belegfelder (Spedition, Weingut, Shipment, Kunde) leer -> `BELEGFELD_FEHLT` (FEHLER).
    Vorbelegung: `weingut` = Lieferantenname, `kunde` = Kundenname, `spedition` aus
    `Firma.belegfelder_standard`, `shipment` = Liefertag oder leer.
* `pipeline.py`: `erstelle_belegpaket(roh: RohBestellung, settings, *, firma_id: str | None
  = None, stammdaten: Stammdaten | None = None) -> Belegpaket` orchestriert alles oben.
  `einkauf_erforderlich = Firma.einkauf_bestellung`.

### `lexware/`
* `basis.py` (vorhanden): Protocol `LexwareSchreiber`.
* `opentrans.py`: `OpenTransSchreiber(settings, firma)`: `erzeuge_xml(paket) -> bytes`
  (openTRANS ORDER für die Lexware-Standard-Shopschnittstelle; Preise = `vk_preis` aus
  dem Stamm, NICHT PDF-Preis; Kundennummer, Kundenbestellnummer, Liefertag, Lieferadresse,
  Positionen mit Artikelnummer/Menge/Preis, übersprungene Positionen weglassen);
  `validiere_xml(xml) -> list[str]`; `schreibe_verkauf_ab(paket, *, dry_run=True)
  -> Belegergebnis`: Dry Run schreibt nach `settings.xml_ausgabe_ordner`, scharf nach
  `firma.opentrans.import_ordner`. Dateiname eindeutig (Datum, Kundennr., Bestellnr.).
  Dokumentiere im Modul-Docstring, welche Elemente verifiziert und welche Annahme sind.
* `ui_automation.py`: `UiSchreiber(settings, firma)` mit pywinauto (UIA-Backend), Import
  nur unter Windows (`sys.platform == "win32"`), sonst `NichtAufWindows`-Exception beim
  Instanziieren. Stabile Control-IDs/automation_id/Titel, keine Pixel-Klicks.
  Ablauf "Bestellung schreiben": Firma prüfen (Fenstertitel enthält
  `firma.lexware_firmenname`, sonst Abbruch), Assistent 3 Schritte (Lieferant ->
  Positionen -> Summe), nach jedem "Weiter" den aktiven Schritt prüfen (kein
  Doppelklick), Artikel per Artikelnummer, Menge: Feld markieren, Ziffern einzeln tippen,
  grüner Haken, Summe im Schritt "Summe" gegen `paket.summe_netto_ek` prüfen, Feld
  "Customer" = Endkunde, Belegfelder füllen, dann **Dry Run: Abbrechen statt Speichern**.
  Dialoge über `dialoge.py`. Jede Aktion loggen; bei Fehler Screenshot nach
  `settings.lexware.screenshots_ordner`. Das Modul muss ohne Windows importierbar und die
  Ablauflogik (Schrittfolge, Mengen-Tippen, Summenvergleich) mit einem Fake-Backend testbar sein.
* `dialoge.py`: Erkennung/Behandlung bekannter Dialoge: "per E-Mail/Ausdruck" -> "Überspringen";
  "USt-IdNr. des Lieferanten prüfen?" -> "Nein"; "Anwendung kann nur einmal gestartet werden".
* `instanz.py`: `lexware_laeuft() -> bool`, `stelle_sicher_dass_lexware_laeuft(settings)`
  (startet nicht doppelt), `aktive_firma(fenster) -> str`.
* `buchen.py`: `buche(paket: Belegpaket, settings, *, dry_run: bool = True,
  schreiber: LexwareSchreiber | None = None) -> BuchungsErgebnis`. Reihenfolge: 1. Einkauf
  (wenn `paket.einkauf_erforderlich`), 2. Verkauf-AB (openTRANS, wenn
  `firma.opentrans.lizenziert`, sonst UI), 3. Lieferschein (nur wenn
  `paket.lieferschein_erstellen`). Verweigert ohne `paket.freigegeben` oder bei
  `paket.blockiert`. Dubletten unmittelbar vor dem Schreiben erneut prüfen.
* `dry_run.py`: `DryRunSchreiber` (für Tests/CLI): simuliert beide Belege, liefert
  Summen, schreibt die openTRANS-XML in den Ausgabeordner.

### `protokoll.py`
`schreibe_protokoll(paket, ergebnis, log_ordner) -> Path` hängt eine Zeile an
`logs/protokoll.csv` an (Zeitpunkt, Firma, Quelldatei, Kunde, Lieferant, Beleg-Nr. Einkauf,
Beleg-Nr. Verkauf, Netto EK, Brutto EK, Netto VK, Brutto VK, Dry Run, Warnungen) und
schreibt eine Kurz-Zusammenfassung `logs/<datei>_<zeit>.txt`.
`zusammenfassung(paket, ergebnis) -> str` (mehrzeiliger Text für CLI/UI).
`archiviere(datei, settings, firma_id) -> Path` verschiebt die Quelldatei nach `archiv/<firma>/<jahr>/`.

### `freigabe/`
* `zustand.py`: `speichere_paket(paket, ordner) -> Path`, `lade_paket(pfad) -> Belegpaket`
  (JSON; `Belegpaket.model_validate_json`), `liste_pakete(ordner)`.
* `app.py`: Streamlit-App. Seitenleiste: Firma, Dry Run (Standard an), Eingangsdateien.
  Hauptbereich: Kopf (Kunde, Lieferant, Belegnr., Bestellnr., Liefertag, Firma, Belegfelder
  editierbar), Positionstabelle (Dokumenttext, Zuordnung mit Kandidaten-Auswahl, Menge
  editierbar, EK, VK, Preis laut PDF, Abweichung rot), Hinweise nach Schweregrad,
  Gesamtmenge vs. Summenvermerk, Summen. Buttons "Buchen" (gesperrt wenn `blockiert`),
  "Korrigieren" (Zuordnungen/Mengen bearbeiten und erneut prüfen), Checkbox "Lieferschein
  erstellen". Nach Buchen: Zusammenfassung + Protokollpfad. Start über
  `auftragsautomat freigabe` (ruft `streamlit run app.py`).

### `cli.py` (typer)
`auftragsautomat extrahieren <datei> [--json]`, `pruefen <datei> [--firma] [--json]`,
`buchen <paket.json> [--scharf]`, `freigabe`, `watch`, `stammdaten-pruefen --firma`,
`odbc-erkunden --firma`, `beispiel-pdfs [ordner]`. Ohne `--scharf` immer Dry Run.

### `watcher.py`
`starte_watch(settings, verarbeite: Callable[[Path], None])` mit watchdog; neue Dateien
in `eingang/` (und Unterordnern) erst verarbeiten, wenn sie fertig geschrieben sind.

## Testdaten
* `tests/fixtures/stammdaten/<firma>/*.csv`: synthetische Stämme (Spaltennamen = Modellfelder).
  wik_leh enthält bewusst Sondervarianten (1644 = 0,375 l, 958 = 1,5 l, 2004 = Holzkiste),
  einen inaktiven Artikel (3003), Jahrgangsvarianten (2001/2002), ähnliche Kundennamen
  (2100/2300) und vorhandene Belege für die Dublettenprüfung (Kundenbestellnr. "B-4711").
* `tests/fixtures/pdfs/`: von `scripts/beispiel_pdfs_erzeugen.py` erzeugte Beispiel-PDFs
  (Kundenbestellung "Weinmarkt Kirchhellen" mit Villa-Armellina-Positionen, "DAA Neptun"
  0,00 €, Hinweis "11+1", Bestellnr., Liefertag; eine zweite Bestellung "Weinhaus Menrath"
  mit Colosi/Bollina; ein Bild-PDF ohne Textebene als Scan-Beispiel).

## Geschäftsregeln (Checkliste, Quelle: Spezifikation)
1. Firma immer prüfen; ohne eindeutige Firma keine Buchung.
2. Einkauf-Bestellung: Lieferant = Weingut, Preise = EK, Feld "Customer" = Endkunde.
   Bei Weinstrasse Agentur keine Einkauf-Bestellung.
3. Verkauf-AB: Kunde aus Kundenliste, Preise = VK Preisgruppe 1, nie der PDF-Preis.
4. Nichts drucken, nichts mailen; USt-IdNr.-Dialog -> Nein.
5. Keine Dubletten (Belegnr./Kundenbestellnr. vorher prüfen).
6. Preisabweichungen nie stillschweigend übernehmen -> Bericht.
7. 37,5 cl, 1,5 l, Holzkiste nie automatisch.
8. 0,00-€-Positionen ohne Stammartikel überspringen und melden.
9. Belegfelder Spedition, Weingut, Shipment, Kunde immer füllen.
10. Lieferschein nur nach Vorlage und Freigabe.
11. Dry Run: alles bis kurz vor "Speichern", dann abbrechen.
12. Lexware nur einmal starten.
