"""Tests für lexware.opentrans (openTRANS-XML für die Lexware-Standard-Shopschnittstelle)."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from auftragsautomat.config import Settings
from auftragsautomat.lexware.opentrans import (
    OPENTRANS_NS,
    OpenTransFehler,
    OpenTransSchreiber,
    import_anleitung,
)
from auftragsautomat.models import (
    Artikel,
    Belegart,
    Belegfelder,
    Belegpaket,
    Eingangstyp,
    Extraktionsmethode,
    Kunde,
    KundeZuordnung,
    MatchStatus,
    PositionsZuordnung,
    Pruefcode,
    Pruefhinweis,
    RohBestellung,
    RohPosition,
    Schweregrad,
)

NS = {"o": OPENTRANS_NS}
FESTE_ZEIT = datetime(2026, 10, 18, 9, 15)


def _kunde_3441() -> Kunde:
    return Kunde(
        firma_id="wik_leh",
        kundennummer="3441",
        name="Hegmann",
        name2="Weinmarkt Bottrop-Kirchhellen",
        strasse="Hauptstraße 1",
        plz="46244",
        ort="Bottrop",
        land="DE",
        preisgruppe=1,
        email="bestellung@hegmann.example",
    )


def _artikel(nummer: str, bezeichnung: str) -> Artikel:
    return Artikel(
        firma_id="wik_leh",
        artikelnummer=nummer,
        bezeichnung=bezeichnung,
        warengruppe="Cide",
        lieferantennummer="72",
        volumen_cl=Decimal("75"),
        ek_preis=Decimal("4.20"),
        vk_preis_1=Decimal("6.90"),
        einheit="Fl",
    )


def _paket() -> Belegpaket:
    """Kundenbestellung Weinmarkt Kirchhellen: 60 x 1643, 24 x 957, 'DAA Neptun' übersprungen."""
    roh = RohBestellung(
        eingangstyp=Eingangstyp.KUNDENBESTELLUNG_PDF,
        quelldatei=Path("eingang/wik_leh/kundenbestellung_kirchhellen.pdf"),
        extraktionsmethode=Extraktionsmethode.PDFPLUMBER,
        kunde_text="Weinmarkt Kirchhellen",
        belegnummer="2026-0917",
        bestellnummer="B-4711",
        datum=date(2026, 9, 10),
        liefertag=date(2026, 9, 15),
        lieferadresse="Weinmarkt Kirchhellen Lager\nIndustriestraße 5\n46244 Bottrop\nDeutschland",
        positionen=[
            RohPosition(laufnr=1, bezeichnung="Gloria Secco Frizzante", menge=Decimal("60"), preis=Decimal("6.50"), hinweis="11+1"),
            RohPosition(laufnr=2, bezeichnung="Rosato Secco Frizzante", menge=Decimal("24"), preis=Decimal("6.90")),
            RohPosition(laufnr=3, bezeichnung="DAA Neptun", menge=Decimal("1"), preis=Decimal("0")),
        ],
    )
    gloria = _artikel("1643", "Villa Armellina Gloria Secco Frizzante 0,75 l")
    rosato = _artikel("957", "Villa Armellina Rosato Secco Frizzante 0,75 l")
    return Belegpaket(
        roh=roh,
        firma_id="wik_leh",
        kunde=KundeZuordnung(kunde=_kunde_3441(), status=MatchStatus.SICHER, quelle="alias"),
        positionen=[
            PositionsZuordnung(
                roh=roh.positionen[0],
                artikel=gloria,
                status=MatchStatus.SICHER,
                menge=Decimal("60"),
                ek_preis=Decimal("4.20"),
                vk_preis=Decimal("6.90"),
                preis_dokument=Decimal("6.50"),  # PDF-Preis weicht ab und darf NIE in die XML
                hinweise=[
                    Pruefhinweis(
                        code=Pruefcode.PREIS_ABWEICHUNG,
                        schweregrad=Schweregrad.WARNUNG,
                        text="VK 6,90 != PDF 6,50",
                        position_laufnr=1,
                    )
                ],
            ),
            PositionsZuordnung(
                roh=roh.positionen[1],
                artikel=rosato,
                status=MatchStatus.SICHER,
                menge=Decimal("24"),
                ek_preis=Decimal("4.20"),
                vk_preis=Decimal("6.90"),
                preis_dokument=Decimal("6.90"),
            ),
            PositionsZuordnung(
                roh=roh.positionen[2],
                status=MatchStatus.UEBERSPRUNGEN,
                menge=Decimal("0"),
                preis_dokument=Decimal("0"),
                hinweise=[
                    Pruefhinweis(
                        code=Pruefcode.NULLPREIS_OHNE_ARTIKEL,
                        schweregrad=Schweregrad.WARNUNG,
                        text="0,00 € ohne Stammartikel, übersprungen",
                        position_laufnr=3,
                    )
                ],
            ),
        ],
        belegfelder=Belegfelder(spedition="Spedition Weinlogistik", weingut="Cide srl", shipment="15.09.2026", kunde="Hegmann"),
    )


@pytest.fixture()
def paket() -> Belegpaket:
    return _paket()


@pytest.fixture()
def schreiber(settings: Settings) -> OpenTransSchreiber:
    return OpenTransSchreiber(settings, settings.firma("wik_leh"), jetzt=lambda: FESTE_ZEIT)


def _order(xml: bytes) -> ET.Element:
    root = ET.fromstring(xml)
    assert root.tag == "ORDER_LIST"
    orders = root.findall("o:ORDER", NS)
    assert len(orders) == 1
    return orders[0]


def _info(order: ET.Element) -> ET.Element:
    info = order.find("o:ORDER_HEADER/o:ORDER_INFO", NS)
    assert info is not None
    return info


def _text(elem: ET.Element, pfad: str) -> str:
    kind = elem.find(pfad, NS)
    assert kind is not None, pfad
    return (kind.text or "").strip()


# ---------------------------------------------------------------------------
# erzeuge_xml
# ---------------------------------------------------------------------------


def test_xml_kopf_und_struktur(schreiber: OpenTransSchreiber, paket: Belegpaket) -> None:
    xml = schreiber.erzeuge_xml(paket)
    text = xml.decode("utf-8")
    assert text.startswith('<?xml version="1.0" encoding="UTF-8"?>\n<ORDER_LIST>\n  <ORDER xmlns="http://www.opentrans.org/XMLSchema/1.0"')
    assert "<!DOCTYPE" not in text
    assert "\n    <ORDER_HEADER>" in text  # eingerückt
    order = _order(xml)
    assert order.get("version") == "1.0"
    assert order.get("type") == "standard"
    assert _text(order, "o:ORDER_HEADER/o:CONTROL_INFO/o:GENERATOR_INFO") == "auftragsautomat 0.1"
    assert _text(order, "o:ORDER_HEADER/o:CONTROL_INFO/o:GENERATION_DATE").startswith("2026-10-18T09:15:00+")


def test_order_info_inhalte(schreiber: OpenTransSchreiber, paket: Belegpaket) -> None:
    info = _info(_order(schreiber.erzeuge_xml(paket)))
    assert _text(info, "o:ORDER_ID") == "B-4711"
    assert re.fullmatch(r"2026-09-10T00:00:00[+-]\d{2}:\d{2}", _text(info, "o:ORDER_DATE"))
    assert info.find("o:DELIVERY_DATE", NS).get("type") == "fixed"
    assert _text(info, "o:DELIVERY_DATE/o:DELIVERY_START_DATE").startswith("2026-09-15T00:00:00")
    assert _text(info, "o:DELIVERY_DATE/o:DELIVERY_END_DATE").startswith("2026-09-15T00:00:00")
    assert _text(info, "o:PRICE_CURRENCY") == "EUR"
    zahlung = info.find("o:PAYMENT/o:CASH/o:PAYMENT_TERM", NS)
    assert zahlung is not None and zahlung.get("TYPE") == "unece" and zahlung.text == "10"
    bemerkungen = {r.get("type"): (r.text or "") for r in info.findall("o:REMARK", NS)}
    assert bemerkungen["tax_area"] == "Merchant"
    order_text = bemerkungen["order"]
    for erwartet in (
        "Kunde 3441 Hegmann",
        "Kundenbestellung B-4711",
        "Liefertermin 15.09.2026",
        "Lieferadresse: Weinmarkt Kirchhellen Lager Industriestraße 5 46244 Bottrop Deutschland",
        "Spedition: Spedition Weinlogistik",
        "Weingut: Cide srl",
        "Shipment: 15.09.2026",
        "Pos. 1 (1643): 11+1",
        "Pos. 3 'DAA Neptun'",
        "PREIS_ABWEICHUNG",
        "Quelldatei: kundenbestellung_kirchhellen.pdf",
    ):
        assert erwartet in order_text, erwartet
    assert "\n" not in order_text


def test_order_info_reihenfolge(schreiber: OpenTransSchreiber, paket: Belegpaket) -> None:
    info = _info(_order(schreiber.erzeuge_xml(paket)))
    namen = [kind.tag.rsplit("}", 1)[-1] for kind in info]
    assert namen == [
        "ORDER_ID",
        "ORDER_DATE",
        "DELIVERY_DATE",
        "ORDER_PARTIES",
        "PRICE_CURRENCY",
        "PAYMENT",
        "REMARK",
        "REMARK",
    ]


def test_parteien(schreiber: OpenTransSchreiber, paket: Belegpaket) -> None:
    parteien = _info(_order(schreiber.erzeuge_xml(paket))).find("o:ORDER_PARTIES", NS)
    assert parteien is not None
    assert [k.tag.rsplit("}", 1)[-1] for k in parteien] == ["BUYER_PARTY", "INVOICE_PARTY", "SUPPLIER_PARTY"]
    rechnung = parteien.find("o:INVOICE_PARTY/o:PARTY", NS)
    assert rechnung is not None
    pid = rechnung.find("o:PARTY_ID", NS)
    assert pid is not None and pid.text == "3441" and pid.get("type") == "supplier_specific"
    adresse = rechnung.find("o:ADDRESS", NS)
    assert adresse is not None
    assert [k.tag.rsplit("}", 1)[-1] for k in adresse] == ["NAME", "NAME2", "STREET", "ZIP", "CITY", "COUNTRY", "EMAIL"]
    assert _text(adresse, "o:NAME") == "Hegmann"
    assert _text(adresse, "o:NAME2") == "Weinmarkt Bottrop-Kirchhellen"
    assert _text(adresse, "o:STREET") == "Hauptstraße 1"
    assert _text(adresse, "o:ZIP") == "46244"
    assert _text(adresse, "o:CITY") == "Bottrop"
    assert _text(adresse, "o:COUNTRY") == "DE"
    assert _text(adresse, "o:EMAIL") == "bestellung@hegmann.example"
    # abweichende Lieferadresse -> BUYER_PARTY (heuristisch zerlegt)
    liefer = parteien.find("o:BUYER_PARTY/o:PARTY/o:ADDRESS", NS)
    assert liefer is not None
    assert liefer.find("o:PARTY_ID", NS) is None
    assert _text(liefer, "o:NAME") == "Weinmarkt Kirchhellen Lager"
    assert _text(liefer, "o:STREET") == "Industriestraße 5"
    assert _text(liefer, "o:ZIP") == "46244"
    assert _text(liefer, "o:CITY") == "Bottrop"
    assert _text(liefer, "o:COUNTRY") == "Deutschland"
    assert _text(parteien, "o:SUPPLIER_PARTY/o:PARTY/o:ADDRESS/o:NAME") == "Wein-Import-Kreis LEH"


def test_ohne_lieferadresse_kein_buyer_party(schreiber: OpenTransSchreiber, paket: Belegpaket) -> None:
    paket.roh.lieferadresse = None
    parteien = _info(_order(schreiber.erzeuge_xml(paket))).find("o:ORDER_PARTIES", NS)
    assert parteien is not None
    assert parteien.find("o:BUYER_PARTY", NS) is None
    assert parteien.find("o:INVOICE_PARTY", NS) is not None


def test_lieferadresse_einzeilig_und_ohne_plz(schreiber: OpenTransSchreiber, paket: Belegpaket) -> None:
    paket.roh.lieferadresse = "Lager Nord, Am Hafen 3, D-46244 Bottrop"
    liefer = _info(_order(schreiber.erzeuge_xml(paket))).find("o:ORDER_PARTIES/o:BUYER_PARTY/o:PARTY/o:ADDRESS", NS)
    assert liefer is not None
    assert _text(liefer, "o:NAME") == "Lager Nord"
    assert _text(liefer, "o:STREET") == "Am Hafen 3"
    assert _text(liefer, "o:ZIP") == "46244"
    assert _text(liefer, "o:CITY") == "Bottrop"
    assert liefer.find("o:COUNTRY", NS) is None

    paket.roh.lieferadresse = "Nur ein Name\nIrgendeine Straße 7"
    liefer = _info(_order(schreiber.erzeuge_xml(paket))).find("o:ORDER_PARTIES/o:BUYER_PARTY/o:PARTY/o:ADDRESS", NS)
    assert liefer is not None
    assert _text(liefer, "o:NAME") == "Nur ein Name"
    assert _text(liefer, "o:STREET") == "Irgendeine Straße 7"
    assert liefer.find("o:ZIP", NS) is None


def test_positionen_nur_aktive_mit_vk_preis(schreiber: OpenTransSchreiber, paket: Belegpaket) -> None:
    order = _order(schreiber.erzeuge_xml(paket))
    items = order.findall("o:ORDER_ITEM_LIST/o:ORDER_ITEM", NS)
    assert len(items) == 2  # "DAA Neptun" (übersprungen) ist nicht enthalten
    assert [_text(i, "o:LINE_ITEM_ID") for i in items] == ["1", "2"]
    assert [_text(i, "o:ARTICLE_ID/o:SUPPLIER_AID") for i in items] == ["1643", "957"]
    assert _text(items[0], "o:ARTICLE_ID/o:DESCRIPTION_SHORT") == "Villa Armellina Gloria Secco Frizzante 0,75 l"
    assert items[0].find("o:ARTICLE_ID/o:DESCRIPTION_LONG", NS) is None
    assert [_text(i, "o:QUANTITY") for i in items] == ["60", "24"]
    assert [_text(i, "o:ORDER_UNIT") for i in items] == ["Fl", "Fl"]
    for item in items:
        preis = item.find("o:ARTICLE_PRICE", NS)
        assert preis is not None and preis.get("type") == "net_list"
        assert _text(preis, "o:PRICE_AMOUNT") == "6.90"  # VK aus dem Stamm, nicht 6.50 aus dem PDF
        assert _text(preis, "o:TAX") == "0.19"
        assert item.find("o:REMARK", NS) is None  # keine GUID konfiguriert
    assert _text(items[0], "o:ARTICLE_PRICE/o:PRICE_LINE_AMOUNT") == "414.00"
    assert _text(items[1], "o:ARTICLE_PRICE/o:PRICE_LINE_AMOUNT") == "165.60"
    assert _text(order, "o:ORDER_SUMMARY/o:TOTAL_ITEM_NUM") == "2"
    assert _text(order, "o:ORDER_SUMMARY/o:TOTAL_AMOUNT") == "579.60"
    text = schreiber.erzeuge_xml(paket).decode("utf-8")
    assert "6.50" not in text
    assert "DAA Neptun" not in text.split("<ORDER_ITEM_LIST>")[1]


def test_mengen_und_betraege_format(schreiber: OpenTransSchreiber, paket: Belegpaket) -> None:
    paket.positionen[0].menge = Decimal("1.50")
    paket.positionen[0].vk_preis = Decimal("1234.5")
    paket.positionen[1].menge = Decimal("3.000")
    paket.positionen[1].vk_preis = Decimal("6.9")
    items = _order(schreiber.erzeuge_xml(paket)).findall("o:ORDER_ITEM_LIST/o:ORDER_ITEM", NS)
    assert _text(items[0], "o:QUANTITY") == "1.5"
    assert _text(items[0], "o:ARTICLE_PRICE/o:PRICE_AMOUNT") == "1234.50"  # kein Tausendertrennzeichen
    assert _text(items[0], "o:ARTICLE_PRICE/o:PRICE_LINE_AMOUNT") == "1851.75"
    assert _text(items[1], "o:QUANTITY") == "3"
    assert _text(items[1], "o:ARTICLE_PRICE/o:PRICE_LINE_AMOUNT") == "20.70"


def test_mwst_satz_aus_firmenkonfiguration(settings: Settings, paket: Belegpaket) -> None:
    firma = settings.firma("wik_leh")
    firma.opentrans.mwst_satz = Decimal("7")
    firma.opentrans.waehrung = "978"
    schreiber = OpenTransSchreiber(settings, firma, jetzt=lambda: FESTE_ZEIT)
    order = _order(schreiber.erzeuge_xml(paket))
    assert _text(order, "o:ORDER_ITEM_LIST/o:ORDER_ITEM/o:ARTICLE_PRICE/o:TAX") == "0.07"
    assert _text(_info(order), "o:PRICE_CURRENCY") == "978"
    ergebnis = schreiber.schreibe_verkauf_ab(paket, dry_run=True)
    assert ergebnis.brutto == Decimal("620.17")  # 579.60 * 1.07


def test_origin_company_id_und_preistyp(settings: Settings, paket: Belegpaket) -> None:
    schreiber = OpenTransSchreiber(
        settings,
        settings.firma("wik_leh"),
        preistyp="gros_list",
        steuergebiet="EU",
        firmen_guid="87AD28A4-08BA-409d-86B4-26DF93FB3D0C",
        jetzt=lambda: FESTE_ZEIT,
    )
    xml = schreiber.erzeuge_xml(paket)
    order = _order(xml)
    items = order.findall("o:ORDER_ITEM_LIST/o:ORDER_ITEM", NS)
    for item in items:
        guid = item.find("o:REMARK", NS)
        assert guid is not None and guid.get("type") == "origin_company_id"
        assert guid.text == "87AD28A4-08BA-409d-86B4-26DF93FB3D0C"
        assert item.find("o:ARTICLE_PRICE", NS).get("type") == "gros_list"
    bemerkungen = {r.get("type"): r.text for r in _info(order).findall("o:REMARK", NS)}
    assert bemerkungen["tax_area"] == "EU"
    assert schreiber.validiere_xml(xml) == []


def test_unbekannter_preistyp_oder_steuergebiet(settings: Settings) -> None:
    firma = settings.firma("wik_leh")
    with pytest.raises(ValueError, match="Preistyp"):
        OpenTransSchreiber(settings, firma, preistyp="brutto")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="Steuergebiet"):
        OpenTransSchreiber(settings, firma, steuergebiet="Inland")


def test_sonderzeichen_werden_bereinigt(schreiber: OpenTransSchreiber, paket: Belegpaket) -> None:
    kunde = paket.kunde.kunde
    assert kunde is not None
    kunde.name = "Weinmarkt Kirchhellen GmbH & Co. KG"
    kunde.name2 = "Preis in €\x07 'Aktion' <Test>"
    paket.roh.bestellnummer = 'B-4711 "Eilt"'
    xml = schreiber.erzeuge_xml(paket)
    text = xml.decode("utf-8")
    assert "&amp; Co. KG" in text
    assert "€" not in text
    assert "\x07" not in text
    assert "&#" not in text  # keine numerischen Entities
    assert "&lt;Test&gt;" in text
    adresse = _info(_order(xml)).find("o:ORDER_PARTIES/o:INVOICE_PARTY/o:PARTY/o:ADDRESS", NS)
    assert adresse is not None
    assert _text(adresse, "o:NAME") == "Weinmarkt Kirchhellen GmbH & Co. KG"
    assert _text(adresse, "o:NAME2") == "Preis in EUR 'Aktion' <Test>"
    assert schreiber.validiere_xml(xml) == []


def test_fallbacks_fuer_bestellnummer_und_datum(schreiber: OpenTransSchreiber, paket: Belegpaket) -> None:
    paket.roh.bestellnummer = None
    paket.roh.datum = None
    paket.roh.liefertag = None
    info = _info(_order(schreiber.erzeuge_xml(paket)))
    assert _text(info, "o:ORDER_ID") == "2026-0917"  # Belegnummer als Fallback
    assert _text(info, "o:ORDER_DATE").startswith("2026-10-18T00:00:00")  # "heute" = feste Zeit
    assert info.find("o:DELIVERY_DATE", NS) is None
    ergebnis = schreiber.schreibe_verkauf_ab(paket, dry_run=True)
    assert any("Belegnummer '2026-0917' als ORDER_ID" in w for w in ergebnis.warnungen)
    assert any("Tagesdatum als ORDER_DATE" in w for w in ergebnis.warnungen)

    paket.roh.belegnummer = None
    info = _info(_order(schreiber.erzeuge_xml(paket)))
    assert _text(info, "o:ORDER_ID") == "kundenbestellung_kirchhellen"


# ---------------------------------------------------------------------------
# Pflichtprüfung
# ---------------------------------------------------------------------------


def test_pflichtpruefung_ohne_kunde(schreiber: OpenTransSchreiber, paket: Belegpaket) -> None:
    paket.kunde = KundeZuordnung()
    with pytest.raises(OpenTransFehler) as exc:
        schreiber.erzeuge_xml(paket)
    assert exc.value.probleme == ["Kein Kunde zugeordnet (Lexware würde beim Übernehmen einen neuen Kunden anlegen)"]


def test_pflichtpruefung_ohne_vk_preis(schreiber: OpenTransSchreiber, paket: Belegpaket) -> None:
    paket.positionen[1].vk_preis = None
    with pytest.raises(OpenTransFehler) as exc:
        schreiber.erzeuge_xml(paket)
    assert len(exc.value.probleme) == 1
    assert "Position 2" in exc.value.probleme[0]
    assert "kein VK-Preis" in exc.value.probleme[0]


def test_pflichtpruefung_menge_null(schreiber: OpenTransSchreiber, paket: Belegpaket) -> None:
    paket.positionen[0].menge = Decimal("0")
    with pytest.raises(OpenTransFehler) as exc:
        schreiber.erzeuge_xml(paket)
    assert len(exc.value.probleme) == 1
    assert "Position 1" in exc.value.probleme[0]
    assert "Menge 0" in exc.value.probleme[0]


def test_pflichtpruefung_sammelt_alle_probleme(schreiber: OpenTransSchreiber, paket: Belegpaket) -> None:
    paket.kunde = KundeZuordnung()
    paket.positionen[0].artikel = None
    paket.positionen[0].vk_preis = None
    paket.positionen[1].menge = Decimal("-1")
    paket.positionen[1].vk_preis = Decimal("0")
    probleme = schreiber.pruefe_paket(paket)
    assert len(probleme) == 5
    assert any("Kein Kunde" in p for p in probleme)
    assert any("Position 1" in p and "kein Artikel" in p for p in probleme)
    assert any("Position 1" in p and "kein VK-Preis" in p for p in probleme)
    assert any("Position 2" in p and "Menge -1" in p for p in probleme)
    assert any("Position 2" in p and "VK-Preis 0" in p for p in probleme)
    with pytest.raises(OpenTransFehler) as exc:
        schreiber.erzeuge_xml(paket)
    assert exc.value.probleme == probleme
    assert "Kein Kunde" in str(exc.value)


def test_pflichtpruefung_keine_aktive_position(schreiber: OpenTransSchreiber, paket: Belegpaket) -> None:
    for p in paket.positionen:
        p.status = MatchStatus.UEBERSPRUNGEN
    assert schreiber.pruefe_paket(paket) == ["Keine aktive Position (alle übersprungen oder keine vorhanden)"]


def test_pflichtpruefung_mandant(settings: Settings, paket: Belegpaket) -> None:
    schreiber = OpenTransSchreiber(settings, settings.firma("woe_leh"), jetzt=lambda: FESTE_ZEIT)
    probleme = schreiber.pruefe_paket(paket)
    assert any("Paket gehört zu Firma 'wik_leh'" in p for p in probleme)
    assert any("Kunde 3441 gehört zu Firma 'wik_leh'" in p for p in probleme)
    assert sum("Artikel" in p and "gehört zu Firma 'wik_leh'" in p for p in probleme) == 2


# ---------------------------------------------------------------------------
# validiere_xml
# ---------------------------------------------------------------------------


def test_validiere_xml_leer_fuer_erzeugte_xml(schreiber: OpenTransSchreiber, paket: Belegpaket) -> None:
    assert schreiber.validiere_xml(schreiber.erzeuge_xml(paket)) == []


def test_validiere_xml_nicht_wohlgeformt(schreiber: OpenTransSchreiber) -> None:
    probleme = schreiber.validiere_xml(b"<ORDER_LIST><ORDER></ORDER_LIST>")
    assert len(probleme) == 1
    assert probleme[0].startswith("XML nicht wohlgeformt")


def test_validiere_xml_erkennt_fehler(schreiber: OpenTransSchreiber, paket: Belegpaket) -> None:
    text = schreiber.erzeuge_xml(paket).decode("utf-8")
    kaputt = (
        text.replace("<PRICE_LINE_AMOUNT>414.00</PRICE_LINE_AMOUNT>", "<PRICE_LINE_AMOUNT>400.00</PRICE_LINE_AMOUNT>")
        .replace("<SUPPLIER_AID>957</SUPPLIER_AID>", "<SUPPLIER_AID> </SUPPLIER_AID>")
        .replace('<REMARK type="tax_area">Merchant</REMARK>', "")
        .replace("<TOTAL_ITEM_NUM>2</TOTAL_ITEM_NUM>", "<TOTAL_ITEM_NUM>3</TOTAL_ITEM_NUM>")
        .replace("<PRICE_CURRENCY>EUR</PRICE_CURRENCY>", "")
        .replace("<TAX>0.19</TAX>", "<TAX>19</TAX>", 1)
    )
    probleme = schreiber.validiere_xml(kaputt.encode("utf-8"))
    gesamt = "\n".join(probleme)
    assert "Position 1: PRICE_LINE_AMOUNT 400.00 != QUANTITY x PRICE_AMOUNT = 414.00" in gesamt
    assert "Position 2: ARTICLE_ID/SUPPLIER_AID fehlt oder ist leer" in gesamt
    assert 'REMARK type="tax_area" fehlt' in gesamt
    assert "TOTAL_ITEM_NUM '3' passt nicht zu 2 Positionen" in gesamt
    assert "TOTAL_AMOUNT 579.60 != Summe der Positionen 565.60" in gesamt
    assert "PRICE_CURRENCY fehlt" in gesamt
    assert "TAX '19' hat nicht das Format 0.19" in gesamt


def test_validiere_xml_reihenfolge_namespace_und_euro(schreiber: OpenTransSchreiber, paket: Belegpaket) -> None:
    text = schreiber.erzeuge_xml(paket).decode("utf-8")
    # ORDER_DATE vor ORDER_ID, Euro-Zeichen im Text, falsche Version, DOCTYPE
    zeilen = text.splitlines()
    i_id = next(i for i, z in enumerate(zeilen) if "<ORDER_ID>" in z)
    zeilen[i_id], zeilen[i_id + 1] = zeilen[i_id + 1], zeilen[i_id]
    kaputt = "\n".join(zeilen).replace("Kunde 3441 Hegmann", "Kunde 3441 Hegmann 5 €").replace('version="1.0"', 'version="2.1"')
    kaputt = kaputt.replace("<ORDER_LIST>", "<!DOCTYPE ORDER_LIST>\n<ORDER_LIST>", 1)
    probleme = schreiber.validiere_xml(kaputt.encode("utf-8"))
    gesamt = "\n".join(probleme)
    assert "Reihenfolge in ORDER_INFO falsch" in gesamt
    assert "Euro-Zeichen in <REMARK>" in gesamt
    assert "ORDER version muss '1.0' sein" in gesamt
    assert "DOCTYPE" in gesamt

    ohne_ns = text.replace(' xmlns="http://www.opentrans.org/XMLSchema/1.0"', "")
    assert any("Namespace" in p for p in schreiber.validiere_xml(ohne_ns.encode("utf-8")))
    assert any("ORDER_LIST" in p for p in schreiber.validiere_xml(b'<?xml version="1.0"?><ORDER/>'))


# ---------------------------------------------------------------------------
# dateiname
# ---------------------------------------------------------------------------


def test_dateiname_stabil_und_eindeutig(schreiber: OpenTransSchreiber, paket: Belegpaket) -> None:
    name = schreiber.dateiname(paket)
    assert re.fullmatch(r"20260910_3441_B-4711_[0-9a-f]{8}\.xml", name)
    assert schreiber.dateiname(_paket()) == name  # reproduzierbar, unabhängig vom Erzeugungszeitpunkt

    anders = _paket()
    anders.positionen[0].menge = Decimal("61")
    assert schreiber.dateiname(anders) != name
    assert schreiber.dateiname(anders)[:20] == name[:20]

    andere_firma = OpenTransSchreiber(schreiber.settings, schreiber.settings.firma("woe_leh"), jetzt=lambda: FESTE_ZEIT)
    assert andere_firma.dateiname(_paket()) != name  # Hash enthält die Firma


def test_dateiname_bereinigt_bestellnummer(schreiber: OpenTransSchreiber, paket: Belegpaket) -> None:
    paket.roh.bestellnummer = " Best./Nr: 4711 Ä \"eilt\" "
    name = schreiber.dateiname(paket)
    assert re.fullmatch(r"20260910_3441_Best.-Nr-4711-Ae-eilt_[0-9a-f]{8}\.xml", name)
    paket.roh.bestellnummer = None
    paket.roh.belegnummer = None
    assert schreiber.dateiname(paket).startswith("20260910_3441_kundenbestellung_kirchhellen_")
    paket.kunde = KundeZuordnung()
    assert schreiber.dateiname(paket).startswith("20260910_ohne-kunde_")


# ---------------------------------------------------------------------------
# schreibe_verkauf_ab
# ---------------------------------------------------------------------------


def test_dry_run_schreibt_in_xml_ausgabe_ordner(settings: Settings, schreiber: OpenTransSchreiber, paket: Belegpaket) -> None:
    ergebnis = schreiber.schreibe_verkauf_ab(paket)  # Standard: Dry Run
    assert ergebnis.erfolgreich is True
    assert ergebnis.dry_run is True
    assert ergebnis.methode == "opentrans"
    assert ergebnis.belegart == Belegart.AUFTRAGSBESTAETIGUNG
    assert ergebnis.belegnummer is None
    assert ergebnis.netto == Decimal("579.60")
    assert ergebnis.brutto == Decimal("689.72")
    assert ergebnis.fehler is None
    assert ergebnis.artefakt is not None
    assert ergebnis.artefakt.parent == settings.xml_ausgabe_ordner
    assert ergebnis.artefakt.name == schreiber.dateiname(paket)
    assert ergebnis.artefakt.read_bytes() == schreiber.erzeuge_xml(paket)
    assert ergebnis.warnungen[0] == "Belegnummer wird erst beim Import in Lexware vergeben"
    assert any("Dry Run" in w for w in ergebnis.warnungen)
    import_ordner = settings.firma("wik_leh").opentrans.import_ordner
    assert import_ordner is not None and not import_ordner.exists()


def test_dry_run_legt_ausgabeordner_an(settings: Settings, paket: Belegpaket, tmp_path: Path) -> None:
    settings.xml_ausgabe_ordner = tmp_path / "neu" / "opentrans"
    schreiber = OpenTransSchreiber(settings, settings.firma("wik_leh"), jetzt=lambda: FESTE_ZEIT)
    ergebnis = schreiber.schreibe_verkauf_ab(paket, dry_run=True)
    assert ergebnis.erfolgreich
    assert ergebnis.artefakt is not None and ergebnis.artefakt.parent == settings.xml_ausgabe_ordner


def test_dry_run_mit_pflichtverstoss_liefert_fehler(schreiber: OpenTransSchreiber, paket: Belegpaket) -> None:
    paket.kunde = KundeZuordnung()
    ergebnis = schreiber.schreibe_verkauf_ab(paket, dry_run=True)
    assert ergebnis.erfolgreich is False
    assert ergebnis.methode == "opentrans"
    assert ergebnis.fehler is not None and "Kein Kunde zugeordnet" in ergebnis.fehler
    assert ergebnis.artefakt is None
    assert list(schreiber.settings.xml_ausgabe_ordner.iterdir()) == []


def _scharf_vorbereiten(settings: Settings, paket: Belegpaket, firma_id: str = "wik_leh") -> OpenTransSchreiber:
    firma = settings.firma(firma_id)
    assert firma.opentrans.import_ordner is not None
    firma.opentrans.import_ordner.mkdir(parents=True, exist_ok=True)
    paket.freigegeben = True
    return OpenTransSchreiber(settings, firma, jetzt=lambda: FESTE_ZEIT)


def test_scharf_ohne_lizenz_scheitert(settings: Settings, paket: Belegpaket) -> None:
    schreiber = _scharf_vorbereiten(settings, paket)
    schreiber.firma.opentrans.lizenziert = False
    ergebnis = schreiber.schreibe_verkauf_ab(paket, dry_run=False)
    assert ergebnis.erfolgreich is False
    assert ergebnis.dry_run is False
    assert ergebnis.fehler is not None and "nicht lizenziert" in ergebnis.fehler
    assert "UI-Automation" in ergebnis.fehler
    assert ergebnis.artefakt is None
    assert list(schreiber.firma.opentrans.import_ordner.iterdir()) == []
    assert list(settings.xml_ausgabe_ordner.iterdir()) == []

    schreiber.firma.opentrans.lizenziert = None
    ergebnis = schreiber.schreibe_verkauf_ab(paket, dry_run=False)
    assert ergebnis.erfolgreich is False
    assert ergebnis.fehler is not None and "noch nicht geprüft" in ergebnis.fehler


def test_scharf_ohne_import_ordner_scheitert(settings: Settings, paket: Belegpaket) -> None:
    schreiber = _scharf_vorbereiten(settings, paket)
    schreiber.firma.opentrans.import_ordner = None
    ergebnis = schreiber.schreibe_verkauf_ab(paket, dry_run=False)
    assert ergebnis.erfolgreich is False
    assert ergebnis.fehler is not None and "import_ordner ist nicht konfiguriert" in ergebnis.fehler

    schreiber.firma.opentrans.import_ordner = settings.xml_ausgabe_ordner / "gibt-es-nicht"
    ergebnis = schreiber.schreibe_verkauf_ab(paket, dry_run=False)
    assert ergebnis.erfolgreich is False
    assert ergebnis.fehler is not None and "existiert nicht" in ergebnis.fehler
    assert not schreiber.firma.opentrans.import_ordner.exists()  # wird scharf nicht angelegt


def test_scharf_nur_freigegeben_und_nicht_blockiert(settings: Settings, paket: Belegpaket) -> None:
    schreiber = _scharf_vorbereiten(settings, paket)
    paket.freigegeben = False
    ergebnis = schreiber.schreibe_verkauf_ab(paket, dry_run=False)
    assert ergebnis.erfolgreich is False
    assert ergebnis.fehler is not None and "nicht freigegeben" in ergebnis.fehler

    paket.freigegeben = True
    paket.hinweise.append(Pruefhinweis(code=Pruefcode.DUBLETTE, schweregrad=Schweregrad.FEHLER, text="B-4711 existiert"))
    ergebnis = schreiber.schreibe_verkauf_ab(paket, dry_run=False)
    assert ergebnis.erfolgreich is False
    assert ergebnis.fehler is not None and "blockiert" in ergebnis.fehler
    assert list(schreiber.firma.opentrans.import_ordner.iterdir()) == []


def test_scharf_schreibt_in_import_ordner_und_ueberschreibt_nie(settings: Settings, paket: Belegpaket) -> None:
    schreiber = _scharf_vorbereiten(settings, paket)
    ergebnis = schreiber.schreibe_verkauf_ab(paket, dry_run=False)
    assert ergebnis.erfolgreich is True
    assert ergebnis.dry_run is False
    assert ergebnis.belegnummer is None
    assert ergebnis.artefakt is not None
    assert ergebnis.artefakt.parent == schreiber.firma.opentrans.import_ordner
    assert ergebnis.artefakt.exists()
    assert schreiber.validiere_xml(ergebnis.artefakt.read_bytes()) == []
    assert ergebnis.warnungen[0] == "Belegnummer wird erst beim Import in Lexware vergeben"
    assert any("Daten austauschen" in w and "Kunde zuordnen" in w for w in ergebnis.warnungen)
    assert list(settings.xml_ausgabe_ordner.iterdir()) == []

    inhalt = ergebnis.artefakt.read_bytes()
    zweites = schreiber.schreibe_verkauf_ab(paket, dry_run=False)
    assert zweites.erfolgreich is False
    assert zweites.fehler is not None and "existiert bereits" in zweites.fehler
    assert ergebnis.artefakt.read_bytes() == inhalt
    assert len(list(schreiber.firma.opentrans.import_ordner.iterdir())) == 1


def test_warnungen_bei_positionslieferadresse(schreiber: OpenTransSchreiber, paket: Belegpaket) -> None:
    paket.positionen[1].roh.lieferadresse = "Filiale Süd, Südring 1, 46240 Bottrop"
    ergebnis = schreiber.schreibe_verkauf_ab(paket, dry_run=True)
    assert ergebnis.erfolgreich
    assert any("Position 2: positionsbezogene Lieferadresse" in w for w in ergebnis.warnungen)
    assert any("Abweichende Lieferadresse als BUYER_PARTY" in w for w in ergebnis.warnungen)
    assert ergebnis.artefakt is not None
    assert "Pos. 2 (957) Lieferadresse: Filiale Süd, Südring 1, 46240 Bottrop" in ergebnis.artefakt.read_text(encoding="utf-8")


def test_einkauf_und_lieferschein_nicht_per_opentrans(schreiber: OpenTransSchreiber, paket: Belegpaket) -> None:
    einkauf = schreiber.schreibe_einkauf_bestellung(paket, dry_run=True)
    assert einkauf.erfolgreich is False
    assert einkauf.belegart == Belegart.BESTELLUNG
    assert einkauf.methode == "opentrans"
    assert einkauf.fehler is not None and "nicht möglich" in einkauf.fehler
    assert einkauf.netto == paket.summe_netto_ek == Decimal("352.80")
    lieferschein = schreiber.auftrag_weiterfuehren_lieferschein(paket, "AB-2026-0199", dry_run=False)
    assert lieferschein.erfolgreich is False
    assert lieferschein.belegart == Belegart.LIEFERSCHEIN
    assert lieferschein.fehler is not None and "AB-2026-0199" in lieferschein.fehler


def test_import_anleitung(settings: Settings) -> None:
    firma = settings.firma("wik_leh")
    text = import_anleitung(firma, Path("C:/ablage/datei.xml"))
    assert "Wein-Import-Kreis LEH" in text
    assert "Daten austauschen" in text
    assert "datei.xml" in text
    assert "Auftragsbestätigung" in text
