"""Dateinamen für Mail-Anhänge – dünne Schicht über der Titulierung (``auftragsautomat.benennung``).

Geschäftsregel 18: Dateinamen für Belege, Mail-Anhänge, Dossier-Dateien und Ablage kommen
ausschließlich aus ``src/auftragsautomat/benennung.py``. Dieses Modul baut deshalb keine eigenen
Muster, sondern bietet dem Kommunikationsmodul (und anderen Aufrufern) bequeme Einstiege je
Dokumentart, die alle auf :func:`auftragsautomat.benennung.dateiname`, :func:`speditionsname`
und :func:`name_fuer_paket` zurückführen:

* :func:`bestellung_pdf` – Bestellung ans Weingut ("Cde", bei Revision "Neu")
* :func:`auftragskopie_pdf` – Auftragskopie an den Kunden ("Kopie" / "Neu Kopie"; nie "Auftragsbestätigung")
* :func:`speditionsauftrag_pdf` – Speditionsschema ``Spedition – Speditionsauftrag – Lieferant – Kunde|Bestell-Nr – Datum``
* :func:`lieferschein_pdf` – Lieferschein ("Ls")
* :func:`anhangsname` – Name aus dem Belegpaket (``name_fuer_paket``)
* :func:`firmenkuerzel`, :func:`firma_fuer`, :func:`bereinige`, :func:`pruefe_auftragskopie_name`

Umlaute und Leerzeichen bleiben erhalten (Titulierung); verbotene Zeichen ersetzt ``benennung.bereinige``.
"""

from __future__ import annotations

import logging
from datetime import date

from auftragsautomat import benennung as _titulierung
from auftragsautomat.benennung import ist_agentur, normalisiere, pruefe_auftragskopie_name
from auftragsautomat.config import Firma, Settings
from auftragsautomat.models import Belegpaket

logger = logging.getLogger(__name__)

__all__ = [
    "anhangsname",
    "auftragskopie_pdf",
    "bereinige",
    "bestellung_pdf",
    "enthaelt_verbotene_woerter",
    "firma_fuer",
    "firmenkuerzel",
    "lieferschein_pdf",
    "normalisiert",
    "pruefe_auftragskopie_name",
    "speditionsauftrag_pdf",
]


def firma_fuer(firma: Firma | str, settings: Settings) -> Firma:
    """``Firma`` aus einem ``Firma``-Objekt, einer Firmen-ID (``wik_leh``) oder einem Kürzel (``WIK``).

    Ein Kürzel, das mehrere Firmen tragen (WIK = wik_fh_seh und wik_leh), ergibt die erste in der
    Reihenfolge von ``config/firmen.yaml`` – für Dateinamen ist nur das Kürzel relevant.
    ``KeyError`` bei unbekannter Firma.
    """
    if isinstance(firma, Firma):
        return firma
    text = (firma or "").strip()
    if text in settings.firmen:
        return settings.firmen[text]
    for kandidat in settings.firmen.values():
        if kandidat.kurz.casefold() == text.casefold():
            return kandidat
    raise KeyError(f"Unbekannte Firma '{firma}'. Bekannt: {', '.join(settings.firmen)}")


def firmenkuerzel(firma: Firma | str, settings: Settings | None = None) -> str:
    """Kürzel für Dateinamen (WS / WIK / WOE). Ohne ``settings`` wird die ID selbst abgeleitet
    (``wik_leh`` -> ``WIK``), ein bereits kurzes Kürzel bleibt."""
    if isinstance(firma, Firma):
        return firma.kurz
    if settings is not None:
        try:
            return firma_fuer(firma, settings).kurz
        except KeyError:
            logger.warning("Firma '%s' unbekannt – Kürzel aus dem Text abgeleitet.", firma)
    text = (firma or "").strip()
    return text.split("_")[0].upper() if text else ""


def bereinige(text: str, settings: Settings | None = None) -> str:
    """Dateinamentauglicher Text nach Titulierung (``benennung.bereinige``); ohne ``settings`` mit den
    Standardwerten der ``BenennungKonfig``."""
    return _titulierung.bereinige(text, settings or Settings())


def _kuerzel(basis: str, revision: bool) -> str:
    return _titulierung.REVISIONS_KUERZEL.get(basis, basis) if revision else basis


def bestellung_pdf(
    lieferant_kurz: str,
    firma: Firma | str,
    bestellnr: str | None,
    datum: date | None,
    *,
    settings: Settings,
    kunde: str | None = None,
    revision: bool = False,
    endung: str = ".pdf",
) -> str:
    """Name der Bestellung ans Weingut (Kürzel "Cde", bei ``revision`` "Neu").

    WIK/WOE: ``Lieferant – WIK|WOE – Bestell-Nr – Cde – Datum``; Weinstrasse Agentur:
    ``Lieferant – Kunde – [Auftragsnummer –] Cde – Datum`` (``kunde`` nötig, ``bestellnr`` =
    Auftragsnummer für Kunden mit Auftragsnummer).
    """
    f = firma_fuer(firma, settings)
    return _titulierung.dateiname(
        _kuerzel("Cde", revision),
        firma=f,
        lieferant=lieferant_kurz,
        kunde=kunde or "",
        bestellnummer=bestellnr,
        auftragsnummer=bestellnr,
        datum=datum,
        settings=settings,
        endung=endung,
    )


def auftragskopie_pdf(
    lieferant_kurz: str,
    kunde_kurz: str,
    bestellnr: str | None,
    datum: date | None,
    *,
    firma: Firma | str,
    settings: Settings,
    revision: bool = False,
    endung: str = ".pdf",
) -> str:
    """Name der Auftragskopie an den Kunden (Kürzel "Kopie", bei ``revision`` "Neu Kopie").

    Der Name enthält nie "Auftragsbestätigung"; die Titulierung warnt, wenn Kunden-/Lieferantenname
    ein verbotenes Wort enthalten (verbindlich prüft der Aufrufer mit :func:`pruefe_auftragskopie_name`).
    """
    f = firma_fuer(firma, settings)
    return _titulierung.dateiname(
        _kuerzel("Kopie", revision),
        firma=f,
        lieferant=lieferant_kurz,
        kunde=kunde_kurz,
        bestellnummer=bestellnr,
        auftragsnummer=bestellnr,
        datum=datum,
        settings=settings,
        endung=endung,
    )


def speditionsauftrag_pdf(
    spedition: str | None,
    lieferant_kurz: str,
    firma: Firma | str,
    bestellnr: str | None,
    datum: date | None,
    *,
    settings: Settings,
    kunde: str | None = None,
    endung: str = ".pdf",
) -> str:
    """``Spedition – Speditionsauftrag – Lieferant – Kunde|Bestell-Nr – Datum``: vierter Teil ist der
    Kunde bei der Weinstrasse Agentur oder ohne Bestell-Nr, sonst die Bestell-Nr."""
    f = firma_fuer(firma, settings)
    nummer = (bestellnr or "").strip() or None
    vierter = kunde if ist_agentur(f) or nummer is None else nummer
    return _titulierung.speditionsname(
        "Speditionsauftrag",
        spedition=spedition,
        lieferant=lieferant_kurz,
        kunde_oder_bestellnr=vierter,
        datum=datum,
        settings=settings,
        endung=endung,
    )


def lieferschein_pdf(
    lieferant_kurz: str,
    firma: Firma | str,
    bestellnr: str | None,
    datum: date | None,
    *,
    settings: Settings,
    kunde: str | None = None,
    endung: str = ".pdf",
) -> str:
    """Name des Lieferscheins (Kürzel "Ls") nach dem Firmenschema."""
    f = firma_fuer(firma, settings)
    return _titulierung.dateiname(
        "Ls",
        firma=f,
        lieferant=lieferant_kurz,
        kunde=kunde or "",
        bestellnummer=bestellnr,
        auftragsnummer=bestellnr,
        datum=datum,
        settings=settings,
        endung=endung,
    )


def anhangsname(kuerzel: str, paket: Belegpaket, settings: Settings, *, datum: date | None = None, endung: str = ".pdf") -> str:
    """Name eines Mail-Anhangs aus dem Belegpaket (= ``benennung.name_fuer_paket``)."""
    return _titulierung.name_fuer_paket(kuerzel, paket, settings, datum=datum, endung=endung)


def enthaelt_verbotene_woerter(text: str | None, settings: Settings) -> list[str]:
    """Verbotene Wörter der Auftragskopie in einem beliebigen Text (Betreff, Mailtext, Dateiname);
    Vergleich wie :func:`pruefe_auftragskopie_name` (casefold, ohne Umlaute)."""
    if not text:
        return []
    return pruefe_auftragskopie_name(text, settings)


def normalisiert(text: str | None) -> str:
    """Vergleichsform (``benennung.normalisiere``) – für Aufrufer, die Treffer selbst vergleichen."""
    return normalisiere(text)
