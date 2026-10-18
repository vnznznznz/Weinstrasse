# Meldungen: Intrastat, Schaumweinsteuer, Verpackung (Grüner Punkt / LUCID)

Das Modul `auftragsautomat meldungen` berechnet aus den Lexware-Belegen eines Monats die
drei Pflichtmeldungen und erzeugt Prüflisten, Ausfüllhilfen und Importdateien. **Es
überträgt nichts automatisch an Behörden** – IDEV, Zoll-Portal, Kundenportal des
Systembetreibers und LUCID haben keine offenen Schnittstellen für Dritte; die Abgabe
erfolgt durch Sie mit den erzeugten Werten (CSV-Import bzw. Abtippen aus der Prüfliste).

## Monatsablauf (ab Bezugsmonat Oktober 2026)

1. **Belegexport aus Lexware premium** je Firma: alle Belegpositionen des Monats (Eingangs-
   rechnungen/Wareneingänge, Ausgangsrechnungen, Gutschriften) als CSV oder XLSX nach
   `daten/<firma>/belegpositionen_JJJJ-MM.csv` (Muster konfigurierbar). Siehe unten.
2. `auftragsautomat meldungen pruefen --monat 2026-10` – prüft Export und Artikel-Meldedaten;
   `--vorlage` legt fehlende Artikel als Leerzeilen in `config/meldungen/artikel_meldedaten.csv` an.
   Leerzeilen ausfüllen (Warennummer, Ursprungsland, Volumen, % vol, Erzeugnisart, Verpackungsprofil).
3. `auftragsautomat meldungen erstellen --monat 2026-10` – erzeugt je Firma in
   `meldungen/<firma>/2026-10/`:
   * `intrastat_eingang_idev.csv`, `intrastat_versendung_idev.csv` – Import ins IDEV-Formular,
   * `meldungen_<firma>_2026-10.xlsx` – Blätter Übersicht, Intrastat Eingang/Versendung,
     Schaumweinsteuer 2401 (Mengen je Steuersatz), Schaumweinsteuer Belege, Verpackung,
     Fristen, Hinweise (rot = vor Abgabe beheben),
   * `zusammenfassung.md` – Checkliste zum Abhaken,
   * `meldungen.json` – vollständiges Ergebnis (Nachweis/Archiv).
   Exit-Code 1, solange ein Hinweis mit Schweregrad FEHLER offen ist.
4. Abgabe:
   * **IDEV** (Destatis): Formular "Intrahandel – Eingang" bzw. "Versendung" öffnen, CSV importieren
     oder Positionen aus dem Blatt eintragen; ohne Warenverkehr **Fehlanzeige** senden.
   * **Zoll-Portal**: Dienstleistung "Monatliche Steueranmeldung" → Formular **2401** (Schaumwein,
     Anlage 2402) mit den Litermengen je Steuersatz aus dem Blatt "Schaumweinsteuer 2401".
     Zwischenerzeugnisse (Port, Sherry, Marsala): Formular 2451/2452.
   * **Kundenportal Systembetreiber** (z. B. Der Grüne Punkt): kg je Materialart aus dem Blatt
     "Verpackung" melden – im Turnus des Vertrags – und **dieselben Werte** in LUCID als
     "Unterjährige Mengenmeldung" spiegeln.
5. `auftragsautomat meldungen fristen --monat 2026-10` zeigt die Termine; `meldungen jahr --jahr 2026`
   liefert die Jahres-Ist-Mengen (Jahresabschlussmeldung) und die Intrastat-Schwellenwerte.

## Fristen je Bezugsmonat (automatisch berechnet)

| Meldung | Frist | Rechtsgrundlage |
|---|---|---|
| Intrastat (Eingang/Versendung/Fehlanzeige) | 10. Arbeitstag nach dem Bezugsmonat, keine Verlängerung | Leitfaden Intrahandelsstatistik 2026, 1.6 |
| Schaumweinsteuer – Steueranmeldung 2401 | 10. Tag des Folgemonats | § 15 Abs. 1, § 22b SchaumwZwStG |
| Schaumweinsteuer – Zahlung | 5. Tag des zweiten Folgemonats | § 15 Abs. 1 SchaumwZwStG |
| Verpackung – unterjährige Meldung | laut Systembeteiligungsvertrag (angenommen: 15. des Folgemonats) | VerpackG § 10/§ 11, Vertrag |
| Verpackung – Jahresabschluss (Ist) | 15.05. (Grüner Punkt), LUCID bis 01.06., danach Nachtragsmeldung | VerpackG § 10, ZSVR-Anleitung 08/2026 |
| Verpackung – Planmenge Folgejahr | 31.12. | ZSVR-Anleitung |

## Fachliche Regeln, die das Modul anwendet

**Intrastat** (Leitfaden 2026): Bezugsmonat = Monat der Warenbewegung (Lieferdatum, sonst
Belegdatum). Eingang = Einkaufsbelege von EU-Lieferanten außer DE; Versendung = Verkaufsbelege an
EU-Kunden außer DE. Zusammenfassung je Warennummer, Partnermitgliedstaat, Ursprungsland,
Bundesland, Art des Geschäfts (11), Verkehrszweig (3), bei Versendung zusätzlich USt-IdNr. des
Kunden. Eigenmasse in vollen kg (Liter × Dichte 0,99, ohne Flasche), Besondere Maßeinheit bei
Kapitel 2204 in vollen Litern, Rechnungsbetrag in vollen Euro (< 0,50 → 1), Statistischer Wert =
Rechnungsbetrag + Fracht (bei verbrauchsteuerpflichtigen Waren Pflicht). Gutschriften und
Rücksendungen werden nicht verrechnet, sondern als Warnung gelistet (negative Werte unzulässig,
Rücksendung = Gegenrichtung mit Art des Geschäfts 21). Schwellen: Eingang 3 Mio. EUR, Versendung
1 Mio. EUR (Vorjahr oder laufendes Jahr) – Meldepflicht je Firma in `firmen.yaml` hinterlegen.

**Schaumweinsteuer** (SchaumwZwStG): Schaumwein = Schaumweinstopfen mit Haltevorrichtung ODER
≥ 3 bar bei 20 °C. Frizzante/Perlwein (≤ 2,5 bar, Kronkorken/Schraubverschluss) ist **nicht**
steuerbar – Überdruck und Verschluss je Artikel pflegen und mit Produktdatenblatt belegen.
Sätze: 136 EUR/hl, unter 6 % vol 51 EUR/hl; Zwischenerzeugnisse 102 (≤ 15 % vol) / 153 EUR/hl,
schäumend 136 EUR/hl. Steuerentstehung je Firma: `eingang` (zertifizierter/registrierter
Empfänger: Empfang der Ware) oder `entnahme` (Steuerlager: Lieferung). Stillwein unterliegt der
Weinsteuer mit Satz 0 – keine Steueranmeldung, aber Beförderung mit (vereinfachtem) elektronischem
Verwaltungsdokument.

**Verpackung** (VerpackG): Gemeldet werden die systembeteiligungspflichtigen Verpackungen der in
Deutschland verkauften Flaschen (Verkaufsbelege an DE-Kunden, Gutschriften mindern). Gewichte je
Komponente stehen in `config/meldungen/verpackungen.yaml`; Transportkartons an den Handel sind
als `systembeteiligungspflichtig: false` markiert (Rücknahmepflicht § 15 VerpackG, keine
Systembeteiligung). Materialarten exakt wie im LUCID-Formular, kg mit 3 Nachkommastellen.

## Konfiguration

* `config/firmen.yaml` → `meldungen:` je Firma (Bundesland, USt-IdNr., Meldepflicht, Zollstatus,
  Verbrauchsteuernummer, Systembetreiber, LUCID-Nummer, Meldeturnus, Exportmuster und
  Spaltenzuordnung des Lexware-Exports).
* `config/settings.yaml` → `meldungen:` (Ausgabeordner, Steuersätze, Schwellen, IDEV-Spaltenreihenfolge).
* `config/meldungen/artikel_meldedaten.csv` – je Artikel: Warennummer (8-stellig), Ursprungsland,
  Volumen cl, % vol, Erzeugnisart (stillwein|perlwein|schaumwein|zwischenerzeugnis|sonstiges),
  Überdruck bar, Schaumweinstopfen 0/1, Verpackungsprofil, Flaschen je Lexware-Einheit.
  `firma_id` leer = gilt in allen Firmen.
* `config/meldungen/verpackungen.yaml` – Verpackungsprofile (Gewichte verifizieren!).
* `config/meldungen/warennummern.yaml` – Nachschlagetabelle KN 2204 (vor Erstmeldung im
  Warenverzeichnis prüfen).

### Lexware-Export (Belegpositionen)

Erwartete Felder (Spaltennamen frei, Zuordnung unter `meldungen.bewegungen.spalten`):

| Feld | Pflicht | Inhalt |
|---|---|---|
| belegtyp | ja | ER/WE/BE = Einkauf, RE/LS = Verkauf, GS = Gutschrift Verkauf, EG = Gutschrift Einkauf (Zuordnung `belegtyp_werte`) |
| belegnummer, belegdatum | ja | Lexware-Belegnummer, Datum TT.MM.JJJJ |
| lieferdatum | nein | Wareneingangs-/Lieferdatum (= Bezugsmonat Intrastat) |
| partnernummer, partnername, partner_land, partner_ustid | Land ja | Lieferant bzw. Kunde, ISO-2-Land, USt-IdNr. (Versendung Pflicht) |
| artikelnummer, bezeichnung | Nr. ja | Lexware-Artikelnummer |
| menge, netto_eur, fracht_eur | Menge/Netto ja | Positionsmenge in Lexware-Einheit, Positionsnetto EUR, separat berechnete Fracht |

Weg in Lexware premium: Belegliste (Einkauf bzw. Verkauf) mit Positionsansicht filtern auf den
Monat → Kontextmenü "Export nach MS Excel"; alternativ `Datei → Export` (ASCII/CSV-Assistent)
oder ODBC-Abfrage (nur SELECT). `# TODO-LEXWARE`: exakten Bericht mit Positionsfeldern am
echten System festlegen und die Spaltenzuordnung einmalig eintragen.

## Was zur Inbetriebnahme benötigt wird (Checkliste)

**Zugänge (werden nicht im Tool gespeichert, Abgabe bleibt bei Ihnen):**
- [ ] IDEV-Kennung je meldepflichtiger Firma (Destatis, Materialnummer Eingang/Versendung) –
      Registrierung unter erhebungsportal.estatistik.de; Hinweis: IDEV läuft Ende 2026 aus,
      Nachfolger ist die Online-Meldung im Erhebungsportal (.CORE).
- [ ] Zoll-Portal Geschäftskundenkonto (ELSTER-Zertifikat) mit Dienstleistung "Monatliche
      Steueranmeldung" für das zuständige Hauptzollamt.
- [ ] Erlaubnis als **zertifizierter Empfänger** (Dauererlaubnis, § 20a SchaumwZwStG) oder
      Steuerlager/registrierter Empfänger je Firma, Verbrauchsteuernummer, ggf. Sicherheitsleistung.
- [ ] Kundenportal des Systembetreibers (Der Grüne Punkt) – Vertragsnummer, Meldeturnus.
- [ ] LUCID-Zugang je Firma (Registrierungsnummer DE…, Systembeteiligungsvertrag hinterlegt).

**Unterlagen / Stammdaten:**
- [ ] Artikelliste je Firma mit Warennummer (KN 8-stellig), Ursprungsland, Volumen, % vol,
      Überdruck (Frizzante!) und Verschlussart – Produktdatenblätter der Weingüter.
- [ ] Verpackungsgewichte (Flasche, Verschluss, Kapsel, Etikett, Karton) – einmal wiegen oder
      Spezifikationen der Weingüter; welche Kunden sind Endverbraucher/Gastronomie (B2C)?
- [ ] Vorjahresumsätze innergemeinschaftlich (Eingang/Versendung) je Firma zur Schwellenprüfung;
      letzte abgegebene Intrastat-, Steuer- und Verpackungsmeldungen als Abgleich.
- [ ] Sitz/Lager-Bundesland je Firma, USt-IdNr., zuständiges Hauptzollamt.
- [ ] Ein Lexware-Belegexport (Einkauf + Verkauf) für September oder Oktober 2026 zum Einrichten
      der Spaltenzuordnung.
