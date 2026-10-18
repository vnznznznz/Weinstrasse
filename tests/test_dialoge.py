"""Tests für lexware.dialoge (Regeln und Behandlung mit dem FakeLexwareBackend)."""

from __future__ import annotations

from pathlib import Path

import pytest

from auftragsautomat.lexware.dialoge import (
    BEKANNTE_DIALOGE,
    DialogAktion,
    DialogFehler,
    DialogKontext,
    DialogRegel,
    LexwareDoppelstart,
    UnbekannterDialog,
    behandle_dialoge,
    finde_regel,
)
from auftragsautomat.lexware.fake_backend import FakeDialog, FakeLexwareBackend


@pytest.mark.parametrize(
    "titel, text, regel, control",
    [
        ("Bestellung drucken", "Möchten Sie die Bestellung ausdrucken oder per E-Mail versenden?", "druck_email", "DIALOG_UEBERSPRINGEN"),
        ("Ausgabe", "Auftrag per E-Mail verschicken?", "druck_email", "DIALOG_UEBERSPRINGEN"),
        ("USt-IdNr.", "Soll die USt-IdNr. des Lieferanten geprüft werden?", "ust_idnr", "DIALOG_NEIN"),
        ("Hinweis", "Die Umsatzsteuer-ID konnte nicht geprüft werden.", "ust_idnr", "DIALOG_NEIN"),
        ("Lexware", "Die Anwendung kann nur einmal gestartet werden.", "einmalstart", None),
        ("Abbrechen", "Möchten Sie die Änderungen wirklich verwerfen?", "aenderungen_verwerfen", "DIALOG_JA"),
        ("Frage", "Soll der Auftrag als eRechnung verarbeitet werden?", "erechnung_poststelle_internetmarke", "DIALOG_NEIN"),
        ("Frage", "Auftrag jetzt festschreiben?", "festschreiben", "DIALOG_NEIN"),
        ("Buchen", "Soll der Auftrag gebucht werden?", "auftrag_buchen", "DIALOG_NEIN"),
        ("Hinweis", "Für diesen Kunden besteht ein Lieferstopp.", "lieferstopp", None),
        ("Hinweis", "Der Kreditrahmen wird überschritten.", "kreditrahmen", None),
    ],
)
def test_finde_regel(titel, text, regel, control):
    gefunden = finde_regel(titel, text)
    assert gefunden is not None, (titel, text)
    assert gefunden.name == regel
    assert gefunden.control == control
    if control is None:
        assert gefunden.aktion is DialogAktion.FEHLER


def test_finde_regel_unbekannt():
    assert finde_regel("Hinweis", "Der Lagerbestand reicht nicht aus.") is None


def test_regeln_vollstaendig():
    for regel in BEKANNTE_DIALOGE:
        assert regel.titel_regex or regel.text_regex, regel.name
        if regel.aktion is DialogAktion.KLICKEN:
            assert regel.control, regel.name
    assert "DIALOG_JA" not in {r.control for r in BEKANNTE_DIALOGE if not r.nur_kontext}, "'Ja' nie ohne Kontext"


def test_keine_dialoge_liefert_leeres_protokoll():
    fake = FakeLexwareBackend()
    assert behandle_dialoge(fake, DialogKontext.ASSISTENT) == []
    assert fake.aktionen == []


def test_druck_und_ust_dialog_werden_beantwortet(tmp_path: Path):
    fake = FakeLexwareBackend(dialoge_beim_start=[
        FakeDialog("Bestellung drucken", "Bestellung ausdrucken oder per E-Mail versenden?", buttons=("DIALOG_DRUCKEN", "DIALOG_UEBERSPRINGEN")),
        FakeDialog("USt-IdNr.", "USt-IdNr. des Lieferanten prüfen?", buttons=("DIALOG_JA", "DIALOG_NEIN")),
    ])
    protokoll = behandle_dialoge(fake, DialogKontext.NACH_SPEICHERN, screenshots_ordner=tmp_path)
    assert fake.dialoge == []
    assert fake.aktionen == ["klicke:DIALOG_UEBERSPRINGEN", "klicke:DIALOG_NEIN"]
    assert len(protokoll) == 2
    assert "druck_email" in protokoll[0] and "ust_idnr" in protokoll[1]
    assert not list(tmp_path.iterdir())  # kein Screenshot nötig


def test_unbekannter_dialog_wird_nicht_bestaetigt(tmp_path: Path):
    fake = FakeLexwareBackend(dialoge_beim_start=[
        FakeDialog("Hinweis", "Der Lagerbestand reicht nicht aus. Trotzdem fortfahren?", buttons=("DIALOG_JA", "DIALOG_NEIN")),
    ])
    with pytest.raises(UnbekannterDialog) as exc:
        behandle_dialoge(fake, DialogKontext.ASSISTENT, screenshots_ordner=tmp_path)
    assert "Lagerbestand" in str(exc.value)
    assert exc.value.titel == "Hinweis"
    assert exc.value.screenshot is not None and exc.value.screenshot.exists()
    assert exc.value.screenshot.parent == tmp_path
    assert not any(a.startswith("klicke:") for a in fake.aktionen)
    assert len(fake.dialoge) == 1  # Dialog bleibt offen


def test_verwerfen_nur_im_dry_run_abbruch(tmp_path: Path):
    def fake_mit_dialog() -> FakeLexwareBackend:
        return FakeLexwareBackend(dialoge_beim_start=[FakeDialog("Abbrechen", "Änderungen verwerfen?")])

    fake = fake_mit_dialog()
    with pytest.raises(UnbekannterDialog, match="nicht vorgesehen"):
        behandle_dialoge(fake, DialogKontext.ASSISTENT, screenshots_ordner=tmp_path)
    assert "klicke:DIALOG_JA" not in fake.aktionen

    fake = fake_mit_dialog()
    protokoll = behandle_dialoge(fake, DialogKontext.DRY_RUN_ABBRUCH)
    assert fake.aktionen == ["klicke:DIALOG_JA"]
    assert protokoll and "aenderungen_verwerfen" in protokoll[0]


def test_einmalstart_meldung_wirft_doppelstart(tmp_path: Path):
    fake = FakeLexwareBackend(dialoge_beim_start=[
        FakeDialog("Lexware", "Die Anwendung kann nur einmal gestartet werden.", buttons=("DIALOG_OK",)),
    ])
    with pytest.raises(LexwareDoppelstart) as exc:
        behandle_dialoge(fake, DialogKontext.START, screenshots_ordner=tmp_path)
    assert exc.value.screenshot is not None and exc.value.screenshot.exists()
    assert "klicke:DIALOG_OK" not in fake.aktionen


def test_lieferstopp_ist_fehler():
    fake = FakeLexwareBackend(dialoge_beim_start=[FakeDialog("Hinweis", "Für diesen Kunden besteht ein Lieferstopp!", buttons=("DIALOG_OK",))])
    with pytest.raises(DialogFehler, match="Lieferstopp"):
        behandle_dialoge(fake, DialogKontext.ASSISTENT)
    assert fake.aktionen == []


def test_screenshot_callable_wird_genutzt(tmp_path: Path):
    aufrufe: list[str] = []

    def bild(name: str) -> Path:
        aufrufe.append(name)
        pfad = tmp_path / f"{name}.png"
        pfad.write_text("x")
        return pfad

    fake = FakeLexwareBackend(dialoge_beim_start=[FakeDialog("Frage", "Irgendetwas Unerwartetes?")])
    with pytest.raises(UnbekannterDialog) as exc:
        behandle_dialoge(fake, "assistent", screenshot=bild)
    assert aufrufe == ["unbekannter_dialog"]
    assert exc.value.screenshot == tmp_path / "unbekannter_dialog.png"


def test_warnung_der_regel_landet_im_protokoll():
    fake = FakeLexwareBackend(dialoge_beim_start=[FakeDialog("Festschreiben", "Auftrag jetzt festschreiben?")])
    protokoll = behandle_dialoge(fake, DialogKontext.NACH_SPEICHERN)
    assert fake.aktionen == ["klicke:DIALOG_NEIN"]
    assert any("nicht festgeschrieben" in z for z in protokoll)


def test_dialog_der_sich_nicht_schliesst_wird_gemeldet(tmp_path: Path):
    class HartnaeckigerFake(FakeLexwareBackend):
        def klicke(self, dialog, control):  # type: ignore[override]
            self.aktionen.append(f"klicke:{control}")  # Dialog bleibt offen

    fake = HartnaeckigerFake(dialoge_beim_start=[FakeDialog("USt-IdNr.", "USt-IdNr. prüfen?")])
    with pytest.raises(DialogFehler, match="bleibt"):
        behandle_dialoge(fake, DialogKontext.NACH_SPEICHERN, screenshots_ordner=tmp_path)
    assert fake.aktionen.count("klicke:DIALOG_NEIN") == 2


def test_eigene_regeln_parameter():
    regeln = [DialogRegel(name="test", aktion=DialogAktion.KLICKEN, control="DIALOG_OK", text_regex="alles klar")]
    fake = FakeLexwareBackend(dialoge_beim_start=[FakeDialog("Info", "Alles klar?", buttons=("DIALOG_OK",))])
    assert behandle_dialoge(fake, DialogKontext.ASSISTENT, regeln=regeln)
    assert fake.aktionen == ["klicke:DIALOG_OK"]
