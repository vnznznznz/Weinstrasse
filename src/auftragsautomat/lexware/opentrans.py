"""openTRANS-XML für die Lexware-Standard-Shopschnittstelle (Verkauf-Auftragsbestätigung).

Erzeugt aus einem geprüften ``Belegpaket`` eine openTRANS-1.0-Bestelldatei, so wie sie die
"Offene Standard-Shopschnittstelle" von Lexware warenwirtschaft pro/premium (Bereich
eCommerce) einliest. Grundlage ist ``docs/recherche/opentrans_lexware.md`` (Lexware-
Spezifikation 2009, offizielle Beispieldatei, Handbuch eCommerce 2026, Connector-Quellcodes).

Was die XML leistet und was nicht
---------------------------------
Der Import erzeugt KEINEN Beleg, sondern einen "Webshop-Auftrag" in der Ansicht eCommerce.
Erst "Auftrag übernehmen als … Auftragsbestätigung" + "Übernehmen" legt die AB in der
Auftragsliste an. Die XML spart also die Positionserfassung; Importieren, Kunde zuordnen,
Belegart wählen, Übernehmen und das Füllen der Belegfelder (Spedition, Weingut, Shipment,
Kunde) bleiben manuelle Schritte oder Aufgabe der UI-Automation (``ui_automation.py``).
Einkaufs-Bestellungen sind über diesen Weg nicht möglich (nur Verkauf).

Verifiziert [V] / Annahme [A] (Quellen und Zitate in der Recherche)
-------------------------------------------------------------------
==========================================  =====  ================================================
Element / Regel                             Stand  Bemerkung
==========================================  =====  ================================================
Wurzel ORDER_LIST, darin ORDER mit xmlns    [V]    openTRANS 1.0, version="1.0" type="standard",
                                                   kein DOCTYPE, Namespace nur am ORDER-Element
xmlns:xsi am ORDER                          [A]    Beispieldatei hat es, Drucktool nicht
Encoding UTF-8                              [V]    Empfehlung der Spezifikation (Beispiel: ISO-8859-1)
"€" -> "EUR", nur benannte Entities         [V]    numerische Entities vermutlich problematisch [A]
Steuerzeichen < 0x20 entfernen              [V]    XML-1.0-Regel (Drucktool)
Reihenfolge der ORDER_INFO-Kinder           [V]    openTRANS 1.0; Lexware verlangt sie vermutlich [A]
ORDER_ID -> Feld "Bestellnummer" der AB     [V]    Kundenbestellnummer (z. B. B-4711)
ORDER_DATE -> Auftragsdatum                 [V]    Handbuch 2026: Beleg erhält beim Übernehmen das
                                                   Tagesdatum (Widerspruch zur Spez. 2009)
DELIVERY_DATE (Liefertermin)                [A]    standardkonform, von Lexware vermutlich ignoriert;
                                                   deshalb zusätzlich Klartext in REMARK order
INVOICE_PARTY = Kundendaten                 [V]    NAME=Firma, NAME2=Nachname, NAME3=Vorname; werden
                                                   im Kundenstamm ergänzt, nie überschrieben
PARTY_ID (Kundennummer)                     [V]    wird NICHT übernommen; mitgesendet, schadet nicht [A]
Kundenzuordnung                             [V]    manuell in der Ansicht eCommerce (Namensfilter +
                                                   E-Mail-Abgleich); ohne Zuordnung -> NEUER Kunde
BUYER_PARTY = Lieferadresse                 [V]    nur bei abweichender Lieferadresse senden
SUPPLIER_PARTY                              [V]    wird nicht übernommen; nur Standardkonformität
PRICE_CURRENCY "EUR"                        [A]    Beispieldatei "EUR", Spezifikation 2009 "978"
PAYMENT/CASH/PAYMENT_TERM unece 10          [V]    = Rechnung; TYPE="unece" wie Beispieldatei
REMARK type="tax_area" = Merchant           [V]    Pflichtfeld seit Version 2012
REMARK type="order" -> Bemerkungstext       [V]    landet vermutlich in der Nachbemerkung [A]
SUPPLIER_AID = Lexware-Artikelnummer        [V]    unbekannt -> Position verworfen, keine Neuanlage
DESCRIPTION_SHORT                           [V]    Artikelbezeichnung; DESCRIPTION_LONG weggelassen [A]
QUANTITY / ORDER_UNIT                       [V]    Menge; ORDER_UNIT wird nicht übernommen
ARTICLE_PRICE type="net_list", TAX 0.19     [V]    Nettofirma -> net_list [A]; Preis = vk_preis_1
Kundenspezifische Preise / Preisaktionen    [V]    können beim Übernehmen die XML-Preise ersetzen
Preisgruppe                                 [A]    nicht per XML wählbar
REMARK type="origin_company_id" (GUID)      [V]    Mandantenschutz im ORDER_ITEM; GUID-Herkunft offen
Belegfelder / Kopftext                      [A]    nicht per XML setzbar -> Klartext in REMARK order
Kein Importordner, kein automatischer Import [V]   Datei wird im Dialog "Daten austauschen" gewählt
Kein Doppelimport-Schutz                    [A]    Dublettenprüfung bleibt im Automaten
==========================================  =====  ================================================

So stößt der Anwender den Import an (Lexware warenwirtschaft premium 2026)
--------------------------------------------------------------------------
1. Richtige Firma öffnen (Datei -> Firma wechseln). Voraussetzung: Firmenangaben -> Services
   -> "Anbindung an Lexware eCommerce" ist aktiv (eCommerce ist im Leistungsumfang enthalten).
2. Extras -> eCommerce -> Daten austauschen… -> Bereich "Bestelldaten importieren" ->
   Durchsuchen (XML aus dem Ablageordner wählen) -> Importieren.
3. Verwaltung -> eCommerce: Webshop-Auftrag markieren, Register "Kunde" -> richtigen Kunden
   in der Liste markieren -> "Kunde zuordnen" (sonst legt Lexware beim Übernehmen einen
   NEUEN Kunden an).
4. "Auftrag übernehmen als…" -> Auftragsbestätigung -> Übernehmen. Der Dialog "Es konnten
   nicht alle Positionen übernommen werden" darf nicht erscheinen (sonst Artikelnummer prüfen).
5. AB in der Auftragsliste öffnen (Spalte "Herkunft"): Belegfelder Spedition, Weingut,
   Shipment, Kunde nachtragen (Werte stehen in der Nachbemerkung), Bestellnummer, Preise und
   Liefertermin prüfen. Danach wie gewohnt "Auftrag weiterführen" zum Lieferschein.
"""

from __future__ import annotations

import hashlib
import logging
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, tzinfo
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from auftragsautomat.config import Firma, Settings
from auftragsautomat.models import (
    Belegart,
    Belegergebnis,
    Belegpaket,
    PositionsZuordnung,
    Schweregrad,
)

logger = logging.getLogger(__name__)

__all__ = [
    "GENERATOR_INFO",
    "OPENTRANS_NS",
    "OpenTransFehler",
    "OpenTransSchreiber",
    "import_anleitung",
]

# openTRANS-1.0-Spezifikation (nur Referenz, wird NICHT heruntergeladen; es wird bewusst nicht
# gegen DTD/XSD validiert, weil Lexware das offenbar auch nicht tut):
#   https://unite.eu/assets/support/uk/specification_opentrans_v1_0_en-2.pdf
# Lexware-Spezifikation 2009, Beispieldatei und Handbuch 2026: docs/recherche/opentrans_lexware.md
OPENTRANS_NS = "http://www.opentrans.org/XMLSchema/1.0"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"
OPENTRANS_VERSION = "1.0"
GENERATOR_INFO = "auftragsautomat 0.1"
ZEITZONE_NAME = "Europe/Berlin"
STEUERGEBIET_INLAND = "Merchant"  # weitere Werte laut Spezifikation: "EU", "Non_EU"
ZAHLUNGSART_RECHNUNG = "10"  # unece: 10 Rechnung, 25 Vorkasse, 52 Nachnahme, 56 Barzahlung
MAX_BEMERKUNG_ZEICHEN = 2000  # TODO-LEXWARE: Feldlänge der Nachbemerkung ist nicht dokumentiert

Preistyp = Literal["net_list", "gros_list"]

# Reihenfolge der Kinder von ORDER_INFO laut openTRANS 1.0 (Elementbibliothek)
_ORDER_INFO_REIHENFOLGE: tuple[str, ...] = (
    "ORDER_ID",
    "ALT_CUSTOMER_ORDER_ID",
    "ORDER_DATE",
    "DELIVERY_DATE",
    "ORDER_PARTIES",
    "PRICE_CURRENCY",
    "PAYMENT",
    "TERMS_AND_CONDITIONS",
    "PARTIAL_SHIPMENT_ALLOWED",
    "TRANSPORT",
    "INTERNATIONAL_RESTRICTIONS",
    "REMARK",
)
_STEUERGEBIETE = {"Merchant", "EU", "Non_EU"}
_PREISTYPEN = {"net_list", "gros_list"}

_STEUERZEICHEN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_BETRAG_MUSTER = re.compile(r"^-?\d+\.\d{2}$")
_STEUER_MUSTER = re.compile(r"^\d+\.\d{2,}$")
_ISO_DATUM_MUSTER = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:[+-]\d{2}:\d{2}|Z)$")
_PLZ_ORT_MUSTER = re.compile(r"^(?:[A-Za-z]{1,3}\s*-\s*)?(\d{4,5})\s+(\S.*)$")
_UMLAUTE = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue", "ß": "ss"})


class OpenTransFehler(Exception):
    """Pflichtprüfung oder Zielprüfung fehlgeschlagen; ``probleme`` enthält alle Gründe."""

    def __init__(self, probleme: list[str]) -> None:
        self.probleme = list(probleme)
        super().__init__("openTRANS: " + "; ".join(self.probleme))


@dataclass
class _Adresse:
    name: str
    name2: str | None = None
    name3: str | None = None
    strasse: str | None = None
    plz: str | None = None
    ort: str | None = None
    land: str | None = None
    ust_id: str | None = None
    email: str | None = None


# ---------------------------------------------------------------------------
# Formatierung
# ---------------------------------------------------------------------------


def _sauber(text: object) -> str:
    """Text für die XML: Euro-Zeichen -> EUR, Steuerzeichen raus, eine Zeile, getrimmt."""
    if text is None:
        return ""
    t = str(text).replace("€", "EUR")
    t = _STEUERZEICHEN.sub("", t)
    return " ".join(t.split())


def _betrag(wert: Decimal) -> str:
    """Geldbetrag mit Punkt und genau zwei Nachkommastellen, ohne Tausendertrennzeichen."""
    return str(Decimal(wert).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _menge(wert: Decimal) -> str:
    """Menge ohne unnötige Nachkommastellen: 60.00 -> "60", 1.50 -> "1.5"."""
    return format(Decimal(wert).normalize(), "f")


def _steuersatz(prozent: Decimal) -> str:
    """19 -> "0.19", 7 -> "0.07", 0 -> "0.00", 5.5 -> "0.055"."""
    text = format((Decimal(prozent) / Decimal(100)).normalize(), "f")
    ganz, _, nach = text.partition(".")
    return f"{ganz}.{nach.ljust(2, '0')}"


def _deutsches_datum(d: date) -> str:
    return f"{d:%d.%m.%Y}"


def _bereinige_dateiteil(text: str, maximal: int = 40) -> str:
    """Nur Buchstaben, Ziffern, Punkt, Unterstrich, Bindestrich (für Dateinamen)."""
    t = _sauber(text).translate(_UMLAUTE)
    t = re.sub(r"[^A-Za-z0-9._-]+", "-", t).strip("-._")
    return t[:maximal].rstrip("-._")


def _zeitzone() -> tzinfo:
    try:
        return ZoneInfo(ZEITZONE_NAME)
    except ZoneInfoNotFoundError:
        lokal = datetime.now().astimezone().tzinfo
        logger.warning(
            "Zeitzone %s nicht verfügbar (Paket tzdata fehlt?), verwende Systemzeitzone %s",
            ZEITZONE_NAME,
            lokal,
        )
        return lokal  # type: ignore[return-value]


def _parse_adresse(text: str) -> _Adresse:
    """Zerlegt eine Freitext-Lieferadresse heuristisch in NAME/STREET/ZIP/CITY/COUNTRY.

    Zeilen werden an Zeilenumbrüchen getrennt, bei einzeiligem Text an Komma/Semikolon.
    Erste Zeile = Name, Zeile "PLZ Ort" wird erkannt, die Zeile davor ist die Straße,
    weitere Zeilen davor werden NAME2, eine Zeile danach gilt als Land. Der Rohtext wird
    zusätzlich in der Bemerkung mitgegeben, damit nichts verloren geht.
    """
    zeilen = [z.strip() for z in re.split(r"[\r\n]+", text) if z.strip()]
    if len(zeilen) == 1:
        zeilen = [z.strip() for z in re.split(r"\s*[,;]\s*", zeilen[0]) if z.strip()]
    if not zeilen:
        return _Adresse(name=_sauber(text))
    adresse = _Adresse(name=_sauber(zeilen[0]))
    rest = zeilen[1:]
    for i, zeile in enumerate(rest):
        treffer = _PLZ_ORT_MUSTER.match(zeile)
        if not treffer:
            continue
        adresse.plz, adresse.ort = treffer.group(1), _sauber(treffer.group(2))
        davor, danach = rest[:i], rest[i + 1 :]
        if davor:
            adresse.strasse = _sauber(davor[-1])
        if len(davor) > 1:
            adresse.name2 = _sauber(" ".join(davor[:-1]))
        if danach:
            # Länderkürzel wird von Lexware nicht umgewandelt -> Text wie im Dokument
            adresse.land = _sauber(danach[0])
        return adresse
    # keine "PLZ Ort"-Zeile erkannt: zweite Zeile als Straße, Rest als Namenszusatz
    if rest:
        adresse.strasse = _sauber(rest[0])
    if len(rest) > 1:
        adresse.name2 = _sauber(" ".join(rest[1:]))
    return adresse


# ---------------------------------------------------------------------------
# XML-Hilfen (Lesen)
# ---------------------------------------------------------------------------


def _lokal(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _kind(elem: ET.Element | None, name: str) -> ET.Element | None:
    if elem is None:
        return None
    for k in elem:
        if _lokal(k.tag) == name:
            return k
    return None


def _kinder(elem: ET.Element | None, name: str) -> list[ET.Element]:
    if elem is None:
        return []
    return [k for k in elem if _lokal(k.tag) == name]


def _pfad(elem: ET.Element | None, *namen: str) -> ET.Element | None:
    for name in namen:
        elem = _kind(elem, name)
        if elem is None:
            return None
    return elem


def _inhalt(elem: ET.Element | None) -> str:
    return (elem.text or "").strip() if elem is not None else ""


def _dezimal(text: str) -> Decimal | None:
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def import_anleitung(firma: Firma, datei: Path | None = None) -> str:
    """Kurzanleitung für den Bediener, wie die XML in Lexware importiert wird."""
    wo = f" ({datei})" if datei else ""
    return (
        f"Lexware-Firma '{firma.lexware_firmenname}' öffnen -> Extras -> eCommerce -> Daten austauschen… "
        f"-> 'Bestelldaten importieren' -> Durchsuchen{wo} -> Importieren; dann Verwaltung -> eCommerce: "
        "Auftrag markieren, Register Kunde -> Kunde zuordnen, 'Auftrag übernehmen als…' Auftragsbestätigung "
        "-> Übernehmen; anschließend Belegfelder Spedition/Weingut/Shipment/Kunde in der AB nachtragen."
    )


# ---------------------------------------------------------------------------
# Schreiber
# ---------------------------------------------------------------------------


class OpenTransSchreiber:
    """Erzeugt openTRANS-Bestelldateien für die Lexware-Shopschnittstelle (Verkauf-AB).

    Dry Run (Standard): XML wird nur nach ``settings.xml_ausgabe_ordner`` geschrieben.
    Scharf: XML wird in ``firma.opentrans.import_ordner`` abgelegt. Das ist ein Ablageordner
    für den Bediener bzw. die UI-Automation; Lexware überwacht ihn NICHT.
    """

    def __init__(
        self,
        settings: Settings,
        firma: Firma,
        *,
        preistyp: Preistyp | None = None,
        steuergebiet: str | None = None,
        firmen_guid: str | None = None,
        jetzt: Callable[[], datetime] | None = None,
    ) -> None:
        # Ohne Argument gelten die Werte aus config/firmen.yaml (firma.opentrans.preistyp/steuergebiet)
        if preistyp is None:
            preistyp = firma.opentrans.preistyp
        if steuergebiet is None:
            steuergebiet = firma.opentrans.steuergebiet or STEUERGEBIET_INLAND
        if preistyp not in _PREISTYPEN:
            raise ValueError(f"Unbekannter Preistyp '{preistyp}' (erlaubt: net_list, gros_list)")
        if steuergebiet not in _STEUERGEBIETE:
            raise ValueError(f"Unbekanntes Steuergebiet '{steuergebiet}' (erlaubt: Merchant, EU, Non_EU)")
        self.settings = settings
        self.firma = firma
        # TODO-LEXWARE: Preistyp passend zur Firmeneinstellung (Nettofirma -> net_list), sonst
        # rechnet Lexware mit Rundungsfehlern um. Steuergebiet Merchant = Inland.
        self.preistyp: Preistyp = preistyp
        self.steuergebiet = steuergebiet
        # TODO-LEXWARE: Firmen-GUID für REMARK origin_company_id ist nicht dokumentiert (Prüfaufgabe:
        # catalog.xml-Export / Firmenverwaltung). Sobald bekannt: pro Firma konfigurieren.
        self.firmen_guid = firmen_guid or firma.opentrans.firmen_guid
        self._tz = _zeitzone()
        self._jetzt = jetzt

    # ----------------------------------------------------------------- Zeit

    def jetzt(self) -> datetime:
        wert = self._jetzt() if self._jetzt else datetime.now(self._tz)
        if wert.tzinfo is None:
            wert = wert.replace(tzinfo=self._tz)
        return wert

    def _iso(self, d: date) -> str:
        """ISO 8601 mit Zeitzonen-Offset, wie Beispieldatei: 2026-09-10T00:00:00+02:00."""
        return datetime.combine(d, time(0, 0), tzinfo=self._tz).isoformat(timespec="seconds")

    # ----------------------------------------------------------- Kopfdaten

    def _bestelldatum(self, paket: Belegpaket) -> date:
        return paket.roh.datum or self.jetzt().date()

    def _bestellnummer(self, paket: Belegpaket) -> str:
        roh = paket.roh
        for wert in (roh.bestellnummer, roh.belegnummer):
            if wert and wert.strip():
                return _sauber(wert)
        return _sauber(roh.quelldatei.stem)[:60]

    # ------------------------------------------------------- Pflichtprüfung

    def pruefe_paket(self, paket: Belegpaket) -> list[str]:
        """Sammelt alle Gründe, warum aus dem Paket keine gültige XML entstehen kann."""
        probleme: list[str] = []
        if paket.firma_id and paket.firma_id != self.firma.id:
            probleme.append(f"Paket gehört zu Firma '{paket.firma_id}', Schreiber ist für '{self.firma.id}'")
        kunde = paket.kunde.kunde
        if kunde is None:
            probleme.append("Kein Kunde zugeordnet (Lexware würde beim Übernehmen einen neuen Kunden anlegen)")
        else:
            if not kunde.kundennummer.strip():
                probleme.append("Kundennummer ist leer")
            if not kunde.name.strip():
                probleme.append(f"Kunde {kunde.kundennummer}: Name ist leer (INVOICE_PARTY/NAME ist Pflicht)")
            if kunde.firma_id != self.firma.id:
                probleme.append(
                    f"Kunde {kunde.kundennummer} gehört zu Firma '{kunde.firma_id}', nicht '{self.firma.id}'"
                )
        aktive = paket.aktive_positionen()
        if not aktive:
            probleme.append("Keine aktive Position (alle übersprungen oder keine vorhanden)")
        for p in aktive:
            bez = f"Position {p.roh.laufnr} ('{_sauber(p.roh.bezeichnung)[:40]}')"
            if p.artikel is None:
                probleme.append(f"{bez}: kein Artikel zugeordnet")
            else:
                if not p.artikel.artikelnummer.strip():
                    probleme.append(f"{bez}: Artikelnummer ist leer")
                if p.artikel.firma_id != self.firma.id:
                    probleme.append(
                        f"{bez}: Artikel {p.artikel.artikelnummer} gehört zu Firma '{p.artikel.firma_id}', "
                        f"nicht '{self.firma.id}'"
                    )
            if p.vk_preis is None:
                probleme.append(f"{bez}: kein VK-Preis (Preisgruppe 1) aus dem Stamm")
            elif p.vk_preis <= 0:
                probleme.append(f"{bez}: VK-Preis {p.vk_preis} ist nicht größer als 0")
            if p.menge is None or p.menge <= 0:
                probleme.append(f"{bez}: Menge {p.menge} ist nicht größer als 0")
        return probleme

    # --------------------------------------------------------------- Erzeugen

    def erzeuge_xml(self, paket: Belegpaket) -> bytes:
        """openTRANS-ORDER_LIST als UTF-8-Bytes; wirft OpenTransFehler bei Pflichtverstößen."""
        xml, _ = self._erzeuge(paket)
        return xml

    def _erzeuge(self, paket: Belegpaket) -> tuple[bytes, list[str]]:
        probleme = self.pruefe_paket(paket)
        if probleme:
            logger.warning("openTRANS-Pflichtprüfung für %s: %s", paket.roh.quelldatei.name, "; ".join(probleme))
            raise OpenTransFehler(probleme)
        warnungen: list[str] = []
        positionen = paket.aktive_positionen()

        root = ET.Element("ORDER_LIST")  # Lexware-Wurzel ohne Namespace [V]
        order = ET.SubElement(root, "ORDER")
        # Die Namespace-Deklarationen gehören laut Lexware-Spezifikation an das ORDER-Element,
        # nicht an ORDER_LIST; deshalb als gewöhnliche Attribute gesetzt (ElementTree würde sie
        # sonst an die Wurzel hängen).
        order.set("xmlns", OPENTRANS_NS)
        order.set("xmlns:xsi", XSI_NS)  # TODO-LEXWARE: optional laut Drucktool; Beispieldatei hat es
        order.set("version", OPENTRANS_VERSION)
        order.set("type", "standard")

        header = ET.SubElement(order, "ORDER_HEADER")
        self._control_info(header)
        self._order_info(header, paket, positionen, warnungen)
        self._order_item_list(order, positionen)
        self._order_summary(order, positionen, paket)

        ET.indent(root, space="  ")
        body = ET.tostring(root, encoding="unicode")
        text = '<?xml version="1.0" encoding="UTF-8"?>\n' + body + "\n"
        xml = text.encode("utf-8")
        logger.info(
            "openTRANS-XML erzeugt: Firma %s, Kunde %s, Bestellnr. %s, %d Positionen, netto %s",
            self.firma.id,
            paket.kunde.kunde.kundennummer if paket.kunde.kunde else "-",
            self._bestellnummer(paket),
            len(positionen),
            _betrag(paket.summe_netto_vk),
        )
        return xml, warnungen

    def _control_info(self, header: ET.Element) -> None:
        control = ET.SubElement(header, "CONTROL_INFO")  # wird nicht übernommen [V]
        ET.SubElement(control, "GENERATOR_INFO").text = GENERATOR_INFO
        ET.SubElement(control, "GENERATION_DATE").text = self.jetzt().isoformat(timespec="seconds")

    def _order_info(
        self,
        header: ET.Element,
        paket: Belegpaket,
        positionen: list[PositionsZuordnung],
        warnungen: list[str],
    ) -> None:
        roh = paket.roh
        kunde = paket.kunde.kunde
        assert kunde is not None  # durch pruefe_paket gesichert
        info = ET.SubElement(header, "ORDER_INFO")

        # ORDER_ID -> Feld "Bestellnummer" der AB [V]
        bestellnummer = self._bestellnummer(paket)
        if not (roh.bestellnummer and roh.bestellnummer.strip()):
            if roh.belegnummer and roh.belegnummer.strip():
                warnungen.append(f"Keine Kundenbestellnummer im Dokument; Belegnummer '{bestellnummer}' als ORDER_ID verwendet")
            else:
                warnungen.append(f"Keine Bestell-/Belegnummer im Dokument; Dateiname '{bestellnummer}' als ORDER_ID verwendet")
        ET.SubElement(info, "ORDER_ID").text = bestellnummer

        # ORDER_DATE: Datumsteil = Auftragsdatum laut Spez. 2009 [V]
        # TODO-LEXWARE: Handbuch 2026 sagt, der übernommene Beleg erhält das Tagesdatum -> prüfen.
        if roh.datum is None:
            warnungen.append("Kein Bestelldatum im Dokument; Tagesdatum als ORDER_DATE verwendet")
        ET.SubElement(info, "ORDER_DATE").text = self._iso(self._bestelldatum(paket))

        # DELIVERY_DATE: openTRANS-konform, im Lexware-Mapping nicht enthalten [A]
        # TODO-LEXWARE: prüfen, ob DELIVERY_DATE ein Lieferdatum im Auftrag setzt; sonst nur REMARK.
        if roh.liefertag is not None:
            liefer = ET.SubElement(info, "DELIVERY_DATE")
            liefer.set("type", "fixed")
            ET.SubElement(liefer, "DELIVERY_START_DATE").text = self._iso(roh.liefertag)
            ET.SubElement(liefer, "DELIVERY_END_DATE").text = self._iso(roh.liefertag)

        parteien = ET.SubElement(info, "ORDER_PARTIES")
        # Reihenfolge wie die offizielle Lexware-Beispieldatei: BUYER, INVOICE, SUPPLIER.
        # TODO-LEXWARE: openTRANS-DTD nennt BUYER, SUPPLIER, INVOICE; Lexware validiert offenbar nicht.
        if roh.lieferadresse and roh.lieferadresse.strip():
            # BUYER_PARTY = Lieferadresse; wird im Kundenstamm ergänzt, nie überschrieben [V]
            # TODO-LEXWARE: ob sie auch als abweichende Lieferanschrift im Beleg steht, ist unklar.
            lieferadresse = _parse_adresse(roh.lieferadresse)
            self._party(parteien, "BUYER_PARTY", lieferadresse)
            warnungen.append(
                "Abweichende Lieferadresse als BUYER_PARTY gesendet (heuristisch zerlegt) - "
                "in Lexware prüfen: "
                + _sauber(roh.lieferadresse)
            )
        rechnung = _Adresse(
            name=kunde.name,
            # TODO-LEXWARE: Lexware liest NAME2 als Nachname; im Stamm ist name2 die zweite Namenszeile.
            # Prüfen, ob der Namensfilter der eCommerce-Ansicht nur NAME vergleicht.
            name2=kunde.name2,
            strasse=kunde.strasse,
            plz=kunde.plz,
            ort=kunde.ort,
            land=kunde.land,  # Länderkürzel wird nicht umgewandelt [V]
            ust_id=kunde.ust_id,
            email=kunde.email,  # E-Mail dient dem Kundenabgleich [V]
        )
        # PARTY_ID wird laut Spez./Forum NICHT übernommen; schadet nicht [A]
        self._party(parteien, "INVOICE_PARTY", rechnung, party_id=kunde.kundennummer)
        # SUPPLIER_PARTY wird nicht übernommen; nur Standardkonformität [V]
        self._party(parteien, "SUPPLIER_PARTY", _Adresse(name=self.firma.lexware_firmenname))

        # TODO-LEXWARE: "EUR" (Beispieldatei) oder "978" (Spezifikation 2009)? Beides scheint zu gehen.
        ET.SubElement(info, "PRICE_CURRENCY").text = _sauber(self.firma.opentrans.waehrung) or "EUR"

        zahlung = ET.SubElement(ET.SubElement(ET.SubElement(info, "PAYMENT"), "CASH"), "PAYMENT_TERM")
        zahlung.set("TYPE", "unece")  # Großschreibung wie Beispieldatei [V]
        zahlung.text = ZAHLUNGSART_RECHNUNG

        steuergebiet = ET.SubElement(info, "REMARK")  # Pflicht seit Version 2012 [V]
        steuergebiet.set("type", "tax_area")
        steuergebiet.text = self.steuergebiet

        bemerkung = ET.SubElement(info, "REMARK")  # -> Bemerkungstext/Nachbemerkung [V]/[A]
        bemerkung.set("type", "order")
        bemerkung.text = self._bemerkung(paket, positionen, warnungen)

    def _party(
        self,
        parteien: ET.Element,
        tag: str,
        adresse: _Adresse,
        *,
        party_id: str | None = None,
    ) -> None:
        party = ET.SubElement(ET.SubElement(parteien, tag), "PARTY")
        if party_id:
            pid = ET.SubElement(party, "PARTY_ID")
            pid.set("type", "supplier_specific")  # vom Lieferanten (uns) vergebene Kundennummer
            pid.text = _sauber(party_id)
        addr = ET.SubElement(party, "ADDRESS")
        # Reihenfolge laut openTRANS-1.0-Elementbibliothek ADDRESS
        felder = (
            ("NAME", adresse.name),
            ("NAME2", adresse.name2),
            ("NAME3", adresse.name3),
            ("STREET", adresse.strasse),
            ("ZIP", adresse.plz),
            ("CITY", adresse.ort),
            ("COUNTRY", adresse.land),
            ("VAT_ID", adresse.ust_id),
            ("EMAIL", adresse.email),
        )
        for name, wert in felder:
            text = _sauber(wert)
            if text:
                ET.SubElement(addr, name).text = text

    def _bemerkung(
        self,
        paket: Belegpaket,
        positionen: list[PositionsZuordnung],
        warnungen: list[str],
    ) -> str:
        """Klartext für die Nachbemerkung: alles, was Lexware nicht aus XML-Feldern übernimmt."""
        roh = paket.roh
        kunde = paket.kunde.kunde
        assert kunde is not None
        teile: list[str] = [f"Kunde {kunde.kundennummer} {_sauber(kunde.name)}"]
        if roh.bestellnummer:
            teile.append(f"Kundenbestellung {_sauber(roh.bestellnummer)}")
        if roh.belegnummer:
            teile.append(f"Beleg {_sauber(roh.belegnummer)}")
        if roh.datum:
            teile.append(f"Bestelldatum {_deutsches_datum(roh.datum)}")
        if roh.liefertag:
            teile.append(f"Liefertermin {_deutsches_datum(roh.liefertag)}")
        if roh.lieferadresse and roh.lieferadresse.strip():
            teile.append(f"Lieferadresse: {_sauber(roh.lieferadresse)}")
        if roh.lieferbedingung:
            teile.append(f"Lieferbedingung: {_sauber(roh.lieferbedingung)}")
        if paket.lieferant.lieferant is not None:
            teile.append(f"Lieferant {paket.lieferant.lieferant.lieferantennummer} {_sauber(paket.lieferant.lieferant.name)}")
        # Belegfelder sind nicht per XML setzbar [A] -> Klartext, damit der Bediener sie nachträgt
        for feld, wert in paket.belegfelder.model_dump().items():
            if wert:
                teile.append(f"{feld.capitalize()}: {_sauber(wert)}")
        for p in positionen:
            artnr = p.artikel.artikelnummer if p.artikel else "?"
            if p.roh.hinweis:
                teile.append(f"Pos. {p.roh.laufnr} ({artnr}): {_sauber(p.roh.hinweis)}")
            if p.roh.lieferadresse and p.roh.lieferadresse.strip():
                teile.append(f"Pos. {p.roh.laufnr} ({artnr}) Lieferadresse: {_sauber(p.roh.lieferadresse)}")
                warnungen.append(
                    f"Position {p.roh.laufnr}: positionsbezogene Lieferadresse kann openTRANS/Lexware "
                    "nicht abbilden - nur als Bemerkung übergeben"
                )
        uebersprungen = [p for p in paket.positionen if p.uebersprungen]
        if uebersprungen:
            liste = ", ".join(f"Pos. {p.roh.laufnr} '{_sauber(p.roh.bezeichnung)[:40]}'" for p in uebersprungen)
            teile.append(f"Nicht übernommen (übersprungen): {liste}")
        hinweise = [h for h in paket.alle_hinweise if h.schweregrad != Schweregrad.INFO]
        if hinweise:
            texte = " | ".join(f"{h.code.value}: {_sauber(h.text)}" for h in hinweise)
            teile.append(f"Hinweise: {texte}")
        teile.append(f"Quelldatei: {_sauber(roh.quelldatei.name)}")
        teile.append(f"Erzeugt von {GENERATOR_INFO} am {self.jetzt():%d.%m.%Y %H:%M}")
        text = "; ".join(teile)
        if len(text) > MAX_BEMERKUNG_ZEICHEN:
            text = text[: MAX_BEMERKUNG_ZEICHEN - 1].rstrip() + "…"
            warnungen.append(f"Bemerkung auf {MAX_BEMERKUNG_ZEICHEN} Zeichen gekürzt")
        return text

    def _order_item_list(self, order: ET.Element, positionen: list[PositionsZuordnung]) -> None:
        liste = ET.SubElement(order, "ORDER_ITEM_LIST")
        for nr, p in enumerate(positionen, 1):
            assert p.artikel is not None and p.vk_preis is not None  # durch pruefe_paket gesichert
            item = ET.SubElement(liste, "ORDER_ITEM")
            ET.SubElement(item, "LINE_ITEM_ID").text = str(nr)  # wird nicht übernommen [V]
            artikel = ET.SubElement(item, "ARTICLE_ID")
            # SUPPLIER_AID = Lexware-Artikelnummer; unbekannt -> Position verworfen [V]
            ET.SubElement(artikel, "SUPPLIER_AID").text = _sauber(p.artikel.artikelnummer)
            # DESCRIPTION_SHORT -> Artikelbezeichnung/Matchcode [V]; Stammbezeichnung, nie Dokumenttext.
            # TODO-LEXWARE: DESCRIPTION_LONG (Positionstext) bewusst weggelassen, damit der Stammtext
            # nicht durch einen leeren Text ersetzt wird.
            ET.SubElement(artikel, "DESCRIPTION_SHORT").text = _sauber(p.artikel.bezeichnung)
            ET.SubElement(item, "QUANTITY").text = _menge(p.menge)
            # ORDER_UNIT wird nicht übernommen [V]; Einheit aus dem Stamm, sonst "1" wie Beispieldatei
            ET.SubElement(item, "ORDER_UNIT").text = _sauber(p.artikel.einheit or p.roh.einheit) or "1"
            preis = ET.SubElement(item, "ARTICLE_PRICE")
            preis.set("type", self.preistyp)
            # Preis = vk_preis (Preisgruppe 1 aus dem Stamm), NIEMALS preis_dokument [Geschäftsregel 3]
            ET.SubElement(preis, "PRICE_AMOUNT").text = _betrag(p.vk_preis)
            ET.SubElement(preis, "PRICE_LINE_AMOUNT").text = _betrag(p.vk_preis * p.menge)
            ET.SubElement(preis, "TAX").text = _steuersatz(self.firma.opentrans.mwst_satz)
            if self.firmen_guid:
                # Mandantenschutz: Import nur in diese Firma möglich [V]
                guid = ET.SubElement(item, "REMARK")
                guid.set("type", "origin_company_id")
                guid.text = _sauber(self.firmen_guid)

    def _order_summary(self, order: ET.Element, positionen: list[PositionsZuordnung], paket: Belegpaket) -> None:
        summary = ET.SubElement(order, "ORDER_SUMMARY")
        ET.SubElement(summary, "TOTAL_ITEM_NUM").text = str(len(positionen))
        summe = sum(
            ((p.vk_preis or Decimal(0)) * p.menge).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            for p in positionen
        )
        summe = Decimal(summe)
        if summe != paket.summe_netto_vk:
            logger.warning(
                "Summe der XML-Positionen (%s) weicht von paket.summe_netto_vk (%s) ab",
                _betrag(summe),
                _betrag(paket.summe_netto_vk),
            )
        # TOTAL_AMOUNT = Summe der PRICE_LINE_AMOUNT (netto bei net_list) [A]
        ET.SubElement(summary, "TOTAL_AMOUNT").text = _betrag(summe)

    # -------------------------------------------------------------- Validieren

    def validiere_xml(self, xml: bytes) -> list[str]:
        """Prüft Wohlgeformtheit, Pflichtelemente, Reihenfolge, Zahlenformate und Summen.

        Liefert eine leere Liste, wenn alles stimmt. Es wird nicht gegen DTD/XSD validiert
        (keine Downloads; Lexware validiert offenbar ebenfalls nicht).
        """
        probleme: list[str] = []
        if b"<!DOCTYPE" in xml:
            probleme.append("DOCTYPE-Deklaration vorhanden (Lexware-Beispieldatei und Spezifikation haben keine)")
        try:
            root = ET.fromstring(xml)
        except ET.ParseError as exc:
            return [f"XML nicht wohlgeformt: {exc}"]
        if root.tag != "ORDER_LIST":
            probleme.append(f"Wurzelelement muss ORDER_LIST ohne Namespace sein, ist '{root.tag}'")
        orders = _kinder(root, "ORDER")
        if not orders:
            probleme.append("Kein ORDER-Element in ORDER_LIST")
        for nr, order in enumerate(orders, 1):
            probleme.extend(f"ORDER {nr}: {p}" for p in self._pruefe_order(order))
        for elem in root.iter():
            text = elem.text or ""
            if "€" in text:
                probleme.append(f"Euro-Zeichen in <{_lokal(elem.tag)}> (Lexware: 'EUR' verwenden)")
            if _STEUERZEICHEN.search(text):
                probleme.append(f"Steuerzeichen in <{_lokal(elem.tag)}>")
        return probleme

    def _pruefe_order(self, order: ET.Element) -> list[str]:
        probleme: list[str] = []
        if order.tag != f"{{{OPENTRANS_NS}}}ORDER":
            probleme.append(f"ORDER muss im Namespace {OPENTRANS_NS} liegen")
        if order.get("version") != OPENTRANS_VERSION:
            probleme.append(f"ORDER version muss '{OPENTRANS_VERSION}' sein, ist '{order.get('version')}'")
        if order.get("type") != "standard":
            probleme.append(f"ORDER type muss 'standard' sein, ist '{order.get('type')}'")

        info = _pfad(order, "ORDER_HEADER", "ORDER_INFO")
        if info is None:
            probleme.append("ORDER_HEADER/ORDER_INFO fehlt")
        else:
            probleme.extend(self._pruefe_order_info(info))

        items = _kinder(_kind(order, "ORDER_ITEM_LIST"), "ORDER_ITEM")
        if not items:
            probleme.append("ORDER_ITEM_LIST ohne ORDER_ITEM (mindestens eine Position mit Stammartikel nötig)")
        zeilensumme = Decimal(0)
        for nr, item in enumerate(items, 1):
            zeile, fehler = self._pruefe_item(item)
            probleme.extend(f"Position {nr}: {f}" for f in fehler)
            zeilensumme += zeile

        summary = _kind(order, "ORDER_SUMMARY")
        if summary is None:
            probleme.append("ORDER_SUMMARY fehlt")
        else:
            anzahl = _inhalt(_kind(summary, "TOTAL_ITEM_NUM"))
            if anzahl != str(len(items)):
                probleme.append(f"TOTAL_ITEM_NUM '{anzahl}' passt nicht zu {len(items)} Positionen")
            gesamt_text = _inhalt(_kind(summary, "TOTAL_AMOUNT"))
            gesamt = _dezimal(gesamt_text) if _BETRAG_MUSTER.match(gesamt_text) else None
            if gesamt is None:
                probleme.append(f"TOTAL_AMOUNT '{gesamt_text}' fehlt oder hat nicht das Format 0.00")
            elif gesamt != zeilensumme:
                probleme.append(f"TOTAL_AMOUNT {gesamt_text} != Summe der Positionen {_betrag(zeilensumme)}")
        return probleme

    def _pruefe_order_info(self, info: ET.Element) -> list[str]:
        probleme: list[str] = []
        raenge: list[int] = []
        for kind in info:
            name = _lokal(kind.tag)
            if name not in _ORDER_INFO_REIHENFOLGE:
                probleme.append(f"Unbekanntes Element ORDER_INFO/{name}")
                continue
            raenge.append(_ORDER_INFO_REIHENFOLGE.index(name))
        if raenge != sorted(raenge):
            probleme.append(
                "Reihenfolge in ORDER_INFO falsch (erwartet: ORDER_ID, ORDER_DATE, [DELIVERY_DATE], "
                "ORDER_PARTIES, PRICE_CURRENCY, [PAYMENT], REMARK*)"
            )
        if not _inhalt(_kind(info, "ORDER_ID")):
            probleme.append("ORDER_ID fehlt oder ist leer")
        datum = _inhalt(_kind(info, "ORDER_DATE"))
        if not _ISO_DATUM_MUSTER.match(datum):
            probleme.append(f"ORDER_DATE '{datum}' ist kein ISO-8601-Zeitpunkt mit Offset")
        liefer = _kind(info, "DELIVERY_DATE")
        if liefer is not None:
            for name in ("DELIVERY_START_DATE", "DELIVERY_END_DATE"):
                wert = _inhalt(_kind(liefer, name))
                if not _ISO_DATUM_MUSTER.match(wert):
                    probleme.append(f"DELIVERY_DATE/{name} '{wert}' ist kein ISO-8601-Zeitpunkt mit Offset")
        parteien = _kind(info, "ORDER_PARTIES")
        if parteien is None:
            probleme.append("ORDER_PARTIES fehlt")
        else:
            if not _inhalt(_pfad(parteien, "INVOICE_PARTY", "PARTY", "ADDRESS", "NAME")):
                probleme.append("INVOICE_PARTY/PARTY/ADDRESS/NAME fehlt oder ist leer (Kundenname)")
            buyer = _kind(parteien, "BUYER_PARTY")
            if buyer is not None and not _inhalt(_pfad(buyer, "PARTY", "ADDRESS", "NAME")):
                probleme.append("BUYER_PARTY vorhanden, aber ohne PARTY/ADDRESS/NAME")
        if not _inhalt(_kind(info, "PRICE_CURRENCY")):
            probleme.append("PRICE_CURRENCY fehlt oder ist leer")
        steuergebiete = [_inhalt(r) for r in _kinder(info, "REMARK") if r.get("type") == "tax_area"]
        if not steuergebiete:
            probleme.append('REMARK type="tax_area" fehlt (Pflicht seit Lexware 2012)')
        elif steuergebiete[0] not in _STEUERGEBIETE:
            probleme.append(f"REMARK tax_area '{steuergebiete[0]}' ist nicht Merchant/EU/Non_EU")
        return probleme

    def _pruefe_item(self, item: ET.Element) -> tuple[Decimal, list[str]]:
        probleme: list[str] = []
        artnr = _inhalt(_pfad(item, "ARTICLE_ID", "SUPPLIER_AID"))
        if not artnr:
            probleme.append("ARTICLE_ID/SUPPLIER_AID fehlt oder ist leer (Artikelnummer)")
        menge_text = _inhalt(_kind(item, "QUANTITY"))
        menge = _dezimal(menge_text)
        if menge is None or menge <= 0:
            probleme.append(f"QUANTITY '{menge_text}' ist keine Menge > 0")
        preis = _kind(item, "ARTICLE_PRICE")
        zeile = Decimal(0)
        if preis is None:
            probleme.append("ARTICLE_PRICE fehlt")
            return zeile, probleme
        if preis.get("type") not in _PREISTYPEN:
            probleme.append(f"ARTICLE_PRICE type '{preis.get('type')}' ist nicht net_list/gros_list")
        einzel_text = _inhalt(_kind(preis, "PRICE_AMOUNT"))
        zeile_text = _inhalt(_kind(preis, "PRICE_LINE_AMOUNT"))
        steuer_text = _inhalt(_kind(preis, "TAX"))
        einzel = _dezimal(einzel_text) if _BETRAG_MUSTER.match(einzel_text) else None
        zeile_wert = _dezimal(zeile_text) if _BETRAG_MUSTER.match(zeile_text) else None
        if einzel is None:
            probleme.append(f"PRICE_AMOUNT '{einzel_text}' fehlt oder hat nicht das Format 0.00")
        if zeile_wert is None:
            probleme.append(f"PRICE_LINE_AMOUNT '{zeile_text}' fehlt oder hat nicht das Format 0.00")
        if not _STEUER_MUSTER.match(steuer_text):
            probleme.append(f"TAX '{steuer_text}' hat nicht das Format 0.19")
        if einzel is not None and zeile_wert is not None and menge is not None:
            erwartet = (einzel * menge).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if erwartet != zeile_wert:
                probleme.append(f"PRICE_LINE_AMOUNT {zeile_text} != QUANTITY x PRICE_AMOUNT = {_betrag(erwartet)}")
        if zeile_wert is not None:
            zeile = zeile_wert
        return zeile, probleme

    # ---------------------------------------------------------------- Dateiname

    def _inhalts_hash(self, paket: Belegpaket) -> str:
        """Kurzer, stabiler Hash über den fachlichen Inhalt (ohne Erzeugungszeitpunkt)."""
        kunde = paket.kunde.kunde
        roh = paket.roh
        teile = [
            self.firma.id,
            kunde.kundennummer if kunde else "",
            self._bestellnummer(paket),
            self._bestelldatum(paket).isoformat(),
            roh.liefertag.isoformat() if roh.liefertag else "",
            _sauber(roh.lieferadresse),
        ]
        for p in paket.aktive_positionen():
            artnr = p.artikel.artikelnummer if p.artikel else "?"
            vk = _betrag(p.vk_preis) if p.vk_preis is not None else ""
            teile.append(f"{artnr}|{_menge(p.menge)}|{vk}")
        return hashlib.sha256("\n".join(teile).encode("utf-8")).hexdigest()[:8]

    def dateiname(self, paket: Belegpaket) -> str:
        """<JJJJMMTT>_<Kundennr>_<Bestellnr bereinigt>_<Hash>.xml - eindeutig und reproduzierbar."""
        kunde = paket.kunde.kunde
        kundennr = _bereinige_dateiteil(kunde.kundennummer) if kunde else ""
        bestellnr = _bereinige_dateiteil(self._bestellnummer(paket))
        return (
            f"{self._bestelldatum(paket):%Y%m%d}_{kundennr or 'ohne-kunde'}_"
            f"{bestellnr or 'ohne-bestellnr'}_{self._inhalts_hash(paket)}.xml"
        )

    # ---------------------------------------------------------------- Schreiben

    def _zielordner(self, paket: Belegpaket, dry_run: bool) -> Path:
        """Ausgabeordner ermitteln und Voraussetzungen prüfen; OpenTransFehler bei Verstößen."""
        if dry_run:
            ordner = Path(self.settings.xml_ausgabe_ordner)
            try:
                ordner.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise OpenTransFehler([f"Ausgabeordner {ordner} kann nicht angelegt werden: {exc}"]) from exc
            return ordner
        probleme: list[str] = []
        konfig = self.firma.opentrans
        if konfig.lizenziert is None:
            probleme.append(
                f"Firma '{self.firma.id}': opentrans.lizenziert ist noch nicht geprüft (null). In Lexware "
                "prüfen: Firmenangaben -> Services -> 'Anbindung an Lexware eCommerce', dann in "
                "config/firmen.yaml eintragen"
            )
        elif konfig.lizenziert is not True:
            probleme.append(
                f"Firma '{self.firma.id}': Lexware eCommerce (Standard-Shopschnittstelle) ist nicht "
                "lizenziert/aktiviert (opentrans.lizenziert = false) - Verkauf-AB per UI-Automation anlegen"
            )
        if konfig.import_ordner is None:
            probleme.append(f"Firma '{self.firma.id}': opentrans.import_ordner ist nicht konfiguriert")
        elif not Path(konfig.import_ordner).is_dir():
            probleme.append(
                f"Firma '{self.firma.id}': opentrans.import_ordner {konfig.import_ordner} existiert nicht "
                "(wird im scharfen Betrieb bewusst nicht angelegt)"
            )
        if not paket.freigegeben:
            probleme.append("Paket ist nicht freigegeben - scharfes Schreiben nur nach Freigabe")
        if paket.blockiert:
            probleme.append("Paket ist blockiert (Prüfhinweise mit Schweregrad FEHLER)")
        if probleme:
            raise OpenTransFehler(probleme)
        assert konfig.import_ordner is not None
        return Path(konfig.import_ordner)

    def _fehlschlag(self, paket: Belegpaket, dry_run: bool, fehler: str, warnungen: list[str] | None = None) -> Belegergebnis:
        logger.error("openTRANS Verkauf-AB (%s) für %s fehlgeschlagen: %s", "Dry Run" if dry_run else "scharf", paket.roh.quelldatei.name, fehler)
        return Belegergebnis(
            belegart=Belegart.AUFTRAGSBESTAETIGUNG,
            belegnummer=None,
            netto=paket.summe_netto_vk,
            brutto=self._brutto(paket.summe_netto_vk),
            erfolgreich=False,
            dry_run=dry_run,
            methode="opentrans",
            fehler=fehler,
            warnungen=list(warnungen or []),
        )

    def _brutto(self, netto: Decimal) -> Decimal:
        faktor = Decimal(1) + Decimal(self.firma.opentrans.mwst_satz) / Decimal(100)
        return (netto * faktor).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    def schreibe_verkauf_ab(self, paket: Belegpaket, *, dry_run: bool = True) -> Belegergebnis:
        """Schreibt die openTRANS-XML für die Verkauf-AB.

        Dry Run: nach ``settings.xml_ausgabe_ordner``. Scharf: nach ``firma.opentrans.import_ordner``
        (nur wenn lizenziert, Ordner vorhanden, Paket freigegeben und nicht blockiert). Eine schon
        vorhandene Datei gleichen Namens wird nie überschrieben. Die Belegnummer bleibt None, weil
        Lexware sie erst beim Übernehmen des Webshop-Auftrags vergibt.
        """
        modus = "Dry Run" if dry_run else "scharf"
        logger.info("openTRANS Verkauf-AB (%s): Firma %s, Quelldatei %s", modus, self.firma.id, paket.roh.quelldatei.name)
        try:
            ordner = self._zielordner(paket, dry_run)
            xml, warnungen = self._erzeuge(paket)
        except OpenTransFehler as exc:
            return self._fehlschlag(paket, dry_run, "; ".join(exc.probleme))

        fehler = self.validiere_xml(xml)
        if fehler:
            return self._fehlschlag(paket, dry_run, "Erzeugte XML ist ungültig: " + "; ".join(fehler), warnungen)

        ziel = ordner / self.dateiname(paket)
        try:
            # "xb": schlägt fehl, wenn die Datei schon existiert (kein Überschreiben, kein Doppelimport)
            with ziel.open("xb") as f:
                f.write(xml)
        except FileExistsError:
            return self._fehlschlag(
                paket,
                dry_run,
                f"Datei {ziel} existiert bereits und wird nicht überschrieben (möglicher Doppelimport; "
                "Dublettenprüfung und Ablageordner kontrollieren)",
                warnungen,
            )
        except OSError as exc:
            return self._fehlschlag(paket, dry_run, f"Datei {ziel} kann nicht geschrieben werden: {exc}", warnungen)

        netto = paket.summe_netto_vk
        warnungen = [
            "Belegnummer wird erst beim Import in Lexware vergeben",
            *warnungen,
        ]
        if dry_run:
            warnungen.append(f"Dry Run: XML nur nach {ziel} geschrieben, nicht in den Lexware-Importordner")
        else:
            warnungen.append(
                "Kein automatischer Import: " + import_anleitung(self.firma, ziel)
            )
        warnungen.append(
            "Beim Übernehmen in Lexware prüfen: Kunde zugeordnet (sonst Neukunde), Preise = VK Preisgruppe 1 "
            "(kundenspezifische Preise/Preisaktionen könnten sie ersetzen), Belegfelder Spedition/Weingut/"
            "Shipment/Kunde nachtragen"
        )
        logger.info("openTRANS-XML geschrieben (%s): %s (netto %s)", modus, ziel, _betrag(netto))
        return Belegergebnis(
            belegart=Belegart.AUFTRAGSBESTAETIGUNG,
            belegnummer=None,
            netto=netto,
            brutto=self._brutto(netto),
            erfolgreich=True,
            dry_run=dry_run,
            methode="opentrans",
            warnungen=warnungen,
            artefakt=ziel,
        )

    # ------------------------------------------ Protokoll LexwareSchreiber (Rest)

    def schreibe_einkauf_bestellung(self, paket: Belegpaket, *, dry_run: bool = True) -> Belegergebnis:
        """Nicht möglich: die Shopschnittstelle legt nur Verkaufsaufträge an (AB/Rechnung/Lieferschein)."""
        text = (
            "Einkaufs-Bestellungen sind über die Lexware-Standard-Shopschnittstelle nicht möglich "
            "(openTRANS-Import erzeugt nur Verkaufsaufträge). Bestellung per UI-Automation "
            "(Bestellassistent) oder 'Auftrag wandeln' anlegen."
        )
        logger.warning("openTRANS: %s (Quelldatei %s)", text, paket.roh.quelldatei.name)
        return Belegergebnis(
            belegart=Belegart.BESTELLUNG,
            netto=paket.summe_netto_ek,
            erfolgreich=False,
            dry_run=dry_run,
            methode="opentrans",
            fehler=text,
        )

    def auftrag_weiterfuehren_lieferschein(
        self, paket: Belegpaket, ab_belegnummer: str, *, dry_run: bool = True
    ) -> Belegergebnis:
        """Nicht möglich: 'Auftrag weiterführen' ist eine UI-Funktion, kein XML-Import."""
        text = (
            f"Lieferschein zur AB {ab_belegnummer} kann nicht per openTRANS erzeugt werden; "
            "'Auftrag weiterführen' nur per Lexware-Oberfläche (UI-Automation)."
        )
        logger.warning("openTRANS: %s", text)
        return Belegergebnis(
            belegart=Belegart.LIEFERSCHEIN,
            erfolgreich=False,
            dry_run=dry_run,
            methode="opentrans",
            fehler=text,
        )
