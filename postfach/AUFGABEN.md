# Geplante Aufgaben des Postfach-Assistenten

Angelegt am 24.10.2026 aus der Einrichtungssitzung. Sichtbar in claude.ai unter „Geplante Aufgaben“.

| Aufgabe | Zeitplan (Europe/Berlin) | Trigger-ID | Benachrichtigung |
|---|---|---|---|
| Postfach Einsortieren (Knopf) | kein Zeitplan – nur „Jetzt ausführen“ | trig_01WKC5yF8s3qdkKtEPm2L7Aj | Push |
| Postfach Morgen-Briefing | Mo–Fr 06:37 | trig_01XxFPYGgrkHtYkSPuFeqL4p | Push + E-Mail |
| Postfach Antwort-Entwürfe | Mo–Fr stündlich 07:12–18:12 | trig_01HwsfkJyMRS9PpFDdKhiVcS | Push |
| Postfach Lernen (wöchentlich) | Fr 17:15 | trig_019yDY7Dj8118mg1JpMperoy | Push |

## Einmalig nötig (JD)

1. **Microsoft-365-Connector aktivieren:** claude.ai → Geplante Aufgaben → Aufgabe öffnen → Connectors → „Microsoft 365“.
   Aus der Claude-Code-Sitzung konnte der Connector nicht mitgegeben werden; ohne ihn melden die Läufe nur, dass sie
   kein Postfach erreichen.
2. **Repository prüfen:** Die Aufgaben wurden ohne hinterlegte Repository-Quelle gespeichert. Der Prompt klont
   `https://github.com/vnznznznz/Weinstrasse` selbst, falls es fehlt. Meldet ein Lauf, dass Klonen oder Pushen nicht geht,
   die Aufgabe in claude.ai neu anlegen und dabei das Repository Weinstrasse als Quelle wählen (Prompt siehe unten).
3. **Kadenz anpassen (optional):** Stündliche Entwürfe = bis zu 12 Läufe am Tag. Reicht alle zwei Stunden, Zeitplan auf
   `12 7-17/2 * * 1-5` ändern.

## Prompts (zum Kopieren, falls eine Aufgabe neu angelegt werden muss)

Alle vier beginnen gleich:

```
Du bist der Postfach-Assistent von JD (j.duenschede@weinstrasseadolph.de). Lauf: <EINSORTIEREN | MORGEN-BRIEFING | ANTWORT-ENTWÜRFE | LERNEN>.

Vorbereitung: Prüfe, ob das Repository Weinstrasse (Projektstamm mit postfach/ und src/postfach/) im Arbeitsverzeichnis liegt. Falls nicht: git clone https://github.com/vnznznznz/Weinstrasse.git und hineinwechseln. Dann:
git fetch origin claude/sleepy-wright-8zmnua && git checkout claude/sleepy-wright-8zmnua && git pull --ff-only origin claude/sleepy-wright-8zmnua
(Existiert der Branch nicht mehr, auf dem Standard-Branch arbeiten und das im Bericht nennen.)

Dann lies postfach/AGENT.md vollständig und danach .claude/skills/postfach-<einsortieren|briefing|entwuerfe|lernen>/SKILL.md und führe den Lauf Schritt für Schritt aus. Nutze den Microsoft-365-Connector (Werkzeuge bei Bedarf per ToolSearch laden). Ist der Connector nicht verfügbar, nichts am Postfach tun und das als Abschlussmeldung berichten.

Unverrückbar: nie Mails senden, nie hart löschen, verschieben nur im Lauf EINSORTIEREN (dort nur nach den Regeln; ungelesen, markiert, Antworten auf JDs Mails, Bewerbungen, offene Vorgänge, Kunden und Newsletter bleiben im Posteingang). Im Zweifel liegen lassen und im Bericht nennen.

Am Ende: Journal, Zustand und Wissen aktualisieren, committen und pushen (AGENT.md Abschnitt 4). Abschlussmeldung = Bericht laut Skill (Briefing: der vollständige Briefing-Text).
```

Nach einem Merge in den Hauptbranch: Branchnamen in den Prompts und in `postfach/einstellungen.yaml` (`git_branch`) anpassen.
