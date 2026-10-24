"""Tests für rabatte.anwenden (finde_aktionen, rabatt_prozent_aus, wende_an, alle_aktionen) und die
Integration in matching.pipeline / matching.pruefung (RABATT_ABWEICHUNG).

Die Belegpakete entstehen über die echte Pipeline mit den synthetischen Stammdaten aus
tests/fixtures (wik_leh: Kunde 3441 "Hegmann", Lieferant 72 "Cide", Artikel 1643/957 zu VK 6,90 €).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from openpyxl import Workbook

from auftragsautomat.config import Settings
from auftragsautomat.matching.aliase import AliasTabellen
from auftragsautomat.matching.pipeline import erstelle_belegpaket, pruefe_erneut
from auftragsautomat.matching.pruefung import pruefe
from auftragsautomat.models import (
    Aktion,
    AktionTreffer,
    Belegpaket,
    Eingangstyp,
    Extraktionsmethode,
    Kundengruppe,
    Pruefcode,
    RohBestellung,
    RohPosition,
    Schweregrad,
)
from auftragsautomat.rabatte import anwenden
from auftragsautomat.rabatte.anwenden import (
    alle_aktionen,
    bezugsdatum,
    finde_aktionen,
    leere_cache,
    rabatt_prozent_aus,
    wende_an,
)
from auftragsautomat.rabatte.rabattliste_pdf import speichere_aktionen
from auftragsautomat.stammdaten.laden import lade_stammdaten

# ---------------------------------------------------------------------------
# Hilfen
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _ohne_cache_und_gedaechtnis(settings: Settings, tmp_path: Path):
    leere_cache()
    settings.rabatte.gedaechtnis_datei = tmp_path / "config" / "aktionen.yaml"
    yield
    leere_cache()


def _pos(laufnr: int, bezeichnung: str, menge: int, preis: str, **felder: Any) -> RohPosition:
    return RohPosition(laufnr=laufnr, bezeichnung=bezeichnung, menge=Decimal(menge), preis=Decimal(preis), **felder)


def _kirchhellen(settings: Settings, **felder: Any) -> RohBestellung:
    felder.setdefault("kunde_text", "Weinmarkt Kirchhellen GmbH & Co. KG")
    felder.setdefault("bestellnummer", "B-4712")
    felder.setdefault("datum", date(2026, 9, 10))
    felder.setdefault("liefertag", date(2026, 9, 15))
    felder.setdefault(
        "positionen",
        [
            _pos(1, "Villa Armellina Gloria Secco Frizzante", 60, "6.90"),
            _pos(2, "Villa Armellina Rosato Secco Frizzante", 24, "6.90"),
            _pos(3, "DAA Neptun", 1, "0.00"),
        ],
    )
    return RohBestellung(
        eingangstyp=Eingangstyp.KUNDENBESTELLUNG_PDF,
        extraktionsmethode=Extraktionsmethode.PDFPLUMBER,
        quelldatei=settings.eingang_ordner / "kirchhellen.pdf",
        **felder,
    )


def _menrath(settings: Settings) -> RohBestellung:
    return RohBestellung(
        eingangstyp=Eingangstyp.KUNDENBESTELLUNG_PDF,
        extraktionsmethode=Extraktionsmethode.PDFPLUMBER,
        quelldatei=settings.eingang_ordner / "menrath.pdf",
        kunde_text="Weinhaus Menrath",
        bestellnummer="M-2026-09",
        liefertag=date(2026, 9, 20),
        positionen=[_pos(1, "Bollina Gavi di Gavi DOCG 2023", 12, "12.50"), _pos(2, "Colosi Nero d'Avola Sicilia DOC 2022", 6, "8.40")],
    )


def _aktion(aktion_id: str = "excel:Aktionen:2", **felder: Any) -> Aktion:
    basis: dict[str, Any] = {
        "aktion_id": aktion_id,
        "von": date(2026, 9, 1),
        "bis": date(2026, 9, 30),
        "rabatt_prozent": Decimal("10"),
        "quelle": aktion_id,
    }
    basis.update(felder)
    return Aktion(**basis)


def _paket(settings: Settings, roh: RohBestellung | None = None, aktionen: list[Aktion] | None = None) -> Belegpaket:
    return erstelle_belegpaket(roh or _kirchhellen(settings), settings, aktionen=aktionen if aktionen is not None else [])


def _treffer(paket: Belegpaket, settings: Settings, *aktionen: Aktion, **kw: Any) -> list[AktionTreffer]:
    return finde_aktionen(paket, list(aktionen), settings, **kw)


def _laufnummern(treffer: list[AktionTreffer]) -> list[int | None]:
    return [t.position_laufnr for t in treffer]


def _codes(paket: Belegpaket, code: Pruefcode) -> list[Any]:
    return [h for h in paket.alle_hinweise if h.code == code]


def _position(paket: Belegpaket, laufnr: int):
    return next(p for p in paket.positionen if p.roh.laufnr == laufnr)


# ---------------------------------------------------------------------------
# Treffer-Kriterien
# ---------------------------------------------------------------------------


def test_grundfall_treffer_auf_alle_aktiven_positionen(settings: Settings):
    paket = _paket(settings)
    treffer = _treffer(paket, settings, _aktion())
    assert _laufnummern(treffer) == [1, 2]  # Position 3 (DAA, übersprungen) nicht
    assert all(t.sicher for t in treffer)
    assert all(t.rabatt_prozent == Decimal("10.00") for t in treffer)
    assert "alle Kunden" in treffer[0].grund and "alle Artikel" in treffer[0].grund
    assert "Liefertag 15.09.2026 im Zeitraum 01.09.2026–30.09.2026" in treffer[0].grund


def test_kriterium_firma(settings: Settings):
    paket = _paket(settings)
    assert _treffer(paket, settings, _aktion(firma_id="woe_leh")) == []
    assert _laufnummern(_treffer(paket, settings, _aktion(firma_id="wik_leh"))) == [1, 2]
    assert _laufnummern(_treffer(paket, settings, _aktion(firma_id="WIK"))) == [1, 2]  # Kürzel
    assert _laufnummern(_treffer(paket, settings, _aktion(firma_id="Wein-Import-Kreis LEH"))) == [1, 2]
    assert _treffer(paket, settings, _aktion(firma_id="WOE")) == []


def test_kriterium_kunde(settings: Settings):
    paket = _paket(settings)
    assert _laufnummern(_treffer(paket, settings, _aktion(kundennummern=["3441"]))) == [1, 2]
    assert _laufnummern(_treffer(paket, settings, _aktion(kundennummern=["03441"]))) == [1, 2]
    assert _treffer(paket, settings, _aktion(kundennummern=["9999"])) == []
    assert _laufnummern(_treffer(paket, settings, _aktion(kundennummern=["9999"], kunde_kurz="Hegmann"))) == [1, 2]
    treffer = _treffer(paket, settings, _aktion(kunde_kurz="hegmann"))  # Matchcode
    assert _laufnummern(treffer) == [1, 2] and "Matchcode HEGMANN" in treffer[0].grund
    treffer = _treffer(paket, settings, _aktion(kunde_kurz="Weinmarkt Kirchhellen"))  # Kunden-Alias / Kundentext
    assert _laufnummern(treffer) == [1, 2]
    assert _laufnummern(_treffer(paket, settings, _aktion(kunde_kurz="Kirchhellen"))) == [1, 2]  # Teilwort im Kundentext
    assert _treffer(paket, settings, _aktion(kunde_kurz="Menrath")) == []
    assert _treffer(paket, settings, _aktion(kunde_kurz="Heg")) == []  # kein Wortteil


def test_kriterium_kundengruppe(settings: Settings):
    paket = _paket(settings)
    assert _treffer(paket, settings, _aktion(kunde_kurz="Fachhandel")) == []
    paket.kundengruppe = Kundengruppe.FACHHANDEL
    treffer = _treffer(paket, settings, _aktion(kunde_kurz="Fachhandel"))
    assert _laufnummern(treffer) == [1, 2] and "Kundengruppe fachhandel" in treffer[0].grund
    assert _laufnummern(_treffer(paket, settings, _aktion(kunde_kurz="FH"))) == [1, 2]
    assert _treffer(paket, settings, _aktion(kunde_kurz="LEH")) == []
    paket.kundengruppe = Kundengruppe.KETTE
    assert _laufnummern(_treffer(paket, settings, _aktion(kunde_kurz="Ketten"))) == [1, 2]
    assert _laufnummern(_treffer(paket, settings, _aktion(kunde_kurz="LEH"))) == [1, 2]


def test_kriterium_lieferant(settings: Settings):
    paket = _paket(settings)
    treffer = _treffer(paket, settings, _aktion(lieferant_kurz="Cide"))  # Alias -> Lieferant 72 der Artikel
    assert _laufnummern(treffer) == [1, 2] and "Lieferant 72 des Artikels" in treffer[0].grund
    treffer = _treffer(paket, settings, _aktion(lieferant_kurz="Cide"), aliase=AliasTabellen())  # ohne Aliase -> Warengruppe
    assert _laufnummern(treffer) == [1, 2] and "Warengruppe Cide" in treffer[0].grund
    assert _laufnummern(_treffer(paket, settings, _aktion(lieferant_kurz="72"))) == [1, 2]  # Lieferantennummer
    assert _laufnummern(_treffer(paket, settings, _aktion(lieferant_kurz="Villa Armellina"))) == [1, 2]  # Produktmarke
    assert _laufnummern(_treffer(paket, settings, _aktion(lieferant_kurz="Cide srl"))) == [1, 2]  # Lieferantenname
    assert _treffer(paket, settings, _aktion(lieferant_kurz="Colosi")) == []
    assert _treffer(paket, settings, _aktion(lieferant_kurz="Bollina")) == []


def test_kriterium_lieferant_je_position_bei_mehreren_weinguetern(settings: Settings):
    paket = _paket(settings, _menrath(settings))
    assert [p.artikel.artikelnummer for p in paket.positionen] == ["3001", "2001"]
    assert _laufnummern(_treffer(paket, settings, _aktion(lieferant_kurz="Bollina"))) == [1]
    assert _laufnummern(_treffer(paket, settings, _aktion(lieferant_kurz="Colosi"))) == [2]
    assert _laufnummern(_treffer(paket, settings, _aktion(lieferant_kurz="Cantine Colosi"))) == [2]


def test_kriterium_artikelnummer(settings: Settings):
    paket = _paket(settings)
    treffer = _treffer(paket, settings, _aktion(artikelnummern=["1643"]))
    assert _laufnummern(treffer) == [1] and treffer[0].sicher and "Artikelnummer 1643" in treffer[0].grund
    assert _laufnummern(_treffer(paket, settings, _aktion(artikelnummern=["0957", "1643"]))) == [1, 2]
    assert _treffer(paket, settings, _aktion(artikelnummern=["9999"])) == []
    # Artikelnummer passt nicht, aber der Artikeltext -> unscharf über den Text
    treffer = _treffer(paket, settings, _aktion(artikelnummern=["9999"], artikel_text="Villa Armellina Gloria Secco Frizzante"))
    assert _laufnummern(treffer)[0] == 1 and treffer[0].sicher


def test_kriterium_artikeltext_fuzzy(settings: Settings):
    paket = _paket(settings)
    treffer = _treffer(paket, settings, _aktion(artikel_text="Gloria Secco Frizzante 0,75 l"))
    assert treffer and treffer[0].position_laufnr == 1 and treffer[0].sicher
    assert "passt (95)" in treffer[0].grund  # alle Wörter der Aktion kommen im Artikel vor
    unsichere = [t for t in treffer if not t.sicher]
    assert all(t.position_laufnr == 2 for t in unsichere)  # Rosato nur ähnlich (falls überhaupt Kandidat)
    # ein einzelnes Wort bleibt unscharf
    treffer = _treffer(paket, settings, _aktion(artikel_text="Gloria"))
    assert [(t.position_laufnr, t.sicher) for t in treffer] == [(1, False)]
    # Volumen widerspricht -> kein Treffer
    assert _treffer(paket, settings, _aktion(artikel_text="Gloria Secco Frizzante 0,375 l")) == []
    # ganz anderer Artikel -> kein Treffer
    assert _treffer(paket, settings, _aktion(artikel_text="Riesling trocken")) == []


def test_kriterium_artikeltext_jahrgang_weicht_ab(settings: Settings):
    paket = _paket(settings, _menrath(settings))
    treffer = _treffer(paket, settings, _aktion(artikel_text="Bollina Gavi di Gavi DOCG 2023"))
    assert [(t.position_laufnr, t.sicher) for t in treffer] == [(1, True)]
    treffer = _treffer(paket, settings, _aktion(artikel_text="Bollina Gavi di Gavi DOCG 2022"))
    assert [(t.position_laufnr, t.sicher) for t in treffer] == [(1, False)]
    assert "Jahrgang 2023 statt 2022" in treffer[0].grund


def test_sondervariante_ohne_volumen_in_der_aktion_ist_unsicher(settings: Settings):
    paket = _paket(settings)
    daten = lade_stammdaten(settings.firma("wik_leh"))
    from auftragsautomat.matching.artikel import ordne_manuell

    paket.positionen[1] = ordne_manuell(paket.positionen[1], daten.artikel_nach_nummer["958"])  # 1,5 l Magnum
    treffer = _treffer(paket, settings, _aktion(artikel_text="Villa Armellina Rosato Secco Frizzante"))
    magnum = next(t for t in treffer if t.position_laufnr == 2)
    assert magnum.sicher is False and "Sondervariante" in magnum.grund


# ---------------------------------------------------------------------------
# Datumsbezug und Toleranz
# ---------------------------------------------------------------------------


def test_bezugsdatum(settings: Settings):
    paket = _paket(settings)
    assert bezugsdatum(paket, settings) == (date(2026, 9, 15), "Liefertag", True)
    settings.rabatte.bezug = "auftragsdatum"
    assert bezugsdatum(paket, settings) == (date(2026, 9, 10), "Auftragsdatum", True)
    paket.roh.datum = None
    assert bezugsdatum(paket, settings)[0:2] == (date(2026, 9, 15), "Liefertag")
    paket.roh.liefertag = None
    datum, text, aus_dokument = bezugsdatum(paket, settings, heute=date(2026, 10, 1))
    assert (datum, aus_dokument) == (date(2026, 10, 1), False) and "heute" in text


def test_zeitraum_bezug_liefertag_oder_auftragsdatum(settings: Settings):
    paket = _paket(settings)
    oktober = _aktion(von=date(2026, 10, 1), bis=date(2026, 10, 31))
    um_den_10_9 = _aktion(von=date(2026, 9, 8), bis=date(2026, 9, 12))
    assert _treffer(paket, settings, oktober) == []
    assert _treffer(paket, settings, um_den_10_9) == []  # Liefertag 15.09. liegt außerhalb
    settings.rabatte.bezug = "auftragsdatum"
    treffer = _treffer(paket, settings, um_den_10_9)
    assert _laufnummern(treffer) == [1, 2] and all(t.sicher for t in treffer)
    assert "Auftragsdatum 10.09.2026" in treffer[0].grund
    assert _treffer(paket, settings, _aktion(von=date(2026, 9, 15), bis=date(2026, 9, 15))) == []


def test_toleranz_tage_macht_treffer_unsicher(settings: Settings):
    paket = _paket(settings)
    oktober = _aktion(von=date(2026, 10, 1), bis=date(2026, 10, 31))
    settings.rabatte.toleranz_tage = 15
    assert _treffer(paket, settings, oktober) == []  # 16 Tage Abstand
    settings.rabatte.toleranz_tage = 16
    treffer = _treffer(paket, settings, oktober)
    assert _laufnummern(treffer) == [1, 2]
    assert all(not t.sicher for t in treffer)
    assert "16 Tag(e) außerhalb des Zeitraums" in treffer[0].grund
    paket = wende_an(paket, treffer, settings)
    assert _position(paket, 1).rabatt_prozent is None
    assert len(_codes(paket, Pruefcode.RABATT_UNSICHER)) == 2


def test_ohne_liefertag_und_datum_zaehlt_heute_als_unsicher(settings: Settings):
    paket = _paket(settings, _kirchhellen(settings, liefertag=None, datum=None))
    treffer = _treffer(paket, settings, _aktion(), heute=date(2026, 9, 20))
    assert _laufnummern(treffer) == [1, 2] and all(not t.sicher for t in treffer)
    assert "heute" in treffer[0].grund
    assert _treffer(paket, settings, _aktion(), heute=date(2026, 12, 1)) == []


def test_inaktive_aktionen_und_rabatte_aus(settings: Settings):
    paket = _paket(settings)
    assert _treffer(paket, settings, _aktion(aktiv=False)) == []
    settings.rabatte.aktiv = False
    assert _treffer(paket, settings, _aktion()) == []
    assert alle_aktionen(settings) == []


# ---------------------------------------------------------------------------
# EUR / Aktionspreis -> Prozent
# ---------------------------------------------------------------------------


def test_rabatt_prozent_aus():
    vk = Decimal("6.90")
    assert rabatt_prozent_aus(_aktion(rabatt_prozent=Decimal("10")), vk) == Decimal("10.00")
    assert rabatt_prozent_aus(_aktion(rabatt_prozent=Decimal("7.555")), vk) == Decimal("7.56")
    assert rabatt_prozent_aus(_aktion(rabatt_prozent=Decimal("10")), None) == Decimal("10.00")
    assert rabatt_prozent_aus(_aktion(rabatt_prozent=None, rabatt_eur=Decimal("0.69")), vk) == Decimal("10.00")
    assert rabatt_prozent_aus(_aktion(rabatt_prozent=None, rabatt_eur=Decimal("0.345")), vk) == Decimal("5.00")
    assert rabatt_prozent_aus(_aktion(rabatt_prozent=None, rabatt_eur=Decimal("1.00")), vk) == Decimal("14.49")
    assert rabatt_prozent_aus(_aktion(rabatt_prozent=None, aktionspreis=Decimal("6.21")), vk) == Decimal("10.00")
    assert rabatt_prozent_aus(_aktion(rabatt_prozent=None, aktionspreis=Decimal("5.90")), vk) == Decimal("14.49")
    # nicht berechenbar / negativ
    assert rabatt_prozent_aus(_aktion(rabatt_prozent=None, rabatt_eur=Decimal("0.69")), None) is None
    assert rabatt_prozent_aus(_aktion(rabatt_prozent=None, rabatt_eur=Decimal("0.69")), Decimal("0")) is None
    assert rabatt_prozent_aus(_aktion(rabatt_prozent=None, aktionspreis=Decimal("6.90")), vk) is None
    assert rabatt_prozent_aus(_aktion(rabatt_prozent=None, aktionspreis=Decimal("7.50")), vk) is None
    assert rabatt_prozent_aus(_aktion(rabatt_prozent=None, rabatt_eur=Decimal("9.00")), vk) is None
    assert rabatt_prozent_aus(_aktion(rabatt_prozent=None), vk) is None
    assert rabatt_prozent_aus(_aktion(rabatt_prozent=Decimal("0")), vk) is None


def test_eur_und_aktionspreis_im_paket(settings: Settings):
    paket = _paket(settings)
    treffer = _treffer(paket, settings, _aktion(rabatt_prozent=None, aktionspreis=Decimal("6.21")))
    assert [t.rabatt_prozent for t in treffer] == [Decimal("10.00"), Decimal("10.00")]
    paket = wende_an(paket, treffer, settings)
    assert _position(paket, 1).rabatt_prozent == Decimal("10.00")
    assert _position(paket, 1).vk_preis_rabattiert == Decimal("6.2100")

    # Aktionspreis über dem VK: Treffer bleibt, aber unsicher und ohne Rabatt
    treffer = _treffer(paket, settings, _aktion(rabatt_prozent=None, aktionspreis=Decimal("7.50")))
    assert treffer and all(t.rabatt_prozent is None and not t.sicher for t in treffer)
    assert "nicht berechenbar" in treffer[0].grund


# ---------------------------------------------------------------------------
# wende_an
# ---------------------------------------------------------------------------


def test_wende_an_sicherer_treffer(settings: Settings):
    paket = _paket(settings)
    aktion = _aktion(bezeichnung="Herbstaktion", artikelnummern=["1643"])
    treffer = _treffer(paket, settings, aktion)
    paket = wende_an(paket, treffer, settings)
    p1, p2 = _position(paket, 1), _position(paket, 2)
    assert (p1.rabatt_prozent, p1.rabatt_quelle) == (Decimal("10.00"), "aktion:excel:Aktionen:2")
    assert (p2.rabatt_prozent, p2.rabatt_quelle) == (None, None)
    assert p1.vk_preis == Decimal("6.90")  # VK bleibt Preisgruppe 1
    assert p1.vk_preis_rabattiert == Decimal("6.2100")
    assert (p1.ek_preis, p1.netto_ek) == (Decimal("4.20"), Decimal("252.00"))
    assert p1.netto_vk == Decimal("372.60")
    assert paket.aktionen == treffer and treffer[0].angewendet is True
    vorschlag = _codes(paket, Pruefcode.RABATT_VORSCHLAG)
    assert len(vorschlag) == 1
    assert vorschlag[0].schweregrad == Schweregrad.INFO and vorschlag[0].position_laufnr == 1
    assert "Herbstaktion" in vorschlag[0].text and "10 %" in vorschlag[0].text
    assert vorschlag[0].details["angewendet"] == "ja" and vorschlag[0].details["aktion_id"] == aktion.aktion_id
    assert _codes(paket, Pruefcode.RABATT_UNSICHER) == []
    assert not paket.blockiert


def test_wende_an_automatisch_anwenden_aus(settings: Settings):
    settings.rabatte.automatisch_anwenden = False
    paket = _paket(settings)
    paket = wende_an(paket, _treffer(paket, settings, _aktion()), settings)
    assert all(p.rabatt_prozent is None for p in paket.positionen)
    vorschlag = _codes(paket, Pruefcode.RABATT_VORSCHLAG)
    assert len(vorschlag) == 2 and all(h.details["angewendet"] == "nein" for h in vorschlag)
    assert all(t.angewendet is False for t in paket.aktionen)


def test_mehrere_sichere_aktionen_werden_unsicher_hoechster_zuerst(settings: Settings):
    paket = _paket(settings)
    fuenf = _aktion("excel:Aktionen:2", rabatt_prozent=Decimal("5"))
    zehn = _aktion("excel:Aktionen:3", rabatt_prozent=Decimal("10"))
    treffer = _treffer(paket, settings, fuenf, zehn)
    assert all(not t.sicher for t in treffer)
    assert [(t.position_laufnr, t.rabatt_prozent) for t in treffer] == [
        (1, Decimal("10.00")),
        (1, Decimal("5.00")),
        (2, Decimal("10.00")),
        (2, Decimal("5.00")),
    ]
    assert "mehrere Aktionen passen" in treffer[0].grund and "höchster Rabatt 10 %" in treffer[0].grund
    paket = wende_an(paket, treffer, settings)
    assert all(p.rabatt_prozent is None for p in paket.positionen)
    unsicher = _codes(paket, Pruefcode.RABATT_UNSICHER)
    assert [h.position_laufnr for h in unsicher] == [1, 2]
    assert unsicher[0].details["rabatt_vorschlag"] == "10 %"
    assert unsicher[0].details["kandidaten"] == "excel:Aktionen:3,excel:Aktionen:2"
    assert not paket.blockiert


def test_sichere_und_unsichere_aktion_mit_anderem_satz_werden_unsicher(settings: Settings):
    """Regression (Review Befund 14): Aktion A (September, 10 %, sicher) und Aktion B (Oktober, 25 %, nur über
    Toleranztage) auf derselben Position – A darf nicht still angewendet werden; RABATT_UNSICHER nennt beide."""
    settings.rabatte.toleranz_tage = 30
    paket = _paket(settings)
    a = _aktion("excel:Aktionen:2")
    b = _aktion("excel:Aktionen:3", von=date(2026, 10, 1), bis=date(2026, 10, 31), rabatt_prozent=Decimal("25"))
    treffer = _treffer(paket, settings, a, b)
    pos1 = [t for t in treffer if t.position_laufnr == 1]
    assert len(pos1) == 2 and all(not t.sicher for t in pos1)
    assert all("excel:Aktionen:2 (10 %)" in t.grund and "excel:Aktionen:3 (25 %)" in t.grund for t in pos1)
    paket = wende_an(paket, treffer, settings)
    assert _position(paket, 1).rabatt_prozent is None and _position(paket, 1).rabatt_quelle is None
    unsicher = [h for h in _codes(paket, Pruefcode.RABATT_UNSICHER) if h.position_laufnr == 1]
    assert len(unsicher) == 1 and set(unsicher[0].details["kandidaten"].split(",")) == {"excel:Aktionen:2", "excel:Aktionen:3"}
    assert not [h for h in _codes(paket, Pruefcode.RABATT_VORSCHLAG) if h.position_laufnr == 1]


def test_mehrere_aktionen_mit_gleichem_rabatt_bleiben_sicher(settings: Settings):
    paket = _paket(settings)
    a = _aktion("excel:Aktionen:2")
    b = _aktion("pdf:liste.pdf:1:1")
    treffer = _treffer(paket, settings, a, b)
    pos1 = [t for t in treffer if t.position_laufnr == 1]
    assert [t.sicher for t in pos1] == [True, False]
    paket = wende_an(paket, treffer, settings)
    assert _position(paket, 1).rabatt_quelle == "aktion:excel:Aktionen:2"


def test_doppelte_aktion_id_zaehlt_einmal(settings: Settings):
    paket = _paket(settings)
    treffer = _treffer(paket, settings, _aktion("x"), _aktion("x"))
    assert _laufnummern(treffer) == [1, 2] and all(t.sicher for t in treffer)


def test_manueller_rabatt_wird_nie_ueberschrieben(settings: Settings):
    paket = _paket(settings)
    p1 = _position(paket, 1)
    p1.rabatt_prozent = Decimal("7")
    p1.rabatt_quelle = "manuell"
    treffer = _treffer(paket, settings, _aktion())
    paket = wende_an(paket, treffer, settings)
    assert (_position(paket, 1).rabatt_prozent, _position(paket, 1).rabatt_quelle) == (Decimal("7"), "manuell")
    assert (_position(paket, 2).rabatt_prozent, _position(paket, 2).rabatt_quelle) == (Decimal("10.00"), "aktion:excel:Aktionen:2")
    hinweis = next(h for h in _codes(paket, Pruefcode.RABATT_VORSCHLAG) if h.position_laufnr == 1)
    assert hinweis.schweregrad == Schweregrad.INFO and "bleibt" in hinweis.text
    assert hinweis.details["rabatt_quelle"] == "manuell" and hinweis.details["angewendet"] == "nein"
    # auch ein Rabatt mit Quelle "dokument" oder ohne Quelle bleibt stehen
    p2 = _position(paket, 2)
    p2.rabatt_quelle = "dokument"
    paket = wende_an(paket, _treffer(paket, settings, _aktion(rabatt_prozent=Decimal("20"))), settings)
    assert (_position(paket, 2).rabatt_prozent, _position(paket, 2).rabatt_quelle) == (Decimal("10.00"), "dokument")
    assert _position(paket, 1).rabatt_prozent == Decimal("7")


def test_max_rabatt_blockiert(settings: Settings):
    paket = _paket(settings)
    treffer = _treffer(paket, settings, _aktion(rabatt_prozent=Decimal("60")))
    assert all(t.sicher for t in treffer)
    paket = wende_an(paket, treffer, settings)
    assert all(p.rabatt_prozent is None for p in paket.positionen)
    fehler = [h for h in _codes(paket, Pruefcode.RABATT_VORSCHLAG) if h.schweregrad == Schweregrad.FEHLER]
    assert [h.position_laufnr for h in fehler] == [1, 2]
    assert "Plausibilitätsgrenze von 50 %" in fehler[0].text
    assert fehler[0].details["max_rabatt_prozent"] == "50 %"
    assert paket.blockiert
    settings.rabatte.max_rabatt_prozent = Decimal("70")
    paket = wende_an(paket, _treffer(paket, settings, _aktion(rabatt_prozent=Decimal("60"))), settings)
    assert _position(paket, 1).rabatt_prozent == Decimal("60.00") and not paket.blockiert


def test_wende_an_ist_idempotent_und_raeumt_auf(settings: Settings):
    paket = _paket(settings)
    treffer = _treffer(paket, settings, _aktion())
    wende_an(paket, treffer, settings)
    wende_an(paket, _treffer(paket, settings, _aktion()), settings)
    assert len(_codes(paket, Pruefcode.RABATT_VORSCHLAG)) == 2
    assert _position(paket, 1).rabatt_prozent == Decimal("10.00")
    # Aktion entfällt -> eigener Eintrag wird entfernt, Hinweise verschwinden
    paket = wende_an(paket, [], settings)
    assert all(p.rabatt_prozent is None and p.rabatt_quelle is None for p in paket.positionen)
    assert _codes(paket, Pruefcode.RABATT_VORSCHLAG) == [] and paket.aktionen == []


def test_beleg_ebene_treffer_nur_als_info(settings: Settings):
    paket = _paket(settings)
    treffer = [AktionTreffer(aktion=_aktion(), position_laufnr=None, rabatt_prozent=Decimal("10"), grund="Belegebene")]
    paket = wende_an(paket, treffer, settings)
    assert all(p.rabatt_prozent is None for p in paket.positionen)
    info = _codes(paket, Pruefcode.RABATT_VORSCHLAG)
    assert len(info) == 1 and info[0].position_laufnr is None and "Belegebene" in info[0].text


# ---------------------------------------------------------------------------
# Pipeline-Integration und Prüfung (RABATT_ABWEICHUNG)
# ---------------------------------------------------------------------------


def test_pipeline_ohne_aktionsquellen_unveraendert(settings: Settings):
    paket = erstelle_belegpaket(_kirchhellen(settings), settings)  # aktionen=None -> alle_aktionen -> []
    assert paket.aktionen == []
    assert all(p.rabatt_prozent is None for p in paket.positionen)
    assert not any(h.code.value.startswith("RABATT") for h in paket.alle_hinweise)
    assert paket.summe_netto_vk == Decimal("579.60")


def test_pipeline_integration_rabatt_im_verkauf_nicht_im_einkauf(settings: Settings):
    aktion = _aktion(kunde_kurz="Hegmann", lieferant_kurz="Cide", artikelnummern=["1643"])
    paket = erstelle_belegpaket(_kirchhellen(settings), settings, aktionen=[aktion])
    p1, p2 = _position(paket, 1), _position(paket, 2)
    assert (p1.rabatt_prozent, p1.rabatt_quelle) == (Decimal("10.00"), "aktion:excel:Aktionen:2")
    assert p1.netto_vk == Decimal("372.60") and p2.netto_vk == Decimal("165.60")
    assert p1.netto_ek == Decimal("252.00") and p2.netto_ek == Decimal("100.80")
    assert paket.summe_netto_vk == Decimal("538.20")
    assert paket.summe_netto_ek == Decimal("352.80")
    assert len(paket.aktionen) == 1 and paket.aktionen[0].angewendet
    assert [h.position_laufnr for h in _codes(paket, Pruefcode.RABATT_VORSCHLAG)] == [1]
    assert _codes(paket, Pruefcode.RABATT_ABWEICHUNG) == []
    assert not paket.blockiert

    # erneute Prüfung behält den Rabatt (Aktion weiterhin gültig) und bleibt idempotent
    paket = pruefe_erneut(paket, settings, aktionen=[aktion])
    assert _position(paket, 1).rabatt_prozent == Decimal("10.00")
    assert len(_codes(paket, Pruefcode.RABATT_VORSCHLAG)) == 1
    # ohne Aktionen verschwindet der automatische Rabatt wieder
    paket = pruefe_erneut(paket, settings, aktionen=[])
    assert _position(paket, 1).rabatt_prozent is None and paket.aktionen == []


def test_pipeline_ohne_firma_wendet_prozentaktionen_an(settings: Settings):
    roh = _kirchhellen(settings, kunde_text="Unbekannter Kunde", positionen=[_pos(1, "Irgendein Wein", 6, "5.00")])
    paket = erstelle_belegpaket(roh, settings, aktionen=[_aktion()])
    assert paket.firma_id is None
    assert _position(paket, 1).rabatt_prozent == Decimal("10.00")
    assert paket.blockiert  # FIRMA_UNKLAR, unabhängig vom Rabatt


def test_rabatt_abweichung_dokument_ohne_aktion(settings: Settings):
    roh = _kirchhellen(settings)
    roh.positionen[0].rabatt_prozent = Decimal("10")
    roh.positionen[0].rabatt_text = "Aktion -10 %"
    paket = erstelle_belegpaket(roh, settings, aktionen=[])
    abweichung = _codes(paket, Pruefcode.RABATT_ABWEICHUNG)
    assert len(abweichung) == 1
    hinweis = abweichung[0]
    assert (hinweis.schweregrad, hinweis.position_laufnr) == (Schweregrad.WARNUNG, 1)
    assert hinweis.details == {"rabatt_dokument": "10", "rabatt_zugeordnet": "0", "rabatt_quelle": ""}
    assert "Aktion -10 %" in hinweis.text and "kein Rabatt" in hinweis.text
    assert not paket.blockiert


def test_rabatt_abweichung_aktion_passt_oder_weicht_ab(settings: Settings):
    roh = _kirchhellen(settings)
    roh.positionen[0].rabatt_prozent = Decimal("10")
    paket = erstelle_belegpaket(roh, settings, aktionen=[_aktion(artikelnummern=["1643"])])
    assert _codes(paket, Pruefcode.RABATT_ABWEICHUNG) == []

    paket = erstelle_belegpaket(roh, settings, aktionen=[_aktion(artikelnummern=["1643"], rabatt_prozent=Decimal("5"))])
    abweichung = _codes(paket, Pruefcode.RABATT_ABWEICHUNG)
    assert len(abweichung) == 1
    assert abweichung[0].details == {"rabatt_dokument": "10", "rabatt_zugeordnet": "5", "rabatt_quelle": "aktion:excel:Aktionen:2"}


def test_rabatt_abweichung_ist_idempotent(settings: Settings):
    roh = _kirchhellen(settings)
    roh.positionen[0].rabatt_prozent = Decimal("10")
    paket = erstelle_belegpaket(roh, settings, aktionen=[])
    pruefe(paket, None, settings)
    pruefe(paket, None, settings)
    assert len(_codes(paket, Pruefcode.RABATT_ABWEICHUNG)) == 1
    _position(paket, 1).rabatt_prozent = Decimal("10")
    _position(paket, 1).rabatt_quelle = "manuell"
    pruefe(paket, None, settings)
    assert _codes(paket, Pruefcode.RABATT_ABWEICHUNG) == []


# ---------------------------------------------------------------------------
# alle_aktionen
# ---------------------------------------------------------------------------


def _xlsx(pfad: Path, zeilen: list[list[Any]], blatt: str = "Aktionen") -> Path:
    mappe = Workbook()
    ws = mappe.active
    ws.title = blatt
    for zeile in zeilen:
        ws.append(zeile)
    mappe.save(pfad)
    return pfad


def test_alle_aktionen_excel_und_gedaechtnis(settings: Settings, tmp_path: Path):
    xlsx = _xlsx(
        tmp_path / "aktionen.xlsx",
        [
            ["Kunde", "Lieferant", "Von", "Bis", "Rabatt %", "Firma"],
            ["Hegmann", "Cide", date(2026, 9, 1), date(2026, 9, 30), "10 %", "WOE"],
            ["Menrath", "Bollina", date(2026, 9, 1), date(2026, 9, 30), None, None],  # ohne Rabatt -> Warnung
        ],
    )
    settings.rabatte.aktionen_datei.lokaler_pfad = xlsx
    speichere_aktionen([_aktion("pdf:liste.pdf:1:1", rabatt_prozent=Decimal("5")), _aktion("excel:Aktionen:2", rabatt_prozent=Decimal("99"))], settings.rabatte.gedaechtnis_datei)

    aktionen = alle_aktionen(settings, cache=False)
    assert [a.aktion_id for a in aktionen] == ["excel:Aktionen:2", "pdf:liste.pdf:1:1"]
    assert aktionen[0].firma_id == "woe_leh" and aktionen[0].rabatt_prozent == Decimal("10")  # Excel vor Gedächtnis

    # Pipeline ohne explizite Aktionen zieht beide Quellen (Excel-Aktion gilt nur für WOE -> nur die PDF-Aktion greift)
    paket = erstelle_belegpaket(_kirchhellen(settings), settings)
    assert _position(paket, 1).rabatt_quelle == "aktion:pdf:liste.pdf:1:1"
    assert _position(paket, 1).rabatt_prozent == Decimal("5.00")


def test_alle_aktionen_nur_gedaechtnis_und_cache(settings: Settings):
    speichere_aktionen([_aktion("pdf:liste.pdf:1:1")], settings.rabatte.gedaechtnis_datei)
    assert [a.aktion_id for a in alle_aktionen(settings)] == ["pdf:liste.pdf:1:1"]
    speichere_aktionen([_aktion("pdf:liste.pdf:1:2")], settings.rabatte.gedaechtnis_datei)
    assert [a.aktion_id for a in alle_aktionen(settings)] == ["pdf:liste.pdf:1:1"]  # aus dem Cache
    assert [a.aktion_id for a in alle_aktionen(settings, cache=False)] == ["pdf:liste.pdf:1:1", "pdf:liste.pdf:1:2"]
    leere_cache()
    assert [a.aktion_id for a in alle_aktionen(settings)] == ["pdf:liste.pdf:1:1", "pdf:liste.pdf:1:2"]


def test_alle_aktionen_fehler_werden_nur_protokolliert(settings: Settings, tmp_path: Path, caplog: pytest.LogCaptureFixture):
    settings.rabatte.aktionen_datei.lokaler_pfad = tmp_path / "gibt_es_nicht.xlsx"
    speichere_aktionen([_aktion("pdf:liste.pdf:1:1")], settings.rabatte.gedaechtnis_datei)
    with caplog.at_level("WARNING", logger=anwenden.__name__):
        aktionen = alle_aktionen(settings, cache=False)
    assert [a.aktion_id for a in aktionen] == ["pdf:liste.pdf:1:1"]
    assert any("Aktionen-Excel" in r.message for r in caplog.records)

    settings.rabatte.gedaechtnis_datei.write_text("aktionen: [kaputt", encoding="utf-8")
    with caplog.at_level("WARNING", logger=anwenden.__name__):
        assert alle_aktionen(settings, cache=False) == []


def test_alle_aktionen_mit_uebergebenem_backend(settings: Settings, tmp_path: Path):
    from auftragsautomat.m365.fake import FakeExcelBackend

    settings.rabatte.aktionen_datei.lokaler_pfad = tmp_path / "egal.xlsx"
    backend = FakeExcelBackend(
        {str(settings.rabatte.aktionen_datei.lokaler_pfad): {"Aktionen": [["Von", "Bis", "Rabatt %"], [date(2026, 9, 1), date(2026, 9, 30), 10]]}}
    )
    aktionen = alle_aktionen(settings, backends=backend, cache=False)
    assert [a.aktion_id for a in aktionen] == ["excel:Aktionen:2"]
