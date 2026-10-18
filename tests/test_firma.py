"""Tests für matching.firma (Firmen-Ermittlung) mit firmen.yaml, Aliasen und Stämmen der Fixtures."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from auftragsautomat.config import Settings
from auftragsautomat.matching.aliase import AliasEintrag, AliasTabellen, normalisiere_name
from auftragsautomat.matching.firma import (
    QUELLE_ALIAS_KUNDE,
    QUELLE_ALIAS_LIEFERANT,
    QUELLE_EINGANG_ORDNER,
    QUELLE_EMPFAENGER,
    QUELLE_STAMMDATEN,
    FirmaErgebnis,
    ermittle_firma,
    firma_aus_empfaenger,
    firma_aus_ordner,
)
from auftragsautomat.models import (
    Eingangstyp,
    Extraktionsmethode,
    Kunde,
    Pruefcode,
    RohBestellung,
    RohPosition,
    Schweregrad,
)
from auftragsautomat.stammdaten.basis import Stammdaten


@pytest.fixture()
def aliase(settings: Settings) -> AliasTabellen:
    return AliasTabellen.lade(settings.alias_ordner)


def _roh(settings: Settings, datei: str | Path = "bestellung.pdf", **felder) -> RohBestellung:
    quelldatei = (
        Path(datei)
        if Path(datei).is_absolute() or len(Path(datei).parts) > 1
        else settings.eingang_ordner / datei
    )
    felder.setdefault("eingangstyp", Eingangstyp.KUNDENBESTELLUNG_PDF)
    felder.setdefault("extraktionsmethode", Extraktionsmethode.PDFPLUMBER)
    return RohBestellung(quelldatei=quelldatei, **felder)


def _kein_lader(settings: Settings) -> dict[str, Stammdaten]:
    raise AssertionError("Stammdaten dürfen in diesem Fall nicht geladen werden")


def _kunde(firma_id: str, nummer: str, name: str, ort: str = "Lübeck") -> Kunde:
    return Kunde(firma_id=firma_id, kundennummer=nummer, name=name, ort=ort, plz="23570")


def _alias_tabellen_kunde(*firmen: str, alias: str = "Hotel Seeblick", nummer: str = "7001") -> AliasTabellen:
    return AliasTabellen(
        kunden=[AliasEintrag(nummer, firma_id, alias, normalisiere_name(alias)) for firma_id in firmen]
    )


# ---------------------------------------------------------------------------
# 1. Eingangsordner
# ---------------------------------------------------------------------------


def test_ordner_unterhalb_des_eingangsordners(settings: Settings, aliase: AliasTabellen):
    roh = _roh(settings, settings.eingang_ordner / "wik_leh" / "bestellung.pdf", kunde_text="Irgendwer")
    ergebnis = ermittle_firma(roh, settings, aliase, stammdaten_lader=_kein_lader)
    assert isinstance(ergebnis, FirmaErgebnis)
    assert (ergebnis.firma_id, ergebnis.quelle) == ("wik_leh", QUELLE_EINGANG_ORDNER)
    assert ergebnis.hinweise == []
    assert ergebnis.gefunden is True
    assert ergebnis.kandidaten == ("wik_leh",)


def test_ordner_verschachtelt_und_gross_klein(settings: Settings):
    datei = settings.eingang_ordner / "WIK_LEH" / "2026" / "september" / "x.pdf"
    assert firma_aus_ordner(datei, settings) == "wik_leh"


def test_ordner_relativer_pfad(settings: Settings):
    assert firma_aus_ordner(Path("eingang/woe_leh/x.pdf"), settings) == "woe_leh"
    assert firma_aus_ordner("eingang\\wik_fh_seh\\x.pdf".replace("\\", "/"), settings) == "wik_fh_seh"


def test_ordner_auch_im_archiv(settings: Settings):
    assert firma_aus_ordner(Path("/irgendwo/archiv/wik_fh_seh/2026/x.pdf"), settings) == "wik_fh_seh"


def test_ordner_unbekannt_oder_direkt_im_eingang(settings: Settings):
    assert firma_aus_ordner(settings.eingang_ordner / "fremd" / "x.pdf", settings) is None
    assert firma_aus_ordner(settings.eingang_ordner / "x.pdf", settings) is None
    assert firma_aus_ordner(None, settings) is None


def test_ordner_ohne_konfigurierte_unterordner(settings: Settings):
    for firma in settings.firmen.values():
        firma.eingang_unterordner = None
    assert firma_aus_ordner(settings.eingang_ordner / "wik_leh" / "x.pdf", settings) is None


# ---------------------------------------------------------------------------
# 2./3. Aliase
# ---------------------------------------------------------------------------


def test_alias_kunde(settings: Settings, aliase: AliasTabellen):
    roh = _roh(settings, kunde_text="Weinmarkt Kirchhellen GmbH & Co. KG")
    ergebnis = ermittle_firma(roh, settings, aliase, stammdaten_lader=_kein_lader)
    assert (ergebnis.firma_id, ergebnis.quelle) == ("wik_leh", QUELLE_ALIAS_KUNDE)
    assert ergebnis.hinweise == []


def test_alias_kunde_in_adresse_und_rohtext(settings: Settings, aliase: AliasTabellen):
    roh = _roh(settings, kunde_adresse="Hegmann\nHauptstraße 1\n46244 Bottrop")
    assert tuple(ermittle_firma(roh, settings, aliase, stammdaten_lader=_kein_lader)) == (
        "wik_leh",
        QUELLE_ALIAS_KUNDE,
    )
    roh = _roh(settings, rohtext="Bestellung\nfür Weinhaus Menrath, Lieferung bis 15.09.")
    assert tuple(ermittle_firma(roh, settings, aliase, stammdaten_lader=_kein_lader)) == (
        "wik_leh",
        QUELLE_ALIAS_KUNDE,
    )


def test_alias_lieferant_text(settings: Settings, aliase: AliasTabellen):
    roh = _roh(settings, kunde_text="Unbekannter Kunde", lieferant_text="Cantine Colosi")
    ergebnis = ermittle_firma(roh, settings, aliase, stammdaten_lader=_kein_lader)
    assert (ergebnis.firma_id, ergebnis.quelle) == ("wik_leh", QUELLE_ALIAS_LIEFERANT)


def test_alias_lieferant_ueber_produktmarke(settings: Settings, aliase: AliasTabellen):
    roh = _roh(
        settings,
        positionen=[
            RohPosition(laufnr=1, bezeichnung="Villa Armellina Gloria Secco Frizzante", menge=Decimal(6)),
            RohPosition(laufnr=2, bezeichnung="Gestrichen Colosi", menge=Decimal(6), durchgestrichen=True),
        ],
    )
    ergebnis = ermittle_firma(roh, settings, aliase, stammdaten_lader=_kein_lader)
    assert (ergebnis.firma_id, ergebnis.quelle) == ("wik_leh", QUELLE_ALIAS_LIEFERANT)


def test_alias_kunde_vor_lieferant(settings: Settings, aliase: AliasTabellen):
    roh = _roh(settings, kunde_text="Weinhaus Menrath", lieferant_text="Bollina srl")
    assert ermittle_firma(roh, settings, aliase, stammdaten_lader=_kein_lader).quelle == QUELLE_ALIAS_KUNDE


def test_alias_auf_unbekannte_firma_wird_ignoriert(settings: Settings, caplog):
    tabellen = _alias_tabellen_kunde("gibtsnicht")
    roh = _roh(settings, kunde_text="Hotel Seeblick")
    ergebnis = ermittle_firma(roh, settings, tabellen, stammdaten_lader=lambda s: {})
    assert (ergebnis.firma_id, ergebnis.quelle) == (None, None)
    assert "unbekannte Firma" in caplog.text


def test_alias_mehrdeutig_ohne_empfaenger(settings: Settings):
    tabellen = _alias_tabellen_kunde("wik_fh_seh", "woe_leh")
    roh = _roh(settings, kunde_text="Hotel Seeblick")
    ergebnis = ermittle_firma(roh, settings, tabellen, stammdaten_lader=lambda s: {})
    assert (ergebnis.firma_id, ergebnis.quelle) == (None, None)
    assert set(ergebnis.kandidaten) == {"wik_fh_seh", "woe_leh"}
    assert len(ergebnis.hinweise) == 1
    hinweis = ergebnis.hinweise[0]
    assert (hinweis.code, hinweis.schweregrad) == (Pruefcode.FIRMA_UNKLAR, Schweregrad.WARNUNG)
    assert hinweis.details["kandidaten"] == "wik_fh_seh, woe_leh"


def test_alias_mehrdeutig_empfaenger_entscheidet(settings: Settings):
    tabellen = _alias_tabellen_kunde("wik_fh_seh", "woe_leh")
    roh = _roh(settings, kunde_text="Hotel Seeblick", empfaenger_firma_text="Wine-of-Excellence LEH GmbH")
    ergebnis = ermittle_firma(roh, settings, tabellen, stammdaten_lader=_kein_lader)
    assert (ergebnis.firma_id, ergebnis.quelle) == ("woe_leh", QUELLE_ALIAS_KUNDE)
    assert ergebnis.hinweise == []


# ---------------------------------------------------------------------------
# Ordner vs. Alias
# ---------------------------------------------------------------------------


def test_ordner_gewinnt_gegen_alias_mit_warnung(settings: Settings, aliase: AliasTabellen):
    roh = _roh(settings, settings.eingang_ordner / "wik_fh_seh" / "x.pdf", kunde_text="Weinmarkt Kirchhellen")
    ergebnis = ermittle_firma(roh, settings, aliase, stammdaten_lader=_kein_lader)
    assert (ergebnis.firma_id, ergebnis.quelle) == ("wik_fh_seh", QUELLE_EINGANG_ORDNER)
    assert len(ergebnis.hinweise) == 1
    hinweis = ergebnis.hinweise[0]
    assert (hinweis.code, hinweis.schweregrad) == (Pruefcode.FIRMA_UNKLAR, Schweregrad.WARNUNG)
    assert hinweis.details["firma_ordner"] == "wik_fh_seh"
    assert hinweis.details["firma_alias"] == "wik_leh"
    assert hinweis.details["alias"] == "Weinmarkt Kirchhellen"
    assert "wik_fh_seh" in hinweis.text and "wik_leh" in hinweis.text
    assert set(ergebnis.kandidaten) == {"wik_fh_seh", "wik_leh"}


def test_ordner_ohne_gegenprobe_durch_lieferanten_alias(settings: Settings, aliase: AliasTabellen):
    """Colosi-Weine im Ordner wik_fh_seh: der Lieferanten-Alias (wik_leh) ist kein Widerspruch."""
    roh = _roh(
        settings,
        settings.eingang_ordner / "wik_fh_seh" / "x.pdf",
        kunde_text="Hotel Seeblick",
        lieferant_text="Cantine Colosi",
        positionen=[
            RohPosition(laufnr=1, bezeichnung="Colosi Nero d'Avola Sicilia DOC 2022", menge=Decimal(12))
        ],
    )
    ergebnis = ermittle_firma(roh, settings, aliase, stammdaten_lader=_kein_lader)
    assert (ergebnis.firma_id, ergebnis.quelle) == ("wik_fh_seh", QUELLE_EINGANG_ORDNER)
    assert ergebnis.hinweise == []
    assert ergebnis.kandidaten == ("wik_fh_seh",)


def test_ordner_und_alias_einig(settings: Settings, aliase: AliasTabellen):
    roh = _roh(settings, settings.eingang_ordner / "wik_leh" / "x.pdf", kunde_text="Weinmarkt Kirchhellen")
    ergebnis = ermittle_firma(roh, settings, aliase, stammdaten_lader=_kein_lader)
    assert (ergebnis.firma_id, ergebnis.quelle) == ("wik_leh", QUELLE_EINGANG_ORDNER)
    assert ergebnis.hinweise == []


# ---------------------------------------------------------------------------
# 4. Stammdaten
# ---------------------------------------------------------------------------


def test_stammdaten_eindeutig_mit_echten_fixtures(settings: Settings, aliase: AliasTabellen):
    roh = _roh(settings, kunde_text="Hotel Seeblick", kunde_adresse="Seestraße 1\n23570 Lübeck")
    ergebnis = ermittle_firma(roh, settings, aliase)  # Standard-Lader: stammdaten.laden.lade_alle
    assert (ergebnis.firma_id, ergebnis.quelle) == ("wik_fh_seh", QUELLE_STAMMDATEN)
    assert ergebnis.hinweise == []


def test_stammdaten_lader_wird_benutzt(settings: Settings, aliase: AliasTabellen):
    aufrufe: list[Settings] = []

    def lader(s: Settings) -> dict[str, Stammdaten]:
        aufrufe.append(s)
        return {
            "woe_leh": Stammdaten("woe_leh", kunden=[_kunde("woe_leh", "8101", "Edeka Zentrale Rhein-Ruhr")])
        }

    roh = _roh(settings, kunde_text="Edeka Zentrale Rhein-Ruhr")
    ergebnis = ermittle_firma(roh, settings, aliase, stammdaten_lader=lader)
    assert (ergebnis.firma_id, ergebnis.quelle) == ("woe_leh", QUELLE_STAMMDATEN)
    assert aufrufe == [settings]


def test_stammdaten_mehrdeutig(settings: Settings, aliase: AliasTabellen):
    def lader(s: Settings) -> dict[str, Stammdaten]:
        return {
            "wik_fh_seh": Stammdaten("wik_fh_seh", kunden=[_kunde("wik_fh_seh", "7001", "Hotel Seeblick")]),
            "woe_leh": Stammdaten("woe_leh", kunden=[_kunde("woe_leh", "8201", "Hotel Seeblick")]),
        }

    roh = _roh(settings, kunde_text="Hotel Seeblick")
    ergebnis = ermittle_firma(roh, settings, aliase, stammdaten_lader=lader)
    assert (ergebnis.firma_id, ergebnis.quelle) == (None, None)
    assert set(ergebnis.kandidaten) == {"wik_fh_seh", "woe_leh"}
    assert [h.code for h in ergebnis.hinweise] == [Pruefcode.FIRMA_UNKLAR]
    assert "7001" in ergebnis.hinweise[0].text and "8201" in ergebnis.hinweise[0].text

    roh = _roh(settings, kunde_text="Hotel Seeblick", empfaenger_firma_text="Wein-Import-Kreis FH & SEH")
    ergebnis = ermittle_firma(roh, settings, aliase, stammdaten_lader=lader)
    assert (ergebnis.firma_id, ergebnis.quelle) == ("wik_fh_seh", QUELLE_STAMMDATEN)


def test_stammdaten_nur_sichere_treffer(settings: Settings, aliase: AliasTabellen):
    """'Weinhandlung Mueller' mit Lüdenscheid-Adresse ist in wik_leh UNSICHER -> keine Firma."""
    roh = _roh(settings, kunde_text="Weinhandlung Mueller", kunde_adresse="Kirchweg 9\n58507 Lüdenscheid")
    ergebnis = ermittle_firma(roh, settings, aliase)
    assert (ergebnis.firma_id, ergebnis.quelle) == (None, None)


def test_stammdaten_ohne_kundentext_nicht_geladen(settings: Settings, aliase: AliasTabellen):
    roh = _roh(settings, lieferant_text="Unbekanntes Weingut")
    ergebnis = ermittle_firma(roh, settings, aliase, stammdaten_lader=_kein_lader)
    assert (ergebnis.firma_id, ergebnis.quelle) == (None, None)
    assert ergebnis.hinweise == [] and ergebnis.kandidaten == ()


# ---------------------------------------------------------------------------
# 5. Empfänger und Sonstiges
# ---------------------------------------------------------------------------


def test_empfaenger_als_letzter_ausweg(settings: Settings, aliase: AliasTabellen):
    roh = _roh(
        settings,
        kunde_text="Unbekannter Kunde",
        empfaenger_firma_text="Wein-Import-Kreis LEH\nMusterstraße 1",
    )
    ergebnis = ermittle_firma(roh, settings, aliase, stammdaten_lader=lambda s: {})
    assert (ergebnis.firma_id, ergebnis.quelle) == ("wik_leh", QUELLE_EMPFAENGER)


def test_firma_aus_empfaenger(settings: Settings):
    assert firma_aus_empfaenger("Wein-Import-Kreis LEH", settings) == "wik_leh"
    assert firma_aus_empfaenger("An die Weinstrasse Agentur GmbH", settings) == "weinstrasse_agentur"
    assert firma_aus_empfaenger("wik_leh", settings) == "wik_leh"
    assert firma_aus_empfaenger("Wein-Import-Kreis", settings) is None  # passt zu keinem vollständigen Namen
    assert firma_aus_empfaenger("Hotel Seeblick", settings) is None
    assert firma_aus_empfaenger(None, settings) is None
    assert firma_aus_empfaenger("Wein-Import-Kreis LEH", settings, kandidaten=["woe_leh"]) is None


def test_nichts_gefunden(settings: Settings, aliase: AliasTabellen):
    roh = _roh(settings, kunde_text="Château Inconnu", lieferant_text="Domaine Inconnue")
    ergebnis = ermittle_firma(roh, settings, aliase)
    assert (ergebnis.firma_id, ergebnis.quelle) == (None, None)
    assert ergebnis.gefunden is False
    assert ergebnis.hinweise == []


def test_ergebnis_entpackbar_und_datei_parameter(settings: Settings, aliase: AliasTabellen):
    roh = _roh(settings, kunde_text="Weinmarkt Kirchhellen")
    firma_id, quelle = ermittle_firma(
        roh,
        settings,
        aliase,
        stammdaten_lader=_kein_lader,
        datei=settings.eingang_ordner / "woe_leh" / "x.pdf",
    )
    assert (firma_id, quelle) == ("woe_leh", QUELLE_EINGANG_ORDNER)
