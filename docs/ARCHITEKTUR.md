# Architektur des Auftrags-Automaten

Python-Paket `auftragsautomat` (Quellcode in `src/auftragsautomat/`). Läuft auf dem
Windows-PC mit Lexware premium 2026; Extraktion, Matching, Prüfung und Freigabe-UI
sind plattformunabhängig und werden unter Linux/Windows getestet. Nur die
Lexware-UI-Automation (pywinauto), ODBC und die COM-Backends für Outlook/Excel laufen
ausschließlich unter Windows.

Dieses Dokument nennt die öffentlichen Schnittstellen so, wie sie im Code stehen. Fachliche
Herleitung und Ablaufpläne: `docs/AUSBAUSTUFE_2.md`. Bei Abweichungen in Signaturen gilt dieses
Dokument (bzw. der Code), bei Geschäftsregeln gelten beide gemeinsam.

## Datenfluss

```
Eingang: OneDrive-Ordner "Eingang" (settings.eingang_ordner)/[<firma>/]*.pdf|*.eml|*.msg|*.txt|*.png|*.jpg
         + Upload "Auftrag hochladen" in der Freigabe-UI (eingang.speichere_upload)
        │
        ▼
  extraktion.erkennung.extrahiere(datei, settings)      -> RohBestellung
        │   A) Text-PDF           -> extraktion.text_pdf   (pdfplumber)
        │   B) eigene E-Mail      -> extraktion.email_bestellung
        │   C) Scan/Handschrift   -> extraktion.vision     (Claude API, Konfidenzen)
        ▼
  matching.pipeline.erstelle_belegpaket(roh, settings)  -> Belegpaket
        │   Firma ermitteln (Eingangsordner, Gedächtnis, Aliase, Katalog, Stammdaten, Empfänger)
        │   Stammdaten laden (CSV/ODBC, nur lesend)
        │   Kunde / Lieferant / Artikel zuordnen (Aliase > Fuzzy)
        │   Aktionen/Rabatte anwenden (rabatte.anwenden, Positionsrabatt im Verkauf)
        │   matching.pruefung.pruefe(...)  (Preise, Summen, 0,00 €, Dubletten, Belegfelder)
        ▼
  freigabe (Streamlit)  – Tabelle, rote Abweichungen, Firmenwahl, Rabatt %, "Freigeben & Buchen"
        ▼
  lexware.buchen.buche(paket, settings, dry_run=...)    -> BuchungsErgebnis
        │   Einkauf-Best.: je Weingut lexware.ui_automation ("Bestellung schreiben") -> PDF Cde + Speditionsauftrag
        │   Verkauf:       Angebot -> Weiterführen/Wandeln -> AB (UI)  oder  AB per openTRANS -> PDF Kopie
        │   Lieferschein:  optional, nur nach Freigabe und wenn die Firma ihn erlaubt -> PDF Ls
        ▼
  protokoll.schreibe_protokoll(...)  -> logs/protokoll.csv + Bericht; scharf: protokoll.archiviere
        ▼
  lexware.nachbereitung.nachbereiten(...)               -> Nachbereitung
            Fixtermin (Excel-Spalten D..K, im Dry Run Vorschau-CSV), E-Mail-Entwürfe (nie senden),
            Dossier ("vorangefertigte Dossiers"), Ablage "Aufträge mit/ohne Provision" (nur scharf)

täglich: ueberwachung.lauf.ueberwachen(settings, ...)  -> Ueberwachungsbericht + logs/ueberwachung/
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
  Dialog "USt-IdNr. des Lieferanten prüfen?" -> "Nein". E-Mails entstehen nur als Entwurf;
  kein Backend hat eine Sendefunktion.
* Dry Run ist Standard (`settings.dry_run = True`). Scharf nur per expliziter Option.
* Tests mit pytest in `tests/`, Fixtures aus `tests/conftest.py` (`settings`, `fixtures_ordner`,
  `pdf_ordner`). Die Fixture `settings` leitet alle schreibenden Pfade nach `tmp_path` um; Tests
  schreiben nichts ins Repo. Jedes Modul bringt eigene Tests mit. `ruff check src tests scripts`
  muss sauber sein.
* Keine Netzwerk- oder Lexware-Zugriffe in Tests: Claude-Client, pywinauto, Graph-Transport,
  COM-Dispatch und Playwright werden gemockt bzw. injiziert.
* Datenschutz: in Code, Fixtures, Vorlagen und Doku nur synthetische Beispiele (Domain
  `example.org`, "Frau Beispiel", Referenz `DEMB00000`); `tests/test_datenschutz.py` prüft das.
* Code, der Lexware-Details annimmt, die noch nicht am echten System verifiziert sind,
  bekommt einen Kommentar `# TODO-LEXWARE: ...` mit dem, was zu prüfen ist.

## Modulübersicht und öffentliche Schnittstellen (Ausbaustufe 1)

### `config.py`
`lade_settings(stamm=None) -> Settings` (Projektstamm: `AUFTRAGSAUTOMAT_HOME` oder Repo; liest
`config/settings.yaml` und `config/firmen.yaml`, relative Pfade werden absolut); `projekt_stamm()`;
`Settings.firma(id) -> Firma`; `Firma.stammdaten.csv|odbc`
(`OdbcQuelle` zusätzlich `kodierung`, `timeout_sekunden`), `Firma.opentrans` (`import_ordner`,
`lizenziert`, `waehrung`, `mwst_satz`, `firmen_guid`, `preistyp` net_list|gros_list, `steuergebiet`),
`Firma.einkauf_bestellung`, `Firma.belegfelder_standard`, `Firma.eingang_unterordner`,
`Settings.matching` (Schwellen), `Settings.claude`, `Settings.lexware` (`exe_pfad`,
`fenster_titel_regex`, Wartezeiten, `screenshots_ordner`, `firmenwechsel_erlaubt`, `backend` uia|win32,
`controls_datei`), `Settings.alias_ordner`. Konfiguration der Ausbaustufe 2 siehe unten.

### `models.py`
`Eingangstyp`, `RohPosition`, `RohBestellung` (u. a. `kundennummer_dokument`,
`empfaenger_firma_text`, `ladeadresse`, `ist_revision`, `revision_von`, `aktionshinweis`), `Artikel`,
`Kunde`, `Lieferant`, `Beleg`, `Pruefhinweis`/`Pruefcode`/`Schweregrad`, `PositionsZuordnung`,
`KundeZuordnung`, `LieferantZuordnung`, `Belegfelder`, `Belegpaket`, `Belegergebnis` (`partner` =
Lieferant der Einkauf-Bestellung), `BuchungsErgebnis` (`einkauf`, `einkauf_weitere` für weitere
Weingüter, `verkauf`, `lieferschein`, `protokoll`, `archiv`; `alle_einkaeufe()`).
`Pruefcode.MEHRERE_LIEFERANTEN`: Positionen mehrerer Weingüter in einer Bestellung.
Verträge der Ausbaustufe 2 siehe unten.

### `extraktion/`
* `basis.py`: Protocol `Extraktor` mit `kann_verarbeiten(datei)`, `extrahiere(datei)`.
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
  `settings.claude.modell` (`claude-opus-5-5`), `output_config={"effort": ...}`. Ohne API-Schlüssel
  (`ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN`) wirft `erzeuge_client` einen `VisionApiFehler`
  (die CLI meldet das als eine Zeile, kein Traceback).
  Ergebnis: Positionen mit `konfidenz_menge`/`konfidenz_preis`, `durchgestrichen`,
  Summenvermerke ("180 R"). Konfidenz < Schwelle -> `Pruefhinweis(KONFIDENZ_NIEDRIG, WARNUNG)`,
  durchgestrichen -> `Pruefhinweis(DURCHGESTRICHEN, WARNUNG)` und Position bleibt mit
  `durchgestrichen=True` erhalten (Menge zählt nicht). `stop_reason == "refusal"` abfangen.
  Tests mit gemocktem Client (kein echter API-Aufruf).
* `email_bestellung.py`: `EmailExtraktor(settings)` für `.eml` (stdlib `email`), `.txt`
  und optional `.msg` (`extract_msg`, nur wenn installiert). Eigene Bestellung an das
  Weingut: Lieferant = Empfänger/Anrede, Endkunde aus Betreff/Text ("für Musterhandel"),
  Positionen aus dem Text; PDF-Anhänge werden extrahiert und an `text_pdf` weitergereicht.
* `zahlen.py`: `parse_dezimal("1.234,50") -> Decimal`, `parse_datum("15.09.2026") -> date`,
  `parse_jahrgang(text) -> int | None`.

### `stammdaten/`
* `basis.py`: Protocol `StammdatenQuelle`, Klasse `Stammdaten` (Indizes).
* `csv_quelle.py`: `CsvStammdatenQuelle(firma_id, konfig: CsvQuellen)`. Spalten laut Fixtures
  (`tests/fixtures/stammdaten/*/*.csv`, Trennzeichen `;`, cp1252, deutsche Dezimalzahlen,
  `aktiv`/`holzkiste` als 0/1). Spaltenzuordnung über `konfig.spalten_*` (unser Name ->
  Exportspalte), unbekannte Spalten ignorieren, fehlende Pflichtspalten -> klare Fehlermeldung.
  `volumen_cl` leer -> aus der Bezeichnung ableiten (`matching.volumen.parse_volumen_cl`),
  `holzkiste` leer -> `matching.volumen.ist_holzkiste(bezeichnung)`. Echte Lexware-Exporte:
  Spaltenzuordnung in `config/firmen.yaml` (YAML-Anker `x_lexware_export`, "!" negiert Wahrheitswerte).
* `odbc_quelle.py`: `OdbcStammdatenQuelle(firma_id, konfig: config.OdbcQuelle)`. `pyodbc` nur bei Bedarf
  importieren (Linux-Tests ohne pyodbc müssen laufen). Ausschließlich SELECT; die SQL kommt
  aus der Konfiguration (`sql_artikel` usw.), mit dokumentierten Standardabfragen als
  Vorschlag (`# TODO-LEXWARE`). Verbindung mit `readonly=True` öffnen, `autocommit=False`,
  niemals `commit()`. `erkunde_schema(konfig, *, praefix=None) -> list[dict]` listet Tabellen/Spalten,
  `schreibe_schema_bericht(konfig, ziel, *, praefix=None) -> Path` (CLI `odbc-erkunden`).
* `laden.py`: `lade_stammdaten(firma: Firma, *, cache=True) -> Stammdaten` (wählt csv/odbc) mit
  einfachem Cache pro Prozess; `lade_alle(settings, *, cache=True) -> dict[str, Stammdaten]`;
  `stammdaten_bericht(stammdaten) -> str`. `StammdatenFehler` liegt in `csv_quelle` (re-exportiert
  aus `laden` und `odbc_quelle`).

### `matching/`
* `aliase.py`: `AliasTabellen.lade(alias_ordner: Path) -> AliasTabellen` liest
  `kunden.yaml`, `lieferanten.yaml`, `artikel.yaml` (Format siehe `config/aliase/`).
  Methoden: `kunde(text) -> AliasTreffer | None`, `lieferant(text)`,
  `lieferant_fuer_marke(bezeichnung)`, `artikel(bezeichnung)`,
  `ist_zu_ueberspringen(bezeichnung) -> bool`. `AliasTreffer(nummer, firma_id, anzeigename)`.
  Vergleich normalisiert (Kleinschreibung, Umlaute, Satzzeichen, Mehrfach-Leerzeichen),
  Alias-Treffer auch als Teilstring ("Musterhandel GmbH & Co. KG").
* `volumen.py`: `parse_volumen_cl(text) -> Decimal | None` ("0,75 l", "75 cl", "750 ml",
  "0,375", "1,5 l", "Magnum", "halbe Flasche"); `ist_holzkiste(text) -> bool`
  ("Holzkiste", "HK", "OHK", "Kiste"); `normalisiere_bezeichnung(text) -> str` (entfernt
  Volumen, Jahrgang, Einheit, Satzzeichen).
* `partner.py`: `KundenMatcher(stammdaten, aliase, konfig).finde(text, adresse=None, *,
  kundennummer=None) -> KundeZuordnung`; `LieferantenMatcher(stammdaten, aliase, konfig).finde(text,
  positionen=None) -> LieferantZuordnung` (Lieferant aus Text-Alias, sonst aus den zugeordneten
  Artikeln/Produktmarken der Positionen, sonst Fuzzy über die Lieferantennamen). rapidfuzz,
  Score 0..100, Alias = 100. Mehrere gleich gute Kandidaten -> UNSICHER. Zeigen die Positionen auf
  **mehrere Weingüter** (Kundenbestellung mit Weinen mehrerer Lieferanten), wird der
  Hauptlieferant SICHER gesetzt (im Text genannt, sonst die meisten Positionen, dann die meisten
  Flaschen) und alle Beteiligten bleiben Kandidaten; Prüfung und Buchung teilen den Einkauf auf.
* `artikel.py`: `ArtikelMatcher(stammdaten, aliase, konfig).finde(roh: RohPosition,
  lieferant: Lieferant | None = None) -> PositionsZuordnung`. Regeln:
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
* `firma.py`: `ermittle_firma(roh, settings, aliase, *, stammdaten_lader=None, datei=None,
  katalog=None, gedaechtnis=None) -> FirmaErgebnis` (`firma_id`, `quelle`, `hinweise`, `kandidaten`,
  `kundengruppe`; als Tupel entpackbar). Reihenfolge:
  1. Unterordner von `eingang/` (`Firma.eingang_unterordner`, Quelle `eingang_ordner`),
  1b. Firmen-Gedächtnis (`gedaechtnis`, `Pruefhinweis(FIRMA_GELERNT, INFO)`),
  2. Kunden-Alias (`alias_kunde`),
  2b. Firmenkatalog (`katalog`; mehrere Firmen möglich -> Ende der Suche mit `FIRMA_UNKLAR` und Kandidaten),
  3. Lieferanten-Alias (`alias_lieferant`), 4. Suche in allen Stämmen (eindeutiger Kundentreffer,
  `stammdaten`), 5. Empfänger laut Dokument (`empfaenger`). Sonst `firma_id=None` -> die Prüfung
  meldet `Pruefhinweis(FIRMA_UNKLAR, FEHLER)` mit `details["kandidaten"]`/`details["kundengruppe"]`.
  Hilfen: `firma_aus_ordner(quelldatei, settings)`, `firma_aus_empfaenger(text, settings, kandidaten=None)`,
  `hinweis_gelernt(regel)`. Quellen-Konstanten `QUELLE_*` (u. a. `QUELLE_GEDAECHTNIS = "gedaechtnis"`,
  `QUELLE_KATALOG = "katalog"`, `QUELLE_MANUELL = "manuell"`).
* `pruefung.py`: `pruefe(paket: Belegpaket, stammdaten: Stammdaten | None, settings) -> Belegpaket`:
  * Preisabweichung `|vk_preis - preis_dokument| > preis_toleranz_eur` -> `PREIS_ABWEICHUNG`
    (WARNUNG, rot in der UI; **nie stillschweigend übernehmen**, immer im Bericht).
  * Summenvermerk vs. Gesamtmenge -> `SUMME_ABWEICHUNG` (WARNUNG).
  * Menge fehlt/0 bei aktiver Position -> `MENGE_FEHLT` (FEHLER).
  * EK fehlt (bei Einkauf erforderlich) -> `EK_FEHLT` (FEHLER); VK fehlt -> `VK_FEHLT` (FEHLER).
  * Kein Kunde -> `KEIN_KUNDE` (FEHLER); kein Lieferant bei Einkauf -> `KEIN_LIEFERANT` (FEHLER);
    bei Kandidaten zusätzlich `KUNDE_UNSICHER`/`LIEFERANT_UNSICHER` (WARNUNG).
  * Positionen mehrerer Weingüter (`gruppiere_nach_lieferant(paket, stammdaten) ->
    list[LieferantenGruppe]`, Lieferant je Position über `Artikel.lieferantennummer` bzw.
    Warengruppe) -> `MEHRERE_LIEFERANTEN` (WARNUNG; je Weingut eine eigene Einkauf-Bestellung).
    Gehört keine Position zum zugeordneten Lieferanten -> `MEHRERE_LIEFERANTEN` (FEHLER).
  * Dubletten: `roh.belegnummer`/`roh.bestellnummer` in `stammdaten.belege`
    (Belegnummer oder Kundenbestellnummer, Partner gleich) -> `DUBLETTE` (FEHLER). Revised Order:
    Treffer zur ersetzten Bestellung werden zu INFO (`revision_dubletten_abstufen`, `revision_nummern`,
    `ist_revision`).
  * Belegfelder (Spedition, Weingut, Shipment, Kunde) leer -> `BELEGFELD_FEHLT` (FEHLER).
    Vorbelegung (`belegfelder_vorbelegen(paket, settings, stammdaten=None)`): `weingut` =
    Lieferantenname (bei mehreren Weingütern alle Namen, " / "-getrennt; bei Firmen ohne Einkauf
    das Weingut laut Dokument), `kunde` = Kundenname, `spedition` aus
    `Firma.belegfelder_standard`, `shipment` = Liefertag oder leer.
  * `pruefe_dubletten(paket, stammdaten, *, eintragen=False) -> list[Pruefhinweis]` auch für die
    erneute Prüfung unmittelbar vor dem Buchen; `zusammenfassung_hinweise(paket) -> str`.
* `pipeline.py`: `erstelle_belegpaket(roh: RohBestellung, settings, *, firma_id: str | None
  = None, stammdaten: Stammdaten | None = None, aliase=None, stammdaten_lader=None, aktionen=None)
  -> Belegpaket` orchestriert alles oben (inkl. Aktionen/Rabatte, `aktionen=None` = alle
  konfigurierten Quellen). `einkauf_erforderlich = Firma.einkauf_bestellung`.
  `pruefe_erneut(paket, settings, *, stammdaten=None, aliase=None, aktionen=None) -> Belegpaket` nach
  Korrekturen (manuelle Zuordnungen bleiben), `setze_firma(paket, firma_id, settings, *,
  stammdaten=None, aliase=None, merken=False, benutzer=None) -> Belegpaket`,
  `merke_firma_fuer(paket, settings, *, benutzer=None) -> Firmenregel | None`.

### `lexware/`
* `basis.py`: Protocol `LexwareSchreiber` mit `schreibe_einkauf_bestellung(paket, *, dry_run=True)`,
  `schreibe_verkauf_ab(paket, *, dry_run=True)`, `auftrag_weiterfuehren_lieferschein(paket,
  ab_belegnummer, *, dry_run=True)`, `schreibe_angebot(paket, *, dry_run=True)`, `angebot_zu_ab(angebot,
  paket, *, weg="angebot_weiterfuehren", dry_run=True)` (alle `-> Belegergebnis`) und
  `pdf_ausgeben(beleg, *, formular, dateiname, dry_run=True) -> Path | None`. Hilfen
  `unterstuetzt_angebot(schreiber)`, `unterstuetzt_pdf(schreiber)`, `nachbemerkung_einkauf(paket, firma)`,
  `pdf_zielpfad(settings, dateiname)`, `weg_beschreibung(weg)`.
* `opentrans.py`: `OpenTransSchreiber(settings, firma, *, preistyp=None, steuergebiet=None,
  firmen_guid=None, jetzt=None)` (Standardwerte aus `firma.opentrans`): `pruefe_paket(paket) ->
  list[str]`; `erzeuge_xml(paket) -> bytes`
  (openTRANS ORDER für die Lexware-Standard-Shopschnittstelle; Preise = `vk_preis` aus
  dem Stamm, NICHT PDF-Preis; Kundennummer, Kundenbestellnummer, Liefertag, Lieferadresse,
  Positionen mit Artikelnummer/Menge/Preis, übersprungene Positionen weglassen; Positionsrabatt ->
  `PRICE_AMOUNT` = rabattierter VK plus `REMARK`);
  `validiere_xml(xml) -> list[str]`; `schreibe_verkauf_ab(paket, *, dry_run=True)
  -> Belegergebnis`: Dry Run schreibt nach `settings.xml_ausgabe_ordner`, scharf nach
  `firma.opentrans.import_ordner`. Dateiname eindeutig (Datum, Kundennr., Bestellnr.).
  Der Modul-Docstring dokumentiert, welche Elemente verifiziert und welche Annahme sind.
* `ui_automation.py`: `UiSchreiber(settings, firma, backend=None, *, controls=None,
  firmenwechsel_erlaubt=None, heute=None)` mit pywinauto (UIA-Backend), Import
  nur unter Windows (`sys.platform == "win32"`), sonst `NichtAufWindows`-Exception beim
  Instanziieren. Stabile Control-IDs/automation_id/Titel (`config/lexware_controls.yaml`), keine
  Pixel-Klicks. Methoden laut `LexwareSchreiber` (Einkauf, Angebot, Angebot -> AB, AB direkt,
  Lieferschein, PDF-Ausgabe).
  Ablauf "Bestellung schreiben": Firma prüfen (Fenstertitel enthält
  `firma.lexware_firmenname`, sonst Abbruch), Assistent 3 Schritte (Lieferant ->
  Positionen -> Summe), nach jedem "Weiter" den aktiven Schritt prüfen (kein
  Doppelklick), Artikel per Artikelnummer, Menge: Feld markieren, Ziffern einzeln tippen,
  grüner Haken, Summe im Schritt "Summe" gegen `paket.summe_netto_ek` prüfen, Feld
  "Customer" = Endkunde, Belegfelder füllen, dann **Dry Run: Abbrechen statt Speichern**.
  Dialoge über `dialoge.py`. Jede Aktion loggen; bei Fehler Screenshot nach
  `settings.lexware.screenshots_ordner`. Das Modul ist ohne Windows importierbar und die
  Ablauflogik (Schrittfolge, Mengen-Tippen, Summenvergleich) mit `fake_backend.py` testbar.
* `dialoge.py`: Erkennung/Behandlung bekannter Dialoge: "per E-Mail/Ausdruck" -> "Überspringen";
  "USt-IdNr. des Lieferanten prüfen?" -> "Nein"; "Anwendung kann nur einmal gestartet werden".
* `instanz.py`: `lexware_laeuft(prozessname="Framework.exe", *, prozessliste=None, plattform=None) -> bool`,
  `stelle_sicher_dass_lexware_laeuft(settings, backend, *, screenshot=None) -> Fenster`
  (startet nicht doppelt), `aktive_firma(backend) -> str`.
* `buchen.py`: `buche(paket: Belegpaket, settings, *, dry_run: bool = True,
  schreiber: LexwareSchreiber | None = None, stammdaten: Stammdaten | None = None,
  jetzt: Callable[[], datetime] | None = None) -> BuchungsErgebnis` (`jetzt` = Uhr für Buchungszeitpunkt,
  Datum in Dateinamen und Archivjahr; Standard `datetime.now`). Vorbedingungen
  (`vorbedingungen(paket, settings) -> list[str]`): `paket.freigegeben`, nicht `paket.blockiert`,
  Firma, Kunde, Lieferant (bei Einkauf), aktive Position, Mandantenschutz. Reihenfolge:
  1. Einkauf (wenn `paket.einkauf_erforderlich`; bei Positionen mehrerer Weingüter je Weingut eine
  eigene Bestellung über `einkaufs_teilpakete(paket, stammdaten, warnungen=None)`, Ergebnisse in
  `einkauf` + `einkauf_weitere`), Nachbemerkung = Kundenbestellnummer laut Firma, PDF "Cde" und
  "Speditionsauftrag" (`Firma.formulare.einkauf_en`, `speditionsauftrag_kopien`);
  2. Verkauf laut `Firma.verkauf_weg`: Angebot (Positionsrabatte; bei `formulare.angebot_en` PDF "Cde" +
  "Speditionsauftrag") -> `angebot_zu_ab` (Weiterführen/Wandeln, Lieferdatum und AB-Text leeren) ->
  PDF "Kopie" (`formulare.verkauf_ab`); `direkt_ab` bzw. openTRANS (`firma.opentrans.lizenziert`) legt
  die AB direkt an;
  3. Lieferschein (nur wenn `paket.lieferschein_erstellen`, `Firma.lieferschein_erlaubt` und AB-Nummer
  bekannt) -> PDF "Ls".
  Scheitert Einkauf oder Angebot scharf, wird keine AB angelegt. Dubletten unmittelbar vor dem
  Schreiben erneut prüfen. Scharf nur, wenn `dry_run=False` übergeben, `settings.dry_run` aus und
  Lexware erreichbar (Windows); sonst `DryRunSchreiber` (`waehle_schreiber(...) -> Schreiberwahl`).
  Danach Protokoll, Archivierung (nur scharf und erfolgreich) und `nachbereitung.nachbereiten`
  (Fehler dort sind Warnungen, `ergebnis.erfolgreich` bleibt).
  `lieferschein_nachziehen(paket, ab_belegnummer, settings, *, dry_run=True, schreiber=None, jetzt=None)
  -> BuchungsErgebnis` legt nur den Lieferschein zu einer vorhandenen AB an (nach dem
  openTRANS-Import, wenn Lexware die AB-Nummer vergeben hat). `ist_revision(paket)`,
  `revision_nummern(paket)`.
* `dry_run.py`: `DryRunSchreiber(settings, firma, *, opentrans=None)` (für Tests/CLI): simuliert alle
  Belege inkl. Angebot und PDF-Ausgabe (geplanter Pfad, keine Datei), zeigt Formularnamen,
  Nachbemerkung, Rabatte (`rabattzeilen`) und Dateinamen, schreibt die openTRANS-XML in den
  Ausgabeordner.
* `nachbereitung.py`: siehe Ausbaustufe 2.

### `protokoll.py`
`schreibe_protokoll(paket, ergebnis, log_ordner, *, firma=None) -> Path` hängt eine Zeile an
`logs/protokoll.csv` an (Zeitpunkt, Firma, Dry Run, Quelldatei, Kunde, Lieferant, Bestellnummer,
Beleg-Nr. Einkauf (bei mehreren Weingütern " + "-getrennt), Beleg-Nr. Verkauf, Beleg-Nr.
Lieferschein, Netto/Brutto EK (Summe aller Einkauf-Bestellungen), Netto/Brutto VK, Positionen,
übersprungen, Warnungen, erfolgreich; UTF-8 mit BOM, Trennzeichen `;`) und schreibt einen
Bericht `logs/berichte/<datei>_<zeit>.txt`. `protokoll_zeile(paket, ergebnis) -> dict[str, str]`.
`zusammenfassung(paket, ergebnis, *, firma=None) -> str` (mehrzeiliger Text für CLI/UI).
`archiviere(datei, settings, firma_id, *, jahr=None, jetzt=None) -> Path` verschiebt die Quelldatei nach
`archiv/<firma>/<jahr>/` (Jahr der Buchung; `buche` übergibt es). Formatierer `formatiere_betrag`,
`formatiere_menge`, `formatiere_datum`.

### `freigabe/`
* `zustand.py`: `speichere_paket(paket, ordner, *, dateiname=None) -> Path`, `lade_paket(pfad)
  -> Belegpaket` (JSON; `Belegpaket.model_validate_json`; `PaketFehler`), `liste_pakete(ordner)`,
  `loesche_paket(pfad)`, `beschreibe_paket(pfad) -> PaketInfo`, `paket_dateiname(paket)`,
  `paket_ordner(settings)` (`logs/pakete`).
* `app.py`: Streamlit-App. Seitenleiste: Seitenwahl, Dry Run (Standard aus `settings.dry_run`,
  scharf nur mit zusätzlicher Bestätigung), Eingangsdateien, "Auftrag hochladen", gespeicherte Pakete,
  Firma. Hauptbereich: Kopf (Kunde, Lieferant, Belegnr., Bestellnr., Liefertag, Firma, Belegfelder
  editierbar), Positionstabelle (Dokumenttext, Zuordnung mit Kandidaten-Auswahl, Menge
  editierbar, EK, VK, Preis laut PDF, Abweichung rot, Rabatt %), Hinweise nach Schweregrad,
  Gesamtmenge vs. Summenvermerk, Summen. Buttons "Freigeben & Buchen" (gesperrt wenn `blockiert`),
  "Korrigieren / Neu prüfen", "Alias speichern", Checkbox "Lieferschein erstellen". Nach Buchen:
  Zusammenfassung, Protokollpfad, Dateinamen, Ablage und Nachbereitung. Start über
  `auftragsautomat freigabe` (ruft `streamlit run app.py`). Seiten der Ausbaustufe 2 in
  `seiten_stufe2.py` (siehe unten).

### `cli.py` (typer)
Alle Befehle siehe Abschnitt "CLI-Befehle" unten. Ohne `--scharf` immer Dry Run; `--home` bzw.
`AUFTRAGSAUTOMAT_HOME` wählt den Projektstamm, `--log-level` die Log-Stufe.

### `watcher.py`
`starte_watch(settings, verarbeite: Callable[[Path], None], *, einmalig=False, stabil_sekunden=2.0,
stop_ereignis=None, merkliste=None) -> int` mit watchdog; neue Dateien in `settings.eingang_ordner`
(OneDrive-Ordner "Eingang", rekursiv) erst verarbeiten, wenn sie fertig geschrieben sind
(`ist_fertig_geschrieben`, `warte_bis_fertig`). Bereits verarbeitete Dateien merkt
`Verarbeitungsliste` (`logs/verarbeitet.txt`); `ausgeschlossene_ordner(settings)` überspringt den
Rabattlisten-Ordner. `verarbeite_standard(settings, *, paket_ordner=None)` liefert den
Standard-Verarbeiter (extrahieren, prüfen, Paket speichern; nie buchen).

## Ausbaustufe 2 – Module

Design und Geschäftsregeln: `docs/AUSBAUSTUFE_2.md`. Gemeinsame Regeln aller Module: Es wird nie
gesendet und nie gedruckt; Excel-Dateien werden nie mit openpyxl gespeichert; über die
`DateiBackend`s wird nichts verschoben, gelöscht oder (ohne Schalter) überschrieben – die einzige
Verschiebung ist `protokoll.archiviere` (Quelldatei aus dem Eingang ins Archiv nach scharfer,
erfolgreicher Buchung); Änderungen an Terminen, Kontakten und Stammdaten sind Vorschläge, die erst
nach Freigabe (oder per ausdrücklichem Schalter) übernommen werden.

### Verträge in `models.py` und `config.py`
* `models.py`: `Kontaktgruppe`, `Kontakt`, `Fixtermin` (`firma_id`, `lieferant_kurz`, `kunde_kurz`,
  `liefertag`, `auftragsdatum`, `verladedatum`, `lieferant_bestaetigt`, `spedition_bestaetigt`,
  `abgeholt`, `bemerkung`, `bestellnummer`, `kundenbestellnummer`, `ab_nummer`, `spedition`,
  `tracking_referenz`, `zeile`, `quelle`, `revision_von`), `SendungStatus`, `Meilenstein`, `Sendung`,
  `EntwurfTyp`, `EmailEntwurf`, `Ereignistyp`, `MailEreignis`, `Terminaenderung`, `KontaktAenderung`,
  `DossierDatei`, `Dossier`, `UeberwachungsPunkt` (Ampel gruen|gelb|rot|grau), `Ueberwachungsbericht`;
  `Aktion`, `AktionTreffer`, `Rabattliste`, `Kundengruppe`, `Firmenregel`, `AblageErgebnis`,
  `DOKUMENTKUERZEL`, `KUERZEL_DATEITYP`, `Belegart.ANGEBOT`,
  `PositionsZuordnung.rabatt_prozent`/`rabatt_quelle`/`vk_preis_rabattiert`,
  `Belegpaket.ist_revision`/`revision_von`/`revision_bestaetigt`/`revision_wirksam`/`aktionen`/
  `firma_merken`/`kundengruppe`, `Belegergebnis.pdf`/`nachbemerkung`,
  `BuchungsErgebnis.angebot`/`pdfs`/`ablage`/`ist_revision`. Prüfcodes `FIRMA_GELERNT`, `REVISION`,
  `RABATT_VORSCHLAG`, `RABATT_ABWEICHUNG`, `RABATT_UNSICHER`, `ABLAGE`.
* `config.py`: `Firma.kuerzel`/`kurz`, `verkauf_weg` (angebot_weiterfuehren|angebot_wandeln|direkt_ab),
  `lieferdatum_in_ab_loeschen`, `ab_text_loeschen`, `bestellnummer_in_nachbemerkung`,
  `lieferschein_erlaubt`, `formulare` (`FormularKonfig`: `einkauf_en`, `angebot_en`, `verkauf_ab`,
  `lieferschein`, `speditionsauftrag_kopien`), `kundengruppen`, `ablage_ordner`;
  `LexwareKonfig.pdf_ausgabe` (keine|drucker|export), `pdf_drucker`, `pdf_ausgabe_ordner`;
  `DateiRefKonfig` (`web_url`, `drive_id`, `item_id`, `lokaler_pfad`, `blatt`); `M365Konfig`,
  `KommunikationKonfig`, `TrackingKonfig`, `UeberwachungKonfig`, `KontakteKonfig`, `DossierKonfig`,
  `BenennungKonfig`, `RabatteKonfig`, `FirmenwahlKonfig`, `AblageKonfig` (`provisionsordner(firma_id)`),
  `EingangKonfig`.

### `m365/` – Zugriffsschicht Excel, Mail, Dateien
Drei austauschbare Backends (`settings.m365.backend`: `graph` | `lokal` | `fake`, Standard `fake`).
* `basis.py`: Protokolle (`runtime_checkable`):
  * `ExcelBackend`: `blattnamen(datei) -> list[str]`, `lese_bereich(datei, blatt, adresse) ->
    list[list[Any]]`, `lese_benutzten_bereich(datei, blatt) -> tuple[str, list[list[Any]]]`,
    `schreibe_bereich(datei, blatt, adresse, werte, *, zahlenformat=None) -> None` (`None` = Zelle
    unverändert, `""` = leeren; Formeln werden nie geschrieben). Optional `sitzung(datei)`
    (Kontextmanager; `excel_sitzung(backend, datei)` nutzt ihn, falls vorhanden).
  * `MailBackend`: `erstelle_entwurf(entwurf: EmailEntwurf) -> str` (Speicherort), `suche_mails(*, seit,
    absender=None, betreff_enthaelt=None, ordner=None, maximal=50) -> list[MailKopf]`,
    `lese_mail(mail_id) -> Mail`, `speichere_anhang(mail_id, anhang_id, ziel: Path) -> Path`.
    Es gibt keine Sendefunktion.
  * `DateiBackend`: `existiert(ref, relativ="") -> bool`, `ordner_anlegen(ref, relativ) -> str`,
    `liste(ref, relativ="") -> list[DateiInfo]`, `hochladen(lokal, ref, relativ) -> str`,
    `herunterladen(ref, relativ, ziel) -> Path`, `schreibe_text(ref, relativ, text) -> str`.
    Es gibt kein Verschieben und kein Löschen.
  * Modelle `MailKopf`, `MailAnhang`, `Mail`, `DateiInfo`; Fehler `M365Fehler` mit `M365Konflikt`,
    `M365NichtGefunden`, `M365NichtAngemeldet`; Hilfen `parse_adresse`/`Zellbereich`,
    `spaltenbuchstabe`, `spaltenindex`, `datum_zu_serienzahl`, `serienzahl_zu_datum`,
    `datei_schluessel`, `html_zu_text`, `sicherer_dateiname`.
  * Abweichend von `docs/AUSBAUSTUFE_2.md` 2.1 gibt es **kein** `finde_zeilen`, `anhaenge_speichern`
    oder `verschieben`: Zeilen sucht `termine.register` im gelesenen Bereich, Anhänge speichert
    `speichere_anhang` einzeln, Dateien werden nie verschoben.
* `fabrik.py`: `Backends(excel, mail, datei, backend="fake", anmeldung=None)`;
  `erzeuge_backends(settings) -> Backends`; `pruefe_anmeldung(settings) -> str` (ohne Browser);
  `anmelden(settings, *, device_code=False, ausgabe=print) -> str`.
* `auth.py`: `GraphAnmeldung(konfig: M365Konfig, *, cache_pfad=None)` mit `token()`, `konto()`,
  `konten()`, `anmelden_interaktiv(*, login_hint=None, port=None)`, `anmelden_device_code(ausgabe=print)`,
  `abmelden()`; msal PublicClientApplication, Token-Cache per msal-extensions (Windows: DPAPI);
  `bereinige_scopes(bereiche)` entfernt `offline_access`/`openid`/`profile` (msal ergänzt sie selbst);
  `standard_cache_pfad()`. Ohne gültiges Token -> `M365NichtAngemeldet`, nie stillschweigend Browser.
* `graph.py`: `GraphClient(token_provider, *, basis=GRAPH_BASIS, sende=None, anfragen_pro_sekunde=4.0,
  max_versuche=5, schlaf=..., uhr=...)` (`get`/`post`/`patch`/`put`/`delete`, `anfrage`, `seiten`;
  sequenziell, `Retry-After`, Fehlerabbildung auf `M365*`), `GraphExcelBackend(client)` (Workbook-Session
  `persistChanges`, `sitzung`), `GraphMailBackend(client, postfach=None, *, kennzeichnen=True)` (Entwürfe
  per `POST …/messages`, Anhänge < 3 MB direkt, größer per Upload-Session; kein `/send`),
  `GraphDateiBackend(client)`; `requests_transport(session=None, *, zeitlimit=60.0)`.
* `adressierung.py`: `aufloesen(ref, client) -> DateiRefKonfig` (`web_url` -> `drive_id`/`item_id`;
  Weg A `/shares/u!…`, Weg B über die Site), `zerlege_web_url`, `kodiere_sharing_url`, `kodiere_pfad`,
  `aufloesen_ueber_shares`, `aufloesen_ueber_site`.
* `lokal.py` (nur Windows): `OutlookComMailBackend(*, dispatch=None, geschuetzte_felder_lesen=True)`
  (Entwurf per `CreateItem(0)`, `Display`, Text vor die Signatur, `Save`, `Close(0)`),
  `ExcelComBackend(*, dispatch=None, xlwings_nutzen=None, versuche=10, wartezeit=1.5, schlaf=...)`
  (Excel speichert selbst; `ReadOnly` -> `M365Konflikt`; `RPC_E_CALL_REJECTED` wird wiederholt;
  Formelzellen werden nie überschrieben), `LokalerDateiBackend(wurzel)`, `NichtAufWindows`; Hilfen
  `dasl_filter`, `mit_wiederholung`, `ist_rpc_abgelehnt`, `fuege_vor_signatur_ein`, `html_rumpf`.
* `fake.py`: `FakeExcelBackend(arbeitsmappen=None, *, aus_xlsx=None, jetzt=None)` (lädt `.xlsx` nur
  lesend über eine Kopie, `schreibprotokoll`, `vorschau_csv(pfad, *, anhaengen=False)`),
  `FakeMailBackend(entwuerfe_ordner, posteingang=None, *, anhaenge_ordner=None, jetzt=None)` (`.eml`
  mit `X-Unsent: 1`), `FakeDateiBackend(wurzel)`, `mail_aus_eml(pfad, *, mail_id=None, ordner="inbox")`,
  `lade_xlsx(pfad)`.

### `termine/` – Fixtermin-Register
* `register.py`: `FixterminRegister(excel: ExcelBackend, datei: DateiRefKonfig, *, kopfzeile=4,
  slots_je_tag=5, blatt=None, bundesland="NW")` mit `lade(*, erneut=False)`, `zeitraum()`,
  `zeilen_fuer_datum(datum)`, `datum_der_zeile(zeile)`, `freie_slots(datum)`, `eintrag(zeile)`,
  `eintraege(ab=None, bis=None)`, `offene(bis=None, *, ab=None)`, `finde(fixtermin)`,
  `trage_ein(fixtermin) -> Fixtermin`, `setze_bestaetigung(fixtermin, feld: "lieferant"|"spedition"|
  "abgeholt", wert=True)`, `setze_verladedatum(fixtermin, datum)`, `verschiebe(fixtermin,
  neues_datum, grund)`. Fehler `RegisterFehler`, `RegisterVoll`. `fixtermin_aus_paket(paket,
  ergebnis=None, kurznamen=None, *, heute=None) -> Fixtermin`; `bemerkung_teile(text)`,
  `bemerkung_zusammenfuegen(bisher, fixtermin)`.
  Regeln: Spaltenindizes aus der Kopfzeile; geschrieben werden **nur die Datenspalten D..K** einer
  Zeile, nie die Spalten A–C (Formeln) und nie das Blatt "Ressourcen für Auto DropDown";
  `trage_ein` belegt den ersten freien Slot des Tages, ein vorhandener Eintrag (Tag, Lieferant,
  Kunde, ggf. Bestellnummer) wird aktualisiert; kein freier Slot -> `RegisterVoll` statt Überschreiben.
* `kurznamen.py`: `Kurznamen(daten=None, *, vorhandene=(), schwelle=90.0)`,
  `Kurznamen.lade(pfad=None, *, settings=None, vorhandene=(), schwelle=90.0)`, `kunde(...)`,
  `lieferant(...)`, `ermittle_kunde(...)`/`ermittle_lieferant(...) -> KurznameErgebnis`,
  `fuer_paket(paket) -> (lieferant_kurz, kunde_kurz)`, `angleichen(name)`, `neue_namen`;
  `vorhandene_kurznamen(excel, datei, blatt="Ressourcen für Auto DropDown")` (nur lesen),
  `lade_kurznamen_yaml(pfad)`, `erster_wortbestandteil(name, ...)`. Reihenfolge:
  `config/aliase/kurznamen.yaml` -> Lexware-Matchcode -> erster Namensbestandteil; Abgleich mit der
  Dropdown-Ressourcenliste (Ähnlichkeit >= 90 %).
* `kalender.py`: `ist_werktag`, `ist_feiertag`, `feiertag`, `ist_wochenende`,
  `addiere_werktage(datum, n, bundesland="NW")`, `werktage_zwischen(a, b, ...)`,
  `werktage_im_zeitraum`, `naechster_werktag`, `vorheriger_werktag`, `kw`, `wochentag_kurz`
  (Feiertage über `holidays`, Bundesland aus `ueberwachung.bundesland`).

### `kontakte/` – Kontaktliste "Ansprechpartner Abwicklung"
* `liste.py`: `lade_kontaktliste(quelle: Path | str | tuple[ExcelBackend, DateiRefKonfig], *,
  settings=None, bekannte_speditionen=()) -> Kontaktliste`; `kontaktliste_aus_settings(settings,
  excel=None) -> Kontaktliste` (`m365.kontaktliste_datei`); `Kontaktliste` (`kontakte`, `lieferanten`,
  `speditionen`, `kunden`, `abwicklungsverlauf`, `blaetter`, `quelle`, `hinweise`; `je_gruppe`,
  `kontakt_fuer_email`, `finde_spedition`, `kontakte_der_spedition`, `bekannte_speditionen`);
  `KontakteFehler`; `lies_xlsx_lokal(pfad)` (openpyxl nur lesend).
* `zuordnung.py`: `Zuordner(kontaktliste, stammdaten: Mapping[str, Stammdaten] | None, settings, *,
  anrede_regeln=None)` mit `kontakt_fuer_lieferant(lieferant)` (Position "Orders" bevorzugt),
  `kontakte_fuer_lieferant`, `kontakte_fuer_kunde(kunde, lieferant=None)`,
  `spedition_fuer_kunde(kunde, lieferant=None) -> str` (laut Liste, sonst `kontakte.standard_spedition`),
  `postfach_spedition(spedition, zweck: "neue_auftraege"|"status"|"zoll"|"buchhaltung") -> Kontakt | None`
  (Postfächer nur aus der Liste, nie fest kodiert), `firma_fuer_lieferant`, `firma_fuer_kunde`,
  `nummer_fuer(kontakt, firma_id)`, `sprache_fuer`, `anrede_fuer`, `ergaenze_anrede`,
  `empfaenger_cc(settings=None)`; `person_aus_name(name, *, anrede=None) -> Person`.
* `aenderungen.py`: `erkenne_signatur(text) -> Signatur`, `vergleiche(kontakt, signatur, *,
  quelle=...) -> list[KontaktAenderung]`, `aenderungen_aus_mail(mail, kontaktliste) ->
  list[KontaktAenderung]`, `bericht(aenderungen) -> str`, `uebernehme(aenderung, excel, datei, *,
  settings, freigegeben=False, pruefen=True) -> bool`: schreibt nur mit
  `kontakte.automatisch_uebernehmen` (Standard aus) oder `freigegeben=True`, sonst bleibt es beim
  Vorschlag. Lexware-Stammdaten werden nie geändert (nur Hinweis).

### `kommunikation/` – E-Mail-Entwürfe und eingehende Mails
* `vorlagen.py`: `Vorlagen(ordner)` (`namen()`, `existiert(name)`, `rendere(name, *, signatur=None,
  **kontext) -> (html, text)`), `vorlagen_aus_settings(settings)`, `rendere(name, *, settings,
  signatur=None, **kontext)`, `lade_signaturen(pfad) -> SignaturKatalog`, `signatur_fuer(settings,
  firma_id, *, variante=None, katalog=None) -> SignaturBlock`, Filter `datum_de`, `betrag_de`,
  `menge_de`, `nl2br`; `VorlagenFehler`. Vorlagen `config/vorlagen/<name>.html.j2` + `.txt.j2`:
  `bestellung_weingut`, `auftragskopie_kunde`, `spedition_auftrag`, `lieferschein`,
  `nachfrage_lieferant`, `nachfrage_spedition`, `erinnerung_intern`; Signaturen `config/signaturen.yaml`.
* `anrede.py`: `lade_anrede_regeln(pfad) -> AnredeRegeln` (`config/anrede.yaml`), `baue_anrede(kontakt,
  sprache, *, regeln=None) -> str`, `grussformel(sprache, firma_id=None, *, regeln=None)`,
  `anrede_regeln_fuer_zuordner(regeln)`.
* `benennung.py` (Anhangsnamen, delegiert an `auftragsautomat.benennung`): `anhangsname(kuerzel, paket,
  settings, *, datum=None, endung=".pdf")`, `bestellung_pdf(...)`, `auftragskopie_pdf(...)`,
  `speditionsauftrag_pdf(...)`, `lieferschein_pdf(...)`, `enthaelt_verbotene_woerter(text, settings)`,
  `pruefe_auftragskopie_name`.
* `entwuerfe.py`: `Entwurfsfabrik(settings, kontakte: Zuordner | None, firma, *, vorlagen=None,
  anrede_regeln=None, signaturen=None, signatur_variante=None, signatur_einfuegen=None,
  bestellung_ohne_einkauf=False, heute=None)` mit `bestellung_weingut(paket, ergebnis, fixtermin,
  anhaenge)`, `auftragskopie_kunde(...)`, `spedition_auftrag(..., *, paletten=None) -> EmailEntwurf |
  None`, `lieferschein(..., *, an_kunde=False)`, `fuer_buchung(paket, ergebnis, fixtermin,
  anhaenge=None, *, weitere_weingueter=()) -> list[EmailEntwurf]`, `nachfrage_lieferant(fixtermin,
  kontakt)`, `nachfrage_spedition(fixtermin, sendung, kontakt)`, `erinnerung_intern(bericht)`,
  `pruefe_auftragskopie(entwurf) -> list[str]`, `speichere(entwuerfe, mail: MailBackend) ->
  list[EmailEntwurf]`. Funktionen `erstelle_entwuerfe(paket, ergebnis, settings, *, kontakte=None,
  fixtermin=None, anhaenge=None) -> list[EmailEntwurf]`, `pruefe_auftragskopie(entwurf, settings)`.
  Regeln: nie senden; eine Auftragskopie mit einem Wort aus
  `kommunikation.verbotene_woerter_auftragskopie` (u. a. "Auftragsbestätigung", "bestätigen",
  "verbindlich") in Betreff, Text oder Anhangsname wird nicht erzeugt (Hinweis stattdessen); fehlt ein
  Anhang oder Kontakt, entsteht der Entwurf mit Hinweis; Anhänge tragen immer den Namen laut
  Titulierung.
* `eingang.py`: `klassifiziere(mail, *, spedition_regeln=None, kontaktliste=None) -> MailEreignis |
  None`, `klassifiziere_alle(...) -> list[MailEreignis]`, `sammle_ereignisse(mail_backend, settings, *,
  seit, kontaktliste=None, maximal=200, ordner=None) -> list[MailEreignis]`,
  `lade_spedition_regeln(regeln)`. Ereignistypen (`models.Ereignistyp`): Bestätigung Spedition,
  Bestätigung Lieferant, Terminänderung, Abholung, Zustellung, Kontaktänderung, Sonstiges.

### `tracking/` – Sendungsverfolgung (Stufenmodell)
* `basis.py`: Protocol `TrackingQuelle` (`verfuegbar() -> bool`, `aktualisiere(sendung: Sendung) ->
  Sendung`); `HillebrandKonfig` aus `config/tracking_hillebrand.yaml` (`lade_hillebrand_konfig(pfad=None)`,
  `KonfigFehler`); `status_aus_meilensteinen(meilensteine, konfig=None, *, puenktlich=None, basis=...)`,
  `vereinige_meilensteine`, `normalisiere_referenz`, `ist_fortschritt`, `ist_bekannt`.
* `hillebrand_mail.py` (primär): `MailQuelle(ereignisse: Iterable[MailEreignis], konfig=None)`;
  `erkenne_ereignis(mail, konfig=None) -> MailEreignis | None`, `erkenne_ereignisse(mails, konfig=None)`,
  `erkenne_ereignis_aus_datei(pfad, konfig=None)`, `meilensteine_aus_ereignissen(ereignisse, konfig)`.
* `hillebrand_api.py` (empfohlen, sobald ein API-Zugang vorliegt): `HillebrandApi(konfig=None,
  tracking=None, *, sende=None, umgebung=None, ...)` (`token()`, `sendungen(referenz)`,
  `sendung(sendung_id)`, `ereignisse(sendung_id, seit=None)`, `zugangsdaten_vorhanden()`,
  `fehlende_variablen()`; Zugangsdaten nur aus den Umgebungsvariablen `tracking.api_*_env`),
  `ApiQuelle(api, konfig=None)`, `ApiFehler`.
* `hillebrand_public.py` (ohne Login, Playwright/Chromium): `PublicTrackingQuelle(konfig=None, *,
  lade_seite=None, tracking=None)`, `parse_public_tracking_html(html, konfig=None, *, selektoren=None,
  bezug=None) -> (SendungStatus, list[Meilenstein])`, `erkenne_seitentyp(...)`, `playwright_verfuegbar()`.
* `hillebrand_portal.py` (nur Rückfall, nicht in den Standardquellen): `PortalQuelle(konfig=None,
  settings=None, *, lade_seite=None)` (`profil()`, `verfuegbar()`, `aktualisiere(sendung)`,
  `aufzeichnen(referenz, ziel=None)`, `einrichten(*, warte=None) -> bool`), `profilverzeichnis(settings=None,
  ...)` (eigenes Chrome-Profil, sichtbar, keine Stealth-Techniken).
* `kette.py`: `TrackingKette(quellen)` (`quellen`, `namen`, `verfuegbar()`, `aktualisiere(sendung)`),
  `erzeuge_kette(settings, mail_ereignisse=None, *, konfig=None, sende=None, umgebung=None,
  lade_seite_public=None, lade_seite_portal=None) -> TrackingKette` (Reihenfolge `tracking.quellen`,
  Standard mail -> api -> public_tracking).

### `dossier/` – vorangefertigte Dossiers
* `erzeugung.py`: `DossierVerwaltung(datei_backend, wurzel: DateiRefKonfig, settings, *, jetzt=None,
  arbeitsordner=None)`, `DossierVerwaltung.aus_settings(settings, *, datei_backend=None, jetzt=None)`
  (Dry Run/fake: `dossier.lokaler_ordner`, sonst `m365.dossier_ordner`);
  `erstelle_oder_aktualisiere(paket, ergebnis, *, quelldatei=None, lexware_pdfs=None, entwuerfe=(),
  fixtermin=None, sendung=None, weitere_dateien=(), dossier=None) -> Dossier`, `ergaenze_mail(dossier,
  mail_eml, *, typ=None, checkliste=None) -> Dossier`, `finde_dossier(bestellnummer=None,
  kundenbestellnummer=None, referenz=None, *, firma_id=None)`, `lade(ordnername)`, `lade_paket(dossier)`,
  `liste()`. Dateien: `00_Deckblatt.pdf`, `01_Kundenbestellung_…`, `02_Bestellung_…`,
  `03_Auftragskopie_…`, `04_Speditionsauftrag_…`, `05_Lieferschein_…`, `06_Mails/`, `07_Fixtermin.txt`,
  `08_Bestaetigung_Weingut_…`/`08_Bestaetigung_Spedition_…`, `09_Reklamation_…`, `dossier.json`,
  `paket.json`, optional `Dossier_komplett.pdf`. Belege werden nie überschrieben (gleicher Name und
  Größe -> übersprungen, sonst `_2`, `_3`); Deckblatt, Index und Fixtermin-Text werden ersetzt.
  Hilfen `eml_aus_entwurf`, `fixtermin_text`, `mail_dateiname`.
* `deckblatt.py`: `erzeuge_deckblatt(dossier, paket, ziel, *, settings=None, jetzt=None) -> Path`
  (reportlab), `CHECKLISTE`/`CHECKLISTE_REIHENFOLGE` (Bestellung gesendet, Auftragskopie gesendet,
  Spedition beauftragt, Lieferant bestätigt, Spedition bestätigt, abgeholt, zugestellt).
* `ablage.py`: `dossier_name(paket, ergebnis=None, *, settings=None, datum=None) -> str`
  (`<Lieferant>-<Firma>-<Kunde>-<Bestellnr>-<TT.MM.JJJJ>`), `zielordner_vorschlag(paket, settings, *,
  datum=None) -> str` (derselbe Zielordner wie `ablage.zielordner`), `dateityp_aus_name(name)`,
  `stichtag(paket, ergebnis=None, *, datum=None)`. Das Tool verschiebt nichts aus
  "vorangefertigte Dossiers"; das Einsortieren macht der Mitarbeiter.

### `ueberwachung/` – täglicher Lauf
* `lauf.py`: `ueberwachen(settings, *, dry_run=None, heute=None, backends=None, firma_id=None,
  berichte_ordner=None, **weitere) -> tuple[Ueberwachungsbericht, Path]`;
  `Ueberwachungslauf(settings, backends, register, kontakte, entwurfsfabrik, tracking_kette_fabrik,
  dossiers, *, heute=None, regelwerk=None, drossel=None, jetzt=None)`,
  `Ueberwachungslauf.aus_settings(settings, *, backends=None, heute=None, firma_id=None, ...)`,
  `ausfuehren(dry_run=True) -> Ueberwachungsbericht`; `eml_aus_mail(mail, *, empfangen=None)`.
  Ablauf: Fixtermine (7 Tage zurück bis `tage_voraus`) lesen, Mails der letzten
  `mails_rueckblick_tage` auswerten, Ereignisse zuordnen; Häkchen nur bei eindeutiger Zuordnung und
  `bestaetigungen_automatisch_setzen`; Terminänderungen nur als `Terminaenderung`-Vorschlag
  (Verschieben nur mit `termine_automatisch_anpassen`, Standard aus); Kontaktänderungen als
  Vorschlag; Tracking aktualisieren; Nachfragen als Entwürfe; Erinnerung intern. `dry_run=True`
  schreibt weder Excel noch Dossiers noch Merkdatei.
* `regeln.py`: `bewerte(fixtermin, sendung, heute, *, konfig=None, regelwerk=None) -> Bewertung`
  (Ampel, Grund, Maßnahmen `nachfrage_lieferant`|`nachfrage_spedition`|`status_spedition`|`erinnerung`|
  `tracking`), `Regelwerk.lade(pfad)`/`aus_settings(settings)` (`config/ueberwachung.yaml`),
  `Drossel(pfad, *, erneut_nach_werktagen=2, bundesland="NW")` (`erlaubt`, `merke`, `vergiss`,
  `speichere`; Merkdatei `logs/nachfragen.json`), `hoechste_ampel`, `tracking_*`-Prädikate.
* `zuordnung.py`: `ordne_zu(ereignis, fixtermine, dossiers=(), *, konfidenz_min=0.5, text_suche=True)
  -> Zuordnung` (über Referenz, Bestellnummer, AB-Nummer, Kundenbestellnummer, Bemerkung, Dossier,
  zuletzt Kurznamen im Text = nur Vorschlag), `ordne_ereignis_zu(...) -> Fixtermin | None`,
  `dossier_fuer`, `dossier_fuer_fixtermin`.
* `bericht.py`: `schreibe_bericht(bericht, ordner, *, hinweise=(), protokoll=()) -> Path`
  (`ueberwachung_<JJJJ-MM-TT>.md` + `.json`), `bericht_markdown(...)`, `lade_bericht(pfad)`,
  `lade_letzten_bericht(ordner)`, `lade_letzten_bericht_mit_zusatz(ordner, *, datei=None)`,
  `AMPEL_REIHENFOLGE`, `AMPEL_TEXT`.

### `rabatte/` – Aktionen und Rabatte
* `aktionen_excel.py`: `lese_aktionen(excel: ExcelBackend, ref: DateiRefKonfig, konfig: RabatteKonfig, *,
  firmen=None) -> AktionenLadeErgebnis` (`aktionen`, `hinweise`, `blatt`, `kopfzeile`, `spalten`):
  Kopfzeile tolerant suchen (`rabatte.spalten` + Synonyme), Datumswerte als date/Serienzahl/Text,
  Prozent als "10 %", "0,1" oder 10; fehlerhafte Zeilen als `Pruefhinweis(FELD_FEHLT)` sammeln,
  fehlende Pflichtspalten als FEHLER-Hinweis. Die Excel wird nur gelesen. Gemeinsamer Zeilenparser
  `aktion_aus_werten(...)`.
* `rabattliste_pdf.py`: `lese_rabattliste(pdf: Path, settings, *, client=None, vision_erzwingen=False)
  -> Rabattliste` (pdfplumber-Tabellen/Textzeilen; ohne Textebene Claude Vision mit `output_format`;
  Konfidenz je Zeile; `RabattlisteFehler`), `speichere_aktionen(aktionen, datei: Path, *, ersetzen=False)
  -> Path`, `lade_gespeicherte_aktionen(datei) -> list[Aktion]` (`config/aktionen.yaml`).
* `anwenden.py`: `finde_aktionen(paket, aktionen, settings, *, heute=None, aliase=None) ->
  list[AktionTreffer]`, `wende_an(paket, treffer, settings) -> Belegpaket` (sicher +
  `automatisch_anwenden` -> `rabatt_prozent`/`rabatt_quelle = "aktion:<id>"` + `RABATT_VORSCHLAG`
  INFO; unscharf -> `RABATT_UNSICHER` WARNUNG ohne Eintrag; über `max_rabatt_prozent` -> FEHLER;
  vorhandene manuelle Rabatte werden nie überschrieben; idempotent), `rabatt_prozent_aus(aktion,
  vk_preis) -> Decimal | None` (EUR/Aktionspreis -> % auf VK Preisgruppe 1), `bezugsdatum(paket,
  settings, *, heute=None) -> (datum, quelle, aus_dokument)`, `alle_aktionen(settings, *, backends=None,
  cache=True) -> list[Aktion]` (Excel + Gedächtnis; ohne Quellen leer, nie Exception),
  `leere_cache()`. Der Einkauf bleibt unberührt.

### `matching/firmenwahl.py` – Firmenwahl nach Katalog mit Gedächtnis
* `Firmenkatalog(gruppen=(), lieferanten=None, *, pfad=None)`, `Firmenkatalog.lade(pfad, settings=None)`,
  `Firmenkatalog.aus_settings(settings)` (`config/firmenkatalog.yaml`; fehlt die Datei: leerer Katalog
  mit Warnung); `klassifiziere(kunde_text, adresse=None, kundennummer=None, *, lieferant_text=None,
  firma_kandidaten=None) -> Klassifikation(kundengruppe, firmen: list[str], entscheidung:
  "eindeutig"|"lieferant"|"fragen"|"unbekannt", grund)` (`Klassifikation.firma`);
  `firma_fuer_lieferant(lieferant_text) -> str | None`; `gruppe(kundengruppe) -> Gruppenregel | None`.
* `Firmengedaechtnis(regeln=(), *, pfad=None, tolerant=True)`, `Firmengedaechtnis.lade(pfad, *,
  tolerant=True)`, `.aus_settings(settings)`, `.finde(kunde_kurz, lieferant_kurz=None, *, streng=False)
  -> Firmenregel | None` (erst Kunde + Lieferant, dann Kunde allgemein),
  `.finde_fuer_texte(kunde_texte, lieferant_texte, *, streng=False)`, `.merke(regel)`,
  `.entferne(kunde_kurz, lieferant_kurz=None)`, `.speichere(pfad=None) -> Path` (YAML, atomar),
  `.regeln` (`config/firmen_gedaechtnis.yaml`).
* `merke_firma(paket, settings, *, benutzer=None, gedaechtnis=None, jetzt=None) -> Firmenregel | None`
  (aus der Freigabe, wenn `paket.firma_merken`, Firma gesetzt und `firmenwahl.merken`; Schreibfehler
  nur im Log).
* Einbettung in `matching/firma.py`: Gedächtnis nach dem Eingangsordner (`FIRMA_GELERNT` INFO,
  "gelernt am … von …"), Katalog nach dem Kunden-Alias; bei "fragen" ->
  `FirmaErgebnis(None, None, [FIRMA_UNKLAR WARNUNG], kandidaten, kundengruppe)`; `matching/pruefung.py`
  macht daraus den blockierenden FEHLER mit Kandidaten.

### `benennung.py` – Titulierung
* `dateiname(kuerzel, *, firma: Firma, lieferant, kunde, bestellnummer, auftragsnummer, datum=None,
  settings, endung=".pdf") -> str` (WS: `Lieferant – Kunde – [Auftragsnummer –] Kürzel – Datum`,
  Auftragsnummer nur für Kunden aus `benennung.kunden_mit_auftragsnummer`; WIK/WOE:
  `Lieferant – WIK|WOE – Bestell-Nr – Kürzel – Datum`).
* `speditionsname(art: "Speditionsauftrag"|"Speditionsbestätigung"|"Lieferschein", *, spedition,
  lieferant, kunde_oder_bestellnr, datum=None, settings, endung=".pdf") -> str`.
* `name_fuer_paket(kuerzel, paket, settings, *, datum=None, endung=".pdf") -> str` (Kurznamen aus
  Matchcode, sonst Name, sonst Dokumenttext; Revision -> "Neu"/"Neu Kopie"; `ValueError` bei
  unbekanntem Kürzel oder Paket ohne Firma).
* `bereinige(text, settings) -> str` (verbotene Zeichen, Mehrfach-Leerzeichen, Länge),
  `pruefe_auftragskopie_name(name, settings) -> list[str]` (verbotene Wörter),
  `entferne_verbotene_woerter(text, settings)`, `kurzname_kunde(paket)`, `kurzname_lieferant(paket)`,
  `spedition_fuer_paket(paket, settings)`, `ist_agentur(firma)`, `mit_auftragsnummer(kunde, settings)`,
  `datum_text(datum, settings)`, `normalisiere(text)`.

### `ablage/` – Ablage "Aufträge mit/ohne Provision"
* `lege_ab(paket, ergebnis, settings, *, datei_backend: DateiBackend, dry_run=True, datum=None) ->
  list[AblageErgebnis]`: Dateien aus `ergebnis.pdfs` (+ Eingangsdokument als "Kd", wenn
  `eingang.als_kundenbestellung_ablegen` und `ablage.kundenbestellung_ablegen`) nach `ablage.struktur`
  unter `ablage.auftraege_ordner`; `{provision}` -> `AblageKonfig.provisionsordner(firma_id)`:
  "Aufträge mit Provision" (Weinstrasse Agentur) oder "Aufträge ohne Provision" (alle anderen Firmen).
  Nur protokollieren (`AblageErgebnis.dry_run = True`) bei Dry Run, `ablage.aktiv = False` oder
  `ablage.nur_scharf` mit Dry-Run-Buchung; scharf nur für freigegebene Pakete mit erfolgreicher Buchung.
  Fehler je Datei in `AblageErgebnis.fehler`.
* `zielordner(paket, settings, *, datum=None) -> str` (Platzhalter `{provision}`, `{firma}`, `{jahr}`,
  `{monat}`, `{lieferant}`, `{kunde}`), `freier_name(backend, ref, ordner, name) -> str` (" (2)",
  " (3)" …; `vorhandene_ueberschreiben` Standard aus), `quellen(paket, ergebnis, settings, *, datum=None)
  -> list[AblageQuelle]`, `ablage_ref(settings) -> DateiRefKonfig` (ohne Konfiguration lokaler Rückfall
  `ablage.lokaler_ordner`), `platzhalter_werte(...)`, `basis_kuerzel(schluessel)`, `AblageFehler`.

### `eingang.py` – OneDrive-Ordner "Eingang" und Upload
* `speichere_upload(dateiname, inhalt: bytes, settings, *, firma_id=None, jetzt=None) -> Path`: prüft
  Dateityp (`eingang.upload_dateitypen`) und Größe (`eingang.upload_max_mb`; Verstoß ->
  `UploadAbgelehnt`), bereinigt den Namen, löst Kollisionen auf und schreibt atomar
  (`.upload_….tmp` + `os.replace`) nach `eingang_ordner(settings, firma_id)` =
  `settings.eingang_ordner/[<Firma.eingang_unterordner>/]`.
* `liste_eingang(settings) -> list[Path]` (rekursiv, nur erlaubte Typen, ohne temporäre Dateien und
  ohne den Rabattlisten-Ordner), `pruefe_upload(dateiname, groesse, settings) -> str`,
  `ist_erlaubter_typ`, `erlaubte_endungen`, `ist_temporaer`, `freier_pfad`, `schreibe_atomar`.
* `settings.eingang_ordner` zeigt auf den lokal synchronisierten OneDrive-Ordner "Eingang"
  (`eingang.ordner_name`); Watcher, Upload und Verarbeitung nutzen denselben Ordner.
  `eingang.onedrive_ordner` ist in der Konfiguration vorgesehen, wird vom Code aber noch nicht
  ausgewertet.

### `lexware/nachbereitung.py` – Schritte nach der Buchung
* `nachbereiten(paket, ergebnis, settings, *, backends=None, lexware_pdfs=None, speichern=True) ->
  Nachbereitung` (von `buche` nach erfolgreicher Buchung aufgerufen; jeder Schritt einzeln abgesichert).
  `Nachbereitung` (`fixtermin`, `weitere_fixtermine`, `dossier`, `entwuerfe`, `hinweise`, `ablage`,
  `vorschau_csv`, `dry_run`, `zeitpunkt`, `ergebnis`, `speicherort`; `kurzfassung`), gespeichert als
  `<paket>.nachbereitung.json` im Paketordner.
* Einzelschritte: `erzeuge_backends_fuer(settings, *, dry_run, hinweise=None) -> Backends` (Dry Run:
  Fake-Backends bzw. `VorschauExcelBackend` = echte Excel nur lesen, Schreibzugriffe in die
  Vorschau-CSV), `backends_fuer_ueberwachung(settings, *, scharf=False, nur_lesen=False)`,
  `lade_kurznamen(settings, excel=None, hinweise=None)`, `fixtermin_eintragen(paket, ergebnis, settings,
  *, backends, dry_run, kurznamen=None, hinweise=None, heute=None, weitere=None) -> (Fixtermin | None,
  Path | None)`, `lade_kontakte(settings, backends, *, stammdaten=None, hinweise=None)`,
  `entwuerfe_erstellen(paket, ergebnis, settings, *, backends, fixtermin, kontakte, lexware_pdfs=None,
  hinweise=None, speichern=True) -> list[EmailEntwurf]`, `dossier_anlegen(paket, ergebnis, settings, *,
  backends, dry_run, entwuerfe=(), fixtermin=None, lexware_pdfs=None, hinweise=None) -> Dossier | None`,
  `dossier_verwaltung(settings, *, dry_run, backends=None)`, `ablegen(paket, ergebnis, settings, *,
  backends, dry_run, hinweise=None) -> list[AblageErgebnis]`, `finde_lexware_pdfs(paket, settings, *,
  datum=None, ordner=None, kuerzel=("Cde", "Speditionsauftrag", "Kopie", "Ls"), nicht_vor=None) ->
  dict[str, Path]`, `weitere_weingueter(paket, ergebnis, settings, *, stammdaten=None, hinweise=None)`.
* Speichern/Laden: `lade_nachbereitung(paket | pfad, settings, *, ordner=None)`,
  `speichere_nachbereitung(nb, paket, settings, *, ordner=None)`, `nachbereitung_pfad(...)`,
  `zusammenfassung_nachbereitung(nb) -> str`, `ist_konfiguriert(ref) -> bool`. Hilfsklassen
  `VorschauExcelBackend(lesen, *, jetzt=None)`, `LeseSchreibMailBackend(lesen, schreiben)`.

### `freigabe/seiten_stufe2.py` – Seiten der Freigabe-Oberfläche
Logik ohne Oberfläche (testbar ohne Streamlit) plus `zeige_*`-Funktionen. Seiten `SEITEN =
("Freigabe", "Heute", "Dossiers", "Rabatte", "Einstellungen")`; `UiKontext(melde, setze_paket,
speichere, benutzer, dry_run)`.
* Upload: `upload_speichern_und_pruefen(dateiname, inhalt, settings, *, firma_id=None,
  paket_ordner=None) -> UploadErgebnis(datei, paket, paket_pfad)`, `zeige_upload(settings, kontext)`.
* Firmenwahl: `firmenwahl_kandidaten(paket) -> Firmenwahl(kandidaten, kundengruppe, text, blockiert) |
  None`, `firma_waehlen(paket, firma_id, settings, *, merken, benutzer) -> Belegpaket`,
  `gelernte_firma(paket)`, `zeige_firmenwahl(paket, settings, kontext)` (Häkchen "für diesen Kunden und
  Lieferanten merken", Standard an).
* Rabatte: `setze_rabatt_manuell(position, wert) -> bool` (Quelle "manuell"), `rabatt_farbe(position)`,
  `rabatt_hinweise(position)`, `rabatt_tooltip(position)`, `rabattliste_einlesen(dateiname, inhalt,
  settings, *, jetzt=None) -> (Path, Rabattliste)`, `aktionen_zu_tabelle`, `aktionen_aus_tabelle(zeilen,
  vorlage)`, `aktionen_uebernehmen(aktionen, settings) -> Path`, `aktionen_zeilen(...)`,
  `zeige_seite_rabatte(settings, kontext)`.
* Heute: `lade_bericht(settings)`, `ampel_zeilen(bericht)`, `pruefe_jetzt(settings, *, dry_run,
  nur_lesen=False)`, `terminaenderung_uebernehmen(aenderung, settings, *, dry_run, backends=None) ->
  Fixtermin`, `kontaktaenderung_uebernehmen(aenderung, settings, *, dry_run, backends=None) -> bool`,
  `zeige_seite_heute(settings, kontext)` (Übernahme nur auf Klick des Anwenders).
* Dossiers und Einstellungen: `dossier_zeilen(dossiers)`, `checklisten_fortschritt(dossier)`,
  `zeige_seite_dossiers(...)`, `einstellungen_zeilen(settings, *, anmeldestatus=None)`,
  `zeige_seite_einstellungen(...)`.
* Nach dem Buchen: `zeige_dateien_und_ablage(ergebnis)`, `zeige_nachbereitung(paket, settings)`.

### CLI-Befehle (`cli.py`)
Globale Optionen `--home <pfad>` (Projektstamm, sonst `AUFTRAGSAUTOMAT_HOME` bzw. Repo) und
`--log-level`. Ohne `--scharf` ist jeder Befehl ein Dry Run; mit `--scharf` bleibt es ein Dry Run,
solange `settings.dry_run` an ist.

| Befehl | Zweck |
|---|---|
| `version` | Versionsnummer |
| `extrahieren DATEI [--json]` | nur Extraktion, Rohbestellung anzeigen |
| `pruefen DATEI [--firma ID] [--json] [--paket-ordner P]` | Extraktion + Matching + Prüfung, Paket speichern |
| `buchen PAKET [--scharf] [--lieferschein] [--freigeben] [--ja]` | Paket buchen (scharf nur nach Rückfrage) |
| `lieferschein PAKET --ab NR [--scharf] [--ja]` | nur Lieferschein zu vorhandener AB |
| `freigabe [--port 8501] [--kein-browser]` | Freigabe-Oberfläche starten |
| `watch [--einmalig] [--paket-ordner P]` | Eingang beobachten, nie buchen |
| `stammdaten-pruefen [--firma ID]` | Stammdaten laden, Datenqualität |
| `odbc-erkunden --firma ID [--ziel DATEI] [--praefix X]` | ODBC-Schema lesen (nur lesend) |
| `beispiel-pdfs [ORDNER]` | Beispiel-PDFs erzeugen |
| `m365-anmelden [--device-code] [--nur-status]` | Microsoft-365-Anmeldung (Backend graph) |
| `m365-pruefen` | nur lesen: Fixterminüberwachung, Kontaktliste, Dossier-Ordner |
| `fixtermin-eintragen PAKET [--scharf]` | Fixtermin eintragen (sonst Vorschau-CSV) |
| `entwuerfe PAKET [--scharf]` | E-Mail-Entwürfe (nie senden; ohne `--scharf` als `.eml`) |
| `dossier PAKET [--scharf]` | Dossier anlegen/fortschreiben |
| `ablegen PAKET [--scharf]` | Ablage "Aufträge mit/ohne Provision" (sonst Protokoll) |
| `benennen PAKET --kuerzel K [--datum TT.MM.JJJJ]` | Dateiname laut Titulierung |
| `firma-merken PAKET --firma ID` | Firma setzen und je Kunde + Lieferant merken |
| `ueberwachen [--tage N] [--scharf] [--nur-lesen]` | täglicher Überwachungslauf |
| `heute` | letzten Überwachungsbericht anzeigen |
| `tracking REFERENZ [--kundenreferenz] [--spedition S]` | Sendungsstatus über die Tracking-Kette |
| `tracking-einrichten` | Portal-Login im eigenen Chrome-Profil einrichten |
| `kontakte-pruefen` | Kontaktliste laden und zusammenfassen |
| `rabatte-einlesen PDF [--uebernehmen]` | Rabattlisten-PDF einlesen, optional ins Gedächtnis |
| `aktionen [--firma ID] [--datum TT.MM.JJJJ] [--alle]` | gültige Aktionen anzeigen |

## Geschäftsregeln (Ergänzung aus dem dokumentierten Ablauf)
13. Verkauf läuft über Angebot → Weiterführen/Wandeln → AB (`Firma.verkauf_weg`); in der AB
    Lieferdatum und AB-Text löschen, wenn konfiguriert.
14. WIK/WOE: Kundenbestellnummer in die Nachbemerkung der Einkauf-Bestellung; Formulare laut
    `Firma.formulare`; WS: kein Lieferschein.
15. Revised Order: Cde und AB neu anlegen, nie ändern; Kürzel "Neu" / "Neu Kopie".
16. Rabatte nur als Positionsrabatt in % im Verkauf; VK bleibt Preisgruppe 1; Aktionsrabatt
    nur aus Aktionszeiträumen/Rabattliste, unsichere Treffer und Abweichungen zum Dokument
    sichtbar markieren; nie im Einkauf.
17. Firma bei Region/Kette/Großkunde nur nach Antwort des Anwenders; gelernte Regel je Kunde +
    Lieferant wiederverwenden und als solche kennzeichnen.
18. Dateinamen ausschließlich nach Titulierung (`benennung.py`); Auftragskopie nie
    "Auftragsbestätigung".
19. Ablage nur nach Freigabe und scharfem Lauf: Weinstrasse Agentur → "Aufträge mit Provision",
    alle anderen Firmen → "Aufträge ohne Provision"; nichts verschieben, nichts überschreiben.
20. Hochgeladene Dateien gelten als Eingang: extrahieren und prüfen ja, buchen nur nach Freigabe.

## Testdaten
* `tests/fixtures/stammdaten/<firma>/*.csv`: synthetische Stämme (Spaltennamen = Modellfelder);
  `tests/fixtures/stammdaten_lexware/`: Stämme im Format der echten Lexware-Exporte.
  wik_leh enthält bewusst Sondervarianten (1644 = 0,375 l, 958 = 1,5 l, 2004 = Holzkiste),
  einen inaktiven Artikel (3003), Jahrgangsvarianten (2001/2002), ähnliche Kundennamen
  (2100/2300) und vorhandene Belege für die Dublettenprüfung (Kundenbestellnr. "B-4711").
* `tests/fixtures/pdfs/`: von `scripts/beispiel_pdfs_erzeugen.py` erzeugte Beispiel-PDFs:
  Kundenbestellungen des Fachhandels (eine mit 0,00-€-Werbeposition, Hinweis "11+1", Bestellnr.
  und Liefertag; eine mit Weinen zweier Weingüter; ein Shopsystem-Layout), LEH-Bestellungen
  (SAP-Bestellung in Verpackungseinheiten, Lagerbestellung in Kolli), eine Revised Order, eine
  eigene Bestellung an ein Weingut, ein Bild-PDF ohne Textebene als Scan-Beispiel und eine
  Rabattliste.
* `tests/fixtures/excel/`: erzeugte Beispiel-Arbeitsmappen (Fixterminüberwachung,
  Ansprechpartner) mit den Erzeugerskripten; `tests/fixtures/emails/`: eigene Bestellungen als
  `.eml`/`.txt`; `tests/fixtures/mails/`: eingehende Mails (Bestätigungen, Terminänderungen,
  Kontaktänderung); `tests/fixtures/tracking/`: API-Antworten, Tracking-Seiten und Mails der
  Spedition; `tests/fixtures/config/`: Test-Konfiguration.

## Geschäftsregeln (Checkliste, Quelle: Spezifikation)
1. Firma immer prüfen; ohne eindeutige Firma keine Buchung.
2. Einkauf-Bestellung: Lieferant = Weingut, Preise = EK, Feld "Customer" = Endkunde.
   Enthält eine Kundenbestellung Weine mehrerer Weingüter, entsteht je Weingut eine eigene
   Einkauf-Bestellung (die Verkauf-AB bleibt ein Beleg). Bei Weinstrasse Agentur keine
   Einkauf-Bestellung.
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
