"""Tests für den Postfach-Klassifikator (Regeln aus postfach/regeln.json)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from postfach.klassifikator import Klassifikator, enthaelt_stichwort
from postfach.modelle import Mail
from postfach.regeln import lade_regelwerk

HEUTE = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
JD = "j.duenschede@weinstrasseadolph.de"


@pytest.fixture(scope="module")
def rw():
    return lade_regelwerk()


@pytest.fixture
def k(rw):
    return Klassifikator(rw, heute=HEUTE)


def mail(
    absender: str, betreff: str, vorschau: str = "", *, gelesen: bool = True, tage_alt: int = 3, **extra
) -> Mail:
    empfangen = HEUTE - timedelta(days=tage_alt)
    return Mail(
        id=f"ID-{abs(hash((absender, betreff))) % 10_000}",
        betreff=betreff,
        absender=absender,
        empfaenger=extra.pop("empfaenger", [JD]),
        empfangen=empfangen,
        gelesen=gelesen,
        vorschau=vorschau,
        **extra,
    )


# ------------------------------------------------------------------ Regeln in Reihenfolge
def test_steuerberater_wird_rot_und_verschoben(k):
    e = k.klassifiziere(mail("boehling@steuerberatung-kb.de", "Termin DATEV smart Login"))
    assert e.regel == "Steuerberater"
    assert e.farbe == "rot"
    assert e.ziel_ordner == "Steuerberater"
    assert e.wird_verschoben
    assert e.antwort == "ja"


def test_steuerberater_auch_als_empfaenger(k):
    m = mail(JD, "AW: Lohnabrechnung", empfaenger=["boehling@steuerberatung-kb.de"])
    e = k.klassifiziere(m)
    assert e.regel == "Steuerberater"
    assert e.antwort == "nein"  # eigene Mail


def test_privat_newsletter_gelb_kein_entwurf(k):
    e = k.klassifiziere(mail("wm-emea-intl@mailing.ubs.com", "Aktien könnten weiter zulegen"))
    assert e.regel.startswith("Privat")
    assert e.ziel_ordner == "Privat"
    assert e.antwort == "nein"


def test_sharepoint_systemmail_ist_privat_und_ohne_entwurf(k):
    e = k.klassifiziere(mail("no-reply@sharepointonline.com", "Bianca hat einen Zugriffslink erstellt"))
    assert e.regel.startswith("Privat")
    assert e.antwort == "nein"


def test_intern_kollegin_blau_nach_intern(k):
    e = k.klassifiziere(mail("c.tiedemann@weinstrasseadolph.de", "Frage zu Urlaub"))
    assert e.regel == "Intern"
    assert e.farbe == "blau"
    assert e.ziel_ordner == "INTERN"
    assert e.antwort == "ja"


def test_intern_abwicklung_landet_in_buchhaltung(k):
    e = k.klassifiziere(
        mail(
            "b.mietzen@weinstrasseadolph.de", "AW: DEMB43404 AW: neuer Auftrag Norma", "Liefertermin verlegen"
        )
    )
    assert e.regel == "Intern"
    assert e.ziel_ordner == "Interne Buchhaltung und Abwicklungskorrespondenz"


def test_intern_umlaut_auftraege_zaehlt_als_abwicklung(k):
    e = k.klassifiziere(mail("v.teubner@weinstrasseadolph.de", "Neue Aufträge für nächste Woche"))
    assert e.ziel_ordner == "Interne Buchhaltung und Abwicklungskorrespondenz"


def test_weiterleitung_vom_kollegen_ist_pruefen(k):
    e = k.klassifiziere(
        mail("c.tiedemann@weinstrasseadolph.de", "Fw: Messias Bestellung", "Wie kann das denn sein?")
    )
    assert e.antwort == "pruefen"


def test_lieferant_mit_rechnung_ist_kommission(k):
    e = k.klassifiziere(mail("export@lustau.es", "Invoice 2026/118 and balance", "please find attached"))
    assert e.regel == "Kommissionen"
    assert e.farbe == "orange"
    assert e.ziel_ordner == "Kommissionen"
    assert e.antwort == "ja"


def test_lieferant_mit_promo_ist_marketing(k):
    e = k.klassifiziere(mail("marketing@togni.it", "ProWein 2027 – our stand"))
    assert e.regel == "Marketing"
    assert e.ziel_ordner == "Marketing, Promos und Messen"


def test_lieferant_newsletter_kein_entwurf(k):
    e = k.klassifiziere(mail("news@ogier.fr", "Newsletter October", "Click here to unsubscribe"))
    assert e.regel == "Marketing"
    assert e.antwort == "nein"


def test_lieferant_sonst_allgemein_und_preisliste_sonderordner(k):
    e = k.klassifiziere(mail("export@vilano.com", "Labels for Germany"))
    assert e.regel == "Allgemeine Kommunikation"
    assert e.farbe == "gruen"
    e2 = k.klassifiziere(mail("export@vilano.com", "Pricelist 2026 Vilano"))
    assert e2.ziel_ordner == "Preislisten 2026"
    assert e2.wird_verschoben


def test_lieferant_einzeladresse_gmail(k):
    e = k.klassifiziere(mail("bollinasrl@gmail.com", "Commission statement"))
    assert e.regel == "Kommissionen"


def test_messeveranstalter_ist_marketing_ohne_lieferant(k):
    e = k.klassifiziere(mail("badge@mybadgeonline.com", "Your badge"))
    assert e.regel == "Marketing"


def test_eigener_newsletter_info_ist_marketing(k):
    e = k.klassifiziere(mail("info@weinstrasseadolph.de", "Weinstrasse Newsletter Herbst"))
    assert e.regel == "Marketing"


def test_spediteur_ohne_farbe_in_buchhaltung(k):
    e = k.klassifiziere(mail("heiko.fleischer@hillebrandgori.com", "WG: DEMB43813 neue Aufträge anbei"))
    assert e.regel == "Spediteure"
    assert e.farbe is None
    assert e.ziel_ordner == "Interne Buchhaltung und Abwicklungskorrespondenz"
    assert e.antwort == "ja"


def test_spediteur_automatische_ab_kein_entwurf(k):
    e = k.klassifiziere(
        mail(
            "rossana.sanfilippo@hillebrandgori.com",
            "Auftragsbestätigung; Your Ref: 20260038; Our Ref: DEMB43800",
        )
    )
    assert e.antwort == "nein"


def test_spediteur_fragment_domain(k):
    e = k.klassifiziere(mail("dispo@boucheundpartner.de", "Abholung"))
    assert e.regel == "Spediteure"


def test_bank_in_buchhaltung_pruefen(k):
    e = k.klassifiziere(mail("service@sparkasse-koelnbonn.de", "Kontoauszug"))
    assert e.regel == "Bank und Versicherung"
    assert e.antwort == "pruefen"


def test_leh_kunde_ordner_fehlt_bleibt_liegen(k):
    e = k.klassifiziere(mail("einkauf@rossmann.de", "Listung Frühjahr"))
    assert e.regel == "LEH-Kunden"
    assert e.aktion == "ordner_fehlt"
    assert not e.wird_verschoben
    assert e.antwort == "ja"


def test_quintessenzio_per_stichwort(k):
    e = k.klassifiziere(mail("anwalt@example.org", "Quintessenzio – Vertragsentwurf"))
    assert e.regel == "Quintessenzio"
    assert e.aktion == "ordner_fehlt"


def test_unzustellbar_wird_geloescht(k):
    e = k.klassifiziere(mail("MAILER-DAEMON@mail.example.com", "Undeliverable: Preisliste"))
    assert e.aktion == "soft_delete"
    assert e.antwort == "nein"


def test_unzustellbar_an_steuerberater_bleibt(k):
    e = k.klassifiziere(
        mail(
            "MAILER-DAEMON@mail.example.com",
            "Undeliverable: Lohn",
            "boehling@steuerberatung-kb.de konnte nicht zugestellt werden",
        )
    )
    assert e.aktion != "soft_delete"
    assert any("Ausnahme" in h for h in e.hinweise)


def test_weingut_ohne_zusammenarbeit_geloescht(k):
    e = k.klassifiziere(mail("info@realrubio.es", "Nueva añada"))
    assert e.aktion == "soft_delete"
    e2 = k.klassifiziere(mail("x@2617115.brevosend.com", "Offer"))
    assert e2.aktion == "soft_delete"


def test_kunde_bleibt_im_posteingang_mit_entwurf(k):
    e = k.klassifiziere(mail("einkauf@rewe-group.com", "Bestellung KW 44"))
    assert e.regel == "Rest"
    assert e.aktion == "liegenlassen"
    assert e.antwort == "ja"


def test_openai_newsletter_rest_ohne_entwurf(k):
    e = k.klassifiziere(mail("noreply@email.openai.com", "4 neue Bildstile"))
    assert e.regel == "Rest"
    assert e.antwort == "nein"


def test_unbekanntes_weingut_wird_gemeldet(k):
    e = k.klassifiziere(mail("export@bodegasnuevas.es", "Samples for Germany"))
    assert e.regel == "Rest"
    assert any("Weingut" in h for h in e.hinweise)


# ------------------------------------------------------------------ Behalten-Logik
def test_ungelesen_bleibt(k):
    e = k.klassifiziere(mail("export@lustau.es", "Invoice", gelesen=False))
    assert e.ziel_ordner == "Kommissionen"
    assert e.bleibt_grund == "ungelesen"
    assert not e.wird_verschoben


def test_ungelesen_aelter_als_60_tage_darf_raus(k):
    e = k.klassifiziere(mail("export@lustau.es", "Invoice", gelesen=False, tage_alt=90))
    assert e.wird_verschoben


def test_markiert_bleibt(k):
    e = k.klassifiziere(mail("export@lustau.es", "Invoice", markiert=True))
    assert e.bleibt_grund == "markiert"


def test_antwort_auf_eigene_mail_bleibt(k):
    e = k.klassifiziere(mail("export@lustau.es", "AW: Etiketten", antwort_auf_eigene=True))
    assert "Antwort" in (e.bleibt_grund or "")
    assert not e.wird_verschoben


def test_behalten_liste(k):
    m = mail("export@lustau.es", "Invoice")
    e = k.klassifiziere(m, behalten_ids={m.id})
    assert e.bleibt_grund.startswith("ausdrücklich")


def test_bewerbung_bleibt(k):
    e = k.klassifiziere(mail("l.kaempf@weinstrasseadolph.de", "WG: Bewerbung Lager"))
    assert e.bleibt_grund.startswith("Bewerbung")


# ------------------------------------------------------------------ Farbe gewinnt
def test_vorhandene_farbe_bestimmt_ordner(k):
    e = k.klassifiziere(mail("export@lustau.es", "Invoice", kategorien=["Allgemeine Kommunikation"]))
    assert e.ziel_ordner == "Allgemeine Kommunikation"
    assert any("Farbe gewinnt" in h for h in e.hinweise)


def test_farbe_intern_mit_abwicklung(k):
    e = k.klassifiziere(
        mail("k.ehrhardt@weinstrasseadolph.de", "Lieferschein Palette", kategorien=["Intern"])
    )
    assert e.ziel_ordner == "Interne Buchhaltung und Abwicklungskorrespondenz"


# ------------------------------------------------------------------ Stichworte
@pytest.mark.parametrize(
    ("text", "woerter", "erwartet"),
    [
        ("Eventuell nächste Woche", ["Event"], None),
        ("Our event in Berlin", ["Event"], "Event"),
        ("Promos für Q4", ["Promo"], "Promo"),
        ("Auftragsbestätigung anbei", ["Auftrag"], "Auftrag"),
        ("Aufträge der Woche", ["Auftrag"], "Auftrag"),
        ("Auktion", ["Aktion"], None),
        ("Pending Documents", ["Pending Documents"], "Pending Documents"),
    ],
)
def test_enthaelt_stichwort(text, woerter, erwartet):
    assert enthaelt_stichwort(text, woerter) == erwartet


def test_mail_aus_suchtreffer():
    roh = {
        "id": "AAMk…AAAAAAEMAAA…",
        "subject": "Test",
        "sender": "a@b.de",
        "senderName": "A B",
        "recipients": [JD],
        "receivedDateTime": "2026-10-23T14:26:02.000Z",
        "isRead": False,
        "summary": "Hallo",
        "hasAttachments": True,
        "importance": "high",
        "internetMessageId": "<x@y>",
        "webLink": "https://outlook.office365.com/owa/?ItemID=…",
    }
    m = Mail.aus_suche(roh)
    assert m.ordner_laut_id == "Posteingang"
    assert m.gelesen is False
    assert m.empfangen.year == 2026
    assert m.domain == "b.de"
