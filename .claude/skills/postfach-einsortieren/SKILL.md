---
name: postfach-einsortieren
description: Der Knopf „Einsortieren“ für JDs Outlook-Postfach – bearbeitete Mails nach den Regeln in Ordner verschieben, Antworten auf eigene Mails und Offenes im Posteingang lassen, zwei freigegebene Lösch-Regeln anwenden. Nutzen bei „Einsortieren“, „räum auf“, „Postfach aufräumen“, „in die Ordner“.
---

# Postfach einsortieren (Knopf)

Zuerst `postfach/AGENT.md` lesen und Schritt 0 ausführen. Dieser Lauf ist der **einzige**, der verschiebt.
Alles aus `postfach/einstellungen.yaml` → `einsortieren`.

## Schritt 1: Posteingang einlesen
1. `outlook_email_search(folderName="Inbox", order="newest", limit=25)`, weiter mit `offset=nextOffset`,
   bis Mails älter als `rueckblick_tage` kommen (Standard 60 Tage). Alle Treffer unverändert in `mails.json` sammeln.
2. Gesendete Mails derselben Spanne: `outlook_email_search(folderName="Sent Items", order="newest", limit=25)` (+offset) → `gesendet.json`.
   Sie dienen der Antwort-Erkennung („Antwort auf deine Mail bleibt im Eingang“).
3. Farben nachladen, weil die Suche keine Kategorien zeigt – je Farbe:
   `outlook_email_search(folderName="Inbox", query="category:<Wort>", limit=25)` (+cursor) mit Wort =
   Steuerberater · Kommissionen · Marketing · Allgemeine · Intern · Privat. Die IDs je Farbe merken und in `mails.json`
   beim passenden Objekt als `"categories": ["<voller Kategoriename>"]` ergänzen (kleines Python-Skript, nicht von Hand).
   Volle Namen: Steuerberater, Kommissionen, Marketing und Promos, Allgemeine Kommunikation, Intern, Privat.
4. Markierte Mails: `query="isflagged:true"` → beim Objekt `"isFlagged": true` setzen.

## Schritt 2: Behalten-Liste
`behalten.txt` mit einer Mail-ID je Zeile für:
- Mails zu **nicht abgehakten** Punkten in `postfach/wissen/offene_vorgaenge.md` (per Betreff/Absender in `mails.json` finden),
- alles, was JD im Aufruf ausdrücklich nennt.
Ungelesen, markiert, Antworten auf eigene Mails und Bewerbungen behandelt der Kern selbst.

## Schritt 3: Plan rechnen
```bash
PYTHONPATH=src python3 -m postfach klassifizieren mails.json --gesendet gesendet.json --behalten behalten.txt --plan > plan.md
```
Plan lesen. Stutzig machen sollten dich: Zielordner „Privat“ für offensichtlich Geschäftliches, Lösch-Liste mit
echten Absendern, Hinweise „Farbe gewinnt“ mit abweichender Regel. Solche Mails aus den Gruppen nehmen, im Bericht nennen.

## Schritt 4: Ausführen
- Je Gruppe `outlook_batch_modify_labels(messageIds=[…max 5], moveToFolderId="…")`; 5–7 Aufrufe parallel, `movedCount` prüfen.
- Lösch-Liste nur bei `loeschen_erlaubt: true`: `outlook_batch_delete_messages(messageIds=[…])`.
- Bei 429/Rate-Limit ~60 s warten und die restlichen Gruppen fortsetzen. Zwischenstand in `plan.md` abhaken.
- Zielordner mit fehlender ID (LEH, Quintessenzio): nichts tun, im Bericht als Frage („Ordner anlegen?“).

## Schritt 5: Kontrolle, Journal, Push
- 2–3 Ordner stichprobenartig mit `read_resource(uri="mail:///folders/<id>")` prüfen.
- Links in `offene_vorgaenge.md` zu verschobenen Mails sind tot → per Suche neuen `webLink` holen und ersetzen.
- Journal, `zustand lauf einsortieren --notiz "<N> verschoben"`, Wissen, Commit, Push (AGENT.md Abschnitt 4).

## Bericht
```
Erledigt. <N> Mails einsortiert: Kommissionen <a>, Marketing <b>, Steuerberater <c>, Allgemeine <d>, INTERN <e>, Interne Buchhaltung <f>, Privat <g>, Preislisten <h>. <k> unzustellbare/unerwünschte Mails in „Gelöschte Elemente“.
Im Posteingang geblieben: <u> ungelesen, <m> markiert, <r> Antworten auf deine Mails, <o> offene Vorgänge, Kunden & Newsletter.
Unklar (kurz entscheiden): …
Neue Absender: …
```
