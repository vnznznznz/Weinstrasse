"""Tests für ``auftragsautomat lexware-erkunden`` (lexware/erkundung.py): Baum nur lesend erfassen, Dateien,
Abgleich mit config/lexware_controls.yaml, Offline-Abgleich (--aus-datei) und CLI.

Der Lexware-Baum ist synthetisch (Fake-Objekte, die pywinauto-Wrapper nachahmen); keine echten Daten.
"""

from __future__ import annotations

import inspect
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from auftragsautomat import cli
from auftragsautomat.config import Settings
from auftragsautomat.lexware import erkundung as ek
from auftragsautomat.lexware.ui_automation import (
    ControlSpec,
    Fenster,
    LexwareControls,
    PywinautoBackend,
    lade_controls,
)

REPO = Path(__file__).resolve().parents[1]
REPO_CONTROLS = REPO / "config" / "lexware_controls.yaml"
HAUPTTITEL = "Lexware warenwirtschaft premium - Wein-Import-Kreis LEH"
VERBOTEN = ("click", "click_input", "double_click_input", "type_keys", "set_focus", "close", "set_text",
            "set_edit_text", "select", "invoke", "menu_select", "send_keystrokes", "set_value", "drag_mouse_input")
ERLAUBT = {"element_info", "children", "rectangle", "is_visible", "is_enabled", "friendly_class_name", "handle"}

runner = CliRunner()


# ---------------------------------------------------------------------------
# Fake-Baum
# ---------------------------------------------------------------------------


class Protokoll:
    def __init__(self) -> None:
        self.verboten: list[str] = []
        self.unbekannt: list[str] = []


class FakeRect:
    def __init__(self, l: int, t: int, r: int, b: int) -> None:  # noqa: E741
        self.left, self.top, self.right, self.bottom = l, t, r, b


class FakeInfo:
    def __init__(self, control_type: str | None, name: str | None, automation_id: str = "", class_name: str = "",
                 control_id: int | None = None, kaputt: tuple[str, ...] = ()) -> None:
        self._werte = {"control_type": control_type, "name": name, "automation_id": automation_id,
                       "class_name": class_name, "control_id": control_id}
        self._kaputt = kaputt

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        if name in self._kaputt:
            raise RuntimeError(f"COMError beim Lesen von {name}")
        if name in self._werte:
            return self._werte[name]
        raise AttributeError(name)


class FakeWrapper:
    """Imitiert einen pywinauto-Wrapper; schreibende Methoden werden nur protokolliert."""

    def __init__(self, protokoll: Protokoll, control_type: str | None, name: str | None = None, *,
                 automation_id: str = "", class_name: str = "", control_id: int | None = None,
                 kinder: list[FakeWrapper] | None = None, sichtbar: bool = True, aktiviert: bool = True,
                 info_kaputt: bool = False, kinder_kaputt: bool = False, rechteck_kaputt: bool = False,
                 attribut_kaputt: tuple[str, ...] = ()) -> None:
        self._p = protokoll
        self._info = FakeInfo(control_type, name, automation_id, class_name, control_id, attribut_kaputt)
        self._kinder = kinder or []
        self._sichtbar, self._aktiviert = sichtbar, aktiviert
        self._info_kaputt, self._kinder_kaputt, self._rechteck_kaputt = info_kaputt, kinder_kaputt, rechteck_kaputt
        self.handle = 4711

    @property
    def element_info(self) -> FakeInfo:
        if self._info_kaputt:
            raise RuntimeError("ElementNotAvailable")
        return self._info

    def children(self) -> list[FakeWrapper]:
        if self._kinder_kaputt:
            raise RuntimeError("children() fehlgeschlagen")
        return list(self._kinder)

    def rectangle(self) -> FakeRect:
        if self._rechteck_kaputt:
            raise RuntimeError("rectangle() fehlgeschlagen")
        return FakeRect(0, 0, 100, 20)

    def is_visible(self) -> bool:
        return self._sichtbar

    def is_enabled(self) -> bool:
        return self._aktiviert

    def friendly_class_name(self) -> str:
        return "Fake"

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        protokoll = self.__dict__.get("_p")
        if name in VERBOTEN:
            def verboten(*_a: Any, **_k: Any) -> None:
                protokoll.verboten.append(name)
            return verboten
        if protokoll is not None:
            protokoll.unbekannt.append(name)
        raise AttributeError(name)


def _hauptbaum(p: Protokoll) -> FakeWrapper:
    W = FakeWrapper
    return W(p, "Window", HAUPTTITEL, class_name="TLxFrame", kinder=[
        W(p, "MenuBar", "Menü", kinder=[W(p, "MenuItem", "Datei"), W(p, "MenuItem", "Bearbeiten"),
                                         W(p, "MenuItem", "Verkauf")]),
        W(p, "Pane", "Auftragsliste", kinder=[
            W(p, "DataGrid", "Aufträge", automation_id="gridAuftraege", kinder=[
                W(p, "HeaderItem", "Belegnummer"), W(p, "HeaderItem", "Kundennummer"),
                W(p, "HeaderItem", "Status"),
            ]),
            W(p, "Button", "Weiterführen", automation_id="btnWeiterfuehren"),
            W(p, "Button", "kaputt", info_kaputt=True, kinder=[W(p, "Button", "nie gelesen")]),
            W(p, "Pane", "ohne Kinder lesbar", kinder_kaputt=True),
            W(p, "Text", "Rechteck fehlt", rechteck_kaputt=True),
            W(p, "Text", "Name kaputt", attribut_kaputt=("name",)),
        ]),
        W(p, "Pane", "tief", kinder=[W(p, "Pane", "tiefer", kinder=[W(p, "Edit", "ganz tief")])]),
    ])


def _assistent(p: Protokoll) -> FakeWrapper:
    W = FakeWrapper
    return W(p, "Window", "Auftragsassistent Verkauf", kinder=[
        W(p, "List", "Seiten", kinder=[W(p, "ListItem", "1 Kunde"), W(p, "ListItem", "2 Positionen")]),
        W(p, "Button", "Weiter >", automation_id="btnNext"),
        W(p, "Button", "Abbrechen"),
        W(p, "Button", "Speichern", sichtbar=False),
        W(p, "Edit", "Menge", automation_id="edtMenge1"),
        W(p, "Edit", "Menge", automation_id="edtMenge2"),
        W(p, "Edit", "Kunden-Nr.", automation_id="edtKundennummer"),
        W(p, "ComboBox", "Auftragsart"),
        W(p, "Text", "Hinweis 1"),
        W(p, "Text", "Hinweis 2"),
    ])


class FakeQuelle:
    def __init__(self, backend_name: str, protokoll: Protokoll, *, laeuft: bool = True, mit_assistent: bool = True) -> None:
        self.backend_name = backend_name
        self.controls = LexwareControls()
        self._p = protokoll
        self._laeuft = laeuft
        self._mit_assistent = mit_assistent
        self.aufrufe: list[str] = []

    def prozess_laeuft(self) -> bool:
        self.aufrufe.append("prozess_laeuft")
        return self._laeuft

    def aktive_firma(self) -> str:
        return HAUPTTITEL

    def lese_fenster_fuer_erkundung(self, *, alle_fenster: bool = False, timeout: float | None = None) -> list[Fenster]:
        self.aufrufe.append(f"lese_fenster(alle={alle_fenster})")
        fenster = [Fenster(HAUPTTITEL, _hauptbaum(self._p), "haupt")]
        if alle_fenster and self._mit_assistent:
            fenster.append(Fenster("Auftragsassistent Verkauf", _assistent(self._p), "assistent"))
        return fenster

    # Wer hier landet, hat etwas Schreibendes versucht
    def starte(self, *_a: Any) -> None:
        self._p.verboten.append("starte")

    def klicke(self, *_a: Any) -> None:
        self._p.verboten.append("klicke")

    def sende_tasten(self, *_a: Any) -> None:
        self._p.verboten.append("sende_tasten")


@pytest.fixture()
def protokoll() -> Protokoll:
    return Protokoll()


@pytest.fixture()
def erk_settings(settings: Settings) -> Settings:
    settings.lexware.controls_datei = REPO_CONTROLS
    settings.lexware.backend = "uia"
    return settings


def _erkunde(settings: Settings, protokoll: Protokoll, **kwargs: Any) -> ek.Erkundung:
    backends = kwargs.pop("backends", ["uia"])
    return ek.erkunde(settings, backends=backends, quelle_fabrik=lambda name: FakeQuelle(name, protokoll), **kwargs)


# ---------------------------------------------------------------------------
# Baumerfassung
# ---------------------------------------------------------------------------


def test_baum_erfassung_felder_und_pfade(protokoll: Protokoll):
    baum = ek.erfasse_fenster(Fenster(HAUPTTITEL, _hauptbaum(protokoll), "haupt"), backend="uia")
    nach_pfad = {e.pfad: e for e in baum.elemente}
    wurzel = nach_pfad["0"]
    assert (wurzel.control_type, wurzel.name, wurzel.class_name, wurzel.ebene) == ("Window", HAUPTTITEL, "TLxFrame", 0)
    grid = nach_pfad["0.1.0"]
    assert grid.auto_id == "gridAuftraege" and grid.control_type == "DataGrid" and grid.ebene == 2
    assert nach_pfad["0.1.0.0"].name == "Belegnummer"
    assert grid.rechteck == [0, 0, 100, 20] and grid.sichtbar is True and grid.aktiviert is True
    # Reihenfolge = Baumreihenfolge (Vorordnung)
    assert [e.pfad for e in baum.elemente][:4] == ["0", "0.0", "0.0.0", "0.0.1"]


def test_fehlertoleranz_ueberspringt_und_zaehlt(protokoll: Protokoll):
    baum = ek.erfasse_fenster(Fenster(HAUPTTITEL, _hauptbaum(protokoll), "haupt"), backend="uia")
    namen = [e.name for e in baum.elemente]
    assert "kaputt" not in namen and "nie gelesen" not in namen  # element_info kaputt -> samt Unterbaum übersprungen
    assert baum.uebersprungen == 1
    assert "ohne Kinder lesbar" in namen  # children() kaputt -> Element bleibt, Fehler gezählt
    rechteck = next(e for e in baum.elemente if e.name == "Rechteck fehlt")
    assert rechteck.rechteck is None
    name_kaputt = next(e for e in baum.elemente if e.pfad == "0.1.5")
    assert name_kaputt.name is None and name_kaputt.control_type == "Text"
    assert baum.lesefehler == 3 and baum.fehler == 4
    assert len(baum.fehlermeldungen) == 4 and any("element_info" in m for m in baum.fehlermeldungen)


def test_tiefe_begrenzt(protokoll: Protokoll):
    tief = ek.erfasse_fenster(Fenster(HAUPTTITEL, _hauptbaum(protokoll), "haupt"), backend="uia", tiefe=12)
    flach = ek.erfasse_fenster(Fenster(HAUPTTITEL, _hauptbaum(protokoll), "haupt"), backend="uia", tiefe=1)
    assert max(e.ebene for e in tief.elemente) == 3
    assert max(e.ebene for e in flach.elemente) == 1
    assert {e.pfad for e in flach.elemente} == {"0", "0.0", "0.1", "0.2"}
    null = ek.erfasse_fenster(Fenster(HAUPTTITEL, _hauptbaum(protokoll), "haupt"), backend="uia", tiefe=0)
    assert [e.pfad for e in null.elemente] == ["0"]


def test_obergrenze_schneidet_ab(protokoll: Protokoll):
    baum = ek.erfasse_fenster(Fenster(HAUPTTITEL, _hauptbaum(protokoll), "haupt"), backend="uia", max_elemente=5)
    assert len(baum.elemente) == 5 and baum.abgeschnitten


def test_keine_schreibenden_methoden(erk_settings: Settings, protokoll: Protokoll):
    erk = _erkunde(erk_settings, protokoll, alle_fenster=True, backends=["uia", "win32"])
    assert erk.anzahl_elemente > 20
    assert protokoll.verboten == []
    assert set(protokoll.unbekannt) <= ERLAUBT, protokoll.unbekannt


def test_quelltext_ohne_schreibende_aufrufe():
    quellen = [inspect.getsource(ek), inspect.getsource(PywinautoBackend.lese_fenster_fuer_erkundung)]
    for text in quellen:
        for methode in ("click", "click_input", "type_keys", "set_focus", "close", "set_text", "menu_select",
                        "starte", "sende_tasten", "klicke", "setze_text", "waehle"):
            assert f".{methode}(" not in text, methode
        assert "stelle_sicher_dass_lexware_laeuft" not in text


def test_erkunde_kopf_und_firma(erk_settings: Settings, protokoll: Protokoll):
    erk = _erkunde(erk_settings, protokoll, alle_fenster=True)
    assert erk.prozess_laeuft and erk.prozessname == "Framework.exe"
    assert erk.hauptfenster_titel == HAUPTTITEL and erk.aktive_firma == HAUPTTITEL
    assert erk.firma_id == "wik_leh"
    assert [f.art for f in erk.fenster] == ["haupt", "assistent"]
    ohne = _erkunde(erk_settings, protokoll)
    assert len(ohne.fenster) == 1


def test_erkunde_lexware_laeuft_nicht(erk_settings: Settings, protokoll: Protokoll):
    quelle = FakeQuelle("uia", protokoll, laeuft=False)
    with pytest.raises(ek.LexwareNichtGestartet, match="Bitte Lexware starten und die gewünschte Firma öffnen"):
        ek.erkunde(erk_settings, backends=["uia"], quelle_fabrik=lambda _n: quelle)
    assert quelle.aufrufe == ["prozess_laeuft"]  # nicht weiter angedockt, nichts gestartet
    assert protokoll.verboten == []


def test_erkunde_backend_ohne_hauptfenster(erk_settings: Settings, protokoll: Protokoll):
    from auftragsautomat.lexware.ui_automation import FensterNichtGefunden

    class OhneFenster(FakeQuelle):
        def lese_fenster_fuer_erkundung(self, *, alle_fenster: bool = False, timeout: float | None = None) -> list[Fenster]:
            raise FensterNichtGefunden("Hauptfenster nicht gefunden")

    with pytest.raises(ek.LexwareNichtGestartet, match="Bitte Lexware starten"):
        ek.erkunde(erk_settings, backends=["uia"], quelle_fabrik=lambda n: OhneFenster(n, protokoll))
    # beide: win32 fehlt, uia klappt -> Ergebnis mit Hinweis
    erk = ek.erkunde(erk_settings, backends=["uia", "win32"],
                     quelle_fabrik=lambda n: FakeQuelle(n, protokoll) if n == "uia" else OhneFenster(n, protokoll))
    assert erk.fenster and "win32" in erk.backend_fehler


@pytest.mark.skipif(sys.platform == "win32", reason="nur außerhalb von Windows")
def test_erkunde_ohne_windows(erk_settings: Settings):
    from auftragsautomat.lexware.ui_automation import NichtAufWindows

    with pytest.raises(NichtAufWindows):
        ek.erkunde(erk_settings, backends=["uia"])


# ---------------------------------------------------------------------------
# Dateien
# ---------------------------------------------------------------------------


def test_json_txt_und_rundreise(erk_settings: Settings, protokoll: Protokoll, tmp_path: Path):
    erk = _erkunde(erk_settings, protokoll, alle_fenster=True)
    json_pfad = ek.schreibe_baum_json(erk, tmp_path / "e" / "baum.json")
    txt_pfad = ek.schreibe_baum_txt(erk, tmp_path / "e" / "baum.txt")
    daten = json.loads(json_pfad.read_text(encoding="utf-8"))
    assert daten["format"] == ek.BAUM_FORMAT
    kopf = daten["kopf"]
    assert kopf["anzahl_elemente"] == erk.anzahl_elemente and kopf["hinweis"] == ek.DATENSCHUTZ_HINWEIS
    assert kopf["aktive_firma"] == HAUPTTITEL and kopf["backends"] == ["uia"]
    assert {"pfad", "ebene", "control_type", "name", "auto_id", "class_name", "control_id", "sichtbar",
            "aktiviert", "rechteck"} <= set(daten["fenster"][0]["elemente"][0])
    text = txt_pfad.read_text(encoding="utf-8")
    assert "nicht weitergeben" in text and HAUPTTITEL in text
    assert '\n      HeaderItem "Belegnummer"' in text  # Ebene 3 -> 6 Leerzeichen
    assert "[unsichtbar]" in text and "Lesefehler" in text
    geladen = ek.lade_erkundung(json_pfad)
    assert geladen.anzahl_elemente == erk.anzahl_elemente
    assert {e.pfad: e for e in geladen.fenster[1].elemente}["0.1"].auto_id == "btnNext"


def test_lade_erkundung_fehler(tmp_path: Path):
    with pytest.raises(ek.ErkundungsFehler, match="nicht gefunden"):
        ek.lade_erkundung(tmp_path / "fehlt.json")
    kaputt = tmp_path / "kaputt.json"
    kaputt.write_text("{", encoding="utf-8")
    with pytest.raises(ek.ErkundungsFehler, match="JSON"):
        ek.lade_erkundung(kaputt)
    fremd = tmp_path / "fremd.json"
    fremd.write_text('{"format": "anders"}', encoding="utf-8")
    with pytest.raises(ek.ErkundungsFehler, match="Format"):
        ek.lade_erkundung(fremd)


def test_standard_zielordner_unter_logs_und_ignoriert(tmp_path: Path):
    from datetime import datetime

    from auftragsautomat.config import lade_settings

    jetzt = datetime(2026, 10, 29, 8, 15, 30)
    ziel = ek.standard_zielordner(tmp_path / "logs", jetzt)
    assert ziel == tmp_path / "logs" / "lexware_erkundung" / "2026-10-29_081530"
    ziel.mkdir(parents=True)
    assert ek.standard_zielordner(tmp_path / "logs", jetzt).name == "2026-10-29_081530_2"
    # Projektkonfiguration: log_ordner = <Projekt>/logs, und logs/ ist im Git ausgeschlossen
    echte = lade_settings(REPO)
    assert Path(echte.log_ordner) == REPO / "logs"
    assert "logs/*" in (REPO / ".gitignore").read_text(encoding="utf-8").splitlines()


# ---------------------------------------------------------------------------
# Abgleich
# ---------------------------------------------------------------------------


def _abgleich(erk_settings: Settings, protokoll: Protokoll) -> ek.Abgleich:
    erk = _erkunde(erk_settings, protokoll, alle_fenster=True)
    return ek.gleiche_ab(erk, lade_controls(REPO_CONTROLS), backend="uia", controls_datei=REPO_CONTROLS,
                         fenster_titel_regex=erk_settings.lexware.fenster_titel_regex)


def test_abgleich_status(erk_settings: Settings, protokoll: Protokoll):
    abgleich = _abgleich(erk_settings, protokoll)
    nach_name = {e.name: e for e in abgleich.eintraege}
    assert set(nach_name) == {f for f in LexwareControls.__dataclass_fields__}
    s = ek.Status
    assert nach_name["ABBRECHEN"].status == s.GEFUNDEN
    assert nach_name["WEITERFUEHREN_BUTTON"].status == s.GEFUNDEN
    assert nach_name["AUFTRAGSART"].status == s.GEFUNDEN
    assert nach_name["MENGE"].status == s.MEHRDEUTIG and len(nach_name["MENGE"].treffer) == 2
    assert nach_name["SPEICHERN"].status == s.NICHT_GEFUNDEN and "unsichtbar" in nach_name["SPEICHERN"].bemerkung
    assert nach_name["SEITENLISTE"].status == s.GEFUNDEN  # found_index 0
    assert nach_name["DIALOG_TEXT"].status == s.GEFUNDEN  # mehrere Texte erlaubt
    assert nach_name["HAUPTFENSTER_TITEL_REGEX"].status == s.GEFUNDEN
    assert nach_name["SPALTE_BELEGNUMMER"].status == s.GEFUNDEN
    assert nach_name["SPALTE_MENGE"].status == s.GEFUNDEN and "Spaltenkopf" in nach_name["SPALTE_MENGE"].bemerkung
    assert nach_name["SPALTE_PREIS"].status == s.NICHT_GEFUNDEN
    assert nach_name["MENUE_FIRMA_WECHSELN"].status == s.NICHT_PRUEFBAR  # "Datei" da, "Wechseln" erst aufgeklappt
    assert nach_name["TASTE_NEUER_VERKAUFSAUFTRAG"].status == s.NICHT_PRUEFBAR
    assert nach_name["PROZESSNAME"].status == s.GEFUNDEN
    assert nach_name["SCHRITT_POSITIONEN_REGEX"].status == s.GEFUNDEN  # "2 Positionen" in der Seitenliste
    assert abgleich.bilanz_text().startswith(f"{abgleich.anzahl(s.GEFUNDEN)} gefunden, ")


def test_abgleich_vorschlaege(erk_settings: Settings, protokoll: Protokoll):
    nach_name = {e.name: e for e in _abgleich(erk_settings, protokoll).eintraege}
    weiter = nach_name["WEITER"]
    assert weiter.status == ek.Status.NICHT_GEFUNDEN
    assert 1 <= len(weiter.vorschlaege) <= 3
    besten = weiter.vorschlaege[0]
    assert besten.element.name == "Weiter >" and besten.element.auto_id == "btnNext"
    assert besten.yaml_wert() == '{title: "Weiter >", auto_id: "btnNext", control_type: "Button"}'
    kunde = nach_name["KUNDENNUMMER"]
    assert kunde.status == ek.Status.NICHT_GEFUNDEN
    assert any(v.element.auto_id == "edtKundennummer" for v in kunde.vorschlaege)
    assert nach_name["WEITER"].todo is None and nach_name["KUNDENNUMMER"].todo  # TODO-LEXWARE aus der YAML


def test_abgleich_nutzt_yaml_override(erk_settings: Settings, protokoll: Protokoll, tmp_path: Path):
    datei = tmp_path / "lexware_controls.yaml"
    datei.write_text(
        "controls:\n"
        "  # TODO-LEXWARE: Weiter-Knopf\n"
        "  WEITER: {auto_id: \"btnNext\", control_type: \"Button\"}\n"
        "  MENGE: {title: \"Menge\", control_type: \"Edit\", found_index: 1}\n"
        "  ABBRECHEN: \"Abbrechen\"  # TODO-LEXWARE inline\n",
        encoding="utf-8",
    )
    erk = _erkunde(erk_settings, protokoll, alle_fenster=True)
    abgleich = ek.gleiche_ab(erk, lade_controls(datei), backend="uia", controls_datei=datei)
    nach_name = {e.name: e for e in abgleich.eintraege}
    assert nach_name["WEITER"].status == ek.Status.GEFUNDEN and nach_name["WEITER"].todo == "TODO-LEXWARE: Weiter-Knopf"
    assert nach_name["MENGE"].status == ek.Status.GEFUNDEN
    assert nach_name["ABBRECHEN"].todo == "TODO-LEXWARE inline"
    assert ek.todo_eintraege(datei) == {"WEITER": "TODO-LEXWARE: Weiter-Knopf", "ABBRECHEN": "TODO-LEXWARE inline"}


def test_todo_eintraege_der_projektdatei():
    todos = ek.todo_eintraege(REPO_CONTROLS)
    assert "KUNDENNUMMER" in todos and "NACHBEMERKUNG" in todos
    assert "WEITER" not in todos  # [V] belegt
    assert ek.todo_eintraege(None) == {}


def test_spec_passt_wie_pywinauto():
    e = ek.ElementDaten(pfad="0.1", ebene=1, control_type="Button", name="Überspringen", auto_id="b1")
    assert ek._spec_passt(ControlSpec(title_re=".*(Überspringen|Weder).*", control_type="Button"), e)
    assert not ek._spec_passt(ControlSpec(title="überspringen"), e)  # title exakt
    assert ek._spec_passt(ControlSpec(auto_id="b1"), e)
    assert not ek._spec_passt(ControlSpec(title_re="(", control_type="Button"), e)  # ungültiger Regex


def test_abgleich_md(erk_settings: Settings, protokoll: Protokoll, tmp_path: Path):
    erk = _erkunde(erk_settings, protokoll, alle_fenster=True)
    abgleich = ek.gleiche_ab(erk, lade_controls(REPO_CONTROLS), backend="uia", controls_datei=REPO_CONTROLS)
    pfad = ek.schreibe_abgleich_md(erk, [abgleich], tmp_path / "abgleich.md", controls_datei=REPO_CONTROLS)
    text = pfad.read_text(encoding="utf-8")
    assert ek.DATENSCHUTZ_HINWEIS in text
    assert "## Backend `uia`" in text and abgleich.bilanz_text() in text
    assert "#### `WEITER`" in text and 'Vorschlag: `WEITER: {title: "Weiter >"' in text
    assert "### Mehrdeutig" in text and "#### `MENGE`" in text
    assert "### TODO-LEXWARE-Einträge" in text and "| `KUNDENNUMMER` |" in text


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _log_handler_aufraeumen():
    import logging

    yield
    if cli._log_handler is not None:
        logging.getLogger().removeHandler(cli._log_handler)
        cli._log_handler = None


@pytest.fixture()
def cli_settings(erk_settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setattr(cli, "_lade_settings", lambda: erk_settings)
    return erk_settings


@pytest.fixture()
def fake_quelle(monkeypatch: pytest.MonkeyPatch, protokoll: Protokoll) -> list[str]:
    erzeugt: list[str] = []

    def fabrik(_settings: Settings, name: str) -> FakeQuelle:
        erzeugt.append(name)
        return FakeQuelle(name, protokoll)

    monkeypatch.setattr(ek, "_standard_quelle", fabrik)
    return erzeugt


def test_cli_erkunden_standardziel_und_abgleich(cli_settings: Settings, fake_quelle: list[str], protokoll: Protokoll):
    ergebnis = runner.invoke(cli.app, ["lexware-erkunden", "--alle-fenster", "--abgleich"])
    assert ergebnis.exit_code == 0, ergebnis.output
    ordner = list((cli_settings.log_ordner / "lexware_erkundung").iterdir())
    assert len(ordner) == 1
    for name in ("baum.json", "baum.txt", "abgleich.md"):
        assert (ordner[0] / name).is_file()
        assert str(ordner[0] / name) in ergebnis.output
    assert "Abgleich (uia):" in ergebnis.output and " gefunden, " in ergebnis.output and " fehlen, " in ergebnis.output
    assert "mehrdeutig" in ergebnis.output and "nicht weitergeben" in ergebnis.output
    assert "WARNUNG" not in ergebnis.output
    assert fake_quelle == ["uia"] and protokoll.verboten == []


def test_cli_erkunden_beide_backends_und_ziel(cli_settings: Settings, fake_quelle: list[str], tmp_path: Path):
    ziel = tmp_path / "anderswo"
    ergebnis = runner.invoke(cli.app, ["lexware-erkunden", "--backend", "beide", "--ziel", str(ziel), "--tiefe", "1"])
    assert ergebnis.exit_code == 0, ergebnis.output
    assert fake_quelle == ["uia", "win32"]
    daten = json.loads((ziel / "baum.json").read_text(encoding="utf-8"))
    assert daten["kopf"]["backends"] == ["uia", "win32"] and daten["kopf"]["tiefe"] == 1
    assert not (ziel / "abgleich.md").exists()
    assert "WARNUNG" in ergebnis.output  # Ziel nicht unter logs/


def test_cli_unbekanntes_backend(cli_settings: Settings):
    ergebnis = runner.invoke(cli.app, ["lexware-erkunden", "--backend", "java"])
    assert ergebnis.exit_code == 1 and "Unbekanntes Backend" in ergebnis.output


def test_cli_lexware_laeuft_nicht(cli_settings: Settings, monkeypatch: pytest.MonkeyPatch, protokoll: Protokoll):
    monkeypatch.setattr(ek, "_standard_quelle", lambda _s, n: FakeQuelle(n, protokoll, laeuft=False))
    ergebnis = runner.invoke(cli.app, ["lexware-erkunden"])
    assert ergebnis.exit_code != 0
    assert "Bitte Lexware starten und die gewünschte Firma öffnen" in ergebnis.output
    assert ergebnis.exception is None or isinstance(ergebnis.exception, SystemExit)
    assert not (cli_settings.log_ordner / "lexware_erkundung").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="nur außerhalb von Windows")
def test_cli_nicht_auf_windows(cli_settings: Settings):
    ergebnis = runner.invoke(cli.app, ["lexware-erkunden", "--abgleich"])
    assert ergebnis.exit_code == 1
    assert "nur unter Windows" in ergebnis.output and "--aus-datei" in ergebnis.output
    assert "Traceback" not in ergebnis.output
    assert isinstance(ergebnis.exception, SystemExit)


def test_cli_backend_nicht_verfuegbar(cli_settings: Settings, monkeypatch: pytest.MonkeyPatch):
    from auftragsautomat.lexware.ui_automation import BackendNichtVerfuegbar

    def ohne_pywinauto(_s: Settings, _n: str) -> FakeQuelle:
        raise BackendNichtVerfuegbar("pywinauto ist nicht installiert")

    monkeypatch.setattr(ek, "_standard_quelle", ohne_pywinauto)
    ergebnis = runner.invoke(cli.app, ["lexware-erkunden"])
    assert ergebnis.exit_code == 1 and "pywinauto ist nicht installiert" in ergebnis.output


def test_cli_aus_datei_offline(cli_settings: Settings, protokoll: Protokoll, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    erk = _erkunde(cli_settings, protokoll, alle_fenster=True, backends=["uia", "win32"])
    baum = ek.schreibe_baum_json(erk, tmp_path / "frueher" / "baum.json")

    def verboten(*_a: Any) -> None:
        raise AssertionError("--aus-datei darf Lexware nicht ansprechen")

    monkeypatch.setattr(ek, "_standard_quelle", verboten)
    ergebnis = runner.invoke(cli.app, ["lexware-erkunden", "--aus-datei", str(baum)])
    assert ergebnis.exit_code == 0, ergebnis.output
    md = tmp_path / "frueher" / "abgleich.md"
    assert md.is_file() and str(md) in ergebnis.output
    assert "Abgleich (uia):" in ergebnis.output and "Abgleich (win32):" in ergebnis.output
    text = md.read_text(encoding="utf-8")
    assert "## Backend `uia`" in text and "## Backend `win32`" in text and str(baum) in text
    assert not (tmp_path / "frueher" / "baum.txt").exists()

    nur_win32 = runner.invoke(cli.app, ["lexware-erkunden", "--aus-datei", str(baum), "--backend", "win32",
                                         "--ziel", str(tmp_path / "neu")])
    assert nur_win32.exit_code == 0, nur_win32.output
    assert "Abgleich (win32):" in nur_win32.output and "Abgleich (uia):" not in nur_win32.output
    assert (tmp_path / "neu" / "abgleich.md").is_file()


def test_cli_aus_datei_fehler(cli_settings: Settings, tmp_path: Path):
    ergebnis = runner.invoke(cli.app, ["lexware-erkunden", "--aus-datei", str(tmp_path / "fehlt.json")])
    assert ergebnis.exit_code == 1 and "nicht gefunden" in ergebnis.output


# ---------------------------------------------------------------------------
# Review-Ergänzungen: nur lesend auch im echten Backend-Pfad, kritische Vorschläge, win32-control_type
# ---------------------------------------------------------------------------

VERBOTEN_QUELLTEXT = ("click", "click_input", "double_click_input", "right_click_input", "type_keys", "set_focus",
                      "close", "set_text", "set_edit_text", "send_keys", "send_keystrokes", "menu_select", "invoke",
                      "toggle", "select", "start", "starte", "sende_tasten", "klicke", "setze_text", "waehle",
                      "set_value", "drag_mouse_input", "kill")


def test_quelltext_erreichbarer_pfad_ohne_schreibende_aufrufe():
    """Alles, was ``lexware-erkunden`` auf dem echten System aufruft, ist frei von schreibenden Aufrufen."""
    quellen = {
        "erkundung": inspect.getsource(ek),
        "cli.lexware_erkunden": inspect.getsource(cli.lexware_erkunden),
        "lese_fenster_fuer_erkundung": inspect.getsource(PywinautoBackend.lese_fenster_fuer_erkundung),
        "finde_hauptfenster": inspect.getsource(PywinautoBackend.finde_hauptfenster),
        "_verbinde": inspect.getsource(PywinautoBackend._verbinde),
        "prozess_laeuft": inspect.getsource(PywinautoBackend.prozess_laeuft),
        "aktive_firma": inspect.getsource(PywinautoBackend.aktive_firma),
        "__init__": inspect.getsource(PywinautoBackend.__init__),
    }
    for wo, text in quellen.items():
        for methode in VERBOTEN_QUELLTEXT:
            assert f".{methode}(" not in text, f"{wo}: .{methode}("
        assert "subprocess" not in text, wo
        assert "stelle_sicher_dass_lexware_laeuft" not in text, wo
    # pywinauto nur lazy (im Konstruktor von PywinautoBackend), nie beim Import von erkundung/cli
    for modul in (ek, cli):
        for zeile in inspect.getsource(modul).splitlines():
            if not zeile.startswith((" ", "\t")):
                assert "pywinauto" not in zeile or zeile.lstrip().startswith(("#", '"', "*")), zeile


class _PwaProtokoll:
    def __init__(self) -> None:
        self.aufrufe: list[str] = []
        self.verboten: list[str] = []


class _PwaObjekt:
    """Fake für pywinauto-Application/WindowSpecification/Wrapper: erlaubte Methoden explizit, alles andere verboten."""

    ERLAUBT = {"connect", "window", "wait", "wrapper_object", "window_text", "is_process_running"}

    def __init__(self, p: _PwaProtokoll, titel: str = HAUPTTITEL, handle: int = 1) -> None:
        self._p, self._titel, self.handle, self.process = p, titel, handle, 4242

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        p = self.__dict__["_p"]
        if name in self.ERLAUBT:
            def erlaubt(*_a: Any, **k: Any) -> Any:
                p.aufrufe.append(name)
                if name == "window_text":
                    return self._titel
                if name == "is_process_running":
                    return True
                if name in ("window", "wrapper_object"):
                    handle = k.get("handle", self.handle)
                    return _PwaObjekt(p, "Auftragsassistent Verkauf" if handle == 2 else self._titel, handle)
                return None
            return erlaubt
        p.verboten.append(name)
        return lambda *_a, **_k: None


class _FakeElement:
    def __init__(self, handle: int, name: str) -> None:
        self.handle, self.name = handle, name


def test_pywinauto_backend_lese_fenster_nur_lesend():
    """Der echte ``PywinautoBackend``-Pfad (mit pywinauto-Fakes): nur connect/window/wait/wrapper_object/window_text."""
    p = _PwaProtokoll()
    gesucht: dict[str, Any] = {}

    class FakeApplication(_PwaObjekt):
        def __init__(self, backend: str = "uia") -> None:
            super().__init__(p)

    class FakeFindwindows:
        @staticmethod
        def find_elements(**kwargs: Any) -> list[_FakeElement]:
            gesucht.update(kwargs)
            return [_FakeElement(1, HAUPTTITEL), _FakeElement(2, "Auftragsassistent Verkauf")]

    backend = object.__new__(PywinautoBackend)
    backend.__dict__.update(_Application=FakeApplication, _findwindows=FakeFindwindows, backend_name="uia",
                            controls=LexwareControls(), max_wartezeit=0.1, wartezeit=0.05, _app=None)
    fenster = backend.lese_fenster_fuer_erkundung(alle_fenster=True, timeout=0.1)
    assert [(f.titel, f.art) for f in fenster] == [(HAUPTTITEL, "haupt"), ("Auftragsassistent Verkauf", "assistent")]
    assert gesucht["top_level_only"] and gesucht["visible_only"] and gesucht["process"] == 4242
    assert p.verboten == []
    assert set(p.aufrufe) <= _PwaObjekt.ERLAUBT and "connect" in p.aufrufe


def test_vorschlaege_ohne_kritische_aktionen(erk_settings: Settings, protokoll: Protokoll):
    """WEITER darf nie "Speichern"/"Weiterführen" vorgeschlagen bekommen; SPEICHERN selbst schon."""
    nach_name = {e.name: e for e in _abgleich(erk_settings, protokoll).eintraege}
    for name, eintrag in nach_name.items():
        erlaubt = ek._kritische_gruppen([name, eintrag.soll])
        for v in eintrag.vorschlaege:
            assert ek._kritische_gruppen([v.element.name, v.element.auto_id]) <= erlaubt, (name, v.element.name)
    namen = [v.element.name for v in nach_name["WEITER"].vorschlaege]
    assert "Speichern" not in namen and "Weiterführen" not in namen
    katalog = ek._Katalog(_erkunde(erk_settings, protokoll, alle_fenster=True).fenster_von("uia"))
    assert any(v.element.name == "Speichern" for v in katalog.vorschlaege(["Speichern"], bezeichner="SPEICHERN"))
    weiter = katalog.vorschlaege(["Weiter", "Spei"], bezeichner="WEITER", anzahl=50)
    assert weiter and not any(v.element.name in ("Speichern", "Weiterführen") for v in weiter)


def test_win32_control_type_wie_pywinauto(protokoll: Protokoll):
    """win32: control_type bleibt leer (wie HwndElementInfo); friendly_class_name nur getrennt gespeichert."""
    W = FakeWrapper
    wurzel = W(protokoll, None, "Lexware", kinder=[W(protokoll, None, "Weiter", class_name="TButton")])
    baum = ek.erfasse_fenster(Fenster("Lexware", wurzel, "haupt"), backend="win32")
    knopf = baum.elemente[1]
    assert knopf.control_type is None and knopf.klasse_freundlich == "Fake" and knopf.typ == "Fake"
    katalog = ek._Katalog([baum])
    mit_typ = ek._pruefe_control("WEITER", ControlSpec(title="Weiter", control_type="Button"), katalog)
    assert mit_typ.status == ek.Status.NICHT_GEFUNDEN and "nur ohne control_type" in mit_typ.bemerkung
    ohne_typ = ek._pruefe_control("WEITER", ControlSpec(title="Weiter"), katalog)
    assert ohne_typ.status == ek.Status.GEFUNDEN
    # control_type wird wie bei pywinauto exakt verglichen
    e = ek.ElementDaten(pfad="0.1", ebene=1, control_type="Button", name="Weiter")
    assert not ek._spec_passt(ControlSpec(title="Weiter", control_type="button"), e)


def test_erkunde_backend_mit_comfehler(erk_settings: Settings, protokoll: Protokoll):
    class Kaputt(FakeQuelle):
        def lese_fenster_fuer_erkundung(self, *, alle_fenster: bool = False, timeout: float | None = None) -> list[Fenster]:
            raise OSError("COMError -2147220991")

    erk = ek.erkunde(erk_settings, backends=["uia", "win32"],
                     quelle_fabrik=lambda n: FakeQuelle(n, protokoll) if n == "uia" else Kaputt(n, protokoll))
    assert erk.fenster and "COMError" in erk.backend_fehler["win32"]
    with pytest.raises(ek.LexwareNichtGestartet):
        ek.erkunde(erk_settings, backends=["uia"], quelle_fabrik=lambda n: Kaputt(n, protokoll))
