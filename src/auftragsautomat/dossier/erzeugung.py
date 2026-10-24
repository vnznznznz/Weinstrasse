"""Dossiers im Ordner "vorangefertigte Dossiers" anlegen und fortschreiben.

:class:`DossierVerwaltung` arbeitet über ein :class:`~auftragsautomat.m365.basis.DateiBackend`
(Graph, lokal oder fake) unterhalb einer Wurzel (:class:`~auftragsautomat.config.DateiRefKonfig`).
Je Vorgang entsteht ein Unterordner (Name laut :func:`auftragsautomat.dossier.ablage.dossier_name`)
mit nummerierten Dateien:

* ``00_Deckblatt.pdf`` (wird bei jeder Aktualisierung neu erzeugt),
* ``01_Kundenbestellung_<Belegname Kd>``, ``02_Bestellung_<Cde|Neu>``, ``03_Auftragskopie_<Kopie|Neu Kopie>``,
  ``04_Speditionsauftrag_<…>``, ``05_Lieferschein_<Ls>`` (Belegnamen ausschließlich aus ``benennung.py``),
* ``06_Mails/<Zeit>_<Typ>.eml`` (Kopien der erzeugten Entwürfe und später eingehende Bestätigungen),
* ``07_Fixtermin.txt`` (die Zeile der Fixterminüberwachung als Text),
* ``dossier.json`` (Index: Dateien, Quellen, Zeitstempel, Checkliste, offene Punkte), ``paket.json``
  (das Belegpaket, damit spätere Aktualisierungen das Deckblatt mit Positionen neu bauen können),
* optional ``Dossier_komplett.pdf`` (alle PDFs zusammengeführt, ``settings.dossier.gesamt_pdf``).

Belege werden **nie überschrieben**: gleicher Name und gleiche Größe -> übersprungen, sonst Suffix
``_2``, ``_3`` … Deckblatt, Fixtermin-Text, Index und Gesamt-PDF sind Statusdateien und werden
ersetzt. Ein zweiter Lauf mit denselben Eingaben erzeugt keine Duplikate (Idempotenz). Das Tool
verschiebt und löscht nichts; das Einsortieren in die Auftragsablage übernimmt der Mitarbeiter.

Dry Run bzw. Backend ``fake``: Wurzel ist ``settings.dossier.lokaler_ordner`` (``logs/dossiers``),
siehe :meth:`DossierVerwaltung.aus_settings`.
"""

from __future__ import annotations

import email
import email.policy
import hashlib
import json
import logging
import mimetypes
import tempfile
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from datetime import date, datetime
from email.message import EmailMessage
from email.utils import formatdate, parsedate_to_datetime
from pathlib import Path

from pydantic import ValidationError

from auftragsautomat.config import DateiRefKonfig, Settings
from auftragsautomat.dossier.ablage import dateityp_aus_name, dossier_name, stichtag, zielordner_vorschlag
from auftragsautomat.dossier.deckblatt import (
    CHECKLISTE,
    CHECKLISTE_REIHENFOLGE,
    datum_text,
    erzeuge_deckblatt,
    zeit_text,
)
from auftragsautomat.m365.basis import DateiBackend, DateiInfo, M365Fehler, M365NichtGefunden
from auftragsautomat.models import (
    KUERZEL_DATEITYP,
    Belegpaket,
    BuchungsErgebnis,
    Dossier,
    DossierDatei,
    DossierDateityp,
    EmailEntwurf,
    EntwurfTyp,
    Fixtermin,
    Schweregrad,
    Sendung,
    SendungStatus,
)

__all__ = [
    "DECKBLATT",
    "DossierVerwaltung",
    "FIXTERMIN_DATEI",
    "GESAMT_PDF",
    "INDEX_DATEI",
    "MAIL_ORDNER",
    "PAKET_DATEI",
    "PRAEFIX_JE_KUERZEL",
    "WEITERE_PRAEFIX",
    "eml_aus_entwurf",
    "fixtermin_text",
    "mail_dateiname",
]

logger = logging.getLogger(__name__)

DECKBLATT = "00_Deckblatt.pdf"
MAIL_ORDNER = "06_Mails"
FIXTERMIN_DATEI = "07_Fixtermin.txt"
INDEX_DATEI = "dossier.json"
PAKET_DATEI = "paket.json"
GESAMT_PDF = "Dossier_komplett.pdf"
WEITERE_PRAEFIX = "09_Weitere"
ENTWURF_DOMAIN = "auftragsautomat.invalid"

#: Dokumentkürzel (Titulierung) -> Präfix des Dateinamens im Dossier.
PRAEFIX_JE_KUERZEL: dict[str, str] = {
    "Kd": "01_Kundenbestellung",
    "Cde": "02_Bestellung",
    "Neu": "02_Bestellung",
    "Kopie": "03_Auftragskopie",
    "Neu Kopie": "03_Auftragskopie",
    "Speditionsauftrag": "04_Speditionsauftrag",
    "Ls": "05_Lieferschein",
    "Oc": "08_Bestaetigung_Weingut",
    "Speditionsbestätigung": "08_Bestaetigung_Spedition",
    "Rekl": "09_Reklamation",
}
#: Kurzbezeichnung je Kürzel für offene Punkte.
_BEZEICHNUNG_JE_KUERZEL: dict[str, str] = {
    "Kd": "Kundenbestellung",
    "Cde": "Bestellung ans Weingut",
    "Kopie": "Auftragskopie an den Kunden",
    "Speditionsauftrag": "Speditionsauftrag",
    "Ls": "Lieferschein",
}
#: Dateitypen, die einen erwarteten Beleg erfüllen (Revised Order ersetzt Cde/Kopie).
_ERFUELLT_DURCH: dict[str, set[DossierDateityp]] = {
    "Kd": {"kundenbestellung"},
    "Cde": {"bestellung", "revision"},
    "Kopie": {"auftragskopie", "revision"},
    "Speditionsauftrag": {"speditionsauftrag"},
    "Ls": {"lieferschein"},
}
_STATUS_RANG: dict[SendungStatus, int] = {
    SendungStatus.UNBEKANNT: 0,
    SendungStatus.BEAUFTRAGT: 1,
    SendungStatus.BESTAETIGT: 2,
    SendungStatus.ABHOLUNG_GEPLANT: 3,
    SendungStatus.ABGEHOLT: 4,
    SendungStatus.UNTERWEGS: 5,
    SendungStatus.ZUSTELLUNG_GEPLANT: 6,
    SendungStatus.ZUGESTELLT: 7,
}
_WOCHENTAGE = ("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag")
_ERWARTETE_ENTWUERFE: tuple[tuple[EntwurfTyp, str, str], ...] = (
    (EntwurfTyp.BESTELLUNG_WEINGUT, "bestellung_gesendet", "E-Mail-Entwurf 'Bestellung ans Weingut' fehlt"),
    (EntwurfTyp.AUFTRAGSKOPIE_KUNDE, "auftragskopie_gesendet", "E-Mail-Entwurf 'Auftragskopie an den Kunden' fehlt"),
    (EntwurfTyp.SPEDITION_AUFTRAG, "spedition_beauftragt", "E-Mail-Entwurf 'Auftrag an die Spedition' fehlt"),
)


# ---------------------------------------------------------------------------
# Hilfsfunktionen (öffentlich)
# ---------------------------------------------------------------------------


def mail_dateiname(zeit: datetime, typ: str) -> str:
    """Technischer Name einer Mail-Kopie im Dossier: ``06_Mails/<JJJJMMTT_HHMMSS>_<typ>.eml``."""
    typ_text = "".join(z if z.isalnum() or z in "-_" else "_" for z in (typ or "mail").strip()) or "mail"
    return f"{MAIL_ORDNER}/{zeit:%Y%m%d_%H%M%S}_{typ_text}.eml"


def eml_aus_entwurf(entwurf: EmailEntwurf, zeit: datetime) -> bytes:
    """Baut aus einem :class:`EmailEntwurf` eine ``.eml`` (für Dossiers, wenn das Mail-Backend keine
    Datei liefert, z. B. Graph). Deterministisch: gleiche Eingabe -> gleiche Bytes (Idempotenz);
    ``X-Unsent: 1``, damit Outlook die Datei als Entwurf öffnet. Es wird nichts gesendet."""
    nachricht = EmailMessage(policy=email.policy.SMTP)
    nachricht["Subject"] = entwurf.betreff
    if entwurf.absender:
        nachricht["From"] = entwurf.absender
    if entwurf.an:
        nachricht["To"] = ", ".join(entwurf.an)
    if entwurf.cc:
        nachricht["Cc"] = ", ".join(entwurf.cc)
    nachricht["Date"] = formatdate(zeit.timestamp(), localtime=True)
    kennung = hashlib.sha1(
        "|".join([entwurf.typ.value, entwurf.betreff, zeit.isoformat(), ",".join(entwurf.an)]).encode("utf-8")
    ).hexdigest()
    nachricht["Message-ID"] = f"<dossier-{kennung}@{ENTWURF_DOMAIN}>"
    nachricht["X-Unsent"] = "1"
    nachricht["X-Auftragsautomat-Typ"] = entwurf.typ.value
    nachricht.set_content(entwurf.text_plain or "")
    if entwurf.text_html:
        nachricht.add_alternative(entwurf.text_html, subtype="html")
    for pfad in entwurf.anhaenge:
        pfad = Path(pfad)
        if not pfad.is_file():
            logger.warning("Anhang %s des Entwurfs '%s' fehlt; Mail-Kopie im Dossier ohne diesen Anhang.", pfad, entwurf.typ.value)
            continue
        typ = mimetypes.guess_type(pfad.name)[0] or "application/octet-stream"
        haupt, _, unter = typ.partition("/")
        nachricht.add_attachment(pfad.read_bytes(), maintype=haupt, subtype=unter, filename=pfad.name)
    # Feste MIME-Grenzen statt Zufallswerten, damit gleiche Entwürfe gleiche Bytes (und Größe) ergeben.
    for i, teil in enumerate(nachricht.walk()):
        if teil.is_multipart():
            teil.set_boundary(f"==dossier-{kennung[:24]}-{i}==")
    return nachricht.as_bytes()


def fixtermin_text(fixtermin: Fixtermin, *, stand: datetime | None = None) -> str:
    """Die Zeile der Fixterminüberwachung als lesbarer Text (Kopfzeile der Excel, Werte, Zusatzangaben)."""
    kopf = [
        "KW", "Datum", "Tag", "Lieferant", "Kunde", "Auftragsdatum", "Verladedatum",
        "Bestätigung des Lieferanten", "Bestätigung der Spedition", "Abgeholt", "Bemerkung",
    ]
    ja_nein = {True: "ja", False: "nein"}
    werte = [
        str(fixtermin.liefertag.isocalendar().week),
        datum_text(fixtermin.liefertag),
        _WOCHENTAGE[fixtermin.liefertag.weekday()],
        fixtermin.lieferant_kurz,
        fixtermin.kunde_kurz,
        datum_text(fixtermin.auftragsdatum),
        datum_text(fixtermin.verladedatum),
        ja_nein[fixtermin.lieferant_bestaetigt],
        ja_nein[fixtermin.spedition_bestaetigt],
        ja_nein[fixtermin.abgeholt],
        fixtermin.bemerkung or "",
    ]
    zeilen = [
        "Fixterminüberwachung – Zeile " + (str(fixtermin.zeile) if fixtermin.zeile else "noch nicht eingetragen"),
        "",
        " | ".join(kopf),
        " | ".join(werte),
        "",
    ]
    zusatz = [
        ("Firma", fixtermin.firma_id),
        ("Bestellnummer (Lexware)", fixtermin.bestellnummer),
        ("Kundenbestellnummer", fixtermin.kundenbestellnummer),
        ("AB-Nummer (Lexware)", fixtermin.ab_nummer),
        ("Spedition", fixtermin.spedition),
        ("Referenz der Spedition", fixtermin.tracking_referenz),
        ("Quelle", fixtermin.quelle),
    ]
    zeilen.extend(f"{bez}: {wert}" for bez, wert in zusatz if wert)
    zeilen.append(f"Stand: {zeit_text(stand) if stand else datum_text(date.today())}")
    return "\n".join(zeilen) + "\n"


def _norm_nummer(wert: str | None) -> str | None:
    if wert is None:
        return None
    text = "".join(str(wert).split()).casefold()
    return text or None


def _zeit_schluessel(zeit: datetime | None) -> float:
    """Sortierschlüssel für Zeitstempel (naiv = lokale Zeit); ``None`` sortiert ganz nach vorn."""
    if zeit is None:
        return float("-inf")
    if zeit.tzinfo is None:
        zeit = zeit.astimezone()
    return zeit.timestamp()


def _eindeutig(punkte: Iterable[str]) -> list[str]:
    gesehen: set[str] = set()
    ergebnis: list[str] = []
    for p in punkte:
        text = (p or "").strip()
        if text and text not in gesehen:
            gesehen.add(text)
            ergebnis.append(text)
    return ergebnis


def _ist_datei(pfad: Path | str | None) -> bool:
    if not pfad:
        return False
    try:
        return Path(pfad).is_file()
    except (OSError, ValueError):
        return False


def _kurznamen(paket: Belegpaket) -> tuple[str, str]:
    from auftragsautomat.benennung import kurzname_kunde, kurzname_lieferant

    return kurzname_lieferant(paket), kurzname_kunde(paket)


# ---------------------------------------------------------------------------
# Verwaltung
# ---------------------------------------------------------------------------


class DossierVerwaltung:
    """Dossiers unterhalb von ``wurzel`` anlegen, fortschreiben, finden und auflisten."""

    def __init__(
        self,
        datei_backend: DateiBackend,
        wurzel: DateiRefKonfig,
        settings: Settings,
        *,
        jetzt: Callable[[], datetime] | None = None,
        arbeitsordner: Path | None = None,
    ) -> None:
        self.backend = datei_backend
        self.wurzel = wurzel
        self.settings = settings
        self._jetzt = jetzt or (lambda: datetime.now().astimezone())
        self._arbeitsordner = Path(arbeitsordner) if arbeitsordner is not None else None

    @classmethod
    def aus_settings(
        cls,
        settings: Settings,
        *,
        datei_backend: DateiBackend | None = None,
        jetzt: Callable[[], datetime] | None = None,
    ) -> DossierVerwaltung:
        """Verwaltung laut Konfiguration.

        Dry Run (``settings.dry_run``) oder Backend ``fake``: :class:`FakeDateiBackend` auf
        ``settings.dossier.lokaler_ordner`` (``logs/dossiers``) – nichts geht nach OneDrive. Sonst das
        übergebene Backend (bzw. ``erzeuge_backends(settings).datei``) auf ``settings.m365.dossier_ordner``;
        ist dieser Ordner nicht konfiguriert, fällt die Verwaltung mit Warnung auf den lokalen Ordner zurück.
        """
        lokal = DateiRefKonfig(lokaler_pfad=Path(settings.dossier.lokaler_ordner))
        if settings.dry_run or settings.m365.backend == "fake":
            from auftragsautomat.m365.fake import FakeDateiBackend

            logger.info("Dossiers im Dry Run/Fake-Backend nach %s", settings.dossier.lokaler_ordner)
            return cls(FakeDateiBackend(Path(settings.dossier.lokaler_ordner)), lokal, settings, jetzt=jetzt)
        if datei_backend is None:
            from auftragsautomat.m365.fabrik import erzeuge_backends

            datei_backend = erzeuge_backends(settings).datei
        wurzel = settings.m365.dossier_ordner
        if not (wurzel.web_url or (wurzel.drive_id and wurzel.item_id) or wurzel.lokaler_pfad):
            logger.warning(
                "m365.dossier_ordner ist nicht konfiguriert (web_url/IDs/lokaler_pfad fehlen); Dossiers landen lokal unter %s.",
                settings.dossier.lokaler_ordner,
            )
            from auftragsautomat.m365.fake import FakeDateiBackend

            return cls(FakeDateiBackend(Path(settings.dossier.lokaler_ordner)), lokal, settings, jetzt=jetzt)
        # TODO-PRUEFEN: Am echten Tenant prüfen, dass der Ordner "vorangefertigte Dossiers" per web_url
        # auflösbar ist (Graph: /shares/u!… bzw. /sites/…) und der Anwender Schreibrechte darauf hat.
        return cls(datei_backend, wurzel, settings, jetzt=jetzt)

    # -- Hilfen -----------------------------------------------------------------------

    @contextmanager
    def _arbeit(self) -> Iterator[Path]:
        if self._arbeitsordner is not None:
            self._arbeitsordner.mkdir(parents=True, exist_ok=True)
            yield self._arbeitsordner
            return
        with tempfile.TemporaryDirectory(prefix="auftragsautomat_dossier_") as tmp:
            yield Path(tmp)

    def _rel(self, dossier: Dossier, name: str) -> str:
        return f"{dossier.name}/{name}"

    def _vorhandene_dateien(self, ordnername: str) -> dict[str, DateiInfo]:
        """Dateien des Dossier-Ordners (und ``06_Mails``), Schlüssel = Pfad relativ zum Dossier."""
        vorhandene: dict[str, DateiInfo] = {}
        try:
            eintraege = self.backend.liste(self.wurzel, ordnername)
        except M365NichtGefunden:
            return vorhandene
        for eintrag in eintraege:
            if eintrag.ist_ordner:
                if eintrag.name == MAIL_ORDNER:
                    try:
                        for mail in self.backend.liste(self.wurzel, f"{ordnername}/{MAIL_ORDNER}"):
                            if not mail.ist_ordner:
                                vorhandene[f"{MAIL_ORDNER}/{mail.name}"] = mail
                    except M365NichtGefunden:
                        pass
                continue
            vorhandene[eintrag.name] = eintrag
        return vorhandene

    @staticmethod
    def _ersetze_eintrag(dossier: Dossier, eintrag: DossierDatei) -> None:
        dossier.dateien = [d for d in dossier.dateien if d.name != eintrag.name] + [eintrag]
        dossier.dateien.sort(key=lambda d: d.name.casefold())

    def _lege_datei_ab(
        self,
        dossier: Dossier,
        vorhandene: dict[str, DateiInfo],
        quelle: Path,
        zielname: str,
        typ: DossierDateityp,
        *,
        quelle_text: str | None,
        jetzt: datetime,
    ) -> DossierDatei:
        """Lädt ``quelle`` als ``zielname`` ins Dossier – nie überschreiben: gibt es unter ``zielname`` (oder
        ``…_2``, ``…_3``) schon dieselbe Datei, wird übersprungen (vorhandener Eintrag bleibt), sonst unter dem
        ersten freien Namen abgelegt.

        "Dieselbe Datei" heißt gleiche Größe **und** gleicher Inhalt: zwei Entwürfe desselben Typs aus derselben
        Sekunde (z. B. die Bestellungen an zwei Weingüter) haben denselben technischen Namen und oft dieselbe
        Größe. Lässt sich die vorhandene Datei nicht laden, gilt wie bisher die gleiche Größe als gleich."""
        groesse = quelle.stat().st_size
        ordner, _, basis = zielname.rpartition("/")
        stamm, punkt, endung = basis.rpartition(".")
        if not punkt:
            stamm, endung = basis, ""
        kandidat = zielname
        laufnr = 2
        while (info := vorhandene.get(kandidat)) is not None:
            if info.groesse == groesse and self._gleicher_inhalt(dossier, kandidat, quelle) is not False:
                logger.info("Dossier %s: %s ist bereits vorhanden (gleicher Inhalt) – übersprungen.", dossier.name, kandidat)
                bisher = next((d for d in dossier.dateien if d.name == kandidat), None)
                if bisher is None:
                    bisher = DossierDatei(name=kandidat, typ=typ, quelle=quelle_text, hinzugefuegt=info.geaendert, groesse=groesse)
                    self._ersetze_eintrag(dossier, bisher)
                return bisher
            name = f"{stamm}_{laufnr}{'.' + endung if punkt else ''}"
            kandidat = f"{ordner}/{name}" if ordner else name
            laufnr += 1
        if kandidat != zielname:
            logger.warning("Dossier %s: %s existiert mit anderem Inhalt – abgelegt als %s.", dossier.name, zielname, kandidat)
        zielname = kandidat
        # TODO-PRUEFEN: Am echten Tenant prüfen, dass OneDrive/SharePoint die Dossier-Namen annimmt
        # (Gedankenstrich " – " aus der Titulierung, Umlaute, Gesamtpfad < 400 Zeichen) und dass der
        # Graph-Upload in "06_Mails/" nach ordner_anlegen ohne Konflikt (409) durchläuft.
        self.backend.hochladen(quelle, self.wurzel, self._rel(dossier, zielname))
        vorhandene[zielname] = DateiInfo(name=zielname.rsplit("/", 1)[-1], pfad=self._rel(dossier, zielname), groesse=groesse, geaendert=jetzt)
        eintrag = DossierDatei(name=zielname, typ=typ, quelle=quelle_text, hinzugefuegt=jetzt, groesse=groesse)
        self._ersetze_eintrag(dossier, eintrag)
        logger.info("Dossier %s: %s abgelegt (%s).", dossier.name, zielname, typ)
        return eintrag

    def _gleicher_inhalt(self, dossier: Dossier, zielname: str, quelle: Path) -> bool | None:
        """``True``/``False``, ob die vorhandene Datei ``zielname`` denselben Inhalt wie ``quelle`` hat;
        ``None``, wenn sie sich nicht laden lässt."""
        try:
            with tempfile.TemporaryDirectory(prefix="auftragsautomat_dossier_vergleich_") as tmp:
                vorhanden = self.backend.herunterladen(self.wurzel, self._rel(dossier, zielname), Path(tmp) / "vorhanden")
                return Path(vorhanden).read_bytes() == quelle.read_bytes()
        except (M365Fehler, OSError) as exc:
            logger.debug("Dossier %s: %s nicht zum Vergleich ladbar (%s)", dossier.name, zielname, exc)
            return None

    def _hochladen_ersetzen(self, dossier: Dossier, lokal: Path, zielname: str, typ: DossierDateityp, jetzt: datetime) -> DossierDatei:
        """Statusdateien (Deckblatt, Gesamt-PDF): immer ersetzen."""
        self.backend.hochladen(lokal, self.wurzel, self._rel(dossier, zielname))
        eintrag = DossierDatei(name=zielname, typ=typ, quelle="auftragsautomat", hinzugefuegt=jetzt, groesse=lokal.stat().st_size)
        self._ersetze_eintrag(dossier, eintrag)
        return eintrag

    def _belegname(self, kuerzel: str, paket: Belegpaket, tag: date, endung: str) -> str:
        """Belegname ausschließlich über ``benennung.name_fuer_paket`` (Titulierung); für die Auftragskopie
        verbindlich ohne verbotene Wörter (``pruefe_auftragskopie_name``, Geschäftsregel 18)."""
        from auftragsautomat.benennung import (
            AUFTRAGSKOPIE_KUERZEL,
            entferne_verbotene_woerter,
            name_fuer_paket,
            pruefe_auftragskopie_name,
        )

        name = name_fuer_paket(kuerzel, paket, self.settings, datum=tag, endung=endung)
        if kuerzel in AUFTRAGSKOPIE_KUERZEL and pruefe_auftragskopie_name(name, self.settings):
            stamm = entferne_verbotene_woerter(Path(name).stem, self.settings) or kuerzel
            logger.warning("Dossier: verbotene Wörter aus dem Namen der Auftragskopie entfernt: %s", name)
            name = stamm + Path(name).suffix
        return name

    def _lege_beleg_ab(
        self,
        dossier: Dossier,
        vorhandene: dict[str, DateiInfo],
        kuerzel: str,
        quelle: Path,
        paket: Belegpaket,
        tag: date,
        *,
        quelle_text: str,
        jetzt: datetime,
    ) -> DossierDatei:
        from auftragsautomat.ablage.ablage import basis_kuerzel

        basis, weingut = basis_kuerzel(kuerzel)
        if weingut is not None and basis in PRAEFIX_JE_KUERZEL:
            # PDF eines weiteren Weinguts ("Cde (2)" …): lexware.buchen hat es bereits mit dem Teilpaket des
            # Weinguts nach Titulierung benannt; das Gesamtpaket würde den Hauptlieferanten einsetzen.
            typ = KUERZEL_DATEITYP.get(basis, "sonstiges")
            return self._lege_datei_ab(
                dossier, vorhandene, quelle, f"{PRAEFIX_JE_KUERZEL[basis]}_{quelle.name}", typ, quelle_text=quelle_text, jetzt=jetzt
            )
        praefix = PRAEFIX_JE_KUERZEL.get(kuerzel)
        if praefix is None:
            logger.warning("Unbekanntes Dokumentkürzel '%s' – Datei %s wird als weitere Datei abgelegt.", kuerzel, quelle.name)
            return self._lege_datei_ab(
                dossier, vorhandene, quelle, f"{WEITERE_PRAEFIX}_{quelle.name}", dateityp_aus_name(quelle.name), quelle_text=quelle_text, jetzt=jetzt
            )
        belegname = self._belegname(kuerzel, paket, tag, quelle.suffix or ".pdf")
        zielname = f"{praefix}_{belegname}"
        typ = dateityp_aus_name(belegname)
        if typ == "sonstiges":
            typ = KUERZEL_DATEITYP.get(kuerzel, "sonstiges")
        return self._lege_datei_ab(dossier, vorhandene, quelle, zielname, typ, quelle_text=quelle_text, jetzt=jetzt)

    def _lege_entwurf_ab(
        self,
        dossier: Dossier,
        vorhandene: dict[str, DateiInfo],
        entwurf: EmailEntwurf,
        arbeit: Path,
        laufnr: int,
        *,
        fallback_zeit: datetime,
        jetzt: datetime,
    ) -> DossierDatei:
        zeit = entwurf.erstellt_am or fallback_zeit
        zielname = mail_dateiname(zeit, entwurf.typ.value)
        if entwurf.speicherort and str(entwurf.speicherort).lower().endswith(".eml") and _ist_datei(entwurf.speicherort):
            quelle = Path(entwurf.speicherort)
            quelle_text = f"entwurf:{entwurf.typ.value}:{quelle.name}"
        else:
            quelle = arbeit / f"entwurf_{laufnr}.eml"
            quelle.write_bytes(eml_aus_entwurf(entwurf, zeit))
            quelle_text = f"entwurf:{entwurf.typ.value}" + (f":{entwurf.speicherort}" if entwurf.speicherort else "")
        return self._lege_datei_ab(dossier, vorhandene, quelle, zielname, "mail", quelle_text=quelle_text, jetzt=jetzt)

    # -- Kopf, Checkliste, offene Punkte ------------------------------------------------

    def _aktualisiere_kopf(
        self,
        dossier: Dossier,
        paket: Belegpaket,
        ergebnis: BuchungsErgebnis | None,
        fixtermin: Fixtermin | None,
        sendung: Sendung | None,
    ) -> None:
        roh = paket.roh
        lieferant_kurz, kunde_kurz = _kurznamen(paket)
        dossier.firma_id = paket.firma_id or dossier.firma_id
        dossier.kunde_kurz = kunde_kurz or dossier.kunde_kurz
        dossier.lieferant_kurz = lieferant_kurz or dossier.lieferant_kurz
        dossier.kundenbestellnummer = (roh.bestellnummer or "").strip() or dossier.kundenbestellnummer
        if ergebnis is not None:
            if ergebnis.einkauf is not None and ergebnis.einkauf.belegnummer:
                dossier.bestellnummer = ergebnis.einkauf.belegnummer
            elif ergebnis.angebot is not None and ergebnis.angebot.belegnummer:
                dossier.bestellnummer = ergebnis.angebot.belegnummer
            if ergebnis.verkauf is not None and ergebnis.verkauf.belegnummer:
                dossier.ab_nummer = ergebnis.verkauf.belegnummer
            if ergebnis.lieferschein is not None and ergebnis.lieferschein.belegnummer:
                dossier.lieferschein_nummer = ergebnis.lieferschein.belegnummer
        if fixtermin is not None:
            dossier.fixtermin = fixtermin.liefertag
            dossier.bestellnummer = dossier.bestellnummer or fixtermin.bestellnummer
            dossier.ab_nummer = dossier.ab_nummer or fixtermin.ab_nummer
        elif roh.liefertag is not None:
            dossier.fixtermin = roh.liefertag
        spedition = (fixtermin.spedition if fixtermin else None) or (paket.belegfelder.spedition or "").strip() or (sendung.spedition if sendung else None)
        dossier.spedition = spedition or dossier.spedition
        referenz = (sendung.referenz if sendung else None) or (fixtermin.tracking_referenz if fixtermin else None)
        dossier.tracking_referenz = referenz or dossier.tracking_referenz

    @staticmethod
    def _checkliste(
        bisher: Mapping[str, bool],
        entwuerfe: Iterable[EmailEntwurf],
        fixtermin: Fixtermin | None,
        sendung: Sendung | None,
    ) -> dict[str, bool]:
        """Checkliste aus Entwürfen, Fixtermin und Sendung ableiten; einmal gesetzte Häkchen bleiben."""
        neu: dict[str, bool] = {k: bool(bisher.get(k, False)) for k in CHECKLISTE_REIHENFOLGE}
        for k, v in bisher.items():
            neu.setdefault(k, bool(v))
        typen = {e.typ for e in entwuerfe if e.an}
        neu["bestellung_gesendet"] |= EntwurfTyp.BESTELLUNG_WEINGUT in typen
        neu["auftragskopie_gesendet"] |= EntwurfTyp.AUFTRAGSKOPIE_KUNDE in typen
        neu["spedition_beauftragt"] |= EntwurfTyp.SPEDITION_AUFTRAG in typen
        if fixtermin is not None:
            neu["lieferant_bestaetigt"] |= fixtermin.lieferant_bestaetigt
            neu["spedition_bestaetigt"] |= fixtermin.spedition_bestaetigt
            neu["abgeholt"] |= fixtermin.abgeholt
        if sendung is not None:
            rang = _STATUS_RANG.get(sendung.status)
            if rang is not None and rang >= _STATUS_RANG[SendungStatus.BEAUFTRAGT]:
                neu["spedition_beauftragt"] = True
            if (rang is not None and rang >= _STATUS_RANG[SendungStatus.BESTAETIGT]) or bool(sendung.referenz):
                neu["spedition_bestaetigt"] = True
            if rang is not None and rang >= _STATUS_RANG[SendungStatus.ABGEHOLT]:
                neu["abgeholt"] = True
            if sendung.status == SendungStatus.ZUGESTELLT:
                neu["zugestellt"] = True
        return neu

    @staticmethod
    def _erwartete_kuerzel(paket: Belegpaket) -> list[str]:
        erwartet = ["Kd", "Cde", "Kopie", "Speditionsauftrag"]
        if paket.lieferschein_erstellen:
            erwartet.append("Ls")
        return erwartet

    def _offene_punkte(
        self,
        dossier: Dossier,
        paket: Belegpaket,
        ergebnis: BuchungsErgebnis | None,
        entwuerfe: list[EmailEntwurf],
        fixtermin: Fixtermin | None,
        sendung: Sendung | None,
        fehlende_dateien: list[str],
    ) -> list[str]:
        punkte: list[str] = list(fehlende_dateien)
        typen_vorhanden = {d.typ for d in dossier.dateien}
        for kuerzel in self._erwartete_kuerzel(paket):
            if not (_ERFUELLT_DURCH[kuerzel] & typen_vorhanden):
                punkte.append(f"{_BEZEICHNUNG_JE_KUERZEL[kuerzel]} ({kuerzel}) fehlt im Dossier (PDF nicht vorhanden)")
        for entwurf in entwuerfe:
            if not entwurf.an:
                punkte.append(
                    f"Kein Empfänger für den E-Mail-Entwurf '{entwurf.typ.value}' – Kontakt in der Kontaktliste fehlt"
                )
            punkte.extend(f"E-Mail-Entwurf '{entwurf.typ.value}': {h}" for h in entwurf.hinweise)
        typen = {e.typ for e in entwuerfe}
        for typ, schluessel, text in _ERWARTETE_ENTWUERFE:
            if typ not in typen and not dossier.checkliste.get(schluessel, False):
                punkte.append(text)
        for h in paket.alle_hinweise:
            if h.schweregrad in (Schweregrad.FEHLER, Schweregrad.WARNUNG):
                pos = f" (Pos. {h.position_laufnr})" if h.position_laufnr else ""
                punkte.append(f"[{h.schweregrad.value.upper()}] {h.code.value}{pos}: {h.text}")
        if fixtermin is None and dossier.fixtermin is None:
            punkte.append("Kein Fixtermin bekannt (Liefertag fehlt im Dokument, kein Eintrag in der Fixterminüberwachung)")
        elif fixtermin is not None and fixtermin.zeile is None:
            punkte.append("Fixtermin ist noch nicht in der Fixterminüberwachung eingetragen")
        if not dossier.checkliste.get("lieferant_bestaetigt", False):
            punkte.append("Bestätigung des Lieferanten steht aus")
        if not dossier.checkliste.get("spedition_bestaetigt", False):
            punkte.append("Bestätigung der Spedition steht aus")
        if dossier.checkliste.get("spedition_beauftragt", False) and not dossier.tracking_referenz:
            punkte.append("Keine Referenz der Spedition bekannt (Tracking nicht möglich)")
        if sendung is not None:
            punkte.extend(sendung.hinweise)
            if sendung.status in (SendungStatus.VERSPAETET, SendungStatus.PROBLEM):
                punkte.append(f"Sendung meldet Status '{sendung.status.value}'")
        if ergebnis is not None:
            if ergebnis.dry_run:
                punkte.append("Dry Run: Belege wurden nicht in Lexware gespeichert (keine Belegnummern)")
            teile = [ergebnis.angebot, *ergebnis.alle_einkaeufe(), ergebnis.verkauf, ergebnis.lieferschein]
            for teil in teile:
                if teil is not None and not teil.erfolgreich:
                    punkte.append(f"Lexware-Beleg {teil.belegart.value} nicht erfolgreich: {teil.fehler or 'ohne Fehlertext'}")
            punkte.extend(ergebnis.warnungen)
        return _eindeutig(punkte)

    # -- Index ---------------------------------------------------------------------------

    def _schreibe_index(self, dossier: Dossier, paket: Belegpaket | None) -> None:
        self.backend.schreibe_text(self.wurzel, self._rel(dossier, INDEX_DATEI), dossier.model_dump_json(indent=2))
        if paket is not None:
            self.backend.schreibe_text(self.wurzel, self._rel(dossier, PAKET_DATEI), paket.model_dump_json(indent=2))

    def _lade_text(self, relativ: str, arbeit: Path) -> str | None:
        if not self.backend.existiert(self.wurzel, relativ):
            return None
        ziel = arbeit / relativ.replace("/", "__")
        try:
            pfad = self.backend.herunterladen(self.wurzel, relativ, ziel)
        except M365NichtGefunden:
            return None
        return Path(pfad).read_text(encoding="utf-8")

    def lade(self, ordnername: str) -> Dossier | None:
        """Liest ``dossier.json`` eines Unterordners; fehlt oder passt sie nicht, ``None`` (Hinweis im Log)."""
        with self._arbeit() as arbeit:
            text = self._lade_text(f"{ordnername}/{INDEX_DATEI}", arbeit)
        if text is None:
            return None
        try:
            return Dossier.model_validate_json(text)
        except (ValidationError, json.JSONDecodeError, ValueError) as exc:
            logger.warning("dossier.json in %s ist nicht lesbar: %s", ordnername, exc)
            return None

    def lade_paket(self, dossier: Dossier) -> Belegpaket | None:
        """Das zum Dossier gespeicherte Belegpaket (``paket.json``), falls vorhanden und gültig."""
        with self._arbeit() as arbeit:
            text = self._lade_text(self._rel(dossier, PAKET_DATEI), arbeit)
        if text is None:
            return None
        try:
            return Belegpaket.model_validate_json(text)
        except (ValidationError, json.JSONDecodeError, ValueError) as exc:
            logger.warning("paket.json in %s ist nicht lesbar: %s", dossier.name, exc)
            return None

    def liste(self) -> list[Dossier]:
        """Alle Dossiers unterhalb der Wurzel (Unterordner mit lesbarer ``dossier.json``), nach Name sortiert."""
        try:
            eintraege = self.backend.liste(self.wurzel, "")
        except M365NichtGefunden:
            return []
        dossiers: list[Dossier] = []
        for eintrag in eintraege:
            if not eintrag.ist_ordner:
                continue
            dossier = self.lade(eintrag.name)
            if dossier is not None:
                dossiers.append(dossier)
        dossiers.sort(key=lambda d: d.name.casefold())
        return dossiers

    def finde_dossier(
        self,
        bestellnummer: str | None = None,
        kundenbestellnummer: str | None = None,
        referenz: str | None = None,
        *,
        firma_id: str | None = None,
    ) -> Dossier | None:
        """Dossier zu einer Lexware-Bestell-/AB-Nummer, Kundenbestellnummer oder Speditionsreferenz
        (Vergleich ohne Leerzeichen und Groß-/Kleinschreibung). ``firma_id`` schränkt auf eine Firma
        ein (Lexware-Nummern sind nur je Firma eindeutig). Mehrere Treffer: das zuletzt
        aktualisierte. Ohne Suchbegriff ``None``."""
        best = _norm_nummer(bestellnummer)
        kd = _norm_nummer(kundenbestellnummer)
        ref = _norm_nummer(referenz)
        if not (best or kd or ref):
            return None
        treffer: list[Dossier] = []
        for dossier in self.liste():
            if firma_id and dossier.firma_id and dossier.firma_id != firma_id:
                continue
            if best and best in {_norm_nummer(dossier.bestellnummer), _norm_nummer(dossier.ab_nummer)}:
                treffer.append(dossier)
            elif kd and kd == _norm_nummer(dossier.kundenbestellnummer):
                treffer.append(dossier)
            elif ref and ref == _norm_nummer(dossier.tracking_referenz):
                treffer.append(dossier)
        if not treffer:
            return None
        return max(treffer, key=lambda d: _zeit_schluessel(d.aktualisiert_am or d.erstellt_am))

    def _vorhandenes_dossier(self, paket: Belegpaket, ergebnis: BuchungsErgebnis | None, name: str) -> Dossier | None:
        """Vorhandenes Dossier: gleicher Ordnername, sonst über Kundenbestellnummer / Lexware-Nummern."""
        dossier = self.lade(name)
        if dossier is not None:
            return dossier
        kd = (paket.roh.bestellnummer or "").strip() or None
        nummern: list[str] = []
        if ergebnis is not None:
            for teil in (ergebnis.einkauf, ergebnis.angebot, ergebnis.verkauf):
                if teil is not None and teil.belegnummer:
                    nummern.append(teil.belegnummer)
        for nummer in nummern:
            dossier = self.finde_dossier(bestellnummer=nummer, firma_id=paket.firma_id)
            if dossier is not None:
                return dossier
        if kd:
            return self.finde_dossier(kundenbestellnummer=kd, firma_id=paket.firma_id)
        return None

    # -- Gesamt-PDF ------------------------------------------------------------------------

    def _gesamt_pdf(self, dossier: Dossier, arbeit: Path, jetzt: datetime) -> DossierDatei | None:
        from pypdf import PdfWriter

        pdfs = sorted(
            (d for d in dossier.dateien if d.name.lower().endswith(".pdf") and d.name != GESAMT_PDF and "/" not in d.name),
            key=lambda d: d.name.casefold(),
        )
        if not pdfs:
            return None
        writer = PdfWriter()
        seiten = 0
        quellordner = arbeit / "gesamt"
        quellordner.mkdir(parents=True, exist_ok=True)
        for d in pdfs:
            try:
                lokal = self.backend.herunterladen(self.wurzel, self._rel(dossier, d.name), quellordner / d.name)
                vorher = len(writer.pages)
                writer.append(str(lokal))
                seiten += len(writer.pages) - vorher
            except (M365Fehler, OSError, ValueError) as exc:
                logger.warning("Dossier %s: %s konnte nicht ins Gesamt-PDF übernommen werden: %s", dossier.name, d.name, exc)
            except Exception as exc:  # pypdf-Fehler bei defekten Dateien
                logger.warning("Dossier %s: %s ist kein lesbares PDF (%s) – im Gesamt-PDF ausgelassen.", dossier.name, d.name, exc)
        if seiten == 0:
            return None
        ziel = arbeit / GESAMT_PDF
        with ziel.open("wb") as f:
            writer.write(f)
        writer.close()
        logger.info("Dossier %s: Gesamt-PDF mit %d Seiten aus %d Dateien erzeugt.", dossier.name, seiten, len(pdfs))
        return self._hochladen_ersetzen(dossier, ziel, GESAMT_PDF, "gesamt_pdf", jetzt)

    # -- Öffentliche Abläufe -----------------------------------------------------------------

    def erstelle_oder_aktualisiere(
        self,
        paket: Belegpaket,
        ergebnis: BuchungsErgebnis | None,
        *,
        quelldatei: Path | None = None,
        lexware_pdfs: Mapping[str, Path] | None = None,
        entwuerfe: Iterable[EmailEntwurf] = (),
        fixtermin: Fixtermin | None = None,
        sendung: Sendung | None = None,
        weitere_dateien: Iterable[Path] = (),
        dossier: Dossier | None = None,
    ) -> Dossier:
        """Legt das Dossier zum Vorgang an bzw. schreibt es fort und liefert den aktuellen Stand.

        * Ordner (falls neu) laut :func:`dossier_name`; ein vorhandenes Dossier wird über den Namen,
          die Lexware-Nummern oder die Kundenbestellnummer wiedergefunden und fortgeschrieben
          (``dossier`` gibt es direkt vor, z. B. beim Nachtrag aus dem Überwachungslauf).
        * Datum in den Namen: Buchungsdatum (:func:`stichtag`); ohne ``ergebnis`` das beim Anlegen
          gespeicherte ``Dossier.stichtag``, damit Nachträge (Oc) dasselbe Datum tragen wie Kd/Cde.
        * ``quelldatei`` (sonst ``paket.roh.quelldatei``) -> ``01_Kundenbestellung_<Kd>``;
          ``lexware_pdfs`` (Dokumentkürzel -> Pfad) -> ``02_…``/``03_…``/``04_…``/``05_…``;
          ``entwuerfe`` -> ``06_Mails/``; ``fixtermin`` -> ``07_Fixtermin.txt``; ``weitere_dateien``
          -> ``09_Weitere_<Name>``. Nichts wird überschrieben (gleich -> übersprungen, sonst ``_2``).
        * Checkliste und offene Punkte aus Ergebnis, Entwürfen, Fixtermin und Sendung; Deckblatt
          immer neu; ``dossier.json``/``paket.json`` schreiben; Gesamt-PDF bei ``dossier.gesamt_pdf``.
        """
        jetzt = self._jetzt()
        tag = stichtag(paket, ergebnis)
        name = dossier_name(paket, ergebnis, settings=self.settings)
        if dossier is not None:
            dossier = self.lade(dossier.name) or dossier  # aktueller Stand aus dossier.json
        else:
            dossier = self._vorhandenes_dossier(paket, ergebnis, name)
        if dossier is None:
            dossier = Dossier(name=name, ordner="", erstellt_am=jetzt, stichtag=tag)
            logger.info("Neues Dossier %s", name)
        else:
            logger.info("Dossier %s wird fortgeschrieben", dossier.name)
            if ergebnis is None and dossier.stichtag is not None:
                tag = dossier.stichtag  # Nachtrag ohne Buchung: Datum der Buchung beibehalten
            elif dossier.stichtag is None:
                dossier.stichtag = tag
        dossier.ordner = self.backend.ordner_anlegen(self.wurzel, dossier.name)
        self._aktualisiere_kopf(dossier, paket, ergebnis, fixtermin, sendung)
        entwuerfe = list(entwuerfe)
        lexware_pdfs = dict(lexware_pdfs or {})
        fehlende: list[str] = []

        with self._arbeit() as arbeit:
            vorhandene = self._vorhandene_dateien(dossier.name)

            quelle = quelldatei if quelldatei is not None else paket.roh.quelldatei
            if _ist_datei(quelle):
                self._lege_beleg_ab(dossier, vorhandene, "Kd", Path(quelle), paket, tag, quelle_text=f"eingang:{Path(quelle).name}", jetzt=jetzt)
            elif not any(d.typ == KUERZEL_DATEITYP["Kd"] for d in dossier.dateien):
                fehlende.append(f"Kundenbestellung (Kd): Quelldatei {quelle} nicht gefunden")

            for kuerzel, pfad in lexware_pdfs.items():
                if not _ist_datei(pfad):
                    fehlende.append(f"{_BEZEICHNUNG_JE_KUERZEL.get(kuerzel, kuerzel)} ({kuerzel}): PDF {pfad} nicht gefunden")
                    continue
                self._lege_beleg_ab(dossier, vorhandene, kuerzel, Path(pfad), paket, tag, quelle_text=f"lexware:{Path(pfad).name}", jetzt=jetzt)

            if entwuerfe:
                self.backend.ordner_anlegen(self.wurzel, self._rel(dossier, MAIL_ORDNER))
                fallback_zeit = ergebnis.zeitpunkt if ergebnis is not None else jetzt
                for laufnr, entwurf in enumerate(entwuerfe, start=1):
                    self._lege_entwurf_ab(dossier, vorhandene, entwurf, arbeit, laufnr, fallback_zeit=fallback_zeit, jetzt=jetzt)

            if fixtermin is not None:
                self.backend.schreibe_text(self.wurzel, self._rel(dossier, FIXTERMIN_DATEI), fixtermin_text(fixtermin, stand=jetzt))
                text_groesse = len(fixtermin_text(fixtermin, stand=jetzt).encode("utf-8"))
                self._ersetze_eintrag(dossier, DossierDatei(name=FIXTERMIN_DATEI, typ="fixtermin", quelle="fixterminueberwachung", hinzugefuegt=jetzt, groesse=text_groesse))

            for pfad in weitere_dateien:
                pfad = Path(pfad)
                if not _ist_datei(pfad):
                    fehlende.append(f"Weitere Datei {pfad} nicht gefunden")
                    continue
                self._lege_datei_ab(dossier, vorhandene, pfad, f"{WEITERE_PRAEFIX}_{pfad.name}", dateityp_aus_name(pfad.name), quelle_text=f"weitere:{pfad.name}", jetzt=jetzt)

            dossier.checkliste = self._checkliste(dossier.checkliste, entwuerfe, fixtermin, sendung)
            dossier.vorgeschlagener_zielordner = zielordner_vorschlag(paket, self.settings)
            dossier.aktualisiert_am = jetzt
            dossier.offene_punkte = self._offene_punkte(dossier, paket, ergebnis, entwuerfe, fixtermin, sendung, fehlende)

            # Deckblatt immer neu: Eintrag vorab, damit die Dateiliste im Deckblatt vollständig ist.
            self._ersetze_eintrag(dossier, DossierDatei(name=DECKBLATT, typ="deckblatt", quelle="auftragsautomat", hinzugefuegt=jetzt))
            if self.settings.dossier.gesamt_pdf:
                self._ersetze_eintrag(dossier, DossierDatei(name=GESAMT_PDF, typ="gesamt_pdf", quelle="auftragsautomat", hinzugefuegt=jetzt))
            deckblatt_lokal = erzeuge_deckblatt(dossier, paket, arbeit / DECKBLATT, settings=self.settings, jetzt=jetzt)
            self._hochladen_ersetzen(dossier, deckblatt_lokal, DECKBLATT, "deckblatt", jetzt)

            if self.settings.dossier.gesamt_pdf:
                if self._gesamt_pdf(dossier, arbeit, jetzt) is None:
                    dossier.dateien = [d for d in dossier.dateien if d.name != GESAMT_PDF]

            self._schreibe_index(dossier, paket)
        logger.info("Dossier %s: %d Dateien, %d offene Punkte.", dossier.name, len(dossier.dateien), len(dossier.offene_punkte))
        return dossier

    def ergaenze_mail(
        self,
        dossier: Dossier,
        mail_eml: Path,
        *,
        typ: str | None = None,
        checkliste: Mapping[str, bool] | None = None,
    ) -> Dossier:
        """Legt eine eingegangene Mail (``.eml``) unter ``06_Mails/`` ab, setzt optional Häkchen der
        Checkliste (``checkliste``, z. B. ``{"spedition_bestaetigt": True}``), erzeugt das Deckblatt
        neu (mit dem gespeicherten Belegpaket, falls vorhanden) und schreibt den Index.

        ``typ`` ist der Typ-Teil des Dateinamens (Standard: Kopfzeile ``X-Auftragsautomat-Typ`` der
        Mail, sonst ``eingang``); der Zeitstempel kommt aus der ``Date``-Kopfzeile, sonst von der Datei.
        """
        mail_eml = Path(mail_eml)
        if not mail_eml.is_file():
            raise FileNotFoundError(f"Mail-Datei nicht gefunden: {mail_eml}")
        jetzt = self._jetzt()
        zeit, typ_text = self._mail_metadaten(mail_eml, typ)
        self.backend.ordner_anlegen(self.wurzel, self._rel(dossier, MAIL_ORDNER))
        vorhandene = self._vorhandene_dateien(dossier.name)
        self._lege_datei_ab(dossier, vorhandene, mail_eml, mail_dateiname(zeit, typ_text), "mail", quelle_text=f"mail:{mail_eml.name}", jetzt=jetzt)
        if checkliste:
            for schluessel, wert in checkliste.items():
                if schluessel not in CHECKLISTE:
                    logger.warning("Unbekannter Checklistenpunkt '%s' wird trotzdem übernommen.", schluessel)
                dossier.checkliste[schluessel] = bool(wert)
        dossier.aktualisiert_am = jetzt
        paket = self.lade_paket(dossier)
        with self._arbeit() as arbeit:
            deckblatt_lokal = erzeuge_deckblatt(dossier, paket, arbeit / DECKBLATT, settings=self.settings, jetzt=jetzt)
            self._hochladen_ersetzen(dossier, deckblatt_lokal, DECKBLATT, "deckblatt", jetzt)
        self._schreibe_index(dossier, None)
        return dossier

    @staticmethod
    def _mail_metadaten(mail_eml: Path, typ: str | None) -> tuple[datetime, str]:
        zeit: datetime | None = None
        typ_text = typ
        try:
            nachricht = email.message_from_bytes(mail_eml.read_bytes(), policy=email.policy.default)
            if not typ_text:
                typ_text = str(nachricht.get("X-Auftragsautomat-Typ") or "").strip() or None
            datum = nachricht.get("Date")
            if datum:
                zeit = parsedate_to_datetime(str(datum))
        except (OSError, TypeError, ValueError) as exc:
            logger.warning("Kopfzeilen von %s nicht lesbar (%s); Dateizeit wird verwendet.", mail_eml, exc)
        if zeit is None:
            zeit = datetime.fromtimestamp(mail_eml.stat().st_mtime).astimezone()
        return zeit, typ_text or "eingang"
