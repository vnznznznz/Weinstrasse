"""Tests für lexware.instanz (Prozessprüfung, Einmalstart, Andocken) ohne echtes Lexware."""

from __future__ import annotations

from pathlib import Path

import pytest

from auftragsautomat.config import Settings
from auftragsautomat.lexware.dialoge import LexwareDoppelstart
from auftragsautomat.lexware.fake_backend import FakeDialog, FakeLexwareBackend
from auftragsautomat.lexware.instanz import (
    InstanzFehler,
    LexwareNichtGestartet,
    aktive_firma,
    lexware_laeuft,
    parse_tasklist,
    stelle_sicher_dass_lexware_laeuft,
)

TASKLIST = (
    '"System Idle Process","0","Services","0","8 K"\n'
    '"explorer.exe","4712","Console","1","120.000 K"\n'
    '"Framework.exe","5220","Console","1","410.512 K"\n'
    '"dbsrv17.exe","1200","Services","0","80.000 K"\n'
)


def test_parse_tasklist():
    assert parse_tasklist(TASKLIST) == ["System Idle Process", "explorer.exe", "Framework.exe", "dbsrv17.exe"]
    assert parse_tasklist('INFO: Es werden keine Tasks ausgeführt...\n') == []
    assert parse_tasklist("") == []


def test_lexware_laeuft_mit_prozessliste():
    assert lexware_laeuft(prozessliste=lambda: parse_tasklist(TASKLIST)) is True
    assert lexware_laeuft(prozessliste=lambda: ["framework.exe"]) is True  # Groß-/Kleinschreibung egal
    assert lexware_laeuft(prozessliste=lambda: ["explorer.exe"]) is False
    assert lexware_laeuft("LxWin.exe", prozessliste=lambda: parse_tasklist(TASKLIST)) is False
    assert lexware_laeuft(prozessliste=list) is False


def test_lexware_laeuft_ausserhalb_windows_false():
    assert lexware_laeuft(plattform="linux") is False
    assert lexware_laeuft(plattform="darwin") is False


def test_aktive_firma():
    fake = FakeLexwareBackend(firmenname="Wine-of-Excellence LEH")
    assert "Wine-of-Excellence LEH" in aktive_firma(fake)

    class OhneTitel(FakeLexwareBackend):
        def aktive_firma(self) -> str:
            return "  "

    with pytest.raises(InstanzFehler):
        aktive_firma(OhneTitel())


def test_laufende_instanz_wird_nicht_neu_gestartet(settings: Settings, tmp_path: Path):
    exe = tmp_path / "Framework.exe"
    exe.write_bytes(b"")
    settings.lexware.exe_pfad = exe
    fake = FakeLexwareBackend(laeuft=True)
    fenster = stelle_sicher_dass_lexware_laeuft(settings, fake)
    assert fenster.art == "haupt"
    assert "Lexware" in fenster.titel
    assert not any(a.startswith("starte:") for a in fake.aktionen)
    assert fake.gestartet_mit is None


def test_nicht_laufend_ohne_exe_pfad(settings: Settings):
    settings.lexware.exe_pfad = None
    fake = FakeLexwareBackend(laeuft=False)
    with pytest.raises(LexwareNichtGestartet, match="exe_pfad"):
        stelle_sicher_dass_lexware_laeuft(settings, fake)
    assert fake.aktionen == []


def test_nicht_laufend_exe_fehlt(settings: Settings, tmp_path: Path):
    settings.lexware.exe_pfad = tmp_path / "gibt_es_nicht.exe"
    fake = FakeLexwareBackend(laeuft=False)
    with pytest.raises(LexwareNichtGestartet, match="existiert nicht"):
        stelle_sicher_dass_lexware_laeuft(settings, fake)


def test_start_wenn_nicht_laufend(settings: Settings, tmp_path: Path):
    exe = tmp_path / "Framework.exe"
    exe.write_bytes(b"")
    settings.lexware.exe_pfad = exe
    fake = FakeLexwareBackend(laeuft=False)
    fenster = stelle_sicher_dass_lexware_laeuft(settings, fake)
    assert fake.gestartet_mit == exe
    assert fake.laeuft is True
    assert fenster.art == "haupt"
    assert fake.aktionen[0] == f"starte:{exe}"


def test_einmalstart_meldung_beim_start(settings: Settings, tmp_path: Path):
    exe = tmp_path / "Framework.exe"
    exe.write_bytes(b"")
    settings.lexware.exe_pfad = exe
    fake = FakeLexwareBackend(laeuft=False, doppelstart_beim_starten=True)
    bilder: list[str] = []

    def screenshot(name: str) -> Path:
        bilder.append(name)
        p = tmp_path / f"{name}.png"
        p.write_text("x")
        return p

    with pytest.raises(LexwareDoppelstart) as exc:
        stelle_sicher_dass_lexware_laeuft(settings, fake, screenshot=screenshot)
    assert exc.value.screenshot is not None and exc.value.screenshot.exists()
    assert bilder == ["dialog_einmalstart"]
    assert "klicke:DIALOG_OK" not in fake.aktionen


def test_unbekannter_dialog_beim_andocken(settings: Settings):
    fake = FakeLexwareBackend(laeuft=True, dialoge_beim_start=[
        FakeDialog("Anmeldung", "Bitte Benutzername und Kennwort eingeben.", buttons=("DIALOG_OK", "DIALOG_ABBRECHEN")),
    ])
    from auftragsautomat.lexware.dialoge import UnbekannterDialog

    with pytest.raises(UnbekannterDialog, match="Anmeldung"):
        stelle_sicher_dass_lexware_laeuft(settings, fake)
    assert not any(a.startswith("klicke:") for a in fake.aktionen)
