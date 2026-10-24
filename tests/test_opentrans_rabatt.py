"""Tests für lexware.opentrans: Positionsrabatt (PRICE_AMOUNT = rabattierter VK, REMARK) und die
Protokoll-Erweiterungen (Angebot, Angebot -> AB, PDF) der Shopschnittstelle."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from auftragsautomat.config import Settings
from auftragsautomat.lexware.opentrans import OPENTRANS_NS, OpenTransSchreiber
from auftragsautomat.models import (
    Artikel,
    Belegart,
    Belegergebnis,
    Belegfelder,
    Belegpaket,
    Eingangstyp,
    Extraktionsmethode,
    Kunde,
    KundeZuordnung,
    MatchStatus,
    PositionsZuordnung,
    RohBestellung,
    RohPosition,
)

NS = {"o": OPENTRANS_NS}
FESTE_ZEIT = datetime(2026, 10, 24, 9, 0)


def _paket(rabatt: str | None = "10") -> Belegpaket:
    roh = RohBestellung(
        eingangstyp=Eingangstyp.KUNDENBESTELLUNG_PDF, quelldatei=Path("eingang/wik_leh/bestellung_beispiel.pdf"),
        extraktionsmethode=Extraktionsmethode.PDFPLUMBER, kunde_text="Weinhandel Beispiel", bestellnummer="B-0001",
        datum=date(2026, 10, 1), liefertag=date(2026, 11, 5),
        positionen=[
            RohPosition(laufnr=1, bezeichnung="Muster Rotwein", menge=Decimal("60"), preis=Decimal("6.90")),
            RohPosition(laufnr=2, bezeichnung="Muster Weißwein", menge=Decimal("24"), preis=Decimal("6.90")),
        ],
    )
    rot = Artikel(firma_id="wik_leh", artikelnummer="4001", bezeichnung="Muster Rotwein 0,75 l", vk_preis_1=Decimal("6.90"), einheit="Fl")
    weiss = Artikel(firma_id="wik_leh", artikelnummer="4002", bezeichnung="Muster Weißwein 0,75 l", vk_preis_1=Decimal("6.90"), einheit="Fl")
    return Belegpaket(
        roh=roh, firma_id="wik_leh",
        kunde=KundeZuordnung(kunde=Kunde(firma_id="wik_leh", kundennummer="90001", name="Weinhandel Beispiel GmbH"), status=MatchStatus.SICHER),
        positionen=[
            PositionsZuordnung(roh=roh.positionen[0], artikel=rot, status=MatchStatus.SICHER, menge=Decimal("60"), vk_preis=Decimal("6.90"),
                               preis_dokument=Decimal("6.90"), rabatt_prozent=Decimal(rabatt) if rabatt else None,
                               rabatt_quelle="aktion:herbst" if rabatt else None),
            PositionsZuordnung(roh=roh.positionen[1], artikel=weiss, status=MatchStatus.SICHER, menge=Decimal("24"), vk_preis=Decimal("6.90"),
                               preis_dokument=Decimal("6.90")),
        ],
        belegfelder=Belegfelder(spedition="Spedition Beispiel", weingut="Weingut Muster", shipment="05.11.2026", kunde="Weinhandel Beispiel"),
    )


@pytest.fixture()
def schreiber(settings: Settings) -> OpenTransSchreiber:
    return OpenTransSchreiber(settings, settings.firma("wik_leh"), jetzt=lambda: FESTE_ZEIT)


def _items(xml: bytes) -> list[ET.Element]:
    root = ET.fromstring(xml)
    return root.findall("o:ORDER/o:ORDER_ITEM_LIST/o:ORDER_ITEM", NS)


def _text(elem: ET.Element, pfad: str) -> str:
    kind = elem.find(pfad, NS)
    assert kind is not None, pfad
    return (kind.text or "").strip()


def test_rabatt_als_rabattierter_preis_und_remark(schreiber: OpenTransSchreiber) -> None:
    paket = _paket("10")
    xml = schreiber.erzeuge_xml(paket)
    items = _items(xml)
    assert _text(items[0], "o:ARTICLE_PRICE/o:PRICE_AMOUNT") == "6.21"  # 6,90 - 10 %
    assert _text(items[0], "o:ARTICLE_PRICE/o:PRICE_LINE_AMOUNT") == "372.60"
    remark = items[0].find("o:REMARK", NS)
    assert remark is not None and remark.get("type") == "rabatt"
    assert remark.text == "Rabatt 10 % (aktion:herbst)"
    assert _text(items[1], "o:ARTICLE_PRICE/o:PRICE_AMOUNT") == "6.90"  # ohne Rabatt unverändert
    assert items[1].find("o:REMARK", NS) is None
    root = ET.fromstring(xml)
    assert _text(root, "o:ORDER/o:ORDER_SUMMARY/o:TOTAL_AMOUNT") == "538.20"
    assert schreiber.validiere_xml(xml) == []
    bemerkung = [r for r in root.findall("o:ORDER/o:ORDER_HEADER/o:ORDER_INFO/o:REMARK", NS) if r.get("type") == "order"][0]
    assert "Pos. 1 (4001): Rabatt 10 % (aktion:herbst) - PRICE_AMOUNT 6.21 statt VK 6.90" in (bemerkung.text or "")
    assert "6.50" not in xml.decode("utf-8")


def test_rabatt_mit_rundung_bleibt_valide(schreiber: OpenTransSchreiber) -> None:
    paket = _paket("7.5")  # 6,90 - 7,5 % = 6,3825 -> 6.38
    xml = schreiber.erzeuge_xml(paket)
    items = _items(xml)
    assert _text(items[0], "o:ARTICLE_PRICE/o:PRICE_AMOUNT") == "6.38"
    assert _text(items[0], "o:ARTICLE_PRICE/o:PRICE_LINE_AMOUNT") == "382.80"
    assert items[0].find("o:REMARK", NS).text == "Rabatt 7,5 % (aktion:herbst)"
    assert schreiber.validiere_xml(xml) == []
    assert _text(ET.fromstring(xml), "o:ORDER/o:ORDER_SUMMARY/o:TOTAL_AMOUNT") == "548.40"


def test_dateiname_haengt_vom_rabatt_ab(schreiber: OpenTransSchreiber) -> None:
    assert schreiber.dateiname(_paket("10")) != schreiber.dateiname(_paket(None))
    assert schreiber.dateiname(_paket("10")) == schreiber.dateiname(_paket("10"))


def test_schreibe_verkauf_ab_warnt_bei_rabatt(settings: Settings, schreiber: OpenTransSchreiber) -> None:
    ergebnis = schreiber.schreibe_verkauf_ab(_paket("10"), dry_run=True)
    assert ergebnis.erfolgreich
    assert ergebnis.netto == Decimal("538.20")
    assert any("Positionsrabatte (Pos. 1: 10 %)" in w for w in ergebnis.warnungen)
    ohne = schreiber.schreibe_verkauf_ab(_paket(None), dry_run=True)
    assert not any("Positionsrabatte" in w for w in ohne.warnungen)


def test_angebot_nicht_moeglich_und_ab_direkt(schreiber: OpenTransSchreiber) -> None:
    paket = _paket("10")
    angebot = schreiber.schreibe_angebot(paket, dry_run=True)
    assert angebot.erfolgreich is True and angebot.methode == "keine"
    assert angebot.belegart is Belegart.ANGEBOT and angebot.belegnummer is None
    assert angebot.warnungen == ["Angebot per openTRANS nicht möglich; AB direkt"]
    assert angebot.netto == Decimal("538.20")

    ab = schreiber.angebot_zu_ab(angebot, paket, weg="angebot_weiterfuehren", dry_run=True)
    assert ab.erfolgreich and ab.methode == "opentrans" and ab.artefakt is not None
    assert ab.warnungen[0].startswith("Verkaufsweg 'Angebot -> Weiterführen -> Auftragsbestätigung' ist per openTRANS nicht abbildbar")
    assert "Lieferdatum und AB-Text beim Übernehmen laut Firma leeren" in ab.warnungen[0]


def test_pdf_ausgeben_nicht_moeglich(settings: Settings, schreiber: OpenTransSchreiber) -> None:
    beleg = Belegergebnis(belegart=Belegart.AUFTRAGSBESTAETIGUNG, erfolgreich=True, dry_run=False, methode="opentrans")
    assert schreiber.pdf_ausgeben(beleg, formular="WIK AB_LS", dateiname="x – Kopie.pdf", dry_run=False) is None
    assert beleg.pdf is None
    assert any("kann per openTRANS nicht erzeugt werden" in w and "WIK AB_LS" in w for w in beleg.warnungen)
