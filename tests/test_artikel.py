"""Tests für matching.artikel (ArtikelMatcher, ordne_manuell) mit den wik_leh-Fixtures."""

from __future__ import annotations

from decimal import Decimal

import pytest

from auftragsautomat.config import Settings
from auftragsautomat.matching.aliase import AliasEintrag, AliasTabellen, normalisiere_name
from auftragsautomat.matching.artikel import ArtikelMatcher, ordne_manuell
from auftragsautomat.models import (
    Artikel,
    Lieferant,
    MatchStatus,
    PositionsZuordnung,
    Pruefcode,
    RohPosition,
    Schweregrad,
)
from auftragsautomat.stammdaten.basis import Stammdaten
from auftragsautomat.stammdaten.laden import lade_stammdaten


@pytest.fixture()
def stammdaten(settings: Settings) -> Stammdaten:
    return lade_stammdaten(settings.firma("wik_leh"))


@pytest.fixture()
def aliase(settings: Settings) -> AliasTabellen:
    return AliasTabellen.lade(settings.alias_ordner)


@pytest.fixture()
def matcher(stammdaten: Stammdaten, aliase: AliasTabellen, settings: Settings) -> ArtikelMatcher:
    return ArtikelMatcher(stammdaten, aliase, settings.matching)


@pytest.fixture()
def cide(stammdaten: Stammdaten) -> Lieferant:
    return stammdaten.lieferanten_nach_nummer["72"]


@pytest.fixture()
def colosi(stammdaten: Stammdaten) -> Lieferant:
    return stammdaten.lieferanten_nach_nummer["87"]


def _roh(bezeichnung: str, **felder) -> RohPosition:
    felder.setdefault("laufnr", 1)
    felder.setdefault("menge", Decimal(12))
    return RohPosition(bezeichnung=bezeichnung, **felder)


def _codes(z: PositionsZuordnung) -> list[Pruefcode]:
    return [h.code for h in z.hinweise]


def _nummern(z: PositionsZuordnung) -> list[str]:
    return [k.artikel.artikelnummer for k in z.kandidaten]


def _kandidat(z: PositionsZuordnung, nummer: str):
    return next(k for k in z.kandidaten if k.artikel.artikelnummer == nummer)


# ---------------------------------------------------------------------------
# Alias / Artikelnummer
# ---------------------------------------------------------------------------


def test_alias_gloria_sicher(matcher: ArtikelMatcher, cide: Lieferant):
    z = matcher.finde(_roh("Villa Armellina Gloria Secco Frizzante", preis=Decimal("6.90")), cide)
    assert z.status == MatchStatus.SICHER
    assert z.artikel.artikelnummer == "1643"
    assert z.quelle == "alias"
    assert z.menge == Decimal(12)
    assert z.ek_preis == Decimal("4.20")
    assert z.vk_preis == Decimal("6.90")
    assert z.preis_dokument == Decimal("6.90")
    assert z.preis_abweichung == Decimal("0.00")
    assert z.hinweise == []
    assert _nummern(z) == ["1643"] and z.kandidaten[0].score == 100.0


def test_alias_mit_abweichendem_volumen_0375(matcher: ArtikelMatcher):
    """Alias zeigt auf 1643 (75 cl), Dokument nennt 0,375 -> 1644 als manueller Kandidat."""
    z = matcher.finde(_roh("Gloria Secco Frizzante 0,375"))
    assert z.status == MatchStatus.UNSICHER
    assert z.artikel is None
    assert z.ek_preis is None and z.vk_preis is None
    assert z.menge == Decimal(12)
    assert _nummern(z)[0] == "1644"
    k1644 = _kandidat(z, "1644")
    assert k1644.manuell_erforderlich is True
    assert _kandidat(z, "1643").manuell_erforderlich is False
    assert k1644.score > _kandidat(z, "1643").score
    assert _codes(z) == [Pruefcode.VARIANTE_MANUELL]
    assert z.hinweise[0].schweregrad == Schweregrad.WARNUNG
    assert z.hinweise[0].position_laufnr == 1
    assert "1644" in z.hinweise[0].text and "37,5 cl" in z.hinweise[0].text


def test_alias_mit_magnum(matcher: ArtikelMatcher):
    z = matcher.finde(_roh("Villa Armellina Rosato Secco Frizzante Magnum 1,5 l"))
    assert z.status == MatchStatus.UNSICHER
    assert _nummern(z)[0] == "958"
    assert _kandidat(z, "958").manuell_erforderlich is True
    assert Pruefcode.VARIANTE_MANUELL in _codes(z)
    assert "150 cl" in z.hinweise[0].text


def test_alias_direkt_auf_sondervariante(stammdaten: Stammdaten, settings: Settings):
    """Auch ein Alias direkt auf eine Sondervariante führt nie zu SICHER."""
    aliase = AliasTabellen(
        artikel=[AliasEintrag("1644", "wik_leh", "Gloria klein", normalisiere_name("Gloria klein"))]
    )
    matcher = ArtikelMatcher(stammdaten, aliase, settings.matching)
    z = matcher.finde(_roh("Gloria klein"))
    assert z.status == MatchStatus.UNSICHER
    assert z.quelle == "alias"
    assert _nummern(z) == ["1644"]
    assert z.kandidaten[0].manuell_erforderlich is True and z.kandidaten[0].score == 100.0
    assert _codes(z) == [Pruefcode.VARIANTE_MANUELL]


def test_alias_anderer_firma_wird_ignoriert(aliase: AliasTabellen, settings: Settings):
    stammdaten = lade_stammdaten(settings.firma("wik_fh_seh"))
    matcher = ArtikelMatcher(stammdaten, aliase, settings.matching)
    z = matcher.finde(_roh("Villa Armellina Gloria Secco Frizzante"))
    assert z.status == MatchStatus.KEIN_TREFFER
    assert _codes(z) == [Pruefcode.KEIN_ARTIKEL]


def test_alias_auf_inaktiven_artikel(stammdaten: Stammdaten, settings: Settings):
    aliase = AliasTabellen(
        artikel=[AliasEintrag("3003", "wik_leh", "Gavi alt", normalisiere_name("Gavi alt"))]
    )
    matcher = ArtikelMatcher(stammdaten, aliase, settings.matching)
    z = matcher.finde(_roh("Gavi alt"))
    assert z.status == MatchStatus.UNSICHER
    assert _nummern(z)[0] == "3003"
    assert "inaktiv" in z.kandidaten[0].grund
    assert Pruefcode.ARTIKEL_UNSICHER in _codes(z)


def test_artikelnummer_im_dokument(matcher: ArtikelMatcher):
    z = matcher.finde(_roh("Irgendein Wein", artikelnummer="2003"))
    assert (z.status, z.artikel.artikelnummer, z.quelle) == (MatchStatus.SICHER, "2003", "artikelnummer")
    assert z.vk_preis == Decimal("8.60")
    z = matcher.finde(_roh("Irgendein Wein", artikelnummer="002003"))
    assert z.artikel.artikelnummer == "2003"


def test_artikelnummer_sondervariante_nie_automatisch(matcher: ArtikelMatcher):
    z = matcher.finde(_roh("Gloria", artikelnummer="1644"))
    assert z.status == MatchStatus.UNSICHER
    assert _codes(z) == [Pruefcode.VARIANTE_MANUELL]
    assert z.kandidaten[0].manuell_erforderlich


def test_artikelnummer_unbekannt_dann_fuzzy(matcher: ArtikelMatcher, caplog):
    z = matcher.finde(_roh("Bollina Gavi di Gavi DOCG 2023", artikelnummer="99999"))
    assert (z.status, z.artikel.artikelnummer, z.quelle) == (MatchStatus.SICHER, "3001", "fuzzy")
    info = [h for h in z.hinweise if h.schweregrad == Schweregrad.INFO]
    assert len(info) == 1 and "99999" in info[0].text
    assert "unbekannt" in caplog.text


# ---------------------------------------------------------------------------
# Fuzzy
# ---------------------------------------------------------------------------


def test_fuzzy_jahrgang_entscheidet(matcher: ArtikelMatcher, colosi: Lieferant):
    z = matcher.finde(_roh("Colosi Nero d'Avola Sicilia DOC", jahrgang=2022, preis=Decimal("8.40")), colosi)
    assert (z.status, z.artikel.artikelnummer, z.quelle) == (MatchStatus.SICHER, "2001", "fuzzy")
    assert z.ek_preis == Decimal("5.10") and z.vk_preis == Decimal("8.40")
    assert _nummern(z)[:2] == ["2001", "2002"]
    assert _kandidat(z, "2004").manuell_erforderlich is True  # Holzkiste nur Kandidat
    assert z.hinweise == []


def test_fuzzy_jahrgang_aus_bezeichnung(matcher: ArtikelMatcher):
    z = matcher.finde(_roh("Colosi Nero d'Avola Sicilia DOC 2021"))
    assert (z.status, z.artikel.artikelnummer) == (MatchStatus.SICHER, "2002")


def test_fuzzy_ohne_jahrgang_unsicher(matcher: ArtikelMatcher):
    """2001 (2022) und 2002 (2021) sind gleich gut -> Anwender entscheidet."""
    z = matcher.finde(_roh("Colosi Nero d'Avola Sicilia DOC"))
    assert z.status == MatchStatus.UNSICHER
    assert z.artikel is None
    assert set(_nummern(z)[:2]) == {"2001", "2002"}
    assert _kandidat(z, "2001").score == _kandidat(z, "2002").score == 100.0
    assert _codes(z) == [Pruefcode.ARTIKEL_UNSICHER]
    assert z.hinweise[0].schweregrad == Schweregrad.WARNUNG
    assert "2001" in z.hinweise[0].text and "2002" in z.hinweise[0].text


def test_fuzzy_gavi_2023_standard_vor_variante(matcher: ArtikelMatcher):
    z = matcher.finde(_roh("Bollina Gavi di Gavi DOCG 2023", menge=Decimal(24)))
    assert (z.status, z.artikel.artikelnummer) == (MatchStatus.SICHER, "3001")
    assert z.menge == Decimal(24)
    k3002 = _kandidat(z, "3002")
    assert k3002.manuell_erforderlich is True
    assert k3002.score < z.kandidaten[0].score
    assert Pruefcode.VARIANTE_MANUELL not in _codes(z)


def test_fuzzy_inaktiver_artikel_nur_kandidat(matcher: ArtikelMatcher):
    z = matcher.finde(_roh("Gavi di Gavi 2022"))
    assert z.status == MatchStatus.UNSICHER
    assert z.artikel is None
    assert _nummern(z)[0] == "3003"
    assert "inaktiv" in _kandidat(z, "3003").grund
    assert Pruefcode.ARTIKEL_UNSICHER in _codes(z)


def test_fuzzy_holzkiste_angefordert(matcher: ArtikelMatcher):
    z = matcher.finde(_roh("Colosi Nero d'Avola 2022 Holzkiste", menge=Decimal(2)))
    assert z.status == MatchStatus.UNSICHER
    assert _nummern(z)[0] == "2004"
    assert _kandidat(z, "2004").manuell_erforderlich is True
    assert _codes(z) == [Pruefcode.VARIANTE_MANUELL]
    assert "Holzkiste" in z.hinweise[0].text


def test_fuzzy_volumen_aus_feld(matcher: ArtikelMatcher):
    z = matcher.finde(_roh("Villa Armellina Gloria Secco Frizzante", volumen_cl=Decimal("37.5")))
    assert z.status == MatchStatus.UNSICHER
    assert _nummern(z)[0] == "1644"
    assert Pruefcode.VARIANTE_MANUELL in _codes(z)


def test_fuzzy_kurzer_text_unsicher_mit_variantenhinweis(matcher: ArtikelMatcher):
    z = matcher.finde(_roh("Gloria", volumen_cl=Decimal("37.5")))
    assert z.status == MatchStatus.UNSICHER
    assert Pruefcode.ARTIKEL_UNSICHER in _codes(z)
    assert Pruefcode.VARIANTE_MANUELL in _codes(z)
    assert _kandidat(z, "1644").manuell_erforderlich


def test_fuzzy_lieferant_bonus(matcher: ArtikelMatcher, colosi: Lieferant, cide: Lieferant):
    mit = matcher.finde(_roh("Colosi Grillo Sicilia DOC 2023"), colosi)
    ohne = matcher.finde(_roh("Colosi Grillo Sicilia DOC 2023"))
    fremd = matcher.finde(_roh("Colosi Grillo Sicilia DOC 2023"), cide)
    assert mit.artikel.artikelnummer == ohne.artikel.artikelnummer == fremd.artikel.artikelnummer == "2003"
    assert mit.kandidaten[0].score >= ohne.kandidaten[0].score >= fremd.kandidaten[0].score
    assert "Lieferant passt" in mit.kandidaten[0].grund
    assert "passt nicht" in fremd.kandidaten[0].grund


def test_fuzzy_warengruppe_ohne_lieferantennummer(
    settings: Settings, aliase: AliasTabellen, colosi: Lieferant
):
    artikel = [
        Artikel(
            firma_id="wik_leh",
            artikelnummer="A",
            bezeichnung="Grillo Sicilia DOC 2023 0,75 l",
            warengruppe="Colosi",
        ),
        Artikel(
            firma_id="wik_leh",
            artikelnummer="B",
            bezeichnung="Grillo Sicilia DOC 2023 0,75 l",
            warengruppe="Bollina",
        ),
    ]
    daten = Stammdaten("wik_leh", artikel=artikel, lieferanten=[colosi])
    matcher = ArtikelMatcher(daten, aliase, settings.matching)
    z = matcher.finde(_roh("Grillo Sicilia DOC 2023"), colosi)
    assert (z.status, z.artikel.artikelnummer) == (MatchStatus.SICHER, "A")
    assert matcher.finde(_roh("Grillo Sicilia DOC 2023")).status == MatchStatus.UNSICHER


def test_direkt_lieferant_widerspricht_nur_hinweis(matcher: ArtikelMatcher, cide: Lieferant):
    z = matcher.finde(_roh("Colosi Nero d'Avola", artikelnummer="2001"), cide)
    assert (z.status, z.artikel.artikelnummer) == (MatchStatus.SICHER, "2001")
    assert [h.schweregrad for h in z.hinweise] == [Schweregrad.INFO]
    assert "Lieferant 72" in z.hinweise[0].text or "72" in z.hinweise[0].text


def test_menge_fehlt_wird_null(matcher: ArtikelMatcher):
    z = matcher.finde(_roh("Colosi Salina Bianco 2023", menge=None))
    assert (z.status, z.artikel.artikelnummer) == (MatchStatus.SICHER, "2005")
    assert z.menge == Decimal(0)
    assert z.netto_vk == Decimal(0)
    assert z.hinweise == []  # MENGE_FEHLT kommt aus der Prüfung


def test_kein_treffer(matcher: ArtikelMatcher):
    z = matcher.finde(_roh("Château Inconnu Rouge 2019", preis=Decimal("9.99")))
    assert z.status == MatchStatus.KEIN_TREFFER
    assert z.artikel is None and z.kandidaten == [] and z.quelle is None
    assert z.menge == Decimal(12) and z.preis_dokument == Decimal("9.99")
    assert _codes(z) == [Pruefcode.KEIN_ARTIKEL]
    assert z.hinweise[0].schweregrad == Schweregrad.FEHLER
    assert z.hinweise[0].position_laufnr == 1


def test_leere_bezeichnung(matcher: ArtikelMatcher):
    z = matcher.finde(_roh("   "))
    assert z.status == MatchStatus.KEIN_TREFFER


# ---------------------------------------------------------------------------
# Überspringen / 0,00 €
# ---------------------------------------------------------------------------


def test_ueberspringen_liste(matcher: ArtikelMatcher):
    z = matcher.finde(_roh("DAA Neptun", preis=Decimal("0.00"), menge=Decimal(1)))
    assert z.status == MatchStatus.UEBERSPRUNGEN
    assert z.uebersprungen is True
    assert z.artikel is None and z.quelle is None
    assert _codes(z) == [Pruefcode.NULLPREIS_OHNE_ARTIKEL]
    assert z.hinweise[0].schweregrad == Schweregrad.WARNUNG
    assert z.netto_ek == Decimal(0) and z.netto_vk == Decimal(0)


def test_ueberspringen_liste_auch_mit_preis(matcher: ArtikelMatcher):
    z = matcher.finde(_roh("DAA Neptun Aufsteller", preis=Decimal("5.00")))
    assert z.status == MatchStatus.UEBERSPRUNGEN


def test_nullpreis_ohne_treffer(matcher: ArtikelMatcher):
    z = matcher.finde(_roh("Werbeaufsteller Holz", preis=Decimal("0")))
    assert z.status == MatchStatus.UEBERSPRUNGEN
    assert _codes(z) == [Pruefcode.NULLPREIS_OHNE_ARTIKEL]
    assert "0,00" in z.hinweise[0].text


def test_nullpreis_mit_sicherem_treffer(matcher: ArtikelMatcher):
    z = matcher.finde(
        _roh("Villa Armellina Gloria Secco Frizzante", preis=Decimal("0.00"), menge=Decimal(60))
    )
    assert (z.status, z.artikel.artikelnummer) == (MatchStatus.SICHER, "1643")
    assert z.vk_preis == Decimal("6.90") and z.preis_dokument == Decimal("0.00")
    assert len(z.hinweise) == 1
    assert z.hinweise[0].schweregrad == Schweregrad.INFO
    assert "0,00" in z.hinweise[0].text


def test_nullpreis_mit_unsicherem_treffer(matcher: ArtikelMatcher):
    z = matcher.finde(_roh("Colosi Nero d'Avola Sicilia DOC", preis=Decimal("0")))
    assert z.status == MatchStatus.UNSICHER
    assert Pruefcode.ARTIKEL_UNSICHER in _codes(z)
    assert any(h.schweregrad == Schweregrad.INFO and "0,00" in h.text for h in z.hinweise)


# ---------------------------------------------------------------------------
# Manuelle Zuordnung (Freigabe-UI)
# ---------------------------------------------------------------------------


def test_ordne_manuell(matcher: ArtikelMatcher, stammdaten: Stammdaten):
    z = matcher.finde(_roh("Gloria Secco Frizzante 0,375", preis=Decimal("4.20")))
    assert z.status == MatchStatus.UNSICHER
    neu = ordne_manuell(z, stammdaten.artikel_nach_nummer["1644"])
    assert neu.status == MatchStatus.MANUELL
    assert neu.quelle == "manuell"
    assert neu.artikel.artikelnummer == "1644"
    assert neu.ek_preis == Decimal("2.60") and neu.vk_preis == Decimal("4.20")
    assert neu.preis_dokument == Decimal("4.20")
    assert neu.menge == Decimal(12)
    assert neu.hinweise == []
    assert neu.netto_vk == Decimal("50.40")
    # Original unverändert
    assert z.status == MatchStatus.UNSICHER and z.artikel is None and len(z.hinweise) == 1


def test_ordne_manuell_nach_kein_treffer_und_uebersprungen(matcher: ArtikelMatcher, stammdaten: Stammdaten):
    z = matcher.finde(_roh("Unbekannt", menge=None))
    neu = ordne_manuell(z, stammdaten.artikel_nach_nummer["2003"], menge=Decimal(6))
    assert neu.status == MatchStatus.MANUELL and neu.menge == Decimal(6)
    assert neu.hinweise == []
    assert neu.kandidaten[0].artikel.artikelnummer == "2003"
    z = matcher.finde(_roh("DAA Neptun", preis=Decimal(0)))
    neu = ordne_manuell(z, stammdaten.artikel_nach_nummer["2003"])
    assert neu.status == MatchStatus.MANUELL and neu.uebersprungen is False
    assert Pruefcode.NULLPREIS_OHNE_ARTIKEL not in _codes(neu)


def test_ordne_manuell_behaelt_info_hinweise(matcher: ArtikelMatcher, stammdaten: Stammdaten):
    z = matcher.finde(_roh("Colosi Nero d'Avola Sicilia DOC", preis=Decimal("0")))
    neu = ordne_manuell(z, stammdaten.artikel_nach_nummer["2001"])
    assert [h.schweregrad for h in neu.hinweise] == [Schweregrad.INFO]


def test_ordne_manuell_inaktiver_artikel_warnt(matcher: ArtikelMatcher, stammdaten: Stammdaten):
    z = matcher.finde(_roh("Gavi di Gavi 2022"))
    neu = ordne_manuell(z, stammdaten.artikel_nach_nummer["3003"])
    assert neu.status == MatchStatus.MANUELL
    assert [(h.code, h.schweregrad) for h in neu.hinweise] == [
        (Pruefcode.ARTIKEL_UNSICHER, Schweregrad.WARNUNG)
    ]
    assert "inaktiv" in neu.hinweise[0].text


def test_zuordnung_ist_json_serialisierbar(matcher: ArtikelMatcher):
    z = matcher.finde(_roh("Gloria Secco Frizzante 0,375"))
    kopie = PositionsZuordnung.model_validate_json(z.model_dump_json())
    assert kopie.kandidaten[0].artikel.artikelnummer == "1644"
    assert kopie.kandidaten[0].manuell_erforderlich is True
