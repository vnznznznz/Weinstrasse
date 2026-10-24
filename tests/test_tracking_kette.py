"""Tests für tracking/kette.py: Reihenfolge, erste bekannte Quelle gewinnt, Hinweise, Rückschritte."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from auftragsautomat.config import Settings
from auftragsautomat.models import Meilenstein, Sendung, SendungStatus
from auftragsautomat.tracking.basis import lade_hillebrand_konfig
from auftragsautomat.tracking.hillebrand_api import ApiQuelle
from auftragsautomat.tracking.hillebrand_mail import MailQuelle, erkenne_ereignis_aus_datei
from auftragsautomat.tracking.hillebrand_portal import PortalQuelle
from auftragsautomat.tracking.hillebrand_public import PublicTrackingQuelle
from auftragsautomat.tracking.kette import TrackingKette, erzeuge_kette


class FakeQuelle:
    def __init__(
        self,
        name: str,
        status: SendungStatus | None = None,
        *,
        verfuegbar: bool = True,
        hinweis: str | None = None,
        meilensteine: list[Meilenstein] | None = None,
        fehler: Exception | None = None,
    ) -> None:
        self.name = name
        self._status = status
        self._verfuegbar = verfuegbar
        self._hinweis = hinweis
        self._meilensteine = meilensteine or []
        self._fehler = fehler
        self.aufrufe = 0

    def verfuegbar(self) -> bool:
        if isinstance(self._verfuegbar, Exception):
            raise self._verfuegbar
        return self._verfuegbar

    def aktualisiere(self, sendung: Sendung) -> Sendung:
        self.aufrufe += 1
        if self._fehler is not None:
            raise self._fehler
        hinweise = list(sendung.hinweise) + ([self._hinweis] if self._hinweis else [])
        if self._status is None:
            return sendung.model_copy(update={"hinweise": hinweise, "letzte_pruefung": datetime.now()})
        return sendung.model_copy(
            update={
                "status": self._status,
                "quelle": self.name,  # Vertrag: bei Erfolg quelle = name
                "meilensteine": list(self._meilensteine),
                "hinweise": hinweise,
                "letzte_pruefung": datetime.now(),
            }
        )


def _sendung(**felder) -> Sendung:
    return Sendung(spedition="Hillebrand", referenz="DEMB00000", **felder)


def test_erste_bekannte_quelle_gewinnt() -> None:
    a = FakeQuelle("mail", None, hinweis="keine Mail")
    b = FakeQuelle("api", SendungStatus.BESTAETIGT, hinweis="API ok")
    c = FakeQuelle("public_tracking", SendungStatus.ZUGESTELLT)
    kette = TrackingKette([a, b, c])
    assert kette.namen == ["mail", "api", "public_tracking"]
    assert kette.verfuegbar()
    original = _sendung()
    ergebnis = kette.aktualisiere(original)
    assert ergebnis.status == SendungStatus.BESTAETIGT
    assert ergebnis.quelle == "api"
    assert ergebnis.hinweise == ["keine Mail", "API ok"]
    assert (a.aufrufe, b.aufrufe, c.aufrufe) == (1, 1, 0)
    assert original.status == SendungStatus.UNBEKANNT and original.hinweise == []


def test_nicht_verfuegbare_quelle_wird_uebersprungen() -> None:
    a = FakeQuelle("api", SendungStatus.ZUGESTELLT, verfuegbar=False)
    b = FakeQuelle("mail", SendungStatus.BESTAETIGT)
    ergebnis = TrackingKette([a, b]).aktualisiere(_sendung())
    assert ergebnis.status == SendungStatus.BESTAETIGT
    assert ergebnis.hinweise == ["Quelle 'api' nicht verfügbar"]
    assert a.aufrufe == 0
    kaputt = FakeQuelle("portal", SendungStatus.ZUGESTELLT, verfuegbar=RuntimeError("x"))  # type: ignore[arg-type]
    assert not TrackingKette([kaputt]).verfuegbar()


def test_alle_unbekannt() -> None:
    kette = TrackingKette([FakeQuelle("mail", None, hinweis="h1"), FakeQuelle("api", None, hinweis="h2"), FakeQuelle("api", None, hinweis="h2")])
    ergebnis = kette.aktualisiere(_sendung(hinweise=["alt"]))
    assert ergebnis.status == SendungStatus.UNBEKANNT
    assert ergebnis.quelle == "keine"
    assert ergebnis.hinweise == ["alt", "h1", "h2"]
    assert ergebnis.letzte_pruefung is not None
    assert TrackingKette([]).aktualisiere(_sendung()).status == SendungStatus.UNBEKANNT


def test_rueckschritt_gewinnt_nicht() -> None:
    alt = _sendung(status=SendungStatus.ABGEHOLT, quelle="api", meilensteine=[Meilenstein(code="shpsupdepcnf", text="x")])
    mail = FakeQuelle("mail", SendungStatus.BESTAETIGT, meilensteine=[Meilenstein(code="bestaetigung", text="b")])
    api = FakeQuelle("api", SendungStatus.UNTERWEGS)
    ergebnis = TrackingKette([mail, api]).aktualisiere(alt)
    assert ergebnis.status == SendungStatus.UNTERWEGS
    assert ergebnis.quelle == "api"
    assert any("bisheriger Stand 'abgeholt' bleibt" in h and "'mail'" in h for h in ergebnis.hinweise)
    # keine weitere Quelle: alter Status bleibt, Meilensteine werden vereinigt
    ergebnis = TrackingKette([mail, FakeQuelle("api", None)]).aktualisiere(alt)
    assert ergebnis.status == SendungStatus.ABGEHOLT
    assert ergebnis.quelle == "api"
    assert {m.code for m in ergebnis.meilensteine} == {"shpsupdepcnf", "bestaetigung"}
    assert ergebnis.letzte_pruefung is not None


def test_ampelzustaende_gewinnen_immer() -> None:
    alt = _sendung(status=SendungStatus.ABGEHOLT, quelle="api")
    ergebnis = TrackingKette([FakeQuelle("api", SendungStatus.VERSPAETET)]).aktualisiere(alt)
    assert ergebnis.status == SendungStatus.VERSPAETET
    alt = _sendung(status=SendungStatus.VERSPAETET, quelle="api")
    ergebnis = TrackingKette([FakeQuelle("mail", SendungStatus.BESTAETIGT)]).aktualisiere(alt)
    assert ergebnis.status == SendungStatus.BESTAETIGT  # von "verspaetet" aus ist jeder Stand ein Ergebnis


def test_unveraenderte_rueckgabe_ist_kein_ergebnis() -> None:
    """Eine Quelle, die die Sendung nur mit Hinweis zurückgibt, gewinnt nicht – auch bei bekanntem Status."""
    alt = _sendung(status=SendungStatus.ABGEHOLT, quelle="api")
    ergebnis = TrackingKette([FakeQuelle("api", None, hinweis="HTTP 503"), FakeQuelle("mail", SendungStatus.ZUGESTELLT)]).aktualisiere(alt)
    assert ergebnis.status == SendungStatus.ZUGESTELLT and ergebnis.quelle == "mail"
    assert ergebnis.hinweise == ["HTTP 503"]
    ergebnis = TrackingKette([FakeQuelle("api", None, hinweis="HTTP 503")]).aktualisiere(alt)
    assert ergebnis.status == SendungStatus.ABGEHOLT and ergebnis.quelle == "api"
    assert ergebnis.hinweise == ["HTTP 503"]


def test_exception_in_quelle_wird_gefangen() -> None:
    kette = TrackingKette([FakeQuelle("mail", fehler=RuntimeError("Boom")), FakeQuelle("api", SendungStatus.BEAUFTRAGT)])
    ergebnis = kette.aktualisiere(_sendung())
    assert ergebnis.status == SendungStatus.BEAUFTRAGT
    assert ergebnis.hinweise == ["Quelle 'mail': RuntimeError: Boom"]


def test_erzeuge_kette_laut_settings(settings: Settings) -> None:
    settings.tracking.quellen = ["portal", "public_tracking", "api", "mail", "unbekannt"]  # type: ignore[list-item]
    kette = erzeuge_kette(settings, [], umgebung={})
    assert kette.namen == ["portal", "public_tracking", "api", "mail"]
    assert isinstance(kette.quellen[0], PortalQuelle)
    assert isinstance(kette.quellen[1], PublicTrackingQuelle)
    assert isinstance(kette.quellen[2], ApiQuelle)
    assert isinstance(kette.quellen[3], MailQuelle)
    assert not kette.quellen[2].verfuegbar()  # keine Umgebungsvariablen
    settings.tracking.quellen = []
    assert erzeuge_kette(settings).namen == []


def test_erzeuge_kette_ende_zu_ende(settings: Settings, fixtures_ordner: Path) -> None:
    konfig = lade_hillebrand_konfig(settings.tracking.hillebrand_konfig)
    mails = fixtures_ordner / "tracking" / "mails"
    ereignisse = [e for e in (erkenne_ereignis_aus_datei(f, konfig) for f in sorted(mails.glob("*.eml"))) if e is not None]
    html = (fixtures_ordner / "tracking" / "html" / "public_tracking.html").read_text(encoding="utf-8")
    geladen: list[str] = []

    def lade(url: str) -> str:
        geladen.append(url)
        return html

    settings.tracking.quellen = ["mail", "api", "public_tracking"]
    kette = erzeuge_kette(settings, ereignisse, konfig=konfig, umgebung={}, lade_seite_public=lade)

    aus_mail = kette.aktualisiere(_sendung())
    assert aus_mail.quelle == "mail" and aus_mail.status == SendungStatus.ZUGESTELLT
    assert geladen == []

    ohne_mail = kette.aktualisiere(Sendung(spedition="Hillebrand", referenz="DEMB55555"))
    assert ohne_mail.quelle == "public_tracking"
    assert ohne_mail.status == SendungStatus.ZUSTELLUNG_GEPLANT
    assert "Keine Hillebrand-Mail zu DEMB55555 gefunden" in ohne_mail.hinweise
    assert "Quelle 'api' nicht verfügbar" in ohne_mail.hinweise
    assert geladen == [konfig.public_url("DEMB55555")]


def test_kette_ist_selbst_eine_quelle() -> None:
    from auftragsautomat.tracking.basis import TrackingQuelle

    assert isinstance(TrackingKette([]), TrackingQuelle)


@pytest.mark.parametrize("name", ["mail", "api", "public_tracking", "portal"])
def test_einzelquellen_erfuellen_protokoll(settings: Settings, name: str) -> None:
    from auftragsautomat.tracking.basis import TrackingQuelle

    settings.tracking.quellen = [name]  # type: ignore[list-item]
    quelle = erzeuge_kette(settings, [], umgebung={}).quellen[0]
    assert isinstance(quelle, TrackingQuelle)
    assert quelle.name == name
