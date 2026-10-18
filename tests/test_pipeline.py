"""Tests für matching.pipeline (erstelle_belegpaket, pruefe_erneut, setze_firma) mit den Fixtures.

Die RohBestellungen werden direkt gebaut (unabhängig von der PDF-Extraktion).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from auftragsautomat.config import Settings
from auftragsautomat.matching.aliase import AliasTabellen
from auftragsautomat.matching.artikel import ordne_manuell
from auftragsautomat.matching.partner import GRUND_LIEFERANTEN_KONFLIKT
from auftragsautomat.matching.pipeline import (
    _nicht_schlechter,
    erstelle_belegpaket,
    pruefe_erneut,
    setze_firma,
)
from auftragsautomat.matching.pruefung import zusammenfassung_hinweise
from auftragsautomat.models import (
    Artikel,
    Belegfelder,
    Belegpaket,
    Eingangstyp,
    Extraktionsmethode,
    Kunde,
    Lieferant,
    LieferantZuordnung,
    MatchStatus,
    PositionsZuordnung,
    Pruefcode,
    RohBestellung,
    RohPosition,
    Schweregrad,
    Summenvermerk,
)
from auftragsautomat.stammdaten.basis import Stammdaten
from auftragsautomat.stammdaten.laden import lade_stammdaten


@pytest.fixture()
def aliase(settings: Settings) -> AliasTabellen:
    return AliasTabellen.lade(settings.alias_ordner)


def _roh(settings: Settings, datei: str | Path = "bestellung.pdf", **felder) -> RohBestellung:
    quelldatei = Path(datei) if Path(datei).is_absolute() else settings.eingang_ordner / datei
    felder.setdefault("eingangstyp", Eingangstyp.KUNDENBESTELLUNG_PDF)
    felder.setdefault("extraktionsmethode", Extraktionsmethode.PDFPLUMBER)
    return RohBestellung(quelldatei=quelldatei, **felder)


def _pos(laufnr: int, bezeichnung: str, menge: int | None, preis: str | None, **felder) -> RohPosition:
    return RohPosition(
        laufnr=laufnr,
        bezeichnung=bezeichnung,
        menge=Decimal(menge) if menge is not None else None,
        preis=Decimal(preis) if preis is not None else None,
        **felder,
    )


def _kirchhellen(settings: Settings, bestellnummer: str = "B-4712", **felder) -> RohBestellung:
    felder.setdefault("kunde_text", "Weinmarkt Kirchhellen GmbH & Co. KG")
    felder.setdefault("belegnummer", "2026-0917")
    felder.setdefault("datum", date(2026, 9, 10))
    felder.setdefault("liefertag", date(2026, 9, 15))
    felder.setdefault(
        "positionen",
        [
            _pos(1, "Villa Armellina Gloria Secco Frizzante", 60, "6.90", hinweis="11+1"),
            _pos(2, "Villa Armellina Rosato Secco Frizzante", 24, "6.90"),
            _pos(3, "DAA Neptun", 1, "0.00"),
        ],
    )
    return _roh(settings, "kirchhellen.pdf", bestellnummer=bestellnummer, **felder)


def _menrath(settings: Settings) -> RohBestellung:
    return _roh(
        settings,
        "menrath.pdf",
        kunde_text="Weinhaus Menrath",
        bestellnummer="M-2026-09",
        liefertag=date(2026, 9, 20),
        positionen=[
            _pos(1, "Bollina Gavi di Gavi DOCG 2023", 12, "12.00"),
            _pos(2, "Villa Armellina Rosato Secco Frizzante Magnum 1,5 l", 6, "14.90"),
        ],
    )


def _codes(paket: Belegpaket) -> list[Pruefcode]:
    return [h.code for h in paket.alle_hinweise]


def _hinweise(paket: Belegpaket, code: Pruefcode):
    return [h for h in paket.alle_hinweise if h.code == code]


def _signatur(paket: Belegpaket) -> list[tuple[str, str, int | None]]:
    return [(h.code.value, h.schweregrad.value, h.position_laufnr) for h in paket.alle_hinweise]


# ---------------------------------------------------------------------------
# Kirchhellen (wik_leh über Kunden-Alias)
# ---------------------------------------------------------------------------


def test_kirchhellen_komplett(settings: Settings):
    paket = erstelle_belegpaket(_kirchhellen(settings), settings)
    assert (paket.firma_id, paket.firma_quelle) == ("wik_leh", "alias_kunde")
    assert paket.einkauf_erforderlich is True
    assert (paket.kunde.status, paket.kunde.kunde.kundennummer, paket.kunde.quelle) == (
        MatchStatus.SICHER,
        "3441",
        "alias",
    )
    assert (paket.lieferant.status, paket.lieferant.lieferant.lieferantennummer, paket.lieferant.quelle) == (
        MatchStatus.SICHER,
        "72",
        "positionen",
    )
    p1, p2, p3 = paket.positionen
    assert (p1.status, p1.artikel.artikelnummer, p1.quelle, p1.menge) == (
        MatchStatus.SICHER,
        "1643",
        "alias",
        Decimal(60),
    )
    assert (p1.ek_preis, p1.vk_preis, p1.preis_dokument) == (
        Decimal("4.20"),
        Decimal("6.90"),
        Decimal("6.90"),
    )
    assert (p2.status, p2.artikel.artikelnummer, p2.menge) == (MatchStatus.SICHER, "957", Decimal(24))
    assert p3.status == MatchStatus.UEBERSPRUNGEN and p3.artikel is None and p3.uebersprungen
    assert [h.code for h in p3.hinweise] == [Pruefcode.NULLPREIS_OHNE_ARTIKEL]
    assert paket.belegfelder == Belegfelder(
        spedition="Spedition Weinlogistik", weingut="Cide srl", shipment="15.09.2026", kunde="Hegmann"
    )
    assert paket.gesamtmenge == Decimal(84)
    assert paket.summe_netto_ek == Decimal("352.80") and paket.summe_netto_vk == Decimal("579.60")
    assert Pruefcode.DUBLETTE not in _codes(paket)
    assert paket.hinweise == []  # keine Hinweise auf Belegebene
    assert _codes(paket) == [Pruefcode.NULLPREIS_OHNE_ARTIKEL]
    assert paket.blockiert is False
    assert paket.freigegeben is False


def test_kirchhellen_dublette_b4711(settings: Settings):
    paket = erstelle_belegpaket(_kirchhellen(settings, bestellnummer="B-4711"), settings)
    dubletten = _hinweise(paket, Pruefcode.DUBLETTE)
    assert len(dubletten) == 1
    assert dubletten[0].schweregrad == Schweregrad.FEHLER
    assert "AB-2026-0101" in dubletten[0].text and dubletten[0].details["partnernummer"] == "3441"
    assert paket.blockiert is True
    assert "BLOCKIERT" in zusammenfassung_hinweise(paket)


def test_summenvermerk_97_gegen_gesamtmenge(settings: Settings):
    roh = _kirchhellen(
        settings,
        summenvermerke=[
            Summenvermerk(wert=Decimal(97), einheit="R", rohtext="97 R"),
            Summenvermerk(wert=Decimal(84), einheit="Fl", rohtext="Gesamt 84 Fl."),
        ],
    )
    paket = erstelle_belegpaket(roh, settings)
    summen = _hinweise(paket, Pruefcode.SUMME_ABWEICHUNG)
    assert len(summen) == 1
    assert summen[0].schweregrad == Schweregrad.WARNUNG
    assert summen[0].details == {"vermerk": "97 R", "wert": "97", "einheit": "R", "gesamtmenge": "84"}
    assert paket.blockiert is False


def test_blockiert_logik(settings: Settings):
    paket = erstelle_belegpaket(_kirchhellen(settings), settings)
    assert paket.blockiert is False  # nur Warnung (DAA Neptun)
    paket = erstelle_belegpaket(_kirchhellen(settings, bestellnummer="B-4711"), settings)
    assert paket.blockiert is True
    paket.hinweise = [h for h in paket.hinweise if h.schweregrad != Schweregrad.FEHLER]
    assert paket.blockiert is False  # berechnet, nicht gespeichert
    paket.positionen[0].hinweise.append(
        _hinweise(
            erstelle_belegpaket(_kirchhellen(settings, bestellnummer="B-4711"), settings), Pruefcode.DUBLETTE
        )[0]
    )
    assert paket.blockiert is True


# ---------------------------------------------------------------------------
# Menrath (Preisabweichung, Magnum, Lieferantenkonflikt, Korrektur in der Freigabe)
# ---------------------------------------------------------------------------


def test_menrath_preisabweichung_und_magnum(settings: Settings):
    paket = erstelle_belegpaket(_menrath(settings), settings)
    assert (paket.firma_id, paket.firma_quelle) == ("wik_leh", "alias_kunde")
    assert paket.kunde.kunde.kundennummer == "1392"

    gavi, magnum = paket.positionen
    assert (gavi.status, gavi.artikel.artikelnummer) == (MatchStatus.SICHER, "3001")
    abweichung = [h for h in gavi.hinweise if h.code == Pruefcode.PREIS_ABWEICHUNG]
    assert len(abweichung) == 1 and abweichung[0].schweregrad == Schweregrad.WARNUNG
    assert abweichung[0].details["vk_preis"] == "12,50" and abweichung[0].details["preis_dokument"] == "12,00"
    assert (
        abweichung[0].details["abweichung_eur"] == "0,50"
        and abweichung[0].details["abweichung_prozent"] == "4,0"
    )
    assert gavi.preis_abweichung == Decimal("0.50")

    assert magnum.status == MatchStatus.UNSICHER and magnum.artikel is None
    k958 = next(k for k in magnum.kandidaten if k.artikel.artikelnummer == "958")
    assert k958.manuell_erforderlich is True
    assert [h.code for h in magnum.hinweise] == [Pruefcode.VARIANTE_MANUELL, Pruefcode.VK_FEHLT]
    assert magnum.hinweise[0].schweregrad == Schweregrad.WARNUNG
    assert magnum.hinweise[1].schweregrad == Schweregrad.FEHLER

    # Bollina-Position und Villa-Armellina-Position -> zwei Weingüter: Hauptlieferant Bollina (12 Flaschen
    # gegen 6), Cide bleibt Kandidat; die Magnum-Position hat noch keinen Artikel und bildet keine Gruppe
    assert paket.lieferant.status == MatchStatus.SICHER and paket.lieferant.lieferant is not None
    assert paket.lieferant.lieferant.lieferantennummer == "138"
    assert {k.lieferant.lieferantennummer for k in paket.lieferant.kandidaten} == {"138", "72"}
    assert paket.lieferant.kandidaten[0].grund.startswith(GRUND_LIEFERANTEN_KONFLIKT)
    assert Pruefcode.LIEFERANT_UNSICHER not in _codes(paket)
    assert Pruefcode.KEIN_LIEFERANT not in _codes(paket)
    assert Pruefcode.MEHRERE_LIEFERANTEN not in _codes(paket)
    assert paket.belegfelder.weingut == "Bollina srl"
    assert paket.blockiert is True  # Magnum nur manuell (VK_FEHLT) und Belegfeld Shipment leer


def test_menrath_korrektur_und_pruefe_erneut(settings: Settings):
    paket = erstelle_belegpaket(_menrath(settings), settings)
    stammdaten_958 = next(
        k.artikel for k in paket.positionen[1].kandidaten if k.artikel.artikelnummer == "958"
    )

    # 1. Anwender bestätigt die Magnum-Variante
    paket.positionen[1] = ordne_manuell(paket.positionen[1], stammdaten_958)
    ergebnis = pruefe_erneut(paket, settings)
    assert ergebnis is paket
    magnum = paket.positionen[1]
    assert (magnum.status, magnum.artikel.artikelnummer, magnum.quelle) == (
        MatchStatus.MANUELL,
        "958",
        "manuell",
    )
    assert (magnum.ek_preis, magnum.vk_preis, magnum.preis_dokument) == (
        Decimal("9.50"),
        Decimal("14.90"),
        Decimal("14.90"),
    )
    assert magnum.hinweise == []
    assert Pruefcode.VARIANTE_MANUELL not in _codes(paket)
    # Magnum (Cide) neben Gavi (Bollina): zwei Weingüter -> Warnung, je Weingut eine Einkauf-Bestellung
    assert Pruefcode.KEIN_LIEFERANT not in _codes(paket)
    mehrere = _hinweise(paket, Pruefcode.MEHRERE_LIEFERANTEN)
    assert len(mehrere) == 1 and mehrere[0].schweregrad == Schweregrad.WARNUNG
    assert mehrere[0].details == {"hauptlieferant": "138", "weitere": "72", "positionen": "138:1;72:2"}
    assert len(_hinweise(paket, Pruefcode.PREIS_ABWEICHUNG)) == 1  # nicht doppelt

    # 2. Anwender bestätigt den Lieferanten (Bollina bleibt Hauptlieferant)
    bollina = next(k.lieferant for k in paket.lieferant.kandidaten if k.lieferant.lieferantennummer == "138")
    paket.lieferant = LieferantZuordnung(lieferant=bollina, status=MatchStatus.MANUELL, quelle="manuell")
    pruefe_erneut(paket, settings)
    assert Pruefcode.KEIN_LIEFERANT not in _codes(paket)
    assert Pruefcode.LIEFERANT_UNSICHER not in _codes(paket)
    assert paket.lieferant.status == MatchStatus.MANUELL  # bleibt erhalten
    assert paket.belegfelder.weingut == "Bollina srl / Cide srl"
    assert paket.blockiert is False
    assert [h.code for h in paket.alle_hinweise] == [Pruefcode.MEHRERE_LIEFERANTEN, Pruefcode.PREIS_ABWEICHUNG]

    # 2b. Wählt der Anwender ein Weingut, zu dem keine Position gehört, blockiert die Prüfung
    stamm = lade_stammdaten(settings.firma("wik_leh"))
    paket.lieferant = LieferantZuordnung(
        lieferant=stamm.lieferanten_nach_nummer["150"], status=MatchStatus.MANUELL, quelle="manuell"
    )
    paket.belegfelder.weingut = None
    pruefe_erneut(paket, settings)
    fehler = _hinweise(paket, Pruefcode.MEHRERE_LIEFERANTEN)
    assert len(fehler) == 1 and fehler[0].schweregrad == Schweregrad.FEHLER
    assert "Keine Position gehört zum zugeordneten Lieferanten 150" in fehler[0].text
    assert paket.blockiert is True
    paket.lieferant = LieferantZuordnung(lieferant=bollina, status=MatchStatus.MANUELL, quelle="manuell")
    paket.belegfelder.weingut = None
    pruefe_erneut(paket, settings)
    assert paket.blockiert is False

    # 3. Idempotenz der erneuten Prüfung
    vorher = _signatur(paket)
    pruefe_erneut(paket, settings)
    pruefe_erneut(paket, settings)
    assert _signatur(paket) == vorher


def test_pruefe_erneut_aktualisiert_preise_und_mengen(settings: Settings):
    paket = erstelle_belegpaket(_kirchhellen(settings), settings)
    gloria = paket.positionen[0]
    gloria.menge = Decimal(72)  # Anwender korrigiert die Menge
    gloria.vk_preis = Decimal("1.00")  # veralteter Wert, muss aus dem Stamm zurückkommen
    pruefe_erneut(paket, settings)
    assert gloria.menge == Decimal(72)
    assert gloria.vk_preis == Decimal("6.90") and gloria.ek_preis == Decimal("4.20")
    assert paket.summe_netto_vk == Decimal("662.40")
    assert paket.blockiert is False


def test_pruefe_erneut_mit_neuem_alias_loest_position(settings: Settings, tmp_path: Path):
    """Nach dem Speichern eines Alias in der Freigabe greift der Alias bei der erneuten Prüfung."""
    from auftragsautomat.matching.aliase import speichere_alias_artikel

    roh = _kirchhellen(settings, positionen=[_pos(1, "Hausmarke Rot", 12, "7.90")])
    paket = erstelle_belegpaket(roh, settings)
    assert paket.positionen[0].status == MatchStatus.KEIN_TREFFER
    assert Pruefcode.KEIN_ARTIKEL in _codes(paket)

    alias_ordner = tmp_path / "aliase"
    speichere_alias_artikel(alias_ordner, "4001", "wik_leh", "Hausmarke Rot")
    settings.alias_ordner = alias_ordner
    pruefe_erneut(paket, settings)
    assert (paket.positionen[0].status, paket.positionen[0].artikel.artikelnummer) == (
        MatchStatus.SICHER,
        "4001",
    )
    assert Pruefcode.KEIN_ARTIKEL not in _codes(paket)


def test_pruefe_erneut_ohne_firma(settings: Settings):
    roh = _roh(
        settings, "unbekannt.pdf", kunde_text="Château Inconnu", positionen=[_pos(1, "Rouge", 6, "9.00")]
    )
    paket = erstelle_belegpaket(roh, settings)
    assert paket.firma_id is None
    pruefe_erneut(paket, settings)
    assert paket.firma_id is None and Pruefcode.FIRMA_UNKLAR in _codes(paket)


# ---------------------------------------------------------------------------
# Zweiter Matching-Durchlauf mit Lieferant
# ---------------------------------------------------------------------------


def _stamm_mit_namensgleichen_artikeln() -> Stammdaten:
    def artikel(nummer: str, lieferant: str, warengruppe: str) -> Artikel:
        return Artikel(
            firma_id="wik_leh",
            artikelnummer=nummer,
            bezeichnung="Grillo Sicilia DOC 2023 0,75 l",
            warengruppe=warengruppe,
            lieferantennummer=lieferant,
            volumen_cl=Decimal("75"),
            jahrgang=2023,
            ek_preis=Decimal("5.00"),
            vk_preis_1=Decimal("8.00"),
        )

    return Stammdaten(
        "wik_leh",
        artikel=[artikel("A", "87", "Colosi"), artikel("B", "138", "Bollina")],
        kunden=[
            Kunde(
                firma_id="wik_leh", kundennummer="3441", name="Hegmann", name2="Weinmarkt Bottrop-Kirchhellen"
            )
        ],
        lieferanten=[
            Lieferant(firma_id="wik_leh", lieferantennummer="87", name="Cantine Colosi", name2="Colosi"),
            Lieferant(firma_id="wik_leh", lieferantennummer="138", name="Bollina srl"),
        ],
    )


def test_zweiter_durchlauf_loest_mehrdeutigkeit(settings: Settings, aliase: AliasTabellen):
    stammdaten = _stamm_mit_namensgleichen_artikeln()
    roh = _kirchhellen(
        settings, lieferant_text="Cantine Colosi", positionen=[_pos(1, "Grillo Sicilia DOC 2023", 12, "8.00")]
    )
    paket = erstelle_belegpaket(roh, settings, stammdaten=stammdaten, aliase=aliase)
    assert (paket.firma_id, paket.firma_quelle) == ("wik_leh", "manuell")
    assert (paket.lieferant.status, paket.lieferant.lieferant.lieferantennummer, paket.lieferant.quelle) == (
        MatchStatus.SICHER,
        "87",
        "alias",
    )
    position = paket.positionen[0]
    assert (position.status, position.artikel.artikelnummer, position.quelle) == (
        MatchStatus.SICHER,
        "A",
        "fuzzy",
    )
    assert "Lieferant passt" in position.kandidaten[0].grund
    assert paket.blockiert is False

    # ohne Lieferant im Text bleibt es mehrdeutig (und ohne Artikel gibt es keinen Lieferanten)
    roh = _kirchhellen(settings, positionen=[_pos(1, "Grillo Sicilia DOC 2023", 12, "8.00")])
    paket = erstelle_belegpaket(roh, settings, stammdaten=stammdaten, aliase=aliase)
    assert paket.positionen[0].status == MatchStatus.UNSICHER
    assert {k.artikel.artikelnummer for k in paket.positionen[0].kandidaten} == {"A", "B"}
    assert paket.lieferant.status == MatchStatus.KEIN_TREFFER and paket.lieferant.lieferant is None
    assert paket.blockiert is True


def test_nicht_schlechter():
    roh = _pos(1, "Rouge", 12, "9.00")
    alt = PositionsZuordnung(
        roh=roh, status=MatchStatus.UNSICHER, menge=Decimal(10)
    )  # Menge vom Anwender geändert
    schlechter = PositionsZuordnung(roh=roh, status=MatchStatus.KEIN_TREFFER, menge=Decimal(12))
    assert _nicht_schlechter(alt, schlechter) is alt
    gleich = PositionsZuordnung(roh=roh, status=MatchStatus.UNSICHER, menge=Decimal(12))
    assert _nicht_schlechter(alt, gleich) is gleich and gleich.menge == Decimal(10)
    besser = PositionsZuordnung(roh=roh, status=MatchStatus.SICHER, menge=Decimal(12))
    assert _nicht_schlechter(alt, besser) is besser and besser.menge == Decimal(10)
    unveraendert = PositionsZuordnung(roh=roh, status=MatchStatus.UNSICHER, menge=Decimal(12))
    assert _nicht_schlechter(unveraendert, besser).menge == Decimal(
        10
    )  # besser trägt schon 10 aus dem Schritt davor
    frisch = PositionsZuordnung(roh=roh, status=MatchStatus.SICHER, menge=Decimal(12))
    assert _nicht_schlechter(unveraendert, frisch).menge == Decimal(12)


# ---------------------------------------------------------------------------
# Firma über Unterordner, Weinstrasse Agentur, manuelle Firma
# ---------------------------------------------------------------------------


def test_firma_per_unterordner_wik_fh_seh(settings: Settings):
    roh = _roh(
        settings,
        settings.eingang_ordner / "wik_fh_seh" / "seeblick.pdf",
        kunde_text="Hotel Seeblick",
        kunde_adresse="Seestraße 1\n23570 Lübeck",
        bestellnummer="HS-77",
        liefertag=date(2026, 10, 1),
        positionen=[_pos(1, "Colosi Nero d'Avola Sicilia DOC 2022", 12, "9.20")],
    )
    paket = erstelle_belegpaket(roh, settings)
    assert (paket.firma_id, paket.firma_quelle) == ("wik_fh_seh", "eingang_ordner")
    assert (paket.kunde.status, paket.kunde.kunde.kundennummer, paket.kunde.quelle) == (
        MatchStatus.SICHER,
        "7001",
        "fuzzy",
    )
    assert (paket.positionen[0].status, paket.positionen[0].artikel.artikelnummer) == (
        MatchStatus.SICHER,
        "5001",
    )
    assert paket.lieferant.lieferant.lieferantennummer == "87"
    assert paket.belegfelder == Belegfelder(
        spedition="Spedition Weinlogistik",
        weingut="Cantine Colosi",
        shipment="01.10.2026",
        kunde="Hotel Seeblick",
    )
    assert paket.alle_hinweise == []
    assert paket.blockiert is False


def test_weinstrasse_agentur_ohne_einkauf(settings: Settings):
    roh = _roh(
        settings,
        settings.eingang_ordner / "weinstrasse_agentur" / "provision.pdf",
        kunde_text="Weingut Sonnenhang",
        bestellnummer="P-1",
        liefertag=date(2026, 10, 5),
        positionen=[_pos(1, "Agentur Provision Weinvermittlung", 1, "150.00")],
    )
    paket = erstelle_belegpaket(roh, settings)
    assert (paket.firma_id, paket.firma_quelle) == ("weinstrasse_agentur", "eingang_ordner")
    assert paket.einkauf_erforderlich is False
    assert paket.kunde.kunde.kundennummer == "9001"
    assert paket.positionen[0].artikel.artikelnummer == "8001"
    assert paket.lieferant.lieferant is None
    assert Pruefcode.KEIN_LIEFERANT not in _codes(paket)
    assert Pruefcode.EK_FEHLT not in _codes(paket)
    assert paket.belegfelder.spedition == "Selbstabholung"
    # einzig offener Punkt: das Belegfeld Weingut (kein Lieferant bekannt)
    assert [(h.code, h.details.get("feld")) for h in paket.hinweise] == [
        (Pruefcode.BELEGFELD_FEHLT, "weingut")
    ]
    paket.belegfelder.weingut = "Weingut Sonnenhang"
    pruefe_erneut(paket, settings)
    assert paket.alle_hinweise == [] and paket.blockiert is False


def test_firma_manuell(settings: Settings):
    roh = _roh(
        settings,
        "irgendwo.pdf",
        kunde_text="Weinmarkt Kirchhellen",
        positionen=[_pos(1, "Gloria Secco Frizzante", 6, "6.90")],
    )
    paket = erstelle_belegpaket(roh, settings, firma_id="wik_leh")
    assert (paket.firma_id, paket.firma_quelle) == ("wik_leh", "manuell")
    assert paket.positionen[0].artikel.artikelnummer == "1643"
    with pytest.raises(KeyError, match="gibtsnicht"):
        erstelle_belegpaket(roh, settings, firma_id="gibtsnicht")
    with pytest.raises(ValueError, match="wik_leh"):
        erstelle_belegpaket(roh, settings, firma_id="woe_leh", stammdaten=Stammdaten("wik_leh"))


def test_ohne_firma_und_setze_firma(settings: Settings):
    roh = _roh(
        settings,
        "unbekannt.pdf",
        kunde_text="Château Inconnu",
        positionen=[
            _pos(1, "Gloria Secco Frizzante", 6, "6.90"),
            _pos(2, "Gestrichen", 3, "1.00", durchgestrichen=True),
        ],
    )
    paket = erstelle_belegpaket(roh, settings)
    assert (paket.firma_id, paket.firma_quelle) == (None, None)
    fehler = _hinweise(paket, Pruefcode.FIRMA_UNKLAR)
    assert len(fehler) == 1 and fehler[0].schweregrad == Schweregrad.FEHLER
    assert paket.blockiert is True
    assert [p.status for p in paket.positionen] == [MatchStatus.KEIN_TREFFER, MatchStatus.UEBERSPRUNGEN]
    assert paket.positionen[0].menge == Decimal(6) and paket.positionen[0].preis_dokument == Decimal("6.90")
    assert paket.gesamtmenge == Decimal(6)
    paket.lieferschein_erstellen = True

    neu = setze_firma(paket, "wik_leh", settings)
    assert neu is not paket
    assert (neu.firma_id, neu.firma_quelle) == ("wik_leh", "manuell")
    assert neu.lieferschein_erstellen is True and neu.freigegeben is False
    assert Pruefcode.FIRMA_UNKLAR not in _codes(neu)
    assert (neu.positionen[0].status, neu.positionen[0].artikel.artikelnummer) == (MatchStatus.SICHER, "1643")
    assert neu.positionen[1].status == MatchStatus.UEBERSPRUNGEN and neu.positionen[1].menge == Decimal(0)
    assert Pruefcode.KEIN_KUNDE in _codes(neu)  # Château Inconnu gibt es auch in wik_leh nicht
    with pytest.raises(KeyError):
        setze_firma(paket, "gibtsnicht", settings)


def test_setze_firma_verwirft_firma_warnung(settings: Settings):
    roh = _kirchhellen(settings)
    roh.quelldatei = settings.eingang_ordner / "wik_fh_seh" / "kirchhellen.pdf"
    paket = erstelle_belegpaket(roh, settings)
    assert (paket.firma_id, paket.firma_quelle) == ("wik_fh_seh", "eingang_ordner")
    warnungen = [h for h in paket.hinweise if h.code == Pruefcode.FIRMA_UNKLAR]
    assert [h.schweregrad for h in warnungen] == [Schweregrad.WARNUNG]
    assert warnungen[0].details["firma_alias"] == "wik_leh"
    pruefe_erneut(paket, settings)
    assert (
        len([h for h in paket.hinweise if h.code == Pruefcode.FIRMA_UNKLAR]) == 1
    )  # Warnung überlebt die Prüfung
    neu = setze_firma(paket, "wik_leh", settings)
    assert Pruefcode.FIRMA_UNKLAR not in _codes(neu)
    assert neu.kunde.kunde.kundennummer == "3441" and neu.blockiert is False


def test_durchgestrichene_position_zaehlt_nicht(settings: Settings):
    roh = _kirchhellen(settings)
    roh.positionen[1].durchgestrichen = True
    paket = erstelle_belegpaket(roh, settings)
    rosato = paket.positionen[1]
    assert rosato.status == MatchStatus.UEBERSPRUNGEN and rosato.menge == Decimal(0)
    assert rosato.artikel is not None and rosato.artikel.artikelnummer == "957"  # bleibt sichtbar
    assert paket.gesamtmenge == Decimal(60)
    assert paket.summe_netto_vk == Decimal("414.00")
    assert Pruefcode.MENGE_FEHLT not in _codes(paket)


def test_belegpaket_json_roundtrip(settings: Settings):
    paket = erstelle_belegpaket(_menrath(settings), settings)
    kopie = Belegpaket.model_validate_json(paket.model_dump_json())
    assert _signatur(kopie) == _signatur(paket)
    assert kopie.positionen[1].kandidaten[0].manuell_erforderlich is True
    assert kopie.blockiert is True
