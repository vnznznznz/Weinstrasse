"""Kontaktliste "Ansprechpartner Abwicklung.xlsx" (docs/AUSBAUSTUFE_2.md, Abschnitt 2.3).

* ``liste``: Datei lesen (lokal per openpyxl auf einer Kopie, nur lesend; oder über ein
  ``ExcelBackend``) -> :class:`Kontaktliste` mit Lieferanten, Speditionen, Kunden.
* ``zuordnung``: :class:`Zuordner` – Ansprechpartner je Lexware-Lieferant/-Kunde, Spedition je
  Kunde, Postfächer der Spedition nach Zweck (aus der Liste, nie fest kodiert), Anrede/Sprache, CC.
* ``aenderungen``: Signaturen eingehender Mails auswerten, Änderungen vorschlagen
  (:class:`~auftragsautomat.models.KontaktAenderung`) und nach Freigabe in die Excel schreiben.
"""

from auftragsautomat.kontakte.aenderungen import (
    FELD_NEU,
    FELDER_UEBERNEHMBAR,
    Signatur,
    aenderungen_aus_mail,
    bericht,
    erkenne_signatur,
    normalisiere_telefon,
    telefon_gleich,
    uebernehme,
    vergleiche,
)
from auftragsautomat.kontakte.liste import (
    KontakteFehler,
    Kontaktliste,
    bereinige_speditionsname,
    kontaktliste_aus_settings,
    lade_kontaktliste,
    lies_xlsx_lokal,
    quelle_text,
    zerlege_quelle,
)
from auftragsautomat.kontakte.zuordnung import ZWECKE, Person, Zuordner, Zweck, person_aus_name, positionsrang

__all__ = [
    "FELDER_UEBERNEHMBAR",
    "FELD_NEU",
    "KontakteFehler",
    "Kontaktliste",
    "Person",
    "Signatur",
    "ZWECKE",
    "Zuordner",
    "Zweck",
    "aenderungen_aus_mail",
    "bereinige_speditionsname",
    "bericht",
    "erkenne_signatur",
    "kontaktliste_aus_settings",
    "lade_kontaktliste",
    "lies_xlsx_lokal",
    "normalisiere_telefon",
    "person_aus_name",
    "positionsrang",
    "quelle_text",
    "telefon_gleich",
    "uebernehme",
    "vergleiche",
    "zerlege_quelle",
]
