# Recherche: Sendungsverfolgung bei Hillebrand Gori für den Auftrags-Automaten

Stand: 18.10.2026. Zweck: Grundlage für die Module `tracking/` (Hillebrand-Status aus Mails
und Portal), `kommunikation/eingang.py` und `ueberwachung/` in `docs/AUSBAUSTUFE_2.md`.
Umgebung des Anwenders: Windows-PC, Microsoft 365, Outlook Desktop, Google Chrome, Python 3.11+.

Methodik: 29 Suchanfragen (deutsch und englisch), bevorzugt offizielle Quellen
(hillebrandgori.com, developer.hillebrandgori.com, login.hillebrand.com, playwright.dev,
developer.chrome.com, transporeon.com, developer.okta.com). Zusätzlich wurden gelesen: die im
Developer-Portal eingebettete OpenAPI-Spezifikation (Version 1.2.8, hochgeladen 2026-09-17),
die OpenID-Metadaten des Login-Servers, das öffentliche JavaScript-Bundle des Portals
my.hillebrandgori.com (nur zur Feststellung von Login-Verfahren, Routen und akzeptierten
Suchbegriffen; kein Login, keine Daten) sowie Playwright 1.64.0 (installiert, Signaturen und
CLI-Hilfe geprüft). Nichts wurde eingeloggt, gebucht oder verändert.

Kennzeichnung in diesem Dokument:

* **Verifiziert (Quelle)** = direkt in einer genannten Quelle gelesen oder technisch geprüft.
* **Annahme** = plausible Ableitung, nicht belegt; dazu jeweils eine Prüfaufgabe.

---

## Zusammenfassung

1. **Es gibt eine offizielle, dokumentierte REST-API** (`https://api.hillebrandgori.com/`,
   Developer-Portal `https://developer.hillebrandgori.com/`), mit Endpunkten für Sendungen,
   Ereignisse (Meilensteine), Dokumente, Rechnungen und Webhooks. Zugang: myHillebrandGori-
   Benutzerkonto + API-Key (manuelle Freigabe durch Hillebrand). Suche nach Referenz ist
   vorgesehen (`GET /v6/shipments?references=…`). Das ist der **empfohlene Weg** für die
   Automatisierung, deutlich vor Portal-Scraping. *(Verifiziert, Abschnitt 1.6)*
2. **Login des Portals läuft über Okta** (`login.hillebrand.com`, Okta Sign-In Widget 7.50.0,
   Benutzername + Passwort, Selbstbedienung für Passwort-Reset und Entsperren). Ob für das
   Konto des Anwenders MFA erzwungen wird, ist ohne Login nicht feststellbar. *(Verifiziert
   bzw. Prüfaufgabe, Abschnitt 1.1)*
3. **Das vorhandene Chrome-Standardprofil kann nicht automatisiert werden.** Seit Chrome 136
   ignoriert Chrome Remote-Debugging auf dem Standard-Datenverzeichnis; Playwright
   dokumentiert das ausdrücklich als nicht unterstützt. Der Automat braucht ein **eigenes
   Profilverzeichnis** (einmaliger manueller Login im sichtbaren Browser). Das Design in
   `AUSBAUSTUFE_2.md` (Abschnitt 2.5, `settings.tracking.chrome_profil` = vorhandenes Profil)
   ist entsprechend anzupassen. *(Verifiziert, Abschnitt 3.1)*
4. **Öffentliche Track&Trace-Seite ohne Login existiert**: `https://my.hillebrandgori.com/public-tracking/<Suchbegriff>`
   (die Seite "Track a shipment" von hillebrandgori.com leitet auf `my.hillebrand.com/public-tracking`
   um). Akzeptierte Suchbegriff-Typen laut Code: Sendungsreferenz, B/L-Nummer, Containernummer.
   Die Domain `my.hillebrand.com` ist mit einem Cloudflare-Challenge geschützt (curl → 403).
   *(Verifiziert, Abschnitt 1.4)*
5. **"laut Transporeon"** bedeutet: Der Empfänger (z. B. Norma-Zentrallager) betreibt
   Transporeon-Zeitfenstermanagement; die Spedition (Hillebrand) bucht dort das Anlieferfenster.
   Für den Versender gibt es **keine dokumentierte eigene Einsicht**; Transporeon ist ein
   geschlossenes Netz, Sichtbarkeit haben Verlader-Kunde (= Empfänger) und ausführender
   Spediteur. *(Verifiziert für die Rollen, Annahme für die Ableitung; Abschnitt 2)*
6. **Keine mobile App** von Hillebrand Gori gefunden (iTunes-Suche und Google-Play-Suche ohne
   Treffer, offizielle Seiten erwähnen keine App). Das Portal ist eine responsive Web-App.
   *(Verifiziert, Abschnitt 1.5)*
7. **Inhalt und Format der Meilenstein-Mails** sind öffentlich nicht dokumentiert. Belegt ist
   nur: Benachrichtigungen je Meilenstein per In-App, E-Mail oder SMS, einzeln schaltbar. Die
   API liefert aber die vollständige Meilenstein-Liste und Textvorlagen (z. B.
   "Shipment {{shipment}} has departed from {{shipFromLocation}} on {{shipFromDate}}").
   Beispiel-Mails muss der Anwender liefern. *(Verifiziert/Annahme, Abschnitte 1.2 und 4)*
8. **Empfehlung**: Mail-Auswertung (primär, sofort) + offizielle API (sekundär, nach Freigabe des
   API-Keys) + Playwright-Portal nur als Rückfallebene und nur mit eigenem Chrome-Profil,
   sichtbar, mit Screenshots und niedriger Frequenz. *(Abschnitt 5)*

---

## 1. myHillebrandGori (Kundenportal)

### 1.1 Login-URL und Login-Verfahren

**Verifiziert (hillebrandgori.com, HTTP-Weiterleitungen):**

* Die Website führt unter "Log in" drei Ziele: "myHillebrandGori"
  (`https://www.hillebrandgori.com/login/myhillebrandgori`), "SCM" (`/login/scm`) und
  "Vancouver Warehouse" (`/login/connexion`). Die Login-Seite der Website enthält selbst kein
  Formular. (Quelle: https://www.hillebrandgori.com/login)
* `https://www.hillebrandgori.com/login/myhillebrandgori` antwortet mit **301 → `https://my.hillebrandgori.com/`**.
  Dort läuft eine Angular-Single-Page-App mit Titel "myHillebrandGori".
  Registrierung: `https://my.hillebrandgori.com/signup` (Quelle: FAQ und Support-Seite).
* Die FAQ nennt die Login-Seite auch als "login.hillebrand.com" (Artikel "Report feature").

**Verifiziert (login.hillebrand.com, OpenID-Metadaten und Sign-In-Seite, abgerufen 18.10.2026):**

* `https://login.hillebrand.com/` liefert eine Seite mit Titel **"myHillebrandGori - Sign In"**,
  die das **Okta Sign-In Widget 7.50.0** lädt (Assets von `eu1static2.oktacdn.com`). Das
  Portal ist also an **Okta** als Identity-Provider angebunden.
* Widget-Konfiguration (aus der Seite, `oktaData.signIn.features`):
  `registration:false`, `rememberMe:false`, `emailRecovery:true`, `smsRecovery:false`,
  `callRecovery:false`, `selfServiceUnlock:true`, `idpDiscovery:true`, `webauthn:true`,
  `autoPush:true`, `multiOptionalFactorEnroll:true`, `deviceFingerprinting:true`,
  `isBotProtectionEnabled:false`. Platzhalter: "Username", "Password", "Forgot password?",
  "Unlock account?".
* OpenID-Konfiguration `https://login.hillebrand.com/oauth2/aus95hq7r8iIqp14M0i7/.well-known/openid-configuration`:
  `authorization_endpoint = …/v1/authorize`, `token_endpoint = …/v1/token`,
  `grant_types_supported = authorization_code, implicit, refresh_token, password, device_code`,
  `code_challenge_methods_supported = S256`, Scopes u. a. `openid, profile, email, offline_access`.
* Das SPA-Bundle enthält eine `oktaUtility` mit `getCodeVerifier`, `getLocalToken`,
  `refreshAccessToken`, `goToLoginPage`, `goToLogoutPage`. Tokens werden lokal im Browser
  gehalten und per `Authorization: Bearer …` an die API gesendet.

**Annahmen:**

* Der Browser-Login ist ein OAuth-2.0-Authorization-Code-Flow mit PKCE (Umleitung
  my.hillebrandgori.com → login.hillebrand.com → zurück). Abgeleitet aus `getCodeVerifier` und
  `code_challenge_methods_supported=S256`; nicht durch einen echten Login beobachtet.
* **MFA**: Das Widget ist MFA-fähig (`multiOptionalFactorEnroll`, `autoPush` = Okta Verify Push,
  `webauthn`). Ob die Okta-Richtlinie für das Konto des Anwenders einen zweiten Faktor
  **erzwingt**, ist ohne Login nicht erkennbar. `idpDiscovery:true` bedeutet, dass je nach
  E-Mail-Domain ein anderer IdP (z. B. SSO eines Konzerns) gewählt werden kann; für externe
  Kunden ist Benutzername/Passwort anzunehmen.
* **Sitzungsdauer**: Das SPA erneuert Access-Tokens per Refresh-Token; wie lange eine Okta-Sitzung
  bzw. der Refresh-Token gültig bleibt (Tage? Wochen?), ist nicht dokumentiert. Das bestimmt, wie
  oft ein manueller Re-Login im Automations-Profil nötig ist.
* **Kontozugang**: Ein Suchtreffer behauptet, Zugangsdaten setzten einen Vertrag mit Hillebrand
  Gori voraus; auf der gelesenen FAQ-Seite steht das nicht. Da die Firma bereits Kunde ist, ist
  das praktisch irrelevant, aber: Der Anwender braucht ein Konto für **seine** Partei
  (Weinstrasse Agentur / Partnerfirmen), damit die Sendungen "seiner" Referenzen sichtbar sind.

**Prüfaufgaben für den Anwender:**

1. Einmal im normalen Chrome unter `https://my.hillebrandgori.com/` einloggen und notieren:
   Wird nach dem Passwort ein zweiter Faktor verlangt (Push, App-Code, E-Mail-Code, WebAuthn)?
   Wie lange bleibt man eingeloggt (nach 1 Tag / 1 Woche noch angemeldet)?
2. Screenshot der Login-Seite und der ersten Seite nach dem Login (Sendungsliste).

### 1.2 Funktionen Sendungsverfolgung, Dokumente, Benachrichtigungen

**Verifiziert (hillebrandgori.com Support-Seite `/support/myhillebrandgori`, FAQ `/faq/myhillebrandgori`, Blog-Artikel):**

* Tracking: "Track shipments from order through to delivery with milestone visibility, routing
  updates and detailed shipment data." FAQ: Nach dem Login sieht man jede Sendung "with live data
  and an interactive route map"; es gibt "real-time location tracking, routing, and timelines".
* Benachrichtigungen: "Alerts can be shared by in-app message, email or SMS." FAQ:
  Meilenstein-Benachrichtigungen gehen an das gewählte Gerät, "You can turn them on and off for
  each milestone". Einstellung: Konto-Symbol oben rechts → "Notification preferences" (Artikel
  "Container Loading and shipment pick-up task feature").
* Dokumente: Sendungsdokumente speichern/teilen; Entwürfe von Bills of Lading prüfen und
  freigeben. Rechnungen je Sendung einsehen, in einigen Ländern online bezahlen; Rechnungssuche
  nach "invoice number or PO reference" (Artikel "Online invoices and payments feature").
* Berichte: Berichtstypen Sendungen, Rechnungen, Emissionen, Bestände, Container; Inhalte u. a.
  "Loading/pick-up dates and times", "Estimated arrival and delivery dates"; Berichte können
  "previewed, exported, shared, or scheduled" werden, auch regelmäßig per E-Mail an Kollegen.
  Exportformat (CSV/Excel) ist **nicht** genannt (Artikel "Report feature", 12.07.2024).
* Aufgaben ("Tasks") für Lieferanten/Exporteure: "Loading and pick-up task" – nach Verladung
  oder Abholung bestätigt der Lieferant Datum/Uhrzeit, Inhalt, Container-/Siegelnummer, lädt
  Dokumente hoch; "Review the shipment reference and the planned contents shown in the task"
  (Artikel 26.04.2024 und 27.08.2026). Relevant, weil die Firma gegenüber Hillebrand die
  Rolle "Supplier/Exporter" hat.
* Sprachen: 10 – "English, Dutch, Spanish, Italian, French, Portuguese, Japanese, Korean,
  Simplified Chinese, and Russian". **Deutsch ist nicht dabei** → Selektoren im Portal auf
  englische Texte auslegen.
* Weitere Funktionen laut Support-Seite: Angebote, Bestandsübersicht, Container-Lieferslots,
  Detention/Demurrage-Überwachung, "APIs allow secure data exchange between customer systems and
  Hillebrand Gori".

**Verifiziert (Public-Tracking-Modul des SPA, Übersetzungsschlüssel):** Die Tracking-Ansicht
hat die Textbausteine `Track.Details.TrackingInfo.Status/Origin/Destination/ContainerNo/
EquipmentType`, `Track.Details.Title.TitleShipmentRef`, `Track.Details.Title.TurnNotificationsOn`,
`Track.Home.FollowJourney`, `Track.Home.NotifyMilestones`, eine Timeline-Komponente
(`jfh-tracking-timeline`) und einen Google-Maps-Dienst (Karte mit "predicted route").

**Annahme:** Inhalt der Meilenstein-E-Mails. Es gibt keine öffentlichen Beispiele. Die API-
Ereignisse haben Textvorlagen wie "Shipment {{shipment}} has departed from {{shipFromLocation}}
on {{shipFromDate}}" (OpenAPI `EventSummary.template`); es ist plausibel, dass die E-Mails
dieselben Texte verwenden. Absenderadresse, Betreffmuster und ob die Hillebrand-Referenz im
Betreff steht, sind **nicht belegbar** → Abschnitt 4 und Checkliste.

### 1.3 Suche nach Referenz: welche Referenzen funktionieren?

**Verifiziert (OpenAPI 1.2.8):**

* `GET /v6/shipments` hat den Query-Parameter `references` (Array): "Search for 1 or multiple
  reference(s) equal to criteria. References can be customer references, container numbers,
  booking numbers and B/L references as well."
* Ein `Shipment` enthält `references` und `otherReferences` als Liste von
  `{referenceType, referenceValue}`; Beispiel im Schema: `referenceType = "File"`,
  `referenceValue = "SET-US-1808-7116"`. In `ShipmentSummary.references` stehen "All references
  the customer is authorized for."
* `BookingInfo.number` = Buchungsnummer der Hauptstrecke, `billOfLadingNumbers`, `carrier`,
  `shippingLine`.

**Verifiziert (Public-Tracking-Modul):** Suchtyp-Enum `searchParameterType =
shipmentReference | blNumber | containerNumber`; die Suche wird als Parameter `searchterm` an
den Tracking-Dienst übergeben.

**Annahme:** Die Hillebrand-Bestätigung "Our Ref: DEMB43262[jobDEM36983721]" enthält zwei
Nummern. `DEMB43262` ist vermutlich die Akten-/Sendungsreferenz (Typ "File") und
`jobDEM36983721` die interne Job-Nummer. Welche der beiden (und ob die Kundenreferenz "Your
Ref") in Portal, Public Tracking und API trifft, ist nicht belegt.

**Prüfaufgabe:** Im eingeloggten Portal sowie unter
`https://my.hillebrandgori.com/public-tracking/<Wert>` nacheinander `DEMB43262`,
`DEM36983721`, `jobDEM36983721` und die eigene Kundenreferenz eingeben; notieren, welcher Wert
die Sendung findet. Dieser Wert wird `Sendung.referenz` im Modul `tracking/`.

### 1.4 Öffentliche Track&Trace-Seite ohne Login

**Verifiziert:**

* `https://www.hillebrandgori.com/tools/track-a-shipment` ("Track a shipment" im Hauptmenü)
  antwortet mit **301 → `https://my.hillebrand.com/public-tracking`**.
* `https://my.hillebrand.com/public-tracking` liefert an curl (auch mit Browser-User-Agent) einen
  **Cloudflare-Challenge ("Just a moment…", HTTP 403)**. Dieselbe App ist unter
  `https://my.hillebrandgori.com/public-tracking` erreichbar (HTTP 200, SPA-Shell). Auch diese
  Seite bindet das Cloudflare-Skript `/cdn-cgi/challenge-platform/scripts/jsd/main.js` ein
  (Browser-Telemetrie zur Bot-Erkennung).
* Routing im SPA: `{path: "public-tracking", loadChildren: PublicTrackingModule}` mit einem
  **anderen Route-Guard** als die login-pflichtigen Routen (z. B. `billOfLadingReview`,
  `deliveryrequest`); Detailroute `/public-tracking/:id` (`onSearch(n) → navigate(["/public-tracking", n])`).
  Die Seite zeigt Timeline, Status, Herkunft, Ziel, Containernummer, Equipment-Typ und eine
  Karte sowie einen "Get notified"-Aufruf, der zur Registrierung führt.

**Annahme:** Die öffentliche Seite zeigt nur einen reduzierten Datensatz (Meilenstein-Timeline,
keine Dokumente, keine Benachrichtigungen ohne Konto). Ob sie für Lkw-Sendungen innerhalb
Europas (kein Container, keine B/L) über die Sendungsreferenz etwas findet, ist offen.

**Prüfaufgabe:** Im normalen Browser `https://my.hillebrandgori.com/public-tracking/DEMB43262`
aufrufen, Screenshot; dann dieselbe URL mit `jobDEM36983721`.

### 1.5 Mobile App

**Verifiziert (18.10.2026):** iTunes-Such-API (`term=hillebrand gori`, `term=myhillebrand`)
liefert keine App von Hillebrand Gori; die Google-Play-Suche "hillebrand gori" ebenfalls nicht.
Keine der offiziellen Support-/FAQ-Seiten erwähnt eine App. Das Portal ist eine responsive
Web-App (Viewport-Meta, Apple-Touch-Icons). Für die Automatisierung ist das ohne Belang.

### 1.6 API, Datenexport, EDI, Webhooks

**Verifiziert (Developer-Portal `https://developer.hillebrandgori.com/`, eingebettete OpenAPI
3.0.0, `info.version = 1.2.8`, "last uploaded on 2026-09-17"):**

* Basis-URL: `https://api.hillebrandgori.com/`. Rate-Limit: "The APIs can be called up to 100
  times per minute." Token-Gültigkeit: "Token expiration is 2 hours". Zeitstempel: Unix-Sekunden.
* **Zugang (3 Pflichtschritte):**
  1. "you need to have both a myHillebrandGori user account aswell as an API key." Konto:
     `https://my.hillebrandgori.com/signup`; API-Key: `https://developer.hillebrandgori.com/Register`.
     "Note that we need to authorise your request first, which is a manual process." Der API-Key
     kommt per Bestätigungs-E-Mail.
  2. Token holen: `POST https://login.hillebrand.com/oauth2/aus95hq7r8iIqp14M0i7/v1/token`,
     Header `Authorization: Basic base64(apikey:apisecret)`,
     `Content-Type: application/x-www-form-urlencoded`, `Accept: application/json`, Body
     `grant_type=password&username=[username]&password=[password]&scope=offline_access`.
     Antwort enthält `access_token` und `refresh_token`.
  3. API-Aufruf mit `Authorization: Bearer <access_token>`, z. B. `GET https://api.hillebrandgori.com/v6/shipments`.
  4. Erneuerung: `grant_type=refresh_token&refresh_token=[…]&scope=offline_access`.
* **Endpunkte (Auszug):** `GET /v6/shipments` (Parameter `references`, `status`, `shipDateFrom/To`,
  `arrivalDateFrom/To`, `fromCountries`, `toCountries`, `onTimeStatus`, `modifiedSinceTimeStamp`,
  `includeInactiveShipments`, `pageNumber`, `pageSize`, `sortBy`); `GET /v6/shipments/{id}`;
  `…/{id}/events` (Parameter `lastUpdateAfter`, `includeCodes`, `excludeCodes`);
  `…/{id}/legs`, `…/{id}/locations`, `…/{id}/documents` (Dokumenttypen u. a. CBL, HBL, CDO,
  CIN, INC, PLI, LOL, **RDR Readiness receipt**, **LCR Loading confirmation receipt**),
  `…/{id}/tasks` (Typen `shprdy`, `blrev`; Status `open`/`completed`, Filter `overdue`),
  `…/{id}/contacts`, `…/{id}/productlines`, `…/{id}/insurances`; `GET /v1/invoices…`;
  `GET /v1/contacts/{id}`, `GET /v1/parties/{id}`; Webhooks `GET/POST/PUT/DELETE /v1/webhooks/me`,
  `GET /v1/webhooks/me/eventTypes`.
* **Sendungsstatus (`shipmentStatusEnum`):** `notReady, ready, collected, loaded, shipped,
  arrived, unloaded, delivered`. `onTimeStatus`: `onTime | late`. `progress` 0–100.
  Felder u. a. `estimatedDepartureDate`, `actualDepartureDate`, `estimatedArrivalDate`,
  `actualArrivalDate`, `shipFromPartyName`, `shipToPartyName`, `soldBy`, `soldTo`,
  `readyDate`, `lastEvent`, `mainModality` (u. a. `truck`), `modifiedTimeStamp`.
  Hinweis "IF shipmentStatus = (Arrived OR Delivered) AND latest date more than 1 month ago …
  THEN The shipment will be marked as Inactive" (Parameter `includeInactiveShipments`).
* **Ereignisse (`EventSummary`):** `code`, `category`, `date`, `lastUpdate`, `text` (Beispiel
  "Shipment FR99021 has departed from Singapore on 2018-05-22"), `template`, `templateVariables`.
* **Webhook-Ereignistypen** (new = Datum erstmals erfasst, chg = Datum geändert). Für Lkw-
  Transporte ab Weingut zum Kunden sind die wichtigsten:

  | Code | Bedeutung |
  |---|---|
  | `shprdy` / `shprdychg` | Shipment ready for transport |
  | `shpsupdeppln` / `…chg` | Planned departure ship-from location (geplante Abholung) |
  | `shpsupdepcnf` / `…chg` | Confirmed departure ship-from location (Abholung bestätigt) |
  | `shpcusarrpln` / `…chg` | Planned arrival ship-to location (geplante Zustellung) |
  | `shpcusarrcnf` / `…chg` | Confirmed arrival ship-to location (Zustellung bestätigt) |

  Weitere Codes für Konsolidierungslager, Häfen, Flughäfen, Zoll (`shpimpcsmclr`) sind in der
  Spezifikation gelistet. Webhook-Autorisierung zum Empfänger-Endpunkt: BASIC;
  Hillebrand erwartet HTTP 200 (Empfehlung: sofort 202 senden); bei Nicht-2xx kann das Abo
  deaktiviert werden; Status über `GET /v1/webhooks/me`.
* **EDI:** Nur ein Fallbeispiel (maßgeschneiderte EDI/API-Suite für einen US-Importeur, Artikel
  "The people behind the process: Michele Carta"); keine öffentliche EDI-Spezifikation.
* **CSV/Excel-Export:** Nur über die Berichtsfunktion im Portal ("exported", Format nicht
  genannt) bzw. per geplanten Bericht per E-Mail.

**Diskrepanz (verifiziert):** Das `securitySchemes`-Objekt der Spezifikation nennt als
`tokenUrl` `https://jfhillebrand-test.apigee.net/oauth2/token` (Testsystem), der Text
"Get Started" dagegen `https://login.hillebrand.com/oauth2/aus95hq7r8iIqp14M0i7/v1/token`.
Die Text-URL stimmt mit den OpenID-Metadaten überein (`token_endpoint`) und ist zu verwenden.

**Annahmen zur API:**

* Der Passwort-Grant ("Resource Owner Password") wird von Okta ausdrücklich als "last option"
  bezeichnet; die Okta-Doku sagt nichts dazu, ob er mit erzwungener MFA funktioniert. **Falls für
  das Konto MFA erzwungen ist, kann der API-Login scheitern** → dann bei Hillebrand nach einem
  technischen Benutzer ohne MFA bzw. nach dem vorgesehenen Verfahren fragen.
* Webhooks brauchen einen aus dem Internet erreichbaren HTTPS-Endpunkt. Auf dem Windows-PC des
  Anwenders gibt es den nicht; **Polling** mit `modifiedSinceTimeStamp` (z. B. alle 30 Minuten)
  liegt weit unter 100 Aufrufen/Minute und reicht für Fixterminüberwachung aus.
* Die manuelle Freigabe des API-Keys kann Tage dauern und ist evtl. an einen Ansprechpartner bei
  Hillebrand Germany gebunden.

**Python-Beispiel (abgeleitet aus dem dokumentierten Request-Format; nicht gegen das Live-System
getestet, da kein API-Key vorliegt):**

```python
import base64, time, requests

TOKEN_URL = "https://login.hillebrand.com/oauth2/aus95hq7r8iIqp14M0i7/v1/token"
API = "https://api.hillebrandgori.com"


def hole_token(api_key: str, api_secret: str, benutzer: str, passwort: str) -> dict:
    basic = base64.b64encode(f"{api_key}:{api_secret}".encode()).decode()
    r = requests.post(
        TOKEN_URL,
        headers={"Authorization": f"Basic {basic}",
                 "Content-Type": "application/x-www-form-urlencoded",
                 "Accept": "application/json"},
        data={"grant_type": "password", "username": benutzer,
              "password": passwort, "scope": "offline_access"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()          # enthaelt access_token, refresh_token


def sendungen_nach_referenz(access_token: str, referenz: str) -> list[dict]:
    r = requests.get(
        f"{API}/v6/shipments",
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        params={"references": referenz, "includeInactiveShipments": "true"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json().get("items", r.json())   # Feldname der Liste am Live-System pruefen


def ereignisse(access_token: str, shipment_id: int, seit_unix: int | None = None) -> list[dict]:
    params = {"pageSize": 100}
    if seit_unix:
        params["lastUpdateAfter"] = seit_unix
    r = requests.get(
        f"{API}/v6/shipments/{shipment_id}/events",
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        params=params, timeout=30,
    )
    r.raise_for_status()
    return r.json().get("items", r.json())
```

Der Aufbau der Antwort-Container (`shipmentPagedList_ShipmentSummary_`, `PagedList_EventsSummary_`)
ist in der Spezifikation definiert; der konkrete Listen-Feldname ist beim ersten echten Aufruf zu
prüfen (daher der Fallback im Beispiel). Zugangsdaten gehören in den Windows-Credential-Store
(`keyring`) oder `%APPDATA%`, nie ins Repository.

---

## 2. Transporeon: "laut Transporeon soll am 27.10. angeliefert werden"

**Verifiziert (Transporeon Factsheets und Entwickler-Doku, Kundenschreiben):**

* Transporeon Time Slot Management "bildet die tatsächlichen Be- und Entladekapazitäten im
  System nach und stimmt die Ankunftszeiten der LKW automatisch auf die verfügbaren Ressourcen
  ab" (Factsheet DE). Retail-Variante: "developed to manage deliveries at central warehouses,
  distribution centres or cash & carry depots … Carriers book suitable delivery appointments
  themselves online." Ablauf: "Retailer creates POs → Supplier gets PO, carrier (or supplier)
  books an appointment for the delivery → Carrier provides all relevant logistical information".
  Bestellnummern des Händlers werden importiert und dienen "as a reference for the carrier when
  booking a time slot". Status des gebuchten Fensters und Fortschritt am Liefertag werden "by
  traffic light colours" angezeigt (Factsheet Retail EN).
* Rollen (Transporeon Developer-Wiki "Time Slot Management"): "If a shipper customer uses Time
  Slot Management module, available time slots for a particular transport are visible for the
  executing carriers. Carriers optimize their vehicles usage by booking the best available
  slots." Der "shipper customer" (Vertragspartner von Transporeon, hier der **Empfänger** mit dem
  Zentrallager) "can receive all created, changed and deleted bookings" (API `GET /v2/bookings/pull`).
* Geschlossenes Netz: "Transporeon is a closed-circuit platform. This means that you will first
  need the authorization of at least one shipper customer in order to begin to use the platform."
  Registrierung kostenlos, Gebühren je Transaktion, z. B. "booking a time slot"; Beispiel Wanzl:
  "2,34€ für den jeweils Buchenden des Zeitfensters", Buchung "Kleiner als eine Minute",
  Pflichtangaben: Wareneingang, Referenz-Nr. (Bestellnummern lt. Lieferpapieren), Paletten,
  Fahrername, Kennzeichen, Handy des Fahrers.
* Kein Konto nötig für Einmal-Bucher: Bei Aurubis erhält ein nicht registrierter Kunde per E-Mail
  einen "Quick-Login"-Link, "einmalig für jede generierte Transportnummer 60 Tage" gültig;
  "Die Bestätigung eines Timeslots per E-Mail wird nicht verschickt."
* Lieferantensicht bei einem Händler (Zalando-Partnerdoku): Der Lieferant bucht nicht selbst,
  sondern "your carrier who will be using Transporeon to book delivery slots on your behalf";
  eine Buchungsbestätigung per E-Mail gibt es dort nur bei Umbuchung am selben Tag.

**Annahme (Ableitung für unseren Fall):** Der Satz "laut Transporeon soll am 27.10. angeliefert
werden" in der Hillebrand-Bestätigung bedeutet, dass Hillebrand (als ausführender Spediteur) im
Zeitfenstermanagement des Empfängers (z. B. Norma-Zentrallager) für den 27.10. ein Anlieferfenster
gebucht hat oder zugewiesen bekam. Das Datum stammt also aus der Rampenplanung des Kunden, nicht
aus der Hillebrand-Disposition. Eine Verschiebung dieses Datums ist deshalb eine Terminänderung
beim Kunden (Fixtermin!) und muss in `Fixterminüberwachung.xlsx` als `Terminaenderung`-Vorschlag
landen.

**Einsicht für den Versender:** Nicht dokumentiert und nach dem Rollenmodell nicht vorgesehen
(Sichtbarkeit: Verlader-Kunde = Empfänger und ausführender Spediteur). Der praktikable Weg:
Hillebrand nennt das Zeitfenster in der Auftragsbestätigung (so bereits beobachtet) und wird bei
Änderungen per `nachfrage_spedition`-Entwurf an `def-info@hillebrandgori.com` gefragt. Optional
könnte der Kunde (Norma, SPAR, Rheinberg …) dem Lieferanten Zugang zu seinem Lieferantenportal
geben – das ist eine Frage an den jeweiligen Kunden, nicht an Transporeon.

**Prüfaufgabe:** Den Hillebrand-Disponenten fragen, (a) ob die Zeitfensterbuchung bei Norma/SPAR
standardmäßig in die Auftragsbestätigung geschrieben wird, (b) ob Hillebrand bei Umbuchung des
Fensters proaktiv per Mail informiert und mit welchem Betreff.

---

## 3. Automatisierung mit Playwright (Python) und Google Chrome

### 3.1 Das vorhandene Chrome-Profil darf nicht verwendet werden

**Verifiziert (developer.chrome.com, 17.03.2025):** "From Chrome 136 we're making changes to the
behavior of --remote-debugging-port and --remote-debugging-pipe. These switches will no longer be
respected if attempting to debug the default Chrome data directory. They must now be accompanied
by the --user-data-dir switch to point to a non-standard directory." Grund: App-Bound Encryption;
"A non-standard data directory uses a different encryption key". Für Automatisierung empfiehlt
Google "Chrome for Testing".

**Verifiziert (playwright.dev, `launch_persistent_context`):** "Chromium/Chrome: Due to recent
Chrome policy changes, automating the default Chrome user profile is not supported." Hinweis: das
kann "result in pages not loading or the browser exiting"; Empfehlung "Create and use a separate
directory (for example, an empty folder) as your automation profile instead." Außerdem:
"browsers do not allow launching multiple instances with the same User Data Directory" und
"Chromium's user data directory is the parent directory of the 'Profile Path' seen at
chrome://version".

**Verifiziert (playwright.dev, Browsers):** "Certain Enterprise Browser Policies may impact
Playwright's ability to launch and control Google Chrome and Microsoft Edge."

**Folge für das Design:** `settings.tracking.chrome_profil` muss auf ein **eigenes, anfangs leeres
Verzeichnis** zeigen (Vorschlag `%LOCALAPPDATA%\Auftragsautomat\chrome-profil`), nicht auf
`%LOCALAPPDATA%\Google\Chrome\User Data`. Der Anwender loggt sich in diesem Automations-Profil
einmal manuell ein; Cookies und Okta-Tokens bleiben dort erhalten. Der normale Chrome des
Anwenders bleibt unberührt und kann parallel offen sein (anderes Verzeichnis).

**Annahme:** Ein Kopieren der Cookies aus dem Standardprofil in das Automations-Profil
funktioniert unter Windows nicht, weil die App-Bound-Verschlüsselung an das Standardverzeichnis
gebunden ist (Google Security Blog: "if any other app on the system tries to decrypt the same
data, it will fail"). Nicht selbst getestet; es gibt ohnehin keinen Grund, das zu versuchen.

### 3.2 Aufruf mit persistentem Profil und echtem Chrome

**Verifiziert (Playwright 1.64.0, Signatur geprüft; Doku `class-browsertype`):**
`launch_persistent_context(user_data_dir, *, channel=…, headless=…, args=…, slow_mo=…,
viewport=…, accept_downloads=…, record_video_dir=…, traces_dir=…, timeout=…, …)`.
`channel`: "chrome", "chrome-beta", "chrome-dev", "chrome-canary", "msedge", … für
"branded Google Chrome and Microsoft Edge"; "chromium" für den neuen Headless-Modus des
gebündelten Chromium. `headless` "Defaults to true".

```python
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

PROFIL = Path.home() / "AppData" / "Local" / "Auftragsautomat" / "chrome-profil"
PORTAL = "https://my.hillebrandgori.com/"
LOGIN_HOST = "login.hillebrand.com"


def portal_status(referenz: str, screenshots: Path, sichtbar: bool = True) -> dict:
    PROFIL.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(PROFIL), channel="chrome", headless=not sichtbar,
            viewport={"width": 1400, "height": 900}, slow_mo=50,
        )
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(PORTAL, wait_until="domcontentloaded")
            try:
                page.wait_for_url(lambda u: LOGIN_HOST not in u, timeout=15_000)
            except PWTimeout:
                page.screenshot(path=str(screenshots / "login_erforderlich.png"))
                return {"status": "unbekannt", "grund": "Login erforderlich (manuell im sichtbaren Browser)"}
            # Oeffentliche Tracking-Route nutzen, sobald der Suchbegriff verifiziert ist
            page.goto(f"{PORTAL}public-tracking/{referenz}", wait_until="domcontentloaded")
            page.get_by_role("heading").first.wait_for(timeout=20_000)
            page.screenshot(path=str(screenshots / f"{referenz}.png"), full_page=True)
            texte = page.locator("jfh-tracking-timeline").all_inner_texts()
            return {"status": "gelesen", "rohtext": "\n".join(texte)}
        finally:
            ctx.close()
```

Der Selektor `jfh-tracking-timeline` ist ein im Bundle belegter Angular-Elementname
(verifiziert, Abschnitt 1.4); ob er stabil bleibt, ist eine **Annahme** – deshalb gehören alle
Selektoren in `config/tracking_hillebrand.yaml` und werden mit `aufzeichnen()` am echten Portal
eingestellt. Interaktive Elemente nach Möglichkeit mit `get_by_role(…, name=…)`,
`get_by_label`, `get_by_placeholder`, `get_by_text` ansprechen (Abschnitt 3.5).

### 3.3 Umgang mit MFA und Sitzungsablauf

* **Regel:** Der Automat gibt niemals Passwörter oder Einmalcodes ein. Erkennt er die Login-Seite
  (URL enthält `login.hillebrand.com` oder Überschrift "Sign In"), macht er einen Screenshot,
  meldet `Pruefhinweis(WARNUNG, "Portal-Login erforderlich")` und beendet den Portal-Teil mit
  Status "unbekannt" (wie in `AUSBAUSTUFE_2.md` 2.5 vorgesehen: nie Abbruch des Gesamtlaufs).
* **Manueller Login:** Kommando `auftragsautomat tracking login` startet dasselbe Profil
  sichtbar (`headless=False`) und wartet, bis die URL wieder auf `my.hillebrandgori.com` liegt;
  der Anwender tippt Benutzername, Passwort und ggf. den zweiten Faktor selbst ein. Danach bleibt
  die Okta-Sitzung im Profil.
* **Sitzungsablauf:** unbekannt (Prüfaufgabe 1.1). Die Überwachung sollte den Login-Zustand
  täglich melden, damit der Anwender rechtzeitig neu anmeldet.
* Alternative zum Profil: `storage_state` (Playwright-Doku: `context.storage_state(path="state.json")`,
  Wiederverwendung mit `browser.new_context(storage_state="state.json")`). Nachteil: Die Datei
  enthält Sitzungs-Cookies im Klartext ("may contain sensitive cookies and headers"); Session
  Storage wird nicht gesichert ("Playwright does not provide API to persist session storage").
  Das persistente Profil ist für den Einzelplatz-PC die einfachere und sicherere Wahl.

### 3.4 headless vs. sichtbar, Bot-Erkennung, Nutzungsbedingungen

**Verifiziert:**

* Playwright: Chrome/Edge "switched to a new headless mode implementation that is closer to a
  regular headed mode"; Standard-Headless von Playwright ist der "chromium headless shell".
* `my.hillebrand.com` liefert ohne Browser einen Cloudflare-Challenge (403); das Portal bindet
  Cloudflares Challenge-Platform-Skript ein (Abschnitt 1.4). Die Okta-Seite hat
  `deviceFingerprinting:true`, `isBotProtectionEnabled:false`.
* Nutzungsbedingungen `https://www.hillebrandgori.com/terms-of-use` (Hillebrand Gori Group GmbH,
  Mainz; gelten für "www.hillebrandgori.com" und "other websites owned or controlled" by it):
  Abschnitt 10(6) verbietet "use any robot, spider, or other automatic device, software, or means
  to access, use, or copy the Website"; Abschnitt 6: Login-Daten sind vertraulich zu behandeln,
  unbefugte Nutzung ist sofort zu melden; Gerichtsstand Frankfurt, deutsches Recht. Ob für
  myHillebrandGori gesonderte Plattformbedingungen gelten, steht nicht auf der Seite (die
  Registrierung verlangt eine Zustimmung zu Bedingungen – `agreementFC` im Signup-Formular).

**Einordnung (Annahme):** Ein Playwright-Lauf mit echtem Chrome, eigenem Profil, sichtbarem
Fenster und niedriger Frequenz (wenige Aufrufe pro Tag) ist technisch von normaler Nutzung kaum
unterscheidbar; `navigator.webdriver` ist dennoch gesetzt (WebDriver-Spezifikation) und kann
erkannt werden. Der Wortlaut der Nutzungsbedingungen deckt Portal-Scraping formal nicht. Deshalb:
**API ist der von Hillebrand vorgesehene und sanktionierte Weg**; Portal-Automatisierung nur als
Rückfallebene, mit Wissen des Hillebrand-Ansprechpartners, und keinesfalls headless im
Dauerbetrieb. Keine Stealth-Plugins (Patchright u. ä.) einsetzen – das wäre bewusste Umgehung.

### 3.5 Robuste Selektoren

**Verifiziert (playwright.dev Locators):** Bevorzugt `page.get_by_role("button", name="Sign in")`
("reflects how users and assistive technology perceive the page"), `get_by_label("Password")`,
`get_by_placeholder(…)`, `get_by_text("Welcome, John")` für nicht-interaktive Elemente,
`get_by_test_id`; Eingrenzung mit `.filter(has_text="…")`. CSS/XPath "can be tied to the DOM
structure or implementation" → nur als letzte Wahl. Locators sind standardmäßig strikt (mehrere
Treffer = Fehler; `first/last/nth` bewusst einsetzen) und warten automatisch.

Für das Portal (englische Oberfläche, Angular-Komponenten `jfh-tracking-search`,
`jfh-tracking-timeline`, `jfh-tracking-info-card`, Übersetzungsschlüssel `Track.*`) bedeutet
das: Texte wie "Status", "Origin", "Destination", "Container no." sind die stabilsten Anker;
Elementnamen `jfh-…` als zweite Wahl; generierte Klassen (`_ngcontent-…`) nie verwenden.

### 3.6 Einrichtung durch den Anwender mit `playwright codegen`

**Verifiziert (Playwright 1.64.0, `python -m playwright codegen --help`):** Optionen u. a.
`--target python|python-async|python-pytest` (Standard "python"), `-o <datei>`,
`--channel chrome`, `--user-data-dir <verzeichnis>` ("use the specified user data directory
instead of a new context"), `--save-storage`/`--load-storage`, `--viewport-size`, `--lang`,
`--timezone`, `--timeout`, `--save-har`. Doku-Hinweis zu `--user-data-dir`: Chrome 136+ blockiert
das Standardprofil, eigenes Verzeichnis nötig.

Einrichtungsablauf für den Anwender (Windows, PowerShell):

```powershell
py -m pip install playwright
py -m playwright codegen --channel chrome `
   --user-data-dir "$env:LOCALAPPDATA\Auftragsautomat\chrome-profil" `
   --target python -o aufzeichnung_portal.py https://my.hillebrandgori.com/
```

Im geöffneten Chrome einloggen (Login bleibt im Profil), eine Sendung suchen, die
Meilenstein-Ansicht öffnen; Playwright schreibt die Klicks als Python-Code mit
`get_by_role`/`get_by_text`-Locators nach `aufzeichnung_portal.py`. Der Entwickler überträgt die
Locators in `config/tracking_hillebrand.yaml`. Alternative laut Doku: in einem eigenen Skript mit
persistentem Kontext `page.pause()` aufrufen, dann erscheint der Rekorder im Inspector.

### 3.7 Nachvollziehbarkeit: Screenshots und Trace

**Verifiziert (playwright.dev Screenshots; Signaturen 1.64.0):** `page.screenshot(path=…,
full_page=True)`, Element: `page.locator(…).screenshot(path=…)`; Puffer: `page.screenshot()`.
Trace: `context.tracing.start(screenshots=True, snapshots=True, sources=True)` …
`context.tracing.stop(path="trace.zip")`, Anzeige mit `python -m playwright show-trace trace.zip`.
Empfehlung: je Lauf ein Ordner `logs/tracking/<datum>/<referenz>/` mit `seite.png`, bei Fehlern
zusätzlich `trace.zip`; HTML der Seite nur in `aufzeichnen()` speichern (enthält ggf.
personenbezogene Daten).

---

## 4. Alternative: Bestätigungs- und Meilenstein-Mails von Hillebrand

**Verifiziert:**

* Einziges belegtes Muster ist die vom Anwender beobachtete Auftragsbestätigung:
  Betreff `Auftragsbestätigung; Your Ref: <Kundenreferenz>; Our Ref: DEMB43262[jobDEM36983721]`,
  Text mit Lieferzeitfenster "laut Transporeon". Absender-Postfächer: `def-order@hillebrandgori.com`
  (Aufträge), `def-info@hillebrandgori.com` (Nachfragen) – aus `AUSBAUSTUFE_2.md`.
* Das Portal verschickt auf Wunsch je Meilenstein Benachrichtigungen (In-App, E-Mail, SMS),
  einzeln ein-/ausschaltbar (Abschnitt 1.2). Die Meilenstein-Menge entspricht der Webhook-Liste
  (Abschnitt 1.6): bereit, geplante/bestätigte Abholung, geplante/bestätigte Zustellung usw.
* Rechnungen: im Portal je Sendung einsehbar; Berichte "open & unpaid invoice statements" per
  E-Mail planbar. Ob Rechnungen automatisch per Mail kommen, ist nicht belegt.

**Nicht belegbar (öffentlich keine Beispiele):** Betreffmuster der Abhol-/Zustell-Mails, ob
"Our Ref" immer im Betreff steht, Absenderadresse der Portal-Benachrichtigungen, Sprache
(englisch wahrscheinlich, da das Portal kein Deutsch bietet – Annahme).

**Vorschlag für `tracking/hillebrand_mail.py` (Annahme, aus dem einen Muster abgeleitet):**

```python
import re

RE_OUR_REF = re.compile(r"Our\s*Ref\s*:\s*(?P<datei>DEM[A-Z]?\d+)\s*\[\s*job(?P<job>DEM\d+)\s*\]", re.I)
RE_YOUR_REF = re.compile(r"Your\s*Ref\s*:\s*(?P<kunde>[^;\n]+)", re.I)
RE_TRANSPOREON = re.compile(r"laut\s+Transporeon[^0-9]{0,60}(?P<tag>\d{1,2}\.\d{1,2}\.(?:\d{2,4})?)", re.I)
```

Die Muster sind bewusst tolerant (optionaler Buchstabe nach "DEM", Leerzeichen). Sobald der
Anwender 5–10 echte Mails liefert (Checkliste), werden sie gegen Fixtures getestet.
Ergänzend sollte `eingang.py` auch Mails mit Absender `@hillebrandgori.com` ohne "Our Ref" im
Betreff erfassen und die Referenz im Text suchen.

---

## 5. Empfehlung für unseren Fall

| Stufe | Quelle | Aufwand | Risiko | Nutzen |
|---|---|---|---|---|
| 1 (sofort) | **Mails** von Hillebrand im Postfach (Graph/Outlook) | gering, bereits im Design | keines | Referenz, Zeitfenster, Bestätigungen, Terminänderungen |
| 2 (empfohlen) | **Offizielle API** (`/v6/shipments`, `/events`, `/documents`) mit API-Key | mittel; manuelle Freigabe durch Hillebrand abwarten | gering (vorgesehener Weg); offen: MFA beim Passwort-Grant | strukturierte Meilensteine, geplante/tatsächliche Abhol- und Zustelldaten, Dokumente (z. B. Loading confirmation receipt) |
| 2b (optional) | **Public Tracking** `my.hillebrandgori.com/public-tracking/<ref>` | gering | Cloudflare-Challenge, Nutzungsbedingungen | schnelle Statusprüfung ohne Login, falls die Referenz trifft |
| 3 (Rückfall) | **Portal per Playwright**, eigenes Chrome-Profil, sichtbar | mittel; Einrichtung mit codegen | Bot-Erkennung, ToS, Selektoren brechen, Login/MFA | Vollbild der Sendung inkl. Karte, Screenshots für die Akte |

Konkrete Umsetzung in `AUSBAUSTUFE_2.md`:

1. `tracking/hillebrand_mail.py` bleibt Primärquelle (unverändert).
2. Neues Modul `tracking/hillebrand_api.py` (Token-Handling mit Refresh, Suche nach Referenz,
   Ereignisse seit letztem Lauf, Mapping `shipmentStatusEnum` → Ampel; Konfiguration
   `settings.tracking.api.{api_key, benutzer}` mit Secrets im Credential-Store). Polling im
   täglichen Lauf, optional stündlich; Rate-Limit 100/min ist kein Thema.
3. `tracking/hillebrand_portal.py`: `settings.tracking.chrome_profil` = eigenes Verzeichnis;
   `channel="chrome"`, `headless=False` für Login, für Statusläufe ebenfalls sichtbar (Fenster
   darf minimiert sein); Abbruch mit Status "unbekannt" bei Login-Seite; Screenshots je Lauf.
4. Mapping-Regel in `ueberwachung/`: `shpsupdepcnf` (bestätigte Abholung) → Häkchen "Abgeholt";
   `shpcusarrpln`-Änderung oder neues Zeitfenster "laut Transporeon" → `Terminaenderung`-Vorschlag;
   `onTimeStatus = late` → Ampel rot + `nachfrage_spedition`-Entwurf.

**Was der Anwender liefern muss:**

* Login-Daten **nur lokal**: einmaliger manueller Login im Automations-Chrome-Profil; für die API
  Benutzername/Passwort + API-Key/Secret im Windows-Credential-Store (keine Datei im Repo).
* 5–10 Beispiel-Mails von Hillebrand als `.eml` (Auftragsbestätigung, Abholung/Verladung,
  Zustellung, Terminänderung, Rechnung) – personenbezogene Daten dürfen geschwärzt sein, aber
  Betreff, Absender und Referenzzeilen müssen original bleiben.
* Screenshots des Portals: Login-Seite, Sendungsliste, Detailseite mit Timeline, Seite
  "Notification preferences", Public-Tracking-Ergebnis.
* Eine Beispielreferenz (vollständige Zeile "Our Ref: …") einer Sendung, die aktuell im Portal
  sichtbar ist, plus die zugehörige "Your Ref".
* Antwort auf die Prüfaufgaben (MFA ja/nein, Sitzungsdauer, welcher Suchbegriff trifft).

---

## Checkliste für den Anwender

1. [ ] `https://my.hillebrandgori.com/` im normalen Chrome öffnen, einloggen, notieren: zweiter
   Faktor ja/nein, welcher; Screenshot Login-Seite.
2. [ ] Nach 1 Tag und nach 7 Tagen prüfen: noch eingeloggt? (Sitzungsdauer)
3. [ ] Im Portal nacheinander suchen: `DEMB43262`, `DEM36983721`, `jobDEM36983721`, eigene
   Kundenreferenz → welcher Wert findet die Sendung? Screenshot der Detailseite mit Timeline.
4. [ ] `https://my.hillebrandgori.com/public-tracking/<der gefundene Wert>` ohne Login (anderes
   Browserprofil oder Inkognito) aufrufen → Treffer ja/nein, Screenshot.
5. [ ] Konto-Symbol → "Notification preferences": Screenshot; E-Mail-Benachrichtigung für
   Abholung und Zustellung testweise einschalten; die eintreffenden Mails als `.eml` sichern.
6. [ ] 5–10 Hillebrand-Mails aus den letzten Monaten als `.eml` in `tests/fixtures/mails/hillebrand/`
   ablegen (Auftragsbestätigung, Abholung, Zustellung, Terminänderung, Rechnung).
7. [ ] API-Zugang beantragen: `https://developer.hillebrandgori.com/Register` (API-Key) – zuvor
   mit dem Hillebrand-Ansprechpartner (Hillebrand Gori Germany, Mainz) klären, dass der Zugang für
   Sendungsstatus genutzt wird und ob ein technischer Benutzer ohne MFA vorgesehen ist.
8. [ ] Hillebrand fragen: Wird das Transporeon-Zeitfenster bei Umbuchung proaktiv gemeldet? Mit
   welchem Betreff? An welches Postfach gehen Statusfragen (`def-info@`)?
9. [ ] `py -m playwright install chrome` ist **nicht** nötig (vorhandener Chrome wird per
   `channel="chrome"` genutzt); `py -m pip install playwright` genügt. Einmal
   `py -m playwright codegen --channel chrome --user-data-dir "%LOCALAPPDATA%\Auftragsautomat\chrome-profil" https://my.hillebrandgori.com/`
   ausführen, einloggen, eine Sendung öffnen, Fenster schließen, erzeugte Datei an den Entwickler.
10. [ ] Prüfen, ob eine Unternehmensrichtlinie (Gruppenrichtlinie) den Chrome-Start mit fremdem
    `--user-data-dir` blockiert (Playwright-Doku warnt vor Enterprise-Policies).

---

## Quellen

Hillebrand Gori (offiziell)

* https://www.hillebrandgori.com/support/myhillebrandgori – Plattformbeschreibung, Login-/Signup-Links, Benachrichtigungskanäle, API-Hinweis
* https://www.hillebrandgori.com/faq/myhillebrandgori und https://www.hillebrandgori.com/faq/myhillebrand – FAQ (Tracking, Benachrichtigungen, Sprachen, Rechnungen)
* https://www.hillebrandgori.com/login – Login-Verteilerseite (myHillebrandGori, SCM, Vancouver Warehouse)
* https://www.hillebrandgori.com/login/myhillebrandgori → 301 → https://my.hillebrandgori.com/ (geprüft 18.10.2026)
* https://www.hillebrandgori.com/tools/track-a-shipment → 301 → https://my.hillebrand.com/public-tracking (geprüft 18.10.2026)
* https://www.hillebrandgori.com/support/self-services – Liste der Self-Service-Tools
* https://www.hillebrandgori.com/media/publication/report-feature – Berichte, Export, Zeitplanung, Login "login.hillebrand.com"
* https://www.hillebrandgori.com/media/publication/container-loading-and-shipment-pick-up-task-feature – Loading/Pick-up-Task, Notification preferences
* https://www.hillebrandgori.com/media/publication/keep-export-details-aligned-with-the-myhillebrandgori-loading-and-pick-up-task – Task-Details für Exporteure
* https://www.hillebrandgori.com/media/publication/invoices-and-payments – Rechnungen, Suche nach PO-Referenz, Berichte per E-Mail
* https://www.hillebrandgori.com/media/publication/the-people-behind-the-process--michele-carta – EDI/API-Fallbeispiel
* https://www.hillebrandgori.com/terms-of-use – Nutzungsbedingungen (Abschnitte 6, 7, 10, 35)
* https://developer.hillebrandgori.com/ – API-Developer-Portal (ReDoc) mit eingebetteter OpenAPI 3.0.0, Version 1.2.8, "last uploaded on 2026-09-17"; Get-Started-Text, Webhook-Ereignisliste, FAQ (Token 2 h, 100 Aufrufe/min)
* https://login.hillebrand.com/ – Okta-Sign-In-Seite "myHillebrandGori - Sign In" (Widget 7.50.0, Feature-Flags)
* https://login.hillebrand.com/oauth2/aus95hq7r8iIqp14M0i7/.well-known/openid-configuration – OpenID-Metadaten (Endpunkte, Grant-Typen, PKCE)
* https://my.hillebrandgori.com/ (SPA-Shell, `main-XHHNJT52.js`, `chunk-5XJX27KR.js`) – Routen `public-tracking`, `signup`; Okta-Utility; Suchtypen `shipmentReference|blNumber|containerNumber`; Cloudflare-Challenge-Skript
* iTunes Search API `https://itunes.apple.com/search?term=hillebrand+gori&entity=software` und Google-Play-Suche "hillebrand gori" (18.10.2026) – keine App

Transporeon

* https://www.transporeon.com/website/Fact%20Sheet/de/FactSheet_Time_Slot_Management_DE.pdf – Factsheet Zeitfenster-Management (DE)
* https://www.transporeon.com/website/Fact%20Sheet/FactSheet_Retail%20Time%20Slot%20Management_EN_2.pdf – Factsheet Retail Time Slot Management (EN), Prozess "Supplier gets PO, carrier (or supplier) books"
* https://transporeon.com/en/products/time-slot-management – Produktseite
* https://transporeon-hcskb.atlassian.net/wiki/spaces/ADPD/pages/27232826 – Developer-Wiki "Time Slot Management" (Sichtbarkeit für Carrier, Bookings-API)
* https://www.transporeon.com/en_US/platform/transport-execution-visibility-hub/integrations/open-visibility-data – Open Visibility Data (Zugriff nur mit Zustimmung des Dateneigners)
* https://www.wanzl.com/_default_upload_bucket/Informationsschreiben_Time-Slot-Management_09.09.2025_DE_1.pdf – Kundenschreiben: Buchung durch Lieferant/Spediteur, 2,34 €, Pflichtangaben
* https://www.aurubis.com/dam/jcr:18adeed9-6a28-4016-bbb1-c6f1766df53e/Aurubis_Time%20Slot%20Management%20Self%20booking_2022_08_05_bm.2025-09-01-11-41-24.pdf – Quick-Login ohne Konto, keine Bestätigungs-E-Mail
* https://partner.zalando.com/university/article/book-warehouse-delivery-time-mercareon – Lieferantensicht bei einem Händler (Carrier bucht; Bestätigungs-Mail nur bei Umbuchung)
* https://www.cvk.de/wp-content/uploads/Warenanlieferungsrichtlinien_CVK_0726.pdf und https://events.gs1.de/fileadmin/gs1/fachpublikationen/anwendungsempfehlung_zeitfenstersteuerung.pdf – Avis-Pflicht, GS1-Empfehlung (nur Suchzusammenfassung gelesen)

Playwright, Chrome, Okta

* https://playwright.dev/python/docs/api/class-browsertype – `launch_persistent_context`, Warnung zum Chrome-Standardprofil, `channel`-Werte
* https://playwright.dev/python/docs/browsers – Chrome/Edge-Kanäle, neuer Headless-Modus, Enterprise-Policies
* https://playwright.dev/python/docs/codegen – codegen, `--save-storage`, `--user-data-dir`, `page.pause()`
* https://playwright.dev/python/docs/locators – empfohlene Locators
* https://playwright.dev/python/docs/auth – `storage_state`, Session-Storage-Einschränkung
* https://playwright.dev/python/docs/screenshots – Screenshots
* https://playwright.dev/python/docs/chrome-extensions – Beispiel mit persistentem Kontext
* Playwright 1.64.0 (pip, 18.10.2026): Signaturen `launch_persistent_context`, `page.screenshot`, `tracing.start/stop`; `python -m playwright codegen --help`
* https://developer.chrome.com/blog/remote-debugging-port – Chrome 136: Remote Debugging nur mit `--user-data-dir`, App-Bound Encryption, Empfehlung Chrome for Testing
* https://security.googleblog.com/2024/07/improving-security-of-chrome-cookies-on.html – App-Bound Encryption (Cookies an App-Identität gebunden)
* https://developer.okta.com/docs/guides/implement-grant-type/ropassword/main/ – Resource-Owner-Password-Flow ("last option")
* https://blog.castle.io/how-to-detect-headless-chrome-bots-instrumented-with-playwright/ – Erkennungsmerkmale (nur zur Risikoeinschätzung)
