# Website-Relaunch weinstrasseadolph.de (Entwurf)

Statische Website mit Startseite, Weingut-Übersicht, 47 Weingut-Seiten, Katalog 2026 mit Preisen,
Bestellportal für den Fachhandel, Service- und Über-uns-Seite. Noch **nicht veröffentlicht**:
Inhalte und Gestaltung lassen sich hier bearbeiten, prüfen und erst dann live schalten.

## Vorschau lokal

```bash
python3 website/build.py                 # schreibt website/dist/
python3 -m http.server -d website/dist 8080
# dann http://localhost:8080 im Browser öffnen
```

Voraussetzungen: Python 3.11+, `jinja2` (ist bereits Abhängigkeit des Auftragsautomaten).

## Wo wird was gepflegt?

| Was | Datei |
|---|---|
| Weingüter (Name, Ort, Claim, Porträt, Besonderheiten, Mindestmenge, Fracht, Bilder) | `daten/weingueter.json` |
| Alle Artikel mit Preisen, UVP, Auszeichnungen, Beschreibungen | `daten/produkte.json` |
| Weingut-Konditionen aus dem Katalog (Mindestmengen, Frachtstaffeln, Katalogseiten) | `daten/katalog_weingueter.json` |
| Aktuelles-Kacheln auf der Startseite | `daten/aktuelles.json` |
| Feste Texte (Slogan, Wer wir sind, Geschichte, Kontaktdaten) | `daten/texte.json` |
| Seitenaufbau (HTML-Vorlagen) | `templates/*.html` |
| Gestaltung | `static/css/site.css` |
| Effekte, Bestellkorb, Weinberater | `static/js/site.js` |
| Katalogfilter und Bestellportal | `static/js/portal.js` |
| Fotos, Logos, Icons | `static/img/` |

Nach jeder Änderung `python3 website/build.py` ausführen. `dist/` ist Build-Ausgabe und liegt nicht
im Repository.

## Datenherkunft

* `daten/roh/weingueter_web.json`: Texte und Bildlisten der heutigen Weingut-Seiten (überarbeitet
  von Felix Berner, Stand Oktober 2026), per Skript eingesammelt.
* `daten/roh/preisliste_2026.txt`, `daten/roh/katalog_2026.txt`: Rohtext der ProWein-Preisliste 2026
  und des digitalen Katalogs 2026 aus SharePoint, daraus wurden `produkte.json` und
  `katalog_weingueter.json` erzeugt.
* `scripts/zusammenfuehren.py` baut `weingueter.json` aus den Rohdaten neu (`--sanft` behält
  von Hand gepflegte Felder).

Alle Preise: netto ab Keller, Stand Katalog 2026 (gültig ab 1.3.2026). Vor der Veröffentlichung
sollte ein Verkäufer die Artikel und Texte einmal durchsehen (Empfehlung aus dem Übergabe-Protokoll).

## Kundenlogin und Preisschutz

Einkaufspreise und Bestellportal sind nur nach Anmeldung sichtbar. Weil die Seite statisch ist,
werden die Preise beim Bauen mit AES-256-GCM verschlüsselt (`static/js/preise.js`); im öffentlichen
Datenbestand (`daten.js`) stehen keine Einkaufspreise. Jeder Kunde bekommt eine Kundennummer und ein
Passwort; daraus wird im Browser (PBKDF2, 250.000 Runden) der Schlüssel abgeleitet, der den
Preisschlüssel des Kunden entpackt. Die Website enthält weder Passwörter noch Klartextpreise.

```bash
python3 website/scripts/zugang.py anlegen --kundennummer 10001 --firma "Weinhandlung Muster" --passwort "..."
python3 website/scripts/zugang.py anlegen --kundennummer 10002 --firma "Vinothek Beispiel"      # Passwort wird erzeugt
python3 website/scripts/zugang.py liste
python3 website/scripts/zugang.py entfernen --kundennummer 10001
python3 website/build.py
```

* `daten/schluessel.key` ist der Preisschlüssel (geheim, nicht im Repository, sichern!).
* `daten/zugaenge.json` enthält nur eingepackte Schlüssel je Kunde und darf ins Repository.
* Demo-Zugänge im Entwurf: 10001 / Probe-2026 und 10002 / Degustation-26. Vor dem Livegang
  entfernen und echte Kundennummern aus Lexware anlegen.
* Passwort vergessen: Zugang mit `anlegen` einfach neu setzen und bauen.
* Grenzen: Ein angemeldeter Kunde könnte die Preisliste weitergeben (wie beim gedruckten Katalog).
  Wer die Zugänge zentral sperren oder Logins protokollieren will, braucht später einen kleinen
  Server (gleicher Endpunkt wie für den Bestellversand).

## Bestellportal

Der Bestellkorb liegt im Browser des Besuchers (localStorage). Das Portal prüft Mindestmengen je
Weingut, zeigt den Palettenstand (600 Flaschen) und schätzt Fracht nach den Katalogstaffeln.
Beim Absenden entsteht ein Bestelltext (Kopieren oder per E-Mail) sowie ein JSON im Format des
Auftragsautomaten (`quelle`, `kunde`, `positionen`). Für den Live-Betrieb fehlt noch der
Server-Endpunkt, der die Bestellung an `info@weinstrasseadolph.de` und in den Eingang des
Auftragsautomaten legt (z. B. kleine Funktion bei Vercel/Netlify oder ein Formular-Dienst).

## Veröffentlichen (wenn freigegeben)

Der Ordner `dist/` ist eine fertige statische Website und läuft auf jedem Hosting:
Vercel, Netlify, GitHub Pages oder per FTP beim bisherigen Hoster. Empfehlung: Vercel wie bei
wine-of-excellence.de, mit automatischem Build (`python3 website/build.py`, Ausgabeordner
`website/dist`). Vor dem Umschalten der Domain: Weiterleitungen der alten URLs (Weingut-Pfade sind
bewusst identisch zu heute), Impressum/Datenschutz einhängen, Kontakt- und Bestellformular an den
Server anbinden.
