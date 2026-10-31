# Website-Analyse und Optimierungskonzept: weinstrasseadolph.de/laden/

Stand: 31.10.2026. Gemessen mit Chromium (Desktop 1440 px, Mobil 390 px) und Roh-HTML.

## 1. Ist-Zustand in Zahlen

| Messwert | Ist | Zielwert |
|---|---|---|
| Übertragene Daten (eine Seite) | 7,4 MB | unter 1,5 MB |
| davon Bilder | 4,8 MB (JPG, nicht responsiv) | WebP/AVIF, `srcset`, Lazy Loading |
| Geladene Ressourcen | 70 | unter 30 |
| Eingebundene Skripte | 14 (jQuery 2×, anime.js, velocity, fotorama, string.js, Mailjet 2×, CCM19) | 1 bis 3 |
| `<h1>`-Überschriften | 11 | 1 |
| Bilder ohne Alt-Text | 11 von 12 im HTML | 0 |
| `lang`-Attribut | fehlt | `lang="de"` |
| Seitenhöhe Desktop / Mobil | 14.600 px / 25.300 px | Mobil unter 12.000 px |
| Ladezeit bis Netz ruhig (Mobil, Container) | 5,6 s | unter 2 s |

Technik: Baukasten "X3 Photo Gallery" (photo.gallery), gedacht für Fotografen-Portfolios. Kein Shop-Modul, kein Produktdatenmodell, kein Warenkorb.

## 2. Was gut ist

* Title, Meta-Description, Viewport und Canonical sind gesetzt.
* Klare Markenfarbe (Bordeaux `#5b141b`), Playfair Display als Serif passt zur Marke.
* Inhalte sind vorhanden und sympathisch geschrieben ("Veedel", "Wein-Taxi").
* Kölner Lokalbezug (Ulrepforte, Linien 15/16, Parkplätze) ist ein echter Standortvorteil.

## 3. Schwachstellen

### Technik und Performance
1. 4,8 MB Bilder ohne `srcset`, ohne `loading="lazy"`, ohne modernes Format. Auf dem Handy wird die Desktop-Galerie in voller Größe geladen.
2. 14 Skripte, davon jQuery doppelt und Mailjet doppelt. Drei Animationsbibliotheken (anime, velocity, fotorama) für wenig sichtbaren Effekt.
3. Cookie-Banner (CCM19) überdeckt beim ersten Besuch die komplette Seite und liegt über einem Event-Popup. Zwei Overlays übereinander: der Besucher muss zweimal wegklicken, bevor er etwas sieht.
4. Elf `<h1>`. Suchmaschinen können das Hauptthema der Seite nicht erkennen.
5. `lang`-Attribut fehlt (Screenreader, Google-Spracherkennung).
6. Keine strukturierten Daten (Schema.org `LocalBusiness`/`WineStore` mit Öffnungszeiten, Adresse, Geo). Damit erscheinen Öffnungszeiten nicht direkt in Google.

### Inhalt und Nutzerführung
7. Tote Links: Newsletter-Buttons (`#`, `javascript:void(0)`), Eventbrite-Link der Versteigerung (`#`).
8. Widerspruch: Seminar 19.11. steht als "Ausverkauft", der Text fordert weiter zur Anmeldung auf.
9. Zwei Domains für denselben Laden (`weinstrasseadolph.de/laden/` und `weinstrasse.koeln`); der Link "Laden" auf der Startseite führt zur zweiten Domain. Das teilt die Google-Sichtbarkeit.
10. Zwei Zielgruppen auf einer Domain: Startseite B2B (Fachhandel), /laden/ B2C. Ein Privatkunde, der "Weinladen Köln" sucht, landet auf einer Importeurs-Seite.
11. Keine Preise, keine Bestellmöglichkeit. Das Lastenrad wird nur erwähnt ("Details auf Anfrage per E-Mail oder Telefon"). Die stärkste Idee der Seite hat keinen Button.
12. Dreimal "Werte Wein-Genießer und Wissbegierige aufgepasst", Newsletter-Text doppelt.
13. Kleine Fehler: "Falsaff" statt "Falstaff" (Startseite), "seit 1980" vs. "über 50 Jahre Erfahrung", abgeschnittene Überschrift "Champagne M. Hostomme:".
14. Mobil: 25.000 px Scrollhöhe, Galerie und Texte ohne Priorisierung.

## 4. Was der Prototyp (`index.html`) anders macht

Designprinzipien im Stil von Apple: eine Aussage pro Abschnitt, viel Weißraum, große Serifen-Headline, Bewegung nur als Antwort auf Scrollen, Glas-Navigation, abgerundete Karten mit weichem Schatten.

| Element | Umsetzung |
|---|---|
| Hero | Vollbild, Headline mit Verlaufs-Italic, schwebende Flaschen (SVG), Parallax und Zoom beim Scrollen, Hintergrund-Glow |
| Navigation | Fixiert, beim Scrollen Milchglas (`backdrop-filter`), Warenkorb-Button mit Zähler-Animation |
| Kennzahlen | Zähler laufen hoch, wenn sie ins Bild kommen |
| Abschnitte | `IntersectionObserver`-Reveal mit gestaffelter Verzögerung, Karten heben sich beim Hover |
| Lastenrad | Scrollytelling: linke Seite bleibt stehen (sticky), ein SVG-Lastenrad fährt beim Scrollen auf einer Kurve durch die Südstadt-Silhouette, Räder drehen sich, Stationen leuchten auf, rechts wechseln die vier Schritte |
| Shop | Filter-Chips, Produktkarten mit Grundpreis je Liter (PAngV), "In den Korb" mit Bestätigung |
| Warenkorb | Seitlich einfahrende Lade, Fortschrittsbalken "noch n Flaschen bis zur Lastenrad-Lieferung" |
| Checkout | 4 Schritte: Lieferart (Lastenrad / Abholung / Versand), PLZ-Prüfung mit zwei Zonen (Kerngebiet kostenlos, Rand 4,90 €), Zeitfenster (Werktage, Feierabend-Slot donnerstags, Bestellschluss 12 Uhr), Daten mit 18+-Bestätigung und AGB, Übersicht mit "Zahlungspflichtig bestellen" (Button-Lösung nach § 312j BGB) |
| Barrierefreiheit | `lang="de"`, eine `<h1>`, Alt-Texte/`aria-label`, Fokus-Styles, `prefers-reduced-motion` schaltet alle Animationen ab |
| Gewicht | 68 KB HTML inklusive CSS, JS und allen Grafiken; keine Bibliothek, zwei Google-Fonts |

Der Prototyp läuft ohne Server: Warenkorb im `localStorage`, Bestellung endet mit Bestellnummer und kopierbarem Bestelltext. Preise, Jahrgänge und das Liefergebiet sind Beispielwerte.

## 5. Shop-Konzept Lastenrad (Vorschlag für den Betrieb)

* **Mindestmenge** 12 Flaschen gemischt, wie heute kommuniziert. Der Fortschrittsbalken im Warenkorb erhöht den Bon.
* **Zonen** über Postleitzahlen: Kerngebiet (50677, 50676, 50678, 50674, 50667, 50668, 50672) kostenlos, Randgebiet (50670, 50679, 50968, 50969, 50931 bis 50939, 50996) 4,90 €. Liste in einer Konfigurationsdatei pflegen, nicht im Code.
* **Zeitfenster** vormittags 10 bis 13, nachmittags 14 bis 17, donnerstags 17 bis 19 Uhr. Kapazität je Fenster begrenzen (z. B. 6 Touren).
* **Bestellschluss** 12 Uhr für den nächsten Werktag.
* **Übergabe** nur an Volljährige mit Ausweisprüfung (Jugendschutzgesetz § 9). Im Checkout Häkchen plus Hinweis, bei Übergabe Kontrolle.
* **Zahlung** bei Übergabe (Karte, bar) oder vorab PayPal/Kreditkarte. Für Paketversand nur Vorkasse-Verfahren.
* **Anbindung Lexware**: Eine Bestellung erzeugt eine E-Mail an laden@ und eine JSON-Datei; daraus kann der vorhandene Auftrags-Automat den Beleg in Lexware anlegen (Kunde anlegen oder zuordnen, Artikel per Artikelnummer, VK Preisgruppe 1). Der Artikelstamm liefert umgekehrt Preise und Bestände an den Shop (CSV-Export, täglich).

## 6. Pflicht fürs Live-Gehen (Rechtliches, Deutschland)

1. Impressum, Datenschutzerklärung, AGB, Widerrufsbelehrung mit Muster-Widerrufsformular.
2. Button-Lösung: letzter Button heißt "Zahlungspflichtig bestellen", direkt darüber die Bestellübersicht mit Gesamtpreis.
3. Preisangabenverordnung: Endpreis inkl. MwSt., Grundpreis je Liter, Versandkosten vor Bestellabschluss.
4. Jugendschutz: Altersabfrage im Checkout, Prüfung bei Übergabe, bei Paketversand "Alterssichtprüfung 18+" des Paketdienstes.
5. Lebensmittelinformations-Verordnung für Wein: Alkoholgehalt, Allergene (Sulfite), Herkunft, Nennfüllmenge je Produkt; seit Dezember 2023 zusätzlich Zutaten und Nährwerte (auch per Link/QR zulässig).
6. Cookie-Banner nur, wenn tatsächlich einwilligungspflichtige Dienste laufen. Ohne Tracking reicht ein Hinweis; das aktuelle Doppel-Overlay entfällt.
7. Verpackungsgesetz: Registrierung bei LUCID, wenn Versandkartons in Verkehr gebracht werden.

## 7. Umsetzungswege

| Weg | Aufwand | Passt wenn |
|---|---|---|
| **A. Eigene Seite wie der Prototyp + Shop-Backend** (z. B. Medusa, Saleor, oder ein schlanker Node/Python-Dienst mit Stripe/PayPal) | mittel bis hoch | volle Design-Freiheit, enge Lexware-Kopplung gewünscht |
| **B. Shopify mit Theme nach Prototyp-Vorgabe** | mittel | schnell live, Zahlungen, Rechtstexte, Versand fertig; Lastenrad-Zonen über Shopify "Local Delivery" mit PLZ-Liste und Zeitfenster-App |
| **C. WooCommerce (WordPress)** | mittel | eigener Server gewünscht, viele Plugins für Lieferzonen und Alterscheck |
| **D. Shop-Widget im bestehenden X3-Baukasten** (Ecwid/Shopify Buy Button) | gering | Übergangslösung; Design des Prototyps nicht erreichbar |

Empfehlung: **B für den Shop** (Shopify Local Delivery deckt PLZ-Zonen, Mindestbestellwert und Zeitfenster ab) und die **Laden-Seite als eigene Domain `weinstrasse.koeln`** mit dem Prototyp-Design; die Fachhandels-Seite bleibt auf weinstrasseadolph.de. Beide verlinken sich gegenseitig. Danach 301-Weiterleitungen von `/laden/*` auf die neue Domain.

## 8. Sofortmaßnahmen auf der bestehenden Seite (ohne Neubau)

1. Bilder auf WebP umstellen und auf 1600 px begrenzen (spart rund 4 MB).
2. jQuery- und Mailjet-Doppelungen entfernen.
3. `<h1>` auf eine reduzieren, Rest `<h2>`/`<h3>`.
4. `lang="de"` setzen, Alt-Texte ergänzen.
5. Tote Links reparieren oder entfernen, "Ausverkauft"-Text korrigieren, "Falsaff" korrigieren.
6. Schema.org `WineStore` mit Öffnungszeiten eintragen.
7. Lastenrad-Abschnitt mit Button "Lieferung anfragen" versehen (Formular mit PLZ, Flaschenzahl, Wunschtag), bis der Shop steht.
