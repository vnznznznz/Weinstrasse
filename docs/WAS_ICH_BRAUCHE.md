# Was ich von dir brauche

Stand: 18.10.2026. Ich habe mir über den Microsoft-365-Zugang dieser Sitzung (nur lesend)
die relevanten Dateien in SharePoint/OneDrive und Beispiel-Mails angesehen. Vieles, was ich
ursprünglich erfragen wollte, konnte ich dadurch selbst klären (Abschnitt 0). Übrig bleibt
die Liste in den Abschnitten A bis E. Die Reihenfolge entspricht dem Vorgehen: erst
Extraktion + Matching + Freigabe an echten Belegen, dann Lexware im Dry Run, dann scharf,
danach Fixtermine/E-Mail/Tracking (Ausbaustufe 2, siehe `docs/AUSBAUSTUFE_2.md`).

**Die fünf wichtigsten Punkte:**

1. Frische Lexware-Exporte (Artikel, Kunden, Lieferanten, Belege) je Firma im bekannten
   Format nach `daten/<firma>/` (A1).
2. Zwei Screenshots aus Lexware: die Beleg-Erfassungsmaske von Bestellung und Auftrag, damit
   ich sehe, welche Lexware-Felder als "Supplier", "Transport by", "Shipment" und "Customer"
   auf den Formularen landen (B2).
3. Die Antworten auf die Verständnisfragen in Abschnitt C (vor allem Preisgruppe, "11+1",
   Firmenzuordnung).
4. Entscheidung Microsoft-365-Anbindung: Graph-App-Registrierung (empfohlen) oder lokal
   über Outlook/Excel auf dem PC (E1).
5. Einmaliges Einloggen in myHillebrandGori in dem Chrome-Profil, das das Tool nutzen darf,
   plus eine Beispielreferenz (E4).

---

## 0. Was ich bereits selbst gefunden habe (keine Lieferung nötig)

| Thema | Gefunden | Konsequenz |
|---|---|---|
| Lexware-Exportformat | `Buchhaltung … /General/BOME Lexware/` enthält Artikel-, Kunden- und Lieferantenexporte vom Juli 2026 (z. B. `Artikelexport_Wein Import Kreis LEH.csv`, `Kundendaten WOE.csv`, `export lieferanten.txt`) mit den originalen Spaltenköpfen | Spaltenzuordnung ist vorbereitet; ich brauche nur **aktuelle** Exporte im gleichen Format |
| Fixtermin-Excel | `Buchhaltung/General/Aufträge/Fixterminüberwachung.xlsx`: Blatt "Fixterminüberwachung", Kopf in Zeile 4, fünf Zeilen je Werktag, Spalten KW/Datum/Tag/Lieferant/Kunde/Auftragsdatum/Verladedatum/Bestätigung des Lieferanten/Bestätigung der Spedition/Abgeholt/Bemerkung; Kurznamen entsprechen den Lexware-Matchcodes | Register-Modul schreibt genau in diese Struktur, Spalten A–C (Formeln) bleiben unberührt |
| Kontaktliste | `Ansprechpartner Abwicklung.xlsx` (dein OneDrive, Teams-Chatdateien): Reiter Lieferanten (je Land), Speditionen (Hillebrand, Schenker/DSV, Stark, Bouché), Kunden (je Kunde mit Produkt, Dispo, Spedition) | Kontakt-Modul liest diese Datei; Hillebrand-Postfächer für neue Aufträge und Statusanfragen stehen in der Kontaktliste |
| E-Mail-Stil | "new order" ans Weingut (nur Signatur + Bestell-PDF), "Bestätigung" an Kunden ("Hallo Frau X! Bitte finden Sie anbei die Auftragsbestätigung."), "Neuer Auftrag <Kunde>" an das Auftragspostfach der Spedition mit Speditionsauftrag + Lieferschein, CC Auftragsmanagement | Vorlagen sind daraus abgeleitet |
| Lexware-Formulare | Bestellung (englisch "Order": Supplier No., Transport by, Shipment, Order No., delivery date), AB (Bestellnr., KD.USt-IdNr., Supplier, Transport by, Shipment), Lieferschein, Speditionsauftrag als Formularvariante der Bestellung | Die vier Belegfelder sind damit identifiziert; offen ist nur, welche Lexware-Felder dahinterstehen (B2) |
| Hillebrand | Bestätigung per Mail "Auftragsbestätigung; Your Ref: …; Our Ref: DEMB<Nummer>[jobDEM…]"; Portal myHillebrandGori mit Meilensteinen und Benachrichtigungen | Tracking primär aus den Mails, Portal als Ergänzung |
| Ablage-Prozess | SharePoint-Ordner `Aufträge/.KW <n> <Kürzel>/<Kundengruppe>/<Kunde>-<Lieferant>/` mit Dateimuster `<Lieferant>-<Firma>-<Kunde>-<Nr>-<Kd/Cde/AB/LS>-<Datum>.pdf`, danach "Auftragsablage (mit/ohne Provision)" | Eingangs- und Archivordner können direkt darauf zeigen |
| Echte Bestell-Layouts | Weinmarkt Kirchhellen (Shopsystem, "ArtNr", "Hinweis: 11+1", Zeile "DAA Neptun" mit Verbrauchsteuernummer), SPAR (SAP, Mengen in VE à 6), Rheinberg/Edeka ("Lagerbestellung", Kolli × Inhalt), Menrath (Scan ohne Textebene) | Extraktion wird auf genau diese Layouts erweitert; Mengen in Verpackungseinheiten werden in Flaschen umgerechnet |

---

## A. Für Phase 1: Extraktion, Matching, Freigabe an echten Daten

### A1. Aktuelle Stammdaten-Exporte je Firma

Bitte denselben Export wie im Juli (Lexware → Datei → Export), jetzt aktuell, für alle vier
Firmen nach `daten/<firma>/` (`weinstrasse_agentur`, `wik_fh_seh`, `wik_leh`, `woe_leh`):

| Datei | Lexware-Export | Hinweis |
|---|---|---|
| `artikel.csv` | Artikelexport | enthält `Einheit` (= Volumen), `Warengruppe` (= Lieferant), `Preis 1`, `EK Preis Lf.1`, `Lieferantennummer 1`, `Benutzerdefiniert 2` (= Jahrgang?), `Gesperrt` |
| `kunden.csv` | Kundendatenexport | `Matchcode` (Kurzname), `Firma`, `Preisgruppe`, `Inaktiv`, `Bemerkung` |
| `lieferanten.csv` | Lieferantenexport | `Matchcode`, `Firma`, `Inaktiv` |
| `belege.csv` | Export der Aufträge/Bestellungen der letzten 12 Monate (Belegnummer, Kundenbestellnummer, Kunden-/Lieferantennummer, Datum) | falls Lexware das nicht exportiert: Liste aus der Auftragsübersicht kopieren (Excel reicht) |

Der Ordner `daten/` ist vom Repository ausgeschlossen, echte Daten landen nicht auf GitHub.
Frage dazu: Der Juli-Export "Wein Import Kreis FH SEH und Weinstrasse Agentur" ist **eine**
Datei. Sind FH & SEH und Weinstrasse Agentur in Lexware ein Mandant oder zwei?

### A2. Beispiel-Belege für den Test

Die Wochenordner (`.KW <n> …/Fachhandel/…`, `…/Spar/…`) und `Aufträge von Kunden/…` enthalten alles,
was ich brauche. Bitte bestätige, dass ich diese Belege (anonymisiert) als Testfälle in
`beispiele/` ablegen darf, oder lege selbst 3 bis 5 je Typ dort ab (siehe `beispiele/README.md`).
Besonders hilfreich: zwei bis drei eingescannte Preislisten mit handschriftlichen Mengen
(Typ C), davon eine mit durchgestrichenen Zeilen und Summenvermerk ("180 R").

### A3. Alias-Tabellen und Firmenzuordnung

`config/aliase/` enthält deine Beispiele (Kirchhellen → 3441, Menrath → 1392, Colosi 87,
Bollina 138, Cide 72, Artikel 1643/957). Bitte je Eintrag die Firma (Mandant) bestätigen.
Die Weinmarkt-Kirchhellen-Bestellung ist an "Weinstrasse Adolph" adressiert, die Nummer 3441
soll aber laut deiner Angabe im WIK-Stamm liegen. Welcher Mandant bucht Fachhandelsaufträge
mit Villa-Armellina-Ware?

### A4. Exakte Firmennamen in Lexware

Die vier Namen aus der Firmenauswahl (Datei → Firma wechseln) buchstabengetreu, für
`config/firmen.yaml` → `lexware_firmenname`.

---

## B. Für Phase 2: Lexware-Anbindung (Dry Run)

### B1. Version, Datenbank und eCommerce-Modul

Ergebnisse der Recherche (`docs/recherche/opentrans_lexware.md`, `odbc_lexware.md`):

* **Datenbank:** Lexware pro/premium 2026 läuft auf PostgreSQL (Umstellung ab Mitte 2026),
  abgesichert mit Zertifikaten. Ein lesender Direktzugriff per ODBC ist damit praktisch nicht
  möglich und von Lexware nicht vorgesehen. Bitte unter "? → Info → Details" nachsehen, ob in
  der letzten Zeile "SQL Anywhere" oder "PostgreSQL" steht. Bei PostgreSQL bleibt es beim
  CSV-Export (A1), das ist der offiziell gestützte Weg.
* **Belege für die Dublettenprüfung:** Aufträge sind keine Datenquelle des Export-Assistenten.
  Sie kommen nur über die Auftragsliste heraus (Verwaltung → Aufträge → Exportieren → nach
  XML bzw. nach MS Excel). Bitte einmal beide Varianten für einen kleinen Zeitraum erzeugen
  und bereitstellen, damit ich das Format einlesen kann.
* **eCommerce (früher eBusiness):** Das Modul ist in der Warenwirtschaft enthalten, muss aber je
  Firma unter Firmenangaben → Services ("Anbindung an Lexware eCommerce") aktiviert sein.
  Wichtig: Der Import einer openTRANS-Datei (Extras → eCommerce → Daten austauschen) erzeugt
  **keinen** Beleg, sondern einen Webshop-Auftrag in der Ansicht eCommerce. Erst "Kunde
  zuordnen" und "Auftrag übernehmen als Auftragsbestätigung" erzeugen den Beleg, und die
  Kundennummer wird nicht aus der XML übernommen. Der Import spart also nur die
  Positionserfassung. Entscheidung bitte: (a) openTRANS + manuelles Übernehmen in Lexware,
  (b) openTRANS + UI-Automation des eCommerce-Dialogs, oder (c) die Verkaufs-AB komplett
  über den Auftragsassistenten per UI-Automation (so wie die Einkauf-Bestellung). Ich
  empfehle (c), weil dann Kunde, Belegfelder und Bestellnummer in einem Lauf gesetzt werden.
* **Einkauf-Bestellung:** Lexware kennt keine automatische Bestellung aus einer AB. Es gibt
  "Auftrag wandeln" (Positionen eines Verkaufsbelegs in eine Bestellung übernehmen; EK-Preis
  nur, wenn der Lieferant am Artikel als Bezugsquelle hinterlegt ist). Frage: Sind bei euren
  Artikeln die Lieferanten als Bezugsquelle mit EK hinterlegt (Artikelstamm → Einkauf)? Dann
  kann ich "Auftrag wandeln" als schnelleren Weg einbauen; sonst bleibt der Bestellassistent.
* Hilfe → Info: Produkt, Version, Build (Screenshot).

### B2. Beleg-Erfassungsmaske (wichtigster Punkt für die Automation)

Laut Recherche (`docs/recherche/ui_automation_lexware.md`) hat der Auftragsassistent drei
Seiten (Kopfdaten mit bis zu sechs Auftrags-Freifeldern, Positionen, Summen). "Supplier",
"Transport by", "Shipment" und "Customer" sind sehr wahrscheinlich umbenannte Freifelder
(Verwaltung → Einstellungen → Freifelder). Bitte:

* Screenshot der Freifeld-Einstellungen je Firma und der Seite 1 des Assistenten mit einem
  fertigen Beleg (Bestellung und Auftrag), damit ich die Zuordnung Feld ↔ Freifeld festlege.
* `auftragsautomat lexware-inspizieren` ausführen, während die Maske offen ist (schreibt die
  Steuerelemente nach `logs/inspektion/`), für Seite 1 bis 3, für die Dialoge nach dem
  Speichern und für "Auftrag weiterführen". Es gibt keine veröffentlichten Erfahrungen zur
  Automatisierung von Lexware; alle Steuerelement-Namen müssen an eurem System aufgenommen
  werden. Der Lexware-Prozess heißt `Framework.exe`.
* Unter Extras → Optionen → Auftragsbearbeitung / E-Mail-Versand lassen sich die Abfragen
  nach dem Speichern (Drucken, E-Mail, eRechnung, Festschreiben) abschalten. Darf ich das für
  die vier Firmen vorsehen? Dann entfallen die Dialoge, die sonst automatisiert beantwortet
  werden müssen.

### B3. Datenbankzugriff (nur noch, falls das System "SQL Anywhere" zeigt)

Nur wenn unter "? → Info → Details" noch SQL Anywhere steht: System-DSN "LXSYDSN" (32-Bit),
lesender Benutzer, Passwort nur lokal als Umgebungsvariable; danach
`auftragsautomat odbc-erkunden --firma …`. Bei PostgreSQL entfällt dieser Punkt.

### B4. Testumgebung

Test-Mandant oder Datensicherung vor dem ersten scharfen Lauf; Zeitfenster, in dem niemand
sonst in Lexware arbeitet (der Dry Run öffnet Assistenten und bricht vor "Speichern" ab).

### B5. Claude-API-Zugang für Scans

API-Schlüssel aus der Anthropic Console als Umgebungsvariable `ANTHROPIC_API_KEY` auf dem PC.

---

## C. Verständnisfragen, die das Verhalten bestimmen

1. **Preisgruppe:** Auf den Fachhandels-ABs steht ein VK über dem EK der Bestellung. Gilt für die
   AB immer "Preis 1" aus dem Artikelstamm, auch wenn der Kunde in Lexware eine andere
   Preisgruppe hat? Bei LEH-Bestellungen steht der Preis in der Kundenbestellung: nur warnen bei
   Abweichung oder Bestellpreis übernehmen?
2. **"11+1":** Fachhändler bestellen z. B. 330 Fl. mit "Hinweis: 11+1". Wie wird das in
   Lexware gebucht (330 zum Preis, 30 Gratis-Flaschen als eigene Position zu 0,00, oder Rabatt)?
   Gilt das auch in der Bestellung ans Weingut?
3. **"180 R" im Summenvermerk:** Flaschen, Kartons oder etwas anderes?
4. **Mengen in Verpackungseinheiten:** SPAR bestellt "570 VE à 6". Soll die Lexware-Position
   3.420 Flaschen lauten (so geplant) und die VE nur in der Bemerkung stehen?
5. **Belegfeld "Shipment":** Standard "directly" bzw. "end of week <KW des Liefertags>"?
   Woher kommt "Transport by" (fest je Kunde laut Kontaktliste: Bouché für Rheinberg, Stark
   für SPAR, sonst Hillebrand)?
6. **Bestellnummer:** Im Fachhandel vergibt WIK eine eigene Nummer (20260024, auch auf AB und
   Lieferschein), im LEH wird die Kundenbestellnummer verwendet. Vergibt Lexware die
   WIK-Nummer automatisch oder wird sie manuell eingetragen?
7. **Firma je Eingang:** Soll die Firma aus dem Unterordner kommen oder aus dem Beleg
   (Agentur = "mit Provision", WIK/WoE = "ohne Provision")?
8. **Jahrgang ohne Angabe:** nachfragen (aktuell) oder neuesten Jahrgang vorschlagen?
9. **Lieferschein:** nur auf Knopfdruck in der Freigabe, nie automatisch. Richtig?
10. **Mehrere Weingüter in einer Kundenbestellung** (z. B. Villa Armellina und Colosi):
    umgesetzt ist je Weingut eine eigene Einkauf-Bestellung und eine AB mit allen Positionen,
    Belegfeld "Weingut" dann mit allen Namen durch " / " getrennt. Passt das, oder soll je
    Weingut auch eine eigene AB entstehen?
11. **Eigene Bestellung als Eingang (Typ B):** Die Preise im Dokument sind EK-Preise. Soll
    die Prüfung dann gegen den EK statt gegen den VK vergleichen?

---

## D. Was du dafür bekommst (Stand der Umsetzung)

* Stufe 1 ist gebaut und mit 894 automatischen Tests abgesichert: Extraktion (Text-PDF,
  Scan per Claude, E-Mail), Stammdaten (CSV, ODBC-Gerüst), Alias- und Fuzzy-Matching,
  Prüfregeln, Freigabe-Ansicht (`auftragsautomat freigabe`), CLI, Ordner-Watch, Protokoll,
  openTRANS-Export, UI-Automation-Gerüst mit Fake-Backend, Dry-Run-Buchung.
* Dry Run als Standard; scharf nur mit `--scharf` und Bestätigung.
* Alle Annahmen über Lexware im Code als `TODO-LEXWARE` markiert und in `docs/recherche/`
  begründet.

---

## E. Für Ausbaustufe 2: Fixtermine, Kontakte, E-Mail-Entwürfe, Tracking

### E1. Entscheidung Microsoft-365-Anbindung

Zwei Wege, beide sind vorbereitet:

* **Microsoft Graph (empfohlen):** Die Excel wird serverseitig (Excel Online) bearbeitet,
  Formeln und Gliederung bleiben erhalten, auch wenn Kolleginnen die Datei in Excel offen
  haben; Entwürfe landen direkt im Postfach. Dafür muss einmalig eine App-Registrierung in
  Microsoft Entra angelegt werden: Single-Tenant, Plattform "Mobile and desktop applications"
  mit `http://localhost`, "Allow public client flows" = Yes, delegierte Rechte User.Read,
  Files.ReadWrite, Sites.Read.All, Mail.ReadWrite, Mail.Read, offline_access (alle ohne
  Admin-Zustimmung, sofern die Tenant-Richtlinie Benutzerzustimmung erlaubt). Ich brauche dann
  Application (client) ID und Directory (tenant) ID für `config/settings.yaml`, außerdem die
  Web-URLs der beiden Excel-Dateien. Die Anleitung steht in
  `docs/recherche/graph_excel_outlook.md`, Abschnitt "Checkliste für den Anwender".
  Wichtig: Ob die Excel-365-Kontrollkästchen nach einem Graph-Schreibzugriff erhalten bleiben,
  ist nicht dokumentiert; ich teste das zuerst an einer Kopie der Datei.
* **Lokal (ohne Registrierung):** Das klassische Outlook und Excel auf dem PC werden per COM
  gesteuert (das neue Outlook hat keine COM-Schnittstelle). Geht nur, wenn beide auf dem
  Automations-PC installiert und angemeldet sind; bei gleichzeitig geöffneter Excel kann das
  Schreiben scheitern. Frage dazu: Nutzt ihr das klassische oder das neue Outlook?

### E2. Postfach und Absender

* Aus welchem Postfach sollen die Entwürfe erzeugt werden (dein Postfach oder ein Logistik-Postfach)?
* Signatur: persönliche Signatur "i.V. für …" oder die Firmensignatur? Je Firma unterschiedlich
  (WIK-Team, WoE-Team, Weinstrasse-Team)?
* Immer CC an das Auftragsmanagement?
* Welche PDFs sollen angehängt werden und woher: Lexware kann die Belege beim Speichern als
  PDF exportieren (Dialog "Drucken/E-Mail" → PDF) oder ich nehme die abgelegten PDFs aus dem
  SharePoint-Ordner. Hinweis: Deine Vorgabe "nichts drucken, nichts mailen" bleibt; es werden
  nur Entwürfe erzeugt.

### E3. Fixtermin-Regeln

* Ab wie vielen Tagen vor dem Fixtermin ohne Lieferantenbestätigung soll nachgefragt werden
  (Vorschlag 10 Werktage), ohne Speditionsbestätigung (Vorschlag 5 Werktage)?
* Wer bekommt die interne Erinnerung (Entwurf an dich selbst, Outlook-Aufgabe, nur die
  Streamlit-Seite "Heute")?
* Darf das Tool "Bestätigung des Lieferanten/der Spedition" selbst ankreuzen, wenn eine
  eindeutige Mail vorliegt (Hillebrand-Referenz passt zu Lieferant, Kunde und Termin)?
* Terminverschiebungen (z. B. Hillebrand/Transporeon nennt neuen Tag): nur Vorschlag zur
  Freigabe (geplant) oder automatisch eintragen?

### E4. Hillebrand-Tracking

Die Recherche (`docs/recherche/hillebrand_tracking.md`) hat ergeben, dass Hillebrand Gori eine
offizielle REST-API anbietet. Das ist der saubere Weg; Portal-Scraping ist laut Nutzungs-
bedingungen von hillebrandgori.com nicht erlaubt und bleibt nur Rückfall.

* **API-Zugang beantragen**: unter developer.hillebrandgori.com registrieren (myHillebrandGori-
  Konto nötig, API-Key wird von Hillebrand manuell freigegeben). Bitte euren Ansprechpartner bei
  Hillebrand Gori Germany fragen, ob ein technischer Benutzer ohne MFA möglich ist. Key und
  Secret kommen nur als Umgebungsvariablen auf den PC, nie in Dateien.
* **Meilenstein-Mails** im Portal unter "Notification preferences" für Abholung und Zustellung
  per E-Mail aktivieren und 5 bis 10 Beispielmails (Auftragsbestätigung, Abholung, Zustellung,
  Terminänderung) als `.eml` bereitstellen, damit ich die Betreffmuster testen kann.
* **Eigenes Chrome-Profil**: Das Tool kann dein normales Chrome-Profil technisch nicht nutzen
  (Chrome blockiert das seit Version 136). Es legt ein eigenes Profil unter
  `%LOCALAPPDATA%\Auftragsautomat\chrome-profil` an; dort einmal manuell einloggen. Fragen:
  Wird beim Login ein zweiter Faktor verlangt? Wie lange bleibt die Sitzung bestehen?
* Welcher Suchbegriff findet die Sendung im Portal bzw. unter
  `my.hillebrandgori.com/public-tracking/<Begriff>`: die "Our Ref" (DEMB…), die Job-Nummer oder
  eure eigene Referenz? Bitte je einen Screenshot.

### E5. Kontaktpflege

* Darf das Tool die Kontakt-Excel ändern, wenn eine Mail-Signatur neue Daten zeigt
  (geplant: nur Vorschlag zur Freigabe)?
* Lexware-Kundenstamm soll laut Spezifikation nicht automatisch geändert werden; Änderungen
  erscheinen als Hinweis im Bericht. Einverstanden?

### E6. Dossiers in OneDrive

* Wo soll der Ordner **"vorangefertigte Dossiers"** liegen (z. B. neben "Aufträge" im
  Buchhaltungs-SharePoint oder in deinem OneDrive)? Ich lege ihn an, wenn er fehlt.
* Soll je Dossier zusätzlich ein zusammengeführtes Gesamt-PDF erzeugt werden, oder reichen
  Deckblatt und Einzeldateien?
* Nach welcher Logik sortieren die Mitarbeiter ein (Auftragsablage mit/ohne Provision →
  Kunde → Jahr)? Dann kann das Deckblatt den Zielordner gleich vorschlagen.
* Die Auftragskopie an den Kunden: Soll auch der Dateiname "Auftragskopie-…" lauten, und
  darf ich den Formulartitel "Auftragsbestätigung" in Lexware als Änderungswunsch für Bome
  notieren, damit das PDF zur Mail passt?

## F. Lexware-Ablauf laut Ablaufplänen (Rückfragen)

* **Weg A oder B** im Verkauf: Angebot → "Weiterführen" (A) oder Angebot → "Auftrag wandeln"
  (B)? Ich habe A als Standard je Firma eingestellt (`verkauf_weg` in `config/firmen.yaml`).
* **WOE**: eigene Formulare oder ebenfalls "WIK EN" / "WIK AB_LS"? Bitte die exakten
  Formularnamen aus dem Lexware-Druckdialog (Screenshot genügt).
* **PDF-Ausgabe**: Soll das Tool die Belege über den PDF-Drucker ("Microsoft Print to PDF") oder
  über den Lexware-Export als PDF ablegen? Welcher Ordner ist der Arbeitsordner für die PDFs?
* **Revised Order**: immer Bestellung und AB neu anlegen, auch bei reiner Mengenänderung?
  Wie heißt der Hinweis im Kundendokument (z. B. "Revised order", "Änderung", "Storno")?
* **"Sch."** in der Titulierung: steht das für Schenker? Welche Speditionen gibt es außer
  Hillebrand (Stark, Bouché, …) und welche Kunden fahren darüber?

## G. Aktionszeiträume und Rabatte

* **Die Aktionszeiträume-Excel** (Link/Pfad, Blattname, Spaltenüberschriften). Ich habe
  angenommen: Kunde, Lieferant, Artikel, Artikelnummer, Von, Bis, Rabatt %, Rabatt EUR,
  Aktionspreis, Aktion, Bemerkung, Firma. Abweichende Überschriften trage ich in
  `rabatte.spalten` ein (`config/settings.yaml`).
* **2 bis 3 Beispiel-PDFs** mit Rabattlisten, so wie sie wirklich aussehen (Weingut-Preisliste
  mit Aktionsspalte, Kundenvereinbarung, Konditionsblatt).
* Gilt ein Aktionszeitraum für den **Liefertag** oder das **Auftragsdatum**? Standard: Liefertag.
* Rabatt im Angebot/in der AB als **Positionsrabatt in Prozent** – richtig? Gibt es auch
  Naturalrabatte ("11+1") als Aktion, die als Gratisposition erfasst werden?
* Dürfen sichere Treffer **automatisch** eingetragen werden (sichtbar markiert), oder soll jede
  Aktion erst bestätigt werden?

## H. Firmenkatalog

* **Der Katalog**: Welche Lieferanten (Weingüter) laufen im Fachhandel über die Weinstrasse
  Agentur, welche über WIK FH & SEH? Eine Liste oder die Katalog-Datei genügt; ich übertrage sie
  nach `config/firmenkatalog.yaml` (Abschnitt `lieferanten`).
* Liste der **Regionen, Ketten und Großkunden** mit den Namen, wie sie in Lexware stehen; dann
  fülle ich die Muster im Katalog, und das Tool fragt nur noch beim ersten Auftrag je Kunde
  und Lieferant, ob WIK LEH oder WOE.
* Reicht "merken je Kunde + Lieferant", oder soll die Antwort je Kunde **pauschal** gelten?

## I. Ablage "Aufträge mit/ohne Provision" und Eingang "Eingang"

Stand 24.10.2026: Kundenaufträge liegen im OneDrive-Ordner **"Eingang"**; fertige Dokumente
der Weinstrasse Agentur kommen nach **"Aufträge mit Provision"**, die aller anderen Firmen
nach **"Aufträge ohne Provision"**. So ist es konfiguriert (`ablage.*`, `eingang.*`).

* **Links** (Adresszeile im Browser) zu den drei Ordnern "Eingang", "Aufträge mit Provision" und
  "Aufträge ohne Provision" bzw. zu dem Ordner, der die beiden Provisionsordner enthält.
* Brauchst du in den Provisionsordnern Unterordner (z. B. je Jahr oder je Kunde), oder bleibt es
  flach? Standard ist flach (`ablage.struktur = "{provision}"`).
* Welche Dateien gehören hinein: Kundenbestellung (Kd), Bestellung (Cde), Auftragskopie (Kopie),
  Speditionsauftrag, Lieferschein (Ls) – alle, oder nur eine Auswahl?
* Ist der Ordner "Eingang" auf dem Lexware-PC per OneDrive synchronisiert? Dann zeige ich
  `eingang_ordner` auf diesen Pfad; der Upload in der Oberfläche speichert in denselben Ordner.

