"""Namen und Zielordner der Dossiers im Ordner "vorangefertigte Dossiers".

* :func:`dossier_name` – Name des Dossier-Unterordners
  ``<Lieferant>-<Firma>-<Kunde>-<Bestellnr>-<TT.MM.JJJJ>`` (Design ``docs/AUSBAUSTUFE_2.md``,
  Abschnitt 2.7). Kurznamen und Bereinigung kommen aus ``benennung.py`` (Titulierung); fällt der
  Import aus (paralleler Aufbau), greift eine schlichte Rückfalllogik mit derselben Semantik.
* :func:`zielordner_vorschlag` – Vorschlag, wohin der Mitarbeiter das Dossier einsortiert:
  ``Auftragsablage (mit|ohne Provision)/<Kunde_kurz>/<Jahr>`` (``settings.dossier``).
* :func:`dateityp_aus_name` – Dossier-Dateityp aus einem Dateinamen (Dokumentkürzel der
  Titulierung, ältere Hausmuster wie ``-Kd-``/``AB-``/``LS-``/``Hillebrand-`` und Dossier-Präfixe).

Es werden hier keine Belegdateinamen gebildet; Ordnernamen sind keine Belege.
"""

from __future__ import annotations

import logging
import re
from datetime import date

from auftragsautomat.config import Settings
from auftragsautomat.models import (
    KUERZEL_DATEITYP,
    Belegpaket,
    BuchungsErgebnis,
    DossierDateityp,
)

__all__ = [
    "OHNE_NUMMER",
    "ORDNER_TRENNER",
    "UNBEKANNT",
    "dateityp_aus_name",
    "dossier_name",
    "stichtag",
    "zielordner_vorschlag",
]

logger = logging.getLogger(__name__)

#: Trenner zwischen den Teilen des Dossier-Ordnernamens (Hausmuster der Auftragsablage).
ORDNER_TRENNER = "-"
#: Platzhalter, wenn keine Bestellnummer bekannt ist.
OHNE_NUMMER = "ohne-Nr"
#: Platzhalter für fehlende Kurznamen.
UNBEKANNT = "Unbekannt"

_STANDARD_ERSATZ = "-"
_STANDARD_MAX_LAENGE = 180
_VERBOTENE_ZEICHEN = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]+')
_MEHRFACH_LEERZEICHEN = re.compile(r"\s+")
#: Zerlegt Dateinamen in Wörter: Gedankenstrich, Bindestrich, Unterstrich, Punkt, Leerzeichen.
_TOKEN_TRENNER = re.compile(r"[\s_\-–—.]+")

#: Wörter in Dateinamen -> Dossier-Dateityp (Vergleich ohne Groß-/Kleinschreibung und Umlaute).
_WORT_TYPEN: dict[str, DossierDateityp] = {
    "deckblatt": "deckblatt",
    "kundenbestellung": "kundenbestellung",
    "bestellung": "bestellung",
    "angebot": "angebot",
    "auftragskopie": "auftragskopie",
    "speditionsauftrag": "speditionsauftrag",
    "speditionsbestaetigung": "speditionsbestaetigung",
    "lieferschein": "lieferschein",
    "reklamation": "reklamation",
    "rabattliste": "rabattliste",
    "rabatt": "rabattliste",
    "fixtermin": "fixtermin",
    "gesamt": "gesamt_pdf",
    "komplett": "gesamt_pdf",
    "mail": "mail",
    "mails": "mail",
    # Hausmuster aus der bisherigen Ablage
    "ab": "auftragskopie",
    "ls": "lieferschein",
    "hillebrand": "speditionsauftrag",
    "spedition": "speditionsauftrag",
    "bestaetigungwg": "auftragsbestaetigung_lieferant",
}
_MAIL_ENDUNGEN = (".eml", ".msg")


def _normalisiere(text: str) -> str:
    """Vergleichsform (casefold, Umlaute als ae/oe/ue/ss) – lazy aus ``benennung``, sonst eigen."""
    try:
        from auftragsautomat.benennung import normalisiere

        return normalisiere(text)
    except ImportError:  # pragma: no cover - nur während des parallelen Aufbaus
        return text.casefold().translate(str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"}))


def _bereinige(text: str, settings: Settings | None) -> str:
    """Dateinamentauglicher Text über ``benennung.bereinige``; ohne settings/Modul derselbe Algorithmus."""
    if settings is not None:
        try:
            from auftragsautomat.benennung import bereinige

            return bereinige(text, settings)
        except ImportError:  # pragma: no cover - nur während des parallelen Aufbaus
            logger.warning("benennung.py nicht importierbar; Dossier-Name wird mit Rückfalllogik bereinigt.")
    ersatz = settings.benennung.ersatzzeichen if settings is not None else _STANDARD_ERSATZ
    max_laenge = settings.benennung.max_laenge if settings is not None else _STANDARD_MAX_LAENGE
    bereinigt = _VERBOTENE_ZEICHEN.sub(ersatz, text)
    bereinigt = _MEHRFACH_LEERZEICHEN.sub(" ", bereinigt).strip().rstrip(" .")
    if len(bereinigt) > max_laenge:
        bereinigt = bereinigt[:max_laenge].rstrip(" .")
    return bereinigt


def _kurznamen(paket: Belegpaket) -> tuple[str, str]:
    """``(lieferant_kurz, kunde_kurz)`` über ``benennung`` (Matchcode, Name, Dokumenttext)."""
    try:
        from auftragsautomat.benennung import kurzname_kunde, kurzname_lieferant

        return kurzname_lieferant(paket), kurzname_kunde(paket)
    except ImportError:  # pragma: no cover - nur während des parallelen Aufbaus
        lieferant = paket.lieferant.lieferant
        kunde = paket.kunde.kunde
        lieferant_kurz = (lieferant.matchcode or lieferant.name) if lieferant else (paket.roh.lieferant_text or "")
        kunde_kurz = (kunde.matchcode or kunde.name) if kunde else (paket.roh.kunde_text or "")
        return (lieferant_kurz or "").strip() or UNBEKANNT, (kunde_kurz or "").strip() or UNBEKANNT


def _firma_kurz(paket: Belegpaket, settings: Settings | None) -> str:
    """Firmenkürzel (WS, WIK, WOE): ``Firma.kurz`` aus den Settings, sonst aus der Firmen-ID abgeleitet."""
    if not paket.firma_id:
        return UNBEKANNT
    if settings is not None and paket.firma_id in settings.firmen:
        return settings.firmen[paket.firma_id].kurz
    return paket.firma_id.split("_")[0].upper()


def _bestellnummer(paket: Belegpaket, ergebnis: BuchungsErgebnis | None) -> str:
    """Kundenbestellnummer, sonst Belegnummer der Einkauf-Bestellung / des Angebots / der AB,
    sonst Belegnummer des Dokuments, sonst ``ohne-Nr``."""
    kandidaten: list[str | None] = [paket.roh.bestellnummer]
    if ergebnis is not None:
        for teil in (ergebnis.einkauf, ergebnis.angebot, ergebnis.verkauf):
            kandidaten.append(teil.belegnummer if teil is not None else None)
    kandidaten.append(paket.roh.belegnummer)
    for kandidat in kandidaten:
        if kandidat and str(kandidat).strip():
            return str(kandidat).strip()
    return OHNE_NUMMER


def stichtag(paket: Belegpaket, ergebnis: BuchungsErgebnis | None = None, *, datum: date | None = None) -> date:
    """Datum für Ordner- und Dateinamen eines Dossiers: ``datum``, sonst Zeitpunkt der Buchung,
    sonst Belegdatum des Dokuments, sonst heute. Dieselbe Eingabe ergibt denselben Namen
    (Idempotenz des Dossiers)."""
    if datum is not None:
        return datum
    if ergebnis is not None:
        return ergebnis.zeitpunkt.date()
    if paket.roh.datum is not None:
        return paket.roh.datum
    return date.today()


def dossier_name(
    paket: Belegpaket,
    ergebnis: BuchungsErgebnis | None = None,
    *,
    settings: Settings | None = None,
    datum: date | None = None,
) -> str:
    """Name des Dossier-Ordners: ``<Lieferant>-<Firma>-<Kunde>-<Bestellnr>-<TT.MM.JJJJ>``.

    Kurznamen über ``benennung.kurzname_lieferant``/``kurzname_kunde``, Firma = ``Firma.kurz``
    (``settings``), Bestellnummer = Kundenbestellnummer, sonst Lexware-Belegnummer aus ``ergebnis``,
    sonst Belegnummer des Dokuments, sonst ``ohne-Nr``. Datum laut :func:`stichtag`. Das Ergebnis
    ist mit ``benennung.bereinige`` dateinamentauglich (Umlaute bleiben).
    """
    lieferant_kurz, kunde_kurz = _kurznamen(paket)
    teile = [
        lieferant_kurz,
        _firma_kurz(paket, settings),
        kunde_kurz,
        _bestellnummer(paket, ergebnis),
        stichtag(paket, ergebnis, datum=datum).strftime("%d.%m.%Y"),
    ]
    # Verbotene Zeichen werden je Teil durch Leerzeichen ersetzt; Bindestriche in den Kurznamen
    # (Lieferant, Firma, Kunde) ebenfalls, weil der Bindestrich der Trenner ist. Nummern wie
    # "B-4711" behalten ihren Bindestrich.
    bereinigte = []
    for i, teil in enumerate(teile):
        text = _VERBOTENE_ZEICHEN.sub(" ", teil)
        if i < 3:
            text = text.replace(ORDNER_TRENNER, " ")
        bereinigte.append(_MEHRFACH_LEERZEICHEN.sub(" ", text).strip(" .") or UNBEKANNT)
    return _bereinige(ORDNER_TRENNER.join(bereinigte), settings)


def zielordner_vorschlag(paket: Belegpaket, settings: Settings, *, datum: date | None = None) -> str:
    """Vorschlag für den Zielordner in der Auftragsablage:
    ``<Auftragsablage (mit|ohne Provision)>/<Kunde_kurz>/<Jahr>``.

    "mit Provision", wenn ``paket.firma_id`` in ``settings.dossier.firmen_mit_provision`` steht
    (Standard: Weinstrasse Agentur), sonst "ohne Provision". Jahr = ``datum``, sonst Belegdatum,
    sonst Liefertag, sonst heute. Das Tool verschiebt nichts; der Vorschlag steht im Deckblatt.
    """
    k = settings.dossier
    basis = k.ablage_mit_provision if paket.firma_id in k.firmen_mit_provision else k.ablage_ohne_provision
    _, kunde_kurz = _kurznamen(paket)
    jahr = datum or paket.roh.datum or paket.roh.liefertag or date.today()
    kunde_teil = _bereinige(kunde_kurz, settings) or UNBEKANNT
    return f"{basis}/{kunde_teil}/{jahr.year}"


def dateityp_aus_name(name: str) -> DossierDateityp:
    """Dossier-Dateityp aus einem Dateinamen.

    Erkannt werden die Dokumentkürzel der Titulierung (``Kd``, ``Cde``, ``Kopie``, ``Oc``, ``Neu``,
    ``Neu Kopie``, ``Rekl``, ``Ls``, ``Speditionsauftrag``, ``Speditionsbestätigung``), die
    bisherigen Hausmuster (``-Kd-``, ``-Cde-``, ``Auftragskopie-``, ``AB-``, ``LS-``,
    ``Hillebrand-``/``Spedition``), die Dossier-Präfixe (``01_Kundenbestellung_…``) sowie
    ``dossier.json`` (``index``), ``Dossier_komplett.pdf`` (``gesamt_pdf``) und ``.eml``/``.msg``
    (``mail``). Unbekanntes ergibt ``sonstiges``.
    """
    text = (name or "").strip()
    if not text:
        return "sonstiges"
    basis = text.rsplit("/", 1)[-1]
    klein = basis.casefold()
    if klein == "dossier.json":
        return "index"
    if klein.endswith(_MAIL_ENDUNGEN):
        return "mail"
    stamm = basis.rsplit(".", 1)[0] if "." in basis else basis
    woerter = [w for w in _TOKEN_TRENNER.split(stamm) if w]
    norm = [_normalisiere(w) for w in woerter]

    # Mehrwort-Kürzel zuerst: "Neu Kopie" (Revised Order) und "Dossier komplett".
    for i in range(len(norm) - 1):
        if norm[i] == "neu" and norm[i + 1] == "kopie":
            return "revision"
        if norm[i] == "dossier" and norm[i + 1] == "komplett":
            return "gesamt_pdf"

    kuerzel_norm = {_normalisiere(k): typ for k, typ in KUERZEL_DATEITYP.items() if " " not in k}
    for wort in norm:
        if wort in kuerzel_norm:
            return kuerzel_norm[wort]
    for wort in norm:
        if wort in _WORT_TYPEN:
            return _WORT_TYPEN[wort]
    return "sonstiges"
