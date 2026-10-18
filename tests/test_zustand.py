"""Tests für freigabe.zustand: Pakete speichern, laden, auflisten, löschen (JSON-Round-Trip)."""

from __future__ import annotations

import json
import os
import time
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from auftragsautomat.config import Settings
from auftragsautomat.freigabe import zustand
from auftragsautomat.models import (
    Artikel,
    Belegfelder,
    Belegpaket,
    Eingangstyp,
    Extraktionsmethode,
    Kunde,
    KundeZuordnung,
    Lieferant,
    LieferantZuordnung,
    MatchStatus,
    PositionsZuordnung,
    Pruefcode,
    Pruefhinweis,
    RohBestellung,
    RohPosition,
    Schweregrad,
    Summenvermerk,
)

FIRMA = "wik_leh"


def _paket(quelldatei: Path, *, freigegeben: bool = False) -> Belegpaket:
    roh = RohBestellung(
        eingangstyp=Eingangstyp.KUNDENBESTELLUNG_PDF,
        quelldatei=quelldatei,
        extraktionsmethode=Extraktionsmethode.PDFPLUMBER,
        kunde_text="Weinmarkt Kirchhellen GmbH & Co. KG",
        bestellnummer="B-4712",
        datum=date(2026, 9, 10),
        liefertag=date(2026, 9, 15),
        positionen=[
            RohPosition(laufnr=1, bezeichnung="Villa Armellina Gloria Secco Frizzante", menge=Decimal("60"), preis=Decimal("6.90"), hinweis="11+1"),
            RohPosition(laufnr=2, bezeichnung="DAA Neptun", menge=Decimal("1"), preis=Decimal("0.00"), einheit="Stk"),
        ],
        summenvermerke=[Summenvermerk(wert=Decimal("61"), einheit="Fl", rohtext="Gesamt: 61 Fl.", konfidenz=0.95)],
        hinweise=[Pruefhinweis(code=Pruefcode.SUMMENVERMERK, schweregrad=Schweregrad.INFO, text="Summenvermerk erkannt")],
    )
    gloria = Artikel(
        firma_id=FIRMA, artikelnummer="1643", bezeichnung="Villa Armellina Gloria Secco Frizzante 0,75 l",
        warengruppe="Cide", lieferantennummer="72", volumen_cl=Decimal("75"), ek_preis=Decimal("4.20"), vk_preis_1=Decimal("6.90"),
    )
    return Belegpaket(
        roh=roh,
        firma_id=FIRMA,
        firma_quelle="alias_kunde",
        kunde=KundeZuordnung(
            kunde=Kunde(firma_id=FIRMA, kundennummer="3441", name="Hegmann", ort="Bottrop"), status=MatchStatus.SICHER, quelle="alias"
        ),
        lieferant=LieferantZuordnung(
            lieferant=Lieferant(firma_id=FIRMA, lieferantennummer="72", name="Cide srl"), status=MatchStatus.MANUELL, quelle="manuell"
        ),
        positionen=[
            PositionsZuordnung(
                roh=roh.positionen[0], artikel=gloria, status=MatchStatus.SICHER, quelle="alias", menge=Decimal("60"),
                ek_preis=Decimal("4.20"), vk_preis=Decimal("6.90"), preis_dokument=Decimal("6.90"),
            ),
            PositionsZuordnung(
                roh=roh.positionen[1], status=MatchStatus.UEBERSPRUNGEN, menge=Decimal("0"), preis_dokument=Decimal("0.00"),
                hinweise=[Pruefhinweis(code=Pruefcode.NULLPREIS_OHNE_ARTIKEL, schweregrad=Schweregrad.WARNUNG, text="übersprungen", position_laufnr=2)],
            ),
        ],
        belegfelder=Belegfelder(spedition="Spedition Weinlogistik", weingut="Cide srl", shipment="15.09.2026", kunde="Hegmann"),
        hinweise=[Pruefhinweis(code=Pruefcode.PREIS_ABWEICHUNG, schweregrad=Schweregrad.WARNUNG, text="Abweichung", details={"abweichung_eur": "0,40"})],
        einkauf_erforderlich=True,
        lieferschein_erstellen=True,
        freigegeben=freigegeben,
        freigegeben_am=datetime(2026, 10, 18, 9, 15, 30) if freigegeben else None,
        freigegeben_von="jd" if freigegeben else None,
    )


def test_round_trip_erhaelt_decimal_datum_enum(tmp_path: Path):
    paket = _paket(tmp_path / "eingang" / "kundenbestellung_kirchhellen.pdf", freigegeben=True)
    pfad = zustand.speichere_paket(paket, tmp_path / "pakete")

    assert pfad == tmp_path / "pakete" / "kundenbestellung_kirchhellen.paket.json"
    geladen = zustand.lade_paket(pfad)

    assert geladen == paket
    assert geladen.model_dump() == paket.model_dump()
    assert isinstance(geladen.positionen[0].menge, Decimal) and geladen.positionen[0].menge == Decimal("60")
    assert geladen.positionen[0].ek_preis == Decimal("4.20") and isinstance(geladen.positionen[0].ek_preis, Decimal)
    assert geladen.roh.datum == date(2026, 9, 10) and geladen.roh.liefertag == date(2026, 9, 15)
    assert geladen.freigegeben_am == datetime(2026, 10, 18, 9, 15, 30)
    assert geladen.roh.eingangstyp is Eingangstyp.KUNDENBESTELLUNG_PDF
    assert geladen.lieferant.status is MatchStatus.MANUELL
    assert geladen.hinweise[0].code is Pruefcode.PREIS_ABWEICHUNG
    assert geladen.roh.summenvermerke[0].wert == Decimal("61")
    assert geladen.blockiert is False and geladen.gesamtmenge == Decimal("60")
    assert geladen.summe_netto_vk == Decimal("414.00")
    assert Path(geladen.roh.quelldatei) == paket.roh.quelldatei


def test_json_enthaelt_berechnete_felder_und_wird_beim_laden_ignoriert(tmp_path: Path):
    paket = _paket(tmp_path / "x.pdf")
    pfad = zustand.speichere_paket(paket, tmp_path)
    daten = json.loads(pfad.read_text(encoding="utf-8"))
    assert daten["blockiert"] is False
    assert daten["gesamtmenge"] == "60"
    assert daten["positionen"][0]["netto_ek"] == "252.00"

    # Berechnete Felder im JSON manipulieren -> beim Laden neu berechnet, nicht übernommen
    daten["blockiert"] = True
    daten["gesamtmenge"] = "999"
    daten["summe_netto_vk"] = "1"
    pfad.write_text(json.dumps(daten), encoding="utf-8")
    geladen = zustand.lade_paket(pfad)
    assert geladen.blockiert is False
    assert geladen.gesamtmenge == Decimal("60")
    assert geladen.summe_netto_vk == Decimal("414.00")


def test_speichern_ersetzt_vorhandenes_paket_und_laesst_keine_tmp_dateien(tmp_path: Path):
    paket = _paket(tmp_path / "x.pdf")
    zustand.speichere_paket(paket, tmp_path / "p")
    paket.freigegeben = True
    pfad = zustand.speichere_paket(paket, tmp_path / "p")
    assert zustand.lade_paket(pfad).freigegeben is True
    assert sorted(p.name for p in (tmp_path / "p").iterdir()) == ["x.paket.json"]


def test_speichern_mit_dateiname(tmp_path: Path):
    paket = _paket(tmp_path / "x.pdf")
    pfad = zustand.speichere_paket(paket, tmp_path, dateiname="umbenannt.paket.json")
    assert pfad.name == "umbenannt.paket.json"
    assert zustand.lade_paket(pfad).roh.quelldatei == paket.roh.quelldatei


def test_dateiname_bereinigt_unsichere_zeichen(tmp_path: Path):
    paket = _paket(tmp_path / "Bestellung Müller & Co (Sept) .pdf")
    name = zustand.paket_dateiname(paket)
    assert name.endswith(zustand.PAKET_ENDUNG)
    assert " " not in name and "&" not in name and "(" not in name
    assert name.startswith("Bestellung_M")


def test_lade_fehler(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        zustand.lade_paket(tmp_path / "fehlt.paket.json")
    kaputt = tmp_path / "kaputt.paket.json"
    kaputt.write_text("{ kein json", encoding="utf-8")
    with pytest.raises(zustand.PaketFehler, match="kein gültiges JSON"):
        zustand.lade_paket(kaputt)
    ungueltig = tmp_path / "ungueltig.paket.json"
    ungueltig.write_text(json.dumps({"roh": {"quelldatei": "x.pdf"}}), encoding="utf-8")
    with pytest.raises(zustand.PaketFehler, match="ungültig"):
        zustand.lade_paket(ungueltig)
    assert issubclass(zustand.PaketFehler, ValueError)


def test_liste_pakete_neueste_zuerst_und_loeschen(tmp_path: Path):
    ordner = tmp_path / "pakete"
    assert zustand.liste_pakete(ordner) == []  # fehlender Ordner
    alt = zustand.speichere_paket(_paket(tmp_path / "alt.pdf"), ordner)
    neu = zustand.speichere_paket(_paket(tmp_path / "neu.pdf"), ordner)
    jetzt = time.time()
    os.utime(alt, (jetzt - 100, jetzt - 100))
    os.utime(neu, (jetzt, jetzt))
    (ordner / "fremd.json").write_text("{}", encoding="utf-8")  # keine Paketdatei
    (ordner / "unterordner.paket.json").mkdir()  # Ordner mit Paketendung wird ignoriert

    assert zustand.liste_pakete(ordner) == [neu, alt]
    assert zustand.loesche_paket(alt) is True
    assert zustand.loesche_paket(alt) is False
    assert zustand.liste_pakete(ordner) == [neu]


def test_beschreibe_paket(tmp_path: Path):
    paket = _paket(tmp_path / "kundenbestellung_kirchhellen.pdf", freigegeben=True)
    pfad = zustand.speichere_paket(paket, tmp_path)
    info = zustand.beschreibe_paket(pfad)
    assert info.quelldatei == "kundenbestellung_kirchhellen.pdf"
    assert info.firma_id == FIRMA and info.kunde == "Hegmann"
    assert info.blockiert is False and info.freigegeben is True
    assert "freigegeben" in info.anzeige and "wik_leh" in info.anzeige

    kaputt = tmp_path / "kaputt.paket.json"
    kaputt.write_text("???", encoding="utf-8")
    info_kaputt = zustand.beschreibe_paket(kaputt)
    assert info_kaputt.quelldatei == "kaputt" and info_kaputt.blockiert is True and info_kaputt.firma_id is None


def test_paket_ordner_standard(settings: Settings):
    assert zustand.paket_ordner(settings) == settings.log_ordner / "pakete"
