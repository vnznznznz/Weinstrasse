"""Benennungsschema ("Titulierung") für alle Dateinamen des Auftrags-Automaten.

Quelle: ``docs/AUSBAUSTUFE_2.md`` Abschnitt 6, ``docs/ARCHITEKTUR.md`` Abschnitt ``benennung.py``,
Geschäftsregel 18. Namen für Lexware-PDFs, Mail-Anhänge, Dossier-Dateien und die Ablage
"Aufträge mit Provision" / "Aufträge ohne Provision" kommen ausschließlich aus diesem Modul; andere Module bauen keine
Belegdateinamen selbst.

Schemata (Trenner ``settings.benennung.trenner``, Datum ``settings.benennung.datumsformat``):

* Weinstrasse Agentur (``Firma.kurz == "WS"``):
  ``Lieferant – Kunde – Kürzel – Datum``; Kunden aus ``benennung.kunden_mit_auftragsnummer``
  (z. B. Spar): ``Lieferant – Kunde – Auftragsnummer – Kürzel – Datum``
  (Auftragsnummer = Kundenbestellnummer; fehlt sie, Hinweis im Log und Name ohne Nummer).
* WIK / WOE: ``Lieferant – WIK|WOE – Bestell-Nr – Kürzel – Datum``
  (Bestell-Nr = Kundenbestellnummer, sonst Belegnummer des Dokuments, sonst ``ohne-Nr``).
* Spedition: ``Spedition – Speditionsauftrag|Speditionsbestätigung|Lieferschein – Lieferant –
  Kunde|Bestell-Nr – Datum``.

Dokumentkürzel sind die Schlüssel von :data:`auftragsautomat.models.DOKUMENTKUERZEL`; bei einer
Revised Order (``Belegpaket.ist_revision``) wird "Cde" zu "Neu" und "Kopie" zu "Neu Kopie".
Kurznamen sind die Lexware-Matchcodes des zugeordneten Kunden/Lieferanten, sonst der Name,
sonst der Text aus dem Dokument, sonst "Unbekannt". Umlaute bleiben erhalten (Windows und
OneDrive können sie); verbotene Zeichen werden durch ``benennung.ersatzzeichen`` ersetzt und die
Gesamtlänge wird auf ``benennung.max_laenge`` begrenzt (gekürzt werden nur Kurznamen, nie
Kürzel, Nummer, Firma oder Datum).
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from typing import Literal

from auftragsautomat.config import Firma, Settings
from auftragsautomat.models import DOKUMENTKUERZEL, Belegpaket

logger = logging.getLogger(__name__)

Speditionsart = Literal["Speditionsauftrag", "Speditionsbestätigung", "Lieferschein"]

#: Zulässige Arten für :func:`speditionsname` (zweiter Bestandteil des Speditionsschemas).
SPEDITIONSARTEN: tuple[str, ...] = ("Speditionsauftrag", "Speditionsbestätigung", "Lieferschein")
#: Dokumentkürzel, die nach dem Speditionsschema benannt werden (statt nach dem Firmenschema).
SPEDITIONS_KUERZEL: tuple[str, ...] = ("Speditionsauftrag", "Speditionsbestätigung")
#: ``Firma.kurz`` der Weinstrasse Agentur -> Schema "Lieferant – Kunde – Kürzel – Datum".
KUERZEL_AGENTUR = "WS"
#: Platzhalter, wenn WIK/WOE keine Bestell-Nr haben.
OHNE_NUMMER = "ohne-Nr"
#: Platzhalter für fehlende Kurznamen (Kunde, Lieferant, Spedition).
UNBEKANNT = "Unbekannt"
#: Kürzel-Umschreibung bei einer Revised Order.
REVISIONS_KUERZEL: dict[str, str] = {"Cde": "Neu", "Kopie": "Neu Kopie"}
#: Kürzel der Auftragskopie, deren Name keine verbotenen Wörter enthalten darf.
AUFTRAGSKOPIE_KUERZEL: tuple[str, ...] = ("Kopie", "Neu Kopie")
#: Kurznamen werden beim Kürzen nie unter diese Länge gestutzt.
MIN_LAENGE_KUERZBAR = 3

# Zeichen, die Windows/OneDrive in Dateinamen nicht erlauben, plus Steuerzeichen.
_VERBOTENE_ZEICHEN = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]+')
_MEHRFACH_LEERZEICHEN = re.compile(r"\s+")
_UMLAUTE = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})


def _heute() -> date:
    """Heutiges Datum (eigene Funktion, damit Tests es ersetzen können)."""
    return date.today()


# ---------------------------------------------------------------------------
# Textbausteine
# ---------------------------------------------------------------------------


def normalisiere(text: str | None) -> str:
    """Vergleichsform eines Textes: casefold, Umlaute als ae/oe/ue/ss, Akzente entfernt,
    Mehrfach-Leerzeichen zusammengefasst. Für Vergleiche (Kundenliste, verbotene Wörter), nie
    für den Dateinamen selbst."""
    if not text:
        return ""
    t = text.casefold().translate(_UMLAUTE)
    t = unicodedata.normalize("NFKD", t)
    t = "".join(z for z in t if not unicodedata.combining(z))
    return _MEHRFACH_LEERZEICHEN.sub(" ", t).strip()


def _bereinige_text(text: str | None, ersatz: str) -> str:
    """Verbotene Zeichen/Steuerzeichen -> ``ersatz``, Mehrfach-Leerzeichen zusammenfassen,
    Leerzeichen am Anfang sowie Punkte/Leerzeichen am Ende entfernen (ohne Längenbegrenzung)."""
    if text is None:
        return ""
    bereinigt = _VERBOTENE_ZEICHEN.sub(ersatz, str(text))
    bereinigt = _MEHRFACH_LEERZEICHEN.sub(" ", bereinigt)
    return bereinigt.strip().rstrip(" .")


def bereinige(text: str, settings: Settings) -> str:
    """Macht einen Text dateinamentauglich.

    Ersetzt ``/ \\ : * ? " < > |`` und Steuerzeichen durch ``benennung.ersatzzeichen``, fasst
    Mehrfach-Leerzeichen zusammen, entfernt Punkte und Leerzeichen am Ende und begrenzt die
    Länge auf ``benennung.max_laenge``. Umlaute bleiben erhalten.
    """
    k = settings.benennung
    bereinigt = _bereinige_text(text, k.ersatzzeichen)
    if len(bereinigt) > k.max_laenge:
        bereinigt = bereinigt[: k.max_laenge].rstrip(" .")
    return bereinigt


def _kurz(text: str | None, ersatz: str) -> str:
    """Bereinigter Kurzname; leer -> ``Unbekannt``."""
    return _bereinige_text(text, ersatz) or UNBEKANNT


def _nummer(text: str | None, ersatz: str) -> str | None:
    """Bereinigte Nummer; leer -> ``None``."""
    return _bereinige_text(text, ersatz) or None


def _normalisiere_endung(endung: str | None) -> str:
    if not endung:
        return ""
    return endung if endung.startswith(".") else "." + endung


def datum_text(datum: date | None, settings: Settings) -> str:
    """Datum im Format ``benennung.datumsformat``; ``None`` = heute."""
    return (datum or _heute()).strftime(settings.benennung.datumsformat)


def _pruefe_kuerzel(kuerzel: str) -> str:
    if kuerzel not in DOKUMENTKUERZEL:
        raise ValueError(f"Unbekanntes Dokumentkürzel '{kuerzel}'. Erlaubt: {', '.join(DOKUMENTKUERZEL)}")
    return kuerzel


def ist_agentur(firma: Firma) -> bool:
    """``True`` für die Weinstrasse Agentur (``Firma.kurz == "WS"``): Schema ohne Firmenkürzel."""
    return firma.kurz.strip().upper() == KUERZEL_AGENTUR


def mit_auftragsnummer(kunde: str, settings: Settings) -> bool:
    """Steht der Kunde in ``benennung.kunden_mit_auftragsnummer``? Vergleich normalisiert
    (Groß/Klein, Umlaute) als Teilstring, z. B. "Spar" trifft "EUROSPAR Beispiel"."""
    kunde_norm = normalisiere(kunde)
    if not kunde_norm:
        return False
    for eintrag in settings.benennung.kunden_mit_auftragsnummer:
        eintrag_norm = normalisiere(eintrag)
        if eintrag_norm and eintrag_norm in kunde_norm:
            return True
    return False


# ---------------------------------------------------------------------------
# Namen zusammenbauen
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Teil:
    text: str
    kuerzbar: bool = False  # nur Kurznamen (Kunde, Lieferant, Spedition) werden gekürzt


def _baue_name(teile: list[_Teil], settings: Settings, endung: str) -> str:
    """Verbindet die Teile mit dem Trenner, hängt die Endung an und hält ``max_laenge`` ein.

    Zu lange Namen werden gekürzt, indem der jeweils längste kürzbare Teil zeichenweise
    gestutzt wird (nie unter :data:`MIN_LAENGE_KUERZBAR`); feste Teile (Kürzel, Nummer, Firma,
    Datum, Trenner, Endung) bleiben. Reicht das nicht, wird hart abgeschnitten und gewarnt.
    """
    k = settings.benennung
    endung = _normalisiere_endung(endung)
    # TODO-PRUEFEN: Trenner " – " (U+2013), Umlaute und Namen bis max_laenge (180) einmal am echten
    # System prüfen: OneDrive-Sync nach "Aufträge ohne Provision", Graph-Upload und der Dateinamen-Dialog
    # des PDF-Druckers in Lexware (Windows MAX_PATH 260 für den Gesamtpfad bleibt Sache des Aufrufers).
    texte = [t.text for t in teile]
    kuerzbar = [t.kuerzbar for t in teile]

    def zusammen() -> str:
        return k.trenner.join(texte).strip().rstrip(" .") + endung

    name = zusammen()
    while len(name) > k.max_laenge:
        kandidaten = [i for i, kb in enumerate(kuerzbar) if kb and len(texte[i]) > MIN_LAENGE_KUERZBAR]
        if not kandidaten:
            break
        i = max(kandidaten, key=lambda j: len(texte[j]))
        texte[i] = texte[i][:-1].rstrip(" .")
        name = zusammen()
    if len(name) > k.max_laenge:
        stamm_max = max(k.max_laenge - len(endung), 1)
        stamm = name[: len(name) - len(endung)] if endung else name
        name = stamm[:stamm_max].rstrip(" .") + endung
        logger.warning(
            "Dateiname überschreitet max_laenge=%d auch nach Kürzen der Kurznamen; hart abgeschnitten: %s",
            k.max_laenge,
            name,
        )
    return name


def _warne_bei_verbotenen_woertern(kuerzel: str, name: str, settings: Settings) -> None:
    if kuerzel not in AUFTRAGSKOPIE_KUERZEL:
        return
    verstoesse = pruefe_auftragskopie_name(name, settings)
    if verstoesse:
        logger.warning(
            "Name der Auftragskopie enthält verbotene Wörter (%s): %s – bitte Kunden-/Lieferantenkurznamen prüfen.",
            ", ".join(verstoesse),
            name,
        )


# ---------------------------------------------------------------------------
# Öffentliche API
# ---------------------------------------------------------------------------


def dateiname(
    kuerzel: str,
    *,
    firma: Firma,
    lieferant: str,
    kunde: str,
    bestellnummer: str | None,
    auftragsnummer: str | None,
    datum: date | None = None,
    settings: Settings,
    endung: str = ".pdf",
) -> str:
    """Dateiname eines Belegs nach Titulierung.

    * Weinstrasse Agentur (``firma.kurz == "WS"``): ``Lieferant – Kunde – Kürzel – Datum``;
      Kunden aus ``benennung.kunden_mit_auftragsnummer``: ``Lieferant – Kunde – Auftragsnummer –
      Kürzel – Datum`` (``auftragsnummer`` = Kundenbestellnummer; fehlt sie, Hinweis im Log und
      Name ohne Nummer). ``bestellnummer`` wird hier nicht verwendet.
    * WIK / WOE: ``Lieferant – <firma.kurz> – Bestell-Nr – Kürzel – Datum`` mit ``bestellnummer``
      (Kundenbestellnummer, sonst Belegnummer des Dokuments – die Auswahl trifft der Aufrufer,
      z. B. :func:`name_fuer_paket`); fehlt sie, steht ``ohne-Nr`` im Namen.

    ``kuerzel`` muss ein Schlüssel von ``DOKUMENTKUERZEL`` sein, sonst ``ValueError``.
    ``datum`` = ``None`` -> heute. ``endung`` ohne Punkt wird ergänzt, ``""`` = keine Endung
    (z. B. für Dossier-Ordner). Für "Kopie"/"Neu Kopie" wird der Name zusätzlich gegen die
    verbotenen Wörter geprüft (nur Warnung im Log; verbindlich prüft der Aufrufer mit
    :func:`pruefe_auftragskopie_name`).
    """
    kuerzel = _pruefe_kuerzel(kuerzel)
    ersatz = settings.benennung.ersatzzeichen
    lieferant_kurz = _kurz(lieferant, ersatz)
    kunde_kurz = _kurz(kunde, ersatz)
    datum_str = datum_text(datum, settings)

    teile: list[_Teil]
    if ist_agentur(firma):
        teile = [_Teil(lieferant_kurz, kuerzbar=True), _Teil(kunde_kurz, kuerzbar=True)]
        if mit_auftragsnummer(kunde, settings) or mit_auftragsnummer(kunde_kurz, settings):
            nummer = _nummer(auftragsnummer, ersatz)
            if nummer is None:
                logger.warning(
                    "Kunde '%s' wird mit Auftragsnummer benannt, die Kundenbestellnummer fehlt aber – "
                    "Dateiname (%s) ohne Nummer.",
                    kunde_kurz,
                    kuerzel,
                )
            else:
                teile.append(_Teil(nummer))
        teile.extend([_Teil(kuerzel), _Teil(datum_str)])
    else:
        nummer = _nummer(bestellnummer, ersatz)
        if nummer is None:
            logger.warning(
                "Keine Bestell-Nr für %s (%s / %s, Firma %s) – Dateiname mit '%s'.",
                kuerzel,
                lieferant_kurz,
                kunde_kurz,
                firma.kurz,
                OHNE_NUMMER,
            )
            nummer = OHNE_NUMMER
        teile = [
            _Teil(lieferant_kurz, kuerzbar=True),
            _Teil(_kurz(firma.kurz, ersatz)),
            _Teil(nummer),
            _Teil(kuerzel),
            _Teil(datum_str),
        ]

    name = _baue_name(teile, settings, endung)
    _warne_bei_verbotenen_woertern(kuerzel, name, settings)
    return name


def speditionsname(
    art: Speditionsart,
    *,
    spedition: str | None,
    lieferant: str | None,
    kunde_oder_bestellnr: str | None,
    datum: date | None = None,
    settings: Settings,
    endung: str = ".pdf",
) -> str:
    """Dateiname nach dem Speditionsschema:
    ``Spedition – Speditionsauftrag|Speditionsbestätigung|Lieferschein – Lieferant – Kunde|Bestell-Nr – Datum``.

    ``art`` muss eine der :data:`SPEDITIONSARTEN` sein, sonst ``ValueError``. Fehlende Kurznamen
    werden zu ``Unbekannt``. ``datum`` = ``None`` -> heute.
    """
    if art not in SPEDITIONSARTEN:
        raise ValueError(f"Unbekannte Speditionsart '{art}'. Erlaubt: {', '.join(SPEDITIONSARTEN)}")
    ersatz = settings.benennung.ersatzzeichen
    teile = [
        _Teil(_kurz(spedition, ersatz), kuerzbar=True),
        _Teil(art),
        _Teil(_kurz(lieferant, ersatz), kuerzbar=True),
        _Teil(_kurz(kunde_oder_bestellnr, ersatz), kuerzbar=True),
        _Teil(datum_text(datum, settings)),
    ]
    return _baue_name(teile, settings, endung)


def kurzname_kunde(paket: Belegpaket) -> str:
    """Kurzname des Kunden: Lexware-Matchcode des zugeordneten Kunden, sonst sein Name,
    sonst der Kundentext aus dem Dokument, sonst ``Unbekannt``."""
    kunde = paket.kunde.kunde
    if kunde is not None:
        kurz = (kunde.matchcode or "").strip() or (kunde.name or "").strip()
        if kurz:
            return kurz
    return (paket.roh.kunde_text or "").strip() or UNBEKANNT


def kurzname_lieferant(paket: Belegpaket) -> str:
    """Kurzname des Lieferanten: Matchcode des zugeordneten Lieferanten, sonst sein Name,
    sonst der Lieferantentext aus dem Dokument, sonst ``Unbekannt``."""
    lieferant = paket.lieferant.lieferant
    if lieferant is not None:
        kurz = (lieferant.matchcode or "").strip() or (lieferant.name or "").strip()
        if kurz:
            return kurz
    return (paket.roh.lieferant_text or "").strip() or UNBEKANNT


def spedition_fuer_paket(paket: Belegpaket, settings: Settings) -> str:
    """Spedition für das Speditionsschema: Belegfeld ``spedition``, sonst
    ``Firma.belegfelder_standard["spedition"]``, sonst ``Unbekannt``."""
    spedition = (paket.belegfelder.spedition or "").strip()
    if not spedition and paket.firma_id and paket.firma_id in settings.firmen:
        spedition = (settings.firmen[paket.firma_id].belegfelder_standard.get("spedition") or "").strip()
    return spedition or UNBEKANNT


def _firma_fuer(paket: Belegpaket, settings: Settings) -> Firma:
    if not paket.firma_id:
        raise ValueError("Belegpaket ohne Firma: Dateiname nach Titulierung erst nach Festlegen der Firma möglich.")
    return settings.firma(paket.firma_id)


def name_fuer_paket(
    kuerzel: str,
    paket: Belegpaket,
    settings: Settings,
    *,
    datum: date | None = None,
    endung: str = ".pdf",
) -> str:
    """Dateiname für ein Belegpaket (Firma, Kunde, Lieferant und Nummern aus dem Paket).

    * Kurznamen: :func:`kurzname_lieferant` / :func:`kurzname_kunde` (Matchcode, Name, Dokumenttext).
    * Revised Order (``paket.ist_revision``): "Cde" -> "Neu", "Kopie" -> "Neu Kopie".
    * WS: Auftragsnummer = Kundenbestellnummer (``roh.bestellnummer``);
      WIK/WOE: Bestell-Nr = Kundenbestellnummer, sonst Belegnummer des Dokuments, sonst ``ohne-Nr``.
    * "Speditionsauftrag" / "Speditionsbestätigung": Speditionsschema mit der Spedition aus den
      Belegfeldern; vierter Teil = Kunde (WS) bzw. Bestell-Nr (WIK/WOE, ohne Nummer der Kunde).
    * ``datum`` = ``None`` -> heute (``benennung.datumsformat``).

    ``ValueError`` bei unbekanntem Kürzel oder Paket ohne Firma.
    """
    kuerzel = _pruefe_kuerzel(kuerzel)
    firma = _firma_fuer(paket, settings)
    if (paket.ist_revision or paket.roh.ist_revision) and kuerzel in REVISIONS_KUERZEL:
        kuerzel = REVISIONS_KUERZEL[kuerzel]

    lieferant = kurzname_lieferant(paket)
    kunde = kurzname_kunde(paket)
    kundenbestellnummer = (paket.roh.bestellnummer or "").strip() or None
    belegnummer = (paket.roh.belegnummer or "").strip() or None
    bestellnummer = kundenbestellnummer or belegnummer

    if kuerzel in SPEDITIONS_KUERZEL:
        vierter_teil = kunde if ist_agentur(firma) or bestellnummer is None else bestellnummer
        return speditionsname(
            kuerzel,  # type: ignore[arg-type]
            spedition=spedition_fuer_paket(paket, settings),
            lieferant=lieferant,
            kunde_oder_bestellnr=vierter_teil,
            datum=datum,
            settings=settings,
            endung=endung,
        )

    return dateiname(
        kuerzel,
        firma=firma,
        lieferant=lieferant,
        kunde=kunde,
        bestellnummer=bestellnummer,
        auftragsnummer=kundenbestellnummer,
        datum=datum,
        settings=settings,
        endung=endung,
    )


def pruefe_auftragskopie_name(name: str, settings: Settings) -> list[str]:
    """Liste der verbotenen Wörter (``kommunikation.verbotene_woerter_auftragskopie``), die im
    Namen vorkommen. Vergleich casefold und ohne Umlaute (``Bestätigung`` = ``BESTAETIGUNG``),
    als Teilstring. Leere Liste = Name zulässig. Gleichbedeutende Schreibweisen werden nur
    einmal gemeldet."""
    name_norm = normalisiere(name)
    verstoesse: list[str] = []
    gesehen: set[str] = set()
    for wort in settings.kommunikation.verbotene_woerter_auftragskopie:
        wort_norm = normalisiere(wort)
        if not wort_norm or wort_norm in gesehen:
            continue
        if wort_norm in name_norm:
            gesehen.add(wort_norm)
            verstoesse.append(wort)
    return verstoesse
