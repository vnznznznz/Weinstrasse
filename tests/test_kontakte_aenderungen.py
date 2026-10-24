"""Tests für kontakte.aenderungen: Signaturen erkennen, Änderungen vorschlagen, nach Freigabe in die Excel schreiben.

Alle Signaturen sind synthetisch (Domain example.org, erfundene Nummern).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from auftragsautomat.config import DateiRefKonfig, Settings
from auftragsautomat.kontakte import (
    FELD_NEU,
    KontakteFehler,
    Kontaktliste,
    aenderungen_aus_mail,
    bericht,
    erkenne_signatur,
    lade_kontaktliste,
    normalisiere_telefon,
    telefon_gleich,
    uebernehme,
    vergleiche,
)
from auftragsautomat.m365.basis import Mail, MailKopf
from auftragsautomat.m365.fake import FakeExcelBackend
from auftragsautomat.models import Kontakt, KontaktAenderung, Kontaktgruppe

FIXTURE = Path(__file__).parent / "fixtures" / "excel" / "ansprechpartner_beispiel.xlsx"

SIGNATUR_DE = """Hallo Frau Beispiel,

vielen Dank für Ihre Bestellung vom 20.10.2026. Die Ware ist am 24.10.2026 abholbereit.

Mit freundlichen Grüßen
i.A. Petra Beispiel
Disposition / Teamleitung
Spedition Hillebrand Beispiel GmbH
Musterstraße 12
55116 Mainz
Tel.: +49 (0) 6131 98765-43
Mobil: 0171 1234567
Fax: +49 6131 98765-99
p.beispiel@hillebrand.example.org
www.hillebrand.example.org
USt-IdNr. DE123456789
"""

SIGNATUR_EN = """Dear Mr Beispiel,

thank you for your order no. 4711. We confirm loading at end of week 44.

Best regards,
Giulia Esempio
Export Manager
Weingut Beispiel Srl
Via Esempio 1 - 12345 Esempio (CN)
Phone +39 0123 456790 | Mobile +39 333 1234567
giulia.esempio@weingut-beispiel.example.org

From: Logistik <logistik@example.org>
Sent: Monday
Subject: New order
> alter Text mit Nummer +49 999 9999999
"""

SIGNATUR_IT = """Buongiorno,

confermiamo la ricezione dell'ordine.

Cordiali saluti
Dott. Marco Esempio
Responsabile Logistica
Cantina Alpha S.r.l.
Tel. 0039 0456 112299
ordini@cantine-abc.example.org
"""


@pytest.fixture()
def liste(settings: Settings) -> Kontaktliste:
    return lade_kontaktliste(FIXTURE, settings=settings)


# ---------------------------------------------------------------- Signatur erkennen


def test_signatur_deutsch():
    s = erkenne_signatur(SIGNATUR_DE)
    assert s["grussformel"] == "Mit freundlichen Grüßen"
    assert s["name"] == "Petra Beispiel"  # "i.A." entfernt
    assert s["position"] == "Disposition / Teamleitung"
    assert s["firma"] == "Spedition Hillebrand Beispiel GmbH"
    assert s["emails"] == ["p.beispiel@hillebrand.example.org"]
    assert s["telefone"] == ["+49 (0) 6131 98765-43", "0171 1234567"]
    assert s["fax"] == ["+49 6131 98765-99"]
    assert "USt-IdNr. DE123456789" not in " ".join(s["telefone"])
    assert s["zeilen"][0] == "i.A. Petra Beispiel"


def test_signatur_englisch_endet_am_zitat():
    s = erkenne_signatur(SIGNATUR_EN)
    assert s["grussformel"] == "Best regards,"
    assert (s["name"], s["position"], s["firma"]) == ("Giulia Esempio", "Export Manager", "Weingut Beispiel Srl")
    assert s["emails"] == ["giulia.esempio@weingut-beispiel.example.org"]
    assert s["telefone"] == ["+39 0123 456790", "+39 333 1234567"]  # zitierte Nummer nicht enthalten
    assert all("Subject" not in z for z in s["zeilen"])


def test_signatur_italienisch_mit_titel():
    s = erkenne_signatur(SIGNATUR_IT)
    assert s["grussformel"] == "Cordiali saluti"
    assert (s["name"], s["position"], s["firma"]) == ("Marco Esempio", "Responsabile Logistica", "Cantina Alpha S.r.l.")
    assert s["telefone"] == ["0039 0456 112299"] and s["emails"] == ["ordini@cantine-abc.example.org"]


def test_signatur_ohne_grussformel_html_und_leer():
    s = erkenne_signatur("Hallo, Rückruf unter +49 201 5550100 bitte. test@example.org, Termin 24.10.2026")
    assert s["grussformel"] is None and s["name"] is None
    assert s["emails"] == ["test@example.org"] and s["telefone"] == ["+49 201 5550100"]
    html = "<html><body><p>Danke!</p><p>Mit freundlichen Grüßen</p><p>Hans Beispiel<br>Inhaber<br>Tel. 0201 5550299</p></body></html>"
    h = erkenne_signatur(html)
    assert (h["name"], h["position"], h["telefone"]) == ("Hans Beispiel", "Inhaber", ["0201 5550299"])
    leer = erkenne_signatur("")
    assert leer["emails"] == [] and leer["telefone"] == [] and leer["name"] is None
    assert erkenne_signatur(None)["zeilen"] == []


def test_telefon_normalisierung_und_vergleich():
    assert normalisiere_telefon("+49 (0) 6131 12 34-5") == "+49613112345"
    assert normalisiere_telefon("0049 6131 123456") == "+496131123456"
    assert normalisiere_telefon("06131 / 12 34 56") == "0613112 3456".replace(" ", "")
    assert normalisiere_telefon(None) == "" and normalisiere_telefon("abc") == ""
    assert telefon_gleich("+49 6131 9876543", "+49 (0) 6131 98765-43")
    assert telefon_gleich("0049 6131 1234567", "+49 6131 1234567")
    assert telefon_gleich("06131 1234567", "+49 6131 1234567")
    assert not telefon_gleich("+39 0123 456789", "+39 0123 456790")
    assert not telefon_gleich("", "+49 1") and not telefon_gleich("12345", "12345678")


# ---------------------------------------------------------------- vergleichen


def test_vergleiche_findet_nur_echte_unterschiede(liste: Kontaktliste):
    petra = liste.kontakt_fuer_email("p.beispiel@hillebrand.example.org")
    assert petra is not None
    aend = vergleiche(petra, erkenne_signatur(SIGNATUR_DE), quelle="Test-Mail")
    # E-Mail gleich, Telefon gleich (andere Schreibweise), Position geändert
    assert [(a.feld, a.alt, a.neu, a.quelle, a.uebernommen) for a in aend] == [
        ("position", "Disponentin", "Disposition / Teamleitung", "Test-Mail", False)
    ]
    assert aend[0].kontakt is petra
    giulia = liste.kontakt_fuer_email("orders@weingut-beispiel.example.org")
    assert giulia is not None
    aend2 = vergleiche(giulia, erkenne_signatur(SIGNATUR_EN))
    assert [(a.feld, a.neu) for a in aend2] == [
        ("email", "giulia.esempio@weingut-beispiel.example.org"),
        ("telefon", "+39 0123 456790"),
        ("position", "Export Manager"),
    ]
    assert all(a.quelle == "E-Mail-Signatur" for a in aend2)


def test_vergleiche_fremder_name_und_fehlender_name(liste: Kontaktliste, caplog):
    anna = liste.kontakt_fuer_email("ordini@cantine-abc.example.org")
    assert anna is not None
    with caplog.at_level("INFO"):
        assert vergleiche(anna, erkenne_signatur(SIGNATUR_IT)) == []  # Marco statt Anna -> keine Vorschläge
    assert "passt nicht" in caplog.text
    ohne_name = Kontakt(gruppe=Kontaktgruppe.SPEDITION, firma="Hillebrand", name=None, position="Disposition / Teamleitung", email="p.beispiel@hillebrand.example.org", quelle="Speditionen!7")
    aend = vergleiche(ohne_name, erkenne_signatur(SIGNATUR_DE))
    assert [(a.feld, a.neu) for a in aend] == [("telefon", "+49 (0) 6131 98765-43"), ("name", "Petra Beispiel")]
    assert vergleiche(ohne_name, erkenne_signatur("")) == []


def test_bericht(liste: Kontaktliste):
    assert bericht([]) == "Keine Änderungsvorschläge zur Kontaktliste."
    giulia = liste.kontakt_fuer_email("orders@weingut-beispiel.example.org")
    assert giulia is not None
    text = bericht(vergleiche(giulia, erkenne_signatur(SIGNATUR_EN), quelle="Mail 1"))
    assert "Lexware-Stammdaten unverändert" in text
    assert "- [Vorschlag] Weingut Beispiel Srl / Giulia Esempio (Lieferanten!5): email 'orders@weingut-beispiel.example.org' -> 'giulia.esempio@weingut-beispiel.example.org' (Quelle: Mail 1)" in text
    neu = KontaktAenderung(kontakt=giulia, feld=FELD_NEU, alt="Weingut Beispiel Srl", neu="X | Y | x@example.org | ", quelle="Mail 2")
    assert "Neuer Ansprechpartner bei Weingut Beispiel Srl: X | Y | x@example.org |" in bericht([neu])


# ---------------------------------------------------------------- Mail -> Vorschläge


def _mail(absender: str, text: str, *, name: str | None = None, mail_id: str = "m1") -> Mail:
    kopf = MailKopf(id=mail_id, betreff="Re: Neuer Auftrag", absender=absender, absender_name=name, empfangen=datetime(2026, 10, 24, 9, 0, tzinfo=UTC))
    return Mail(kopf=kopf, text=text)


def test_aenderungen_aus_mail(liste: Kontaktliste):
    bekannt = aenderungen_aus_mail(_mail("P.Beispiel@hillebrand.example.org", SIGNATUR_DE), liste)
    assert [(a.feld, a.neu) for a in bekannt] == [("position", "Disposition / Teamleitung")]
    assert bekannt[0].quelle.startswith("Mail m1 von p.beispiel@hillebrand.example.org (Re: Neuer Auftrag, 24.10.2026)")
    # unbekannter Absender mit bekannter Domain -> Vorschlag "neu" bei derselben Firma
    neu = aenderungen_aus_mail(_mail("new.person@hillebrand.example.org", SIGNATUR_DE, name="Neue Person", mail_id="m2"), liste)
    assert len(neu) == 1 and neu[0].feld == FELD_NEU and neu[0].alt == "Hillebrand"
    assert neu[0].neu == "Petra Beispiel | Disposition / Teamleitung | new.person@hillebrand.example.org | +49 (0) 6131 98765-43"
    assert neu[0].kontakt is not None and neu[0].kontakt.firma == "Hillebrand"
    # unbekannter Absender, aber Firma aus der Signatur bekannt
    aus_firma = aenderungen_aus_mail(_mail("someone@fremd.example.org", SIGNATUR_IT, mail_id="m3"), liste)
    assert len(aus_firma) == 1 and aus_firma[0].alt == "Cantina Alpha & Cantina Beta & Cantina Gamma"
    # ganz fremd -> nichts
    assert aenderungen_aus_mail(_mail("x@fremd.example.org", "Hallo\n\nMit freundlichen Grüßen\nFremde Person\nFremdfirma GmbH", mail_id="m4"), liste) == []
    html_mail = Mail(kopf=_mail("orders@weingut-beispiel.example.org", "").kopf, html="<p>Best regards</p><p>Giulia Esempio<br>Export Manager</p>")
    assert [(a.feld, a.neu) for a in aenderungen_aus_mail(html_mail, liste)] == [("position", "Export Manager")]


# ---------------------------------------------------------------- übernehmen


@pytest.fixture()
def backend_und_liste(settings: Settings) -> tuple[FakeExcelBackend, DateiRefKonfig, Kontaktliste]:
    backend = FakeExcelBackend()
    ref = DateiRefKonfig(lokaler_pfad=FIXTURE)
    return backend, ref, lade_kontaktliste((backend, ref), settings=settings)


def test_uebernehme_nur_nach_freigabe_oder_schalter(backend_und_liste, settings: Settings, caplog):
    backend, ref, liste = backend_und_liste
    petra = liste.kontakt_fuer_email("p.beispiel@hillebrand.example.org")
    assert petra is not None
    aend = vergleiche(petra, erkenne_signatur(SIGNATUR_DE), quelle="Mail 1")[0]
    assert settings.kontakte.automatisch_uebernehmen is False
    with caplog.at_level("INFO"):
        assert uebernehme(aend, backend, ref, settings=settings) is False
    assert backend.schreibprotokoll == [] and aend.uebernommen is False and petra.position == "Disponentin"
    assert "bleibt Vorschlag" in caplog.text
    # explizite Freigabe: Position steht in Spalte B ("Bereich / Position"), Zeile aus der Quelle
    assert uebernehme(aend, backend, ref, settings=settings, freigegeben=True) is True
    assert [(z.blatt, z.adresse, z.zellen) for z in backend.schreibprotokoll] == [
        ("Speditionen", "B7", [("B7", "Disposition / Teamleitung", None)])
    ]
    assert aend.uebernommen is True and petra.position == "Disposition / Teamleitung"
    # zweiter Versuch: Zelle enthält nicht mehr den alten Wert
    with pytest.raises(KontakteFehler, match="zwischenzeitlich geändert"):
        uebernehme(aend, backend, ref, settings=settings, freigegeben=True)
    assert uebernehme(aend, backend, ref, settings=settings, freigegeben=True, pruefen=False) is True
    assert len(backend.schreibprotokoll) == 2


def test_uebernehme_automatisch_schreibt_email_und_telefon_in_richtige_spalten(backend_und_liste, settings: Settings):
    backend, ref, liste = backend_und_liste
    settings.kontakte.automatisch_uebernehmen = True
    giulia = liste.kontakt_fuer_email("orders@weingut-beispiel.example.org")
    assert giulia is not None
    for aend in vergleiche(giulia, erkenne_signatur(SIGNATUR_EN), quelle="Mail 2"):
        assert uebernehme(aend, backend, ref, settings=settings) is True
    assert [(z.blatt, z.adresse) for z in backend.schreibprotokoll] == [("Lieferanten", "D5"), ("Lieferanten", "E5"), ("Lieferanten", "C5")]
    zeile = backend.werte(ref, "Lieferanten")[4]
    assert zeile[1:5] == ["Giulia   Esempio", "Export Manager", "giulia.esempio@weingut-beispiel.example.org", "+39 0123 456790"]
    assert (giulia.email, giulia.telefon, giulia.position) == ("giulia.esempio@weingut-beispiel.example.org", "+39 0123 456790", "Export Manager")
    # neu laden: Änderungen sind in der (In-Memory-)Mappe sichtbar
    neu = lade_kontaktliste((backend, ref), settings=settings)
    assert neu.kontakt_fuer_email("giulia.esempio@weingut-beispiel.example.org").position == "Export Manager"  # type: ignore[union-attr]


def test_uebernehme_fehlerfaelle(backend_und_liste, settings: Settings):
    backend, ref, liste = backend_und_liste
    petra = liste.kontakt_fuer_email("p.beispiel@hillebrand.example.org")
    assert petra is not None
    with pytest.raises(KontakteFehler, match="nicht automatisch"):
        uebernehme(KontaktAenderung(kontakt=petra, feld=FELD_NEU, neu="x", quelle="q"), backend, ref, settings=settings, freigegeben=True)
    with pytest.raises(KontakteFehler, match="ohne Kontakt"):
        uebernehme(KontaktAenderung(kontakt=None, feld="email", neu="x@example.org", quelle="q"), backend, ref, settings=settings, freigegeben=True)
    ohne_quelle = Kontakt(gruppe=Kontaktgruppe.SPEDITION, firma="Hillebrand", name="Petra Beispiel", email="p.beispiel@hillebrand.example.org")
    with pytest.raises(KontakteFehler, match="Quelle"):
        uebernehme(KontaktAenderung(kontakt=ohne_quelle, feld="email", neu="x@example.org", quelle="q"), backend, ref, settings=settings, freigegeben=True)
    falsche_zeile = petra.model_copy(update={"quelle": "Speditionen!3"})  # Zeile ohne Kopfzeile darüber
    with pytest.raises(KontakteFehler, match="Kopfzeile"):
        uebernehme(KontaktAenderung(kontakt=falsche_zeile, feld="email", alt=petra.email, neu="x@example.org", quelle="q"), backend, ref, settings=settings, freigegeben=True)
    ausserhalb = petra.model_copy(update={"quelle": "Speditionen!99"})
    with pytest.raises(KontakteFehler, match="außerhalb"):
        uebernehme(KontaktAenderung(kontakt=ausserhalb, feld="email", alt=petra.email, neu="x@example.org", quelle="q"), backend, ref, settings=settings, freigegeben=True)
    assert backend.schreibprotokoll == []
