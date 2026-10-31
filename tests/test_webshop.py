"""Online-Shop: Konfiguration, Export aus dem Artikelstamm, Bestellprüfung, Ablage und HTTP-Server."""

from __future__ import annotations

import json
import threading
from datetime import datetime
from decimal import Decimal
from http.client import HTTPConnection
from pathlib import Path

import pytest

from auftragsautomat.models import Artikel
from auftragsautomat.webshop import bestellung as b
from auftragsautomat.webshop.export import baue_produkte, brutto, exportiere
from auftragsautomat.webshop.konfig import WebshopKonfig, lade_webshop_konfig
from auftragsautomat.webshop.server import erzeuge_server

STAMM = Path(__file__).resolve().parent.parent
MONTAG_VORMITTAG = datetime(2026, 11, 2, 9, 30)  # Montag, vor Bestellschluss


@pytest.fixture()
def konfig(tmp_path: Path) -> WebshopKonfig:
    k = lade_webshop_konfig(STAMM)
    k.ausgabe_ordner = tmp_path / "data"
    k.bestellungen_ordner = tmp_path / "eingang" / "webshop"
    k.mail_ordner = tmp_path / "logs" / "mails"
    return k


@pytest.fixture()
def katalog(konfig: WebshopKonfig) -> b.Produktkatalog:
    exportiere(konfig, {})
    return b.Produktkatalog.aus_datei(konfig.ausgabe_ordner / "produkte.json")


def _anfrage(**aenderungen) -> dict:
    roh = {
        "lieferart": "lastenrad",
        "positionen": [{"artikelnummer": "WS-ZENAS-01", "menge": 6}, {"artikelnummer": "WS-RIES-01", "menge": 6}],
        "kunde": {"name": "Frau Beispiel", "email": "beispiel@example.org", "strasse": "Musterweg 1", "plz": "50677"},
        "zeitfenster": "2026-11-03|vm",
        "alter_bestaetigt": True,
        "agb_akzeptiert": True,
    }
    roh.update(aenderungen)
    return roh


# ---------------------------------------------------------------- Konfiguration und Export


def test_konfig_laedt_zonen_und_zeitfenster(konfig: WebshopKonfig):
    assert konfig.lieferung.zone_fuer("50677").id == "kern"
    assert konfig.lieferung.zone_fuer("50968").gebuehr == Decimal("4.90")
    assert konfig.lieferung.zone_fuer("40210") is None
    assert konfig.lieferung.zeitfenster_nach_id("ab").tage == [4]
    assert konfig.laden.oeffnungszeiten[4] == ("09:00", "18:30")


def test_brutto_rundet_kaufmaennisch():
    assert brutto(Decimal("8.40"), Decimal("19")) == Decimal("10.00")
    assert brutto(Decimal("12.50"), Decimal("19")) == Decimal("14.88")


def test_export_nimmt_stammpreis_vor_beispielpreis(konfig: WebshopKonfig):
    artikel = Artikel(
        firma_id="weinstrasse_agentur", artikelnummer="WS-ZENAS-01", bezeichnung="Zenas",
        vk_preis_1=Decimal("8.40"), volumen_cl=Decimal(75), jahrgang=2023,
    )
    gesperrt = Artikel(
        firma_id="weinstrasse_agentur", artikelnummer="WS-RIES-01", bezeichnung="Riesling", vk_preis_1=Decimal("9"), aktiv=False,
    )
    ergebnis = baue_produkte(konfig, {"WS-ZENAS-01": artikel, "WS-RIES-01": gesperrt})
    zenas = next(p for p in ergebnis.produkte if p["id"] == "WS-ZENAS-01")
    assert zenas["preis"] == 10.0 and zenas["beispielpreis"] is False and zenas["jahrgang"] == 2023
    assert zenas["grundpreis_liter"] == pytest.approx(13.33)
    assert "WS-RIES-01" in ergebnis.uebersprungen
    assert ergebnis.aus_stamm == 1 and ergebnis.beispielpreise == len(konfig.sortiment) - 2


def test_export_schreibt_beide_dateien(konfig: WebshopKonfig):
    exportiere(konfig, {})
    produkte = json.loads((konfig.ausgabe_ordner / "produkte.json").read_text(encoding="utf-8"))
    lieferung = json.loads((konfig.ausgabe_ordner / "lieferung.json").read_text(encoding="utf-8"))
    assert len(produkte["produkte"]) == len(konfig.sortiment)
    assert {z["id"] for z in lieferung["zonen"]} == {"kern", "rand"}
    assert lieferung["laden"]["oeffnungszeiten"]["4"] == ["09:00", "18:30"]


# ---------------------------------------------------------------- Termine


def test_fruehester_liefertag_beachtet_bestellschluss_und_wochenende():
    assert b.fruehester_liefertag(datetime(2026, 11, 2, 11, 59), 12).isoformat() == "2026-11-03"
    assert b.fruehester_liefertag(datetime(2026, 11, 2, 12, 0), 12).isoformat() == "2026-11-04"
    assert b.fruehester_liefertag(datetime(2026, 11, 6, 13, 0), 12).isoformat() == "2026-11-09"  # Fr nach 12 -> Mo
    tage = b.angebotene_tage(datetime(2026, 11, 5, 14, 0), 12, 4)  # Do nach 12 -> Mo..Do
    assert [t.isoformat() for t in tage] == ["2026-11-09", "2026-11-10", "2026-11-11", "2026-11-12"]


# ---------------------------------------------------------------- Prüfung


def test_lastenrad_bestellung_wird_ausgepreist(konfig: WebshopKonfig, katalog: b.Produktkatalog):
    annahme = b.nimm_bestellung_an(_anfrage(), konfig, katalog, jetzt=MONTAG_VORMITTAG, senden=False)
    best = annahme.bestellung
    assert best.nummer == "WS-261102-001"
    assert best.flaschen == 12 and best.zone == "kern" and best.lieferkosten == Decimal("0.00")
    assert best.zwischensumme == Decimal("6") * Decimal("9.90") + Decimal("6") * Decimal("10.90")
    assert best.gesamt == best.zwischensumme
    assert best.zeitfenster_text == "03.11.2026, 10:00–13:00 Uhr"
    assert best.kunde.ort == "Köln"
    assert annahme.datei.exists() and annahme.gesendet is False
    assert [p.name for p in annahme.mail_entwuerfe] == ["WS-261102-001_kunde.eml", "WS-261102-001_laden.eml"]
    assert "Zahlung" not in annahme.mail_entwuerfe[0].read_text(encoding="utf-8")
    gespeichert = json.loads(annahme.datei.read_text(encoding="utf-8"))
    assert gespeichert["firma"] == "weinstrasse_agentur" and gespeichert["positionen"][0]["menge"] == 6


def test_nummern_laufen_je_tag_fort(konfig: WebshopKonfig, katalog: b.Produktkatalog):
    a1 = b.nimm_bestellung_an(_anfrage(), konfig, katalog, jetzt=MONTAG_VORMITTAG, senden=False)
    a2 = b.nimm_bestellung_an(_anfrage(), konfig, katalog, jetzt=MONTAG_VORMITTAG, senden=False)
    assert (a1.bestellung.nummer, a2.bestellung.nummer) == ("WS-261102-001", "WS-261102-002")


@pytest.mark.parametrize(
    ("aenderung", "erwartet"),
    [
        ({"positionen": [{"artikelnummer": "WS-ZENAS-01", "menge": 11}]}, "ab 12 Flaschen"),
        ({"kunde": {"name": "Frau Beispiel", "email": "beispiel@example.org", "strasse": "Weg 1", "plz": "40210"}}, "außerhalb"),
        ({"zeitfenster": "2026-11-03|ab"}, "gibt es an dem Tag nicht"),  # Feierabend nur donnerstags
        ({"zeitfenster": "2026-11-02|vm"}, "außerhalb der angebotenen Tage"),  # heute selbst
        ({"zeitfenster": None}, "Zeitfenster"),
        ({"alter_bestaetigt": False}, "18 Jahre"),
        ({"agb_akzeptiert": False}, "AGB"),
        ({"positionen": [{"artikelnummer": "GIBTS-NICHT", "menge": 12}]}, "im Sortiment"),
        ({"kunde": {"name": "X", "email": "keine-adresse", "strasse": "Weg 1", "plz": "50677"}}, "Ungültige Angabe"),
        ({"positionen": []}, "Ungültige Angabe"),
    ],
)
def test_fehlerhafte_bestellungen_werden_abgelehnt(konfig: WebshopKonfig, katalog: b.Produktkatalog, aenderung: dict, erwartet: str):
    with pytest.raises(b.BestellFehler, match=erwartet):
        b.nimm_bestellung_an(_anfrage(**aenderung), konfig, katalog, jetzt=MONTAG_VORMITTAG, senden=False)
    assert not list(konfig.bestellungen_ordner.glob("*.json")) if konfig.bestellungen_ordner.exists() else True


def test_randgebiet_kostet_gebuehr_und_preise_kommen_vom_server(konfig: WebshopKonfig, katalog: b.Produktkatalog):
    roh = _anfrage(kunde={"name": "Frau Beispiel", "email": "beispiel@example.org", "strasse": "Weg 1", "plz": "50968"})
    roh["positionen"][0]["preis"] = 0.01  # vom Browser mitgeschickte Preise werden ignoriert
    best = b.nimm_bestellung_an(roh, konfig, katalog, jetzt=MONTAG_VORMITTAG, senden=False).bestellung
    assert best.zone == "rand" and best.lieferkosten == Decimal("4.90")
    assert best.gesamt == best.zwischensumme + Decimal("4.90")
    assert best.positionen[0].einzelpreis == Decimal("9.90")


def test_abholung_nur_an_oeffnungstagen(konfig: WebshopKonfig, katalog: b.Produktkatalog):
    best = b.nimm_bestellung_an(
        _anfrage(lieferart="abholung", zeitfenster="2026-11-05", positionen=[{"artikelnummer": "WS-GIN-01", "menge": 1}]),
        konfig, katalog, jetzt=MONTAG_VORMITTAG, senden=False,
    ).bestellung
    assert best.lieferkosten == 0 and best.flaschen == 1 and "18:30" in best.zeitfenster_text
    with pytest.raises(b.BestellFehler, match="geschlossen"):
        b.nimm_bestellung_an(_anfrage(lieferart="abholung", zeitfenster="2026-11-07"), konfig, katalog, jetzt=MONTAG_VORMITTAG, senden=False)


def test_versand_mit_und_ohne_versandkosten(konfig: WebshopKonfig, katalog: b.Produktkatalog):
    kunde = {"name": "Frau Beispiel", "email": "beispiel@example.org", "strasse": "Weg 1", "plz": "10115", "ort": "Berlin"}
    klein = b.nimm_bestellung_an(
        _anfrage(lieferart="versand", kunde=kunde, zeitfenster=None, positionen=[{"artikelnummer": "WS-ZENAS-01", "menge": 2}]),
        konfig, katalog, jetzt=MONTAG_VORMITTAG, senden=False,
    ).bestellung
    assert klein.lieferkosten == Decimal("6.90")
    gross = b.nimm_bestellung_an(
        _anfrage(lieferart="versand", kunde=kunde, zeitfenster=None, positionen=[{"artikelnummer": "WS-CHAMP-01", "menge": 3}]),
        konfig, katalog, jetzt=MONTAG_VORMITTAG, senden=False,
    ).bestellung
    assert gross.lieferkosten == Decimal("0.00") and gross.gesamt == Decimal("149.70")
    with pytest.raises(b.BestellFehler, match="Straße, Postleitzahl und Ort"):
        b.nimm_bestellung_an(_anfrage(lieferart="versand", zeitfenster=None), konfig, katalog, jetzt=MONTAG_VORMITTAG, senden=False)


def test_bestelltext_und_mails(konfig: WebshopKonfig, katalog: b.Produktkatalog):
    annahme = b.nimm_bestellung_an(_anfrage(), konfig, katalog, jetzt=MONTAG_VORMITTAG, senden=False)
    text = b.bestelltext(annahme.bestellung, konfig)
    assert "6 x Zenas Primitivo" in text and "Gesamt inkl. 19 % MwSt." in text and "Lastenrad-Lieferung: kostenlos" in text
    kunde, laden = b.erzeuge_mails(annahme.bestellung, konfig)
    assert kunde["To"] == "beispiel@example.org" and kunde["From"] == konfig.laden.email
    assert "WS-261102-001" in laden["Subject"] and "12 Fl." in laden["Subject"]
    assert b.sende_mails((kunde, laden), konfig) is False  # kein SMTP konfiguriert


# ---------------------------------------------------------------- HTTP-Server


@pytest.fixture()
def server(konfig: WebshopKonfig, katalog: b.Produktkatalog, tmp_path: Path):
    website = tmp_path / "website"
    website.mkdir()
    (website / "index.html").write_text("<!DOCTYPE html><title>Test</title>", encoding="utf-8")
    srv = erzeuge_server(konfig, website, "127.0.0.1", 0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def _request(srv, methode: str, pfad: str, body: dict | None = None):
    conn = HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
    conn.request(methode, pfad, body=json.dumps(body).encode() if body is not None else None, headers={"Content-Type": "application/json"})
    antwort = conn.getresponse()
    daten = antwort.read()
    conn.close()
    return antwort.status, daten


def test_server_liefert_statische_dateien_und_status(server):
    status, daten = _request(server, "GET", "/")
    assert status == 200 and b"<title>Test</title>" in daten
    status, daten = _request(server, "GET", "/api/status")
    assert status == 200 and json.loads(daten)["produkte"] == 12


def test_server_nimmt_bestellung_an_und_lehnt_fehler_ab(server, konfig: WebshopKonfig):
    jetzt = datetime.now()
    tage = b.angebotene_tage(jetzt, konfig.lieferung.bestellschluss_uhr, konfig.lieferung.tage_voraus)
    status, daten = _request(server, "POST", "/api/bestellung", _anfrage(zeitfenster=f"{tage[0].isoformat()}|vm"))
    assert status == 201, daten
    antwort = json.loads(daten)
    assert antwort["nummer"].startswith(f"WS-{jetzt:%y%m%d}-") and antwort["gesamt"] == pytest.approx(124.8)
    assert (konfig.bestellungen_ordner / f"{antwort['nummer']}.json").exists()

    status, daten = _request(server, "POST", "/api/bestellung", _anfrage(alter_bestaetigt=False))
    assert status == 422 and "18 Jahre" in json.loads(daten)["fehler"]
    status, _ = _request(server, "POST", "/api/woanders", {})
    assert status == 404
    conn = HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    conn.request("POST", "/api/bestellung", body=b"kein json", headers={"Content-Type": "application/json"})
    assert conn.getresponse().status == 400
    conn.close()
