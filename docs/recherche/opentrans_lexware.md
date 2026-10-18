# Recherche: Lexware "Standard-Shopschnittstelle" (openTRANS-Bestellimport) für Lexware warenwirtschaft premium 2026

Stand der Recherche: 2026-10-18. Zielsystem: Lexware warenwirtschaft premium 2026 (Windows, vier Firmen/Mandanten).
Projektkontext: `docs/ARCHITEKTUR.md`, Modul `lexware/opentrans.py` (Verkaufs-Auftragsbestätigung per XML-Import).

Kennzeichnung im Dokument:

* **[V]** = verifiziert, mit Quelle und Zitat.
* **[A]** = Annahme / unsicher, mit Begründung. Muss am echten System geprüft werden.
* **[?]** = nicht auffindbar; konkrete Prüfaufgabe für den Anwender in der Checkliste am Ende.

## Zusammenfassung

1. Die Standard-Shopschnittstelle importiert Bestellungen im Format **openTRANS 1.0** (Namespace `http://www.opentrans.org/XMLSchema/1.0`), mit der Lexware-Erweiterung **`ORDER_LIST`** als Wurzelelement (beliebig viele `ORDER` pro Datei). openTRANS 2.1 ist nirgends als unterstützt belegt. [V]
2. Es gibt eine offizielle Lexware-Spezifikation (2009, Version 1.1, "ab Warenwirtschaft 9.00") und eine offizielle **Beispieldatei** (`Beispieldatei Bestellimport.xml`); beide sind weiterhin verlinkt und werden von aktuellen Connectoren (Drucktool 2026, WooCommerce-Plugins, OXID/Magento-Connector, BYCEPS) 1:1 nachgebaut. Das Handbuch "Lexware eCommerce 2026" bestätigt den Weg, enthält aber keine Feldliste mehr. [V]
3. Artikelzuordnung: **`SUPPLIER_AID` = Lexware-Artikelnummer**; unbekannte Artikelnummer => Position wird verworfen, keine Artikelneuanlage. [V]
4. Kundenzuordnung: **`PARTY_ID` wird nicht übernommen**; die Kundennummer kann nicht per XML gesetzt werden. Die Zuordnung erfolgt in der Ansicht eCommerce (Kundenliste, Filter auf Kundenname, zusätzlich E-Mail-Abgleich) per Klick "Kunde zuordnen"; ohne Zuordnung legt Lexware beim Übernehmen einen **neuen Kunden** an. [V]
5. Der Import erzeugt **keinen Beleg direkt**, sondern einen "Webshop-Auftrag" in der Ansicht eCommerce. Erst "Auftrag übernehmen als…" (Auftragsbestätigung / Rechnung / Lieferschein laut Spezifikation 2009; Auswahlmenü laut Handbuch 2026) erzeugt den Beleg in der Auftragsliste. Der übernommene Beleg erhält laut Handbuch 2026 das aktuelle Tagesdatum. [V]
6. Es gibt **keinen Importordner und keinen automatischen Import**: Die Datei wird im Dialog "Daten austauschen" (Extras -> eCommerce -> Daten austauschen…) per "Durchsuchen" gewählt und mit "Importieren" eingelesen. Ein Fehlerprotokoll als Datei und ein Doppelimport-Schutz sind nicht dokumentiert. [V]/[A]
7. Preise kommen aus der XML (`ARTICLE_PRICE type="net_list"|"gros_list"`, `PRICE_AMOUNT`, `PRICE_LINE_AMOUNT`, `TAX`), mit Netto/Brutto-Umrechnung je nach Firmeneinstellung; `REMARK type="tax_area"` = `Merchant` ist seit Version 2012 Pflicht. Laut Handbuch 2026 werden beim Übernehmen kundenspezifische Preise/Preisaktionen aus Lexware berücksichtigt. [V]
8. Lizenz: "Lexware eCommerce ist im Leistungsumfang von Lexware warenwirtschaft inbegriffen", kein Zusatzmodul, keine separate Installation; Aktivierung pro Firma unter Firmenangaben -> Services ("Anbindung an Lexware eCommerce"). Menü heißt seit 2016 "eCommerce", nicht mehr "eBusiness". [V]
9. Alternativen: Lexware selbst bietet **keinen** Datei-Import für Aufträge/Belege ("Ein Import von Bewegungsdaten, wie z.B. Aufträge, ist garnicht möglich"), keine COM-/API-Schnittstelle für die Warenwirtschaft; Drittanbieter (csv2Lex, sync4, OscWare, LexShop/Woo2LX) legen nur **Verkaufs**aufträge an. Für die **Einkaufs-Bestellung** bleibt nur die UI (Bestellassistent oder "Auftrag wandeln": Verkaufsbeleg -> Bestellung). [V]
10. Konsequenz für das Projekt: openTRANS spart die Positionserfassung der Verkaufs-AB, ersetzt aber **nicht** die Klicks "Importieren", "Kunde zuordnen", "Übernehmen" und das Füllen der Belegfelder (Spedition, Weingut, Shipment, Kunde). Diese Schritte müssen entweder manuell oder per UI-Automation erfolgen. Ab 2026 läuft pro/premium auf einer **neuen Datenbank (PostgreSQL)**; das betrifft ODBC, nicht den XML-Import. [V]/[A]

## 1. Importformat

### 1.1 Version, Wurzelelement, Namespace, DOCTYPE

* **[V] openTRANS Version 1.0.** Spezifikation 2009, Kap. 1: "Die zu importierenden Bestellungen müssen in dem Format openTRANS Version 1.0 vorliegen. Dieses Format ist im Detail beschrieben im Dokument openTRANS-V1_0.pdf im Kapitel 4.3 ORDER (AUFTRAG)". Handbuch eCommerce 2026, S. 9: "Für den Transfer der Bestellungen vom Webshop nach Lexware warenwirtschaft wird der Standard openTRANS® verwendet." (ohne Versionsangabe).
* **[A] openTRANS 2.1 wird nicht unterstützt.** Kein Lexware-Dokument und kein Connector-Code nennt 2.1 für Lexware. Das WooCommerce-Plugin "Order Export to Lexware" bietet 2.1 nur im Premium-Add-on "for other suitable systems"; der kostenlose Lexware-Export ist openTRANS 1. Alle gefundenen Lexware-Generatoren (Drucktool 10/2026, nscwto, adlib, Check24/OXID, BYCEPS) erzeugen 1.0.
* **[V] Wurzelelement `ORDER_LIST` (Lexware-Abweichung vom Standard).** Spezifikation 2009, Kap. 2.1: "Entgegen dem openTRANS-Standard soll eine Datei beliebig viele ORDER-Elemente enthalten können. Als neues Root-Element wird das Element ORDER_LIST verwendet. […] Für jedes ORDER-Element gilt der openTRANS-Standard. […] Die Angaben zu den Namespaces bleiben im ORDER-Element." Die offizielle Beispieldatei und alle Connectoren verwenden `ORDER_LIST` auch bei nur einer Bestellung.
* **[V] `ORDER`-Attribute:** `xmlns="http://www.opentrans.org/XMLSchema/1.0" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" version="1.0" type="standard"` (offizielle Beispieldatei). Drucktool (2026) lässt `xmlns:xsi` weg und importiert laut Code-Kommentar erfolgreich ("das die Standard-Shopschnittstelle von Lexware importiert"). [A] `xmlns:xsi` ist optional.
* **[V] Kein DOCTYPE.** Weder Spezifikation, Beispieldatei noch Connectoren verwenden eine DOCTYPE-Deklaration oder `xsi:schemaLocation`.

### 1.2 Zeichensatz, Sonderzeichen

* **[V] Encoding:** Spezifikation 2009, Kap. 3.1.1: "Die Kodierung der Zeichen in der XML-Datei sollte in dem Zeichensatz UTF-8 erfolgen […] Wird eine andere Kodierung verwendet, ist die Darstellung bestimmter Zeichen nicht möglich bzw. falsch." Die offizielle Beispieldatei (2007/2016) ist dagegen `encoding="ISO-8859-1"`. Connectoren nutzen UTF-8 (Drucktool, nscwto, adlib, BYCEPS) oder ISO-8859-15 (Check24). **Empfehlung: UTF-8**, wie in der Spezifikation.
* **[V] Euro-Zeichen vermeiden:** Kap. 3.1.2: "Eine bekannte Einschränkung ist die Übernahme des €-Zeichens durch eine Lexware Anwendung. Statt des €-Zeichens sollte der Text Euro oder EUR verwendet werden." Alle Connectoren ersetzen `€` durch `EUR`.
* **[V] Escaping:** Kap. 3.1.3: `&` -> `&amp;`, `>` -> `&gt;`, `<` -> `&lt;`, `'` -> `&apos;`, `"` -> `&quot;`. Das Plugin adlib ersetzt zusätzlich `&#039;` durch `&apos;` ("sonst funktionieren die einfachen Anführungszeichen nicht") -> [A] numerische Entities werden von Lexware nicht sauber verarbeitet; nur die fünf benannten Entities verwenden.
* **[V] Steuerzeichen:** Drucktool entfernt alle Zeichen außer Tab/LF/CR unterhalb 0x20 (XML-1.0-Regel).

### 1.3 Struktur und Pflichtelemente

Elementreihenfolge laut openTRANS-1.0-Spezifikation (Fraunhofer IAO/Uni Essen, 2001, Kap. 4 ORDER und Kap. 5 Elementbibliothek) und offizieller Lexware-Beispieldatei:

```
ORDER_LIST                                  Lexware-Wurzel [V]
  ORDER (xmlns, version="1.0", type="standard")
    ORDER_HEADER
      CONTROL_INFO                          Kann; GENERATOR_INFO, GENERATION_DATE -> "Wird nicht übernommen" [V]
      ORDER_INFO                            Muss
        ORDER_ID                            Muss (Standard) -> Lexware: "Auftrag-Bestellnummer" [V]
        ORDER_DATE                          Muss (Standard) -> Datumsteil als Auftragsdatum [V, aber siehe 3.1]
        DELIVERY_DATE type="fixed|optional" Kann (Standard); in Lexware-Mapping NICHT enthalten [A: ignoriert]
          DELIVERY_START_DATE / DELIVERY_END_DATE
        ORDER_PARTIES                       Muss (Standard)
          BUYER_PARTY/PARTY/[PARTY_ID]/ADDRESS      Standard: Muss; Lexware: Lieferadresse [V]
          SUPPLIER_PARTY/PARTY/ADDRESS              Standard: Muss; Lexware: "Wird nicht übernommen" [V]
          INVOICE_PARTY/PARTY/ADDRESS               Standard: Kann; Lexware: Rechnungsadresse = Kundendaten [V]
        PRICE_CURRENCY                      Standard: Kann ("Muss, sobald PRICE_AMOUNT verwendet wird")
        PAYMENT                             Kann; CASH/PAYMENT_TERM, ACCOUNT, CARD, DEBIT, CHECK [V]
        REMARK type="…"                     Kann, mehrfach; Lexware-Typen siehe 2.x [V]
    ORDER_ITEM_LIST                         Muss
      ORDER_ITEM (mehrfach)
        LINE_ITEM_ID                        Standard: Muss; Lexware: "Wird nicht übernommen" [V]
        ARTICLE_ID
          SUPPLIER_AID                      Muss -> Lexware-Artikelnummer [V]
          DESCRIPTION_SHORT                 -> "Artikelbezeichnung und -Matchcode" [V]
          DESCRIPTION_LONG                  -> "Text der Artikelposition (Positionstext)" [V]
        QUANTITY                            -> Artikelmenge [V]
        ORDER_UNIT                          -> "wird nicht übernommen" [V]
        ARTICLE_PRICE type="net_list|gros_list"
          PRICE_AMOUNT                      -> Einzelpreis [V]
          PRICE_LINE_AMOUNT                 -> Positionspreis [V]
          TAX                               -> Steuersatz, "0.19" = 19 % [V]
        REMARK type="origin_company_id"     Kann; Firmen-GUID, siehe 2.9 [V]
        REMARK type="arbitrary_data"        Kann; "0;84.03" = eigener Nettopreis bei Brutto->Netto [V]
    ORDER_SUMMARY
      TOTAL_ITEM_NUM                        Standard: Muss
      TOTAL_AMOUNT                          Standard: Kann
```

* **[V] Reihenfolge der Kinder von `ORDER_INFO`** (openTRANS 1.0, Elementtabelle ORDER_INFO): ORDER_ID, ALT_CUSTOMER_ORDER_ID, ORDER_DATE, DELIVERY_DATE, ORDER_PARTIES, PRICE_CURRENCY, PAYMENT, TERMS_AND_CONDITIONS, PARTIAL_SHIPMENT_ALLOWED, TRANSPORT, INTERNATIONAL_RESTRICTIONS, REMARK. Die Beispieldatei hält diese Reihenfolge ein (ORDER_ID, ORDER_DATE, ORDER_PARTIES, PRICE_CURRENCY, PAYMENT, REMARK…). Da Lexware-Dokumente für das Katalogformat ausdrücklich auf "Groß- und Kleinschreibung sowie die Reihenfolge der Tags" hinweisen (Doku-Shopschnittstelle, Einführung), sollte die Reihenfolge auch beim Bestellimport eingehalten werden [A].
* **[V] Mindestinhalt für automatische Übernahme** (Spezifikation 2009, Kap. 4.2.1): "Benötigte Informationen für automatische Erzeugung eines Auftrages: Es exisitiert ein ORDER_ITEM Element, das einen Stammartikel enthält." Alles andere (Kunde, Zahlung, Bemerkung) ist optional.
* **[A] `SUPPLIER_PARTY` und `BUYER_PARTY` dürfen fehlen.** Drucktool (Oktober 2026) sendet nur `INVOICE_PARTY` ("Zahlungsart und Lieferadresse bleiben weg: Lexware nimmt dann die Vorgaben am Kunden") und berichtet funktionierenden Import. Nach openTRANS-Standard sind beide Pflicht; Lexware scheint nicht gegen die DTD zu validieren. Sicherer: beide mitsenden (wie die offizielle Beispieldatei).
* **[A] `PRICE_CURRENCY`: `EUR` oder `978`?** Spezifikation 2009: "PRICE_CURRENCY: "978" entspricht Euro. Einzig erlaubter Wert." Die offizielle Beispieldatei (und Check24, nscwto, BYCEPS) verwendet dagegen `EUR`; Drucktool `978`. Beides scheint zu funktionieren. Empfehlung: `EUR` (Standard ISO 4217, offizielle Beispieldatei); Prüfaufgabe siehe Checkliste.

### 1.4 Offizielle Beispieldatei (Auszug, erste Bestellung)

Quelle: `https://lexwiki.de/wp-content/uploads/Beispieldatei-Bestellimport.zip` (Datei "Beispieldatei Bestellimport.xml", ISO-8859-1, Zeitstempel 2016; Inhalt von 2007). Die Spezifikation 2009 nennt sie in Kap. 4.4.1 ("Es wird die Importdatei Beispieldatei Bestellimport.xml verwendet (wird zusammen mit diesem Dokument zur Verfügung gestellt)").

```xml
<?xml version="1.0" encoding="ISO-8859-1"?>
<ORDER_LIST>
	<ORDER xmlns="http://www.opentrans.org/XMLSchema/1.0" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" version="1.0" type="standard">
		<ORDER_HEADER>
			<CONTROL_INFO>
				<GENERATOR_INFO>Shop 2.0</GENERATOR_INFO>
				<GENERATION_DATE>2007-10-16T14:26:24+01:00</GENERATION_DATE>
			</CONTROL_INFO>
			<ORDER_INFO>
				<ORDER_ID>R-10440/01</ORDER_ID>
				<ORDER_DATE>2007-10-16T13:26:24+01:00</ORDER_DATE>
				<ORDER_PARTIES>
					<BUYER_PARTY>
						<PARTY>
							<ADDRESS>
								<NAME>RSN Nord GmbH Liefer</NAME>
								<NAME2>Rautenberg</NAME2>
								<NAME3>Karl-Heinz</NAME3>
								<STREET>Dorfstraße 66</STREET>
								<ZIP>24800</ZIP>
								<CITY>Elsdorf</CITY>
								<COUNTRY>DE</COUNTRY>
								<PHONE type="other"/>
								<FAX/>
								<EMAIL>info@rsn-nord.de</EMAIL>
							</ADDRESS>
						</PARTY>
					</BUYER_PARTY>
					<INVOICE_PARTY>
						<PARTY>
							<ADDRESS>
								<NAME>RSN Nord GmbH</NAME>
								<NAME2>Meier</NAME2>
								<NAME3>Uwe</NAME3>
								<STREET>Hochstr. 33</STREET>
								<ZIP>24800</ZIP>
								<CITY>Elsdorf-Westermühlen</CITY>
								<COUNTRY>DE</COUNTRY>
								<PHONE type="other"/>
								<FAX/>
								<EMAIL>info@rsn-nord.de</EMAIL>
							</ADDRESS>
						</PARTY>
					</INVOICE_PARTY>
					<SUPPLIER_PARTY>
						<PARTY>
							<ADDRESS>
								<NAME>Bregetec Supershop</NAME>
								<NAME2>Grandjean</NAME2>
								<NAME3>Gudrun</NAME3>
								<STREET>Bergstr. 7</STREET>
								<ZIP>79110</ZIP>
								<CITY>Freiburg</CITY>
								<COUNTRY>DE</COUNTRY>
								<VAT_ID>DE812398835</VAT_ID>
								<EMAIL>info@bregetec.com</EMAIL>
							</ADDRESS>
						</PARTY>
					</SUPPLIER_PARTY>
				</ORDER_PARTIES>
				<PRICE_CURRENCY>EUR</PRICE_CURRENCY>
				<PAYMENT>
					<CARD>
						<CARD_NUM>5500000000000004</CARD_NUM>
						<CARD_EXPIRATION_DATE>2005-12</CARD_EXPIRATION_DATE>
						<CARD_TYPE>MC</CARD_TYPE>
						<CARD_HOLDER_NAME>Karsten Meier</CARD_HOLDER_NAME>
					</CARD>
				</PAYMENT>
				<REMARK type="delivery_method">Post - Nachnahme</REMARK>
				<REMARK type="shipping_fee">5.00</REMARK>
				<REMARK type="tax_area">Merchant</REMARK>
				<REMARK type="order">Das ist eine Bestellung vermittelt durch Herrn Mikolwski</REMARK>
				<REMARK type="additional_costs">20.00</REMARK>
			</ORDER_INFO>
		</ORDER_HEADER>
		<ORDER_ITEM_LIST>
			<ORDER_ITEM>
				<LINE_ITEM_ID>0</LINE_ITEM_ID>
				<ARTICLE_ID>
					<SUPPLIER_AID>SI 4248</SUPPLIER_AID>
					<DESCRIPTION_SHORT>Sunny Island SI 4248</DESCRIPTION_SHORT>
					<DESCRIPTION_LONG>Inselnetz Sunny Island SI 4248, …</DESCRIPTION_LONG>
				</ARTICLE_ID>
				<QUANTITY>1</QUANTITY>
				<ORDER_UNIT>1</ORDER_UNIT>
				<ARTICLE_PRICE type="gros_list">
					<PRICE_AMOUNT>2748.90</PRICE_AMOUNT>
					<PRICE_LINE_AMOUNT>2748.90</PRICE_LINE_AMOUNT>
					<TAX>0.19</TAX>
				</ARTICLE_PRICE>
			</ORDER_ITEM>
			<!-- zweite Position analog -->
		</ORDER_ITEM_LIST>
		<ORDER_SUMMARY>
			<TOTAL_ITEM_NUM>2</TOTAL_ITEM_NUM>
			<TOTAL_AMOUNT>5071.22</TOTAL_AMOUNT>
		</ORDER_SUMMARY>
	</ORDER>
	<!-- weitere ORDER-Elemente möglich -->
</ORDER_LIST>
```

Die zweite Bestellung der Beispieldatei zeigt Bankeinzug: `<PAYMENT><ACCOUNT><HOLDER>…</HOLDER><BANK_NAME>…</BANK_NAME><BANK_COUNTRY>DE</BANK_COUNTRY><BANK_CODE>…</BANK_CODE><BANK_ACCOUNT>…</BANK_ACCOUNT><PAYMENT_TERM TYPE="unece">54</PAYMENT_TERM></ACCOUNT></PAYMENT>`.

### 1.5 Aktuelle Connector-Quellcodes (zur Absicherung des Formats)

| Quelle | Jahr | Encoding | Währung | Parteien | Preis-Typ | Besonderheit |
|---|---|---|---|---|---|---|
| Lexware Beispieldatei (lexwiki.de) | 2007/2016 | ISO-8859-1 | EUR | BUYER, INVOICE, SUPPLIER | gros_list | PAYMENT CARD/ACCOUNT, alle 5 REMARK-Typen |
| linus1423/Drucktool `lexware.server.ts` | Okt. 2026 | UTF-8 | 978 | nur INVOICE (+PARTY_ID type=buyer_specific = E-Mail) | gros_list | kein PAYMENT, kein BUYER_PARTY; Kommentar: "Lexware legt daraus Rechnungen an und neue Kunden aus der Rechnungsadresse" |
| WooCommerce "Order Export to Lexware" (nscwto) | 2022 | UTF-8 | Shop-Währung | BUYER, INVOICE | net_list | zusätzlich `TAX_AMOUNT`, `DEPARTMENT`; tax_area Merchant/Non_EU |
| WooCommerce adlib-woo2lex-manuell | 2015-2023 | UTF-8 | Shop-Währung | BUYER, INVOICE | net_list | verlinkt Lexware-FAQ 000000000047110 (heute nicht mehr erreichbar) |
| Check24/OXID/Magento `rs_opentrans_document_writer_order_lexware_1_1` | 2016-2019 | ISO-8859-15 | EUR | INVOICE (+SHIPMENT_PARTIES/DELIVERY_PARTY) | gros_list | PARTY_ID mit type; ORDER_ID auf 7 Zeichen formatiert |
| BYCEPS `export.xml` (Python/Jinja) | 2014-2026 | UTF-8 (Header), Content-Type iso-8859-1 | Shop-Währung | BUYER, INVOICE | gros_list | PAYMENT CASH unece 10; `REMARK type="delivery_method">Online` |

## 2. Zuordnung beim Import

### 2.1 Kunde

* **[V] `PARTY_ID` wird ignoriert.** Spezifikation 2009, Tabelle 4.1: INVOICE_PARTY/PARTY_ID "Wird nicht übernommen"; BUYER_PARTY/PARTY_ID "Wird nicht übernommen". Praxisbestätigung 2023 (Shopware-Forum, Lexware warenwirtschaft premium): "Beim Importieren der Bestellungen sehen wir die Kundennummer in lexware nicht."
* **[V] Zuordnung in der Ansicht eCommerce.** Handbuch eCommerce 2026, S. 13-15: "Im unteren Bereich unter dem Register Kunde sehen Sie rechts die Kundenliste und links die Kundendaten aus dem markierten Auftrag zum Vergleich. […] Die Verbindung zwischen Bestellung und Kunden stellen Sie her, indem Sie einen Kunden in der Liste auf der rechten Seite markieren und dann auf die Schaltfläche Kunde zuordnen klicken oder einen neuen Kunden anlegen. Um eine korrekte Kundenzuordnung der Webshop-Aufträge zu gewährleisten, wird zusätzlich die E-Mail Adresse aus dem Shop für den Abgleich verwendet." Spezifikation 2009, FAQ 5.4: "Gegegebenenfalls sehen sie nicht alle Kunden, sondern nur die, deren Namen mit dem Namen im Auftrag übereinstimmt. Wollen Sie alle Kunden sehen, klicken sie auf die Schaltfläche 'Filter auf Kundenname aufheben'."
* **[A] Keine vollautomatische Zuordnung bei Webshop-Aufträgen.** Handbuch 2026: "Die Zuordnung erfolgt automatisch bei Aufträgen, die aus Lexware mobile importiert werden." Für Webshop-Aufträge wird nur der Abgleich (Vorschlag/Filter) über Name und E-Mail beschrieben. Ob bei eindeutigem Name+E-Mail-Treffer die Zuordnung automatisch gesetzt wird, ist nicht dokumentiert -> Prüfaufgabe.
* **[V] Neukunde bei fehlender Zuordnung.** FAQ 5.5: "Wann wird ein neuer Kunde angelegt? Wenn keine Kundennumer zugeordnet wurde." Handbuch 2026: Mehrfach-Übernahme scheitert, wenn "die Kundenzuordnung fehlt, erscheint ein entsprechender Hinweis und in der Spalte Status wird ein Symbol angezeigt. Übernehmen Sie in diesem Fall den Auftrag einzeln." -> Risiko: Einzel-Übernahme ohne Zuordnung erzeugt einen Dubletten-Kunden.
* **[V] Stammdaten werden ergänzt, nie überschrieben.** Tabelle 4.1 INVOICE_PARTY: "Die Daten werden in die Stammdaten des zugeordneten Kunden übernommen, vorausgesetzt das entsprechende Stammdatum ist leer. Stammdaten werden nie überschrieben."
* **[V] NAME-Semantik:** Kap. 3.3: `NAME` = Firmenname, `NAME2` = Nachname, `NAME3` = Vorname. `COUNTRY`: "Das Länderkürzel wird nicht verarbeitet ('DE' wird nicht in 'Deutschland' umgewandelt)" -> Drucktool sendet deshalb "Deutschland" als Klartext [A: Klartext ist für den Kundenstamm sinnvoller].
* **Empfehlung für das Projekt:** INVOICE_PARTY exakt mit Firmenname/Straße/PLZ/Ort/E-Mail aus dem Lexware-Kundenstamm füllen (Kunde 3441), damit der Namensfilter genau einen Treffer liefert; zusätzlich Kundennummer als Klartext in `REMARK type="order"` ("Kunde 3441 …") für den Bediener. `PARTY_ID type="buyer_specific">3441` mitsenden schadet nicht (Drucktool/Check24 senden PARTY_ID), bringt aber nach aktueller Kenntnis nichts.

### 2.2 Artikel

* **[V] `SUPPLIER_AID` = Artikelnummer.** Kap. 4.2.2: "Das Element SUPPLIER_AID wird als Artikelnummer interpretiert. Über die Artikelnummer wird der Stammartikel in der Datenbank gesucht. Wird kein Stammartikel gefunden, wird das Anlegen eines Auftrages abgebrochen. Es wird folgende Fehlermeldung ausgegeben: Auftragsposition konnte nicht übernommen werden. Artikel-Nr. <Artikelnummer> ist nicht als Stammartikel vorhanden. Eine automatische Artikelneuanlage wurde nicht durchgeführt!" Tabelle 4.1: "Gibt es keinen Stammartikel mit dieser Artikelnummer, wird das komplette ORDER_ITEM verworfen."
* **[V] Dialog bei verworfenen Positionen** (Kap. 4.2.1): "Es konnten nicht alle Positionen übernommen werden. Nicht übernommene Positionen werden gelöscht. Möchten Sie den Auftrag dennoch übernehmen?" Bei "Ja" entsteht ein Auftrag ohne diese Positionen.
* **[A] `BUYER_AID` wird nicht ausgewertet.** Es fehlt in der Lexware-Mapping-Tabelle; kein Connector sendet es.
* **[V] Positionsdaten kommen aus dem Stammartikel:** "wird eine Stammposition erzeugt, die Daten des Stammartikels in die erzeugte Stammposition übernommen, wird die Warengruppe des gefunden Stammartikels ermittelt und das Erlöskonto dieser Warengruppe wird zum Erlöskonto der Auftragsposition". `DESCRIPTION_SHORT` -> "Artikelbezeichnung und -Matchcode", `DESCRIPTION_LONG` -> "Positionstext". [?] Ob DESCRIPTION_SHORT die Stammbezeichnung in der Position überschreibt, ist unklar (Drucktool nutzt DESCRIPTION_SHORT/LONG bewusst als Freitext je Auftrag, was dafür spricht).
* **[V] Reihenfolge nicht garantiert:** LINE_ITEM_ID "Wird nicht übernommen oder verwendet. Die ORDER_ITEM-Elemente werden in nicht festgelegter Reihenfolge übernommen" (Stand 2009).
* **[V] Sonderfälle:** Serien-/Chargenartikel -> "wird ausschließlich eine Auftragsbestätigung (AB) erzeugt"; Lagerartikel mit negativem Bestand -> Rückfrage, bei NEIN "wird eine Position mit der Menge 0 angelegt" (Kap. 4.3).

### 2.3 Preise, Netto/Brutto, Preisgruppe

* **[V] Preise kommen aus der XML.** Kap. 4.2.5: "Das openTRANS-Element ARTICLE_PRICE\PRICE_LINE_AMOUNT wird zum Positionspreis." Tabelle 4.1: `PRICE_AMOUNT` = "EinzelPreis", `TAX` = "Steuersatz, z.B. '0.19'". Attribut `type`: "'net_list' -> Netto, 'gros_list' -> Brutto. Es gilt die Enstellung des zuletzt eingelesenen Artikels" (also pro Auftrag einheitlich wählen).
* **[V] Umrechnungstabelle (Tabelle 2):** Bruttoauftrag in Nettofirma -> "von brutto auf netto" (Formel `preis*tax/(1+tax)`, kaufmännisch gerundet, optional eigener Nettopreis über `<REMARK type="arbitrary_data">0;84.03</REMARK>`); Nettoauftrag in Bruttofirma bei steuerbarem Kunden -> "von netto auf brutto"; sonst keine Umrechnung. "Diese Umrechnungen können Rundungsfehler zur Folge haben." -> **Für das Projekt: Preistyp passend zur Firmeneinstellung senden (Nettofirma -> `net_list`), dann findet keine Umrechnung statt.**
* **[V] Steuergebiet Pflicht:** `REMARK type="tax_area"` mit `Merchant` (Inland), `EU`, `Non_EU`. lexwiki.de: "Variable 'Merchant' […] weil diese Variable ab Programm-Version 2012 ein Muss-Feld ist!" Bei `non_EU` gilt der Kunde als nicht steuerbar.
* **[V] Positionspreise sind "manuell":** Kap. 4.2.5.5: "Die Positionspreise eines über die Offene Shopschnittstelle eingelesenen Auftrages sind 'manuell', d.h. der Wert für 'Preis pro Verkaufseinheit' kann in einem Auftrag nicht geändert werden." (Stand 2009.)
* **[V] Aber: Lexware-Preise beim Übernehmen (2026).** Handbuch eCommerce 2026, S. 16: "Mit dem Übernehmen des Auftrags erfolgt die korrekte Übernahme der Steuersätze, ggf. die Berücksichtigung von §13b und falls im Lexware Programm kundenspezifische Preise für die übernommenen Auftragspositionen oder Preisaktionen eingestellt sind, werden die Preise bei der Übernahme in Ihr Programm übernommen. Abhängig von den in den Firmeneinstellung getroffenen Einstellungen. Somit ist immer gewährleistet, dass die korrekten Preise Ihres führenden Systems berücksichtigt werden." -> Kundenspezifische Preise/Aktionen in Lexware können den XML-Preis überschreiben.
* **[?] Preisgruppe:** Kein openTRANS-Element dafür; die Preisgruppe des Kunden ist nicht per XML wählbar. Geschäftsregel 3 ("VK Preisgruppe 1") wird erfüllt, indem der Automat `vk_preis_1` aus dem Stamm als `PRICE_AMOUNT` sendet. Ob Lexware bei Kunden mit anderer Preisgruppe den XML-Preis stehen lässt, muss getestet werden.

### 2.4 Kundenbestellnummer

* **[V]** `ORDER_ID` -> "Auftrag-Bestellnummer" (Tabelle 4.1); Spezifikation Kap. 4.4.3.1: "Kundendaten (Adresse), Bestellnummer und Datum wurden in die Auftragsbestätigung übernommen." Check24-Connector kürzt ORDER_ID auf 7 Zeichen, openTRANS erlaubt 250. [?] Feldlänge des Lexware-Felds "Bestellnummer" unbekannt; "B-4711" ist unkritisch.

### 2.5 Liefertermin

* **[A] Nicht übernehmbar.** openTRANS 1.0 kennt `ORDER_INFO/DELIVERY_DATE type="fixed|optional"` mit `DELIVERY_START_DATE`/`DELIVERY_END_DATE` (Elementbibliothek S. 150-151). Die Lexware-Mapping-Tabelle 4.1 führt DELIVERY_DATE nicht auf; kein Connector sendet es. Lexware-Aufträge haben zwar Datumsfelder (Handbuch Wawi 2026: "finden Sie im Auftragsassistenten die Datumsfelder"), ein Weg über die XML ist nicht belegt.
* **Empfehlung:** Liefertermin standardkonform als `DELIVERY_DATE` senden (schadet nicht) **und** als Klartext in `REMARK type="order"` ("Liefertermin 15.09.2026"), damit er im Bemerkungstext erscheint. Das Belegfeld "Shipment"/Lieferdatum muss ggf. per UI nachgetragen werden.

### 2.6 Abweichende Lieferadresse

* **[V] `BUYER_PARTY` = Lieferadresse.** Tabelle 4.1: "BUYER_PARTY: Lieferadresse. Daten werden in die Lieferadresse des zugeordneten Kunden übernommen (ergänzt, nie überschrieben). Wenn leer, wird keine Lieferadresse angelegt, insbesondere wird die Rechnungsadresse nicht zur Lieferadresse." EMAIL/PHONE/FAX/VAT_ID in BUYER_PARTY "Wird nicht übernommen". Beispieldatei: BUYER_PARTY-Name trägt den Zusatz "Liefer". Kap. 4.4.4: "Die Rechnungsadresse wurde ergänzt, dieLieferadresse wurde angelegt."
* **[?] Ob die Lieferadresse auch im erzeugten Beleg als abweichende Lieferanschrift steht** oder nur im Kundenstamm (Seite Anschriften) landet, ist nicht eindeutig. Drucktool lässt BUYER_PARTY weg, "Lexware nimmt dann die Vorgaben am Kunden".
* **Empfehlung:** BUYER_PARTY nur senden, wenn das PDF eine abweichende Lieferadresse nennt; sonst weglassen (verhindert, dass im Kundenstamm eine zweite Lieferadresse "ergänzt" wird).

### 2.7 Bemerkungen / Kopftext

* **[V]** `ORDER_INFO/REMARK type="order"` -> "Beliebige Bemerkung zur Belegart 'order'. Übernahme: Bemerkungsttext des Belegs." FAQ 5.3: "Kann ich zu einem Auftrag (Bestellung) eine Bemerkung angeben? Ja, im Element ORDER_INFO\REMARK type="order"". Nicht zugeordnete Versand-/Zahlungskosten landen in der "Nachbemerkung (auf der Seite Summe)" (Kap. 4.4.5) -> [A] "Bemerkungstext" = Nachbemerkung, nicht Kopftext/Auftragsbeschreibung.
* **[A] Kopftext (Auftragsbeschreibung) nicht per XML setzbar;** Handbuch 2026 erwähnt für mobile-Aufträge Textbausteine "für die Auftragsbeschreibung/Nachbemerkung", für Webshop-Aufträge nur Register Konditionen mit "Auftragsbeschreibung und Nachbemerkung" als Anzeige.
* **[A] Belegfelder/Freifelder (Spedition, Weingut, Shipment, Kunde) nicht per XML setzbar.** Kein openTRANS-Element und kein Lexware-Mapping; müssen nach der Übernahme per UI gefüllt werden (Geschäftsregel 9). Workaround: alle vier Werte zusätzlich im `REMARK type="order"` mitgeben, damit sie im Beleg sichtbar sind.

### 2.8 Versandkosten, Steuer, Zahlungsart

* **[V] Versand:** `REMARK type="delivery_method"` (Lieferart, z. B. "UPS") + `REMARK type="shipping_fee"` (Betrag). "Die Lieferkosten werden als Auftragsposition übernommen, wenn eine gleichlautende Nebenleistung (Verwaltung - Leistung - Nebenleistung) angelegt ist. Ist keine gleichlautende Nebenleistung angelegt, werden sie in die Nachbemerkung übernommen." (Kap. 4.2.6). Für das Projekt (Lieferung per Spedition, keine Versandkostenposition laut Beispiel-PDFs): weglassen oder nur `delivery_method` als Info.
* **[V] Zahlungsartkosten:** `REMARK type="additional_costs"` -> Nebenleistung, wenn Wert > 0 und eine Nebenleistung mit dem Namen der Zahlungsart existiert; sonst Nachbemerkung "Zahlungsartkosten Barzahlung 1.35 EUR".
* **[V] Steuer:** je Position `TAX` (0.19 / 0.07 / 0); Steuergebiet über `tax_area`. "Mit dem Übernehmen des Auftrags erfolgt die korrekte Übernahme der Steuersätze" (Handbuch 2026).
* **[V] Zahlungsart** (Kap. 4.2.3/4.2.4): `PAYMENT/CASH/PAYMENT_TERM TYPE="unece"` mit 10 = Rechnung, 25 = Vorkasse, 52 = Nachnahme, 56 = Barzahlung; `PAYMENT/ACCOUNT` (+PAYMENT_TERM 54) = Bankeinzug; `PAYMENT/CARD` = Kartenzahlung; `DEBIT` = Gutschriftverfahren; `CHECK` = Scheck; "nicht vorhanden -> Rechnung"; unbekannter unece-Wert -> wie Barzahlung. Die Zahlungsangaben werden "in den Anmerkungstext des Auftrags übernommen" (z. B. "Rechnung"); Bank-/Kartendaten in die Kundenstammdaten (nur leere Felder). Attributschreibweise: Beispieldatei `TYPE="unece"`, BYCEPS `type="unece"` -> [A] beides akzeptiert; die Beispieldatei (`TYPE`) ist die sichere Variante.
* **Empfehlung:** `<PAYMENT><CASH><PAYMENT_TERM TYPE="unece">10</PAYMENT_TERM></CASH></PAYMENT>` (Rechnung) oder PAYMENT ganz weglassen (ergibt ebenfalls "Rechnung").

### 2.9 Mandantenschutz: `origin_company_id`

* **[V]** Kap. 4.2.8: "Das Element REMARK type='origin_company_id' enthält die Id (faktura&auftrag) bzw. die Guid (Warenwirtschaft) der Firma, in die die Bestellung ausschließlich importiert werden kann. Fehlt das Element oder ist es leer, kann die Bestellung in jede Firma (Mandant) importiert werden." Es steht **im ORDER_ITEM**, es gilt der Wert des ersten Artikels. Beispiel: `<REMARK type="origin_company_id">87AD28A4-08BA-409d-86B4-26DF93FB3D0C</REMARK>`.
* **Nutzen für das Projekt:** Mit vier Firmen ist das ein wirksamer Schutz gegen Import in die falsche Firma (Geschäftsregel 1). [?] Woher die Firmen-GUID kommt, ist nicht dokumentiert (Prüfaufgabe: catalog.xml-Export auf `origin_company_id` prüfen, Firmenverwaltung, DB).

## 3. Ergebnis des Imports

### 3.1 Belegart und Ablauf

* **[V] Zwei Schritte: Importieren, dann Übernehmen.** Handbuch eCommerce 2026, S. 13/22: "Bestelldaten importieren: Klicken Sie auf die Schaltfläche Durchsuchen, damit Sie den gewünschten Pfad für die zu importierende xml-Datei auswählen können. Klicken Sie auf die Schaltfläche Importieren, damit die Bestellungen importiert werden. Danach werden die importierten Bestellungen in der eCommerce Ansicht angezeigt." Dort sind sie "Webshop-Aufträge" (Auswahlbox "Alle eCommerce-Aufträge / mobile-Aufträge / Webshop-Aufträge"), Spalte "Herkunft" mit Symbol, Spalte "Zielbeleg", Spalte "Status".
* **[V] Belegart beim Übernehmen wählbar.** Handbuch 2026, S. 15: "Übernehmen Sie den Auftrag aus dem Shop nach Lexware warenwirtschaft, indem Sie zuerst im Bereich Auftrag übernehmen als… aus dem Auswahlmenü die gewünschte Auftragsart auswählen und danach entweder die Schaltfläche Übernehmen oder über das Kontextmenü den Eintrag Übernehmen klicken." Spezifikation 2009, Kap. 4.4.3: "Die Bestellung kann als Auftragsbestätigung, Rechnung oder Lieferschein übernommen werden. Auf die Einzelheiten der Datenübernahme hat diese Wahl aber keinen Einfluß." [?] Ob 2026 weitere Auftragsarten (Angebot) im Menü stehen und ob die Vorbelegung gespeichert wird -> Prüfaufgabe.
* **[V] Ergebnis:** "Damit werden die Aufträge aus der eCommerce-Liste entfernt und sind in der Auftragsliste im Programm zu finden. Um in der Auftragsliste die übernommenen Aufträge mit einem Blick zu erkennen, lässt sich über die Listeneinstellungen die Spalte 'Herkunft' in der Liste ergänzen." Der Beleg ist ein normaler Auftrag; "Auftrag weiterführen" (Bearbeiten -> Aufträge -> Weiterführen) zum Lieferschein funktioniert damit wie bei jedem anderen Auftrag (Handbuch Wawi 2026, "Auftrag weiterführen").
* **[V] Belegdatum = Tagesdatum (Widerspruch zur Spezifikation 2009).** Handbuch 2026: "Der übernommene Beleg erhält das aktuelle Tagesdatum." Spezifikation 2009: "ORDER_DATE: Der Datumsteil wird als Datum des Auftrags übernommen". Die neuere Aussage gilt vermutlich; ORDER_DATE bleibt aber als Bestelldatum sichtbar. -> Prüfaufgabe.
* **[V] Mehrfachübernahme:** "Möchten Sie mehrere Aufträge gleichzeitig übernehmen, dann nutzen Sie die Möglichkeit der Mehrfachselektion mit Strg und Mausklick."
* **[V] Sperre:** "Ist der Bereich eCommerce von einem anderen Benutzer in Bearbeitung, so ist dies in der Ansicht sichtbar und Sie erhalten einen entsprechenden Hinweis." -> Bei UI-Automation: Ansicht nur einmal öffnen, danach schließen.
* **[V] Löschen:** "Über das Kontextmenü Eintrag Löschen in der oberen Belegliste können Sie den Auftrag löschen."

### 3.2 Importordner, manuell vs. automatisch

* **[V] Kein Importordner, kein Watch-Folder.** Nur Datei-Dialog ("Durchsuchen") im Dialog "Daten austauschen" (Extras -> eCommerce -> Daten austauschen… oder Schaltfläche "Daten austauschen" oben rechts in der Ansicht eCommerce; Ansicht über Verwaltung -> eCommerce). Der Export-Pfad für catalog.xml ist konfigurierbar; für den Import wird jeweils eine Datei gewählt. [?] Ob der zuletzt gewählte Importpfad gemerkt wird -> Prüfaufgabe (wichtig für UI-Automation des Dialogs).
* **[V] Kein automatischer Import.** Weder Handbuch noch Spezifikation beschreiben einen zeitgesteuerten Import; Drittanbieter (sync4, OscWare, LexShop) lösen genau das mit eigener Middleware.
* **Konsequenz:** `firma.opentrans.import_ordner` im Projekt ist ein Ablageordner für den Bediener (oder für die UI-Automation des Dialogs), kein von Lexware überwachter Ordner.

### 3.3 Fehlerprotokoll, Doppelimport

* **[V] Fehler erscheinen als Dialoge** (Positionen verworfen, Artikel fehlt, Kundenzuordnung fehlt -> Status-Symbol, negativer Bestand, Serien-/Chargenhinweis). Ein Protokoll als Datei ist nicht dokumentiert. [?]
* **[A] Kein Doppelimport-Schutz.** Die Spezifikation beschreibt mehrfaches Importieren derselben Datei als normalen Testweg ("wenn man die Importdatei erneut importiert und übernimmt"); kein Hinweis auf Prüfung von ORDER_ID. Drucktool und adlib merken sich exportierte Aufträge selbst ("exportiert"-Flag). -> Die Dublettenprüfung (Geschäftsregel 5) bleibt Aufgabe des Automaten; eindeutiger Dateiname (Datum, Kundennr., Bestellnr.) wie in der Architektur vorgesehen.

## 4. Lizenz / Verfügbarkeit in premium

* **[V] Enthalten, kein Zusatzmodul.** Handbuch eCommerce 2026, S. 3: "Lexware eCommerce ist im Leistungsumfang von Lexware warenwirtschaft inbegriffen." S. 8: "Da Lexware eCommerce ein integriertes Modul von Lexware warenwirtschaft ist, ist eine separate Installation nicht möglich." Bestandteile: "Standard Shopschnittstelle, Schnittstelle zu Turbolister, Schnittstelle zu Lexware mobile Belegerfassung".
* **[V] Produktseite shop.lexware.de (warenwirtschaft pro/premium):** "Offene Standard-Shop-Schnittstelle zwischen eigenem Webshop und Lexware warenwirtschaft"; Vergleichstabelle "Standardisierte eShop-Schnittstelle". Preise (Stand 18.10.2026, netto): pro 61,90 €/Monat (3 Arbeitsplätze), premium 77,90 €/Monat bzw. 934,80 €/Jahr (5 Arbeitsplätze), premium + Support-Service-Paket 87,80 €/Monat.
* **[V] Aktivierung pro Firma:** Handbuch Wawi 2026, Firmenangaben -> Services: "Die Anbindung an Lexware eCommerce aktivieren Sie im gleichnamigen Bereich." Folge: Artikelstamm bekommt die Option "Internetartikel" ("Haben Sie in den Firmenangaben die Anbindung an eCommerce aktiviert, steht Ihnen auf der Seite Allgemein die Option Internetartikel zur Verfügung"); Ansicht "eCommerce" unter Verwaltung ("Über Verwaltung öffnen Sie die Ansicht eCommerce: Aus dieser Ansicht starten Sie den Datenaustausch mit der Standard-Shopschnittstelle").
* **[V] Umbenennung:** Lexware vor Ort 2015: "Aus eBusiness wird eCommerce […] Lesen Sie außerdem, die Belege der offenen Shopschnittstelle im neuen Bereich eCommerce ein." (Auslieferung Frühjahr 2016). Der im Auftrag genannte Menüpfad "eBusiness -> Standard-Shopschnittstelle -> Importieren" stammt aus der Spezifikation 2009 ("Menü Ebusiness - Standard Shopschnittstelle - Importieren") und heißt heute: Verwaltung -> eCommerce bzw. Extras -> eCommerce -> Daten austauschen….
* **[V] Prüfung, ob freigeschaltet:** (a) Firmenangaben -> Services -> Haken "Anbindung an Lexware eCommerce"; (b) Menü Verwaltung enthält "eCommerce"; (c) Extras -> eCommerce -> Daten austauschen… öffnet den Dialog mit "Bestelldaten importieren"; (d) Lizenzen unter "Meine Lizenzen" (Handbuch Wawi 2026: "Mit diesem Assistenten können Sie Ihre Lizenzen verwalten") - eCommerce erscheint dort nicht als eigene Lizenz, da integriert [A].
* **[A] Hinweis Drittanbieter:** qualimero-Guide 2026 (sync4-nah): "Nach dem Update auf Lexware Version 2026 muss der E-Commerce-Bereich in Lexware einmalig neu initialisiert werden, damit die sync4-Verbindung weiterhin funktioniert." Nicht offiziell bestätigt; könnte bedeuten, dass die eCommerce-Ansicht nach dem Datenbankwechsel einmal neu eingerichtet werden muss.
* **[V] Abgrenzung faktura+auftrag:** LexShop/Woo2LX: "Produkte ohne Professional/Premium-Edition können Bestellungen importieren, aber Neukunden werden nicht automatisch angelegt." Für premium irrelevant.

## 5. Alternativen zur UI-Automation

| Weg | Verkaufs-AB | Einkaufs-Bestellung | Status |
|---|---|---|---|
| Standard-Shopschnittstelle (openTRANS) | ja (Import + 2-3 Klicks) | **nein** (nur Verkauf: AB/Rechnung/Lieferschein) | [V] |
| Datei -> Import (Importassistent) | nein | nein | [V] nur Stammdaten |
| "Eingangsrechnung importieren" (E-Rechnung, seit 25.01) | nein | nein (erzeugt Eingangsrechnung im Einkauf, keine Bestellung) | [V] |
| "Auftrag wandeln" (Verkauf <-> Einkauf) | - | ja, aber UI; EK-Preise nur mit Bezugsquelle im Artikelstamm | [V] |
| Bestellvorschläge / Bestellassistent | - | ja, UI | [V] |
| COM-Server / "Lexware API" für Warenwirtschaft | nicht existent | nicht existent | [V] keine Quelle |
| Direktes Schreiben in die DB | technisch denkbar, nicht unterstützt, im Projekt verboten | dito | [A] |
| Drittanbieter csv2Lex / sync4 / OscWare / LexShop / dmConnector | ja | **nicht dokumentiert** | [V] |

Details:

* **[V] Kein Auftragsimport per Datei -> Import.** Lexware-FAQ 000036502 ("Datenübernahme nicht möglich! Export- und Import-Funktionen in Lexware warenwirtschaft/Lexware handwerk"): Import von Kunden-/Lieferantenstammdaten "über Datei - Import" (*.txt/*.csv), Artikelstammdaten importierbar; "Ein Import von Bewegungsdaten, wie z.B. Aufträge, ist garnicht möglich." Handbuch Wawi 2026 kennt unter "importieren" nur "Elektronische Eingangsrechnungen importieren" ("als Aufträge Einkauf zu importieren, klicken Sie in der Menüleiste der warenwirtschaft auf Eingangsrechnung importieren") - das ist die Belegart Eingangsrechnung (ZUGFeRD/XRechnung), keine Bestellung.
* **[V] Auftrag wandeln** (Handbuch Wawi 2026, Menü Bearbeiten): "Die Funktion Auftrag wandeln ermöglicht es, die Positionslisten von Verkaufsaufträgen in Einkaufsaufträge und umgekehrt zu übernehmen. […] Von/in Verkauf: Angebot, Auftragsbestätigung, Lieferschein und Rechnung. In/von Einkauf: Bestellanfrage und Bestellung. […] Wird ein Verkaufsauftrag in einen Einkaufsbeleg gewandelt, können die entsprechenden Einkaufspreise nur gezogen werden, wenn der ausgewählte Lieferant in den Artikelstammdaten als Bezugsquelle (auf der Seite Lieferanten) zugeordnet wurde. Andernfalls werden die Preise auf '0' gesetzt." -> Mögliche Vereinfachung der UI-Automation: erst AB per openTRANS, dann "Wandeln" in die Bestellung (Lieferant wählen, Feld "Customer"/Belegfelder füllen). Erfordert gepflegte Bezugsquellen im Artikelstamm. [A] Ob "Wandeln" das Weingut automatisch aus der Bezugsquelle vorbelegt, ist nicht dokumentiert.
* **[V] Keine COM-/API-Schnittstelle der Warenwirtschaft.** Weder Handbuch Wawi 2026, Technik-Handbuch 2026 noch Suchtreffer nennen eine Programmierschnittstelle; "Lexware API"/"Lexware Office API" betrifft nur die Cloud-Lösung Lexware Office (nicht gemeint). Drittanbieter sprechen von einer "zertifizierten Lexware-Online-Schnittstelle" (sync4) bzw. nutzen "die integrierte eCommerce-Schnittstelle von Lexware" plus lesenden DB-Zugriff (Woo2LX: "Woo2LX schreibt nicht in die Lexware Datenbank").
* **[V] Datenbank 2026:** Lexware-FAQ 000036502/000036324: "Ab dem 01.01.2026 nutzen Lexware pro und premium eine neue Datenbank." Technik-Handbuch Mehrplatz 2026 listet die Dienste "Lexware Datenbank Server (Dbsrv16.exe)" (Sybase SQL Anywhere), "Lexware Postgresql Monitor (Haufe.Components.LexwareService.exe)" und "lexware_postgresql_pro (pg_ctl.exe)"; Datenübernahme "in eine neue Tabellenarchitektur". Update-Notizen 2026 (Juni/August): "Optimierung im Datenbank-Migrationsprozess". -> Für `odbc_quelle.py` relevant (siehe `docs/recherche/odbc_lexware.md`); der XML-Import ist davon unabhängig.
* **[V] Drittanbieter:** csv2Lex (lexpro.de, 480 €/Jahr, "Bestellungen aus Shop, Marktplatz oder Excel-Liste landen als Datei bei Ihnen", Aufträge aus CSV/XLSX, Kunden werden erkannt/angelegt; Einkauf nicht erwähnt); sync4 (Dupp GmbH, Lexware-Partner, "Durch die Bestellungen im Shop werden automatisch entsprechende Aufträge oder Rechnungen erstellt"); OscWare ("Beim Bestellimport bestimmen Sie unter anderem Auftragstyp, Bearbeiter, Kostenstelle, Kostenträger", Neukunde oder Sammelkunde, Mapping Zahlungs-/Versandarten); LexShop24/Woo2LX (139 €, "Bestellungen aus WooCommerce werden in Lexware als Aufträge oder Rechnungen importiert"); dmConnector (Forum 2016). **Keiner** nennt Bestellungen an Lieferanten.

## 6. Beispiel-XML für die Auftragsbestätigung (Kunde 3441, Bestellnr. B-4711)

Annahmen für das Beispiel: Nettofirma (Preise netto, `net_list`), 19 % USt auf Wein, Rechnungskauf, Liefertermin 15.09.2026, keine abweichende Lieferadresse (BUYER_PARTY deshalb weggelassen, Lexware nimmt die Kundendaten), Firma "Weinstrasse GmbH" als SUPPLIER_PARTY. Summen: 60 x 6,90 = 414,00; 24 x 6,90 = 165,60; netto 579,60.

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!-- [V] Encoding UTF-8 laut Spezifikation 3.1.1; Beispieldatei nutzt ISO-8859-1 -->
<ORDER_LIST>
  <!-- [V] ORDER_LIST = Lexware-Wurzel, beliebig viele ORDER -->
  <ORDER xmlns="http://www.opentrans.org/XMLSchema/1.0"
         xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
         version="1.0" type="standard">
    <!-- [V] Namespace/Attribute wie offizielle Beispieldatei; xmlns:xsi optional [A] -->
    <ORDER_HEADER>
      <CONTROL_INFO>
        <!-- [V] wird nicht übernommen, nur Information -->
        <GENERATOR_INFO>auftragsautomat 0.1</GENERATOR_INFO>
        <GENERATION_DATE>2026-09-10T09:15:00+02:00</GENERATION_DATE>
      </CONTROL_INFO>
      <ORDER_INFO>
        <!-- [V] ORDER_ID -> Feld "Bestellnummer" des Auftrags (Kundenbestellnummer) -->
        <ORDER_ID>B-4711</ORDER_ID>
        <!-- [V] Datumsteil -> Auftragsdatum laut Spez. 2009; laut Handbuch 2026 erhält der
             übernommene Beleg das Tagesdatum -> Prüfaufgabe -->
        <ORDER_DATE>2026-09-10T09:15:00+02:00</ORDER_DATE>
        <!-- [A] DELIVERY_DATE ist openTRANS-1.0-konform, aber im Lexware-Mapping nicht
             enthalten; vermutlich ignoriert, deshalb zusätzlich als REMARK unten -->
        <DELIVERY_DATE type="fixed">
          <DELIVERY_START_DATE>2026-09-15T00:00:00+02:00</DELIVERY_START_DATE>
          <DELIVERY_END_DATE>2026-09-15T00:00:00+02:00</DELIVERY_END_DATE>
        </DELIVERY_DATE>
        <ORDER_PARTIES>
          <!-- BUYER_PARTY (= Lieferadresse) bewusst weggelassen: keine abweichende
               Lieferadresse [V: "Wenn leer, wird keine Lieferadresse angelegt"] -->
          <SUPPLIER_PARTY>
            <!-- [V] wird nicht übernommen; nur für Standardkonformität -->
            <PARTY>
              <ADDRESS>
                <NAME>Weinstrasse GmbH</NAME>
                <STREET>Musterstraße 1</STREET>
                <ZIP>12345</ZIP>
                <CITY>Musterstadt</CITY>
                <COUNTRY>DE</COUNTRY>
              </ADDRESS>
            </PARTY>
          </SUPPLIER_PARTY>
          <INVOICE_PARTY>
            <!-- [V] Rechnungsadresse = Kundendaten des Auftrags; Grundlage für den
                 Namensfilter/E-Mail-Abgleich in der eCommerce-Ansicht -->
            <PARTY>
              <!-- [V] PARTY_ID "Wird nicht übernommen" (Spez. 2009, Forum 2023);
                   mitgesendet, schadet nicht [A] -->
              <PARTY_ID type="supplier_specific">3441</PARTY_ID>
              <ADDRESS>
                <!-- [V] NAME = Firmenname, NAME2 = Nachname, NAME3 = Vorname -->
                <NAME>Weinmarkt Kirchhellen GmbH &amp; Co. KG</NAME>
                <NAME2>Mustermann</NAME2>
                <NAME3>Max</NAME3>
                <STREET>Hauptstraße 10</STREET>
                <ZIP>46244</ZIP>
                <CITY>Bottrop</CITY>
                <!-- [V] Länderkürzel wird nicht umgewandelt; Klartext oder Kürzel wie im Stamm -->
                <COUNTRY>Deutschland</COUNTRY>
                <!-- [V] E-Mail wird zusätzlich für den Kundenabgleich verwendet (Handbuch 2026) -->
                <EMAIL>bestellung@weinmarkt-kirchhellen.example</EMAIL>
              </ADDRESS>
            </PARTY>
          </INVOICE_PARTY>
        </ORDER_PARTIES>
        <!-- [A] "EUR" wie Beispieldatei; Spez. 2009 nennt "978" als einzig erlaubten Wert -->
        <PRICE_CURRENCY>EUR</PRICE_CURRENCY>
        <!-- [V] unece 10 = Rechnung; weglassen ergibt ebenfalls "Rechnung" -->
        <PAYMENT>
          <CASH>
            <PAYMENT_TERM TYPE="unece">10</PAYMENT_TERM>
          </CASH>
        </PAYMENT>
        <!-- [V] tax_area: Merchant = Inland, Pflichtfeld seit Version 2012 -->
        <REMARK type="tax_area">Merchant</REMARK>
        <!-- [V] Bemerkungstext des Belegs (Nachbemerkung); hier Liefertermin, Kundennr.
             und Belegfelder als Klartext, weil dafür keine XML-Felder existieren [A] -->
        <REMARK type="order">Kunde 3441 Weinmarkt Kirchhellen; Kundenbestellung B-4711; Liefertermin 15.09.2026; Spedition: Hillebrand; Weingut: Villa Armellina; Shipment: 15.09.2026</REMARK>
      </ORDER_INFO>
    </ORDER_HEADER>
    <ORDER_ITEM_LIST>
      <ORDER_ITEM>
        <!-- [V] LINE_ITEM_ID wird nicht übernommen; Reihenfolge nicht garantiert -->
        <LINE_ITEM_ID>1</LINE_ITEM_ID>
        <ARTICLE_ID>
          <!-- [V] SUPPLIER_AID = Lexware-Artikelnummer; unbekannt -> Position verworfen -->
          <SUPPLIER_AID>1643</SUPPLIER_AID>
          <!-- [V] -> Artikelbezeichnung/Matchcode bzw. Positionstext; Stammdaten des
               Artikels werden ohnehin gezogen -->
          <DESCRIPTION_SHORT>Villa Armellina Rosso 2023 0,75 l</DESCRIPTION_SHORT>
          <DESCRIPTION_LONG></DESCRIPTION_LONG>
        </ARTICLE_ID>
        <!-- [V] Menge -->
        <QUANTITY>60</QUANTITY>
        <!-- [V] wird nicht übernommen; "1" wie Beispieldatei -->
        <ORDER_UNIT>1</ORDER_UNIT>
        <!-- [V] net_list = Nettopreise; PRICE_AMOUNT Einzelpreis, PRICE_LINE_AMOUNT Positionspreis,
             TAX 0.19 = 19 %; Preis = vk_preis_1 aus dem Stamm, nie der PDF-Preis -->
        <ARTICLE_PRICE type="net_list">
          <PRICE_AMOUNT>6.90</PRICE_AMOUNT>
          <PRICE_LINE_AMOUNT>414.00</PRICE_LINE_AMOUNT>
          <TAX>0.19</TAX>
        </ARTICLE_PRICE>
        <!-- [V] optional: Import nur in diese Firma (GUID der Firma); Wert muss am System
             ermittelt werden [?] -->
        <!-- <REMARK type="origin_company_id">GUID-DER-FIRMA</REMARK> -->
      </ORDER_ITEM>
      <ORDER_ITEM>
        <LINE_ITEM_ID>2</LINE_ITEM_ID>
        <ARTICLE_ID>
          <SUPPLIER_AID>957</SUPPLIER_AID>
          <DESCRIPTION_SHORT>Villa Armellina Bianco 2024 0,75 l</DESCRIPTION_SHORT>
          <DESCRIPTION_LONG></DESCRIPTION_LONG>
        </ARTICLE_ID>
        <QUANTITY>24</QUANTITY>
        <ORDER_UNIT>1</ORDER_UNIT>
        <ARTICLE_PRICE type="net_list">
          <PRICE_AMOUNT>6.90</PRICE_AMOUNT>
          <PRICE_LINE_AMOUNT>165.60</PRICE_LINE_AMOUNT>
          <TAX>0.19</TAX>
        </ARTICLE_PRICE>
      </ORDER_ITEM>
    </ORDER_ITEM_LIST>
    <ORDER_SUMMARY>
      <!-- [V] Standard-Pflichtfeld; Lexware-Mapping nennt es nicht -->
      <TOTAL_ITEM_NUM>2</TOTAL_ITEM_NUM>
      <!-- [A] Summe der PRICE_LINE_AMOUNT (netto, da net_list); Beispieldatei summiert Bruttozeilen -->
      <TOTAL_AMOUNT>579.60</TOTAL_AMOUNT>
    </ORDER_SUMMARY>
  </ORDER>
</ORDER_LIST>
```

Hinweise zur Umsetzung in `lexware/opentrans.py`:

* Zahlen immer mit Punkt und zwei Nachkommastellen (`Decimal.quantize("0.00")`), keine Tausendertrennzeichen (Doku-Shopschnittstelle, Datentyp "Number").
* Datum/Zeit ISO 8601 mit Zeitzonen-Offset (`2026-09-10T09:15:00+02:00`), wie Beispieldatei und openTRANS DATETIME.
* `€` -> `EUR`, nur die fünf benannten Entities, Steuerzeichen < 0x20 entfernen.
* Übersprungene Positionen (0,00 €, Sondervarianten) weglassen; eine Position mit unbekannter Artikelnummer würde sonst einen Dialog auslösen.
* Pro Firma `tax_area` und Preis-Typ (`net_list`/`gros_list`) aus der Firmenkonfiguration; Standard `net_list` + `Merchant`.
* `validiere_xml`: Wohlgeformtheit, Pflichtelemente (ORDER_ID, ORDER_DATE, ORDER_PARTIES mit INVOICE_PARTY, mindestens ein ORDER_ITEM mit SUPPLIER_AID/QUANTITY/ARTICLE_PRICE, tax_area vorhanden), Zahlenformat, Reihenfolge der ORDER_INFO-Kinder.

## 7. Konsequenzen für den Ablauf im Projekt

1. Der openTRANS-Weg erzeugt die Verkaufs-AB **nicht ohne Bedienerinteraktion**: Datei wählen -> Importieren -> Kunde zuordnen (oder prüfen) -> "Auftrag übernehmen als Auftragsbestätigung" -> Übernehmen -> Belegfelder (Spedition, Weingut, Shipment, Kunde) im Beleg nachtragen. Entweder bleibt das ein kurzer manueller Schritt in der Freigabe-UI ("XML liegt unter …, bitte importieren"), oder `ui_automation.py` steuert genau diese Dialoge.
2. Die Einkaufs-Bestellung an das Weingut ist per openTRANS **nicht** möglich; UI-Automation (Bestellassistent) bleibt, alternativ "Auftrag wandeln" aus der importierten AB.
3. Dubletten-, Preis- und Mengenprüfung müssen vor dem Schreiben der XML abgeschlossen sein; Lexware prüft nichts davon.
4. Mandantensicherheit: Dateiname und Ablageordner pro Firma; sobald die Firmen-GUID bekannt ist, `origin_company_id` je Position setzen.
5. Dry Run: XML in `settings.xml_ausgabe_ordner` schreiben und nicht importieren; "scharf" = XML in `firma.opentrans.import_ordner` ablegen (plus ggf. UI-Automation des Import-Dialogs).

## Checkliste für den Anwender (am Lexware-System prüfen)

1. **Freischaltung:** In jeder der vier Firmen: Firmenangaben -> Services -> ist "Anbindung an Lexware eCommerce" aktiv? Gibt es Verwaltung -> eCommerce und Extras -> eCommerce -> Daten austauschen…? Screenshot des Dialogs "Daten austauschen" (Bereich "Bestelldaten importieren").
2. **Testimport in einer Testfirma:** Die offizielle Beispieldatei (`Beispieldatei Bestellimport.xml`, aus lexwiki.de) importieren. Erscheinen zwei Webshop-Aufträge? Welche Meldungen kommen (Artikel unbekannt)?
3. **Eigene Testdatei (Kap. 6)** mit echten Artikelnummern (1643, 957) und Kunde 3441 importieren. Prüfen: (a) wird Kunde 3441 im Register Kunde automatisch vorgeschlagen/zugeordnet (Name/E-Mail)? (b) welche Einträge enthält "Auftrag übernehmen als…" (Angebot? Auftragsbestätigung? Lieferschein? Rechnung?) und bleibt die Auswahl gespeichert? (c) Belegdatum nach Übernahme = Tagesdatum oder ORDER_DATE? (d) steht "B-4711" im Feld Bestellnummer? (e) Positionspreis 6,90 netto oder durch kundenspezifischen Preis/Preisgruppe ersetzt? (f) wo landet der `REMARK type="order"`-Text (Nachbemerkung/Seite Summe oder Auftragsbeschreibung)? (g) hat DELIVERY_DATE ein Lieferdatum gesetzt? (h) sind die Belegfelder Spedition/Weingut/Shipment/Kunde leer (erwartet) und wie heißen sie genau (Freifelder?)?
4. **Varianten testen:** einmal `PRICE_CURRENCY` = `EUR`, einmal `978`; einmal ohne `xmlns:xsi`; einmal ohne SUPPLIER_PARTY; einmal mit `encoding="UTF-8"` und Umlauten im Namen. Welche Variante importiert fehlerfrei?
5. **PARTY_ID:** Datei mit `<PARTY_ID type="buyer_specific">3441</PARTY_ID>` und ohne: Unterschied in der Kundenzuordnung?
6. **Doppelimport:** dieselbe Datei zweimal importieren. Erscheint der Auftrag zweimal? Gibt es eine Warnung?
7. **Fehlerprotokoll:** Datei mit unbekannter Artikelnummer importieren; Dialogtexte notieren/screenshotten; gibt es ein Protokoll (Fenster, Datei, Logordner)?
8. **Importpfad:** Wird der zuletzt gewählte Pfad im Dialog gemerkt? Standardordner festlegen (z. B. `C:\Auftragsautomat\lexware-import\<firma>\`).
9. **Firmen-GUID für `origin_company_id`:** catalog.xml über "Artikelkatalog exportieren" erzeugen und auf `origin_company_id` prüfen; sonst Lexware-Support/Firmenverwaltung fragen. Danach Testimport mit falscher GUID in der falschen Firma (muss abgelehnt werden).
10. **Lieferadresse:** Testdatei mit BUYER_PARTY (abweichende Adresse): landet sie im Beleg als Lieferanschrift und/oder im Kundenstamm (Seite Anschriften)?
11. **Zahlungsart/Nebenleistungen:** Existieren Nebenleistungen "Versand"/"Spedition"? Wenn Versandkosten je gebraucht werden: Namen der Nebenleistung = `delivery_method`-Text.
12. **Weiterführen:** Importierte AB markieren -> Bearbeiten -> Aufträge -> Weiterführen -> Lieferschein: funktioniert wie bei manuell erfassten AB? Dialoge "per E-Mail/Ausdruck" notieren.
13. **Auftrag wandeln:** Importierte AB -> Bearbeiten -> Auftrag wandeln -> Bestellung: wird das Weingut aus der Bezugsquelle vorbelegt, stimmen die EK-Preise? (Entscheidung, ob das die Bestellassistent-Automation ersetzen kann.)
14. **Datenbank:** Welche Dienste laufen (Dbsrv16.exe und/oder lexware_postgresql_pro)? Ist die Firma schon auf die neue Datenbank migriert? (Relevant für ODBC, nicht für den XML-Import.)
15. **Hinweis sync4/2026:** Falls die eCommerce-Ansicht nach dem 2026-Update leer/fehlerhaft ist: Bereich einmal neu initialisieren (Dialog in der Ansicht) - unbestätigter Drittanbieter-Hinweis.

## Quellen

Offizielle Lexware-Dokumente:

* Spezifikation "Import von Bestellungen im openTRANS-Format in Lexware warenwirtschaft pro/premium", Stand 20.10.2009, Version 1.1 (lex-blog.de): https://lex-blog.de/wp-content/uploads/2014/10/Spezifikation_Bestellformat_openTRANS_pro.pdf (identisch: https://lexwiki.de/wp-content/uploads/Spezifikationen-Bestellformat-openTRANS-pro.pdf)
* Offizielle Beispieldatei "Beispieldatei Bestellimport.xml": https://lexwiki.de/wp-content/uploads/Beispieldatei-Bestellimport.zip
* Benutzerhandbuch "Lexware eCommerce für Lexware warenwirtschaft" (Ausgabe 2026, Impressum 2025): https://www.lexware.de/fileadmin/support/handbuecher/2026/handbuch_ecommerce_pro.pdf (Kopie: https://lex-blog.de/wp-content/uploads/2026/03/20260103_handbuch_ecommerce.pdf)
* "Dokumentation: Standard Shopschnittstelle - XML Format des Artikelkatalogs" (BMEcat-Katalogformat, Datentypen): https://lexwiki.de/wp-content/uploads/Doku-Shopschnittstelle.pdf
* Benutzerhandbuch Lexware warenwirtschaft pro/premium 2026 (Auftrag weiterführen, Auftrag wandeln, Eingangsrechnung importieren, Firmenangaben/Services, Verwaltung -> eCommerce, Meine Lizenzen): https://www.lexware.de/fileadmin/support/handbuecher/2026/handbuch_warenwirtschaft_pro.pdf
* Technik-Handbuch Mehrplatz 2026 (Dienste Dbsrv16.exe, lexware_postgresql_pro, Datenübernahme in neue Tabellenarchitektur): https://lex-blog.de/wp-content/uploads/2026/03/20260103_handbuch_technik_mehrplatz.pdf
* Lexware-FAQ 000036502 "Datenübernahme nicht möglich! Export- und Import-Funktionen in Lexware warenwirtschaft/Lexware handwerk" (kein Import von Bewegungsdaten; neue Datenbank ab 01.01.2026): https://www.lexware.de/support/faq/faq-beitrag/000036502/
* Lexware Update-Übersicht warenwirtschaft 2026: https://www.lexware.de/update-warenwirtschaft
* Produktseite shop.lexware.de warenwirtschaft premium (Preise, Arbeitsplätze, "Offene Standard-Shop-Schnittstelle"): https://shop.lexware.de/lexware-warenwirtschaft-premium
* Lexware vor Ort 2015, Auftragswesen ("Aus eBusiness wird eCommerce"): https://lex-blog.de/wp-content/uploads/2015/12/20151203_LvO_2015_Auftragswesen.pdf
* Lexware Video-Übersicht Fakturierung (kein eCommerce-Video vorhanden): https://www.lexware.de/support/videos-uebersicht-fakturierung/
* Lexware sync4 Partnerseite: https://www.lexware.de/sync4/

openTRANS-Standard:

* openTRANS Version 1.0, Spezifikation (Fraunhofer IAO / Universität Essen, 2001), Kap. 4 ORDER, Kap. 5 Elementbibliothek (ORDER_INFO, DELIVERY_DATE, ORDER_PARTIES, PARTY_ID, ARTICLE_ID): https://unite.eu/assets/support/uk/specification_opentrans_v1_0_en-2.pdf

Connector-Quellcodes (GitHub, geprüft):

* linus1423/Drucktool, `src/server/invoices/lexware.server.ts` und `tests/lexware.test.ts` (Oktober 2026, TypeScript): https://github.com/linus1423/Drucktool/blob/4ddefbc064da1856d659dd55f1a45d150b725196/src/server/invoices/lexware.server.ts
* common-repository/order-export-to-lexware-opentrans-for-woocommerce, `includes/class-download-orders-nscwto.php` (WordPress-Plugin "Order Export to Lexware for WooCommerce - OpenTRANS", v1.7.3): https://github.com/common-repository/order-export-to-lexware-opentrans-for-woocommerce/blob/71dba1c82e937f369401f14bc1ba254b869ed5c1/includes/class-download-orders-nscwto.php ; Plugin-Seite: https://de.wordpress.org/plugins/order-export-to-lexware-opentrans-for-woocommerce/
* common-repository/adlib-woo2lex-manuell, `adlib-woo2lex-manuell.php`, `definitionen.php` (WordPress-Plugin "WooCommerce-Bestellexport für Lexware", v1.0.4): https://github.com/common-repository/adlib-woo2lex-manuell/blob/07ba87d9cc44107154ca4802424656aeb0657992/adlib-woo2lex-manuell.php
* itratos/Check24_OXID_6_CEPE, `modules/ts_opentrans_orderimport/opentrans/rs_opentrans_document_writer_order_lexware_1_1.class.php` (Testsieger/Check24 OpenTrans Connector, "Lexware-Standard 1.1"): https://github.com/itratos/Check24_OXID_6_CEPE/blob/5bb26b49538c4166de7ecca236a49a4b3b566a05/modules/ts_opentrans_orderimport/opentrans/rs_opentrans_document_writer_order_lexware_1_1.class.php (Magento-Variante: riconeitzel/check24_magento_orderimport)
* byceps/byceps, `byceps/services/shop/order/export/templates/export.xml` und `order_export_service.py` (Python/Jinja, 2014-2026): https://github.com/byceps/byceps/blob/42ca0da96b7ccede36f02bacccb45238ab28a53c/byceps/services/shop/order/export/templates/export.xml

Drittanbieter und Foren:

* lexwiki.de FAQ "Shop Schnittstelle in Faktura / Warenwirtschaft" ("Merchant" Pflichtfeld ab 2012; Downloads): https://lexwiki.de/faq/faktura-warenwirtschaft/shop-schnittstelle
* sync4-Dokumentation "Lexware eCommerce Übersicht": https://docs.sync4.de/faq/warenwirtschaften/lexware/lexware-ecommerce-ubersicht
* lexpro.de "Lexware Tools & Erweiterungen" (csv2Lex, 480 €/Jahr): https://lexpro.de/lexware-tools/
* OscWare Lexware-Schnittstelle: https://www.oscware.de/schnittstellen--warenwirtschaft-lexware
* LexSHOP Woo2LX: https://lexshop.org/produkt/woo2lx/
* qualimero "Lexware und Shopware verbinden: Integration, Plugins und Praxis-Guide 2026": https://qualimero.com/blog/lexware-shopware
* Shopware-Forum "Wie importiere ich Kunden von Shopware nach Lexware?" (24.10.2023, premium, Kundennummer fehlt nach openTRANS-Import): https://forum.shopware.com/t/wie-importiere-ich-kunden-von-shopware-nach-lexware/101684
* Shopware-Forum "Lexware-Shopware" (2016, dmConnector/LexShop/sync4): https://forum.shopware.com/t/lexware-shopware/33660
* Shopware-Forum "Plugin Lexware Anbindung" (2011): https://forum.shopware.com/t/plugin-lexware-anbindung/1778
* Shopware-Forum "Schnittstelle Lexware" (2010, veraltet): https://forum.shopware.com/t/schnittstelle-lexware/548

Nicht mehr erreichbar (Hinweis): Lexware-FAQ 000000000047110 (warenwirtschaft pro, Standard-Shopschnittstelle), auf die das adlib-Plugin verlinkt; leitet heute auf die allgemeine Support-Übersicht um.
