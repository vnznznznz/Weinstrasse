"""Tests für die Freigabe-Oberfläche (Streamlit).

* Die reinen Funktionen (``uebernehme_korrekturen``, ``speichere_aliase``, Auswahllisten) werden ohne
  Oberfläche getestet.
* Der Ablauf (Datei wählen, "Einlesen & prüfen", Lieferant korrigieren, "Freigeben & Buchen" im Dry
  Run) läuft über ``streamlit.testing.v1.AppTest`` gegen ein temporäres Projektheim, auf das die
  Umgebungsvariable ``AUFTRAGSAUTOMAT_HOME`` zeigt (Konfiguration und Stammdaten aus tests/fixtures,
  Eingangsordner mit der Kirchhellen-PDF, Aliase als Kopie, damit Tests nichts im Repo ändern).
"""

from __future__ import annotations

import shutil
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from streamlit.testing.v1 import AppTest

from auftragsautomat.config import Settings, lade_settings
from auftragsautomat.extraktion.erkennung import extrahiere
from auftragsautomat.freigabe import app, zustand
from auftragsautomat.matching.pipeline import erstelle_belegpaket
from auftragsautomat.models import Belegpaket, LieferantZuordnung, MatchStatus, Pruefcode, Schweregrad

FIXTURES = Path(__file__).parent / "fixtures"
REPO = Path(__file__).resolve().parents[1]
PDF = FIXTURES / "pdfs" / "kundenbestellung_kirchhellen.pdf"
APP_DATEI = Path(app.__file__)


# ---------------------------------------------------------------------------
# Hilfen
# ---------------------------------------------------------------------------


def _erzeuge_projektheim(tmp_path: Path) -> Path:
    """Temporärer Projektstamm: config + stammdaten aus den Fixtures, Aliase als Kopie, Eingang mit PDF."""
    heim = tmp_path / "heim"
    shutil.copytree(FIXTURES / "config", heim / "config")
    shutil.copytree(FIXTURES / "stammdaten", heim / "stammdaten")
    shutil.copytree(REPO / "config" / "aliase", heim / "config" / "aliase")
    settings_datei = heim / "config" / "settings.yaml"
    daten = yaml.safe_load(settings_datei.read_text(encoding="utf-8"))
    daten["alias_ordner"] = "config/aliase"
    settings_datei.write_text(yaml.safe_dump(daten, allow_unicode=True), encoding="utf-8")
    eingang = heim / "eingang" / "wik_leh"
    eingang.mkdir(parents=True)
    shutil.copy(PDF, eingang / PDF.name)
    (heim / "logs").mkdir()
    return heim


@pytest.fixture()
def projektheim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    heim = _erzeuge_projektheim(tmp_path)
    monkeypatch.setenv("AUFTRAGSAUTOMAT_HOME", str(heim))
    return heim


@pytest.fixture()
def kirchhellen(settings: Settings) -> Belegpaket:
    ziel = settings.eingang_ordner / "wik_leh" / PDF.name
    ziel.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(PDF, ziel)
    return erstelle_belegpaket(extrahiere(ziel, settings), settings)


def _starte_app() -> AppTest:
    at = AppTest.from_file(str(APP_DATEI), default_timeout=180)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def _einlesen(at: AppTest, datei: str = f"wik_leh/{PDF.name}") -> AppTest:
    at.sidebar.selectbox(key="eingangsdatei").set_value(datei)
    at.sidebar.button(key="einlesen").click().run()
    assert not at.exception, [e.value for e in at.exception]
    return at


# ---------------------------------------------------------------------------
# Reine Funktionen
# ---------------------------------------------------------------------------


def test_uebernehme_korrekturen_lieferant_belegfelder_menge(settings: Settings, kirchhellen: Belegpaket):
    # Cide (Hauptlieferant) + Colosi: freigabefähig mit Warnung MEHRERE_LIEFERANTEN
    assert not kirchhellen.blockiert
    assert kirchhellen.lieferant.lieferant is not None and kirchhellen.lieferant.lieferant.lieferantennummer == "72"
    assert kirchhellen.belegfelder.weingut == "Cide srl / Cantine Colosi"
    kirchhellen.lieferant = LieferantZuordnung(kandidaten=kirchhellen.lieferant.kandidaten)  # Anwender wählt neu
    werte = {
        "lieferant": "72",
        "belegfelder": {"spedition": "Spedition Weinlogistik", "weingut": "", "shipment": "24.10.2026", "kunde": "Hegmann"},
        "positionen": {1: {"zuordnung": "1643", "menge": 66}, 3: {"zuordnung": "2001", "menge": "12"}},
        "belegnummer": " K-0917 ",
        "liefertag": date(2026, 10, 30),
        "lieferschein": True,
    }
    neu = app.uebernehme_korrekturen(kirchhellen, werte, settings)

    assert neu is not kirchhellen and kirchhellen.lieferant.lieferant is None  # Original unverändert
    assert neu.lieferant.lieferant is not None and neu.lieferant.lieferant.lieferantennummer == "72"
    assert neu.lieferant.status is MatchStatus.MANUELL
    assert neu.belegfelder.weingut == "Cide srl / Cantine Colosi"  # Vorbelegung: Hauptlieferant + weitere Weingüter
    assert any(h.code is Pruefcode.MEHRERE_LIEFERANTEN for h in neu.hinweise)
    assert neu.positionen[0].menge == Decimal("66") and neu.positionen[0].status is MatchStatus.SICHER
    assert neu.positionen[2].menge == Decimal("12")
    assert neu.roh.belegnummer == "K-0917" and neu.roh.liefertag == date(2026, 10, 30)
    assert neu.belegfelder.shipment == "24.10.2026"  # gefülltes Feld bleibt
    assert neu.lieferschein_erstellen is True
    assert neu.freigegeben is False
    assert not neu.blockiert
    assert neu.gesamtmenge == Decimal("102")


def test_uebernehme_korrekturen_artikel_ueberspringen_und_manuell(settings: Settings, kirchhellen: Belegpaket):
    werte = {
        "lieferant": "72",
        "positionen": {
            3: {"zuordnung": app.OPTION_UEBERSPRINGEN},
            4: {"zuordnung": app.OPTION_ARTIKELNUMMER, "artikelnummer": "1644", "menge": 6},
            2: {"zuordnung": app.OPTION_OFFEN},
        },
    }
    neu = app.uebernehme_korrekturen(kirchhellen, werte, settings)
    pos3 = neu.positionen[2]
    assert pos3.uebersprungen and pos3.artikel is None and pos3.quelle == "manuell"
    pos4 = neu.positionen[3]
    assert pos4.artikel is not None and pos4.artikel.artikelnummer == "1644"
    assert pos4.status is MatchStatus.MANUELL and pos4.menge == Decimal("6")
    assert pos4.ek_preis == Decimal("2.60") and pos4.vk_preis == Decimal("4.20")
    assert not any(h.code is Pruefcode.NULLPREIS_OHNE_ARTIKEL for h in pos4.hinweise)
    assert neu.positionen[1].artikel is not None and neu.positionen[1].artikel.artikelnummer == "957"
    # Preis laut Dokument 0,00 gegen VK 4,20 -> Abweichung wird gemeldet, nie stillschweigend
    assert any(h.code is Pruefcode.PREIS_ABWEICHUNG and h.position_laufnr == 4 for h in neu.alle_hinweise)


def test_uebernehme_korrekturen_kunde_wechseln_und_fehler(settings: Settings, kirchhellen: Belegpaket):
    neu = app.uebernehme_korrekturen(kirchhellen, {"kunde": "1392", "lieferant": "72"}, settings)
    assert neu.kunde.kunde is not None and neu.kunde.kunde.kundennummer == "1392"
    assert neu.kunde.status is MatchStatus.MANUELL
    assert neu.belegfelder.kunde == "Weinhaus Menrath"  # vorbelegter Name folgt dem Kunden

    ohne = app.uebernehme_korrekturen(neu, {"kunde": ""}, settings)
    assert ohne.kunde.kunde is None
    assert any(h.code is Pruefcode.KEIN_KUNDE for h in ohne.hinweise)

    with pytest.raises(app.KorrekturFehler, match="Kundennummer '9999'"):
        app.uebernehme_korrekturen(kirchhellen, {"kunde": "9999"}, settings)
    with pytest.raises(app.KorrekturFehler, match="Artikelnummer '0000'"):
        app.uebernehme_korrekturen(kirchhellen, {"positionen": {1: {"zuordnung": app.OPTION_ARTIKELNUMMER, "artikelnummer": "0000"}}}, settings)
    with pytest.raises(app.KorrekturFehler, match="Artikelnummer eingeben"):
        app.uebernehme_korrekturen(kirchhellen, {"positionen": {1: {"zuordnung": app.OPTION_ARTIKELNUMMER, "artikelnummer": " "}}}, settings)
    with pytest.raises(app.KorrekturFehler, match="keine Zahl"):
        app.uebernehme_korrekturen(kirchhellen, {"positionen": {1: {"menge": "abc"}}}, settings)
    with pytest.raises(app.KorrekturFehler, match="negativ"):
        app.uebernehme_korrekturen(kirchhellen, {"positionen": {1: {"menge": -1}}}, settings)
    with pytest.raises(app.KorrekturFehler, match="Position 99"):
        app.uebernehme_korrekturen(kirchhellen, {"positionen": {99: {"menge": 1}}}, settings)
    with pytest.raises(app.KorrekturFehler, match="kein Datum"):
        app.uebernehme_korrekturen(kirchhellen, {"liefertag": "morgen"}, settings)


def test_freigeben_und_buchen_dry_run(settings: Settings, kirchhellen: Belegpaket):
    paket = app.uebernehme_korrekturen(kirchhellen, {"lieferant": "72"}, settings)
    ergebnis, text = app.freigeben_und_buchen(paket, settings, dry_run=True)
    assert paket.freigegeben and paket.freigegeben_von == app.benutzername() and paket.freigegeben_am is not None
    assert ergebnis.erfolgreich and ergebnis.dry_run
    assert "DRY RUN" in text and "1643" in text
    assert (settings.log_ordner / "protokoll.csv").exists()


def test_speichere_aliase(settings: Settings, kirchhellen: Belegpaket, tmp_path: Path):
    alias_ordner = tmp_path / "aliase"
    shutil.copytree(settings.alias_ordner, alias_ordner)
    settings.alias_ordner = alias_ordner
    kirchhellen.roh.lieferant_text = "Cide Vini"
    kirchhellen.lieferant = LieferantZuordnung(kandidaten=kirchhellen.lieferant.kandidaten)  # Anwender wählt neu
    paket = app.uebernehme_korrekturen(
        kirchhellen,
        {"lieferant": "72", "positionen": {4: {"zuordnung": app.OPTION_ARTIKELNUMMER, "artikelnummer": "1644"}}},
        settings,
    )
    meldungen = app.speichere_aliase(paket, settings)
    assert any("Lieferant 'Cide Vini' -> 72" in m for m in meldungen)
    assert any("Artikel 'DAA Neptun' -> 1644" in m for m in meldungen)
    lieferanten = yaml.safe_load((alias_ordner / "lieferanten.yaml").read_text(encoding="utf-8"))
    eintrag = next(e for e in lieferanten["lieferanten"] if str(e["lieferantennummer"]) == "72")
    assert "Cide Vini" in eintrag["aliase"]
    artikel = yaml.safe_load((alias_ordner / "artikel.yaml").read_text(encoding="utf-8"))
    assert any(str(e["artikelnummer"]) == "1644" and "DAA Neptun" in e["aliase"] for e in artikel["artikel"])
    assert not (settings.alias_ordner.parent / "config").exists()  # nichts außerhalb des tmp-Ordners

    nichts = app.speichere_aliase(kirchhellen, settings)
    assert nichts == ["Nichts zu speichern: keine manuell zugeordneten Kunden, Lieferanten oder Artikel."]
    kirchhellen.firma_id = None
    assert "keine Firma" in app.speichere_aliase(kirchhellen, settings)[0]


def test_auswahllisten(settings: Settings, kirchhellen: Belegpaket):
    from auftragsautomat.stammdaten.laden import lade_stammdaten

    stammdaten = lade_stammdaten(settings.firma("wik_leh"))
    werte, labels = app.kunden_optionen(kirchhellen, stammdaten)
    assert werte[0] == app.OPTION_KEINE and werte[1] == "3441" and "Kandidat 100" in labels["3441"]
    assert set(werte) >= {k.kundennummer for k in stammdaten.kunden}
    werte_l, labels_l = app.lieferanten_optionen(kirchhellen, stammdaten)
    assert werte_l[1:3] == ["72", "87"] and "Kandidat 67" in labels_l["72"]
    werte_a, labels_a, vorauswahl = app.artikel_optionen(kirchhellen.positionen[2])
    assert vorauswahl == "2001" and werte_a[:3] == [app.OPTION_OFFEN, app.OPTION_UEBERSPRINGEN, app.OPTION_ARTIKELNUMMER]
    assert "[Sondervariante]" in labels_a["2004"]
    assert app.artikel_optionen(kirchhellen.positionen[3])[2] == app.OPTION_UEBERSPRINGEN
    werte_f, labels_f = app.firmen_optionen(settings)
    assert werte_f[0] == app.OPTION_KEINE and "wik_leh" in werte_f and "Wein-Import-Kreis LEH" in labels_f["wik_leh"]


def test_lade_konfiguration_ueber_umgebungsvariable(projektheim: Path):
    settings = app.lade_konfiguration()
    assert settings.eingang_ordner == projektheim / "eingang"
    assert settings.alias_ordner == projektheim / "config" / "aliase"
    assert settings.firmen["wik_leh"].stammdaten.csv.artikel == projektheim / "stammdaten" / "wik_leh" / "artikel.csv"
    assert settings.dry_run is True
    assert lade_settings(projektheim).log_ordner == projektheim / "logs"


# ---------------------------------------------------------------------------
# Ablauf in der Oberfläche (AppTest)
# ---------------------------------------------------------------------------


def test_app_startet_ohne_paket(projektheim: Path):
    at = _starte_app()
    assert at.session_state[app.SS_PAKET] is None
    assert at.sidebar.toggle(key="dry_run").value is True
    assert at.sidebar.selectbox(key="eingangsdatei").options == [f"wik_leh/{PDF.name}"]
    assert any("Eingangsdatei einlesen" in i.value for i in at.info)
    assert all(b.key != "buchen" for b in at.button)  # ohne Paket keine Aktionen


def test_app_einlesen_korrigieren_und_dry_run_buchen(projektheim: Path):
    at = _starte_app()
    _einlesen(at)

    paket: Belegpaket = at.session_state[app.SS_PAKET]
    assert paket.firma_id == "wik_leh" and not paket.blockiert  # Cide + Colosi: Warnung, kein Blocker
    assert any("eingelesen und geprüft" in s.value for s in at.success)
    paketdatei = projektheim / "logs" / "pakete" / "kundenbestellung_kirchhellen.paket.json"
    assert paketdatei.exists()
    # Tabelle: Position 1 ist 1643 zugeordnet, Position 4 übersprungen
    assert any("1643" in m.value for m in at.markdown)
    assert at.selectbox(key="pos_1_zuordnung").value == "1643"
    assert at.selectbox(key="pos_4_zuordnung").value == app.OPTION_UEBERSPRINGEN
    assert at.number_input(key="pos_1_menge").value == 60
    assert at.selectbox(key="kopf_kunde").value == "3441"
    assert at.selectbox(key="kopf_lieferant").value == "72"  # Hauptlieferant Cide
    assert at.sidebar.selectbox(key="firma").value == "wik_leh"
    assert at.text_input(key="bf_weingut").value == "Cide srl / Cantine Colosi"
    assert not at.button(key="buchen").disabled
    assert any("MEHRERE_LIEFERANTEN" in w.value for w in at.warning)
    assert not at.error

    # Korrigieren: Lieferant Colosi als Hauptlieferant wählen, Menge Position 1 auf 66
    at.selectbox(key="kopf_lieferant").set_value("87")
    at.number_input(key="pos_1_menge").set_value(66)
    at.button(key="korrigieren").click().run()
    assert not at.exception, [e.value for e in at.exception]
    paket = at.session_state[app.SS_PAKET]
    assert not paket.blockiert
    assert paket.lieferant.lieferant is not None and paket.lieferant.lieferant.lieferantennummer == "87"
    assert paket.positionen[0].menge == Decimal("66")
    assert paket.belegfelder.weingut == "Cantine Colosi / Cide srl"
    assert at.text_input(key="bf_weingut").value == "Cantine Colosi / Cide srl"  # Oberfläche zeigt den neuen Paketstand
    assert at.number_input(key="pos_1_menge").value == 66
    assert not at.button(key="buchen").disabled
    assert zustand.lade_paket(paketdatei).positionen[0].menge == Decimal("66")

    # Freigeben & Buchen im Dry Run
    at.button(key="buchen").click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert any("Buchung erfolgreich" in s.value and "Dry Run" in s.value for s in at.success)
    paket = at.session_state[app.SS_PAKET]
    assert paket.freigegeben and paket.freigegeben_von
    ergebnis = at.session_state[app.SS_ERGEBNIS]
    assert ergebnis.erfolgreich and ergebnis.dry_run
    assert ergebnis.einkauf is not None and ergebnis.einkauf.partner == "87 Cantine Colosi"
    assert [t.partner for t in ergebnis.einkauf_weitere] == ["72 Cide srl"]
    assert any("aufgeteilt" in i.value for i in at.info)
    assert (projektheim / "logs" / "protokoll.csv").exists()
    assert list((projektheim / "logs" / "opentrans").glob("*.xml"))
    assert any("Protokoll:" in i.value for i in at.info)
    assert any("DRY RUN" in c.value for c in at.code)
    assert (projektheim / "eingang" / "wik_leh" / PDF.name).exists()  # nicht archiviert
    assert zustand.lade_paket(paketdatei).freigegeben is True


def test_app_korrekturfehler_wird_angezeigt(projektheim: Path):
    at = _starte_app()
    _einlesen(at)
    at.selectbox(key="pos_1_zuordnung").set_value(app.OPTION_ARTIKELNUMMER).run()
    at.text_input(key="pos_1_artikelnummer").set_value("0000")
    at.button(key="korrigieren").click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert any("Artikelnummer '0000'" in e.value for e in at.error)
    paket: Belegpaket = at.session_state[app.SS_PAKET]
    assert paket.positionen[0].artikel is not None and paket.positionen[0].artikel.artikelnummer == "1643"


def test_app_scharf_erfordert_bestaetigung(projektheim: Path):
    at = _starte_app()
    _einlesen(at)
    assert not at.button(key="buchen").disabled

    at.sidebar.toggle(key="dry_run").set_value(False).run()
    assert at.button(key="buchen").disabled
    assert any("SCHARF" in w.value for w in at.sidebar.warning)
    at.sidebar.checkbox(key="scharf_bestaetigt").check().run()
    assert not at.button(key="buchen").disabled
    assert "SCHARF" in at.button(key="buchen").label


def test_app_firma_wechseln_und_paket_laden(projektheim: Path):
    at = _starte_app()
    _einlesen(at)
    at.sidebar.selectbox(key="firma").set_value("wik_fh_seh").run()
    assert not at.exception, [e.value for e in at.exception]
    paket: Belegpaket = at.session_state[app.SS_PAKET]
    assert paket.firma_id == "wik_fh_seh" and paket.firma_quelle == "manuell"
    assert at.sidebar.selectbox(key="firma").value == "wik_fh_seh"
    assert any("Firma auf wik_fh_seh gesetzt" in s.value for s in at.success)

    # Gespeichertes Paket laden (zuletzt gespeichert = wik_fh_seh-Stand)
    at.sidebar.selectbox(key="gespeichertes_paket").set_value(
        str(projektheim / "logs" / "pakete" / "kundenbestellung_kirchhellen.paket.json")
    )
    at.sidebar.button(key="paket_laden").click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert at.session_state[app.SS_PAKET].firma_id == "wik_fh_seh"
    assert any("geladen" in s.value for s in at.success)

    at.sidebar.button(key="paket_loeschen").click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert at.session_state[app.SS_PAKET] is None
    assert not (projektheim / "logs" / "pakete" / "kundenbestellung_kirchhellen.paket.json").exists()


def test_app_einlesen_fehler_bleibt_in_der_oberflaeche(projektheim: Path):
    kaputt = projektheim / "eingang" / "kaputt.pdf"
    kaputt.write_bytes(b"kein pdf")
    at = _starte_app()
    at.sidebar.selectbox(key="eingangsdatei").set_value("kaputt.pdf")
    at.sidebar.button(key="einlesen").click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert any("fehlgeschlagen" in e.value for e in at.error)
    assert at.session_state[app.SS_PAKET] is None


def test_hinweis_schweregrade_in_der_oberflaeche(projektheim: Path):
    # Menrath: Magnum nur manuell (FEHLER VK_FEHLT) und Preisabweichung Gavi (WARNUNG)
    menrath = FIXTURES / "pdfs" / "kundenbestellung_menrath.pdf"
    shutil.copy(menrath, projektheim / "eingang" / "wik_leh" / menrath.name)
    at = _starte_app()
    _einlesen(at, f"wik_leh/{menrath.name}")
    paket: Belegpaket = at.session_state[app.SS_PAKET]
    fehler = [h for h in paket.alle_hinweise if h.schweregrad is Schweregrad.FEHLER]
    warnungen = [h for h in paket.alle_hinweise if h.schweregrad is Schweregrad.WARNUNG]
    assert fehler and warnungen
    for h in fehler:
        assert any(h.code.value in e.value for e in at.error)
    for h in warnungen:
        assert any(h.code.value in w.value for w in at.warning)
