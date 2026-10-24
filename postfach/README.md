# Postfach-Assistent für JD

Hält das Outlook-Postfach j.duenschede@weinstrasseadolph.de in Ordnung, schreibt Antwort-Entwürfe,
liefert jeden Morgen ein Briefing und lernt mit. Grundlage ist deine Regeldatei `postfach/regeln.json`
(unverändert übernommen). Der Assistent **sendet nie**, **löscht nur soft** (zwei freigegebene Regeln) und
**verschiebt nur auf Knopfdruck**.

## Die vier Funktionen

| Funktion | Wann | Was passiert | Skill |
|---|---|---|---|
| **Einsortieren (Knopf)** | wenn du es auslöst (geplante Aufgabe „Jetzt ausführen“ oder in einer Sitzung „Einsortieren“) | gelesene, erledigte Mails nach Farbe/Regel in die Ordner; ungelesen, markiert, Antworten auf deine Mails, offene Vorgänge, Kunden und Newsletter bleiben im Posteingang; unzustellbare und unerwünschte Weingut-Mails soft gelöscht | `.claude/skills/postfach-einsortieren` |
| **Antwort-Entwürfe** | stündlich Mo–Fr 7–18 Uhr (geplante Aufgabe) oder auf Zuruf | für jede neue Mail, die eine Antwort braucht, ein versandfertiger Entwurf in „Entwürfe“ (Sprache des Absenders, dein Stil, Signaturblock) | `.claude/skills/postfach-entwuerfe` |
| **Morgen-Briefing** | Mo–Fr 06:37 (geplante Aufgabe) oder auf Zuruf | heutige Termine, Zuerst-erledigen, Antworten auf deine Mails, wartende Entwürfe, Anstehendes der nächsten 4 Wochen, Fragen; als Benachrichtigung und in `postfach/briefings/` | `.claude/skills/postfach-briefing` |
| **Lernen** | freitags 17:15 (geplante Aufgabe) oder auf Zuruf | liest gesendete Mails, Kalender, Postfach und schreibt `postfach/wissen/` fort (Kontakte, wiederkehrende Aufgaben, offene Zusagen, Regelvorschläge) | `.claude/skills/postfach-lernen` |

„Antwort bekommen → wieder im Eingang“: Antworten auf deine gesendeten Mails werden erkannt (Betreff + Empfänger
deiner gesendeten Mail) und beim Einsortieren **nie** verschoben, auch wenn gelesen. Sie stehen außerdem im Briefing
unter „Antworten erhalten“.

## Bedienung

- **Knopf:** In claude.ai → Geplante Aufgaben → „Postfach Einsortieren (Knopf)“ → *Jetzt ausführen*. Oder in einer
  Claude-Code-Sitzung mit diesem Repo: `/postfach-einsortieren` bzw. einfach „Einsortieren“.
- **Entwürfe prüfen:** Outlook → Entwürfe. Jeder Entwurf hängt am Original-Thread, Empfänger ist gesetzt. Lesen, ggf.
  anpassen, senden. Nicht passende Entwürfe einfach löschen.
- **Offene Vorgänge abhaken:** `postfach/wissen/offene_vorgaenge.md` – `[x]` setzen, dann dürfen die zugehörigen Mails
  beim nächsten Einsortieren in den Ordner.
- **Regeln ändern:** nur `postfach/regeln.json` bearbeiten (neue Domain, neue Ordner-ID). Vorschläge des Assistenten
  stehen in `postfach/wissen/regelvorschlaege.md`.
- **Wissen korrigieren:** alles in `postfach/wissen/` ist Klartext – einfach ändern. Einträge mit
  „Annahme – bitte prüfen“ in `wiederkehrend.yaml` stammen aus Branchenwissen, nicht aus deinem Postfach.
- **Einstellungen:** `postfach/einstellungen.yaml` (Rückblick, Höchstzahl Entwürfe, Signatur, Briefing-Zustellung).

## Was du noch tun solltest

1. Ordner **LEH** und **Quintessenzio** in Outlook anlegen (Rechtsklick auf dein Postfach → Neuer Ordner) und die IDs
   in `regeln.json` → `ordner` eintragen (ID: Ordner im Browser anklicken, aus der Adresszeile nach `/mail/` kopieren,
   `%3D`→`=`, `%2F`→`-`, `%2B`→`_`). Bis dahin bleiben diese Mails im Posteingang.
2. Ersten Entwurf prüfen: Erscheint deine Outlook-Signatur doppelt, in `einstellungen.yaml` `signatur_anhaengen: false` setzen.
3. Die drei offenen Fragen in `wissen/entscheidungen.md` beantworten (einfach in die Datei schreiben oder dem Assistenten sagen).

## Technik (für Entwickler)

- Deterministischer Kern: Python-Paket `src/postfach/` (nur Standardbibliothek + PyYAML), Tests in `tests/test_postfach_*.py`.
  ```bash
  PYTHONPATH=src python3 -m postfach regeln
  PYTHONPATH=src python3 -m postfach klassifizieren mails.json --gesendet gesendet.json --plan
  PYTHONPATH=src python3 -m postfach anstehend --tage 28
  PYTHONPATH=src python3 -m postfach briefing-geruest
  python3 -m pytest -q tests/test_postfach_*.py
  ```
- Die Verbindung zum Postfach läuft über den Microsoft-365-Connector der Claude-Sitzung (Mail.ReadWrite, Calendars.Read,
  kein eigener App-Schlüssel nötig). Der Agent folgt `postfach/AGENT.md` und den Skills.
- Zustand (`zustand.json`), Journal, Briefings und Wissen werden nach jedem Lauf auf den Branch aus
  `einstellungen.yaml` → `git_branch` gepusht. Nach einem Merge in den Hauptbranch dort den Branchnamen und die Prompts
  der geplanten Aufgaben anpassen.
- Grenzen des Connectors: max. 5 Mails je Verschiebe-Aufruf, ~6–8 Schreibzugriffe/Minute, Ordner anlegen nur im Browser,
  Suchergebnisse zeigen keine Farben (daher Nachladen per `category:`-Suche).

## Ideen für später (siehe Abschlussbericht)
Teams-Zusammenfassung, Fixtermin-Überwachung aus den Auftragsbestätigungen, automatische Nachfass-Erinnerungen,
Verknüpfung mit dem Auftrags-Automaten (Bestell-Mails → Lexware), Wochenrückblick freitags.
