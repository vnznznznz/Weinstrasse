---
name: postfach-entwuerfe
description: Für jede neu eingetroffene Mail in JDs Outlook, die eine Antwort braucht, einen versandfertigen Antwort-Entwurf im Ordner „Entwürfe“ anlegen (Sprache des Absenders, JDs Stil, nichts erfinden). Läuft stündlich per Zeitplan oder auf Zuruf („Entwürfe schreiben“, „Antworten vorbereiten“). Verschiebt und sendet nichts.
---

# Antwort-Entwürfe für neue Mails

Zuerst `postfach/AGENT.md` lesen und Schritt 0 ausführen. Dann `wissen/stil.md`, `wissen/kontakte.md`,
`wissen/offene_vorgaenge.md` lesen. Einstellungen: `einstellungen.yaml` → `entwuerfe`.

## Schritt 1: Neue Mails bestimmen
1. `seit` = letzter Lauf `entwuerfe` aus `PYTHONPATH=src python3 -m postfach zustand zeigen` (`laeufe.entwuerfe.zeit`);
   fehlt er, `jetzt − rueckblick_stunden`.
2. `outlook_email_search(folderName="Inbox", afterDateTime="<seit ISO>", limit=25)` (+offset) → `mails.json`.
3. `outlook_email_search(folderName="Sent Items", afterDateTime="<seit − 21 Tage>", limit=25)` (+offset) → `gesendet.json`.
4. Bereits bearbeitete `internetMessageId`s (Zustand → `bearbeitet`) und Mails mit vorhandenem Entwurf (Zustand → `entwuerfe`) überspringen.

## Schritt 2: Entscheiden
```bash
PYTHONPATH=src python3 -m postfach --json klassifizieren mails.json --gesendet gesendet.json > entscheidungen.json
```
- `antwort: "nein"` → nichts. `antwort: "ja"` → Entwurf. `antwort: "pruefen"` → Mail mit `read_resource` lesen und entscheiden:
  Entwurf nur, wenn JD gefragt/gebeten wird oder etwas zusagen muss. Reine Information, Weiterleitung ohne Bitte, „nur in Kopie“ ohne Frage → kein Entwurf.
- Zusätzlich kein Entwurf: Absender in `nie_an`, Mails von JD selbst, Kalendereinladungen, Mails, auf die JD laut `gesendet.json` schon geantwortet hat
  (gleicher normalisierter Betreff, Antwort nach Eingang).
- Doppelte vermeiden: vor dem Anlegen `outlook_email_search(folderName="Drafts", query="<Betreff ohne Präfix>", limit=5)`; gibt es schon einen Entwurf im Thread, nichts anlegen.
- Höchstens `hoechstens_je_lauf` Entwürfe; Rest im nächsten Lauf (Zustand nicht als bearbeitet markieren).

## Schritt 3: Formulieren
Vollständige Mail lesen (`read_resource`), bei Threads den letzten Stand erfassen. Dann schreiben nach `stil.md`:
- **Sprache = Sprache der eingehenden Mail** (`sprache: wie_absender`). Deutsch mit Sie/Du laut `kontakte.md`; unbekannt → Sie.
- 1–4 Sätze: Dank/Bezug (kurz), Antwort oder Zusage mit nächstem Schritt, ggf. eine Rückfrage. Fakten nur aus Mail, Thread, Kalender, Wissen.
  Fehlende Angaben als Platzhalter `[Termin]`, `[Auftragsnummer]`, Vermutungen als Frage.
- Aktionen, die JD erst tun muss (Anhang schicken, Spedition fragen), als Zusage formulieren („sende ich Ihnen heute zu“), **nicht** als erledigt.
- HTML: nur `<p>`, `<br>`, `<b>`, `<ul><li>`, `<a>`. Grußformel + Signaturblock aus `stil.md`, wenn `signatur_anhaengen: true`.
- Betreff bleibt (Reply-Draft setzt ihn); `markierung_im_betreff` nur, wenn gesetzt.

Beispiel (Spediteur fragt nach fehlendem Auftrag):
```html
<p>Hallo Herr von Thienen,</p>
<p>danke für den Hinweis. Den Auftrag 20260029 sende ich Ihnen heute noch zu, dann können Sie die Abholung beim Weingut einplanen.</p>
<p>Mit freundlichen Grüßen/ Best regards<br>Josefa Dünschede<br><br>Weinstrasse Adolph GmbH<br>Wein-Import-Kreis GmbH<br>Sachsenring 38, 50677 Köln<br><br>Tel.: +49 221/312413<br>Fax: +49 221/312415<br>j.duenschede@weinstrasseadolph.de<br>www.weinstrasseadolph.de</p>
```

## Schritt 4: Anlegen
- Ein Absender → `outlook_create_reply_draft(messageId=<id>, body=<html>, bodyType="html")`.
- Mehrere Beteiligte, die mitlesen sollen (Kollegen in Kopie, Lieferant + Kunde) → `outlook_create_reply_all_draft`.
- Ergebnis-ID und `webLink` merken: `zustand entwurf "<internetMessageId>" "<entwurfId>" --betreff "…" --an "…"`.
- Danach `zustand bearbeitet <internetMessageId …>` für alle geprüften Mails (auch „nein“), `zustand lauf entwuerfe`.
- Erkannte Antworten auf JDs Mails (`bleibt_grund` beginnt mit „Antwort“): `zustand antwort --betreff … --von … --link …` – sie erscheinen im Briefing unter „Antworten erhalten“ und bleiben im Posteingang.
- Rate-Limit (429): 60 s warten, weiter.

## Schritt 5: Lernen nebenbei
Neue Ansprechpartner/Sprache/Anrede → `kontakte.md`. Neue Zusagen oder Fristen aus der Mail → `offene_vorgaenge.md`.
Unbekannte Lieferanten-Domains → `regelvorschlaege.md`. Dann Journal, Commit, Push (AGENT.md Abschnitt 4).

## Bericht
```
<N> Entwürfe angelegt (<Sprache(n)>): <Absender – Betreff> …  (Ordner „Entwürfe“, versandfertig)
Ohne Entwurf: <k> Newsletter/System, <m> nur Information, <r> schon von dir beantwortet.
Antworten auf deine Mails eingetroffen: …
Bitte prüfen: <Entwürfe mit Platzhaltern oder Unsicherheiten>
```
