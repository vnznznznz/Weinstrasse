"""Vorangefertigte Dossiers: je Auftrag ein Ordner mit allen Belegen, Deckblatt und Index.

Design: ``docs/AUSBAUSTUFE_2.md`` Abschnitt 2.7. Öffentliche Einstiegspunkte:

* :class:`DossierVerwaltung` (``erzeugung``) – anlegen/fortschreiben, finden, auflisten, Mails ergänzen,
* :func:`erzeuge_deckblatt` (``deckblatt``) – Deckblatt-PDF,
* :func:`dossier_name`, :func:`zielordner_vorschlag`, :func:`dateityp_aus_name` (``ablage``).
"""

from auftragsautomat.dossier.ablage import dateityp_aus_name, dossier_name, stichtag, zielordner_vorschlag
from auftragsautomat.dossier.deckblatt import CHECKLISTE, CHECKLISTE_REIHENFOLGE, erzeuge_deckblatt, zahl_text
from auftragsautomat.dossier.erzeugung import (
    DECKBLATT,
    FIXTERMIN_DATEI,
    GESAMT_PDF,
    INDEX_DATEI,
    MAIL_ORDNER,
    PAKET_DATEI,
    PRAEFIX_JE_KUERZEL,
    DossierVerwaltung,
    eml_aus_entwurf,
    fixtermin_text,
    mail_dateiname,
)

__all__ = [
    "CHECKLISTE",
    "CHECKLISTE_REIHENFOLGE",
    "DECKBLATT",
    "DossierVerwaltung",
    "FIXTERMIN_DATEI",
    "GESAMT_PDF",
    "INDEX_DATEI",
    "MAIL_ORDNER",
    "PAKET_DATEI",
    "PRAEFIX_JE_KUERZEL",
    "dateityp_aus_name",
    "dossier_name",
    "eml_aus_entwurf",
    "erzeuge_deckblatt",
    "fixtermin_text",
    "mail_dateiname",
    "stichtag",
    "zahl_text",
    "zielordner_vorschlag",
]
