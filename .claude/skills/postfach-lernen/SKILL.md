---
name: postfach-lernen
description: Wöchentlicher Lern-Lauf des Postfach-Assistenten – aus gesendeten Mails, Kalender und Postfach ableiten, was bei JDs Arbeit wiederkehrt, wer wer ist, welche Fristen und Projekte kommen; Wissen in postfach/wissen fortschreiben, Regelvorschläge sammeln. Auf Zuruf („lern dazu“, „Wissen aktualisieren“, „was hast du gelernt?“) oder per Zeitplan. Verschiebt und sendet nichts.
---

# Lernen: Wissen fortschreiben

Zuerst `postfach/AGENT.md` lesen und Schritt 0 ausführen. Einstellungen: `einstellungen.yaml` → `lernen`.
Ziel: Der Assistent weiß im Voraus, was in den nächsten Wochen ansteht und eilig ist – ohne dass JD es jedes Mal sagt.

## Quellen (Zeitraum `gesendete_mails_rueckblick_tage`, Standard 14 Tage)
1. **Gesendete Mails:** `outlook_email_search(folderName="Sent Items", order="newest", limit=25)` (+offset) bis zum Zeitraumende.
   Pro Mail: an wen (Rolle, Sprache, Anrede), Thema, Zusagen („sende ich bis …“, „melde mich“), wiederkehrende Muster
   (Aufträge dienstags an Hillebrand, Auftragskopien an Kunden, Monatsmeldungen an Böhling).
2. **Kalender:** `outlook_calendar_search(query="*", afterDateTime="<−14 Tage>", beforeDateTime="<+60 Tage>", order="oldest")` –
   Serien erkennen, einmalige Termine mit Vorbereitungsbedarf (Messen, Steuerberater, Audits).
3. **Posteingang:** ungelesen/markiert älter als 3 Tage, Mails mit Frist/Termin/„bis“/„deadline“/„urgent“ im Betreff;
   Lieferanten-Mails von Domains, die nicht in `regeln.json` stehen.
4. **Journal und Zustand:** `postfach/journal/*.md`, `zustand.json` – was wurde oft nachgefragt, welche Entwürfe blieben liegen?
5. **Bestehendes Wissen:** alle Dateien in `postfach/wissen/`.

## Ableiten und schreiben
- `wissen/kontakte.md`: neue Personen/Rollen/Sprachen, geänderte Zuständigkeiten (z. B. Vertretung endet). Alte Angaben nicht löschen, sondern korrigieren mit Datum.
- `wissen/wiederkehrend.yaml`: wiederkehrende Aufgaben mit Rhythmus, `vorlauf_tage`, `aufgaben`, `quelle` (Mail/Kalender/Datum);
  einmalige Termine als `einmalig`; `quelle: Annahme – bitte prüfen` durch echte Quelle ersetzen oder Eintrag streichen, wenn nichts darauf hindeutet.
  Danach `PYTHONPATH=src python3 -m postfach anstehend --tage 60` ausführen – muss ohne Fehler laufen.
- `wissen/offene_vorgaenge.md`: Zusagen aus gesendeten Mails als Punkte ergänzen („JD hat zugesagt: Screenshot an Advini“), Erledigtes abhaken.
- `wissen/stil.md`: nur ergänzen, wenn ein Muster mindestens 3× vorkommt (neue Grußformel, Sprache, Ton je Partner).
- `wissen/regelvorschlaege.md`: neue Domains mit Vorschlag (Lieferant → welche Farbe; Spediteur; Kunde → Rest). `regeln.json` **nicht** anfassen.
- `wissen/profil.md`: Rolle/Schwerpunkte nur bei klaren Hinweisen anpassen (z. B. Vertretung, neue Aufgabe).
- `wissen/entscheidungen.md`: nur, was JD tatsächlich entschieden hat (aus Antworten an den Assistenten oder eindeutigen Mails).

## Qualitätsregeln
- Jede neue Aussage hat eine Quelle (Datum + Mail/Kalender). Keine Branchenannahmen mehr hinzufügen.
- Nichts Privates über Familienmitglieder festhalten, das nicht für die Arbeit nötig ist.
- Höchstens 3 Fragen an JD im Bericht, jede mit Vorschlag; Antworten beim nächsten Lauf nach `entscheidungen.md`.

## Abschluss
`zustand lauf lernen`, Journal, Commit, Push (AGENT.md Abschnitt 4). Bericht:
```
Gelernt: <n> neue Kontakte, <m> wiederkehrende Aufgaben (davon <k> aus Annahmen bestätigt/gestrichen), <o> offene Zusagen ergänzt.
Vorschläge für Regeln: …
Fragen: …
```
