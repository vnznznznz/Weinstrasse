---
name: postfach-briefing
description: Kurzes Morgen-Briefing für JD – heutige Termine, Zuerst-erledigen-Liste, Antworten auf eigene Mails, wartende Entwürfe, anstehende Aufgaben der nächsten Wochen, Fragen. Läuft werktags morgens per Zeitplan oder auf Zuruf („Briefing“, „was steht heute an?“, „Tagesüberblick“). Verschiebt und sendet nichts.
---

# Morgen-Briefing

Zuerst `postfach/AGENT.md` lesen und Schritt 0 ausführen. Einstellungen: `einstellungen.yaml` → `briefing`.
Ziel: in 60 Sekunden lesbar, nur was heute und diese Woche zählt. Keine Werkzeugnamen, keine Vermutungen als Fakten.

## Schritt 1: Material sammeln (parallel)
1. Gerüst: `PYTHONPATH=src python3 -m postfach briefing-geruest --tage <anstehend_tage> > briefing.md`
   (enthält Datum/KW, „Antworten erhalten“ und „Entwürfe“ aus dem Zustand, fällige wiederkehrende Aufgaben).
2. Kalender heute: `outlook_calendar_search(query="*", afterDateTime="<heute 00:00>", beforeDateTime="<heute 23:59>", order="oldest")`;
   Woche: bis `heute + kalender_tage_voraus`. Wandzeit so übernehmen, wie sie kommt (Europe/Berlin).
3. Posteingang seit gestern 06:00: `outlook_email_search(folderName="Inbox", afterDateTime="<gestern 06:00>", limit=25)` (+offset) → `mails.json`,
   plus `query="isflagged:true"` und `query="category:Steuerberater isread:false"`, `query="category:Kommissionen isread:false"`.
4. Gesendete seit gestern → `gesendet.json`. Dann `PYTHONPATH=src python3 -m postfach --json klassifizieren mails.json --gesendet gesendet.json`
   → liefert Antworten auf eigene Mails (`bleibt_grund` „Antwort…“) und Dringlichkeitssignale (Steuerberater, Kommissionen, Spediteur-Fragen).
5. `wissen/offene_vorgaenge.md` lesen; Punkte, zu denen gestern etwas kam, aktualisieren (Status, neuer Link).
6. Entwürfe-Ordner zählen: `outlook_email_search(folderName="Drafts", limit=25)` – wie viele warten, welche älter als 2 Tage.

## Schritt 2: Schreiben (Gerüst ausfüllen, max. ~25 Zeilen)
```
# Briefing <Wochentag>, <TT.MM.JJJJ> (KW <n>)
## Heute
- 08:30 Wochenstartmeeting (Teams) · 14:00 Böhling in Köln – persönlich!
## Zuerst erledigen (max. 5, Reihenfolge: Frist/Geld/Behörde/Steuerberater → wartender Kunde → Spedition → Rest)
- **Hillebrand – Auftrag 20260029 nachsenden** (seit 23.10., Entwurf liegt bereit) [Mail](…)
## Antworten erhalten – wartet auf dich
- Eduardo (Messias) zu „new order“: … [Mail](…)
## Entwürfe, die auf dich warten: <n> (davon <k> älter als 2 Tage)
## Anstehend (nächste 4 Wochen)
- 02.11. Böhling-Termin · 05.11. Monatsabschluss an Steuerberater · ab Mitte Nov: Preislisten 2027 einsammeln
## Diese Woche im Blick
- Termine, zugesagte Rückmeldungen („Frachtkosten Strauss“), erwartete Lieferungen/Abholungen
## Neu gelernt / Fragen an dich (max. 3, je mit Vorschlag)
```
- Jeder Mail-Punkt mit `[Mail](webLink)` aus der Suchausgabe. Keine Punkte ohne Quelle.
- „Zuerst erledigen“ = heute wirklich zu tun; alles andere in „Diese Woche“.
- Wochenende/Feiertag: nur erzeugen, wenn ausdrücklich gewünscht.

## Schritt 3: Zustellen
- Immer: Datei `postfach/briefings/JJJJ-MM-TT.md` schreiben und der **vollständige Briefing-Text als Abschlussmeldung** (das löst die Push-/Mail-Benachrichtigung des Zeitplans aus).
- `teams_chat` in `zustellung`: `teams_send_chat_message` an JD selbst (Chat mit sich selbst, sonst überspringen und melden).
- `mail_an_mich` in `zustellung`: `outlook_send_mail(to=[JD], subject="Briefing <Datum>", body=<html>)` – nur dann.

## Schritt 4: Nachlaufen
`wissen/offene_vorgaenge.md` aktualisieren, neue Termine aus Mails in `wiederkehrend.yaml` (einmalig) eintragen,
`zustand lauf briefing`, Journal, Commit, Push (AGENT.md Abschnitt 4).
