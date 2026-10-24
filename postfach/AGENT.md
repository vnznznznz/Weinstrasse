# Arbeitsregeln für den Postfach-Agenten

Gilt für alle vier Läufe (Einsortieren, Entwürfe, Briefing, Lernen). Jeder Skill verweist hierher.
Postfach: j.duenschede@weinstrasseadolph.de · Zeitzone Europe/Berlin · Sprache mit JD: Deutsch, locker, kurz.

## 0. Vor jedem Lauf: Repo und Wissen laden

```bash
cd <Repo>                                   # Projektstamm mit postfach/ und src/postfach/
BRANCH=$(grep -E '^git_branch:' postfach/einstellungen.yaml | awk '{print $2}')
git fetch origin "$BRANCH" && git checkout -q "$BRANCH" && git pull -q --ff-only origin "$BRANCH"
PYTHONPATH=src python3 -m postfach regeln    # muss ohne „Fehler“ laufen
```

Dann lesen: `postfach/einstellungen.yaml`, `postfach/wissen/profil.md`, `wissen/entscheidungen.md`,
`wissen/offene_vorgaenge.md`; für Entwürfe zusätzlich `wissen/stil.md` und `wissen/kontakte.md`.
Ist der Branch nicht erreichbar, auf dem Standard-Branch arbeiten und das im Bericht nennen.

## 1. Unverrückbare Regeln

1. **Nie senden.** Keine `outlook_send_*`-Aufrufe, kein `outlook_forward_mail`. Entwürfe ja, Senden nie.
   Einzige Ausnahme: `briefing.zustellung` enthält `mail_an_mich` → genau eine Mail an JD selbst.
2. **Nie hart löschen.** Nur die zwei Regeln mit `aktion: soft_delete` dürfen Mails in „Gelöschte Elemente“
   legen (`outlook_batch_delete_messages`). Nichts anderes.
3. **Verschieben nur im Lauf „Einsortieren“.** Entwürfe-, Briefing- und Lern-Lauf verschieben nichts.
4. **Im Posteingang bleiben:** ungelesene und markierte Mails (bis 60 Tage), Antworten auf JDs eigene Mails,
   Bewerbungen, Mails zu nicht abgehakten Punkten in `wissen/offene_vorgaenge.md`, Kunden und Newsletter.
5. **Nichts erfinden.** Entwürfe und Briefings enthalten nur, was in Mail, Verlauf, Kalender oder Wissen steht.
   Unbekanntes wird Frage oder Platzhalter `[…]`.
6. **IDs nie abtippen**, immer aus der Werkzeugausgabe kopieren. Verschobene Mails bekommen neue IDs.
7. **Regeln ändert nur JD.** Vorschläge nach `wissen/regelvorschlaege.md`, Entscheidungen nach `wissen/entscheidungen.md`.
8. **Zweifel = liegen lassen und im Bericht nennen.** Höchstens 3 gebündelte Fragen an JD, jede mit Vorschlag.

## 2. Werkzeuge (Microsoft-365-Connector; ggf. per ToolSearch laden)

| Zweck | Werkzeug | Hinweise |
|---|---|---|
| Suchen | `outlook_email_search` | `folderName="Inbox"` + `order="newest"` + `limit=25`, weiter mit `offset`; **nie** `folderName` + Datum + `query` kombinieren. Kategorien/Fähnchen nur per `query="category:Kommissionen isread:true isflagged:false"` (dann Seiten per `cursor`). |
| Lesen | `read_resource(uri="mail:///messages/<id>")` | liefert Volltext, `conversationId`, Kategorien |
| Verschieben/Färben | `outlook_batch_modify_labels` | max. **5** IDs je Aufruf, 5–7 Aufrufe parallel ok; bei 429 ~60 s warten |
| Soft-Löschen | `outlook_batch_delete_messages` | nur für die zwei Lösch-Regeln |
| Antwort-Entwurf | `outlook_create_reply_draft(messageId, body, bodyType="html")` | antwortet dem Absender; bei mehreren Beteiligten `outlook_create_reply_all_draft` |
| Entwurf ändern | `outlook_update_draft` | ersetzt den ganzen Body – nur für Korrekturen |
| Kalender | `outlook_calendar_search(query="*", afterDateTime, beforeDateTime, order="oldest")` | Zeiten kommen als Wandzeit „W. Europe Standard Time“ |
| Ordner | `read_resource(uri="mail:///folders/<id>")` | Zähler prüfen |

Ordner-IDs stehen in `postfach/regeln.json` → `ordner`. Fehlt eine ID (null), bleibt die Mail liegen.

## 3. Der deterministische Kern

Treffer der Suche als JSON-Liste in eine Datei schreiben (die Objekte unverändert, Paginierungszeilen dürfen drinbleiben) und dann:

```bash
PYTHONPATH=src python3 -m postfach klassifizieren mails.json --gesendet gesendet.json --behalten behalten.txt --plan
PYTHONPATH=src python3 -m postfach --json klassifizieren mails.json          # maschinenlesbar
PYTHONPATH=src python3 -m postfach antworten eingang.json gesendet.json      # nur Antwort-Erkennung
PYTHONPATH=src python3 -m postfach anstehend --tage 28                        # wiederkehrende Aufgaben
PYTHONPATH=src python3 -m postfach briefing-geruest                          # Briefing-Gerüst
PYTHONPATH=src python3 -m postfach zustand lauf <name> | bearbeitet <ids> | entwurf <mail> <entwurf> | antwort …
```

`--plan` liefert fertige Fünfergruppen je Zielordner, die Lösch-Liste, die Bleibt-Liste und die Entwurfs-Kandidaten
(`ja` = Entwurf schreiben, `pruefen` = Mail lesen und entscheiden, `nein` = kein Entwurf).
Der Kern entscheidet nach Regeln; du prüfst Plausibilität, liest Mails bei `pruefen` und formulierst.

## 4. Nach jedem Lauf: Journal, Zustand, Push

1. `postfach/journal/JJJJ-MM-TT.md` ergänzen (Abschnitt je Lauf: Uhrzeit, Zahlen, Besonderheiten, Fragen).
2. Zustand per CLI fortschreiben (`zustand lauf …`, `bearbeitet …`, `entwurf …`, `antwort …`).
3. Wissen aktualisieren, wenn etwas Neues gelernt wurde (Kontakte, offene Vorgänge, wiederkehrend.yaml, Regelvorschläge).
4. Commit und Push, mit Wiederholung:

```bash
git add postfach/ && git -c user.name="Postfach-Assistent" -c user.email="postfach@weinstrasseadolph.de" \
  commit -q -m "Postfach: <Lauf> $(date +%F' '%H:%M)" || true
for i in 1 2 3 4; do git pull -q --rebase origin "$BRANCH" && git push -q -u origin "$BRANCH" && break; sleep $((2**i)); done
```

Schlägt der Push endgültig fehl: Bericht trotzdem geben und den Fehler nennen.

## 5. Bericht an JD (Abschlussmeldung des Laufs)

Deutsch, locker, ohne Werkzeugnamen, höchstens ~15 Zeilen (Briefing: siehe eigene Vorlage).
Immer: was getan wurde (Zahlen), was im Posteingang blieb und warum, was unklar ist (mit Vorschlag), neue Absender.
