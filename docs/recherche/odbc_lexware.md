# Recherche: Lesender Zugriff auf die Datenbank von Lexware premium 2026

Stand: 2026-10-18. Projekt „Auftrags-Automat für Lexware premium“ (siehe `docs/ARCHITEKTUR.md`,
Modul `stammdaten/odbc_quelle.py` bzw. `stammdaten/csv_quelle.py`).

Kennzeichnung in diesem Dokument:

* **[V]** = verifiziert (mit Quelle/URL und Zitat bzw. konkretem Fundort).
* **[V-Dritt]** = verifiziert als *Aussage einer Drittquelle* (Open-Source-Projekt, Partner-Doku, Forum) –
  nicht von Lexware bestätigt. Kann für die konkrete Installation abweichen.
* **[A]** = Annahme / unsicher (mit Begründung). Muss am System geprüft werden.
* **[NF]** = nicht auffindbar; Prüfauftrag für den Anwender formuliert.

## Zusammenfassung (10 Zeilen)

1. **[V]** Lexware pro/premium **Version 2026 läuft nicht mehr auf SAP SQL Anywhere, sondern auf PostgreSQL**
   („Ab der Version 2026 nutzt Lexware nicht mehr SAP SQL Anywhere, sondern PostgreSQL als Datenbank.“).
   Bestandsinstallationen werden „in Etappen“ umgestellt; welche DB läuft, zeigt `? → Info… → Details`, letzte Zeile.
2. **[V]** Lexware verbietet den Direktzugriff nicht ausdrücklich, unterstützt ihn aber auch nicht: Die API ist „nicht
   öffentlich zugänglich“ (nur zertifizierte Partner), Zusatzprogramme lassen sich mit der neuen DB „nicht mehr wie
   gewohnt anbinden“, eigene Anbindungen „funktionieren nach der Umstellung nicht mehr“.
3. **[V-Dritt]** Der PostgreSQL-Server (Standardport **15432**) ist mit TLS-Clientzertifikat und verschlüsselt
   abgelegtem Serverpasswort abgesichert; ein normaler ODBC-/psql-Zugriff ist ohne Reverse Engineering **praktisch nicht möglich**.
4. **[V]** Ein Menüpunkt „Datenbankzugriff/ODBC freischalten“ existiert in keinem Handbuch (Begriff „ODBC“ kommt im
   Warenwirtschafts-Handbuch 2024 und im Technischen Handbuch 2026 nicht vor).
5. **[V-Dritt]** Tabellenwelt (SQL Anywhere bis 2025, in PostgreSQL namensgleich in Kleinbuchstaben): Firmen-DBs `F1…Fn`
   mit ~146 `FK_`-Tabellen (`FK_Artikel`, `FK_Kunde`, `FK_Lieferant`, `FK_Auftrag`, `FK_AuftragPos`,
   `FK_Preismatrix`, `FK_Preisgruppe`, `FK_Nummernkreis`, `FK_Vorgang`), Verwaltungs-DB `LXOFFICE` (`LEXGLOBAL.LXG_FIRMA`, `LXG_USER`).
6. **[V]** Offizieller Export: `Datei → Export → ASCII` für **Artikel, Kunden, Lieferanten, Buchungsdaten** (eine Datenquelle
   je Export), Standard Semikolon + `"`-Textkennzeichen, „ANSI (Windows)“, Option „Feldnamen in den ersten Datensatz“, Profile speicherbar.
   Vollständige Feldlisten liegen in der Lexware-„Schnittstellenbeschreibung.pdf“ (hier übernommen).
7. **[V]** **Keinen Auftrags-/Belegexport** über `Datei → Export`; Belege kommen nur über die Bildschirmliste
   `Verwaltung → Aufträge` → „Exportieren → Ansicht nach MS Excel / ins pdf-Format / nach xml exportieren“ (bzw. Kontextmenü) heraus.
8. **Bewertung:** Für „nur lesen“ ist der **Export-Weg** (ASCII + Listen-Export) der einzige offiziell gestützte, risikofreie
   Weg; ODBC ist nur realistisch, wenn das System *noch* auf SQL Anywhere läuft – und auch dann inoffiziell.
9. Risiken: eine DB pro Firma (4 Firmen = 4 Firmen-DBs + `lxoffice`), Datensicherung sperrt die DB, Schema ohne Doku und
   mit Versionsänderungen, Lizenz-/Supportfragen beim Direktzugriff.
10. Erster Schritt für den Anwender: `? → Info… → Details` (SQL Anywhere oder PostgreSQL?) + Dateien in
    `…\Daten\PropertyStore` prüfen; danach Export-Dateien je Firma erzeugen (Checkliste am Ende).

---

## 1. Datenbanksystem, Dateien, Dienste, Ports, ODBC, Zugang

### 1.1 Welche Datenbank? Bis 2025 vs. ab 2026

**[V]** Lexware-Beratungsseite zur Datenbankumstellung: „Ab der Version 2026 nutzt Lexware nicht mehr SAP SQL Anywhere,
sondern PostgreSQL als Datenbank.“ (https://www.lexware.de/support/lexware-datenbankumstellung-beratung)

**[V]** FAQ 000036493: „Ab dem 01.01.2026 nutzen Lexware pro und premium eine neue Datenbank.“ Prüfung, welche DB läuft:
`? – Info…` → unten links `Details` → **letzter Eintrag der Modulliste** lautet `SQL Anywhere` oder `PostgreSQL`.
(https://www.lexware.de/support/faq/faq-beitrag/000036493)

**[V]** FAQ 000035913 („Zugriff von Drittanbieter-Zusatzprogrammen auf die Datenbank Ihres Lexware-Produkts“):
„Ab etwa Juni 2026 wird Lexware in zahlreichen Produkten eine Umstellung der bisher genutzten Datenbank auf PostgreSQL
vornehmen.“ – „Die Umstellung findet in Etappen statt. Deshalb lässt sich nicht vorhersehen, mit welcher Datenbank das
Programm auf Ihrem alten Rechner in diesem Moment arbeitet.“ (https://www.lexware.de/support/faq/faq-beitrag/000035913/)

**[V]** FAQ 000036324 (Rechnerwechsel 2026): Rücksicherungen zwischen SQL-Anywhere- und PostgreSQL-Installation sind
nicht möglich: „Wechselseitige Datenrücksicherungen zw. A und B sind nicht möglich, auch wenn die Programmversion identisch ist.“
(https://www.lexware.de/support/faq/faq-beitrag/000036324/)

**Konsequenz für den Anwender [A]:** Eine „premium 2026“-Installation ist mit hoher Wahrscheinlichkeit bereits PostgreSQL
(Neuinstallation 2026 = immer PostgreSQL; Update-Installationen wurden ab ca. Juni 2026 migriert). Sicher ist nur die Prüfung
über `Info → Details`.

### 1.2 Stand bis Version 2025: SAP SQL Anywhere 17

| Punkt | Befund | Quelle |
|---|---|---|
| Produkt/Version | **[V]** SQL Anywhere 17 (Prozess `dbsrv17.exe` auf dem Server, `framework.exe` am Client; Ereignisanzeige-Quelle „SQLANY 16.0 / SQLANY 17.0“). Pfad des Treibers: `C:\Program Files (x86)\SQL Anywhere 17\Bin32` | FAQ 000003321, FAQ 000035823 |
| Dienstname | **[V]** „Lexware Premium Datenbank“ bzw. „Lexware Professional Datenbank“ (services.msc) | FAQ 000003321, 000003479 |
| Port | **[V]** 2638 (Client/Netz), 62622 bei Einzelplatzinstallation | FAQ 000003321, FAQ 000003245 |
| DB-Servername | **[V]** `LXDBSRV` (Fehlerlog) / `lxdbsrv` (FubarDev-Konfiguration) | borncity 2018, FubarDev-Wiki |
| ODBC | **[V]** System-DSN **`LXSYDSN`**, Treiber „[Sybase][ODBC Driver][SQL Anywhere]“; Verwaltung über `odbcad32.exe` (64-Bit-Windows: `%windir%\SysWOW64\odbcad32.exe`), Registerkarte „Netzwerk“ mit Servername/IP | FAQ 000003321, borncity 2018 |
| Datenbankdateien | **[V]** `C:\ProgramData\Lexware\[premium|professional|plus]\Datenbank\` mit `LXOFFICE.DB/.LOG`, `LexKK.DB/.LOG`, `LexTransfer.DB/.LOG`, Unterordner `F1`, `F2`, … (ein Ordner je Firma; `F0` = Muster). Musterdatenbank unter `C:\Programme [(x86)]\Lexware\[Reihe]\[Jahr]\RoF\db12\datenbank` | FAQ 000003479, FAQ 000036324 |
| Firmen-DB-Datei | **[V]** Fehlerlog 2018: `DBFile: C:\PROGRAMDATA\LEXWARE\PROFESSIONAL\Datenbank\F178\LXCOMPANY`, `DBName: F178`, `Login: U0`, `Server: LXDBSRV` | borncity 2018 |
| Logins | **[V-Dritt]** Lexware-Benutzer werden als DB-Benutzer `U{userId}` angelegt (Gruppen `LEXGLOBAL`, `F1`, Eintrag in `LXG_USER`); FubarDev-Wiki dokumentiert für die `lxoffice`-DB einen fest hinterlegten Lese-Login `_login_`, Referenz-DBs (LX, UK, …) mit `DBA`. Passwörter in `LXG_USER.PASSWORD` sind verschlüsselt (Verfahren V0–V5) | FubarDev-Wiki „Database Backends“, „Architecture“ |
| Zeichensatz | **[V-Dritt]** Windows-1252; `char(128)`-Felder mit Leerzeichen aufgefüllt → `TRIM()` nötig | FubarDev README/Wiki |

Hinweis: Das ältere, 2022 archivierte GitHub-Projekt `FubarDevelopment/Lexware` warnt: „Lexware hat offenbar geändert, wie
das Passwort (bzw. dessen Hash) gespeichert wird. Der Code funktioniert deshalb nicht mehr.“ – also schon zu SQL-Anywhere-Zeiten
ein fragiler Weg. (https://github.com/FubarDevelopment/Lexware)

### 1.3 Stand ab Version 2026: PostgreSQL

| Punkt | Befund | Quelle |
|---|---|---|
| Programmverzeichnis DB | **[V]** `C:\Programme\Lexware\PostgreSql` | FAQ 000036354 |
| Cluster/Datenverzeichnis | **[V]** `C:\ProgramData\Lexware\LexwarePG` („Nach der Installation wird der Datenbank-Cluster erzeugt“) | FAQ 000036354 |
| Datenverzeichnis Programm | **[V]** `C:\ProgramData\Lexware\[Reihe]\{Daten, Datenbank, Formulare, Konfiguration}`; Client greift auf `\\[Server]\Lexware_[Reihe]` zu | FAQ 000036354 |
| Dienste (Server) | **[V]** `Lexware_postgresql_pro` (pg_ctl.exe), `Lexware Postgresql Monitor` (Haufe.Components.LexwareService.exe), `Lexware API Service`, `Lexware Internal API Service`, `Lexware Security Service`, `Lexware ServiceBus Connector`, `Lexware Task Service`, `Lexware Update Service`, `Lexware WebApps Service`, `Lexware Installer Service` | FAQ 000036354; Technisches Handbuch Mehrplatz 2026, S. 35 |
| Ports | **[V]** **15432** = Datenbankdienst (Datei `PostgresPort.ps`), **14072** = Monitorservice (`ServicePort.ps`). Abweichende Ports: Datenpfad (`Info… → Link „Daten-Pfad“`) → `PropertyStore` → Datei im Texteditor, „der fünfstellige Port steht in der dritten Zeile zwischen den Tags value und /value“ | FAQ 000003245 |
| PropertyStore-Dateien | **[V]** `…\Daten\PropertyStore\`: `dsn.ps`, `PostgresAddress.ps`, `PostgresAddressV6.ps`, `PostgresPort.ps`, `ServicePort.ps` | FAQ 000036320 |
| PostgreSQL-Version | **[NF]** in keiner Lexware-Quelle genannt → Prüfauftrag (`postgres.exe --version`) | – |
| Zugangsdaten | **[V-Dritt]** Login `garinn` + Serverpasswort aus `Daten/postgresql.dd` (**verschlüsselt** abgelegt); **TLS mit Clientzertifikat**, das vom lokalen „LexwareService“ (Port 14072, Endpunkt `/certification`) ausgestellt wird; Authentifizierung dort per **TOTP**, Secret aus `Daten/service/pin2.bin`. Lexware-Benutzer erscheinen als DB-Rollen `ole{userId}`; keine automatische Provisionierung. Erkennung des Backends: enthält der DSN in `dsn.ps` „postgre“, ist es PostgreSQL | FubarDev-Wiki „Database Backends“, „Configuration“, „Architecture“ |
| Datenbanknamen | **[V-Dritt]** `lxoffice` (Benutzer/Firmen), `f1`, `f2`, … (eine DB je Firma), globale `lx`, `lg`, `rk`, `uk`, `lexkonto`, `lxcatalog`, `lexkk`, `lextransfer` – unter PostgreSQL alles in Kleinbuchstaben; Schema `f<N>` in der Firmen-DB, `lexglobal` in `lxoffice` | FubarDev-Wiki „Global Databases“, „Entities LxOffice“ |
| Manuelles Kopieren | **[V]** „Nicht möglich mit PostgreSQL. Datenbanken können nicht von Hand kopiert werden.“ (Rechnerwechsel-FAQ, Abschnitt B) | FAQ 000036324 |
| ODBC-Treiber | **[NF]** Ob Lexware 2026 einen PostgreSQL-ODBC-Treiber (psqlODBC „PostgreSQL Unicode“) mitinstalliert, ist nirgends dokumentiert. **[A]** Vermutlich nicht (der Client spricht ADO.NET/Npgsql). Prüfen mit `pyodbc.drivers()` | – |

**Einordnung [A]:** Der PostgreSQL-Zugang ist bewusst gegen Fremdzugriff gehärtet (Zertifikat + TOTP + verschlüsseltes
Passwort). Ein Python-/pyodbc-Zugriff setzt voraus, dass man die Entschlüsselung von `postgresql.dd` und die
Zertifikatsausstellung nachbaut – das ist Reverse Engineering, nicht „ODBC einrichten“. **Für dieses Projekt nicht empfohlen.**
Wenn später doch ein Direktzugriff gewünscht ist: Die MIT-lizenzierte .NET-Bibliothek `FubarDev.Lexware` (Codeberg, Stand
04/2026, Pakete `…Database.Postgres` / `…Database.SqlAnywhere`) kapselt genau das und könnte als kleiner Brücken-Dienst dienen.

### 1.4 „Datenbankzugriff/ODBC in Lexware freischalten“ (Extras → Optionen?)

**[V]** Nicht vorhanden/nicht dokumentiert: Das Handbuch „Lexware warenwirtschaft pro“ (Ausgabe 12/2024, 10.340 Textzeilen) und
das „Technische Handbuch Mehrplatz 2026“ (03/2026) enthalten das Wort „ODBC“ **kein einziges Mal** (Volltextsuche nach Extraktion
mit `pdftotext`). Auch keine Lexware-FAQ beschreibt eine Freischaltung. Das 2026er Handbuch führt übrigens noch „Lexware Datenbank
Server (Dbsrv16.exe)“ in der Firewall-Liste – ein Relikt.

**[V-Dritt]** Drittanbieter (Co-Orga „LX Data Warehouse“, FubarDev) authentifizieren sich **nicht** mit einem DB-Konto, sondern mit
einem **Lexware-Benutzer** (Supervisor oder eigener Benutzer aus `Extras → Benutzerverwaltung`), der „für die gewünschten Firmen
berechtigt“ sein muss und sich „einmalig an jeder Lexware-Firma anmelden“ muss; Programmrechte sind nicht nötig.
(https://docs.lxconnect.de/lxdw/benutzer-in-lexware-anlegen, https://docs.lxconnect.de/lxdw/voraussetzungen)

### 1.5 Haltung des Lexware-Supports

**[V]** Zitate von lexware.de:

* „Die Lexware API-Schnittstelle ist nicht öffentlich zugänglich. Der Zugang zur API-Schnittstelle wird ausschließlich
  autorisierten und zertifizierten Lexware Partnern für die Entwicklung von Lösungen sowie Integrationen zur Verfügung gestellt.“
* „Eigene Anpassungen oder Entwicklungen, die bisher an Lexware-Applikationen angebunden waren, funktionieren nach der
  Umstellung nicht mehr.“ – „Zertifizierte Lexware Partner unterstützen Sie bei der Umstellung.“ (350 Partner, kostenlose
  Erstberatung ca. 15 Minuten, Rückmeldung in der Regel innerhalb von 5 Werktagen)
  (https://www.lexware.de/support/lexware-datenbankumstellung-beratung)
* „Mit der Einführung der neuen Datenbank wird es nicht mehr möglich sein, Zusatzprogramme wie gewohnt anzubinden.“ –
  „Bitte beachten Sie, dass diese Leistungen durch den Partner auf eigene Rechnung erbracht werden.“ (FAQ 000035913)

**[V-Dritt]** Community: Im lex-forum.net (2015) rät ein erfahrener Nutzer, man solle sich „zuerst selbst mit der Datenbank
beschäftigen“, und hält den DB-Zugriff für nicht erlaubt (Meinung, keine Lexware-Aussage). gofilemaker.de: „Although Lexware
does not currently provide a public REST API“.

**Fazit 1:** Direkter DB-Zugriff ist weder verboten dokumentiert noch unterstützt; ab 2026 ist er technisch versperrt.
Export-Funktionen sind der offiziell vorgesehene Weg („Über die Funktion Datei – Export können Sie Ihre Stammdaten exportieren.“).

---

## 2. Tabellen und Spalten der Warenwirtschaft

Offizielle Schemadokumentation existiert nicht. Alle Angaben stammen aus Open-Source-Mappings (NHibernate) und Partner-/Foren-
Quellen für **SQL Anywhere bis 2025**. Das FubarDev-Projekt verwendet dieselben Tabellen- und Spaltennamen auch für PostgreSQL
2026 (Schema-Rewriter `F<N>` → `f<N>`, Namen klein), daher **[A]**: Schema in PostgreSQL inhaltlich gleich, Bezeichner klein.

### 2.1 Datenbanken und Schemas

**[V-Dritt]**

* `LXOFFICE` (SA) / `lxoffice` (PG): Schema `LEXGLOBAL`/`lexglobal`
  * `LXG_FIRMA`: `lID_Firma` (PK), `szName`, `szOwner` (= Name der Firmen-DB, z. B. `F178`), `szPath` (Verzeichnis der Firmen-DB),
    `datErstelltAm`, `szUserErstelltAm`, `datGeaendertAm`, `szUserGeaendertAm` (28 Spalten insgesamt, 4–8 gemappt)
  * `LXG_USER`: `USER_ID` (PK), `USER_NAME`, `PASSWORD` (verschlüsselt), `FULL_NAME` (char(128), trimmen), Gruppen-ID, Flags
  * `lxg_user2company`: `user_id`, `lid_firma` – „Hat ein Benutzer keinen Eintrag in dieser Tabelle, hat er Zugriff auf alle Firmen.“
* Firmen-DBs `F1…Fn` / `f1…fn`: Schema = Firmenkürzel (`F1` bzw. `f1`); ~146 Tabellen mit Präfix `FK_` (Warenwirtschaft/Faktura),
  daneben `LX_*` (Stammdaten wie `LX_KOSTENSTELLE`, `LX_KOSTENTRAEGER`, `LX_BUNDESLAND`, `LX_LAND`) und `BH_*` (Buchhaltung, z. B. `BH_Journal`).
* Einkaufsbelege (Bestellungen) liegen in derselben Tabelle `FK_Auftrag` wie Verkaufsbelege (Unterscheidung über `AuftragsKennung`, s. u.).

Quellen: FubarDev GitHub `FirmaMap.cs`, `UserMap.cs`; FubarDev-Wiki „Entities LxOffice“, „Global Databases“; lex-forum 1336
(„rund 146 Tabellen mit dem Präfix FK_“); Co-Orga Konfiguration.

### 2.2 Tabellenübersicht (Warenwirtschaft)

| Tabelle | Inhalt | Status |
|---|---|---|
| `FK_Artikel` | Artikelstamm | [V-Dritt] FubarDev `ArtikelMap.cs`, Co-Orga |
| `FK_Preisgruppe` | Preisgruppen (1/2/3) | [V-Dritt] FubarDev `PreisgruppeMap.cs` |
| `FK_Preismatrix` | VK-Preise je Artikel × Staffelmenge × Preisgruppe | [V-Dritt] FubarDev `PreismatrixMap.cs`, Co-Orga („Artikelpreise“) |
| `FK_Menge` | Staffelmengen je Artikel (`MengeNr`, `ArtikelNr`) | [A] abgeleitet aus `MengeMap.cs`-Dateiname und `KeyReference(MengeNr, ArtikelNr)` |
| `FK_Kunde` | Kundenstamm | [V-Dritt] FubarDev `KundeMap.cs`, Co-Orga |
| `FK_Lieferant` | Lieferantenstamm | [V-Dritt] nur Tabellenname (Co-Orga); Spalten **[NF]** |
| `FK_Auftrag` | Belegköpfe aller Belegarten (Verkauf und Einkauf) | [V-Dritt] FubarDev `Auftrag.hbm.xml`, lex-forum, Co-Orga |
| `FK_AuftragPos` | Belegpositionen | [V-Dritt] FubarDev `AuftragPosMap.cs`, Co-Orga |
| `FK_Nummernkreis` (FubarDev-Wiki schreibt `FK_Nummerkreis`) | Belegarten-Register (`Kennung`, `Bezeichnung`, `BezeichnungKurz`) | [V-Dritt] lex-forum 1336, FubarDev-Wiki; Schreibweise **[A]** prüfen |
| `FK_Vorgang` | Geschäftsvorfall; `lID`, `lkv_Status` (0 = in Bearbeitung, 1 = angenommen, 2 = abgelehnt) | [V-Dritt] lex-forum 5892 |
| `FK_Vertreter`, `FK_Anschrift`, `FK_Projekt`, `FK_ProjektAdresse` | Vertreter, weitere Adressen (Kopien!), Projekte | [V-Dritt] lex-forum 1336/5892 |
| Warengruppen-Tabelle, Artikel↔Lieferant-Tabelle (EK-Preise, Bestellnr. beim Lieferanten) | **[NF]** Name unbekannt; Export kennt „Warengruppe“, „EK Preis Lf.1/Lf.2“, „Lieferantennummer 1/2“, „Bestellnr. Lf.1/2“ | [A] z. B. `FK_Warengruppe`, `FK_ArtikelLieferant` – reine Vermutung |

### 2.3 Spalten (verifiziert über Mappings)

**`FK_Auftrag`** [V-Dritt] (FubarDev `Auftrag.hbm.xml` + Wiki „Entities LxCompany“, lex-forum):

* Schlüssel: `SheetNr` (Surrogat-PK, global eindeutig), natürlicher Schlüssel `AuftragsNr` + `AuftragsKennung`
* `KundenNr` (Pflicht), `VorgangNr` (→ `FK_Vorgang.lID`), `Datum_erfassung`, `Liefertermin`, `AboBeginn`, `AboEnde`
* Summen: `Summen_netto_haupt`, `Summen_brutto_haupt`, `Summen_gesamt` (+ weitere USt-/Abschlags-Summen)
* Anschrift-Komponente: `Anschrift_Firma`, `Anschrift_Name`, `Anschrift_Strasse`, `Anschrift_HausNr`, `Anschrift_Plz`,
  `Anschrift_Ort`, `Anschrift_Land`, `Anschrift_Email`
* Status-Flags mit Präfix `bStatus_` (Gebucht, Gedruckt, Exportiert, Bezahlt, Storniert)
* Belegkette: `Verweis_weiter_aus_nr`, `Verweis_weiter_in_nr` (Selbstreferenz auf `SheetNr`, 0 = Kettenanfang/-ende) –
  relevant für „Auftrag weiterführen“ (AB → Lieferschein)
* Systemfelder: `System_created`, `System_created_user`, `System_updated`, `System_updated_user`
* **Kundenbestellnummer: [NF]** – keine Quelle nennt die Spalte. Der Export-Assistent enthält sie nicht (nur Stammdaten).
  Prüfauftrag: Spaltenliste von `FK_Auftrag` ermitteln und nach „Bestell“ suchen.
* **Belegart `AuftragsKennung`** [V-Dritt] (Co-Orga „Auftragskennungen“; lex-forum filtert Angebote mit `AuftragsKennung=0`):

  | Wert | Kennung | Belegart | | Wert | Kennung | Belegart |
  |---|---|---|---|---|---|---|
  | 0 | AG | Angebot | | 5 | B | **Bestellung (Einkauf)** |
  | 1 | AB | **Auftragsbestätigung** | | 15 | BA | Bestellanfrage |
  | 2 | LS | **Lieferschein** | | 16 | WE | Wareneingang |
  | 3 | RG | Rechnung | | 17 | RS | Rücksendung |
  | 4 | GS | Rechnungskorrektur | | 18 | ER | Eingangsrechnung |
  | 6 | SR | Sammelrechnung | | 19 | LG | Lieferantengutschrift |
  | 7 | AR | Abschlagsrechnung | | 34 | GT | Gutschrift |
  | 13 | PR | Proformarechnung | | 26 | KV | Kostenvoranschlag |
  | 14 | ST | Stornierung | | 27 | SA | Serviceauftrag |

  **[A]** Abweichung: FubarDev-Wiki nennt Kurzkennungen `LI` (Lieferschein) und `GU` (Gutschrift), Co-Orga `LS`/`GS`. Die
  numerischen Werte sind die verlässlichere Basis; Kürzel aus `FK_Nummernkreis.BezeichnungKurz` am System prüfen.

**`FK_AuftragPos`** [V-Dritt] (FubarDev `AuftragPosMap.cs`): `LNr` (PK), `AuftragsNr`, `AuftragsKennung`, `LfdNr`, `PosNr`, `PosText`,
`ArtikelNr`, `lArtikelID` (→ `FK_Artikel.SheetNr`), `Artikel_Bezeichnung`, `szArtikel_Kurzbezeichnung`, `Artikel_Menge`,
`Artikel_Einheit`, `Artikel_Preisfaktor`, `Summen_preis`, `Summen_rabatt`, `Summen_rabatt_proz`, `Summen_netto`, `Summen_brutto`,
`Summen_ust_proz`, `Summen_ust_gesamt`, `Summen_ust_nach_Aufrab`, `Summen_netto_nach_Aufrab`, `Summen_brutto_nach_Aufrab`,
`bMitAuftragsrabatt`, `dftArtikelpreisNetto`, `dftArtikelpreisBrutto`, `fPreisManuell`, `lPreisArt`, `lPreisID`, `System_*`.

**`FK_Artikel`** [V-Dritt] (FubarDev `ArtikelMap.cs`, nur Teilmapping): `SheetNr` (PK), `ArtikelNr` (natürlicher Schlüssel),
`Bezeichnung`, `Beschreibung` (CLOB), `System_*`. **[NF]**: Spalten für Warengruppe, Einheit, Matchcode, Gesperrt/Sperrgrund,
Lieferant 1/2, EK-Preis – im Export vorhanden („Warengruppe“, „Einheit“, „Matchcode“, „Gesperrt“, „Sperrgrund“, „EK Preis Lf.1“,
„Lieferantennummer 1“), DB-Spaltennamen unbekannt.

**`FK_Preisgruppe`** [V-Dritt]: `PreisgrpNr` (PK), `Preisgrp` (Name, 20 Zeichen), `System_*`.
**`FK_Preismatrix`** [V-Dritt]: zusammengesetzter Schlüssel `MengeNr` + `ArtikelNr` (→ Staffelmenge) + `PreisgrpNr`; `Vk_preis`, `Vk_preis_eur`, `System_*`.
→ **[A]** „VK Preisgruppe 1“ = `Vk_preis` der Zeile mit Preisgruppe 1 und der *ersten* Staffelmenge; welcher `MengeNr`-Wert die
Grundstaffel ist, muss am System geprüft werden.

**`FK_Kunde`** [V-Dritt] (FubarDev `KundeMap.cs`): `SheetNr` (PK), `KundenNr`, `Anschrift_Firma`, `Anschrift_Anrede`, `Anschrift_Vorname`,
`Anschrift_Name`, `Anschrift_Zusatz`, `Anschrift_Ansprech`, `Anschrift_Strasse`, `Anschrift_HausNr`, `Anschrift_Plz`, `Anschrift_Ort`,
`Anschrift_Email`, `bInaktiv`, `szUserdefined1…6`, `lBDSG_Status`, `lBDSGTextID`, `datBDSG`, `System_*`.
**[NF]**: Spalte für `Preisgruppe` (Export: „Preisgruppe (Ganze Zahl, nur 1, 2, 3)“), `Anschrift_Land`, Matchcode.

**`FK_Lieferant`** [NF]: nur Existenz belegt. **[A]** analog zu `FK_Kunde` (`LieferantenNr`, `Anschrift_*`, `bInaktiv`) – prüfen.

### 2.4 Hinweise zur Semantik [V-Dritt]

* `FK_Anschrift` enthält **Kopien** der Adressdaten, keine Verknüpfungen (lex-forum 1336). Belege tragen ebenfalls eigene
  `Anschrift_*`-Kopien → für Dublettenprüfung immer `KundenNr` verwenden, nicht Namen.
* Belegstatus (Angebot angenommen/abgelehnt) steht in `FK_Vorgang.lkv_Status`, nicht in `FK_Auftrag`.
* Datentypen SA: `char(128)` mit Padding; Datumsfelder als eigener Typ (`LxDateOnly`/`LocalDateType` im Mapping).

---

## 3. CSV-/ASCII-Export aus Lexware

### 3.1 Datei → Export (Assistent) [V]

Quellen: FAQ 000012126 („Der Export von Artikeln und Warengruppen aus Lexware“), FAQ 000012132 („Der Export von Kunden- und
Lieferantenstammdaten“), Handbuch warenwirtschaft pro 2024 (S. 40 „Daten exportieren“, S. 48 „Im-/Export“), FAQ 000036502.

* Menü: **`Datei → Export → ASCII`** – „Über die Funktion ‚Datei – Export – ASCII‘ stellen Sie Ihre Daten als *.txt-Datei für
  andere Systeme bereit.“
* **Datenquellen:** Buchungsdaten (Ausgangsrechnungen für die Fibu; zusätzlich DATEV), **Kunden/Debitoren**, **Artikel**,
  **Lieferanten/Kreditoren**, Kunden-/Lieferantenadressen für MS Outlook, Steuerdatei für Word-Serienbrief.
  „Jeder Export ist auf eine Datenquelle beschränkt.“ Handbuch: „Zur Übergabe von Kunden-, Lieferanten- und Artikeldaten in
  andere Programme … können Sie die entsprechenden Daten im ASCII- bzw. ANSI-Format in einer Datei abspeichern.“
* **Aufträge/Belege sind keine Datenquelle** des Export-Assistenten (weder FAQ noch Handbuch noch Schnittstellenbeschreibung nennen
  sie). Importseitig: „Ein Import von Bewegungsdaten, wie z.B. Aufträge, ist garnicht möglich.“ (FAQ 000036502)
* **Dateiart:** „ASCII (MS-DOS)“ oder **„ANSI (Windows)“ (Standard, empfohlen)**. **[A]** „ANSI (Windows)“ = Windows-1252
  (passt zu FubarDev „Lexware nutzt Windows-1252“ und zur Projekt-Konfiguration `kodierung = "cp1252"`).
* **Trennzeichen:** Standard **Semikolon**, Textkennzeichen **doppeltes Anführungszeichen**; frei einstellbar.
* **Option „Feldnamen in den ersten Datensatz“** → Kopfzeile mit Feldbezeichnungen (für `spalten_*`-Zuordnung in `CsvQuellen` ideal).
* **Feldauswahl:** standardmäßig alle Felder; Eingrenzung nach Nummernbereich oder „nur geänderte bzw. neue“ Datensätze
  (Kunden: optional Datumsbereich).
* **Profile:** „Die Export-Einstellungen lassen sich als Profil speichern und für spätere Exporte wiederverwenden.“
* **Einschränkung:** „Angelegte Kontaktpersonen eines Kundendatensatzes werden im Export nicht berücksichtigt.“
* **Automatisierung:** **[NF]** Kein Kommandozeilen-/Batch-Aufruf dokumentiert. **[A]** Nur per UI-Automation (pywinauto:
  Assistent mit gespeichertem Profil durchklicken) oder manuell/periodisch.

### 3.2 Feldlisten laut „Schnittstellenbeschreibung.pdf“ [V]

Quelle: Lexware-Download aus FAQ 000012126 (PDF, 11 Seiten, erstellt 04.09.2018; Feldnamen = Spaltenköpfe im Export).
Für das Projekt relevante Felder fett.

**Artikel:** **Artikelnummer** (alnum, max. 20; Nummernkreis: 10 numerisch), **Bezeichnung** (Kurztext, 100), Kurzbezeichnung (20, nur
Kassenfunktion premium), **Warengruppe** (60, „Bezeichnet den Namen der Warengruppe, die dem Artikel zugeordnet ist“), Warengr.-Kurzbez. (20),
**Einheit** (20), Gewicht, **Matchcode** (35), Preis pro Anzahl, Warengr.-Konto Inland/Ausland/EG/Bau, Warengr.-KontoAW Inland/Ausland/EG/Bau,
**Preis 1**, Preis 2, Preis 3, Preis I/1…I/3, II/1…II/3, III/1…III/3 (Staffelpreise je Preisgruppe), B/N (J/N = Brutto/Netto), Lagerartikel (J/N),
**EK Preis Lf.1**, EK Preis Lf.2, Staffelmenge 1–3, **Lieferantennummer 1**, Lieferantennummer 2, Bestellmenge Lf.1/2, Bestellnr. Lf.1/2 (30),
Lieferzeit Lf.1/2, EK Preis pro Lf.1/2, Rohgewinn (J/N), **Gesperrt (J/N)**, Sperrgrund (50), Lagerbestand, Mindestbestand, Lagerort (200),
Bestellte Menge, Stückliste (J/N), Internet (J/N), Text (5000), Benutzerdefiniert 1–3 (Freifelder, 50), Reserviert (J/N), Hauptbarcode (30).

**Kunden:** **Kundennummer** (20 / 10 numerisch), Debitorenkonto, Anrede (20), **Name** (50), **Matchcode** (35), Vorname (50), **Firma** (60),
**Zusatz** (60), Ansprechpartner (60), **Straße** (35), HausNr (5), **Postleitzahl** (10), **Ort** (35), **Land** (35), Telefon/Telfon 2/Telefon 3/Telefax (20),
EMAIL (60), Web (60), Lief-Nr beim Kunden (Lieferanschrift), Liefer.-Felder (Anrede, Zusatz, Ansprechpartner, Straße, HausNr, PLZ, Ort, Land, Lieferart,
Telefon…, Firma, Name, Vorname, Fax, Email, Web), **Preisgruppe (Ganze Zahl, nur 1, 2, 3)**, Rabatt, EG ID, steuerbare Umsätze (J/N),
Zahlungsbedingung, Zahlungsziel, Skontotage, Skontosatz, Zahlbed.Text Rechnung, Zahlbed.Text, Skontotage_2, Skontosatz_2, Bankleitzahl, Bankkonto,
Bankbezeichnung, Zahlart (0–5), Einzug (J/N), Bemerkung (1000), Kreditbetrag, Lieferstopp (J/N), Mahnen zulassen (J/N), Kostenträger, Kostenstelle,
Benutzerdefiniert 1–3, IBAN, BIC, **Inaktiv (J/N)**, Abfrage nach § 13b UStG, Abweichender Kontoinhaber (+Name), EU Standardüberweisung,
Steuerbefreiung, Anschrift Lieferart, Liefer Nr. beim Kunden (Anschrift), Anschrift Mobil Tel., Anschrift Email2, Liefer Mobil Tel., Liefer Email2,
Kundengruppen, Vertreter, Branchen, eRechnung (J/N), Abweichende Email (J/N), Abweichende Email Adresse, Lettershop (J/N), Lettershop Farbe, **Länderkürzel** (2).

**Lieferanten:** **Lieferantennummer** (20 / 10 numerisch), Kreditorenkonto, **Matchcode** (35), Anrede, **Name** (50), Vorname, **Firma** (60), **Zusatz** (60),
Ansprechpartner, Straße, HausNr, Postleitzahl, **Ort**, Land, Telefon 1–3, Telefax, E-Mail, Kund-Nr beim Lieferanten (20), Web, Liefer.-Felder (wie Kunden),
Lieferart, Rabatt, EG ID, steuerbare Umsätze, Zahlungsbedingung, Zahlungsziel, Skontotage, Skontosatz, Zahlbed. Text Rechnung, Zahlbed. Text,
Skontotage_2, Skontosatz_2, Bankleitzahl, Bankkonto, Bankbezeichnung, Zahlart (0–6), Einzug, Bemerkung (1000), Kreditbetrag, Einkaufspreis Brutto (J/N),
Kostenträger, Kostenstelle, Benutzerdefiniert 1–3, IBAN, BIC, **Inaktiv (J/N)**, Abfrage nach § 13b UStG, Abweichender Kontoinhaber (+Name),
EU Standardüberweisung, Steuerbefreiung, Steuernummer.

Formate laut PDF: Zahlen als „Gleitkommazahl“ (Beispiele mit Komma: „2,00“, „1500,15“ → deutsche Dezimalschreibweise), Ja/Nein als `J`/`N`.

**[A]** Die PDF ist von 2018; die Feldliste in Version 2026 kann erweitert sein (z. B. eRechnung ist schon enthalten). Die tatsächliche
Kopfzeile des Exports ist maßgeblich → Projekt-Konfiguration `spalten_artikel/-kunden/-lieferanten` entsprechend füllen.

### 3.3 Belege/Aufträge exportieren (Bildschirmlisten) [V]

Handbuch warenwirtschaft pro 2024, S. 28/29 und S. 122:

* Jede Bildschirmliste (`Verwaltung → Aufträge`, `Kunden`, `Artikel`, `Lieferanten`) bietet in der Symbolleiste **Exportieren →
  „Ansicht nach MS® Excel exportieren“ / „Ansicht ins pdf-Format exportieren“ / „Ansicht nach xml exportieren“**.
  „Beinahe alle Felder, die in der Stammdatenerfassung vorhanden sind, können als Spalte in die Bildschirmliste aufgenommen werden“;
  Spaltenauswahl wird gespeichert.
* Kontextmenü der Auftragsliste: „Export nach MS Excel“, „Exportdatei für UPS/GLS/DHL erstellen“ (markierte Datensätze).
* **[A]** Der Excel-Export öffnet Excel per COM (Handbuch: „Anschließend wird das Programm MS® Excel geöffnet“) – nur mit
  installiertem Excel, schlecht automatisierbar. Der **XML-Export der Ansicht** schreibt eine Datei und ist der aussichtsreichste
  Weg für die Dublettenprüfung (Belegnummer, Kundenbestellnummer, Kundennummer, Datum, Netto, Status). **Format der XML-Datei [NF]**
  → Beispieldatei vom Anwender nötig.
* Kundenbestellnummer in der Auftragsliste: **[NF]** ob sie als Spalte einblendbar ist → prüfen.

### 3.4 Bewertung: ODBC vs. Export für „nur lesen“

| Kriterium | ODBC/Direkt-SQL | Export (ASCII + Listen-XML/Excel) |
|---|---|---|
| Verfügbar bei PostgreSQL (2026) | praktisch nein (Zertifikat/TOTP/verschlüsseltes Passwort) | ja |
| Verfügbar bei SQL Anywhere (≤2025) | ja, inoffiziell; Login-Frage ungeklärt | ja |
| Vom Hersteller gestützt | nein | ja („Über die Funktion Datei – Export können Sie Ihre Stammdaten exportieren.“) |
| Schema-/Updatefestigkeit | gering (Hash-/Schemaänderungen haben FubarDev 2022 lahmgelegt) | hoch (Feldnamen seit 2018 stabil) |
| Aktualität | live | Stand des letzten Exports (Stammdaten ändern sich selten; Belege für Dubletten täglich exportieren) |
| Belege/Kundenbestellnr. | möglich (`FK_Auftrag`, Spalte unbekannt) | nur über Listen-Export |
| Risiko für Lexware-Daten | gering bei `SELECT`, aber Sperren/Timeouts möglich | keins |
| Aufwand | hoch | gering (Profile), Automatisierung per pywinauto |

**Empfehlung:** Standardweg **CSV-Export** (`StammdatenKonfig.typ = "csv"`): pro Firma Artikel/Kunden/Lieferanten mit Kopfzeile, Semikolon,
ANSI; Belege über `Verwaltung → Aufträge → Ansicht nach xml exportieren` (Parser noch zu bauen, sobald Beispieldatei vorliegt) oder als
Übergang: Dublettenprüfung direkt in der Lexware-UI (Suche nach Kundenbestellnummer) vor dem Buchen. ODBC nur als **optionaler Pfad, falls
`Info → Details` „SQL Anywhere“ zeigt** – und selbst dann nur mit `readonly=True`, nie produktiv ohne Rücksprache mit Lexware-Partner.

---

## 4. Risiken

* **Sperren/Verfügbarkeit [V]:** „Während der Datensicherung ist kein Zugriff auf die gemeinsame Datenbank möglich. Es darf sich in dieser
  Zeit kein Benutzer an der Datenbank anmelden.“ (Techn. Handbuch 2026, Kapitel Datensicherung, Sybase *und* Postgres). Co-Orga-Modus
  „Automatik“ verlangt, dass „alle Benutzer die Lexware vorübergehend schließen“. **[A]** Reine `SELECT`s im laufenden Betrieb sind
  bei beiden DB-Systemen unkritisch (MVCC), lange Abfragen können aber Backups/Updates verzögern.
* **Nur ein DB-Server [V]:** „Zwei Lexware-Datenbankserver dürfen nicht gleichzeitig laufen“ (FAQ 000003321) – keine eigene DB-Instanz
  der Lexware-Datenbank starten (gilt für SA; bei PG ohnehin nicht kopierbar).
* **Mandanten [V/V-Dritt]:** Eine Datenbank je Firma (`F1…Fn` bzw. `f1…fn`), Zuordnung über `LXOFFICE.LEXGLOBAL.LXG_FIRMA`
  (`szName`, `szOwner`, `szPath`). Vier Firmen ⇒ vier Firmen-DBs + `lxoffice`. In der UI: `Datei → Wechseln` – „Alle noch aktiven Fenster der
  zuvor geöffneten Firma werden automatisch geschlossen.“ Exporte sind **immer firmenbezogen** (Export in der jeweils aktiven Firma).
  Lexware-Benutzer brauchen je Firma eine Berechtigung (`lxg_user2company`; ohne Eintrag = alle Firmen).
* **Lizenz/Recht [V]:** API nur für zertifizierte Partner; Direktzugriff nicht gestützt; Support leitet an Partner „auf eigene Rechnung“.
  **[V-Dritt]** FubarDev beruft sich auf Interoperabilitäts-Ausnahmen des deutschen Urheber- und Geschäftsgeheimnisrechts und betont:
  nur für lizenzierte Nutzer zum Abruf eigener Daten. Die SQL-Anywhere-Client-DLLs unterliegen der **SAP-Lizenz** (nicht weiterverteilbar).
  **[A]** Ob der mit Lexware installierte ODBC-Treiber „SQL Anywhere 17“ für Fremdanwendungen genutzt werden darf, ist lizenzrechtlich ungeklärt.
* **Schema-/Versionsrisiko [V-Dritt]:** Keine offizielle Doku; Hash-Verfahren wechselten (V0–V5); 2026 komplett neues Backend; Juli-2026-Update
  26.51/26.52 verursachte bei Premium-Kunden mit PostgreSQL Startprobleme (borncity 07/2026, Einzelbericht).
* **Datenschutz [V]:** Handbuch: „Beachten Sie beim Export von personenbezogenen Daten die Datenschutzgrundverordnung.“ → Exportdateien
  nicht ins Repo, nur lokal; Fixtures bleiben synthetisch.

---

## 5. Beispiel-Connection-Strings (alle **[A]**, am System zu prüfen)

### 5.1 SQL Anywhere 17 (nur wenn `Info → Details` = „SQL Anywhere“)

Treibername laut SAP-Forum: `SQL Anywhere 17`; Schlüssel `ENG`/`ServerName`, `DBN`/`DatabaseName`, `UID`, `PWD`, `CommLinks`
(FubarDev baut `ServerName=lxdbsrv; CommLinks=TCPIP(Host=…); DatabaseName=<szOwner>; DatabaseFile=…`).

```text
# Variante A: vorhandene System-DSN von Lexware (32-Bit-ODBC!)
DSN=LXSYDSN;DatabaseName=F1;UID=<lexware-db-benutzer>;PWD=<passwort>

# Variante B: DSN-los über den Treiber
Driver={SQL Anywhere 17};ServerName=lxdbsrv;CommLinks=TCPIP(Host=<server>,Port=2638);DatabaseName=F1;UID=<benutzer>;PWD=<passwort>
```

Python (`pyodbc`, nur lesend):

```python
import pyodbc
cn = pyodbc.connect(verbindung, readonly=True, autocommit=False, timeout=10)
cn.setdecoding(pyodbc.SQL_CHAR, encoding="cp1252")   # Lexware: Windows-1252
```

Offen **[NF]**: Welcher Login funktioniert. `U{userId}`-Benutzer haben verschlüsselte Lexware-Passwörter; ob das Lexware-Kennwort
direkt als DB-Passwort gilt, ist unbelegt. Bitness: Lexware-Client und DSN sind 32-Bit → 32-Bit-Python oder eigener 64-Bit-Treiber nötig.

### 5.2 PostgreSQL (Version 2026) – **nicht empfohlen**, nur zur Dokumentation

```text
Driver={PostgreSQL Unicode(x64)};Server=<server>;Port=15432;Database=f1;UID=garinn;PWD=<Serverpasswort aus Daten\postgresql.dd, verschlüsselt>;
sslmode=verify-full;pqopt={sslcert=<client.crt> sslkey=<client.key> sslrootcert=<root.pem>}
```

Hürden: Passwort nur verschlüsselt vorhanden; Clientzertifikat nur über LexwareService (14072) mit TOTP aus `pin2.bin`; `pg_hba.conf` des
Lexware-Clusters unbekannt (**[NF]**). `pqopt={…}` für libpq-Parameter laut psqlODBC-Maintainer (postgresql.org-Mailingliste).

---

## 6. Beispiel-SELECTs

Konvention: Spalten in **GROSS** = verifiziert über Mappings/Foren [V-Dritt]; `/* TODO-LEXWARE */` = unbekannt [NF/A]. Für SQL Anywhere
Schema `F1.` voranstellen oder als Owner verbinden; für PostgreSQL alles klein (`f1.fk_artikel`). Immer nur `SELECT`.

```sql
-- Artikel (Firma 1)  -- Status: Grundspalten [V-Dritt], Preis/Warengruppe/Lieferant [A]
SELECT a.ArtikelNr, a.Bezeichnung, a.Beschreibung,
       /* TODO-LEXWARE: Spaltennamen prüfen */ a.Matchcode, a.Einheit, a.Gesperrt, a.Warengruppe,
       pm.Vk_preis            AS vk_preis_1
FROM   FK_Artikel a
LEFT JOIN FK_Preisgruppe pg ON pg.Preisgrp = 'Preisgruppe 1'          /* TODO-LEXWARE: Name/Nr. der PG 1 */
LEFT JOIN FK_Preismatrix pm ON pm.ArtikelNr = a.ArtikelNr
                           AND pm.PreisgrpNr = pg.PreisgrpNr
                           AND pm.MengeNr = /* TODO-LEXWARE: Grundstaffel */ 0;

-- Kunden  -- Status: [V-Dritt] bis auf Preisgruppe/Land
SELECT k.KundenNr, k.Anschrift_Firma, k.Anschrift_Name, k.Anschrift_Vorname, k.Anschrift_Zusatz,
       k.Anschrift_Strasse, k.Anschrift_HausNr, k.Anschrift_Plz, k.Anschrift_Ort,
       /* TODO-LEXWARE */ k.Anschrift_Land, /* TODO-LEXWARE */ k.PreisgrpNr AS preisgruppe,
       k.bInaktiv
FROM   FK_Kunde k;

-- Lieferanten  -- Status: Tabelle [V-Dritt], Spalten [A]; zuerst Struktur ansehen:
SELECT TOP 5 * FROM FK_Lieferant;              -- SQL Anywhere
-- SELECT * FROM f1.fk_lieferant LIMIT 5;      -- PostgreSQL

-- Belege für die Dublettenprüfung (AB = 1, Bestellung = 5, Lieferschein = 2)  -- Status: [V-Dritt] außer Kundenbestellnr.
SELECT a.SheetNr, a.AuftragsNr, a.AuftragsKennung, n.BezeichnungKurz AS belegart,
       a.KundenNr, a.Datum_erfassung, a.Summen_netto_haupt, a.Summen_gesamt,
       /* TODO-LEXWARE: Spalte für Kundenbestellnummer */ a.Bestellnummer,
       a.bStatus_Storniert, a.Verweis_weiter_in_nr, a.Verweis_weiter_aus_nr
FROM   FK_Auftrag a
LEFT JOIN FK_Nummernkreis n ON n.Kennung = a.AuftragsKennung      /* TODO-LEXWARE: FK_Nummerkreis? */
WHERE  a.AuftragsKennung IN (1, 2, 5)
  AND  a.Datum_erfassung >= CURRENT DATE - 365;

-- Positionen eines Belegs
SELECT p.LfdNr, p.PosNr, p.ArtikelNr, p.Artikel_Bezeichnung, p.Artikel_Menge, p.Artikel_Einheit,
       p.Summen_preis, p.Summen_netto, p.Summen_brutto, p.Summen_ust_proz
FROM   FK_AuftragPos p
WHERE  p.AuftragsNr = ? AND p.AuftragsKennung = ?
ORDER  BY p.LfdNr;

-- Firmenliste (lxoffice)
SELECT lID_Firma, szName, szOwner, szPath FROM LEXGLOBAL.LXG_FIRMA;   -- PG: lexglobal.lxg_firma
```

### 6.1 Tabellenliste selbst ermitteln (für `erkunde_schema`)

Treiberunabhängig über ODBC-Katalogfunktionen (empfohlen, funktioniert mit jedem Treiber):

```python
cur = cn.cursor()
tabellen = [r.table_name for r in cur.tables(tableType="TABLE") if r.table_name.upper().startswith("FK_")]
spalten  = [(r.column_name, r.type_name, r.column_size) for r in cur.columns(table="FK_Auftrag")]
```

SQL Anywhere 17 (Systemkatalog):

```sql
SELECT u.user_name AS owner, t.table_name, c.column_name, d.domain_name, c.width
FROM   SYS.SYSTAB t
JOIN   SYS.SYSTABCOL c ON c.table_id = t.table_id
JOIN   SYS.SYSDOMAIN d ON d.domain_id = c.domain_id
JOIN   SYS.SYSUSER   u ON u.user_id   = t.creator
WHERE  t.table_name LIKE 'FK\_%' ESCAPE '\'
ORDER  BY t.table_name, c.column_id;
```

PostgreSQL:

```sql
SELECT table_schema, table_name, column_name, data_type
FROM   information_schema.columns
WHERE  table_name ILIKE 'fk\_%'
ORDER  BY table_schema, table_name, ordinal_position;
```

---

## 7. Checkliste für den Anwender (am Lexware-Rechner)

Bitte Ergebnisse als Screenshots/Textdateien ablegen (ohne Kundendaten ins Repo!):

1. **Datenbank feststellen:** `? → Info…` → `Details` → letzte Zeile der Modulliste (`SQL Anywhere` oder `PostgreSQL`) **und** Eintrag
   „Datenbank“ (Versionsnummer). Screenshot. Außerdem im Info-Dialog den Link **„Daten-Pfad“** anklicken und den Pfad notieren.
2. **PropertyStore:** Im Daten-Pfad `…\Daten\PropertyStore\` die Dateien `dsn.ps`, `PostgresPort.ps`, `ServicePort.ps`, `PostgresAddress.ps`
   im Texteditor öffnen und Inhalte notieren (Port = 3. Zeile zwischen `<value>` und `</value>`). Existieren `C:\ProgramData\Lexware\LexwarePG`
   und `C:\Programme\Lexware\PostgreSql`? Welche Dateien/Ordner liegen in `C:\ProgramData\Lexware\premium\Datenbank` (`F1…`, `LXOFFICE.DB`)?
3. **Dienste:** `services.msc` → alle Dienste mit „Lexware“ im Namen (z. B. „Lexware Premium Datenbank“, „Lexware_postgresql_pro“,
   „Lexware Postgresql Monitor“) mit Status. Screenshot.
4. **ODBC-Bestand:** `%windir%\SysWOW64\odbcad32.exe` (32-Bit) **und** `odbcad32.exe` (64-Bit): Registerkarte „Treiber“ (gibt es „SQL Anywhere 17“?
   „PostgreSQL Unicode“?) und „System-DSN“ (gibt es `LXSYDSN`?). Alternativ in Python: `import pyodbc; print(pyodbc.drivers()); print(pyodbc.dataSources())`.
5. **Installationsart:** Einzelplatz oder Server/Client? Welcher Rechner ist Server, läuft der Automat auf demselben Rechner?
6. **PostgreSQL-Version (falls PG):** `"C:\Programme\Lexware\PostgreSql\bin\postgres.exe" --version` in der Eingabeaufforderung.
7. **Export-Assistent:** `Datei → Export → ASCII` öffnen; Screenshot der **Liste der Datenquellen** (steht dort irgendwo „Aufträge“/„Belege“?).
   Dann **je Firma** (`Datei → Wechseln`) exportieren: Artikel, Kunden, Lieferanten – Einstellungen: „ANSI (Windows)“, alle Felder,
   Semikolon, Textkennzeichen `"`, **„Feldnamen in den ersten Datensatz“ an**, Profil speichern („Automat-Artikel“ usw.).
   Dateien nach `eingang_stammdaten/<firma>/` (lokal, nicht ins Git). Prüfen, ob die Kopfzeile die Felder *Warengruppe, Einheit, Preis 1,
   EK Preis Lf.1, Lieferantennummer 1, Gesperrt, Inaktiv, Matchcode, Preisgruppe* enthält.
8. **Auftragsliste:** `Verwaltung → Aufträge` → Spalten einblenden (Auftragsnummer, Belegart, Kundennummer, Kunde, **Kundenbestellnummer/
   „Ihre Bestellnummer“**, Datum, Netto, Status). Dann `Exportieren → Ansicht nach xml exportieren` **und** `… nach MS Excel exportieren`
   mit ca. 5 Belegen → beide Dateien liefern (Testdaten anonymisieren oder nur Musterfirma). Gibt es die Kundenbestellnummer als Spalte? Screenshot.
9. **Firmen:** `Datei → Wechseln` → Namen der vier Firmen und Reihenfolge (Screenshot). Falls SQL Anywhere: Ordnernamen `F…` im Datenbank-Ordner
   den Firmen zuordnen (Datum/Größe) – oder später per `LXG_FIRMA`.
10. **Menüs auf „Datenbank/ODBC/Schnittstelle“ prüfen:** `Extras → Optionen`, `Verwaltung → Einstellungen`, `Extras → Benutzerverwaltung`
    durchsehen und bestätigen, dass es **keinen** Punkt „Datenbankzugriff/ODBC“ gibt (Screenshots der Optionsseiten).
11. **Benutzer:** In `Extras → Benutzerverwaltung` prüfen, ob ein eigener Lese-Benutzer („Automat“) angelegt werden kann und für alle vier
    Firmen berechtigt wird (für einen eventuellen späteren Partner-/Bibliothekszugriff).
12. **Nur bei SQL Anywhere:** `auftragsautomat odbc-erkunden --firma <id>` mit DSN `LXSYDSN` ausführen (fragt Login ab, nur Katalog lesen);
    Ausgabe der Tabellen-/Spaltenliste von `FK_Auftrag`, `FK_Artikel`, `FK_Kunde`, `FK_Lieferant`, `FK_Preismatrix`, `FK_Nummernkreis` liefern.
    Dabei insbesondere nach einer Spalte für die **Kundenbestellnummer** und die **Kunden-Preisgruppe** suchen.
13. **Lexware/Partner fragen** (Hotline oder Partnerformular „Datenbankumstellung“): (a) Gibt es für Version 2026 eine offizielle Lese-
    Schnittstelle oder Freigabe für Auswertungen (z. B. ODBC-Read-Only, Export-Automatisierung)? (b) Aktuelle „Schnittstellenbeschreibung“ für 2026?
    (c) Ist die Shop-/openTRANS-Schnittstelle in der Lizenz enthalten? (d) Wie wird ein Auftragsexport (Belegliste) offiziell empfohlen?
14. **Belegmaske:** Im Auftrags-Assistenten Screenshot der Seite mit der Kundenbestellnummer („Bestell-Nr.“/„Ihre Bestellnummer“) und der
    Belegfelder (Spedition, Weingut, Shipment, Kunde) – hilft, Export-Spalten und spätere DB-Spalten zuzuordnen.

---

## 8. Offene Fragen an den Anwender (Kurzfassung)

* Zeigt `Info → Details` „SQL Anywhere“ oder „PostgreSQL“? Welche Datenbank-Version?
* Einzelplatz oder Server? Läuft der Automat auf dem Server?
* Welche ODBC-Treiber/DSNs sind installiert (32/64 Bit)?
* Bietet `Datei → Export` in 2026 zusätzliche Datenquellen (Aufträge)? Enthält der Artikel-/Kundenexport die erwarteten Felder?
* Wie sieht der XML-Export der Auftragsliste aus (Beispieldatei)? Ist die Kundenbestellnummer als Spalte wählbar?
* Namen/Reihenfolge der vier Firmen; wo steht die Kundenbestellnummer in der Belegmaske?
* Antwort von Lexware/Partner zu offizieller Leseschnittstelle und openTRANS-Lizenz.

---

## 9. Quellen

**Lexware (offiziell)**

* Datenbankumstellung – Beratung: https://www.lexware.de/support/lexware-datenbankumstellung-beratung
* FAQ 000035913 Zugriff von Drittanbieter-Zusatzprogrammen auf die Datenbank: https://www.lexware.de/support/faq/faq-beitrag/000035913/
* FAQ 000036493 Neue Datenbank ab 01.01.2026, Prüfung über Info → Details: https://www.lexware.de/support/faq/faq-beitrag/000036493
* FAQ 000036354 Version 2026: Client-Server-Installationen (PostgreSQL): https://www.lexware.de/support/faq/faq-beitrag/000036354/
* FAQ 000036320 Version 2026: Programm mit PostgreSQL-Datenbank bereinigen (PropertyStore-Dateien): https://www.lexware.de/support/faq/faq-beitrag/000036320/
* FAQ 000036324 Version 2026: Rechnerwechsel (SQL Anywhere vs. PostgreSQL, Dateien F1…, LxOffice.db): https://www.lexware.de/support/faq/faq-beitrag/000036324/
* FAQ 000003321 Datenbankserver nicht gestartet (Dienstname, dbsrv17.exe, Port 2638, LXSYDSN, odbcad32): https://www.lexware.de/support/faq/faq-beitrag/000003321
* FAQ 000003479 Keine Rücksicherung möglich (Datenbank-Verzeichnis, LXOFFICE.DB, F1): https://www.lexware.de/support/faq/faq-beitrag/000003479/
* FAQ 000035823 Sybase-Versionskonflikt (SQL Anywhere 17 Pfade): https://www.lexware.de/support/faq/faq-beitrag/000035823/
* FAQ 000003245 Firewall-Freigaben (Ports 15432/14072, 2638/62622, PostgresPort.ps): https://www.lexware.de/support/faq/produkt/business-plus/faq-beitrag/000003245-firewall-benoetigte-freigaben-fuer-die-kommunikation-per-internet
* FAQ 000012126 Export von Artikeln und Warengruppen (ASCII, Schnittstellenbeschreibung-Download): https://www.lexware.de/support/faq/faq-beitrag/000012126/
* FAQ 000012132 Export von Kunden-/Lieferantenstammdaten (Datenquellen, Semikolon, ANSI): https://www.lexware.de/support/faq/faq-beitrag/000012132
* FAQ 000036502 Export-/Import-Funktionen Warenwirtschaft (kein Import von Aufträgen): https://www.lexware.de/support/faq/faq-beitrag/000036502/
* Schnittstellenbeschreibung.pdf (Export-/Importfelder, 2018): https://tsp.haufe-lexware.com/sf_get_wmattachment.php?att=c3a8aab1c91e383daa0c7dd295873e
* Technisches Handbuch Mehrplatz 2026 (Dienste, Datensicherung): https://lex-blog.de/wp-content/uploads/2026/03/20260103_handbuch_technik_mehrplatz.pdf
* Handbuch Lexware warenwirtschaft pro (12/2024; Export, Bildschirmlisten, Firma wechseln): https://lex-blog.de/wp-content/uploads/2024/12/handbuch_warenwirtschaft_pro.pdf

**Open Source / Drittanbieter**

* FubarDev.Lexware (Codeberg, 2026, MIT; Wiki: Database Backends, Configuration, Global Databases, Entities LxCompany/LxOffice, Architecture, Getting Started, Common Use Cases): https://codeberg.org/fubar-dev/FubarDev.Lexware
* FubarDevelopment/Lexware (GitHub, archiviert 2022; Mappings `Auftrag.hbm.xml`, `AuftragPosMap.cs`, `ArtikelMap.cs`, `KundeMap.cs`, `PreismatrixMap.cs`, `PreisgruppeMap.cs`, `FirmaMap.cs`, `UserMap.cs`, `SybaseWindowsConfigurationProvider.cs`): https://github.com/FubarDevelopment/Lexware
* Co-Orga LX Data Warehouse – Konfiguration (Tabellenliste), Voraussetzungen, Benutzer anlegen, Auftragskennungen: https://docs.lxconnect.de/lxdw/konfiguration , https://docs.lxconnect.de/lxdw/voraussetzungen , https://docs.lxconnect.de/lxdw/benutzer-in-lexware-anlegen , https://docs.lxconnect.de/lxdw/auftragskennungen
* lex-forum.net „Zugriff auf Lex-DB per ODBC“ (FK_Auftrag, FK_Vorgang.lkv_Status): https://lex-forum.net/t/zugriff-auf-lex-db-per-odbc/5892
* lex-forum.net „Datenbank Schnittstelle nutzen…“ (146 FK_-Tabellen, FK_Nummernkreis, FK_Anschrift): https://lex-forum.net/t/datenbank-schnittstelle-nutzen-um-projekt-details-anzuzeigen/1336
* Born City 2018 (Fehlerlog mit LXSYDSN, LXDBSRV, DBFile F178\LXCOMPANY, Login U0): https://borncity.com/blog/2018/06/14/lexware-juni-2018-update-fhrt-zu-fehler-fm000205a8/
* Born City 07/2026 (Premium-Update 26.51/26.52, PostgreSQL-Probleme; nur Snippet gelesen): https://borncity.com/blog/2026/07/14/lexware-premium-braucht-juli-2026-update-was-zurueckgezogen-wurde/
* Xentral „Export from Lexware ERP Premium“ (FK_-Tabellen; Seite blockiert Abruf, nur Suchsnippet): https://help.xentral.com/hc/en-us/articles/360017382240-Export-from-Lexware-ERP-Premium
* gofilemaker.de (keine öffentliche REST-API): https://gofilemaker.de/en/filemaker/tips-instructions/connect-filemaker-with-lexware/
* SAP SQL Anywhere Forum – pyodbc/SA17 Connection-String: https://sqlanywhere-forum.sap.com/questions/36133/connecting-to-sql17-from-python
* psqlODBC SSL-Parameter (`sslmode`, `pqopt`): https://www.connectionstrings.com/postgresql-odbc-driver-psqlodbc/ssl/ , https://www.postgresql.org/message-id/2c7d9d2e-065b-d00d-d3e3-cb163fa83323%40aklaver.com
