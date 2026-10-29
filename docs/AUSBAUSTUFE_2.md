# Ausbaustufe 2: Fixtermine, Kontakte, E-Mail-Entwürfe, Nachhaken, Tracking

Stand: 18.10.2026. Grundlage sind die echten Dateien und Belege aus SharePoint/OneDrive
und dem Postfach (nur gelesen, nichts verändert). Dieses Dokument beschreibt, wie der
tatsächliche Ablauf aussieht und wie der Auftrags-Automat ihn abbildet.

## 1. Der reale Ablauf (aus den Belegen abgeleitet)

```
Kundenbestellung (PDF/Mail/Scan)
   LEH:  SPAR (SAP-Bestellung, Bestellmenge in VE à 6), Rheinberg/Edeka ("Lagerbestellung",
         Menge in Kolli × Inhalt), Norma, Rewe, ProIdee ...  → Lieferdatum = Fixtermin
   Fachhandel: Weinmarkt Kirchhellen (Shopsystem, "ArtNr", "Hinweis: 11+1", Zeile "DAA Neptun"),
         Menrath (Scan), weitere Fachhändler ...
        │
        ▼  Ordner SharePoint "Buchhaltung/General/Aufträge/.KW <n> <Kürzel>/<Kundengruppe>/<Kunde>-<Lieferant>/"
        │  Dateinamen: <Lieferant>-<Firma>-<Kunde>-<Bestellnr>-<Kd|Cde|AB|LS|BestätigungWG|RechnungWG>-<Datum>.pdf
        ▼
Lexware (je Firma): 1. Bestellung Einkauf an das Weingut   → Formular englisch "Order"
                       Felder: Supplier No., Transport by (= Spedition), Shipment (= "directly" /
                       "end of week 44"), Order No., Customer (Endkunde), delivery date
                    2. Auftragsbestätigung Verkauf          → Formular "Auftragsbestätigung"
                       Felder: Bestellnr., KD.USt-IdNr., Supplier, Transport by, Shipment
                    3. Lieferschein (optional)              → "Lieferschein Nr."
                    Weinstrasse Agentur: nur Verkaufsbeleg, Formular als englischer "Order"
                    mit Customer = Kunde und Supplier = Weingut (keine Einkauf-Bestellung)
        │
        ▼
E-Mails (Outlook, Logistik-Postfach, CC Kollegin Auftragsmanagement):
   a) "new order" an das Weingut (Orders-Kontakt aus "Ansprechpartner Abwicklung.xlsx",
      Reiter Lieferanten), Text nur Signatur, Anhang Bestell-PDF "<Lieferant>-<Firma>-<Nr>-Cde-<Datum>.pdf"
   b) "Bestätigung" an den Kunden: "Hallo Frau X! Bitte finden Sie anbei die Auftragsbestätigung."
      Anhang "AB-<Lieferant>-<Kunde>_<Nr>-<Datum>.pdf"
   c) "Neuer Auftrag <Kunde>" an die Spedition: Hillebrand → Auftragspostfach laut Kontaktliste
      (ausschließlich dieses Postfach), Stark (für SPAR), Bouché (für Rheinberg). Anhänge: Speditionsauftrag-Formular (Lexware-Formularvariante
      der Bestellung, "Hillebrand-<Lieferant>-<Firma>-<Nr>-<Datum>.pdf") + Lieferschein.
   d) ggf. Lieferschein an Weingut/Kunde.
        │
        ▼
Fixterminüberwachung.xlsx (SharePoint Buchhaltung/General/Aufträge/):
   Blatt "Fixterminüberwachung", Kopfzeile in Zeile 4, ab Zeile 5 je Werktag 5 Zeilen (Slots):
   KW | Datum | Tag | Lieferant | Kunde | Auftragsdatum | Verladedatum |
   Bestätigung des Lieferanten | Bestätigung der Spedition | Abgeholt | Bemerkung
   Lieferant/Kunde = Kurznamen (= Lexware-Matchcodes: "Cide", "Bollina", "Rheinberg", "Spar" …);
   zweites Blatt "Ressourcen für Auto DropDown" füllt sich per Formel (nicht beschreiben).
        │
        ▼
Spedition bestätigt: Hillebrand mailt "Auftragsbestätigung; Your Ref: <unsere Referenz>;
   Our Ref: DEMB<Nummer>[jobDEM…]" → "Our Ref" ist die Hillebrand-Referenz (Tracking).
   Lieferzeitfenster laut Transporeon. Portal: myHillebrandGori (Login),
   Meilensteine je Sendung, Live-Karte, Benachrichtigungen.
```

## 2. Neue Module

| Modul | Zweck |
|---|---|
| `termine/` | Fixtermin-Register: Zeile in Fixterminüberwachung.xlsx anlegen/aktualisieren, Slots je Tag, Häkchen setzen, Überfälligkeits-Logik |
| `kontakte/` | Kontaktliste "Ansprechpartner Abwicklung.xlsx" lesen (Lieferanten/Speditionen/Kunden), Zuordnung zu Lexware-Nummern, Änderungsvorschläge aus E-Mail-Signaturen |
| `kommunikation/` | E-Mail-Entwürfe (nie senden) in Outlook: Bestellung ans Weingut, AB an Kunde, Speditionsauftrag, Lieferschein, Nachfragen/Erinnerungen; Vorlagen im Hausstil |
| `tracking/` | Hillebrand-Status: aus Bestätigungs-/Meilenstein-Mails (primär) und myHillebrandGori per Chrome/Playwright (sekundär) |
| `ueberwachung/` | Täglicher Lauf: offene Fixtermine prüfen, Bestätigungen aus Mails übernehmen, nachhaken, Erinnerungen, Vorschläge zur Terminanpassung |
| `dossier/` | Je Auftrag ein Dossier in OneDrive/SharePoint (Ordner "vorangefertigte Dossiers"): alle Belege mit einheitlichen Dateinamen, Deckblatt, Index; Mitarbeiter sortieren es danach in die Auftragsablage ein |
| `m365/` | Zugriff auf Excel/Outlook/Dateien über zwei austauschbare Backends: Microsoft Graph (empfohlen, erhält Formeln/Formatierung, funktioniert auch ohne geöffnetes Excel) oder lokal (Outlook/Excel per COM auf dem Windows-PC) |

### 2.1 `m365/` – Zugriffsschicht

* `basis.py`: Protokolle `ExcelBackend` (lese_bereich, schreibe_bereich, finde_zeilen),
  `MailBackend` (erstelle_entwurf, suche_mails, lese_mail, anhaenge_speichern) und
  `DateiBackend` (ordner_anlegen, liste, hochladen, herunterladen, verschieben) für OneDrive/SharePoint.
* `graph.py` (Standard, Details in `docs/recherche/graph_excel_outlook.md`): `msal`
  PublicClientApplication (get_accounts → acquire_token_silent → interaktiv/Device-Code),
  verschlüsselter Token-Cache per `msal-extensions` (DPAPI) unter `%APPDATA%`; eigene
  Entra-App-Registrierung (Single-Tenant, Plattform "Mobile and desktop applications",
  "Allow public client flows" = Yes; delegierte Rechte User.Read, Files.ReadWrite, Sites.Read.All,
  Mail.ReadWrite, Mail.Read, offline_access). Excel: Workbook-Session mit `persistChanges`,
  `PATCH …/worksheets/{name}/range(address='D7:K7')` mit `values` (null = Zelle unverändert, ""
  = löschen), Datums- und Boolesche Werte als echte Typen, Fehlercodes `accessConflict` /
  `invalidSessionAccessConflict` mit Wartezeit wiederholen, Requests sequenziell, `Retry-After`
  beachten. Mail: `POST /me/messages` (Entwurf), Anhänge < 3 MB als `fileAttachment`, größer
  per Upload-Session; Signatur-HTML selbst setzen (Graph fügt keine ein); Posteingang per
  `$filter`/`$search`, Anhänge per `…/attachments/{id}/$value`. Datei-IDs (Drive/Item) einmal
  aus der Web-URL auflösen (`/shares/u!…` oder `/sites/{host}:/sites/{name}` → `/drives`) und
  in `settings.yaml` ablegen.
* `lokal.py` (Fallback, nur klassisches Outlook/Excel auf Windows): Outlook-Entwurf per COM
  (`CreateItem(0)`, `Display()` lässt Outlook die Signatur einfügen, Text voranstellen,
  `Save()`, `Close(0)`); Excel per COM/xlwings nur, wenn die Datei lokal verfügbar und nicht
  schreibgeschützt ist (`Workbook.ReadOnly` prüfen, `RPC_E_CALL_REJECTED` wiederholen).
  **openpyxl darf die Fixterminüberwachung nie speichern** (Roundtrip zerstört Excel-365-
  Kontrollkästchen, dynamische Array-Formeln LET/SORT/UNIQUE/FILTER und x14-Erweiterungen);
  openpyxl nur lesend (`read_only=True`, `data_only=True`) auf einer Kopie.
* `fake.py`: In-Memory-Backend für Tests und Linux-Dry-Run; Entwürfe als `.eml` (mit
  `X-Unsent: 1` und gültiger `Message-ID`, damit Outlook sie als bearbeitbaren Entwurf öffnet)
  nach `logs/entwuerfe/`, Excel-Änderungen als Vorschau-Tabelle nach `logs/fixtermine_vorschau.csv`.
* Auswahl über `config/settings.yaml` → `m365.backend: graph | lokal | fake`.

### 2.2 `termine/` – Fixtermin-Register

* `modelle`: `Fixtermin(firma_id, lieferant_kurz, kunde_kurz, liefertag, auftragsdatum,
  verladedatum, lieferant_bestaetigt, spedition_bestaetigt, abgeholt, bemerkung, bestellnummer,
  ab_nummer, spedition, tracking_ref, zeile)` (ergänzt `models.py`).
* `register.py`: `FixterminRegister(backend, datei_konfig)`:
  `finde_slots(datum) -> list[Zeile]` (alle Zeilen mit diesem Datum in Spalte B),
  `trage_ein(fixtermin)` → erster leerer Slot (Spalte D leer) des Tages; keiner frei →
  `Pruefhinweis(FEHLER)` statt Überschreiben; vorhandener Eintrag gleicher Lieferant+Kunde+Datum
  → aktualisieren statt doppelt. Spalten A–C nie beschreiben (Formeln).
  `setze_bestaetigung(fixtermin, feld)`, `verschiebe(fixtermin, neues_datum, grund)` (alte Zeile
  leeren, neue anlegen, Bemerkung "verschoben von …"), `offene(ab, bis)`.
  Kurznamen aus Lexware-Matchcode (`Kunde.matchcode`, `Lieferant.matchcode`), überschreibbar
  in `config/aliase/kurznamen.yaml`.
* Ansteuerung: nach erfolgreichem Buchen (nicht im Dry Run) automatisch, mit Liefertag aus der
  Bestellung; im Dry Run nur Vorschau.

### 2.3 `kontakte/`

* `liste.py`: liest "Ansprechpartner Abwicklung.xlsx": Lieferanten (Blöcke je Land, Spalten
  Firma/Name/Position/E-Mail/Telefon), Speditionen (Tabellen `tbl_Spedition_*`:
  Name/Bereich/E-Mail/Telefon), Kunden (Blöcke je Kunde: Produkt/Name/Position/E-Mail/Telefon/
  Spedition/Abwicklungsverlauf). Ergebnis `Kontakt(gruppe, firma, name, position, email,
  telefon, produkt, spedition)`.
* `zuordnung.py`: `kontakt_fuer_lieferant(lieferant)` (Matchcode/Firma, Position "Orders"),
  `kontakte_fuer_kunde(kunde, produkt)` (Dispo-Kontakt, Standard-Spedition je Kunde:
  "Bouché (für Rheinberg)", "Stark (für SPAR)", sonst Hillebrand), `postfach_spedition(spedition,
  zweck)` ("Neue Aufträge" → Auftragspostfach, "Sendungsnachfragen" → Info-Postfach, beide aus der Kontaktliste).
* `aenderungen.py`: aus eingehenden Mails (Signaturblock) neue Telefon/E-Mail/Namen erkennen
  und als `KontaktAenderung` vorschlagen; Übernahme in die Excel erst nach Freigabe in der UI
  (Schalter `kontakte.automatisch_uebernehmen` standardmäßig aus). Lexware-Stammdaten werden
  nicht automatisch geändert (nur Hinweis im Bericht), weil dafür UI-Automation im Kundenstamm
  nötig wäre.

### 2.4 `kommunikation/`

* Vorlagen in `config/vorlagen/*.html.j2` und `*.txt.j2` (Jinja2, Autoescape für HTML, eigener
  nl2br-Filter, Signatur als Markup) im Hausstil, aus echten Mails abgeleitet.
  **Jede Mail enthält einen kurzen, freundlichen Text** (Anrede mit Namen aus der Kontaktliste,
  zwei bis vier Sätze, Grußformel, Signatur), nie nur die Signatur:
  * `bestellung_weingut` (englisch): Betreff "New order <Order No.> – <Kunde>"; Text: Dank,
    Hinweis auf die beigefügte Bestellung, Lieferziel/Zeitraum, Bitte um Eingangsbestätigung
    und Verladetermin. Anhang Bestell-PDF.
  * `auftragskopie_kunde` (deutsch): Betreff "Ihre Bestellung <Nr> – Auftragskopie zur
    Information"; Text: Dank für die Bestellung, "anbei erhalten Sie zur Information eine Kopie
    Ihres Auftrags, so wie wir ihn erfasst haben; bitte prüfen Sie die Angaben und melden Sie
    sich kurz, falls etwas nicht passt". **Die Wörter "Auftragsbestätigung", "bestätigen" und
    "verbindlich" kommen in Betreff, Text und Dateinamen nicht vor** (Haftung). Dateiname
    `Auftragskopie-<Lieferant>-<Kunde>_<Nr>-<Datum>.pdf`. Ein Prüfschritt im Code lehnt
    Entwürfe ab, die diese Wörter enthalten. Hinweis: Das Lexware-Formular trägt derzeit den
    Titel "Auftragsbestätigung"; damit das PDF zur Mail passt, muss der Formulartitel in Lexware
    (Formulardesigner, ggf. über Bome) auf "Auftragskopie" geändert werden.
  * `spedition_auftrag` (deutsch): Betreff "Neuer Auftrag <Kunde> – Abholung <Lieferant>";
    Text: Abholadresse, Lieferadresse, Fixtermin, Mengen/Paletten, Bitte um Bestätigung des
    Abholtermins. Anhänge Speditionsauftrag-PDF + Lieferschein.
  * `lieferschein` (deutsch/englisch je Empfänger): kurzer Begleittext zum Lieferschein.
  * `nachfrage_lieferant` (englisch): freundliche Nachfrage nach Eingangsbestätigung und
    Verladebereitschaft mit Bezug auf Order No. und Fixtermin.
  * `nachfrage_spedition` (deutsch): Statusanfrage mit "Our Ref" an das Info-Postfach.
  * `erinnerung_intern`: Zusammenfassung der fälligen Punkte an den Anwender selbst.
  * Signatur-Varianten je Firma/Absender in `config/signaturen.yaml`; Anrede-Regeln
    (Frau/Herr, Du/Sie, Englisch) je Kontakt in der Kontaktliste bzw. `config/anrede.yaml`.
* `entwuerfe.py`: `erstelle_entwuerfe(paket, ergebnis, kontakte, anhaenge) -> list[Entwurf]`;
  Anhänge: PDFs aus den SharePoint-Ordnern (Dateinamensmuster) oder aus `logs/pdf/`; fehlt ein
  PDF → Entwurf ohne Anhang + Hinweis. **Es wird nie gesendet**, nur Entwurf gespeichert.
* `eingang.py`: Mails lesen (Hillebrand "Auftragsbestätigung; Your Ref …; Our Ref: DEMB…",
  Lieferantenbestätigungen "order confirmation", Terminänderungen "Lieferdatum … auf … ")
  → `Ereignis(typ, referenz, datum, absender, text)`.

### 2.5 `tracking/` (Stufenmodell laut `docs/recherche/hillebrand_tracking.md`)

1. `hillebrand_mail.py` (sofort, primär): Referenz und Meilensteine aus Hillebrand-Mails
   (Betreff `Auftragsbestätigung; Your Ref: …; Our Ref: (DEMB\d+)[job…]`, Lieferzeitfenster
   "laut Transporeon … am <Datum>"). Meilenstein-Mails des Portals (Abholung/Zustellung) werden
   erkannt, sobald Beispiele vorliegen; Muster in `config/tracking_hillebrand.yaml`.
2. `hillebrand_api.py` (empfohlen, nach Freigabe eines API-Keys durch Hillebrand): offizielle
   REST-API `https://api.hillebrandgori.com/` (OpenAPI im Developer-Portal), Token per
   OAuth-Passwort-Grant mit API-Key/Secret (Umgebungsvariablen, nie in Dateien), `GET
   /v6/shipments?references=…`, `GET /v6/shipments/{id}/events`; Abbildung von
   `shipmentStatusEnum` (notReady/ready/collected/loaded/shipped/arrived/unloaded/delivered) und
   `onTimeStatus` auf die Ampel; Polling mit `modifiedSinceTimeStamp`, höchstens 100 Aufrufe/min.
   Webhooks entfallen (kein öffentlicher Endpunkt am PC).
3. `hillebrand_public.py` (ohne Login): `https://my.hillebrandgori.com/public-tracking/<Suchbegriff>`
   per Playwright, nur reduzierter Datensatz; Cloudflare-Challenge möglich → bei Fehlern "unbekannt".
4. `hillebrand_portal.py` (nur Rückfall): Playwright mit **eigenem** Chrome-Profilverzeichnis
   (`%LOCALAPPDATA%\Auftragsautomat\chrome-profil`, `launch_persistent_context(channel="chrome")`),
   einmaliger manueller Login im sichtbaren Browser (Okta, ggf. MFA). Das normale Chrome-Profil
   des Anwenders kann seit Chrome 136 nicht automatisiert werden. Sichtbar, niedrige Frequenz,
   Screenshots, Abbruch mit Status "unbekannt" bei Login-Seite; keine Stealth-Techniken. Die
   Nutzungsbedingungen von hillebrandgori.com untersagen automatisierten Zugriff auf die Website;
   deshalb nur mit Wissen des Hillebrand-Ansprechpartners und bevorzugt die API nutzen.
   Selektoren in `config/tracking_hillebrand.yaml`; `aufzeichnen()` speichert Screenshot + HTML.
* `modelle`: `Sendung(fixtermin, spedition, referenz, status, on_time, letzte_pruefung,
  meilensteine, quelle)`. Alle Stufen liefern dasselbe Modell; der Überwachungslauf nimmt die
  verlässlichste verfügbare Quelle.

### 2.6 `ueberwachung/`

Täglicher Lauf (`auftragsautomat ueberwachen`, per Windows-Aufgabenplanung):

1. Fixtermine der nächsten N Tage (Standard 21) laden.
2. Mails der letzten 7 Tage lesen → Bestätigungen (Lieferant/Spedition), Referenzen, Terminänderungen erkennen.
   * Eindeutig (Referenz passt zu Lieferant+Kunde+Datum): Häkchen setzen, Referenz in Bemerkung.
   * Terminänderung: nur Vorschlag (`Terminaenderung`), Übernahme nach Freigabe
     (Schalter `ueberwachung.termine_automatisch_anpassen`, Standard aus).
3. Regeln (konfigurierbar in `config/ueberwachung.yaml`):
   * ≥ 10 Werktage vor Fixtermin ohne Lieferantenbestätigung → Entwurf `nachfrage_lieferant`.
   * ≥ 5 Werktage vor Fixtermin ohne Speditionsbestätigung → Entwurf `nachfrage_spedition`.
   * Verladedatum erreicht, nicht "Abgeholt" → Statusanfrage + Tracking.
   * Fixtermin morgen → Erinnerung intern.
   * Jede Nachfrage höchstens alle 2 Werktage je Vorgang (Merkdatei `logs/nachfragen.json`).
4. Tracking für Sendungen mit Referenz aktualisieren.
5. Bericht `logs/ueberwachung_<datum>.md` + Streamlit-Seite "Heute" (Ampel je Fixtermin).

### 2.7 `dossier/` – vorangefertigte Dossiers

Ziel: Ein Mitarbeiter soll jeden Vorgang vollständig und einheitlich vorfinden und nur noch
einsortieren müssen.

* Ablageort: OneDrive/SharePoint-Ordner **"vorangefertigte Dossiers"** (Pfad in
  `config/settings.yaml` → `dossier.ordner`, lokal synchronisiert oder per Graph). Darin je
  Vorgang ein Unterordner `<Lieferant>-<Firma>-<Kunde>-<Bestellnr>-<Datum>/` nach dem
  bestehenden Dateinamensmuster.
* Inhalt des Dossiers (was vorhanden ist; fehlende Teile stehen im Deckblatt als "offen"):
  1. `00_Deckblatt.pdf`: Firma, Kunde, Lieferant, Bestellnr./Kundenbestellnr., AB-/Auftrags-Nr.,
     Lieferschein-Nr., Fixtermin, Spedition und Referenz, Positionen (Artikelnr., Bezeichnung,
     Menge, Einheit), Belegfelder, Checkliste (Bestellung ans Weingut gesendet, Auftragskopie
     an Kunden gesendet, Spedition beauftragt, Lieferant bestätigt, Spedition bestätigt,
     abgeholt, zugestellt), Prüfhinweise, vorgeschlagener Zielordner in der Auftragsablage
     (derselbe wie bei der Ablage: `ablage.struktur` mit "Aufträge mit/ohne Provision", Abschnitt 6).
  2. `01_Kundenbestellung_<…>-Kd.pdf` (Original aus dem Eingang; bei Mail: `.eml` + Anhänge).
  3. `02_Bestellung_<…>-Cde.pdf` (Lexware-Bestellung ans Weingut).
  4. `03_Auftragskopie_<…>.pdf` (Verkaufsbeleg).
  5. `04_Speditionsauftrag_<…>.pdf`, `05_Lieferschein_<…>.pdf`.
  6. `06_Mails/`: Kopien der erzeugten Entwürfe (`.eml`) und später eingehende Bestätigungen
     (Weingut, Spedition mit Referenz).
  7. `07_Fixtermin.txt` (Zeile aus der Fixterminüberwachung) und `dossier.json` (maschinen-
     lesbarer Index: Dateien, Quellen, Zeitstempel, Status) sowie optional
     `Dossier_komplett.pdf` (alle PDFs zusammengeführt, pypdf).
* Erzeugung: automatisch nach dem Buchen (im Dry Run nach `logs/dossiers/` statt OneDrive),
  Aktualisierung durch den Überwachungslauf, wenn neue Dokumente/Mails zum Vorgang
  auftauchen (Zuordnung über Bestellnummer, Kundenbestellnummer, Hillebrand-Referenz).
  Idempotent: vorhandene Dateien werden nicht überschrieben, neue ergänzt, Deckblatt neu
  erzeugt.
* Lexware-PDFs: solange Lexware die Belege nicht selbst als PDF ablegt, übernimmt das Dossier
  die PDFs aus dem SharePoint-Arbeitsordner (Dateinamensmuster) oder aus `logs/pdf/`; im
  scharfen Betrieb ist der PDF-Export beim Speichern in Lexware (Dialog "Drucken/E-Mail" →
  PDF-Datei) als Option vorgesehen (`# TODO-LEXWARE`).
* Einsortieren: Das Tool verschiebt nichts aus "vorangefertigte Dossiers" heraus; das macht
  der Mitarbeiter. Optional kann die Streamlit-Seite "Dossiers" den vorgeschlagenen Zielordner
  anzeigen und per Klick verschieben (Schalter `dossier.verschieben_erlauben`, Standard aus).

## 3. Autonomie-Grenzen (Vorschlag, bitte bestätigen)

| Aktion | Automatisch | Nur Vorschlag/Freigabe |
|---|---|---|
| Fixtermin nach Buchung in Excel eintragen | ja (scharfer Lauf) | – |
| E-Mail-Entwürfe erzeugen | ja (Entwürfe) | Senden immer manuell |
| Bestätigungs-Häkchen aus eindeutigen Mails setzen | ja | bei Unsicherheit Vorschlag |
| Liefertermin verschieben (Excel + Lexware) | – | ja |
| Kontaktdaten in Excel ändern | – (Schalter) | ja |
| Lexware-Stammdaten ändern | – | nur Hinweis |
| Tracking abfragen (Mail/Portal) | ja | – |
| Dossier in "vorangefertigte Dossiers" anlegen/aktualisieren | ja | Einsortieren in die Auftragsablage durch Mitarbeiter |
| Aktionsrabatt aus Aktionszeiträumen in den Auftrag übernehmen | sicherer Treffer: ja (sichtbar markiert) | unscharf / Abweichung zum Dokument |
| Rabattlisten-PDF einlesen | Extraktion ja | Übernahme in `config/aktionen.yaml` nur nach Prüfung |
| Firma wählen (Region/Kette/Großkunde: WIK LEH oder WOE) | gelernte Regel: ja | erste Entscheidung je Kunde + Lieferant |
| Fertige Dateien nach "Aufträge mit/ohne Provision" ablegen | ja (nur scharfer Lauf, nach Freigabe) | – |
| Hochgeladene Aufträge verarbeiten | Extraktion + Prüfung ja | Buchen nur nach Freigabe |

## 4. Erweiterungen an Ausbaustufe 1 (aus den echten Belegen)

* Extraktion: Bestellmengen in Verpackungseinheiten (SPAR "Bestellmenge in Bestell-Einheit" ×
  "Einheiten pro Bestell-Einheit", Rheinberg "Menge KOA × Inhalt") in Flaschen umrechnen und
  beides anzeigen; Lieferdatum aus LEH-Bestellungen als Fixtermin; Ladeadresse/Anlieferadresse;
  Kundenartikelnummer (SPAR Artikelnummer, Rheinberg Artikelnr.) als Alias-Quelle; Scan-PDFs
  ohne Textebene (Menrath) → Vision.
* Stammdaten: Spaltenzuordnung für die echten Lexware-Exporte (Artikel: `Einheit` = Volumen
  "75 cl", `Warengruppe` = Lieferantenname, `Preis 1` = VK Preisgruppe 1, `EK Preis Lf.1`,
  `Lieferantennummer 1`, `Benutzerdefiniert 2` = Jahrgang, `Gesperrt`; Kunden: `Matchcode`
  = Kurzname, `Firma` = Name, `Preisgruppe`, `Inaktiv`, `Bemerkung` (Dispo-Hinweise);
  Lieferanten: `Matchcode`, `Firma`, `Inaktiv`). Kodierungen: Artikel UTF-8 mit BOM,
  Lieferanten cp1252 mit Anführungszeichen, Kunden mit DOS-Umlauten in Kopfzeile → Spalten
  tolerant matchen (Nicht-ASCII ignorieren).
* Belegfelder: `spedition` = "Transport by", `weingut` = "Supplier", `shipment` = "Shipment",
  `kunde` = "Customer"; auf AB und Bestellung gleich. Standard `shipment` = "directly" bzw.
  "end of week <KW>" aus dem Liefertag.
* Eingangsordner: OneDrive-synchronisierter SharePoint-Ordner ".KW …" bzw.
  "Aufträge Zur Bearbeitung"; Archiv "Auftragsablage (mit/ohne Provision)".
* Dateinamen der erzeugten/erwarteten PDFs nach Hausmuster.

## 5. Der dokumentierte Lexware-Ablauf je Firma (Stand 21.10.2026)

Quelle: die Ablaufpläne "Lexware Auftragsablauf", "Ablauf Auftragserfassung WIK/WOE/WS" und
das Benennungsschema "Titulierung" aus dem gemeinsamen LexWare-Chat. Sie sind verbindlich
für `lexware/`, `benennung.py`, `dossier/` und `kommunikation/` und ergänzen die Regeln aus
`docs/ARCHITEKTUR.md`. Konfiguration je Firma: `Firma.verkauf_weg`, `lieferdatum_in_ab_loeschen`,
`ab_text_loeschen`, `bestellnummer_in_nachbemerkung`, `lieferschein_erlaubt`, `formulare`.

### 5.1 WIK FH & SEH, WIK LEH, WOE (Kürzel WIK / WOE)

1. **Einkauf → "Bestellung schreiben"**: Lieferant = Weingut, Positionen mit EK, Feld
   Customer = Endkunde, Belegfelder (Spedition, Weingut, Shipment, Kunde). Die
   **Bestellnummer des Kunden kommt in die Nachbemerkung** (`bestellnummer_in_nachbemerkung`).
   Formular **"WIK EN"** wird zweimal als PDF ausgegeben: Cde an das Weingut und
   Speditionsauftrag (`formulare.einkauf_en`, `speditionsauftrag_kopien = 2`).
2. **Verkauf → Angebot** anlegen (Kunde, Positionen mit VK Preisgruppe 1, Positionsrabatte aus
   Aktionen, Abschnitt 7) → **"Weiterführen"** zur Auftragsbestätigung
   (`verkauf_weg = angebot_weiterfuehren`); Alternative "Auftrag wandeln" (`angebot_wandeln`).
   In der AB **Lieferdatum löschen** (der Fixtermin steht in der Excel) und **AB-Text löschen**,
   Lieferadresse und Rabatte prüfen. Formular **"WIK AB_LS"** als PDF = Auftragskopie an den
   Kunden; der Lieferschein nur, wenn freigegeben (`lieferschein_erlaubt`).
3. **Revised Order** (der Kunde ändert eine Bestellung): Cde **und** AB werden **neu** angelegt,
   nicht geändert (`RohBestellung.ist_revision`, `revision_von`); Dateikürzel "Neu" und
   "Neu Kopie"; Dubletten-Prüfung meldet die alte Bestellung als Info statt als Fehler; das
   Dossier verweist auf die ersetzten Belege.
4. **Oc** (Auftragsbestätigung des Weinguts, Eingang per Mail) → Dossier-Datei "Oc" und
   Fixtermin-Häkchen "Lieferant bestätigt" (`kommunikation/eingang.py`).

### 5.2 Weinstrasse Agentur (Kürzel WS)

Keine Einkauf-Bestellung, kein Lieferschein (`einkauf_bestellung = false`,
`lieferschein_erlaubt = false`). Verkauf → **Angebot** (Lieferdatum löschen, Rabatte) →
Formular **"WS_EN"** zweimal als PDF (Cde ans Weingut + Speditionsauftrag) → "Weiterführen" →
AB mit Formular **"WS_AB"** = Auftragskopie an den Kunden.

### 5.3 Umsetzung in `lexware/`

* `buchen.py`: Reihenfolge je Firma: [Einkauf je Weingut] → Angebot → AB (→ Lieferschein).
  `BuchungsErgebnis.angebot`, `.pdfs` (Dokumentkürzel → Pfad), `.ist_revision`,
  `Belegergebnis.pdf`/`.nachbemerkung`.
* `ui_automation.py`: Schritte "Angebot anlegen", "Weiterführen" bzw. "Auftrag wandeln",
  "Lieferdatum leeren", "AB-Text leeren", "Nachbemerkung setzen", "Positionsrabatt %",
  "PDF ausgeben (Formular, Dateiname laut `benennung.py`)". PDF-Ausgabe nur gemäß
  `settings.lexware.pdf_ausgabe` (`drucker` = PDF-Drucker mit Dateinamen-Dialog, `export` =
  Lexware-Export, `keine`); nie Papier, nie E-Mail aus Lexware (Dialog → Überspringen).
  Im Dry Run endet jeder Beleg vor "Speichern"; die PDF-Ausgabe wird nur protokolliert.
* `opentrans.py`: Positionsrabatt → `PRICE_AMOUNT` = rabattierter VK (Preisgruppe 1 minus
  Rabatt) plus `REMARK` "Rabatt x % (Aktion …)"; openTRANS kennt keinen Positionsrabatt und
  kein Angebot (Webshop-Auftrag → AB manuell übernehmen) → Hinweis im Protokoll.
* `dry_run.py`: zeigt den geplanten Ablauf inkl. Formularnamen, Nachbemerkung, Rabatten und
  Dateinamen.

## 6. Benennungsschema (Titulierung) und Ablage "Aufträge mit/ohne Provision"

Dokumentkürzel (`models.DOKUMENTKUERZEL`): **Kd** Kundenbestellung, **Cde** Bestellung ans
Weingut, **Kopie** Auftragskopie an den Kunden (nie "Auftragsbestätigung"), **Oc** Bestätigung
des Weinguts, **Neu** / **Neu Kopie** Revised Order, **Rekl** Reklamation, **Ls** Lieferschein;
dazu **Speditionsauftrag** und **Speditionsbestätigung**.

* WS: `Lieferant – Kunde – Kürzel – Datum`; Kunden aus `benennung.kunden_mit_auftragsnummer`
  (z. B. Spar): `Lieferant – Kunde – Auftragsnummer – Kürzel – Datum`.
* WIK / WOE: `Lieferant – WIK|WOE – Bestell-Nr – Kürzel – Datum`.
* Spedition: `Spedition – Speditionsauftrag|Speditionsbestätigung|Lieferschein – Lieferant –
  Kunde|Bestell-Nr – Datum`.

Trenner `" – "` (Gedankenstrich), Datum `TT.MM.JJJJ`, in Dateinamen verbotene Zeichen werden
ersetzt (`benennung.ersatzzeichen`), Länge begrenzt. `benennung.py` liefert die Namen für
Lexware-PDFs, Mail-Anhänge, Dossier-Dateien und die Ablage; ein Name darf für die
Auftragskopie keines der `verbotene_woerter_auftragskopie` enthalten.

**Ablage** (`ablage/`), Korrektur vom 24.10.2026: Nach Freigabe und erfolgreichem **scharfem**
Lauf kopiert das Tool alle fertigen Dateien (Kd = Eingangsdokument, Cde, Kopie, Speditionsauftrag,
Ls, bei Revision Neu / Neu Kopie) in OneDrive in den Ordner **"Aufträge mit Provision"**, wenn der
Auftrag in der **Weinstrasse Agentur** geschrieben wurde, sonst (WIK FH & SEH, WIK LEH, WOE) in
**"Aufträge ohne Provision"** (`ablage.ordner_mit_provision`, `ordner_ohne_provision`,
`firmen_mit_provision`, `AblageKonfig.provisionsordner(firma_id)`). `ablage.auftraege_ordner` ist
der OneDrive-Ordner, der beide enthält; `ablage.struktur` (Standard `"{provision}"`) kann um
`{firma}`, `{jahr}`, `{monat}`, `{lieferant}`, `{kunde}` erweitert werden. Die frühere Vorgabe
Das Tool verschiebt und löscht nichts. Dry Run: nur Protokoll
(`AblageErgebnis.dry_run = True`). Namenskollision → `" (2)"` anhängen
(`vorhandene_ueberschreiben = false`).

## 7. Aktionen und Rabatte

* **Quelle 1 – Aktionszeiträume-Excel** in OneDrive (`rabatte.aktionen_datei`, Blatt
  "Aktionen"): Spalten laut `rabatte.spalten` (Kunde, Lieferant, Artikel, Artikelnummer, Von,
  Bis, Rabatt %, Rabatt EUR, Aktionspreis, Aktion, Bemerkung, Firma; Überschriften tolerant,
  Groß-/Kleinschreibung egal, leere Zeilen übersprungen) → `models.Aktion` (Quelle
  `excel:<Blatt>:<Zeile>`).
* **Quelle 2 – Rabattlisten-PDF** per Upload in der Freigabe-UI (Seite "Rabatte") oder CLI
  `rabatte-einlesen <pdf>`: Text-PDF über pdfplumber-Tabellen, sonst Claude Vision mit
  strukturierter Ausgabe (`Rabattliste` mit Konfidenzen) → der Anwender prüft die Tabelle →
  Speichern in `config/aktionen.yaml` (Gedächtnis, Quelle `pdf:<Datei>:<Seite>`).
* **Anwendung** (`rabatte/anwenden.py`, aufgerufen in `matching/pipeline.py` nach der
  Artikelzuordnung und bei `pruefe_erneut`): Eine Aktion passt auf eine Position, wenn Firma
  (oder alle), Kunde (Alias, Kundennummer, Kundengruppe oder Muster im Kundennamen; oder alle),
  Lieferant bzw. Artikelnummer bzw. Artikeltext (fuzzy) und das Bezugsdatum (`rabatte.bezug`:
  Liefertag, sonst Auftragsdatum, sonst heute) im Zeitraum liegen. Sicherer Treffer und
  `automatisch_anwenden` → `PositionsZuordnung.rabatt_prozent`, `rabatt_quelle = "aktion:<id>"`,
  Pruefhinweis `RABATT_VORSCHLAG` (Info; UI gelb). Unscharf (nur Artikeltext, Toleranztage,
  mehrere Aktionen) → `RABATT_UNSICHER` (Warnung), Anwender wählt. Rabatt laut Dokument
  (`RohPosition.rabatt_prozent`) ≠ Aktion → `RABATT_ABWEICHUNG` (Warnung, rot). Rabatt über
  `max_rabatt_prozent` → FEHLER. EUR-Rabatt und Aktionspreis werden in Prozent auf den VK
  Preisgruppe 1 umgerechnet (2 Nachkommastellen). **Der VK bleibt immer Preisgruppe 1; der Rabatt
  ist ein Positionsrabatt im Angebot/in der AB, nie im Einkauf.** `Belegpaket.aktionen` listet
  alle Treffer für die Anzeige; ohne Aktionsdatei läuft alles wie bisher.

## 8. Firmenwahl nach Katalog mit Gedächtnis

Vorgabe des Anwenders: **Fachhandel** → Weinstrasse Agentur **oder** WIK FH & SEH, je nach
Lieferant (Abschnitt `lieferanten` in `config/firmenkatalog.yaml`); **eigenständige
Lebensmittel-Einzelhändler** (eigene Kaufleute) → WIK FH & SEH; **Einkaufsregionen**
(Rhein-Ruhr, Minden-Hannover …), **große Ketten** und **Großkunden** → WIK LEH **oder** WOE →
das Tool **fragt** in der Freigabe und **merkt sich die Antwort je Kunde + Lieferant**
(`config/firmen_gedaechtnis.yaml`, `models.Firmenregel`).

Reihenfolge in `matching/firma.py`: Eingangsordner → **Gedächtnis** → Kunden-Alias →
**Katalog** → Stammdaten → Empfänger. Mehrere Firmen möglich und `firmenwahl.fragen_wenn_unklar`
→ `FIRMA_UNKLAR` (FEHLER, blockiert) mit `details["kandidaten"]`; die UI zeigt die Auswahl und
das Häkchen "für diesen Kunden und Lieferanten merken" (`Belegpaket.firma_merken`, Standard
an) → `firmenwahl.merke(paket, benutzer)`. Treffer aus dem Gedächtnis → `FIRMA_GELERNT` (Info,
"gelernt am … von …"), Quelle `"gedaechtnis"`; Treffer aus dem Katalog → Quelle `"katalog"`,
`Belegpaket.kundengruppe`.

## 9. Eigener Eingang und Upload

Der Eingang des Tools ist der OneDrive-Ordner **"Eingang"** (`eingang.ordner_name`): dort liegen
die Kundenaufträge und die Aufträge der Verkäufer. `Settings.eingang_ordner` zeigt auf den lokal
synchronisierten Pfad dieses Ordners (`eingang.onedrive_ordner` optional für Graph). Der Watcher
und die Freigabe-UI lesen denselben Ordner. Freigabe-UI: **"Auftrag hochladen"** (PDF, MSG, EML, Scan; `eingang.upload_*`) speichert
die Datei nach `eingang/[<firma>/]` und startet Extraktion und Prüfung sofort; danach erscheint
sie wie jede andere Eingangsdatei zur Freigabe. Nach Freigabe wird das Eingangsdokument als
**Kd** mit in den Provisionsordner abgelegt (`eingang.als_kundenbestellung_ablegen`).
