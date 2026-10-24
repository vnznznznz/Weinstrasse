"""Tests für ueberwachung/bericht.py: Markdown + JSON schreiben, jüngsten Bericht laden."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

from auftragsautomat.models import (
    EmailEntwurf,
    EntwurfTyp,
    Ereignistyp,
    Fixtermin,
    Kontakt,
    KontaktAenderung,
    Kontaktgruppe,
    MailEreignis,
    Sendung,
    SendungStatus,
    Terminaenderung,
    Ueberwachungsbericht,
    UeberwachungsPunkt,
)
from auftragsautomat.ueberwachung.bericht import (
    bericht_dateiname,
    bericht_markdown,
    lade_bericht,
    lade_letzten_bericht,
    lade_letzten_bericht_mit_zusatz,
    schreibe_bericht,
)


def _ft(**felder) -> Fixtermin:
    basis = {"lieferant_kurz": "Weingut Beispiel", "kunde_kurz": "Beispielmarkt", "liefertag": date(2026, 11, 11), "bestellnummer": "WIK-2026-0100", "zeile": 12}
    basis.update(felder)
    return Fixtermin(**basis)


def _bericht() -> Ueberwachungsbericht:
    punkte = [
        UeberwachungsPunkt(fixtermin=_ft(lieferant_bestaetigt=True, spedition_bestaetigt=True, abgeholt=True), ampel="gruen", grund="abgeholt"),
        UeberwachungsPunkt(
            fixtermin=_ft(kunde_kurz="Markt | Zwei", liefertag=date(2026, 10, 30)),
            ampel="rot",
            grund="Bestätigung des Lieferanten überfällig",
            aktionen=["Nachfrage Lieferant: Entwurf 'Order WIK-2026-0100' an orders@weingut-beispiel.example.org"],
            sendung=Sendung(spedition="Hillebrand", referenz="DEMB00000", status=SendungStatus.BESTAETIGT, quelle="mail", geplante_zustellung=date(2026, 11, 9)),
        ),
        UeberwachungsPunkt(fixtermin=_ft(kunde_kurz="Dritter"), ampel="gelb", grund="Spedition offen", aktionen=["Nachfrage Spedition gedrosselt (zuletzt 23.10.2026, erneut nach 2 Werktagen)"]),
    ]
    entwurf = EmailEntwurf(typ=EntwurfTyp.NACHFRAGE_LIEFERANT, an=["orders@weingut-beispiel.example.org"], betreff="Order WIK-2026-0100", text_html="<p>x</p>", text_plain="x", speicherort="/tmp/e.eml", hinweise=["Anhang fehlt"])
    ereignis = MailEreignis(typ=Ereignistyp.TERMINAENDERUNG, absender="vertrieb@musterhof.example.org", betreff="neuer Liefertermin", empfangen=datetime(2026, 10, 23, 8, 5), neues_datum=date(2026, 11, 3))
    kontakt = Kontakt(gruppe=Kontaktgruppe.LIEFERANT, firma="Weingut Beispiel Srl", name="Marco Esempio", quelle="Lieferanten!6")
    return Ueberwachungsbericht(
        zeitpunkt=datetime(2026, 10, 26, 7, 5),
        zeitraum_bis=date(2026, 11, 16),
        punkte=punkte,
        entwuerfe=[entwurf],
        terminaenderungen=[Terminaenderung(fixtermin=_ft(kunde_kurz="Dritter"), neues_datum=date(2026, 11, 3), grund="Terminänderung laut Mail", quelle=ereignis)],
        kontaktaenderungen=[KontaktAenderung(kontakt=kontakt, feld="telefon", alt="+39 0123 456780", neu="+39 000 0000099", quelle="Mail m1")],
        fehler=["Tracking Weingut Beispiel / Dritter: RuntimeError: Portal nicht erreichbar"],
    )


def test_markdown_inhalt():
    md = bericht_markdown(_bericht(), hinweise=["Nicht zugeordnet: sonstiges 'x'"], protokoll=["Zeile 12: Häkchen gesetzt"])
    assert md.startswith("# Überwachungslauf 26.10.2026\n")
    assert "3 Fixtermine (ROT 1, GELB 1, GRÜN 1)" in md
    zeilen = md.splitlines()
    tabelle = [z for z in zeilen if z.startswith("| ") and "ROT" in z or z.startswith("| GELB") or z.startswith("| GRÜN")]
    assert [z.split("|")[1].strip() for z in tabelle] == ["ROT", "GELB", "GRÜN"]  # dringend zuerst
    assert "| ROT | 30.10.2026 | Weingut Beispiel | Markt / Zwei |  |  |  | bestaetigt, Ref. DEMB00000, Quelle mail, Zustellung 09.11.2026 | Bestätigung des Lieferanten überfällig |" in md
    assert "| GRÜN | 11.11.2026 | Weingut Beispiel | Beispielmarkt | x | x | x | – | abgeholt |" in md
    assert "### 30.10.2026 – Weingut Beispiel / Markt | Zwei (ROT)" in md and "- Nachfrage Lieferant: Entwurf" in md
    assert "**nachfrage_lieferant** – Order WIK-2026-0100 → orders@weingut-beispiel.example.org; Entwurf: `/tmp/e.eml`; Hinweis: Anhang fehlt" in md
    assert "- [Vorschlag, Freigabe nötig] Weingut Beispiel / Dritter: 11.11.2026 → 03.11.2026 – Terminänderung laut Mail (Mail: neuer Liefertermin, vertrieb@musterhof.example.org, 23.10.2026)" in md
    assert "telefon '+39 0123 456780' -> '+39 000 0000099'" in md
    assert "- Tracking Weingut Beispiel / Dritter: RuntimeError: Portal nicht erreichbar" in md
    assert "## Hinweise\n\n- Nicht zugeordnet" in md and "## Änderungen durch den Lauf\n\n- Zeile 12" in md


def test_markdown_leer():
    md = bericht_markdown(Ueberwachungsbericht(zeitpunkt=datetime(2026, 10, 26, 7, 0), zeitraum_bis=date(2026, 11, 16)))
    assert "keine Fixtermine im Zeitraum" in md and "Keine Maßnahmen fällig." in md and "Keine Entwürfe erzeugt." in md
    assert "Keine Terminänderungen gemeldet." in md and "Keine Änderungsvorschläge zur Kontaktliste." in md
    assert "Keine Fehler." in md and "Keine Hinweise." in md and "Änderungen durch den Lauf" not in md


def test_schreiben_und_laden(tmp_path: Path):
    ordner = tmp_path / "berichte"
    bericht = _bericht()
    md = schreibe_bericht(bericht, ordner, hinweise=["H1"], protokoll=["P1"])
    assert md == ordner / "ueberwachung_2026-10-26.md" and bericht_dateiname(bericht.zeitpunkt) == "ueberwachung_2026-10-26"
    js = ordner / "ueberwachung_2026-10-26.json"
    daten = json.loads(js.read_text(encoding="utf-8"))
    assert daten["hinweise"] == ["H1"] and daten["protokoll"] == ["P1"] and len(daten["punkte"]) == 3
    assert not [p for p in ordner.iterdir() if p.suffix == ".tmp"]
    geladen = lade_letzten_bericht(ordner)
    assert geladen == bericht
    assert lade_letzten_bericht_mit_zusatz(ordner) == (bericht, ["H1"], ["P1"])
    assert lade_bericht(js) == bericht
    # jüngster Bericht gewinnt (Datum im Dateinamen), kaputte Datei wird übersprungen
    neuer = bericht.model_copy(update={"zeitpunkt": datetime(2026, 10, 27, 7, 0), "punkte": []})
    schreibe_bericht(neuer, ordner)
    assert lade_letzten_bericht(ordner) == neuer
    (ordner / "ueberwachung_2026-10-28.json").write_text("{kaputt", encoding="utf-8")
    assert lade_letzten_bericht(ordner) == neuer
    assert lade_letzten_bericht(tmp_path / "leer") is None and lade_bericht(tmp_path / "nix.json") is None
    # zweiter Lauf am selben Tag ersetzt die Dateien
    schreibe_bericht(neuer.model_copy(update={"fehler": ["neu"]}), ordner)
    assert lade_letzten_bericht(ordner).fehler == ["neu"]  # type: ignore[union-attr]
