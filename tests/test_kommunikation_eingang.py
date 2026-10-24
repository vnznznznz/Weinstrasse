"""Tests für kommunikation.eingang: Klassifikation synthetischer Mails (tests/fixtures/mails/*.eml),
Posteingang über FakeMailBackend, Rückfall ohne tracking-Modul."""

from __future__ import annotations

import builtins
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from auftragsautomat.config import Settings
from auftragsautomat.kommunikation import eingang
from auftragsautomat.kommunikation.eingang import (
    klassifiziere,
    klassifiziere_alle,
    lade_spedition_regeln,
    sammle_ereignisse,
)
from auftragsautomat.kontakte import Kontaktliste, lade_kontaktliste
from auftragsautomat.m365.basis import Mail, MailKopf
from auftragsautomat.m365.fake import FakeMailBackend, mail_aus_eml
from auftragsautomat.models import Ereignistyp, Kontakt, Kontaktgruppe

REPO_CONFIG = Path(__file__).resolve().parents[1] / "config"
MAILS = Path(__file__).parent / "fixtures" / "mails"
KONTAKTLISTE = Path(__file__).parent / "fixtures" / "excel" / "ansprechpartner_beispiel.xlsx"
REGELN = REPO_CONFIG / "tracking_hillebrand.yaml"


def _mail(name: str) -> Mail:
    return mail_aus_eml(MAILS / f"{name}.eml")


def _eigene(betreff: str, text: str, *, absender: str = "jemand@example.org", html: str | None = None, mail_id: str = "m1") -> Mail:
    kopf = MailKopf(id=mail_id, betreff=betreff, absender=absender, empfangen=datetime(2026, 10, 23, 9, 0, tzinfo=UTC))
    return Mail(kopf=kopf, text=text, html=html)


@pytest.fixture()
def liste(settings: Settings) -> Kontaktliste:
    return lade_kontaktliste(KONTAKTLISTE, settings=settings)


# ---------------------------------------------------------------- Fixtures klassifizieren


def test_lieferantenbestaetigung_englisch_mit_verladedatum():
    e = klassifiziere(_mail("lieferant_bestaetigung_en"), spedition_regeln=REGELN)
    assert e is not None and e.typ == Ereignistyp.LIEFERANT_BESTAETIGUNG
    assert e.kundenreferenz == "WIK-2026-0100"  # unsere Order No.
    assert e.referenz == "OC-2026-0815"  # Nummer der Bestätigung des Weinguts
    assert e.neues_datum == date(2026, 11, 5)  # "ready for collection on 05.11.2026" = Verladedatum
    assert e.absender == "orders@weingut-beispiel.example.org"
    assert e.konfidenz == pytest.approx(0.9)
    assert e.mail_id == "<lieferant-bestaetigung-en@fixture.example.org>"
    assert e.text_auszug and "ready for collection" in e.text_auszug


def test_lieferantenbestaetigung_italienisch_aus_html():
    e = klassifiziere(_mail("lieferant_bestaetigung_it"), spedition_regeln=REGELN)
    assert e is not None and e.typ == Ereignistyp.LIEFERANT_BESTAETIGUNG
    assert e.kundenreferenz == "4711" and e.referenz is None
    assert e.neues_datum == date(2026, 11, 10)
    assert e.text_auszug and "pronta per il ritiro" in e.text_auszug


def test_terminaenderung_lieferant_mit_datum_und_ohne_zitat():
    e = klassifiziere(_mail("terminaenderung_lieferant"), spedition_regeln=REGELN)
    assert e is not None and e.typ == Ereignistyp.TERMINAENDERUNG
    assert e.neues_datum == date(2026, 11, 3)
    assert e.kundenreferenz == "4712"
    assert e.konfidenz == pytest.approx(0.9)
    assert e.text_auszug == "Die Lieferung für Ihre Bestellung 4712 wird verschoben auf den 03.11.2026."
    # der zitierte Teil ("Liefertermin 27.10.2026") wird nicht ausgewertet
    assert "27.10.2026" not in (e.text_auszug or "")


def test_terminaenderung_englisch_mit_monatsnamen():
    e = klassifiziere(_mail("terminaenderung_en"), spedition_regeln=REGELN)
    assert e is not None and e.typ == Ereignistyp.TERMINAENDERUNG
    assert e.neues_datum == date(2026, 11, 10) and e.kundenreferenz == "WIK-2026-0101"


def test_speditionsbestaetigung_ueber_tracking():
    alle = klassifiziere_alle(_mail("spedition_bestaetigung"), spedition_regeln=REGELN)
    assert [e.typ for e in alle] == [Ereignistyp.SPEDITION_BESTAETIGUNG]  # keine doppelte Terminänderung
    e = alle[0]
    assert e.referenz == "DEMB00000" and e.kundenreferenz == "WIK-2026-0100"
    assert e.neues_datum == date(2026, 11, 9)  # "laut Transporeon soll am 09.11. angeliefert werden"
    assert e.konfidenz == 1.0 and e.absender == "dispo.beispiel@hillebrandgori.com"


def test_kontaktaenderung_nur_marker():
    e = klassifiziere(_mail("kontaktaenderung"), spedition_regeln=REGELN)
    assert e is not None and e.typ == Ereignistyp.KONTAKTAENDERUNG
    assert e.neues_datum is None and e.referenz is None
    assert e.konfidenz == pytest.approx(0.6) and e.text_auszug and "neuen Durchwahl" in e.text_auszug


def test_fremde_mail_ergibt_nichts():
    assert klassifiziere(_mail("fremd"), spedition_regeln=REGELN) is None
    assert klassifiziere_alle(_mail("fremd"), spedition_regeln=REGELN) == []


def test_alle_fixtures_auf_einmal():
    erwartet = {
        "fremd": None,
        "kontaktaenderung": Ereignistyp.KONTAKTAENDERUNG,
        "lieferant_bestaetigung_en": Ereignistyp.LIEFERANT_BESTAETIGUNG,
        "lieferant_bestaetigung_it": Ereignistyp.LIEFERANT_BESTAETIGUNG,
        "spedition_bestaetigung": Ereignistyp.SPEDITION_BESTAETIGUNG,
        "terminaenderung_en": Ereignistyp.TERMINAENDERUNG,
        "terminaenderung_lieferant": Ereignistyp.TERMINAENDERUNG,
    }
    regeln = lade_spedition_regeln(REGELN)
    for pfad in sorted(MAILS.glob("*.eml")):
        e = klassifiziere(mail_aus_eml(pfad), spedition_regeln=regeln)
        assert (e.typ if e else None) == erwartet[pfad.stem], pfad.name


# ---------------------------------------------------------------- Kontaktliste und Nebenereignisse


def test_kontaktliste_hebt_konfidenz_und_schliesst_kunden_aus(liste: Kontaktliste):
    e = klassifiziere(_mail("lieferant_bestaetigung_en"), spedition_regeln=REGELN, kontaktliste=liste)
    assert e is not None and e.konfidenz == 1.0  # Absender ist Lieferant laut Kontaktliste
    kunde = _eigene("Auftragsbestätigung erhalten", "Danke, alles klar.", absender="dispo@beispielmarkt.example.org")
    assert klassifiziere(kunde, spedition_regeln=REGELN, kontaktliste=liste) is None  # Kunde bestätigt nichts fürs Weingut
    assert klassifiziere(kunde, spedition_regeln=REGELN) is not None  # ohne Liste: Betreff zählt
    marker = _mail("kontaktaenderung")
    e2 = klassifiziere(marker, spedition_regeln=REGELN, kontaktliste=liste)
    assert e2 is not None and e2.konfidenz == pytest.approx(0.7)


def test_bestaetigung_mit_kontaktaenderung_und_terminaenderung_als_nebenereignisse():
    text = (
        "Dear Ms Beispiel,\n\nwe confirm your order no. 4713. Loading date: 12.11.2026.\n"
        "Unfortunately the delivery has to be postponed to 20.11.2026.\n"
        "Please note my new e-mail address: neu@weingut-beispiel.example.org.\n\nBest regards\nMarco Esempio\n"
    )
    alle = klassifiziere_alle(_eigene("Order confirmation 4713", text, absender="export@weingut-beispiel.example.org"), spedition_regeln=REGELN)
    assert [e.typ for e in alle] == [Ereignistyp.LIEFERANT_BESTAETIGUNG, Ereignistyp.TERMINAENDERUNG, Ereignistyp.KONTAKTAENDERUNG]
    assert alle[0].neues_datum == date(2026, 11, 12) and alle[0].kundenreferenz == "4713"
    assert alle[1].neues_datum == date(2026, 11, 20)
    assert alle[2].text_auszug and "new e-mail address" in alle[2].text_auszug


def test_terminaenderung_ohne_datum_nur_marker_und_kurzdatum_mit_jahr_aus_empfang():
    e = klassifiziere(_eigene("Lieferverzögerung Bestellung 4714", "Die Lieferung verschiebt sich leider um eine Woche."), spedition_regeln=REGELN)
    assert e is not None and e.typ == Ereignistyp.TERMINAENDERUNG and e.neues_datum is None and e.konfidenz == pytest.approx(0.5)
    e2 = klassifiziere(_eigene("Info", "Neuer Liefertermin: 05.01."), spedition_regeln=REGELN)
    assert e2 is not None and e2.neues_datum == date(2027, 1, 5)  # Empfang im Oktober -> Januar des Folgejahres
    e3 = klassifiziere(_eigene("Info", "Ihre Bestellung 4715 ist rescheduled to 2026-12-01."), spedition_regeln=REGELN)
    assert e3 is not None and e3.neues_datum == date(2026, 12, 1) and e3.kundenreferenz == "4715"


def test_bestaetigung_mehrsprachig():
    faelle = {
        "Confirmación de pedido 4716": "Confirmamos su pedido 4716. Fecha de carga: 15.11.2026.",
        "Confirmation de commande": "Nous confirmons votre commande n° 4717. Chargement prévu le 16.11.2026.",
        "Confirmação de encomenda": "Encomenda n° 4718 confirmada.",
        "Bestellbestätigung": "Hiermit bestätigen wir Ihre Bestellung 4719. Abholbereit ab 17.11.2026.",
    }
    for betreff, text in faelle.items():
        e = klassifiziere(_eigene(betreff, text), spedition_regeln=REGELN)
        assert e is not None and e.typ == Ereignistyp.LIEFERANT_BESTAETIGUNG, betreff
    es = klassifiziere(_eigene("Confirmación de pedido 4716", faelle["Confirmación de pedido 4716"]), spedition_regeln=REGELN)
    assert es is not None and es.kundenreferenz == "4716" and es.neues_datum == date(2026, 11, 15)
    de = klassifiziere(_eigene("Bestellbestätigung", faelle["Bestellbestätigung"]), spedition_regeln=REGELN)
    assert de is not None and de.kundenreferenz == "4719" and de.neues_datum == date(2026, 11, 17)


# ---------------------------------------------------------------- Posteingang


def test_sammle_ereignisse_ueber_fake_backend(settings: Settings, liste: Kontaktliste):
    settings.tracking.hillebrand_konfig = REGELN
    mails = [mail_aus_eml(p, mail_id=p.stem) for p in sorted(MAILS.glob("*.eml"))]
    alt = _eigene("Order confirmation 1", "We confirm your order 1.", mail_id="alt")
    alt.kopf.empfangen = datetime(2026, 9, 1, tzinfo=UTC)
    backend = FakeMailBackend(settings.kommunikation.entwuerfe_ordner, mails + [alt])
    ereignisse = sammle_ereignisse(backend, settings, seit=date(2026, 10, 15), kontaktliste=liste)
    assert [e.typ for e in ereignisse] == [
        Ereignistyp.KONTAKTAENDERUNG,  # 19.10.
        Ereignistyp.SPEDITION_BESTAETIGUNG,  # 20.10.
        Ereignistyp.LIEFERANT_BESTAETIGUNG,  # 21.10.
        Ereignistyp.LIEFERANT_BESTAETIGUNG,  # 22.10.
        Ereignistyp.TERMINAENDERUNG,  # 23.10. 08:05
        Ereignistyp.TERMINAENDERUNG,  # 23.10. 16:30
    ]
    assert ereignisse[1].mail_id == "spedition_bestaetigung"
    assert all(e.mail_id != "alt" for e in ereignisse)
    # ab einem Zeitpunkt nach allen Mails: nichts
    assert sammle_ereignisse(backend, settings, seit=datetime(2026, 10, 25, tzinfo=UTC)) == []


def test_sammle_ereignisse_ueberspringt_kaputte_mails_und_leeren_posteingang(settings: Settings, monkeypatch: pytest.MonkeyPatch):
    settings.tracking.hillebrand_konfig = REGELN
    backend = FakeMailBackend(settings.kommunikation.entwuerfe_ordner, [_mail("terminaenderung_en"), _mail("fremd")])
    original = backend.lese_mail

    def lese(mail_id: str) -> Mail:
        if "fremd" in mail_id:
            raise RuntimeError("kaputt")
        return original(mail_id)

    monkeypatch.setattr(backend, "lese_mail", lese)
    ereignisse = sammle_ereignisse(backend, settings, seit=date(2026, 10, 1))
    assert [e.typ for e in ereignisse] == [Ereignistyp.TERMINAENDERUNG]

    class Kaputt:
        def suche_mails(self, **_: object) -> list:
            raise RuntimeError("Postfach weg")

    assert sammle_ereignisse(Kaputt(), settings, seit=date(2026, 10, 1)) == []  # type: ignore[arg-type]


# ---------------------------------------------------------------- Rückfall ohne tracking-Modul


def test_minimal_erkennung_ohne_tracking_modul(monkeypatch: pytest.MonkeyPatch):
    echter_import = builtins.__import__

    def ohne_tracking(name: str, *args, **kwargs):
        if name.startswith("auftragsautomat.tracking"):
            raise ImportError("tracking fehlt (Test)")
        return echter_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", ohne_tracking)
    regeln = lade_spedition_regeln(REGELN)
    assert isinstance(regeln, eingang._MinimalRegeln) and "hillebrandgori.com" in regeln.absender_domains
    e = klassifiziere(_mail("spedition_bestaetigung"), spedition_regeln=regeln)
    assert e is not None and e.typ == Ereignistyp.SPEDITION_BESTAETIGUNG
    assert e.referenz == "DEMB00000" and e.kundenreferenz == "WIK-2026-0100" and e.neues_datum == date(2026, 11, 9) and e.konfidenz == 1.0
    # unbekannte Domain, aber Referenz im Text -> SONSTIGES mit geringer Konfidenz
    sonst = klassifiziere(_eigene("Info", "Zu Our Ref: DEMB00001 melden wir uns morgen."), spedition_regeln=regeln)
    assert sonst is not None and sonst.typ == Ereignistyp.SONSTIGES and sonst.referenz == "DEMB00001" and sonst.konfidenz == 0.5
    # Terminänderung von bekannter Domain
    termin = klassifiziere(_eigene("Umbuchung", "Das Zeitfenster wurde verschoben auf den 03.11.2026.", absender="x@hillebrandgori.com"), spedition_regeln=regeln)
    assert termin is not None and termin.typ == Ereignistyp.TERMINAENDERUNG and termin.neues_datum == date(2026, 11, 3) and termin.konfidenz == pytest.approx(0.9)
    # Lieferantenmail bleibt Lieferantenbestätigung
    lief = klassifiziere(_mail("lieferant_bestaetigung_en"), spedition_regeln=regeln)
    assert lief is not None and lief.typ == Ereignistyp.LIEFERANT_BESTAETIGUNG
    # Regeln als Dict und ohne Datei
    assert lade_spedition_regeln({"absender_domains": ["sped.example.org"]}).ist_absender_bekannt("a@sped.example.org")
    assert lade_spedition_regeln(None).absender_domains == []
    assert not lade_spedition_regeln(Path("/gibt/es/nicht.yaml")).ist_absender_bekannt("a@hillebrandgori.com")


def test_spedition_regeln_laden_varianten():
    konfig = lade_spedition_regeln(REGELN)
    assert konfig.ist_absender_bekannt("dispo.beispiel@hillebrandgori.com")
    assert lade_spedition_regeln(konfig) is konfig  # geladenes Objekt wird durchgereicht
    aus_dict = lade_spedition_regeln({"absender_domains": ["sped.example.org"]})
    assert aus_dict.ist_absender_bekannt("a@sped.example.org")
    kaputt = lade_spedition_regeln(Path("/gibt/es/nicht/wirklich.yaml"))
    assert kaputt.ist_absender_bekannt("dispo.beispiel@hillebrandgori.com")  # fehlende Datei -> Standarddatei des tracking-Moduls


def test_mail_mit_nur_html_und_absender_ohne_adresse():
    html = "<html><body><p>Order confirmation</p><p>We confirm your order no. 4720; ready for pick-up from 18.11.2026.</p></body></html>"
    kopf = MailKopf(id="h1", betreff="", absender="Weingut Beispiel", empfangen=datetime(2026, 10, 23, 9, 0, tzinfo=UTC))
    e = klassifiziere(Mail(kopf=kopf, html=html), spedition_regeln=REGELN)
    assert e is not None and e.typ == Ereignistyp.LIEFERANT_BESTAETIGUNG
    assert e.absender == "weingut beispiel" and e.kundenreferenz == "4720" and e.neues_datum == date(2026, 11, 18)
    assert e.konfidenz == pytest.approx(0.75)  # nur im Text, nicht im Betreff


def test_kontaktgruppe_aus_eigener_liste():
    liste = Kontaktliste(kontakte=[Kontakt(gruppe=Kontaktgruppe.SPEDITION, firma="Sped", email="dispo@sped.example.org")])
    e = klassifiziere(_eigene("Auftragsbestätigung 4721", "Wir bestätigen den Auftrag.", absender="dispo@sped.example.org"), spedition_regeln=REGELN, kontaktliste=liste)
    assert e is None  # Spedition ohne Hillebrand-Muster ist keine Lieferantenbestätigung
