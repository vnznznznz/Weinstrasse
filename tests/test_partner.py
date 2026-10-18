"""Tests für matching.partner (KundenMatcher, LieferantenMatcher) mit den wik_leh-Fixtures."""

from __future__ import annotations

from decimal import Decimal

import pytest

from auftragsautomat.config import MatchingKonfig, Settings
from auftragsautomat.matching.aliase import AliasTabellen
from auftragsautomat.matching.partner import (
    GRUND_LIEFERANTEN_KONFLIKT,
    KundenMatcher,
    LieferantenMatcher,
    finde_kundennummer,
)
from auftragsautomat.models import Artikel, Kunde, Lieferant, MatchStatus, PositionsZuordnung, RohPosition
from auftragsautomat.stammdaten.basis import Stammdaten
from auftragsautomat.stammdaten.laden import lade_stammdaten


@pytest.fixture()
def stammdaten(settings: Settings) -> Stammdaten:
    return lade_stammdaten(settings.firma("wik_leh"))


@pytest.fixture()
def aliase(settings: Settings) -> AliasTabellen:
    return AliasTabellen.lade(settings.alias_ordner)


@pytest.fixture()
def kunden_matcher(stammdaten: Stammdaten, aliase: AliasTabellen, settings: Settings) -> KundenMatcher:
    return KundenMatcher(stammdaten, aliase, settings.matching)


@pytest.fixture()
def lieferanten_matcher(
    stammdaten: Stammdaten, aliase: AliasTabellen, settings: Settings
) -> LieferantenMatcher:
    return LieferantenMatcher(stammdaten, aliase, settings.matching)


def _position(laufnr: int, bezeichnung: str, menge: int = 6) -> RohPosition:
    return RohPosition(laufnr=laufnr, bezeichnung=bezeichnung, menge=Decimal(menge))


# ---------------------------------------------------------------------------
# Kunden
# ---------------------------------------------------------------------------


def test_kundennummer_im_text():
    assert finde_kundennummer("Kd-Nr. 3441") == ["3441"]
    assert finde_kundennummer("Kundennummer: 3441, Kd.-Nr 2200") == ["3441", "2200"]
    assert finde_kundennummer("Ihre Kundennummer bei uns: K-123") == ["K-123"]
    assert finde_kundennummer("Bahnhofstraße 12, 47051 Duisburg") == []
    assert finde_kundennummer(None) == []


def test_kunde_alias_kirchhellen(kunden_matcher: KundenMatcher):
    z = kunden_matcher.finde("Weinmarkt Kirchhellen GmbH & Co. KG")
    assert z.status == MatchStatus.SICHER
    assert z.kunde is not None and z.kunde.kundennummer == "3441"
    assert z.quelle == "alias"
    assert len(z.kandidaten) == 1
    assert z.kandidaten[0].score == 100.0
    assert "Weinmarkt Kirchhellen" in z.kandidaten[0].grund


def test_kunde_alias_menrath(kunden_matcher: KundenMatcher):
    z = kunden_matcher.finde("Weinhaus Menrath")
    assert (z.status, z.kunde.kundennummer, z.quelle) == (MatchStatus.SICHER, "1392", "alias")


def test_kunde_alias_in_adresse(kunden_matcher: KundenMatcher):
    z = kunden_matcher.finde(None, adresse="Hegmann\nHauptstraße 1\n46244 Bottrop")
    assert (z.status, z.kunde.kundennummer, z.quelle) == (MatchStatus.SICHER, "3441", "alias")


def test_kunde_alias_nur_wenn_firma_passt(aliase: AliasTabellen, settings: Settings):
    """Die Aliase gelten für wik_leh; im Stamm von wik_fh_seh dürfen sie nicht greifen."""
    stammdaten = lade_stammdaten(settings.firma("wik_fh_seh"))
    matcher = KundenMatcher(stammdaten, aliase, settings.matching)
    z = matcher.finde("Weinhaus Menrath")
    assert z.status == MatchStatus.KEIN_TREFFER
    assert z.kunde is None


def test_kunde_fuzzy_mueller_sicher(kunden_matcher: KundenMatcher):
    """Entscheidung: 2100 'Weinhandlung Müller' erhält 100, 2300 'Weinhandlung Müller-Lüdenscheid'
    rund 90; Abstand >= 5 -> SICHER 2100."""
    z = kunden_matcher.finde("Weinhandlung Mueller")
    assert z.status == MatchStatus.SICHER
    assert z.kunde.kundennummer == "2100"
    assert z.quelle == "fuzzy"
    nummern = [k.kunde.kundennummer for k in z.kandidaten]
    assert nummern[:2] == ["2100", "2300"]
    assert z.kandidaten[0].score == 100.0
    assert 85 <= z.kandidaten[1].score < 95


def test_kunde_fuzzy_mueller_luedenscheid(kunden_matcher: KundenMatcher):
    z = kunden_matcher.finde("Weinhandlung Müller-Lüdenscheid")
    assert (z.status, z.kunde.kundennummer) == (MatchStatus.SICHER, "2300")


def test_kunde_fuzzy_adresse_macht_unsicher(kunden_matcher: KundenMatcher):
    """Name sagt 2100, Adresse (PLZ/Ort) spricht für 2300 -> Abstand < 5 -> Anwender entscheidet."""
    z = kunden_matcher.finde("Weinhandlung Mueller", adresse="Kirchweg 9\n58507 Lüdenscheid")
    assert z.status == MatchStatus.UNSICHER
    assert z.kunde is None
    assert {k.kunde.kundennummer for k in z.kandidaten[:2]} == {"2100", "2300"}
    k2300 = next(k for k in z.kandidaten if k.kunde.kundennummer == "2300")
    assert "PLZ 58507" in k2300.grund and "Lüdenscheid" in k2300.grund


def test_kunde_fuzzy_adresse_bestaetigt(kunden_matcher: KundenMatcher):
    z = kunden_matcher.finde("Weinhandlung Müller GmbH", adresse="Bahnhofstraße 12\n47051 Duisburg")
    assert (z.status, z.kunde.kundennummer) == (MatchStatus.SICHER, "2100")
    assert "Duisburg" in z.kandidaten[0].grund


def test_kunde_mehrzeiliger_text(kunden_matcher: KundenMatcher):
    z = kunden_matcher.finde("Vinothek Schmidt GmbH\nAm Markt 7\n45127 Essen")
    assert (z.status, z.kunde.kundennummer) == (MatchStatus.SICHER, "2200")


def test_kunde_name2_fuzzy(
    kunden_matcher: KundenMatcher, aliase: AliasTabellen, settings: Settings, stammdaten: Stammdaten
):
    """Ohne Alias greift name2 'Weinmarkt Bottrop-Kirchhellen'."""
    matcher = KundenMatcher(stammdaten, AliasTabellen(), settings.matching)
    z = matcher.finde("Weinmarkt Bottrop-Kirchhellen")
    assert (z.status, z.kunde.kundennummer, z.quelle) == (MatchStatus.SICHER, "3441", "fuzzy")


def test_kunde_kundennummer_im_text(kunden_matcher: KundenMatcher):
    z = kunden_matcher.finde("Bestellung, Kd-Nr. 2200")
    assert (z.status, z.kunde.kundennummer, z.quelle) == (MatchStatus.SICHER, "2200", "kundennummer")


def test_kunde_kundennummer_parameter(kunden_matcher: KundenMatcher):
    z = kunden_matcher.finde("Irgendwer", kundennummer="0002200")
    assert (z.status, z.kunde.kundennummer, z.quelle) == (MatchStatus.SICHER, "2200", "kundennummer")
    z = kunden_matcher.finde("Irgendwer", kundennummer="99999")
    assert z.status == MatchStatus.KEIN_TREFFER


def test_kunde_kein_treffer(kunden_matcher: KundenMatcher):
    z = kunden_matcher.finde("Irgendwas Ganz Anderes")
    assert z.status == MatchStatus.KEIN_TREFFER
    assert z.kunde is None and z.kandidaten == [] and z.quelle is None
    assert kunden_matcher.finde(None).status == MatchStatus.KEIN_TREFFER
    assert kunden_matcher.finde("   ").status == MatchStatus.KEIN_TREFFER


def test_kunde_unsicher_unter_schwelle(stammdaten: Stammdaten, aliase: AliasTabellen):
    konfig = MatchingKonfig(schwelle_sicher=99.5, schwelle_kandidat=60)
    matcher = KundenMatcher(stammdaten, aliase, konfig)
    z = matcher.finde("Weinhandlung Mueller Duisburg")  # Zusatzwort: unter 99,5
    assert z.status == MatchStatus.UNSICHER
    assert z.kandidaten[0].kunde.kundennummer == "2100"
    assert len(z.kandidaten) <= 5


def test_kunde_inaktiv_nur_kandidat(stammdaten: Stammdaten, aliase: AliasTabellen, settings: Settings):
    kunden = [
        k.model_copy(update={"aktiv": False}) if k.kundennummer == "1392" else k for k in stammdaten.kunden
    ]
    daten = Stammdaten("wik_leh", stammdaten.artikel, kunden, stammdaten.lieferanten, stammdaten.belege)
    matcher = KundenMatcher(daten, aliase, settings.matching)
    z = matcher.finde("Weinhaus Menrath")  # Alias
    assert z.status == MatchStatus.UNSICHER
    assert z.kunde is None
    assert z.kandidaten[0].kunde.kundennummer == "1392"
    assert "inaktiv" in z.kandidaten[0].grund
    # Fuzzy ohne Alias ebenfalls nur Kandidat
    matcher = KundenMatcher(daten, AliasTabellen(), settings.matching)
    z = matcher.finde("Weinhaus Menrath")
    assert z.status == MatchStatus.UNSICHER
    assert z.kandidaten[0].kunde.kundennummer == "1392"


def test_kunde_alias_auf_unbekannte_nummer(
    stammdaten: Stammdaten, aliase: AliasTabellen, settings: Settings, caplog
):
    kunden = [k for k in stammdaten.kunden if k.kundennummer != "1392"]
    daten = Stammdaten("wik_leh", stammdaten.artikel, kunden, stammdaten.lieferanten, stammdaten.belege)
    matcher = KundenMatcher(daten, aliase, settings.matching)
    z = matcher.finde("Weinhaus Menrath")
    assert z.status != MatchStatus.SICHER  # Fuzzy liefert höchstens schwache Kandidaten
    assert z.kunde is None
    assert all(k.score < 70 for k in z.kandidaten)
    assert "fehlt" in caplog.text


def test_kunde_eindeutig_bei_inaktiver_dublette(aliase: AliasTabellen, settings: Settings):
    """Eine inaktive Dublette gleichen Namens verhindert die sichere Zuordnung nicht."""
    kunden = [
        Kunde(firma_id="wik_leh", kundennummer="1", name="Weinhaus Test", aktiv=False),
        Kunde(firma_id="wik_leh", kundennummer="2", name="Weinhaus Test GmbH", aktiv=True),
    ]
    matcher = KundenMatcher(Stammdaten("wik_leh", kunden=kunden), AliasTabellen(), settings.matching)
    z = matcher.finde("Weinhaus Test")
    assert (z.status, z.kunde.kundennummer) == (MatchStatus.SICHER, "2")
    assert [k.kunde.kundennummer for k in z.kandidaten] == ["2", "1"]


def test_kunde_zwei_gleich_gute_aktive(aliase: AliasTabellen, settings: Settings):
    kunden = [
        Kunde(firma_id="wik_leh", kundennummer="1", name="Weinhaus Test", ort="Köln"),
        Kunde(firma_id="wik_leh", kundennummer="2", name="Weinhaus Test", ort="Bonn"),
    ]
    matcher = KundenMatcher(Stammdaten("wik_leh", kunden=kunden), AliasTabellen(), settings.matching)
    z = matcher.finde("Weinhaus Test")
    assert z.status == MatchStatus.UNSICHER
    assert len(z.kandidaten) == 2
    # Adresse entscheidet
    z = matcher.finde("Weinhaus Test", adresse="Domstraße 1, 50667 Köln")
    assert (z.status, z.kunde.kundennummer) == (MatchStatus.SICHER, "1")


# ---------------------------------------------------------------------------
# Lieferanten
# ---------------------------------------------------------------------------


def test_lieferant_alias_text(lieferanten_matcher: LieferantenMatcher):
    z = lieferanten_matcher.finde("Cantine Colosi", [])
    assert (z.status, z.lieferant.lieferantennummer, z.quelle) == (MatchStatus.SICHER, "87", "alias")
    z = lieferanten_matcher.finde("Azienda Agricola Colosi, Messina")
    assert z.lieferant.lieferantennummer == "87"


def test_lieferant_aus_produktmarken(lieferanten_matcher: LieferantenMatcher):
    positionen = [
        _position(1, "Villa Armellina Gloria Secco Frizzante 0,75 l"),
        _position(2, "Villa Armellina Rosato Secco Frizzante"),
        _position(3, "DAA Neptun"),  # keine Marke -> keine Stimme
    ]
    z = lieferanten_matcher.finde(None, positionen)
    assert (z.status, z.lieferant.lieferantennummer, z.quelle) == (MatchStatus.SICHER, "72", "positionen")
    assert "Villa Armellina" in z.kandidaten[0].grund


def test_lieferant_konflikt_mehrere_lieferanten(lieferanten_matcher: LieferantenMatcher):
    positionen = [
        _position(1, "Villa Armellina Gloria Secco Frizzante"),
        _position(2, "Villa Armellina Rosato Secco Frizzante"),
        _position(3, "Colosi Nero d'Avola 2022"),
    ]
    z = lieferanten_matcher.finde(None, positionen)
    # Mehrere Weingüter: Hauptlieferant = die meisten Positionen (72 Cide), alle bleiben Kandidaten;
    # die Buchung legt je Weingut eine eigene Einkauf-Bestellung an (pruefung.gruppiere_nach_lieferant)
    assert (z.status, z.quelle) == (MatchStatus.SICHER, "positionen")
    assert z.lieferant is not None and z.lieferant.lieferantennummer == "72"
    assert [k.lieferant.lieferantennummer for k in z.kandidaten] == ["72", "87"]
    assert all(k.grund.startswith(GRUND_LIEFERANTEN_KONFLIKT) for k in z.kandidaten)
    assert z.kandidaten[0].score > z.kandidaten[1].score


def test_lieferant_konflikt_gleichstand_entscheidet_flaschenmenge(lieferanten_matcher: LieferantenMatcher):
    positionen = [
        _position(1, "Villa Armellina Gloria Secco Frizzante", menge=6),
        _position(2, "Colosi Nero d'Avola 2022", menge=36),
    ]
    z = lieferanten_matcher.finde(None, positionen)
    assert z.status == MatchStatus.SICHER and z.lieferant is not None
    assert z.lieferant.lieferantennummer == "87"  # gleich viele Positionen -> mehr Flaschen gewinnt
    assert [k.lieferant.lieferantennummer for k in z.kandidaten] == ["87", "72"]


def test_lieferant_konflikt_text_nennt_hauptlieferant(lieferanten_matcher: LieferantenMatcher, stammdaten: Stammdaten):
    """Lieferant 150 (Weingut Beispiel) hat keinen Alias: der Text wird nur per Fuzzy erkannt und
    bestimmt bei Positionen mehrerer Weingüter den Hauptlieferanten (Mehrheit der Positionen: Cide)."""
    riesling = stammdaten.artikel_nach_nummer["4001"]
    positionen = [
        _position(1, "Villa Armellina Gloria Secco Frizzante"),
        _position(2, "Villa Armellina Rosato Secco Frizzante"),
        PositionsZuordnung(roh=_position(3, "Riesling trocken 2023"), artikel=riesling, status=MatchStatus.SICHER),
    ]
    z = lieferanten_matcher.finde("Weingut Beispiel GmbH", positionen)
    assert z.status == MatchStatus.SICHER and z.lieferant is not None
    assert z.lieferant.lieferantennummer == "150"  # im Text genannt schlägt die Mehrheit der Positionen
    assert "im Text genannt" in next(k.grund for k in z.kandidaten if k.lieferant.lieferantennummer == "150")
    assert {k.lieferant.lieferantennummer for k in z.kandidaten} == {"72", "150"}


def test_lieferant_aus_zugeordneten_artikeln(lieferanten_matcher: LieferantenMatcher, stammdaten: Stammdaten):
    artikel = stammdaten.artikel_nach_nummer["4001"]  # Weingut Beispiel, Lieferant 150, keine Marke
    positionen = [
        PositionsZuordnung(
            roh=_position(1, "Riesling trocken 2023"), artikel=artikel, status=MatchStatus.SICHER
        ),
        PositionsZuordnung(roh=_position(2, "Werbemittel"), status=MatchStatus.UEBERSPRUNGEN),
    ]
    z = lieferanten_matcher.finde(None, positionen)
    assert (z.status, z.lieferant.lieferantennummer) == (MatchStatus.SICHER, "150")
    assert "Artikel 4001" in z.kandidaten[0].grund


def test_lieferant_aus_warengruppe(aliase: AliasTabellen, settings: Settings, stammdaten: Stammdaten):
    """Artikel ohne Lieferantennummer: Warengruppe 'Colosi' -> Alias -> 87."""
    artikel = Artikel(
        firma_id="wik_leh", artikelnummer="X1", bezeichnung="Grillo Sicilia", warengruppe="Colosi"
    )
    matcher = LieferantenMatcher(stammdaten, aliase, settings.matching)
    z = matcher.finde(None, [PositionsZuordnung(roh=_position(1, "Grillo Sicilia"), artikel=artikel)])
    assert (z.status, z.lieferant.lieferantennummer) == (MatchStatus.SICHER, "87")
    assert "Warengruppe 'Colosi'" in z.kandidaten[0].grund
    # Warengruppe gleich Lieferantenname (ohne Alias)
    artikel = Artikel(
        firma_id="wik_leh", artikelnummer="X2", bezeichnung="Riesling", warengruppe="Weingut Beispiel"
    )
    z = matcher.finde(None, [PositionsZuordnung(roh=_position(1, "Riesling"), artikel=artikel)])
    assert z.lieferant.lieferantennummer == "150"
    assert matcher.lieferant_fuer_warengruppe("Unbekannt") is None
    assert matcher.lieferant_fuer_warengruppe(None) is None


def test_lieferant_durchgestrichene_positionen_zaehlen_nicht(lieferanten_matcher: LieferantenMatcher):
    positionen = [
        _position(1, "Villa Armellina Gloria"),
        RohPosition(laufnr=2, bezeichnung="Colosi Grillo", durchgestrichen=True),
    ]
    z = lieferanten_matcher.finde(None, positionen)
    assert (z.status, z.lieferant.lieferantennummer) == (MatchStatus.SICHER, "72")


def test_lieferant_fuzzy_text(lieferanten_matcher: LieferantenMatcher):
    z = lieferanten_matcher.finde("Weingut Beispiel GmbH")
    assert (z.status, z.lieferant.lieferantennummer, z.quelle) == (MatchStatus.SICHER, "150", "fuzzy")


def test_lieferant_text_widerspricht_positionen(lieferanten_matcher: LieferantenMatcher):
    z = lieferanten_matcher.finde("Weingut Beispiel", [_position(1, "Villa Armellina Gloria")])
    assert z.status == MatchStatus.UNSICHER
    assert {k.lieferant.lieferantennummer for k in z.kandidaten} == {"72", "150"}


def test_lieferant_alias_schlaegt_positionen(lieferanten_matcher: LieferantenMatcher):
    z = lieferanten_matcher.finde("Cide srl", [_position(1, "Colosi Nero d'Avola")])
    assert (z.status, z.lieferant.lieferantennummer, z.quelle) == (MatchStatus.SICHER, "72", "alias")


def test_lieferant_kein_treffer(lieferanten_matcher: LieferantenMatcher):
    z = lieferanten_matcher.finde("Château Inconnu", [_position(1, "Unbekannter Wein")])
    assert z.status == MatchStatus.KEIN_TREFFER
    assert z.lieferant is None and z.kandidaten == [] and z.quelle is None
    assert lieferanten_matcher.finde(None, None).status == MatchStatus.KEIN_TREFFER


def test_lieferant_inaktiv_nur_kandidat(stammdaten: Stammdaten, aliase: AliasTabellen, settings: Settings):
    lieferanten = [
        lf.model_copy(update={"aktiv": False}) if lf.lieferantennummer == "72" else lf
        for lf in stammdaten.lieferanten
    ]
    daten = Stammdaten("wik_leh", stammdaten.artikel, stammdaten.kunden, lieferanten, stammdaten.belege)
    matcher = LieferantenMatcher(daten, aliase, settings.matching)
    z = matcher.finde("Cide srl")
    assert z.status == MatchStatus.UNSICHER and z.kandidaten[0].lieferant.lieferantennummer == "72"
    z = matcher.finde(None, [_position(1, "Villa Armellina Gloria")])
    assert z.status == MatchStatus.UNSICHER and "inaktiv" in z.kandidaten[0].grund


def test_lieferant_fuzzy_unsicher(settings: Settings):
    lieferanten = [
        Lieferant(firma_id="wik_leh", lieferantennummer="1", name="Weingut Sonne"),
        Lieferant(firma_id="wik_leh", lieferantennummer="2", name="Weingut Sonne Nord"),
        Lieferant(firma_id="wik_leh", lieferantennummer="3", name="Weingut Sonne Süd"),
    ]
    matcher = LieferantenMatcher(
        Stammdaten("wik_leh", lieferanten=lieferanten), AliasTabellen(), settings.matching
    )
    z = matcher.finde("Weingut Sonne Nord-Süd")  # Nord und Süd gleich gut
    assert z.status == MatchStatus.UNSICHER
    assert z.lieferant is None
    assert len(z.kandidaten) == 3
    assert {k.lieferant.lieferantennummer for k in z.kandidaten[:2]} == {"2", "3"}
    assert z.kandidaten == sorted(z.kandidaten, key=lambda k: -k.score)
