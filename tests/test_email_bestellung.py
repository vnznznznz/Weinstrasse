"""Tests für die E-Mail-Extraktion (Eingangstyp B): .eml, .txt, .msg (gefakt), PDF-Anhänge."""

from __future__ import annotations

import sys
import types
from datetime import date
from decimal import Decimal
from email import policy
from email.message import EmailMessage
from pathlib import Path

import pytest

from auftragsautomat.extraktion import email_bestellung as eb
from auftragsautomat.extraktion.email_bestellung import (
    EMAIL_ENDUNGEN,
    Adresse,
    Anhang,
    EmailExtraktor,
    EmailFehler,
    EmailInhalt,
    MsgNichtVerfuegbar,
    endkunde_aus_text,
    html_zu_text,
    lese_email,
    lese_eml,
    lese_msg,
    lese_positionen,
    lese_pseudokopf,
    lese_txt,
    lieferant_aus_anrede,
    msg_unterstuetzt,
    teile_zitat,
)
from auftragsautomat.models import (
    Eingangstyp,
    Extraktionsmethode,
    Pruefcode,
    RohBestellung,
    Schweregrad,
)


@pytest.fixture()
def extraktor(settings) -> EmailExtraktor:
    return EmailExtraktor(settings)


@pytest.fixture()
def emails(fixtures_ordner: Path) -> Path:
    ordner = fixtures_ordner / "emails"
    assert ordner.is_dir(), "E-Mail-Fixtures fehlen (tests/fixtures/emails)"
    return ordner


@pytest.fixture()
def pdfs(fixtures_ordner: Path) -> Path:
    return fixtures_ordner / "pdfs"


def _hinweise(roh: RohBestellung, code: Pruefcode, grad: Schweregrad | None = None) -> list[str]:
    return [h.text for h in roh.hinweise if h.code == code and (grad is None or h.schweregrad == grad)]


def _warnungen(roh: RohBestellung) -> list[str]:
    return [h.text for h in roh.hinweise if h.schweregrad != Schweregrad.INFO]


def _eml(
    ziel: Path,
    *,
    text: str | None = None,
    html: str | None = None,
    von: str = "Wein-Import-Kreis LEH <bestellung@wein-import-kreis.example>",
    an: str = "Cide srl <ordini@cide.example>",
    betreff: str = "Bestellung",
    datum: str | None = "Fri, 16 Oct 2026 10:00:00 +0200",
    anhaenge: list[tuple[str, bytes, str, str]] | None = None,
) -> Path:
    """Schreibt eine .eml-Datei (text/plain und/oder text/html, optional Anhänge)."""
    msg = EmailMessage(policy=policy.SMTP)
    msg["From"] = von
    msg["To"] = an
    msg["Subject"] = betreff
    if datum:
        msg["Date"] = datum
    if text is not None:
        msg.set_content(text, charset="utf-8")
        if html is not None:
            msg.add_alternative(html, subtype="html", charset="utf-8")
    elif html is not None:
        msg.set_content(html, subtype="html", charset="utf-8")
    else:
        msg.set_content("", charset="utf-8")
    for name, daten, haupttyp, untertyp in anhaenge or []:
        msg.add_attachment(daten, maintype=haupttyp, subtype=untertyp, filename=name)
    ziel.write_bytes(msg.as_bytes())
    return ziel


# ---------------------------------------------------------------------------
# kann_verarbeiten / Dateiarten
# ---------------------------------------------------------------------------


def test_email_endungen():
    assert EMAIL_ENDUNGEN == {".eml", ".msg", ".txt"}


def test_kann_verarbeiten(extraktor: EmailExtraktor, emails: Path, pdfs: Path, tmp_path: Path, monkeypatch):
    assert extraktor.kann_verarbeiten(emails / "bestellung_cide_positionen_im_text.eml") is True
    assert extraktor.kann_verarbeiten(emails / "bestellung_bollina.txt") is True
    assert extraktor.kann_verarbeiten(pdfs / "eigene_bestellung_cide.pdf") is False
    assert extraktor.kann_verarbeiten(tmp_path / "fehlt.eml") is False
    msg = tmp_path / "mail.msg"
    msg.write_bytes(b"\xd0\xcf\x11\xe0")
    monkeypatch.setitem(sys.modules, "extract_msg", None)
    assert msg_unterstuetzt() is False
    assert extraktor.kann_verarbeiten(msg) is False


def test_extrahiere_falsche_endung_und_fehlende_datei(extraktor: EmailExtraktor, tmp_path: Path):
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF-")
    with pytest.raises(ValueError):
        extraktor.extrahiere(pdf)
    with pytest.raises(FileNotFoundError):
        extraktor.extrahiere(tmp_path / "fehlt.eml")
    with pytest.raises(ValueError):
        lese_email(pdf)


# ---------------------------------------------------------------------------
# Fixture 1: .eml mit Positionen im Text
# ---------------------------------------------------------------------------


def test_eml_positionen_im_text_kopf(extraktor: EmailExtraktor, emails: Path):
    datei = emails / "bestellung_cide_positionen_im_text.eml"
    roh = extraktor.extrahiere(datei)
    assert roh.eingangstyp == Eingangstyp.EIGENE_BESTELLUNG_EMAIL
    assert roh.extraktionsmethode == Extraktionsmethode.EMAIL
    assert roh.quelldatei == datei
    assert roh.lieferant_text == "Cide srl"
    assert roh.kunde_text == "Weinhaus Menrath"
    assert roh.belegnummer == "BE-2026-0078"
    assert roh.bestellnummer is None
    assert roh.datum == date(2026, 10, 17)  # Sendedatum der Mail
    assert roh.liefertag == date(2026, 10, 28)
    assert roh.lieferadresse == "Spedition Weinlogistik, Hafenstraße 2, 47119 Duisburg"
    assert roh.empfaenger_firma_text == "Wein-Import-Kreis LEH"
    assert roh.seiten is None
    assert roh.rohtext and "Betreff: Bestellung BE-2026-0078 für Weinhaus Menrath" in roh.rohtext
    assert "Hafenstraße" in roh.rohtext  # Umlaute aus quoted-printable korrekt dekodiert


def test_eml_positionen_im_text_positionen(extraktor: EmailExtraktor, emails: Path):
    roh = extraktor.extrahiere(emails / "bestellung_cide_positionen_im_text.eml")
    assert [p.laufnr for p in roh.positionen] == [1, 2, 3]
    p1, p2, p3 = roh.positionen
    assert p1.bezeichnung == "Villa Armellina Gloria Secco Frizzante 0,75 l"
    assert p1.menge == Decimal("60")
    assert p1.einheit == "Fl"
    assert p1.preis == Decimal("4.20")
    assert p1.volumen_cl == Decimal("75")
    assert p1.jahrgang is None
    assert p1.artikelnummer is None
    assert p1.durchgestrichen is False
    assert p1.rohtext == "- 60 Fl Villa Armellina Gloria Secco Frizzante 0,75 l à 4,20 €"
    assert p2.bezeichnung == "Villa Armellina Rosato Secco Frizzante 0,75 l"
    assert p2.menge == Decimal("24")
    assert p3.bezeichnung == "Villa Armellina Rosato Secco Frizzante Magnum 1,5 l"
    assert p3.menge == Decimal("6")
    assert p3.preis == Decimal("9.50")
    assert p3.volumen_cl == Decimal("150")
    assert roh.gesamtmenge == Decimal("90")


def test_eml_positionen_im_text_summenvermerk_und_hinweise(extraktor: EmailExtraktor, emails: Path):
    roh = extraktor.extrahiere(emails / "bestellung_cide_positionen_im_text.eml")
    assert len(roh.summenvermerke) == 1
    assert roh.summenvermerke[0].wert == Decimal("90")
    assert roh.summenvermerke[0].einheit == "Fl"
    assert _hinweise(roh, Pruefcode.SUMMENVERMERK) == ["Summenvermerk erkannt: Gesamt: 90 Fl."]
    assert _hinweise(roh, Pruefcode.FELD_FEHLT) == []
    assert _warnungen(roh) == []


def test_lese_eml_inhalt(emails: Path):
    inhalt = lese_eml(emails / "bestellung_cide_positionen_im_text.eml")
    assert inhalt.format == "eml"
    assert inhalt.betreff == "Bestellung BE-2026-0078 für Weinhaus Menrath"
    assert inhalt.absender == Adresse("Wein-Import-Kreis LEH", "bestellung@wein-import-kreis.example")
    assert inhalt.empfaenger[0] == Adresse("Cide srl", "ordini@cide.example")
    assert inhalt.empfaenger[1].name == "Jörg Beispiel"  # RFC-2047-kodierter Anzeigename
    assert inhalt.empfaenger[0].domain == "cide.example"
    assert inhalt.datum == date(2026, 10, 17)
    assert inhalt.text.startswith("Sehr geehrte Damen und Herren der Cide srl,")
    assert "<html>" not in inhalt.text  # text/plain bevorzugt
    assert inhalt.anhaenge == []


# ---------------------------------------------------------------------------
# Fixture 2: .eml mit PDF-Anhang
# ---------------------------------------------------------------------------


def test_eml_mit_pdf_anhang(extraktor: EmailExtraktor, emails: Path):
    datei = emails / "bestellung_cide_mit_pdf_anhang.eml"
    inhalt = lese_eml(datei)
    assert [a.dateiname for a in inhalt.anhaenge] == ["eigene_bestellung_cide.pdf"]
    assert inhalt.anhaenge[0].ist_pdf is True
    assert inhalt.anhaenge[0].inhalt.startswith(b"%PDF-")

    roh = extraktor.extrahiere(datei)
    assert roh.eingangstyp == Eingangstyp.EIGENE_BESTELLUNG_EMAIL
    assert roh.extraktionsmethode == Extraktionsmethode.EMAIL
    assert roh.quelldatei == datei  # nicht die temporäre PDF-Kopie
    # Kopf aus der Mail
    assert roh.lieferant_text == "Cide srl"
    assert roh.kunde_text == "Menrath"
    assert roh.belegnummer == "BE-2026-0077"
    # Fehlende Kopffelder aus dem PDF
    assert roh.datum == date(2026, 10, 17)
    assert roh.liefertag == date(2026, 10, 28)
    assert roh.lieferadresse == "Spedition Weinlogistik, Hafenstraße 2, 47119 Duisburg"
    # Positionen aus dem PDF, weil die Mail keine hat
    assert [p.laufnr for p in roh.positionen] == [1, 2]
    assert [p.artikelnummer for p in roh.positionen] == ["1643", "957"]
    assert [p.menge for p in roh.positionen] == [Decimal("60"), Decimal("24")]
    assert all(p.preis == Decimal("4.20") for p in roh.positionen)
    assert all(p.volumen_cl == Decimal("75") for p in roh.positionen)
    assert roh.gesamtmenge == Decimal("84")
    assert any("aus dem PDF-Anhang" in t for t in _hinweise(roh, Pruefcode.FELD_FEHLT, Schweregrad.INFO))
    assert _warnungen(roh) == []
    assert roh.rohtext and "Anhänge: eigene_bestellung_cide.pdf" in roh.rohtext


# ---------------------------------------------------------------------------
# Fixture 3: .txt mit Outlook-Kopfzeilen
# ---------------------------------------------------------------------------


def test_txt_bollina(extraktor: EmailExtraktor, emails: Path):
    datei = emails / "bestellung_bollina.txt"
    inhalt = lese_txt(datei)
    assert inhalt.format == "txt"
    assert inhalt.betreff == "Bestellung an Bollina srl für Hegmann"
    assert inhalt.empfaenger == [Adresse("Bollina srl", "ordini@bollina.example")]
    assert inhalt.datum == date(2026, 10, 16)
    assert inhalt.text.startswith("Sehr geehrte Damen und Herren,")

    roh = extraktor.extrahiere(datei)
    assert roh.eingangstyp == Eingangstyp.EIGENE_BESTELLUNG_EMAIL
    assert roh.lieferant_text == "Bollina srl"
    assert roh.kunde_text == "Hegmann"
    assert roh.belegnummer == "BE-2026-0079"
    assert roh.datum == date(2026, 10, 16)
    assert roh.liefertag is None
    assert roh.lieferadresse == "Spedition Weinlogistik, Hafenstraße 2, 47119 Duisburg"
    assert [p.laufnr for p in roh.positionen] == [1, 2]
    p1, p2 = roh.positionen
    assert p1.bezeichnung == "Bollina Gavi di Gavi DOCG 2023 0,75 l"
    assert p1.menge == Decimal("36")
    assert p1.einheit == "Fl"
    assert p1.preis == Decimal("7.80")
    assert p1.jahrgang == 2023
    assert p1.volumen_cl == Decimal("75")
    assert p2.bezeichnung == "Bollina Gavi di Gavi DOCG 2023 0,375 l"
    assert p2.menge == Decimal("12")
    assert p2.preis == Decimal("4.50")
    assert p2.volumen_cl == Decimal("37.5")
    warnungen = _hinweise(roh, Pruefcode.FELD_FEHLT, Schweregrad.WARNUNG)
    assert warnungen == ["Liefertag nicht als Datum lesbar: 'KW 44'."]
    assert [h.details.get("rohtext") for h in roh.hinweise if h.code == Pruefcode.FELD_FEHLT] == ["KW 44"]


def test_txt_cp1252(extraktor: EmailExtraktor, tmp_path: Path):
    text = (
        "An: Cide srl <ordini@cide.example>\nBetreff: Bestellung BE-2026-0080 für Hegmann\n\n"
        "Sehr geehrte Damen und Herren,\n\n- 12 Fl Gloria Secco Frizzante à 4,20 €\n\n"
        "Lieferung bitte bis 30.10.2026 nach Düsseldorf.\n\nMit freundlichen Grüßen\n"
    )
    datei = tmp_path / "bestellung.txt"
    datei.write_bytes(text.encode("cp1252"))
    roh = extraktor.extrahiere(datei)
    assert roh.kunde_text == "Hegmann"
    assert roh.lieferant_text == "Cide srl"
    assert roh.belegnummer == "BE-2026-0080"
    assert roh.liefertag == date(2026, 10, 30)
    assert roh.positionen[0].bezeichnung == "Gloria Secco Frizzante"
    assert roh.positionen[0].preis == Decimal("4.20")
    assert "Düsseldorf" in (roh.rohtext or "")


def test_txt_ohne_kopfzeilen(extraktor: EmailExtraktor, tmp_path: Path):
    datei = tmp_path / "notiz.txt"
    datei.write_text(
        "Liebes Team von Bollina,\n\nfür Kunde Weinhaus Menrath bitte:\n\n36 x Gavi di Gavi 2023\n6 x Gavi di Gavi 2023 Magnum\n",
        encoding="utf-8",
    )
    inhalt = lese_txt(datei)
    assert inhalt.betreff is None and inhalt.empfaenger == [] and inhalt.datum is None
    roh = extraktor.extrahiere(datei)
    assert roh.lieferant_text == "Bollina"
    assert roh.kunde_text == "Weinhaus Menrath"
    assert [(p.menge, p.jahrgang, p.volumen_cl) for p in roh.positionen] == [
        (Decimal("36"), 2023, None),
        (Decimal("6"), 2023, Decimal("150")),
    ]
    assert roh.datum is None
    assert "Belegdatum nicht erkannt." in _hinweise(roh, Pruefcode.FELD_FEHLT, Schweregrad.WARNUNG)
    assert "Bestellnummer (Belegnummer) nicht erkannt." in _hinweise(roh, Pruefcode.FELD_FEHLT, Schweregrad.WARNUNG)


def test_lese_pseudokopf():
    kopf, rest = lese_pseudokopf(
        "\nVon: A <a@b.example>\nGesendet: Freitag, 17. Oktober 2026 09:12\nAn: Cide srl <o@cide.example>;\n"
        " Kollege <k@b.example>\nCc: C <c@b.example>\nBetreff: Bestellung\n\nHallo\nText"
    )
    assert kopf == {
        "von": "A <a@b.example>",
        "datum": "Freitag, 17. Oktober 2026 09:12",
        "an": "Cide srl <o@cide.example>; Kollege <k@b.example>",
        "cc": "C <c@b.example>",
        "betreff": "Bestellung",
    }
    assert rest == "Hallo\nText"
    assert lese_pseudokopf("Datum: 17.10.2026\nHallo") == ({}, "Datum: 17.10.2026\nHallo")
    assert lese_pseudokopf("Hallo\nVon: x") == ({}, "Hallo\nVon: x")


# ---------------------------------------------------------------------------
# HTML, Zitate, Weiterleitungen
# ---------------------------------------------------------------------------


def test_html_zu_text():
    html = (
        "<html><head><title>x</title><style>p{color:red}</style></head><body>"
        "<p>Hallo</p><script>alert(1)</script><table><tr><td>60</td><td>Fl</td><td>Gloria</td></tr>"
        "<tr><td>24</td><td>Fl</td><td>Rosato</td></tr></table><div>Gr&uuml;&szlig;e&nbsp;&amp; Dank<br/>Ende</div>"
        "</body></html>"
    )
    text = html_zu_text(html)
    assert "alert" not in text and "color" not in text and "x" not in text.split("\n")[0]
    assert "60   Fl   Gloria" in text
    assert "24   Fl   Rosato" in text
    assert "Grüße & Dank\nEnde" in text
    assert html_zu_text(None) == "" and html_zu_text("") == ""


def test_eml_nur_html(extraktor: EmailExtraktor, tmp_path: Path):
    html = (
        "<html><body><p>Sehr geehrte Damen und Herren der Cide srl,</p>"
        "<p>für unseren Kunden Menrath:</p><ul><li>60 Fl Gloria Secco à 4,20 €</li><li>24 Fl Rosato Secco à 4,20 €</li></ul>"
        "<p>Liefertermin: 28.10.2026</p></body></html>"
    )
    datei = _eml(tmp_path / "html.eml", html=html, betreff="Bestellung BE-2026-0081")
    inhalt = lese_eml(datei)
    assert "<p>" not in inhalt.text and "Gloria Secco" in inhalt.text
    roh = extraktor.extrahiere(datei)
    assert roh.lieferant_text == "Cide srl"
    assert roh.kunde_text == "Menrath"
    assert [p.menge for p in roh.positionen] == [Decimal("60"), Decimal("24")]
    assert roh.liefertag == date(2026, 10, 28)
    assert roh.belegnummer == "BE-2026-0081"


def test_teile_zitat():
    assert teile_zitat("Hallo\nText") == ("Hallo\nText", None)
    eigen, zitat = teile_zitat("FYI\n\n-----Ursprüngliche Nachricht-----\nVon: A\nAn: B\n\n> 60 Fl Gloria\n> Ende")
    assert eigen == "FYI"
    assert zitat == "Von: A\nAn: B\n\n60 Fl Gloria\nEnde"
    eigen, zitat = teile_zitat("Danke!\nAm 16.10.2026 um 10:00 schrieb Cide srl:\n> alt")
    assert eigen == "Danke!" and zitat == "alt"
    eigen, zitat = teile_zitat("Ok\n\nVon: A <a@b.example>\nGesendet: Freitag\nAn: B\nBetreff: X\n\nText")
    assert eigen == "Ok" and zitat.startswith("Von: A <a@b.example>")
    eigen, zitat = teile_zitat("Eigener Text\n> zitiert\n>> doppelt")
    assert eigen == "Eigener Text" and zitat == "zitiert\ndoppelt"


def test_weitergeleitete_bestellung(extraktor: EmailExtraktor, tmp_path: Path):
    """Die Hülle ("FYI") enthält nichts; Kopf und Positionen kommen aus der weitergeleiteten Mail."""
    text = (
        "FYI – bitte einbuchen.\n\n"
        "-----Ursprüngliche Nachricht-----\n"
        "Von: Wein-Import-Kreis LEH <bestellung@wein-import-kreis.example>\n"
        "Gesendet: Donnerstag, 15. Oktober 2026 16:20\n"
        "An: Cantine Colosi <ordini@colosi.example>\n"
        "Betreff: Bestellung BE-2026-0082 für Hegmann\n\n"
        "Sehr geehrte Damen und Herren,\n\n"
        "> 24 Fl Colosi Nero d'Avola Sicilia DOC 2022 à 5,10 €\n"
        "> 12 Fl Colosi Grillo Sicilia DOC 2023 à 5,30 €\n\n"
        "Lieferung bitte bis 30.10.2026.\n"
    )
    datei = _eml(
        tmp_path / "fwd.eml",
        text=text,
        von="Jörg Beispiel <j.beispiel@wein-import-kreis.example>",
        an="Eingang <eingang@wein-import-kreis.example>",
        betreff="WG: Bestellung BE-2026-0082 für Hegmann",
    )
    roh = extraktor.extrahiere(datei)
    assert roh.lieferant_text == "Cantine Colosi"
    assert roh.kunde_text == "Hegmann"
    assert roh.belegnummer == "BE-2026-0082"
    assert roh.datum == date(2026, 10, 15)
    assert roh.liefertag == date(2026, 10, 30)
    assert [(p.menge, p.jahrgang, p.preis) for p in roh.positionen] == [
        (Decimal("24"), 2022, Decimal("5.10")),
        (Decimal("12"), 2023, Decimal("5.30")),
    ]
    assert any("zitierten" in t for t in _hinweise(roh, Pruefcode.FELD_FEHLT, Schweregrad.INFO))
    assert _warnungen(roh) == []


# ---------------------------------------------------------------------------
# Positionen aus Textzeilen
# ---------------------------------------------------------------------------


def test_lese_positionen_formen():
    zeilen = [
        "- 60 Fl Villa Armellina Gloria Secco Frizzante 0,75 l à 4,20 €",
        "• 24 Fl. Villa Armellina Rosato Secco Frizzante à 4,20 EUR",
        "* 6 Flaschen Villa Armellina Rosato Secco Frizzante Magnum 1,5 l 9,50 €/Fl",
        "  11+1",
        "Bollina Gavi di Gavi DOCG 2023 0,75 l: 36 Fl.",
        "Bollina Gavi di Gavi DOCG 2023 0,375 l – 12 Fl. à 4,50 €",
        "Art.-Nr. 1643 60 Fl Gloria Secco 4,20 €/Fl",
        "957 24 Fl Rosato Secco",
        "12 x 6 Colosi Grillo 2023",
        "3. 18 x Nero d'Avola 2022 8,40 EUR",
        "2 Kt à 6 Salina Bianco 2023 gratis",
    ]
    positionen = lese_positionen(zeilen)
    assert [p.laufnr for p in positionen] == list(range(1, 11))  # "11+1" ist eine Hinweiszeile
    werte = [(p.bezeichnung, p.menge, p.einheit, p.preis, p.jahrgang, p.volumen_cl, p.artikelnummer, p.hinweis) for p in positionen]
    assert werte[0] == ("Villa Armellina Gloria Secco Frizzante 0,75 l", Decimal("60"), "Fl", Decimal("4.20"), None, Decimal("75"), None, None)
    assert werte[1] == ("Villa Armellina Rosato Secco Frizzante", Decimal("24"), "Fl", Decimal("4.20"), None, None, None, None)
    assert werte[2] == ("Villa Armellina Rosato Secco Frizzante Magnum 1,5 l", Decimal("6"), "Fl", Decimal("9.50"), None, Decimal("150"), None, "11+1")
    assert werte[3] == ("Bollina Gavi di Gavi DOCG 2023 0,75 l", Decimal("36"), "Fl", None, 2023, Decimal("75"), None, None)
    assert werte[4] == ("Bollina Gavi di Gavi DOCG 2023 0,375 l", Decimal("12"), "Fl", Decimal("4.50"), 2023, Decimal("37.5"), None, None)
    assert werte[5] == ("Gloria Secco", Decimal("60"), "Fl", Decimal("4.20"), None, None, "1643", None)
    assert werte[6] == ("Rosato Secco", Decimal("24"), "Fl", None, None, None, "957", None)
    assert werte[7] == ("Colosi Grillo 2023", Decimal("72"), "Fl", None, 2023, None, None, None)
    assert werte[8] == ("Nero d'Avola 2022", Decimal("18"), None, Decimal("8.40"), 2022, None, None, None)
    assert werte[9] == ("Salina Bianco 2023", Decimal("12"), "Fl", None, 2023, None, None, "gratis")
    assert all(p.durchgestrichen is False for p in positionen)
    assert positionen[0].rohtext == zeilen[0]


def test_lese_positionen_lose_zeilen_nur_im_block():
    # Zwei Zeilen "Menge Bezeichnung" ohne Einheit/Preis nebeneinander -> Positionen
    assert [p.menge for p in lese_positionen(["60 Gloria Secco", "24 Rosato Secco"])] == [Decimal("60"), Decimal("24")]
    # Leerzeile dazwischen ist erlaubt
    assert len(lese_positionen(["60 Gloria Secco", "", "24 Rosato Secco"])) == 2
    # Eine einzelne lose Zeile ist keine Position
    assert lese_positionen(["Wir haben", "3 Fragen zum Angebot", "und melden uns"]) == []
    # Lose Zeile neben einer sicheren Zeile zählt
    assert len(lese_positionen(["60 Fl Gloria Secco", "24 Rosato Secco"])) == 2


@pytest.mark.parametrize(
    "zeile",
    [
        "Kundennr: 3441",
        "Tel: 0211 123456",
        "Gesamt: 90 Fl.",
        "Summe netto: 352,80 €",
        "Liefertermin: 28.10.2026",
        "Lieferadresse: Spedition Weinlogistik, Hafenstraße 2, 47119 Duisburg",
        "Hafenstraße 2, 47119 Duisburg",
        "40210 Düsseldorf",
        "1 x Preisliste anbei",
        "2 Exemplare als PDF",
        "Sehr geehrte Damen und Herren,",
        "Bestellung vom 17.10.2026",
        "10 Tage Zahlungsziel",
        "zzgl. 19 % MwSt.",
    ],
)
def test_lese_positionen_keine_position(zeile: str):
    assert lese_positionen([zeile, "60 Fl Gloria Secco", zeile]) == lese_positionen(["60 Fl Gloria Secco"])


def test_lese_positionen_volumenzahl_ist_kein_preis():
    (p,) = lese_positionen(["60 Fl Gloria Secco 2023 0,75"])
    assert p.preis is None
    assert p.volumen_cl == Decimal("75")
    assert p.bezeichnung == "Gloria Secco 2023 0,75"
    (p,) = lese_positionen(["60 Fl Gloria Secco 2023 6,90"])
    assert p.preis == Decimal("6.90")


def test_lese_positionen_leer():
    assert lese_positionen([]) == []
    assert lese_positionen(["", "   ", "Hallo"]) == []


# ---------------------------------------------------------------------------
# Namen
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("zeile", "erwartet"),
    [
        ("Sehr geehrte Damen und Herren der Cide srl,", "Cide srl"),
        ("Sehr geehrte Damen und Herren von Cantine Colosi!", "Cantine Colosi"),
        ("Liebes Team von Bollina,", "Bollina"),
        ("Hallo Cide-Team,", "Cide"),
        ("Guten Tag Cide srl,", "Cide srl"),
        ("Spett.le Cantine Colosi,", "Cantine Colosi"),
        ("Gentili Signori della Bollina srl,", "Bollina srl"),
        ("Dear Cide team,", "Cide"),
        ("Sehr geehrter Herr Rossi,", None),
        ("Hallo Marco,", None),
        ("Sehr geehrte Damen und Herren,", None),
        ("Hallo zusammen,", None),
        ("hiermit bestellen wir", None),
        ("", None),
    ],
)
def test_lieferant_aus_anrede(zeile: str, erwartet: str | None):
    assert lieferant_aus_anrede(zeile) == erwartet


@pytest.mark.parametrize(
    ("text", "erwartet"),
    [
        ("Bestellung BE-2026-0078 für Weinhaus Menrath", [("Weinhaus Menrath", "fuer")]),
        ("hiermit bestellen wir für unseren Kunden Weinhaus Menrath:", [("Weinhaus Menrath", "fuer_kunden")]),
        ("für Kunde Menrath bitte liefern", [("Menrath", "fuer_kunden")]),
        ("Bestellung für Menrath – KW 44", [("Menrath", "fuer")]),
        ("für Hegmann, Lieferung KW 44", [("Hegmann", "fuer")]),
        ("Kommission Hegmann", [("Hegmann", "kommission")]),
        ("Kunde Weinmarkt Kirchhellen GmbH & Co. KG", [("Weinmarkt Kirchhellen GmbH & Co. KG", "kunde")]),
        ("Vielen Dank für Ihre Mühe", []),
        ("für die Lieferung bitte KW 44", []),
        ("Bitte liefern Sie für Oktober", []),
        ("", []),
    ],
)
def test_endkunde_aus_text(text: str, erwartet: list[tuple[str, str]]):
    assert endkunde_aus_text(text) == erwartet


def test_lieferant_aus_empfaenger_und_domain(extraktor: EmailExtraktor, tmp_path: Path):
    text = "Sehr geehrte Damen und Herren,\n\nfür Kunde Menrath:\n\n- 12 Fl Nero d'Avola 2022 à 5,10 €\n"
    roh = extraktor.extrahiere(_eml(tmp_path / "a.eml", text=text, an="Cantine Colosi <ordini@colosi.example>"))
    assert roh.lieferant_text == "Cantine Colosi"
    # ohne Anzeigename: Domain-Label als schwacher Hinweis
    roh = extraktor.extrahiere(_eml(tmp_path / "b.eml", text=text, an="ordini@colosi.example"))
    assert roh.lieferant_text == "colosi"
    # generische Domain und Kopie an Kollegen liefern keinen Lieferanten
    roh = extraktor.extrahiere(
        _eml(tmp_path / "c.eml", text=text, an="kollege@wein-import-kreis.example, bestellung@gmail.com")
    )
    assert roh.lieferant_text is None
    assert "Lieferant/Weingut nicht erkannt." in _hinweise(roh, Pruefcode.FELD_FEHLT, Schweregrad.WARNUNG)


def test_eigene_firma_nie_lieferant_oder_kunde(extraktor: EmailExtraktor, tmp_path: Path):
    text = (
        "Sehr geehrte Damen und Herren der Wein-Import-Kreis LEH,\n\n"
        "wir bestellen für Wein-Import-Kreis LEH:\n\n- 12 Fl Gloria Secco à 6,90 €\n"
    )
    datei = _eml(
        tmp_path / "kunde.eml",
        text=text,
        von="Weinmarkt Kirchhellen GmbH <einkauf@kirchhellen.example>",
        an="Wein-Import-Kreis LEH <bestellung@wein-import-kreis.example>",
        betreff="Bestellung B-4713",
    )
    roh = extraktor.extrahiere(datei)
    assert roh.lieferant_text is None
    assert roh.kunde_text is None
    assert roh.empfaenger_firma_text == "Wein-Import-Kreis LEH"
    assert roh.belegnummer == "B-4713"
    # Absender ist nicht die eigene Firma -> Plausibilitätswarnung
    assert any("nicht die eigene Firma" in t for t in _hinweise(roh, Pruefcode.FELD_FEHLT, Schweregrad.WARNUNG))


def test_explizite_felder_schlagen_heuristik(extraktor: EmailExtraktor, tmp_path: Path):
    text = (
        "Sehr geehrte Damen und Herren der Cide srl,\n\n"
        "Lieferant: Cantine Colosi\nKunde: Weinhandlung Müller\nKd-Nr.: 2100\n"
        "Beleg-Nr.: BE-2026-0090\nDatum: 14.10.2026\nLieferbedingung: frei Haus\n"
        "Abholadresse: Cantina, Via Roma 1, Messina\n\n- 12 Fl Nero d'Avola 2022 à 5,10 €\n"
    )
    roh = extraktor.extrahiere(_eml(tmp_path / "felder.eml", text=text, betreff="Bestellung für Menrath"))
    assert roh.lieferant_text == "Cantine Colosi"
    assert roh.kunde_text == "Weinhandlung Müller"
    assert roh.kundennummer_dokument == "2100"
    assert roh.belegnummer == "BE-2026-0090"
    assert roh.datum == date(2026, 10, 14)  # explizites Datum vor Sendedatum
    assert roh.lieferbedingung == "frei Haus"
    assert roh.ladeadresse == "Cantina, Via Roma 1, Messina"


# ---------------------------------------------------------------------------
# Anhänge
# ---------------------------------------------------------------------------


def test_mail_positionen_haben_vorrang_vor_pdf(extraktor: EmailExtraktor, tmp_path: Path, pdfs: Path):
    pdf = (pdfs / "eigene_bestellung_cide.pdf").read_bytes()
    text = "Sehr geehrte Damen und Herren der Cide srl,\n\nfür Kunde Menrath:\n- 60 Fl Gloria Secco à 4,20 €\n- 24 Fl Rosato Secco à 4,20 €\n"
    datei = _eml(
        tmp_path / "beides.eml",
        text=text,
        betreff="Bestellung BE-2026-0077",
        anhaenge=[("bestellung.pdf", pdf, "application", "pdf")],
    )
    roh = extraktor.extrahiere(datei)
    assert [p.bezeichnung for p in roh.positionen] == ["Gloria Secco", "Rosato Secco"]
    assert any("verwendet wurden die 2 Positionen aus dem Mailtext" in t for t in _hinweise(roh, Pruefcode.FELD_FEHLT))
    assert _hinweise(roh, Pruefcode.SUMME_ABWEICHUNG) == []  # 84 = 84
    assert roh.liefertag == date(2026, 10, 28)  # fehlender Liefertag aus dem PDF
    assert roh.lieferadresse == "Spedition Weinlogistik, Hafenstraße 2, 47119 Duisburg"

    text_abweichend = text.replace("24 Fl", "12 Fl")
    datei = _eml(
        tmp_path / "abweichend.eml",
        text=text_abweichend,
        betreff="Bestellung BE-2026-0099",
        anhaenge=[("bestellung.pdf", pdf, "application", "pdf")],
    )
    roh = extraktor.extrahiere(datei)
    assert roh.belegnummer == "BE-2026-0099"
    summen = [h for h in roh.hinweise if h.code == Pruefcode.SUMME_ABWEICHUNG]
    assert len(summen) == 1 and summen[0].schweregrad == Schweregrad.WARNUNG
    assert summen[0].details == {"anhang": "bestellung.pdf", "mailtext": "72", "anhang_menge": "84"}
    assert any("weichen ab" in t for t in _hinweise(roh, Pruefcode.FELD_FEHLT, Schweregrad.WARNUNG))


def test_pdf_anhang_ohne_textebene(extraktor: EmailExtraktor, tmp_path: Path, pdfs: Path):
    scan = (pdfs / "scan_preisliste.pdf").read_bytes()
    datei = _eml(
        tmp_path / "scan.eml",
        text="Hallo Cide-Team,\n\nanbei der Scan für Hegmann.\n",
        anhaenge=[("preisliste_scan.pdf", scan, "application", "pdf")],
    )
    roh = extraktor.extrahiere(datei)
    assert roh.positionen == []
    assert roh.lieferant_text == "Cide"
    assert roh.kunde_text == "Hegmann"
    warnungen = _hinweise(roh, Pruefcode.FELD_FEHLT, Schweregrad.WARNUNG)
    assert any("keine Textebene" in t and "preisliste_scan.pdf" in t for t in warnungen)
    fehler = _hinweise(roh, Pruefcode.FELD_FEHLT, Schweregrad.FEHLER)
    assert fehler == ["Keine Positionen in der E-Mail erkannt (preisliste_scan.pdf)."]


def test_kaputter_pdf_anhang_und_bildanhang(extraktor: EmailExtraktor, tmp_path: Path):
    datei = _eml(
        tmp_path / "anhaenge.eml",
        text="Sehr geehrte Damen und Herren der Cide srl,\n\n- 6 Fl Gloria Secco à 4,20 €\n",
        anhaenge=[
            ("kaputt.pdf", b"%PDF-1.4 kaputt", "application", "pdf"),
            ("foto.jpg", b"\xff\xd8\xff\xe0", "image", "jpeg"),
            ("liste.xlsx", b"PK\x03\x04", "application", "octet-stream"),
        ],
    )
    roh = extraktor.extrahiere(datei)
    assert len(roh.positionen) == 1
    warnungen = _hinweise(roh, Pruefcode.FELD_FEHLT, Schweregrad.WARNUNG)
    assert any("kaputt.pdf" in t for t in warnungen)
    assert any("Bildanhang 'foto.jpg'" in t for t in warnungen)
    infos = _hinweise(roh, Pruefcode.FELD_FEHLT, Schweregrad.INFO)
    assert any("liste.xlsx" in t for t in infos)
    assert not any(h.schweregrad == Schweregrad.FEHLER for h in roh.hinweise)


def test_anhang_erkennung():
    assert Anhang("x.pdf", b"abc").ist_pdf is True
    assert Anhang("x.bin", b"%PDF-1.7").ist_pdf is True
    assert Anhang("x.bin", b"abc", "application/pdf").ist_pdf is True
    assert Anhang("x.txt", b"abc").ist_pdf is False
    assert Anhang("scan.TIF", b"abc").ist_bild is True
    assert Anhang("x.bin", b"abc", "image/png").ist_bild is True


def test_sicherer_dateiname():
    assert eb._sicherer_dateiname("../../etc/passwd.pdf", 1) == "01_passwd.pdf"
    assert eb._sicherer_dateiname("Bestellung Nr. 7.pdf", 2) == "02_Bestellung Nr. 7.pdf"
    assert eb._sicherer_dateiname(None, 3) == "03_anhang_3.pdf"
    assert eb._sicherer_dateiname("ohne_endung", 4) == "04_ohne_endung.pdf"


# ---------------------------------------------------------------------------
# .msg
# ---------------------------------------------------------------------------


def test_msg_ohne_extract_msg(extraktor: EmailExtraktor, tmp_path: Path, monkeypatch):
    monkeypatch.setitem(sys.modules, "extract_msg", None)
    datei = tmp_path / "bestellung.msg"
    datei.write_bytes(b"\xd0\xcf\x11\xe0")
    with pytest.raises(MsgNichtVerfuegbar):
        lese_msg(datei)
    roh = extraktor.extrahiere(datei)
    assert roh.eingangstyp == Eingangstyp.EIGENE_BESTELLUNG_EMAIL
    assert roh.extraktionsmethode == Extraktionsmethode.EMAIL
    assert roh.positionen == []
    fehler = [h for h in roh.hinweise if h.schweregrad == Schweregrad.FEHLER]
    assert len(fehler) == 1
    assert fehler[0].code == Pruefcode.FELD_FEHLT
    assert "extract_msg" in fehler[0].text and "pip install extract-msg" in fehler[0].text


class _FakeAnhang:
    def __init__(self, name: str, daten: bytes, mimetype: str | None = None) -> None:
        self.longFilename = name
        self.shortFilename = name[:8]
        self.data = daten
        self.mimetype = mimetype


class _FakeMsg:
    geschlossen = False

    def __init__(self, pfad: str) -> None:
        self.pfad = pfad
        self.subject = "Bestellung BE-2026-0083 für Menrath"
        self.sender = "Wein-Import-Kreis LEH <bestellung@wein-import-kreis.example>"
        self.to = "Cide srl <ordini@cide.example>; Kollege <k@wein-import-kreis.example>"
        self.cc = None
        from datetime import datetime

        self.date = datetime(2026, 10, 16, 9, 30)
        self.body = "Sehr geehrte Damen und Herren der Cide srl,\r\n\r\n- 60 Fl Gloria Secco à 4,20 €\r\n"
        self.htmlBody = b"<p>ignoriert</p>"
        self.attachments = [_FakeAnhang("notiz.txt", b"hallo", "text/plain")]

    def close(self) -> None:
        _FakeMsg.geschlossen = True


def test_msg_mit_fake_extract_msg(extraktor: EmailExtraktor, tmp_path: Path, monkeypatch):
    modul = types.ModuleType("extract_msg")
    modul.openMsg = _FakeMsg  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "extract_msg", modul)
    assert msg_unterstuetzt() is True
    datei = tmp_path / "bestellung.msg"
    datei.write_bytes(b"\xd0\xcf\x11\xe0")
    assert extraktor.kann_verarbeiten(datei) is True

    inhalt = lese_msg(datei)
    assert inhalt.format == "msg"
    assert inhalt.betreff == "Bestellung BE-2026-0083 für Menrath"
    assert inhalt.empfaenger[0] == Adresse("Cide srl", "ordini@cide.example")
    assert inhalt.datum == date(2026, 10, 16)
    assert inhalt.text.startswith("Sehr geehrte Damen und Herren der Cide srl,\n")
    assert [a.dateiname for a in inhalt.anhaenge] == ["notiz.txt"]
    assert _FakeMsg.geschlossen is True

    roh = extraktor.extrahiere(datei)
    assert roh.lieferant_text == "Cide srl"
    assert roh.kunde_text == "Menrath"
    assert roh.belegnummer == "BE-2026-0083"
    assert roh.datum == date(2026, 10, 16)
    assert [p.menge for p in roh.positionen] == [Decimal("60")]


def test_msg_kaputt_mit_fake_extract_msg(tmp_path: Path, monkeypatch):
    def kaputt(pfad: str):
        raise OSError("not an OLE2 file")

    modul = types.ModuleType("extract_msg")
    modul.openMsg = kaputt  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "extract_msg", modul)
    datei = tmp_path / "kaputt.msg"
    datei.write_bytes(b"x")
    with pytest.raises(EmailFehler):
        lese_msg(datei)


# ---------------------------------------------------------------------------
# Sonstiges
# ---------------------------------------------------------------------------


def test_extrahiere_inhalt_direkt(extraktor: EmailExtraktor, tmp_path: Path):
    inhalt = EmailInhalt(
        quelldatei=tmp_path / "direkt.eml",
        betreff="Bestellung BE-2026-0084 an Weingut Beispiel für Vinothek Schmidt GmbH",
        text="Guten Tag,\n\n12 x Riesling trocken 2023\n6 x Riesling trocken 2023 Magnum\n\nLieferung in KW 45.\n",
        datum=date(2026, 10, 16),
    )
    roh = extraktor.extrahiere_inhalt(inhalt)
    assert roh.lieferant_text == "Weingut Beispiel"
    assert roh.kunde_text == "Vinothek Schmidt GmbH"
    assert roh.belegnummer == "BE-2026-0084"
    assert [p.menge for p in roh.positionen] == [Decimal("12"), Decimal("6")]
    assert roh.positionen[1].volumen_cl == Decimal("150")
    assert roh.liefertag is None
    assert "Liefertag nicht als Datum lesbar: 'KW 45'." in _hinweise(roh, Pruefcode.FELD_FEHLT, Schweregrad.WARNUNG)


def test_leere_mail(extraktor: EmailExtraktor, tmp_path: Path):
    roh = extraktor.extrahiere(_eml(tmp_path / "leer.eml", text="", betreff="Test", an="jemand@example.org"))
    assert roh.positionen == []
    fehler = _hinweise(roh, Pruefcode.FELD_FEHLT, Schweregrad.FEHLER)
    assert fehler == ["Keine Positionen in der E-Mail erkannt (keine Anhänge)."]
    warnungen = _hinweise(roh, Pruefcode.FELD_FEHLT, Schweregrad.WARNUNG)
    assert "Lieferant/Weingut nicht erkannt." in warnungen
    assert "Endkunde nicht erkannt." in warnungen
    assert "Bestellnummer (Belegnummer) nicht erkannt." in warnungen
    assert "Liefertag nicht erkannt." in warnungen


def test_rohbestellung_json_roundtrip(extraktor: EmailExtraktor, emails: Path):
    for name in ("bestellung_cide_positionen_im_text.eml", "bestellung_cide_mit_pdf_anhang.eml", "bestellung_bollina.txt"):
        roh = extraktor.extrahiere(emails / name)
        kopie = RohBestellung.model_validate_json(roh.model_dump_json())
        assert kopie.positionen == roh.positionen
        assert kopie.gesamtmenge == roh.gesamtmenge
        assert kopie.liefertag == roh.liefertag
