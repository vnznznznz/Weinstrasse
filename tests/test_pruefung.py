"""Tests für matching.pruefung (Prüfregeln, Dubletten, Belegfelder, Idempotenz) mit dem wik_leh-Stamm."""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from auftragsautomat.config import Settings
from auftragsautomat.matching.pruefung import (
    PRUEF_CODES,
    belegfelder_vorbelegen,
    entferne_pruefhinweise,
    gruppiere_nach_lieferant,
    ist_pruefhinweis,
    lieferant_der_position,
    normalisiere_belegnummer,
    pruefe,
    pruefe_dubletten,
    zusammenfassung_hinweise,
)
from auftragsautomat.models import (
    Artikel,
    ArtikelKandidat,
    Beleg,
    Belegart,
    Belegfelder,
    Belegpaket,
    Eingangstyp,
    Extraktionsmethode,
    Kunde,
    KundeKandidat,
    KundeZuordnung,
    Lieferant,
    LieferantKandidat,
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
from auftragsautomat.stammdaten.basis import Stammdaten
from auftragsautomat.stammdaten.laden import lade_stammdaten


@pytest.fixture()
def stammdaten(settings: Settings) -> Stammdaten:
    return lade_stammdaten(settings.firma("wik_leh"))


def _roh(**felder) -> RohBestellung:
    felder.setdefault("eingangstyp", Eingangstyp.KUNDENBESTELLUNG_PDF)
    felder.setdefault("extraktionsmethode", Extraktionsmethode.PDFPLUMBER)
    felder.setdefault("quelldatei", Path("eingang/kirchhellen.pdf"))
    felder.setdefault("kunde_text", "Weinmarkt Kirchhellen")
    felder.setdefault("belegnummer", "2026-0917")
    felder.setdefault("bestellnummer", "B-4712")
    felder.setdefault("liefertag", date(2026, 9, 15))
    felder.setdefault(
        "positionen",
        [
            RohPosition(
                laufnr=1, bezeichnung="Gloria Secco Frizzante", menge=Decimal(60), preis=Decimal("6.90")
            ),
            RohPosition(
                laufnr=2, bezeichnung="Rosato Secco Frizzante", menge=Decimal(24), preis=Decimal("6.90")
            ),
            RohPosition(laufnr=3, bezeichnung="DAA Neptun", menge=Decimal(1), preis=Decimal("0")),
        ],
    )
    return RohBestellung(**felder)


def _position(roh: RohPosition, artikel: Artikel | None, **felder) -> PositionsZuordnung:
    if artikel is not None:
        felder.setdefault("status", MatchStatus.SICHER)
        felder.setdefault("quelle", "alias")
        felder.setdefault("ek_preis", artikel.ek_preis)
        felder.setdefault("vk_preis", artikel.vk_preis_1)
    felder.setdefault("menge", roh.menge if roh.menge is not None else Decimal(0))
    felder.setdefault("preis_dokument", roh.preis)
    return PositionsZuordnung(roh=roh, artikel=artikel, **felder)


def _uebersprungen(roh: RohPosition) -> PositionsZuordnung:
    return PositionsZuordnung(
        roh=roh,
        status=MatchStatus.UEBERSPRUNGEN,
        menge=Decimal(0),
        preis_dokument=roh.preis,
        hinweise=[
            Pruefhinweis(
                code=Pruefcode.NULLPREIS_OHNE_ARTIKEL,
                schweregrad=Schweregrad.WARNUNG,
                text="übersprungen",
                position_laufnr=roh.laufnr,
            )
        ],
    )


def _paket(stammdaten: Stammdaten, roh: RohBestellung | None = None, **felder) -> Belegpaket:
    """Kirchhellen: 60 x 1643, 24 x 957, 'DAA Neptun' übersprungen; Kunde 3441, Lieferant 72."""
    roh = roh or _roh()
    artikel = stammdaten.artikel_nach_nummer
    felder.setdefault("firma_id", "wik_leh")
    felder.setdefault("firma_quelle", "alias_kunde")
    felder.setdefault(
        "kunde",
        KundeZuordnung(
            kunde=stammdaten.kunden_nach_nummer["3441"], status=MatchStatus.SICHER, quelle="alias"
        ),
    )
    felder.setdefault(
        "lieferant",
        LieferantZuordnung(
            lieferant=stammdaten.lieferanten_nach_nummer["72"], status=MatchStatus.SICHER, quelle="positionen"
        ),
    )
    felder.setdefault(
        "positionen",
        [
            _position(roh.positionen[0], artikel["1643"]),
            _position(roh.positionen[1], artikel["957"]),
            _uebersprungen(roh.positionen[2]),
        ],
    )
    return Belegpaket(roh=roh, **felder)


def _codes(hinweise: list[Pruefhinweis]) -> list[Pruefcode]:
    return [h.code for h in hinweise]


def _hinweis(paket: Belegpaket, code: Pruefcode, laufnr: int | None = None) -> Pruefhinweis:
    return next(h for h in paket.alle_hinweise if h.code == code and h.position_laufnr == laufnr)


def _signatur(paket: Belegpaket) -> list[tuple[str, str, int | None, str]]:
    return [(h.code.value, h.schweregrad.value, h.position_laufnr, h.text) for h in paket.alle_hinweise]


# ---------------------------------------------------------------------------
# Sauberes Paket, Belegfelder
# ---------------------------------------------------------------------------


def test_sauberes_paket(stammdaten: Stammdaten, settings: Settings):
    paket = _paket(stammdaten)
    ergebnis = pruefe(paket, stammdaten, settings)
    assert ergebnis is paket
    assert paket.blockiert is False
    assert _codes(paket.hinweise) == []
    assert _codes(paket.alle_hinweise) == [Pruefcode.NULLPREIS_OHNE_ARTIKEL]
    assert paket.belegfelder == Belegfelder(
        spedition="Spedition Weinlogistik", weingut="Cide srl", shipment="15.09.2026", kunde="Hegmann"
    )
    assert paket.gesamtmenge == Decimal(84)
    assert paket.summe_netto_vk == Decimal("579.60")


def test_belegfelder_vorbelegung_behaelt_anwenderwerte(stammdaten: Stammdaten, settings: Settings):
    paket = _paket(stammdaten, belegfelder=Belegfelder(spedition="Eigene Spedition", kunde="Hegmann Lager"))
    felder = belegfelder_vorbelegen(paket, settings)
    assert felder is paket.belegfelder
    assert felder.spedition == "Eigene Spedition"
    assert felder.kunde == "Hegmann Lager"
    assert felder.weingut == "Cide srl" and felder.shipment == "15.09.2026"


def test_belegfelder_fehlen(stammdaten: Stammdaten, settings: Settings):
    roh = _roh(liefertag=None)
    paket = _paket(stammdaten, roh, lieferant=LieferantZuordnung(), einkauf_erforderlich=False)
    pruefe(paket, stammdaten, settings)
    fehlend = [h for h in paket.hinweise if h.code == Pruefcode.BELEGFELD_FEHLT]
    assert [h.details["feld"] for h in fehlend] == ["weingut", "shipment"]
    assert all(h.schweregrad == Schweregrad.FEHLER for h in fehlend)
    assert paket.blockiert is True
    # Anwender füllt die Felder nach -> erneute Prüfung ist sauber
    paket.belegfelder.weingut = "Cide srl"
    paket.belegfelder.shipment = "KW 38"
    pruefe(paket, stammdaten, settings)
    assert Pruefcode.BELEGFELD_FEHLT not in _codes(paket.hinweise)
    assert paket.blockiert is False


def test_belegfelder_standard_mit_unbekanntem_feld(stammdaten: Stammdaten, settings: Settings, caplog):
    firma = settings.firma("wik_leh")
    firma.belegfelder_standard = {"Spedition": "Standard-Spedition", "foo": "bar", "weingut": "Nicht benutzt"}
    paket = _paket(stammdaten)
    belegfelder_vorbelegen(paket, settings)
    assert paket.belegfelder.spedition == "Standard-Spedition"
    assert paket.belegfelder.weingut == "Cide srl"  # Lieferantenname geht vor Standard
    assert "unbekanntes Feld 'foo'" in caplog.text


# ---------------------------------------------------------------------------
# Preise und Mengen
# ---------------------------------------------------------------------------


def test_preisabweichung(stammdaten: Stammdaten, settings: Settings):
    roh = _roh()
    roh.positionen[1].preis = Decimal("6.50")
    paket = _paket(stammdaten, roh)
    pruefe(paket, stammdaten, settings)
    hinweis = _hinweis(paket, Pruefcode.PREIS_ABWEICHUNG, 2)
    assert hinweis.schweregrad == Schweregrad.WARNUNG
    assert hinweis in paket.positionen[1].hinweise
    assert hinweis.details == {
        "vk_preis": "6,90",
        "preis_dokument": "6,50",
        "abweichung_eur": "0,40",
        "abweichung_prozent": "5,8",
    }
    assert "6,50" in hinweis.text and "6,90" in hinweis.text and "Lexware-Preis" in hinweis.text
    assert paket.blockiert is False  # Warnung, keine Blockade
    assert not [h for h in paket.positionen[0].hinweise if h.code == Pruefcode.PREIS_ABWEICHUNG]


def test_preisabweichung_toleranz(stammdaten: Stammdaten, settings: Settings):
    roh = _roh()
    roh.positionen[1].preis = Decimal("6.50")
    settings.matching.preis_toleranz_eur = Decimal("0.40")
    paket = pruefe(_paket(stammdaten, roh), stammdaten, settings)
    assert Pruefcode.PREIS_ABWEICHUNG not in _codes(paket.alle_hinweise)


def test_preisabweichung_nur_mit_beiden_preisen(stammdaten: Stammdaten, settings: Settings):
    roh = _roh()
    roh.positionen[0].preis = None
    paket = _paket(stammdaten, roh)
    paket.positionen[2].preis_dokument = Decimal(0)  # übersprungen: ohne vk_preis, kein Vergleich
    pruefe(paket, stammdaten, settings)
    assert Pruefcode.PREIS_ABWEICHUNG not in _codes(paket.alle_hinweise)


def test_preis_null_im_dokument(stammdaten: Stammdaten, settings: Settings):
    """Die INFO 'Preis 0,00' des Artikel-Matchings bleibt, die Prüfung meldet die Abweichung zusätzlich rot."""
    roh = _roh()
    roh.positionen[0].preis = Decimal("0.00")
    paket = _paket(stammdaten, roh)
    info = Pruefhinweis(
        code=Pruefcode.PREIS_ABWEICHUNG,
        schweregrad=Schweregrad.INFO,
        text="Preis 0,00 im Dokument",
        position_laufnr=1,
    )
    paket.positionen[0].hinweise.append(info)
    pruefe(paket, stammdaten, settings)
    pruefe(paket, stammdaten, settings)
    grade = sorted(
        h.schweregrad.value for h in paket.positionen[0].hinweise if h.code == Pruefcode.PREIS_ABWEICHUNG
    )
    assert grade == ["info", "warnung"]
    warnung = next(h for h in paket.positionen[0].hinweise if h.schweregrad == Schweregrad.WARNUNG)
    assert warnung.details["abweichung_prozent"] == "100,0" and warnung.details["preis_dokument"] == "0,00"


def test_menge_fehlt(stammdaten: Stammdaten, settings: Settings):
    roh = _roh()
    roh.positionen[0].menge = None
    paket = _paket(stammdaten, roh)
    assert paket.positionen[0].menge == Decimal(0)
    pruefe(paket, stammdaten, settings)
    hinweis = _hinweis(paket, Pruefcode.MENGE_FEHLT, 1)
    assert hinweis.schweregrad == Schweregrad.FEHLER
    assert paket.blockiert is True
    # übersprungene Position mit Menge 0 wird nicht bemängelt
    assert not [h for h in paket.positionen[2].hinweise if h.code == Pruefcode.MENGE_FEHLT]


def test_keine_buchbare_position(stammdaten: Stammdaten, settings: Settings):
    roh = _roh()
    paket = _paket(stammdaten, roh, positionen=[_uebersprungen(p) for p in roh.positionen])
    pruefe(paket, stammdaten, settings)
    hinweis = _hinweis(paket, Pruefcode.MENGE_FEHLT)
    assert hinweis.schweregrad == Schweregrad.FEHLER and "Keine buchbare Position" in hinweis.text


def test_ek_und_vk_fehlen(stammdaten: Stammdaten, settings: Settings):
    roh = _roh()
    ohne_preise = stammdaten.artikel_nach_nummer["1643"].model_copy(
        update={"ek_preis": None, "vk_preis_1": None}
    )
    paket = _paket(stammdaten, roh)
    paket.positionen[0] = _position(roh.positionen[0], ohne_preise)
    pruefe(paket, stammdaten, settings)
    assert _codes(paket.positionen[0].hinweise) == [Pruefcode.EK_FEHLT, Pruefcode.VK_FEHLT]
    assert all(h.schweregrad == Schweregrad.FEHLER for h in paket.positionen[0].hinweise)
    assert _hinweis(paket, Pruefcode.EK_FEHLT, 1).details["artikelnummer"] == "1643"
    # ohne Einkauf (Weinstrasse Agentur) ist der EK egal
    paket.einkauf_erforderlich = False
    pruefe(paket, stammdaten, settings)
    assert _codes(paket.positionen[0].hinweise) == [Pruefcode.VK_FEHLT]


def test_position_ohne_artikel(stammdaten: Stammdaten, settings: Settings):
    roh = _roh()
    paket = _paket(stammdaten, roh)
    unsicher = PositionsZuordnung(
        roh=roh.positionen[0],
        status=MatchStatus.UNSICHER,
        quelle="fuzzy",
        menge=Decimal(60),
        preis_dokument=Decimal("6.90"),
        kandidaten=[
            ArtikelKandidat(artikel=stammdaten.artikel_nach_nummer["1643"], score=88.0, grund="Name 88")
        ],
        hinweise=[
            Pruefhinweis(
                code=Pruefcode.ARTIKEL_UNSICHER,
                schweregrad=Schweregrad.WARNUNG,
                text="unsicher",
                position_laufnr=1,
            )
        ],
    )
    kein_treffer = PositionsZuordnung(
        roh=roh.positionen[1],
        status=MatchStatus.KEIN_TREFFER,
        menge=Decimal(24),
        preis_dokument=Decimal("6.90"),
        hinweise=[
            Pruefhinweis(
                code=Pruefcode.KEIN_ARTIKEL,
                schweregrad=Schweregrad.FEHLER,
                text="kein Artikel",
                position_laufnr=2,
            )
        ],
    )
    paket.positionen = [unsicher, kein_treffer, paket.positionen[2]]
    pruefe(paket, stammdaten, settings)
    # UNSICHER: Matching-Warnung bleibt, Prüfung blockiert über VK_FEHLT
    assert _codes(unsicher.hinweise) == [Pruefcode.ARTIKEL_UNSICHER, Pruefcode.VK_FEHLT]
    assert unsicher.hinweise[1].schweregrad == Schweregrad.FEHLER
    assert unsicher.hinweise[1].details["status"] == "unsicher"
    # KEIN_TREFFER blockiert bereits über KEIN_ARTIKEL; kein zusätzliches Rauschen
    assert _codes(kein_treffer.hinweise) == [Pruefcode.KEIN_ARTIKEL]
    assert paket.blockiert is True


# ---------------------------------------------------------------------------
# Kunde und Lieferant
# ---------------------------------------------------------------------------


def test_kein_kunde(stammdaten: Stammdaten, settings: Settings):
    paket = _paket(stammdaten, kunde=KundeZuordnung())
    pruefe(paket, stammdaten, settings)
    hinweis = _hinweis(paket, Pruefcode.KEIN_KUNDE)
    assert hinweis.schweregrad == Schweregrad.FEHLER
    assert "Weinmarkt Kirchhellen" in hinweis.text
    assert Pruefcode.KUNDE_UNSICHER not in _codes(paket.hinweise)
    assert paket.belegfelder.kunde is None
    assert "kunde" in [h.details["feld"] for h in paket.hinweise if h.code == Pruefcode.BELEGFELD_FEHLT]


def test_kunde_unsicher(stammdaten: Stammdaten, settings: Settings):
    kandidaten = [
        KundeKandidat(kunde=stammdaten.kunden_nach_nummer["2100"], score=100.0, grund="Name 100"),
        KundeKandidat(kunde=stammdaten.kunden_nach_nummer["2300"], score=96.0, grund="Name 90, Ort passt"),
    ]
    paket = _paket(
        stammdaten, kunde=KundeZuordnung(status=MatchStatus.UNSICHER, quelle="fuzzy", kandidaten=kandidaten)
    )
    pruefe(paket, stammdaten, settings)
    assert _codes(paket.hinweise)[:2] == [Pruefcode.KUNDE_UNSICHER, Pruefcode.KEIN_KUNDE]
    unsicher = _hinweis(paket, Pruefcode.KUNDE_UNSICHER)
    assert unsicher.schweregrad == Schweregrad.WARNUNG
    assert "2100" in unsicher.details["kandidaten"] and "2300" in unsicher.details["kandidaten"]
    assert "Kandidaten wählen" in _hinweis(paket, Pruefcode.KEIN_KUNDE).text
    # Anwender wählt -> erneute Prüfung ohne beide Hinweise
    paket.kunde = KundeZuordnung(
        kunde=stammdaten.kunden_nach_nummer["2300"], status=MatchStatus.MANUELL, quelle="manuell"
    )
    pruefe(paket, stammdaten, settings)
    assert Pruefcode.KUNDE_UNSICHER not in _codes(paket.hinweise)
    assert Pruefcode.KEIN_KUNDE not in _codes(paket.hinweise)
    assert paket.belegfelder.kunde == "Weinhandlung Müller-Lüdenscheid"


def test_kein_lieferant_nur_bei_einkauf(stammdaten: Stammdaten, settings: Settings):
    paket = _paket(stammdaten, lieferant=LieferantZuordnung(), belegfelder=Belegfelder(weingut="Cide"))
    pruefe(paket, stammdaten, settings)
    assert _hinweis(paket, Pruefcode.KEIN_LIEFERANT).schweregrad == Schweregrad.FEHLER
    assert paket.blockiert is True
    paket.einkauf_erforderlich = False
    pruefe(paket, stammdaten, settings)
    assert Pruefcode.KEIN_LIEFERANT not in _codes(paket.hinweise)
    assert paket.blockiert is False


def test_lieferant_unsicher(stammdaten: Stammdaten, settings: Settings):
    kandidaten = [
        LieferantKandidat(
            lieferant=stammdaten.lieferanten_nach_nummer["72"],
            score=50.0,
            grund="Positionen gehören zu mehreren Lieferanten",
        ),
        LieferantKandidat(
            lieferant=stammdaten.lieferanten_nach_nummer["138"],
            score=50.0,
            grund="Positionen gehören zu mehreren Lieferanten",
        ),
    ]
    paket = _paket(
        stammdaten,
        lieferant=LieferantZuordnung(status=MatchStatus.UNSICHER, quelle="positionen", kandidaten=kandidaten),
    )
    pruefe(paket, stammdaten, settings)
    unsicher = _hinweis(paket, Pruefcode.LIEFERANT_UNSICHER)
    assert unsicher.schweregrad == Schweregrad.WARNUNG
    assert "72" in unsicher.details["kandidaten"] and "138" in unsicher.details["kandidaten"]
    assert "mehreren Lieferanten" in unsicher.text
    assert _hinweis(paket, Pruefcode.KEIN_LIEFERANT).schweregrad == Schweregrad.FEHLER


# ---------------------------------------------------------------------------
# Firma
# ---------------------------------------------------------------------------


def test_firma_unklar_fehler(stammdaten: Stammdaten, settings: Settings):
    warnung = Pruefhinweis(
        code=Pruefcode.FIRMA_UNKLAR,
        schweregrad=Schweregrad.WARNUNG,
        text="mehrdeutig",
        details={"kandidaten": "wik_leh, woe_leh"},
    )
    paket = _paket(stammdaten, firma_id=None, firma_quelle=None, hinweise=[warnung])
    pruefe(paket, None, settings)
    pruefe(paket, None, settings)
    firma_hinweise = [h for h in paket.hinweise if h.code == Pruefcode.FIRMA_UNKLAR]
    assert [h.schweregrad for h in firma_hinweise] == [Schweregrad.WARNUNG, Schweregrad.FEHLER]
    assert "wik_leh, woe_leh" in firma_hinweise[1].text
    assert paket.blockiert is True


def test_firma_unbekannt_und_fremde_zuordnungen(stammdaten: Stammdaten, settings: Settings):
    paket = _paket(stammdaten, firma_id="gibtsnicht")
    pruefe(paket, stammdaten, settings)
    assert "nicht konfiguriert" in _hinweis(paket, Pruefcode.FIRMA_UNKLAR).text

    paket = _paket(stammdaten, firma_id="wik_fh_seh")
    pruefe(paket, stammdaten, settings)
    hinweis = _hinweis(paket, Pruefcode.FIRMA_UNKLAR)
    assert hinweis.schweregrad == Schweregrad.FEHLER
    assert "Kunde 3441" in hinweis.text and "Lieferant 72" in hinweis.text and "Artikel 1643" in hinweis.text


# ---------------------------------------------------------------------------
# Summenvermerke
# ---------------------------------------------------------------------------


def test_summenvermerke(stammdaten: Stammdaten, settings: Settings):
    roh = _roh(
        summenvermerke=[
            Summenvermerk(wert=Decimal(84), einheit="Fl", rohtext="Gesamt 84 Fl."),
            Summenvermerk(wert=Decimal(97), einheit="R", rohtext="97 R", konfidenz=0.9),
            Summenvermerk(wert=Decimal(85), einheit=None, rohtext="85"),
            Summenvermerk(wert=Decimal(2), einheit="Kt", rohtext="2 Kt"),
            Summenvermerk(wert=Decimal(90), einheit="Xyz", rohtext="90 Xyz"),
        ]
    )
    paket = pruefe(_paket(stammdaten, roh), stammdaten, settings)
    summen = [h for h in paket.hinweise if h.code == Pruefcode.SUMME_ABWEICHUNG]
    assert [(h.details["vermerk"], h.schweregrad) for h in summen] == [
        ("97 R", Schweregrad.WARNUNG),
        ("85", Schweregrad.INFO),
        ("2 Kt", Schweregrad.INFO),
        ("90 Xyz", Schweregrad.WARNUNG),
    ]
    assert summen[0].details["gesamtmenge"] == "84" and summen[0].details["wert"] == "97"
    assert "übersprungener Positionen" in summen[1].text and summen[1].details["dokumentmenge"] == "85"
    assert "nicht vergleichbar" in summen[2].text
    assert "als Flaschen angenommen" in summen[3].text
    assert paket.blockiert is False


# ---------------------------------------------------------------------------
# Dubletten
# ---------------------------------------------------------------------------


def test_normalisiere_belegnummer():
    assert normalisiere_belegnummer(" b - 4711 ") == "B4711"
    assert (
        normalisiere_belegnummer("B-4711")
        == normalisiere_belegnummer("B 4711")
        == normalisiere_belegnummer("b4711")
    )
    assert normalisiere_belegnummer("000123") == "123"
    assert normalisiere_belegnummer(None) == ""
    assert normalisiere_belegnummer("AB 2026 0101") == "AB20260101"


def test_dublette_kundenbestellnummer(stammdaten: Stammdaten, settings: Settings):
    paket = _paket(stammdaten, _roh(bestellnummer="B-4711"))
    pruefe(paket, stammdaten, settings)
    hinweis = _hinweis(paket, Pruefcode.DUBLETTE)
    assert hinweis.schweregrad == Schweregrad.FEHLER
    assert "AB-2026-0101" in hinweis.text and "B-4711" in hinweis.text
    assert hinweis.details["belegnummer"] == "AB-2026-0101"
    assert hinweis.details["kundenbestellnummer"] == "B-4711"
    assert hinweis.details["partnernummer"] == "3441"
    assert paket.blockiert is True


def test_dublette_normalisiert(stammdaten: Stammdaten, settings: Settings):
    paket = _paket(stammdaten, _roh(bestellnummer="b 4711"))
    assert len(pruefe_dubletten(paket, stammdaten)) == 1
    paket = _paket(stammdaten, _roh(bestellnummer="B-4712"))
    assert pruefe_dubletten(paket, stammdaten) == []


def test_dublette_anderer_partner_ist_keine(stammdaten: Stammdaten, settings: Settings):
    kunde = KundeZuordnung(
        kunde=stammdaten.kunden_nach_nummer["1392"], status=MatchStatus.SICHER, quelle="alias"
    )
    paket = _paket(stammdaten, _roh(bestellnummer="B-4711"), kunde=kunde)
    assert pruefe_dubletten(paket, stammdaten) == []
    # Belegnummer des Dokuments gegen Lexware-Belegnummer, Partner 1392 passt
    paket = _paket(stammdaten, _roh(belegnummer="ab-2026-0102", bestellnummer=None), kunde=kunde)
    hinweise = pruefe_dubletten(paket, stammdaten)
    assert len(hinweise) == 1 and hinweise[0].details["belegnummer"] == "AB-2026-0102"
    assert "Belegnummer 'ab-2026-0102'" in hinweise[0].text


def test_dublette_ohne_zugeordneten_kunden_und_ohne_partner(stammdaten: Stammdaten, settings: Settings):
    paket = _paket(stammdaten, _roh(bestellnummer="B-4711"), kunde=KundeZuordnung())
    hinweise = pruefe_dubletten(paket, stammdaten)
    assert len(hinweise) == 1 and "noch nicht zugeordnet" in hinweise[0].text
    ohne_partner = Stammdaten(
        "wik_leh",
        belege=[
            Beleg(
                firma_id="wik_leh",
                belegart=Belegart.AUFTRAGSBESTAETIGUNG,
                belegnummer="AB-1",
                kundenbestellnummer="B-4711",
            )
        ],
    )
    paket = _paket(stammdaten, _roh(bestellnummer="B-4711"))  # Kunde 3441
    hinweise = pruefe_dubletten(paket, ohne_partner)
    assert len(hinweise) == 1 and "nicht angegeben" in hinweise[0].text


def test_dublette_bestellung_vergleicht_lieferant(stammdaten: Stammdaten, settings: Settings):
    paket = _paket(stammdaten, _roh(belegnummer="BE-2026-0055", bestellnummer=None))  # Lieferant 72
    hinweise = pruefe_dubletten(paket, stammdaten)
    assert len(hinweise) == 1 and hinweise[0].details["belegart"] == "BE"
    anderer = LieferantZuordnung(
        lieferant=stammdaten.lieferanten_nach_nummer["87"], status=MatchStatus.SICHER
    )
    paket = _paket(stammdaten, _roh(belegnummer="BE-2026-0055", bestellnummer=None), lieferant=anderer)
    assert pruefe_dubletten(paket, stammdaten) == []


def test_pruefe_dubletten_eintragen_und_ohne_stammdaten(stammdaten: Stammdaten, settings: Settings):
    paket = _paket(stammdaten, _roh(bestellnummer="B-4711"))
    assert pruefe_dubletten(paket, None) == []
    assert pruefe_dubletten(paket, Stammdaten("wik_leh")) == []
    assert paket.hinweise == []
    pruefe_dubletten(paket, stammdaten, eintragen=True)
    pruefe_dubletten(paket, stammdaten, eintragen=True)
    assert _codes(paket.hinweise) == [Pruefcode.DUBLETTE]
    paket.roh.bestellnummer = "B-4712"
    assert pruefe_dubletten(paket, stammdaten, eintragen=True) == []
    assert paket.hinweise == []


def test_dublette_ohne_nummern_im_dokument(stammdaten: Stammdaten, settings: Settings, caplog):
    caplog.set_level(logging.INFO, logger="auftragsautomat.matching.pruefung")
    paket = _paket(stammdaten, _roh(belegnummer=None, bestellnummer=None))
    assert pruefe_dubletten(paket, stammdaten) == []
    assert "weder Belegnummer noch Bestellnummer" in caplog.text


# ---------------------------------------------------------------------------
# Idempotenz und Zusammenfassung
# ---------------------------------------------------------------------------


def test_idempotenz(stammdaten: Stammdaten, settings: Settings):
    roh = _roh(
        bestellnummer="B-4711",
        summenvermerke=[Summenvermerk(wert=Decimal(97), einheit="R", rohtext="97 R")],
        hinweise=[
            Pruefhinweis(code=Pruefcode.FELD_FEHLT, schweregrad=Schweregrad.WARNUNG, text="Datum fehlt")
        ],
    )
    roh.positionen[1].preis = Decimal("6.50")
    paket = _paket(stammdaten, roh, kunde=KundeZuordnung(), lieferant=LieferantZuordnung())
    pruefe(paket, stammdaten, settings)
    erste = _signatur(paket)
    assert Pruefcode.FELD_FEHLT in _codes(paket.alle_hinweise)
    assert Pruefcode.NULLPREIS_OHNE_ARTIKEL in _codes(paket.alle_hinweise)
    assert {
        Pruefcode.DUBLETTE,
        Pruefcode.SUMME_ABWEICHUNG,
        Pruefcode.PREIS_ABWEICHUNG,
        Pruefcode.KEIN_KUNDE,
    } <= set(_codes(paket.alle_hinweise))
    for _ in range(3):
        pruefe(paket, stammdaten, settings)
    assert _signatur(paket) == erste
    assert len(roh.hinweise) == 1  # Extraktionshinweise unangetastet


def test_entferne_pruefhinweise(stammdaten: Stammdaten, settings: Settings):
    paket = pruefe(
        _paket(stammdaten, _roh(bestellnummer="B-4711"), kunde=KundeZuordnung()), stammdaten, settings
    )
    anzahl = sum(1 for h in paket.alle_hinweise if ist_pruefhinweis(h))
    assert anzahl >= 3
    assert entferne_pruefhinweise(paket) == anzahl
    assert _codes(paket.alle_hinweise) == [Pruefcode.NULLPREIS_OHNE_ARTIKEL]
    assert entferne_pruefhinweise(paket) == 0


def test_ist_pruefhinweis():
    def h(code: Pruefcode, grad: Schweregrad) -> Pruefhinweis:
        return Pruefhinweis(code=code, schweregrad=grad, text="")

    assert ist_pruefhinweis(h(Pruefcode.PREIS_ABWEICHUNG, Schweregrad.WARNUNG))
    assert not ist_pruefhinweis(h(Pruefcode.PREIS_ABWEICHUNG, Schweregrad.INFO))
    assert ist_pruefhinweis(h(Pruefcode.FIRMA_UNKLAR, Schweregrad.FEHLER))
    assert not ist_pruefhinweis(h(Pruefcode.FIRMA_UNKLAR, Schweregrad.WARNUNG))
    assert not ist_pruefhinweis(h(Pruefcode.KEIN_ARTIKEL, Schweregrad.FEHLER))
    assert not ist_pruefhinweis(h(Pruefcode.NULLPREIS_OHNE_ARTIKEL, Schweregrad.WARNUNG))
    assert Pruefcode.KUNDE_UNSICHER in PRUEF_CODES and Pruefcode.LIEFERANT_UNSICHER in PRUEF_CODES


def test_zusammenfassung(stammdaten: Stammdaten, settings: Settings):
    roh = _roh(bestellnummer="B-4711")
    roh.positionen[1].preis = Decimal("6.50")
    paket = pruefe(_paket(stammdaten, roh), stammdaten, settings)
    text = zusammenfassung_hinweise(paket)
    zeilen = text.splitlines()
    assert zeilen[0].startswith("Belegpaket kirchhellen.pdf – Firma wik_leh (alias_kunde)")
    assert "Kunde: 3441 Hegmann [sicher]" in zeilen[1] and "Lieferant: 72 Cide srl [sicher]" in zeilen[1]
    assert "Liefertag 15.09.2026" in zeilen[1]
    assert "Gesamtmenge 84 Fl." in zeilen[2] and "Netto VK 579,60 €" in zeilen[2]
    assert "spedition=Spedition Weinlogistik" in zeilen[3]
    assert "Status: BLOCKIERT – 1 Fehler, 2 Warnungen" in zeilen[4]
    assert "[FEHLER ] DUBLETTE" in zeilen[5]
    assert any("[WARNUNG] PREIS_ABWEICHUNG Pos. 2:" in z for z in zeilen)
    # freigabefähig ohne Fehler
    paket = pruefe(_paket(stammdaten), stammdaten, settings)
    assert "Status: freigabefähig, bitte Warnungen prüfen" in zusammenfassung_hinweise(paket)
    paket.positionen[2].hinweise.clear()
    assert "Status: freigabefähig –" in zusammenfassung_hinweise(paket)
    paket.roh.liefertag = None
    paket.kunde = KundeZuordnung()
    assert "Liefertag -" in zusammenfassung_hinweise(
        paket
    ) and "('Weinmarkt Kirchhellen')" in zusammenfassung_hinweise(paket)


def test_paket_json_roundtrip_nach_pruefung(stammdaten: Stammdaten, settings: Settings):
    paket = pruefe(_paket(stammdaten, _roh(bestellnummer="B-4711")), stammdaten, settings)
    kopie = Belegpaket.model_validate_json(paket.model_dump_json())
    assert _signatur(kopie) == _signatur(paket)
    assert kopie.blockiert is True
    assert isinstance(kopie.kunde.kunde, Kunde) and isinstance(kopie.lieferant.lieferant, Lieferant)


# ---------------------------------------------------------------------------
# Mehrere Weingüter in einer Kundenbestellung (Geschäftsregel 2: Lieferant = Weingut)
# ---------------------------------------------------------------------------


def _alle(paket: Belegpaket, code: Pruefcode) -> list[Pruefhinweis]:
    return [h for h in paket.alle_hinweise if h.code == code]


def _paket_zwei_weingueter(stammdaten: Stammdaten, haupt: str = "72") -> Belegpaket:
    roh = _roh(
        positionen=[
            RohPosition(laufnr=1, bezeichnung="Gloria Secco Frizzante", menge=Decimal(60), preis=Decimal("6.90")),
            RohPosition(laufnr=2, bezeichnung="Colosi Nero d'Avola 2022", menge=Decimal(12), preis=Decimal("8.40")),
            RohPosition(laufnr=3, bezeichnung="DAA Neptun", menge=Decimal(1), preis=Decimal("0")),
        ]
    )
    gloria = stammdaten.artikel_nach_nummer["1643"]
    nero = stammdaten.artikel_nach_nummer["2001"]
    return Belegpaket(
        roh=roh,
        firma_id="wik_leh",
        kunde=KundeZuordnung(kunde=stammdaten.kunden_nach_nummer["3441"], status=MatchStatus.SICHER, quelle="alias"),
        lieferant=LieferantZuordnung(
            lieferant=stammdaten.lieferanten_nach_nummer[haupt], status=MatchStatus.SICHER, quelle="positionen"
        ),
        positionen=[
            _position(roh.positionen[0], gloria),
            _position(roh.positionen[1], nero, quelle="fuzzy"),
            _uebersprungen(roh.positionen[2]),
        ],
    )


def test_gruppiere_nach_lieferant(stammdaten: Stammdaten):
    paket = _paket_zwei_weingueter(stammdaten)
    gruppen = gruppiere_nach_lieferant(paket, stammdaten)
    assert [(g.lieferant.lieferantennummer, g.laufnummern, g.haupt) for g in gruppen] == [
        ("72", [1], True),
        ("87", [2], False),
    ]
    assert gruppen[1].beschreibung == "87 Cantine Colosi"
    assert gruppiere_nach_lieferant(paket, None) == []  # ohne Stammdaten keine Aufteilung
    paket.lieferant = LieferantZuordnung()
    assert gruppiere_nach_lieferant(paket, stammdaten) == []  # ohne Hauptlieferant keine Aufteilung


def test_lieferant_der_position_ueber_warengruppe(stammdaten: Stammdaten):
    salina = stammdaten.artikel_nach_nummer["2005"].model_copy(update={"lieferantennummer": None})
    position = _position(RohPosition(laufnr=1, bezeichnung="Salina", menge=Decimal(6)), salina)
    lieferant = lieferant_der_position(position, stammdaten)
    assert lieferant is not None and lieferant.lieferantennummer == "87"  # Warengruppe 'Colosi' = Name2
    ohne = position.model_copy(update={"artikel": None})
    assert lieferant_der_position(ohne, stammdaten) is None


def test_mehrere_lieferanten_warnung_und_weingut_vorbelegung(stammdaten: Stammdaten, settings: Settings):
    paket = pruefe(_paket_zwei_weingueter(stammdaten), stammdaten, settings)
    hinweise = _alle(paket, Pruefcode.MEHRERE_LIEFERANTEN)
    assert len(hinweise) == 1 and hinweise[0].schweregrad == Schweregrad.WARNUNG
    assert hinweise[0].details == {"hauptlieferant": "72", "weitere": "87", "positionen": "72:1;87:2"}
    assert "87 Cantine Colosi (Pos. 2)" in hinweise[0].text
    assert paket.belegfelder.weingut == "Cide srl / Cantine Colosi"
    assert paket.blockiert is False
    assert Pruefcode.MEHRERE_LIEFERANTEN in PRUEF_CODES

    # Idempotenz und Erneuerung der Vorbelegung, Anwendertext bleibt
    pruefe(paket, stammdaten, settings)
    assert len(_alle(paket, Pruefcode.MEHRERE_LIEFERANTEN)) == 1
    paket.belegfelder.weingut = "Cide srl"  # alte Vorbelegung (nur Hauptlieferant) wird erneuert
    pruefe(paket, stammdaten, settings)
    assert paket.belegfelder.weingut == "Cide srl / Cantine Colosi"
    paket.belegfelder.weingut = "Villa Armellina + Colosi"  # Anwendertext bleibt
    pruefe(paket, stammdaten, settings)
    assert paket.belegfelder.weingut == "Villa Armellina + Colosi"


def test_mehrere_lieferanten_ohne_position_beim_hauptlieferanten_blockiert(stammdaten: Stammdaten, settings: Settings):
    paket = pruefe(_paket_zwei_weingueter(stammdaten, haupt="138"), stammdaten, settings)
    hinweise = _alle(paket, Pruefcode.MEHRERE_LIEFERANTEN)
    assert len(hinweise) == 1 and hinweise[0].schweregrad == Schweregrad.FEHLER
    assert "Keine Position gehört zum zugeordneten Lieferanten 138 Bollina srl" in hinweise[0].text
    assert paket.blockiert is True
    assert paket.belegfelder.weingut == "Bollina srl / Cide srl / Cantine Colosi"


def test_mehrere_lieferanten_nicht_ohne_einkauf(stammdaten: Stammdaten, settings: Settings):
    paket = _paket_zwei_weingueter(stammdaten)
    paket.einkauf_erforderlich = False
    pruefe(paket, stammdaten, settings)
    assert Pruefcode.MEHRERE_LIEFERANTEN not in _codes(paket.hinweise)
