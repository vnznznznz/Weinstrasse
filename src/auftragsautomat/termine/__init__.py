"""Fixtermin-Register für die ``Fixterminüberwachung.xlsx`` (docs/AUSBAUSTUFE_2.md, Abschnitt 2.2).

* ``register``  – :class:`FixterminRegister` (Slots je Tag lesen, eintragen, aktualisieren,
  Häkchen setzen, verschieben, offene Termine) und :func:`fixtermin_aus_paket`.
* ``kurznamen`` – Kurznamen für Lieferant/Kunde (``config/aliase/kurznamen.yaml`` ->
  Lexware-Matchcode -> Namensbestandteil) mit Abgleich gegen die Dropdown-Ressourcenliste.
* ``kalender``  – Werktagsrechnung mit Feiertagen (``holidays``, Bundesland aus
  ``settings.ueberwachung.bundesland``).
"""

from auftragsautomat.termine.kalender import (
    KalenderFehler,
    addiere_werktage,
    feiertag,
    ist_feiertag,
    ist_werktag,
    ist_wochenende,
    kw,
    naechster_werktag,
    vorheriger_werktag,
    werktage_im_zeitraum,
    werktage_zwischen,
    wochentag_kurz,
)
from auftragsautomat.termine.kurznamen import (
    KurznameErgebnis,
    Kurznamen,
    KurznamenDaten,
    KurznamenFehler,
    erster_wortbestandteil,
    lade_kurznamen_yaml,
    vorhandene_kurznamen,
)
from auftragsautomat.termine.register import (
    FixterminRegister,
    RegisterFehler,
    RegisterVoll,
    bemerkung_teile,
    bemerkung_zusammenfuegen,
    fixtermin_aus_paket,
)

__all__ = [
    "FixterminRegister",
    "KalenderFehler",
    "KurznameErgebnis",
    "Kurznamen",
    "KurznamenDaten",
    "KurznamenFehler",
    "RegisterFehler",
    "RegisterVoll",
    "addiere_werktage",
    "bemerkung_teile",
    "bemerkung_zusammenfuegen",
    "erster_wortbestandteil",
    "feiertag",
    "fixtermin_aus_paket",
    "ist_feiertag",
    "ist_werktag",
    "ist_wochenende",
    "kw",
    "lade_kurznamen_yaml",
    "naechster_werktag",
    "vorhandene_kurznamen",
    "vorheriger_werktag",
    "werktage_im_zeitraum",
    "werktage_zwischen",
    "wochentag_kurz",
]
