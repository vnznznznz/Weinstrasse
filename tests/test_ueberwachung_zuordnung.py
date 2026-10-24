"""Tests für ueberwachung/zuordnung.py: Mail-Ereignis -> Fixtermin über Referenz, Nummer, Dossier, Text."""

from __future__ import annotations

from datetime import date, datetime

from auftragsautomat.models import Dossier, Ereignistyp, Fixtermin, MailEreignis
from auftragsautomat.ueberwachung.zuordnung import (
    Zuordnung,
    dossier_fuer,
    dossier_fuer_fixtermin,
    kurzname_woerter,
    norm_nummer,
    ordne_ereignis_zu,
    ordne_zu,
)

EMPFANGEN = datetime(2026, 10, 20, 11, 20)


def _ft(**felder) -> Fixtermin:
    basis = {"lieferant_kurz": "Weingut Beispiel", "kunde_kurz": "Beispielmarkt", "liefertag": date(2026, 11, 9), "zeile": 10}
    basis.update(felder)
    return Fixtermin(**basis)


def _ereignis(typ: Ereignistyp = Ereignistyp.SPEDITION_BESTAETIGUNG, **felder) -> MailEreignis:
    basis = {"typ": typ, "absender": "dispo@hillebrand.example.org", "betreff": "Auftragsbestätigung", "empfangen": EMPFANGEN, "mail_id": "m1"}
    basis.update(felder)
    return MailEreignis(**basis)


def _dossier(**felder) -> Dossier:
    basis = {"name": "Weingut Beispiel-WIK-Beispielmarkt-4711-20.10.2026", "ordner": "/tmp/x", "lieferant_kurz": "Weingut Beispiel", "kunde_kurz": "Beispielmarkt"}
    basis.update(felder)
    return Dossier(**basis)


def test_norm_nummer_und_kurzname_woerter():
    assert norm_nummer(" wik-2026 0100 ") == "WIK20260100" and norm_nummer("B_4711/2") == "B47112"
    assert norm_nummer(None) is None and norm_nummer("  ") is None
    assert kurzname_woerter("Weingut Beispiel Srl") == ["beispiel"]
    assert kurzname_woerter("Cantina Alpha & Beta GmbH & Co. KG") == ["alpha", "beta"]
    assert kurzname_woerter("Weingut") == [] and kurzname_woerter(None) == []


def test_referenz_trifft_tracking_referenz_eindeutig():
    fixtermine = [_ft(tracking_referenz="DEMB00000"), _ft(kunde_kurz="Anderer", zeile=11)]
    z = ordne_zu(_ereignis(referenz="DEMB00000", kundenreferenz="4711"), fixtermine)
    assert z.fixtermin is fixtermine[0] and z.quelle == "referenz" and z.eindeutig and z.konfidenz == 1.0
    assert ordne_ereignis_zu(_ereignis(referenz="demb 00000"), fixtermine) is fixtermine[0]


def test_referenz_in_bemerkung_und_nummer_in_bemerkung():
    ft = _ft(bemerkung="Spedition Hillebrand | Ref. DEMB00000")
    z = ordne_zu(_ereignis(referenz="DEMB00000"), [ft])
    assert z.fixtermin is ft and z.quelle in ("referenz", "bemerkung") and z.eindeutig
    ft2 = _ft(bemerkung="Kunde wünscht Vormittag, Nr. WIK-2026-0100", bestellnummer=None)
    z2 = ordne_zu(_ereignis(kundenreferenz="WIK-2026-0100"), [ft2])
    assert z2.fixtermin is ft2 and z2.quelle == "bemerkung" and z2.eindeutig
    # Teilstrings zählen nicht: "47110" ist nicht "4711"
    assert ordne_zu(_ereignis(kundenreferenz="4711"), [_ft(bemerkung="Best.-Nr 47110")]).fixtermin is None


def test_kundenreferenz_trifft_bestellnummer_ab_oder_kundenbestellnummer():
    fixtermine = [_ft(bestellnummer="WIK-2026-0100"), _ft(ab_nummer="AB-55", zeile=11), _ft(kundenbestellnummer="4712", zeile=12)]
    assert ordne_zu(_ereignis(kundenreferenz="WIK 2026 0100"), fixtermine).quelle == "bestellnummer"
    assert ordne_zu(_ereignis(kundenreferenz="ab-55"), fixtermine).fixtermin is fixtermine[1]
    z = ordne_zu(_ereignis(Ereignistyp.TERMINAENDERUNG, kundenreferenz="4712", referenz=None), fixtermine)
    assert z.fixtermin is fixtermine[2] and z.quelle == "kundenbestellnummer" and z.eindeutig


def test_lieferantenbestaetigung_referenz_ist_keine_tracking_referenz():
    ft = _ft(bestellnummer="WIK-2026-0100", tracking_referenz="DEMB00000")
    e = _ereignis(Ereignistyp.LIEFERANT_BESTAETIGUNG, referenz="OC-2026-0815", kundenreferenz="WIK-2026-0100", absender="orders@weingut-beispiel.example.org")
    z = ordne_zu(e, [ft])
    assert z.fixtermin is ft and z.quelle == "bestellnummer"


def test_mehrdeutig_liefert_none_mit_kandidaten():
    a = _ft(bestellnummer="4711", zeile=10)
    b = _ft(bestellnummer="4711", zeile=11, kunde_kurz="Beispielmarkt Filiale")
    z = ordne_zu(_ereignis(kundenreferenz="4711"), [a, b])
    assert z.fixtermin is None and z.kandidaten == [a, b] and z.mehrdeutig and not z.eindeutig
    assert "mehreren" in z.grund and "Beispielmarkt Filiale" in z.grund
    # Verschiedene Tage: der nächste Termin nach dem Empfang gewinnt
    c = b.model_copy(update={"liefertag": date(2026, 10, 15)})  # vor dem Empfang
    d = a.model_copy(update={"liefertag": date(2026, 11, 20)})
    z2 = ordne_zu(_ereignis(kundenreferenz="4711"), [c, d, a])
    assert z2.fixtermin is a and z2.eindeutig


def test_konfidenz_schwelle_macht_treffer_zum_vorschlag():
    ft = _ft(bestellnummer="4711")
    z = ordne_zu(_ereignis(Ereignistyp.TERMINAENDERUNG, kundenreferenz="4711", konfidenz=0.4), [ft])
    assert z.fixtermin is ft and not z.eindeutig and z.konfidenz == 0.4
    assert ordne_zu(_ereignis(kundenreferenz="4711", konfidenz=0.4), [ft], konfidenz_min=0.3).eindeutig


def test_zuordnung_ueber_dossier():
    ft = _ft(bestellnummer=None, kundenbestellnummer=None)
    dossier = _dossier(tracking_referenz="DEMB00000", fixtermin=date(2026, 11, 9), bestellnummer="WIK-2026-0100")
    z = ordne_zu(_ereignis(referenz="DEMB00000"), [ft], [dossier])
    assert z.fixtermin is ft and z.quelle == "dossier" and z.dossier is dossier and z.eindeutig
    # Dossier über Kundenreferenz, Fixtermin über Kurznamen + Liefertag
    z2 = ordne_zu(_ereignis(kundenreferenz="WIK-2026-0100", referenz=None), [ft], [dossier])
    assert z2.fixtermin is ft and z2.quelle == "dossier"
    # Dossier ohne passenden Fixtermin: kein Treffer, aber das Dossier ist bekannt
    z3 = ordne_zu(_ereignis(referenz="DEMB00000"), [_ft(kunde_kurz="Fremd", liefertag=date(2026, 12, 1))], [dossier])
    assert z3.fixtermin is None and z3.dossier is dossier and not z3.kandidaten


def test_dossier_fuer_und_dossier_fuer_fixtermin():
    d1 = _dossier(bestellnummer="4711")
    d2 = _dossier(name="zwei", tracking_referenz="DEMB00001", fixtermin=date(2026, 11, 9))
    assert dossier_fuer(_ereignis(referenz="DEMB00001"), None, [d1, d2]) is d2
    assert dossier_fuer(_ereignis(kundenreferenz="4711"), None, [d1, d2]) is d1
    assert dossier_fuer(_ereignis(), _ft(bestellnummer="4711"), [d1, d2]) is d1
    assert dossier_fuer_fixtermin(_ft(), [d1, d2]) is d2  # Kurznamen + Liefertag
    assert dossier_fuer_fixtermin(_ft(kunde_kurz="Niemand"), [d1, d2]) is None


def test_textzuordnung_ist_nur_vorschlag():
    fixtermine = [_ft(liefertag=date(2026, 11, 9)), _ft(kunde_kurz="Weinhandlung Muster", liefertag=date(2026, 11, 12), zeile=11)]
    e = _ereignis(Ereignistyp.LIEFERANT_BESTAETIGUNG, betreff="Order confirmation for Beispielmarkt", text_auszug="Weingut Beispiel Srl confirms", referenz=None, kundenreferenz=None)
    z = ordne_zu(e, fixtermine)
    assert z.fixtermin is fixtermine[0] and z.quelle == "text" and z.konfidenz == 0.8 and not z.eindeutig
    # nur der Lieferant im Text -> mehrere Kandidaten -> der nächste Termin, geringe Konfidenz
    e2 = _ereignis(betreff="Weingut Beispiel – Nachricht", referenz=None)
    z2 = ordne_zu(e2, fixtermine)
    assert z2.fixtermin is fixtermine[0] and z2.konfidenz == 0.5
    # Textsuche abschaltbar
    z3 = ordne_zu(e, fixtermine, text_suche=False)
    assert z3.fixtermin is None and z3.quelle is None and "kein Fixtermin passt" in z3.grund


def test_zuordnung_dataclass_defaults():
    z = Zuordnung(None)
    assert z.quelle is None and z.kandidaten == [] and not z.eindeutig and not z.mehrdeutig
