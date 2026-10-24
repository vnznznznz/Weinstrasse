"""Zugriffsschicht Microsoft 365 (Excel, Mail, Dateien) mit den Backends graph, lokal und fake.

Öffentliche Einstiegspunkte: :func:`erzeuge_backends`, :func:`pruefe_anmeldung`, :func:`anmelden`
(``fabrik``), die Protokolle und Modelle aus ``basis`` sowie die Backend-Klassen der Teilmodule.
"""

from auftragsautomat.m365.basis import (
    DateiBackend,
    DateiInfo,
    ExcelBackend,
    M365Fehler,
    M365Konflikt,
    M365NichtAngemeldet,
    M365NichtGefunden,
    Mail,
    MailAnhang,
    MailBackend,
    MailKopf,
    Zellbereich,
    datei_schluessel,
    datum_zu_serienzahl,
    excel_sitzung,
    html_zu_text,
    parse_adresse,
    serienzahl_zu_datum,
    spaltenbuchstabe,
    spaltenindex,
)
from auftragsautomat.m365.fabrik import Backends, anmelden, erzeuge_backends, pruefe_anmeldung
from auftragsautomat.m365.fake import FakeDateiBackend, FakeExcelBackend, FakeMailBackend, mail_aus_eml

__all__ = [
    "Backends",
    "DateiBackend",
    "DateiInfo",
    "ExcelBackend",
    "FakeDateiBackend",
    "FakeExcelBackend",
    "FakeMailBackend",
    "M365Fehler",
    "M365Konflikt",
    "M365NichtAngemeldet",
    "M365NichtGefunden",
    "Mail",
    "MailAnhang",
    "MailBackend",
    "MailKopf",
    "Zellbereich",
    "anmelden",
    "datei_schluessel",
    "datum_zu_serienzahl",
    "erzeuge_backends",
    "excel_sitzung",
    "html_zu_text",
    "mail_aus_eml",
    "parse_adresse",
    "pruefe_anmeldung",
    "serienzahl_zu_datum",
    "spaltenbuchstabe",
    "spaltenindex",
]
