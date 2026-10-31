"""Online-Shop des Ladens (Köln-Süd) mit Lastenrad-Lieferung.

* ``konfig``     – ``config/webshop.yaml`` (Firma, Liefergebiet, Zeitfenster, Sortiment, Mail)
* ``export``     – schreibt ``website/data/produkte.json`` (Preise aus dem Lexware-Artikelstamm)
  und ``website/data/lieferung.json`` für das Frontend
* ``bestellung`` – prüft eine Bestellung des Frontends serverseitig, rechnet Summen, vergibt die
  Bestellnummer, legt sie unter ``eingang/webshop/`` ab und erzeugt E-Mail-Entwürfe
* ``server``     – kleiner HTTP-Server (Standardbibliothek) für ``website/`` und ``POST /api/bestellung``
"""
