"""Tests für tracking/hillebrand_mail.py mit synthetischen Mails (tests/fixtures/tracking/mails)."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from auftragsautomat.models import Ereignistyp, MailEreignis, Sendung, SendungStatus
from auftragsautomat.tracking.basis import HillebrandKonfig, lade_hillebrand_konfig
from auftragsautomat.tracking.hillebrand_mail import (
    CODE_BESTAETIGUNG,
    MailQuelle,
    erkenne_ereignis,
    erkenne_ereignis_aus_datei,
    erkenne_ereignisse,
    meilensteine_aus_ereignissen,
)


@pytest.fixture()
def konfig() -> HillebrandKonfig:
    return lade_hillebrand_konfig()


@pytest.fixture()
def mails(fixtures_ordner: Path) -> Path:
    ordner = fixtures_ordner / "tracking" / "mails"
    assert ordner.is_dir()
    return ordner


@pytest.fixture()
def ereignisse(mails: Path, konfig: HillebrandKonfig) -> list[MailEreignis]:
    gefunden = [erkenne_ereignis_aus_datei(f, konfig) for f in sorted(mails.glob("*.eml"))]
    return [e for e in gefunden if e is not None]


# ---------------------------------------------------------------- Erkennung aus .eml


def test_bestaetigung_aus_eml(mails: Path, konfig: HillebrandKonfig) -> None:
    e = erkenne_ereignis_aus_datei(mails / "bestaetigung.eml", konfig)
    assert e is not None
    assert e.typ == Ereignistyp.SPEDITION_BESTAETIGUNG
    assert e.referenz == "DEMB00000"
    assert e.kundenreferenz == "WS-2026-0042"
    assert e.neues_datum == date(2026, 10, 27)  # "laut Transporeon ... am 27.10." + Jahr aus dem Empfang
    assert e.konfidenz == 1.0
    assert e.absender == "dispo.beispiel@hillebrandgori.com"
    assert e.empfangen.date() == date(2026, 10, 19)
    assert e.mail_id and e.mail_id.endswith("bestaetigung.eml")
    assert e.text_auszug and "Transporeon" in e.text_auszug


def test_bestaetigung_html_mit_jahreswechsel(mails: Path, konfig: HillebrandKonfig) -> None:
    e = erkenne_ereignis_aus_datei(mails / "bestaetigung_html.eml", konfig)
    assert e is not None
    assert e.typ == Ereignistyp.SPEDITION_BESTAETIGUNG
    assert e.referenz == "DEMB00001"
    assert e.kundenreferenz == "WS-2026-0043"
    assert e.neues_datum == date(2027, 1, 5)  # Mail im Dezember, Termin im Januar


def test_abholung_und_zustellung(mails: Path, konfig: HillebrandKonfig) -> None:
    abholung = erkenne_ereignis_aus_datei(mails / "abholung.eml", konfig)
    assert abholung is not None
    assert abholung.typ == Ereignistyp.ABHOLUNG
    assert abholung.referenz == "DEMB00000"
    assert abholung.konfidenz == pytest.approx(0.85)  # Referenz ohne "Our Ref", aber bekannte Domain
    assert abholung.text_auszug and "Treviso" in abholung.text_auszug
    zustellung = erkenne_ereignis_aus_datei(mails / "zustellung.eml", konfig)
    assert zustellung is not None
    assert zustellung.typ == Ereignistyp.ZUSTELLUNG
    assert zustellung.referenz == "DEMB00000"


def test_terminaenderung(mails: Path, konfig: HillebrandKonfig) -> None:
    e = erkenne_ereignis_aus_datei(mails / "terminaenderung.eml", konfig)
    assert e is not None
    assert e.typ == Ereignistyp.TERMINAENDERUNG
    assert e.referenz == "DEMB00000"
    assert e.neues_datum == date(2026, 11, 3)
    assert e.konfidenz == 1.0


def test_fremde_mail_wird_ignoriert(mails: Path, konfig: HillebrandKonfig) -> None:
    assert erkenne_ereignis_aus_datei(mails / "fremd.eml", konfig) is None


def test_datei_nicht_lesbar(tmp_path: Path, konfig: HillebrandKonfig) -> None:
    assert erkenne_ereignis_aus_datei(tmp_path / "fehlt.eml", konfig) is None
    assert erkenne_ereignis_aus_datei(tmp_path / "x.pdf", konfig) is None


# ---------------------------------------------------------------- andere Eingabeformen


def test_json_mails_dict_form(mails: Path, konfig: HillebrandKonfig) -> None:
    daten = json.loads((mails / "mails.json").read_text(encoding="utf-8"))
    gefunden = erkenne_ereignisse(daten, konfig)
    assert [e.typ for e in gefunden] == [Ereignistyp.SPEDITION_BESTAETIGUNG, Ereignistyp.SONSTIGES]
    assert gefunden[0].referenz == "DEMB00002"
    assert gefunden[0].kundenreferenz == "WIK-2026-0100"
    assert gefunden[0].neues_datum == date(2026, 10, 30)
    assert gefunden[0].mail_id == "AAMk-fixture-1"
    # dritte Mail: nur HTML-Text, englische Schlüssel (subject/from/received)
    assert gefunden[1].referenz == "DEMB00002"
    assert gefunden[1].text_auszug and "ready for transport" in gefunden[1].text_auszug
    assert gefunden[1].empfangen == datetime(2026, 10, 21, 7, 45)


def test_tupel_form(konfig: HillebrandKonfig) -> None:
    e = erkenne_ereignis(
        (
            "Auftragsbestätigung; Your Ref: WS-1; Our Ref: DEMB00005[jobDEM00000005]",
            "Dispo <dispo.beispiel@hillebrandgori.com>",
            "laut Transporeon soll am 2.11. angeliefert werden",
            datetime(2026, 10, 20, 9, 0),
            "ID-1",
        ),
        konfig,
    )
    assert e is not None
    assert (e.referenz, e.kundenreferenz, e.neues_datum, e.mail_id) == ("DEMB00005", "WS-1", date(2026, 11, 2), "ID-1")


def test_m365_mail_objekt_mit_kopf(konfig: HillebrandKonfig) -> None:
    mail = SimpleNamespace(
        kopf=SimpleNamespace(
            id="AAMk-9",
            betreff="Shipment DEMB00006 has been delivered",
            absender="noreply.beispiel@hillebrandgori.com",
            absender_name="myHillebrandGori",
            empfangen=datetime(2026, 10, 27, 15, 0),
        ),
        text=None,
        html="<p>Shipment DEMB00006 has been delivered to Beispielstadt on 27 Oct 2026.</p>",
    )
    e = erkenne_ereignis(mail, konfig)
    assert e is not None
    assert e.typ == Ereignistyp.ZUSTELLUNG
    assert e.referenz == "DEMB00006"
    assert e.mail_id == "AAMk-9"
    assert e.empfangen == datetime(2026, 10, 27, 15, 0)
    assert e.text_auszug and "Beispielstadt" in e.text_auszug


def test_unbekannter_absender_nur_mit_referenz(konfig: HillebrandKonfig) -> None:
    mit = erkenne_ereignis(("Info", "kollege@example.org", "FYI: Our Ref: DEMB00007 ist verladen", datetime(2026, 10, 1)), konfig)
    assert mit is not None
    assert mit.typ == Ereignistyp.SONSTIGES
    assert mit.referenz == "DEMB00007"
    assert mit.konfidenz <= 0.8
    ohne = erkenne_ereignis(("Info", "kollege@example.org", "Hallo", datetime(2026, 10, 1)), konfig)
    assert ohne is None


def test_erkenne_ereignisse_ueberspringt_kaputte(konfig: HillebrandKonfig) -> None:
    gueltig = ("Terminänderung; Our Ref: DEMB00008", "dispo.beispiel@hillebrandgori.com", "verschoben auf den 05.11.", datetime(2026, 10, 1))
    ergebnis = erkenne_ereignisse([("zu", "kurz"), None, 42, gueltig], konfig)
    assert len(ergebnis) == 1
    assert ergebnis[0].neues_datum == date(2026, 11, 5)


# ---------------------------------------------------------------- Meilensteine und MailQuelle


def test_meilensteine_aus_ereignissen(ereignisse: list[MailEreignis], konfig: HillebrandKonfig) -> None:
    meilensteine = meilensteine_aus_ereignissen([e for e in ereignisse if e.referenz == "DEMB00000"], konfig)
    codes = [m.code for m in meilensteine]
    assert CODE_BESTAETIGUNG in codes
    assert "shpsupdepcnf" in codes and "shpcusarrcnf" in codes
    geplante = [m for m in meilensteine if m.geplant]
    assert {m.code for m in geplante} == {"shpcusarrpln", "shpcusarrplnchg"}
    abholung = next(m for m in meilensteine if m.code == "shpsupdepcnf")
    assert abholung.zeitpunkt == datetime(2026, 10, 22)
    assert meilensteine == sorted(meilensteine, key=lambda m: (m.zeitpunkt or datetime.min, m.geplant))


def test_mailquelle_zuordnung_ueber_referenz(ereignisse: list[MailEreignis], konfig: HillebrandKonfig) -> None:
    quelle = MailQuelle(ereignisse, konfig)
    assert quelle.name == "mail"
    assert quelle.verfuegbar()
    original = Sendung(spedition="Hillebrand", referenz="demb00000")
    ergebnis = quelle.aktualisiere(original)
    assert ergebnis is not original
    assert original.status == SendungStatus.UNBEKANNT
    assert ergebnis.status == SendungStatus.ZUGESTELLT
    assert ergebnis.quelle == "mail"
    assert ergebnis.kundenreferenz == "WS-2026-0042"
    assert ergebnis.letzte_pruefung is not None
    assert ergebnis.geplante_zustellung == date(2026, 11, 3)  # letzte Terminänderung
    assert len(ergebnis.meilensteine) == 5
    assert ergebnis.hinweise == []


def test_mailquelle_nur_bestaetigung(ereignisse: list[MailEreignis], konfig: HillebrandKonfig) -> None:
    nur = [e for e in ereignisse if e.typ == Ereignistyp.SPEDITION_BESTAETIGUNG and e.referenz == "DEMB00000"]
    ergebnis = MailQuelle(nur, konfig).aktualisiere(Sendung(spedition="Hillebrand", referenz="DEMB00000"))
    assert ergebnis.status == SendungStatus.BESTAETIGT  # geplante Zustellung hebt erst nach Abholung
    assert ergebnis.geplante_zustellung == date(2026, 10, 27)
    assert [m.code for m in ergebnis.meilensteine] == [CODE_BESTAETIGUNG, "shpcusarrpln"]


def test_mailquelle_zuordnung_ueber_kundenreferenz(ereignisse: list[MailEreignis], konfig: HillebrandKonfig) -> None:
    ergebnis = MailQuelle(ereignisse, konfig).aktualisiere(Sendung(spedition="Hillebrand", kundenreferenz="ws-2026-0042"))
    assert ergebnis.referenz == "DEMB00000"
    assert ergebnis.status != SendungStatus.UNBEKANNT


def test_mailquelle_ohne_treffer(ereignisse: list[MailEreignis], konfig: HillebrandKonfig) -> None:
    sendung = Sendung(spedition="Hillebrand", referenz="DEMB99999", status=SendungStatus.BEAUFTRAGT)
    ergebnis = MailQuelle(ereignisse, konfig).aktualisiere(sendung)
    assert ergebnis.status == SendungStatus.BEAUFTRAGT
    assert ergebnis.quelle == "keine"
    assert ergebnis.hinweise == ["Keine Hillebrand-Mail zu DEMB99999 gefunden"]
    assert MailQuelle(ereignisse, konfig).passende_ereignisse(sendung) == []


def test_mailquelle_ohne_referenz(konfig: HillebrandKonfig) -> None:
    ergebnis = MailQuelle([], konfig).aktualisiere(Sendung(spedition="Hillebrand"))
    assert "weder Referenz noch Kundenreferenz" in ergebnis.hinweise[0]


def test_mailquelle_verspaetet(ereignisse: list[MailEreignis], konfig: HillebrandKonfig) -> None:
    nur = [e for e in ereignisse if e.typ == Ereignistyp.SPEDITION_BESTAETIGUNG and e.referenz == "DEMB00000"]
    ergebnis = MailQuelle(nur, konfig).aktualisiere(Sendung(spedition="Hillebrand", referenz="DEMB00000", puenktlich=False))
    assert ergebnis.status == SendungStatus.VERSPAETET


def test_mailquelle_unsichere_ereignisse_hinweis(konfig: HillebrandKonfig) -> None:
    unsicher = erkenne_ereignis(("Info", "kollege@example.org", "Our Ref: DEMB00007", datetime(2026, 10, 1)), konfig)
    assert unsicher is not None and unsicher.konfidenz < 0.8
    ergebnis = MailQuelle([unsicher], konfig).aktualisiere(Sendung(spedition="Hillebrand", referenz="DEMB00007"))
    assert any("geringer Konfidenz" in h for h in ergebnis.hinweise)
    assert ergebnis.status == SendungStatus.UNBEKANNT  # "sonstiges" ohne Meilensteinmuster ergibt keinen Status


def test_mailquelle_nie_exception(monkeypatch: pytest.MonkeyPatch, konfig: HillebrandKonfig) -> None:
    quelle = MailQuelle([], konfig)

    def kaputt(sendung: Sendung) -> list[MailEreignis]:
        raise RuntimeError("Boom")

    monkeypatch.setattr(quelle, "passende_ereignisse", kaputt)
    ergebnis = quelle.aktualisiere(Sendung(spedition="Hillebrand", referenz="DEMB00000"))
    assert ergebnis.status == SendungStatus.UNBEKANNT
    assert any("fehlgeschlagen" in h and "Boom" in h for h in ergebnis.hinweise)


def test_mailquelle_ereignisse_eigenschaft(konfig: HillebrandKonfig) -> None:
    quelle = MailQuelle([], konfig)
    assert quelle.ereignisse == []
    quelle.ereignisse.append("x")  # type: ignore[arg-type]  # Kopie, kein Durchgriff
    assert quelle.ereignisse == []
