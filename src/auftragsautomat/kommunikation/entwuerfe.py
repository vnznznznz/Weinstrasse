"""E-Mail-Entwürfe im Hausstil – werden gespeichert, **nie gesendet** (docs/AUSBAUSTUFE_2.md, 2.4).

:class:`Entwurfsfabrik` baut aus Belegpaket, Buchungsergebnis, Fixtermin und den erzeugten PDFs die
Entwürfe eines Vorgangs:

* **Bestellung ans Weingut** (englisch) an den "Orders"-Kontakt des Lieferanten, Anhang Bestell-PDF
  ("Cde", bei Revision "Neu"). Entfällt, wenn keine Einkauf-Bestellung nötig ist
  (``paket.einkauf_erforderlich`` False, Weinstrasse Agentur) – außer ``bestellung_ohne_einkauf`` ist
  gesetzt (Abschnitt 5.2: das WS_EN-Formular der Agentur dient als Cde ans Weingut).
* **Auftragskopie an den Kunden** (deutsch) an den Dispo-Kontakt, CC ``kommunikation.cc_standard`` (+ interne
  Kontakte der Liste), Anhang "Kopie"/"Neu Kopie". Die Wörter aus
  ``kommunikation.verbotene_woerter_auftragskopie`` dürfen in Betreff, Text und Anhangsnamen nicht
  vorkommen; :meth:`Entwurfsfabrik.pruefe_auftragskopie` wird in :meth:`fuer_buchung` erzwungen – ein
  Entwurf mit Treffern wird **nicht** erzeugt, stattdessen ein Hinweis (``hinweise`` der Fabrik und
  ``BuchungsErgebnis.warnungen``).
* **Speditionsauftrag** (deutsch) an das Postfach "Neue Aufträge" der Spedition laut Kontaktliste
  (:meth:`Zuordner.postfach_spedition`; nichts ist fest kodiert), Anhänge Speditionsauftrag + Lieferschein.
* optional **Lieferschein** (``paket.lieferschein_erstellen`` und ``Firma.lieferschein_erlaubt``).
* **Nachfragen/Erinnerung** für den Überwachungslauf: :meth:`nachfrage_lieferant`,
  :meth:`nachfrage_spedition`, :meth:`erinnerung_intern`.

Fehlt ein Anhang, entsteht der Entwurf trotzdem (Hinweis "Anhang fehlt: <Name>"); fehlt ein Kontakt,
bleibt ``an`` leer (Hinweis, der Anwender ergänzt). Anhänge heißen immer nach der Titulierung
(``benennung.name_fuer_paket``): eine PDF mit abweichendem Namen wird unter dem richtigen Namen in
``kommunikation.entwuerfe_ordner/anhaenge`` kopiert und von dort angehängt. :meth:`speichere` legt die
Entwürfe über ein ``MailBackend`` an (Graph-Entwurf, Outlook-Entwurf oder ``.eml``).
"""

from __future__ import annotations

import logging
import shutil
from collections.abc import Callable, Iterable, Mapping
from datetime import date, datetime
from pathlib import Path
from typing import Any

from auftragsautomat import benennung
from auftragsautomat.benennung import normalisiere
from auftragsautomat.config import Firma, Settings
from auftragsautomat.kommunikation.anrede import (
    AnredeRegeln,
    Sprache,
    anrede_regeln_fuer_zuordner,
    baue_anrede,
    grussformel,
    lade_anrede_regeln,
    sprache_normalisiert,
)
from auftragsautomat.kommunikation.vorlagen import (
    SignaturBlock,
    SignaturKatalog,
    Signaturvariante,
    Vorlagen,
    lade_signaturen,
    signatur_fuer,
    vorlagen_aus_settings,
)
from auftragsautomat.kontakte.zuordnung import Zuordner
from auftragsautomat.m365.basis import MailBackend, html_zu_text
from auftragsautomat.models import (
    Belegpaket,
    BuchungsErgebnis,
    EmailEntwurf,
    EntwurfTyp,
    Fixtermin,
    Kontakt,
    Kontaktgruppe,
    Sendung,
    Ueberwachungsbericht,
)

logger = logging.getLogger(__name__)

__all__ = [
    "ANHANG_ORDNER",
    "KEINE_SPEDITION",
    "Entwurfsfabrik",
    "erstelle_entwuerfe",
    "pruefe_auftragskopie",
]

#: Unterordner von ``kommunikation.entwuerfe_ordner`` für Anhangskopien mit Titulierungsnamen.
ANHANG_ORDNER = "anhaenge"
#: Speditionswerte, bei denen kein Speditionsauftrag entsteht (normalisiert verglichen).
KEINE_SPEDITION: frozenset[str] = frozenset({"", "unbekannt", "selbstabholung", "abholung", "keine", "ohne", "-", "–"})
UNBEKANNT = benennung.UNBEKANNT


# ---------------------------------------------------------------------------
# Hilfen
# ---------------------------------------------------------------------------


def _text(wert: Any) -> str:
    return "" if wert is None else str(wert).strip()


def _kunde_name(paket: Belegpaket) -> str:
    kunde = paket.kunde.kunde
    if kunde is not None:
        name = _text(kunde.name) or _text(kunde.matchcode)
        if name:
            return name
    return _text(paket.roh.kunde_text) or UNBEKANNT


def _lieferant_name(paket: Belegpaket) -> str:
    lieferant = paket.lieferant.lieferant
    if lieferant is not None:
        name = _text(lieferant.name) or _text(lieferant.matchcode)
        if name:
            return name
    return _text(paket.roh.lieferant_text) or UNBEKANNT


def _kundenbestellnummer(paket: Belegpaket) -> str | None:
    return _text(paket.roh.bestellnummer) or _text(paket.roh.belegnummer) or None


def _lieferadresse(paket: Belegpaket) -> str | None:
    if paket.roh.lieferadresse:
        return " ".join(paket.roh.lieferadresse.split())
    kunde = paket.kunde.kunde
    if kunde is None:
        return None
    teile = [_text(kunde.strasse), " ".join(t for t in (_text(kunde.plz), _text(kunde.ort)) if t)]
    adresse = ", ".join(t for t in teile if t)
    return adresse or None


def _abholadresse(paket: Belegpaket) -> str | None:
    if paket.roh.ladeadresse:
        return " ".join(paket.roh.ladeadresse.split())
    lieferant = paket.lieferant.lieferant
    if lieferant is None:
        return None
    teile = [t for t in (_text(lieferant.ort), _text(lieferant.land)) if t]
    return ", ".join(teile) or None


def _positionen(paket: Belegpaket) -> list[dict[str, Any]]:
    zeilen: list[dict[str, Any]] = []
    for p in paket.aktive_positionen():
        zeilen.append(
            {
                "laufnr": p.roh.laufnr,
                "menge": p.menge,
                "einheit": p.roh.einheit or "Fl",
                "bezeichnung": p.artikel.bezeichnung if p.artikel is not None else p.roh.bezeichnung,
                "artikelnummer": p.artikel.artikelnummer if p.artikel is not None else p.roh.artikelnummer,
                "jahrgang": p.artikel.jahrgang if p.artikel is not None and p.artikel.jahrgang else p.roh.jahrgang,
            }
        )
    return zeilen


def _bezug(**werte: Any) -> dict[str, str]:
    return {k: _text(v) for k, v in werte.items() if _text(v)}


def _in_text(text: str | None, woerter: Iterable[str]) -> list[str]:
    norm = normalisiere(text)
    if not norm:
        return []
    treffer: list[str] = []
    gesehen: set[str] = set()
    for wort in woerter:
        wn = normalisiere(wort)
        if wn and wn not in gesehen and wn in norm:
            gesehen.add(wn)
            treffer.append(wort)
    return treffer


def pruefe_auftragskopie(entwurf: EmailEntwurf, settings: Settings) -> list[str]:
    """Verbotene Wörter (``kommunikation.verbotene_woerter_auftragskopie``) in Betreff, Text (Klartext und
    HTML) und Anhangsnamen – case-insensitiv und ohne Umlaute ("Auftragsbestaetigung" zählt).
    Gleichbedeutende Schreibweisen werden einmal gemeldet; Fundstellen stehen im Log."""
    woerter = settings.kommunikation.verbotene_woerter_auftragskopie
    stellen: list[tuple[str, str | None]] = [
        ("Betreff", entwurf.betreff),
        ("Text", entwurf.text_plain),
        ("HTML", html_zu_text(entwurf.text_html)),
    ]
    stellen.extend((f"Anhang {Path(a).name}", Path(a).name) for a in entwurf.anhaenge)
    treffer: list[str] = []
    gesehen: set[str] = set()
    for ort, text in stellen:
        for wort in _in_text(text, woerter):
            wn = normalisiere(wort)
            logger.warning("Auftragskopie enthält verbotenes Wort '%s' (%s).", wort, ort)
            if wn not in gesehen:
                gesehen.add(wn)
                treffer.append(wort)
    return treffer


# ---------------------------------------------------------------------------
# Fabrik
# ---------------------------------------------------------------------------


class Entwurfsfabrik:
    """Erzeugt E-Mail-Entwürfe für eine Firma; ``kontakte`` (``Zuordner``) liefert Empfänger, Anrede und
    Sprache aus der Kontaktliste und darf fehlen (dann bleiben Empfänger leer, Hinweis)."""

    def __init__(
        self,
        settings: Settings,
        kontakte: Zuordner | None,
        firma: Firma,
        *,
        vorlagen: Vorlagen | None = None,
        anrede_regeln: AnredeRegeln | None = None,
        signaturen: SignaturKatalog | None = None,
        signatur_variante: Signaturvariante | None = None,
        signatur_einfuegen: bool | None = None,
        bestellung_ohne_einkauf: bool = False,
        heute: Callable[[], date] | None = None,
    ) -> None:
        self.settings = settings
        self.kontakte = kontakte
        self.firma = firma
        self.vorlagen = vorlagen or vorlagen_aus_settings(settings)
        self.anrede_regeln = anrede_regeln or lade_anrede_regeln(settings.kommunikation.anrede_datei)
        self.signaturen = signaturen or lade_signaturen(settings.kommunikation.signaturen_datei)
        self.signatur_variante = signatur_variante
        # Graph und .eml tragen keine Outlook-Signatur -> eigene Signatur aus signaturen.yaml anhängen.
        # Backend "lokal" (COM): Outlook fügt beim Display() seine Standardsignatur selbst ein, unser Text
        # kommt davor (m365.lokal.fuege_vor_signatur_ein) -> keine zweite Signatur anhängen.
        # TODO-PRUEFEN: Fügt das klassische Outlook beim Öffnen eines per Graph erzeugten Entwurfs zusätzlich
        # seine Signatur ein (Recherche B.2: nicht dokumentiert, Erwartung nein)? Sonst hier für "graph" False.
        self.signatur_einfuegen = settings.m365.backend != "lokal" if signatur_einfuegen is None else signatur_einfuegen
        self.bestellung_ohne_einkauf = bestellung_ohne_einkauf
        self._heute = heute or date.today
        self.hinweise: list[str] = []
        if kontakte is not None and self.anrede_regeln.regeln and not kontakte.anrede_regeln:
            kontakte.anrede_regeln = anrede_regeln_fuer_zuordner(self.anrede_regeln)

    # -- Bausteine ------------------------------------------------------------------------

    @property
    def anhang_ordner(self) -> Path:
        return Path(self.settings.kommunikation.entwuerfe_ordner) / ANHANG_ORDNER

    def _signatur(self) -> SignaturBlock:
        return signatur_fuer(self.settings, self.firma.id, variante=self.signatur_variante, katalog=self.signaturen)

    def _sprache(self, kontakt: Kontakt | None, standard: str) -> Sprache:
        if kontakt is None:
            return sprache_normalisiert(standard)
        regel = self.anrede_regeln.regel_fuer(kontakt.email)
        if regel is not None and regel.sprache:
            return regel.sprache
        if self.kontakte is not None:
            return self.kontakte.sprache_fuer(kontakt)
        return sprache_normalisiert(kontakt.sprache)

    def _cc(self) -> list[str]:
        if self.kontakte is not None:
            return self.kontakte.empfaenger_cc(self.settings)
        gesehen: set[str] = set()
        cc: list[str] = []
        for adresse in self.settings.kommunikation.cc_standard:
            norm = adresse.strip().lower()
            if norm and norm not in gesehen:
                gesehen.add(norm)
                cc.append(adresse.strip())
        return cc

    def _empfaenger(self, kontakt: Kontakt | None, was: str) -> tuple[list[str], list[str]]:
        if kontakt is None:
            return [], [f"Kein Empfänger gefunden ({was}) – bitte Adresse ergänzen."]
        if not kontakt.email:
            return [], [f"Kontakt {kontakt.name or kontakt.firma} ({was}) hat keine E-Mail-Adresse in der Kontaktliste."]
        return [kontakt.email], []

    def _kontakt_lieferant(self, paket: Belegpaket) -> Kontakt | None:
        if self.kontakte is None:
            return None
        lieferant = paket.lieferant.lieferant or paket.roh.lieferant_text
        return self.kontakte.kontakt_fuer_lieferant(lieferant)

    def _kontakt_kunde(self, paket: Belegpaket) -> Kontakt | None:
        if self.kontakte is None:
            return None
        kunde = paket.kunde.kunde or paket.roh.kunde_text
        lieferant = paket.lieferant.lieferant or paket.roh.lieferant_text
        kontakte = self.kontakte.kontakte_fuer_kunde(kunde, lieferant)
        return kontakte[0] if kontakte else None

    def _spedition(self, paket: Belegpaket, fixtermin: Fixtermin | None) -> str | None:
        """Spedition des Vorgangs: Belegfeld > Fixtermin > Kontaktliste (Standard-Spedition je Kunde) >
        ``Firma.belegfelder_standard`` > ``kontakte.standard_spedition``. ``None`` bei Selbstabholung o. ä."""
        kandidaten: list[str | None] = [paket.belegfelder.spedition, fixtermin.spedition if fixtermin else None]
        if self.kontakte is not None:
            kunde = paket.kunde.kunde or paket.roh.kunde_text
            lieferant = paket.lieferant.lieferant or paket.roh.lieferant_text
            kandidaten.append(self.kontakte.spedition_fuer_kunde(kunde, lieferant))
        kandidaten.append(self.firma.belegfelder_standard.get("spedition"))
        kandidaten.append(self.settings.kontakte.standard_spedition)
        for kandidat in kandidaten:
            text = _text(kandidat)
            if not text:
                continue
            if normalisiere(text) in KEINE_SPEDITION:
                logger.info("Spedition '%s' – kein Speditionsauftrag nötig.", text)
                return None
            return text
        return None

    def _paket_mit_firma(self, paket: Belegpaket) -> Belegpaket:
        if paket.firma_id:
            return paket
        return paket.model_copy(update={"firma_id": self.firma.id})

    def _anhang(
        self,
        kuerzel: str,
        anhaenge: Mapping[str, Path],
        paket: Belegpaket,
        datum: date | None,
        *,
        pflicht: bool = True,
    ) -> tuple[Path | None, str | None]:
        """Pfad des Anhangs mit Titulierungsnamen und ggf. Hinweis.

        Fehlt die Datei (nicht im Mapping oder nicht vorhanden), gibt es nur den Hinweis "Anhang fehlt:
        <erwarteter Name>" (``pflicht`` False: keinen Hinweis). Heißt die Datei anders als nach Titulierung
        (Datum darf abweichen), wird sie unter dem richtigen Namen nach ``anhang_ordner`` kopiert.
        """
        paket = self._paket_mit_firma(paket)
        pfad = anhaenge.get(kuerzel)
        if pfad is None and kuerzel in benennung.REVISIONS_KUERZEL and paket.revision_wirksam:
            pfad = anhaenge.get(benennung.REVISIONS_KUERZEL[kuerzel])
        endung = Path(pfad).suffix if pfad is not None and Path(pfad).suffix else ".pdf"
        try:
            erwartet = benennung.name_fuer_paket(kuerzel, paket, self.settings, datum=datum, endung=endung)
        except (ValueError, KeyError) as exc:
            logger.warning("Titulierungsname für %s nicht bestimmbar (%s) – Datei bleibt wie sie ist.", kuerzel, exc)
            if pfad is None:
                return None, (f"Anhang fehlt: {kuerzel}" if pflicht else None)
            return Path(pfad), None
        if pfad is None or not Path(pfad).is_file():
            if pfad is not None:
                logger.warning("Anhang %s nicht gefunden: %s", kuerzel, pfad)
            return None, (f"Anhang fehlt: {erwartet}" if pflicht else None)
        pfad = Path(pfad)
        if pfad.name == erwartet:
            return pfad, None
        trenner = self.settings.benennung.trenner
        stamm_ohne_datum = Path(erwartet).stem.rsplit(trenner, 1)[0]
        if pfad.stem.startswith(stamm_ohne_datum):
            return pfad, None  # gleicher Name, nur anderes Datum (Datum der Lexware-Ausgabe)
        ziel = self.anhang_ordner / erwartet
        ziel.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(pfad, ziel)
        logger.info("Anhang %s als '%s' bereitgestellt (Quelle %s).", kuerzel, erwartet, pfad.name)
        return ziel, None

    def _entwurf(
        self,
        typ: EntwurfTyp,
        *,
        vorlage: str,
        kontakt: Kontakt | None,
        sprache: str,
        an: list[str],
        betreff: str,
        kontext: dict[str, Any],
        anhaenge: list[Path],
        bezug: dict[str, str],
        hinweise: list[str],
        cc: list[str] | None = None,
    ) -> EmailEntwurf:
        sp = sprache_normalisiert(sprache)
        kontext = {
            "anrede": baue_anrede(kontakt, sp, regeln=self.anrede_regeln),
            "gruss": grussformel(sp, self.firma.id, regeln=self.anrede_regeln),
            "sprache": sp,
            "firma": self.firma.name,
            "firma_kurz": self.firma.kurz,
            "absender_name": self.settings.kommunikation.absender_name,
            "anhaenge": [a.name for a in anhaenge],
            **kontext,
        }
        signatur = self._signatur() if self.signatur_einfuegen else None
        html, text = self.vorlagen.rendere(vorlage, signatur=signatur, **kontext)
        return EmailEntwurf(
            typ=typ,
            an=list(an),
            cc=list(cc or []),
            betreff=betreff,
            text_html=html,
            text_plain=text,
            anhaenge=list(anhaenge),
            absender=self.settings.kommunikation.absender_email,
            bezug=bezug,
            hinweise=list(hinweise),
        )

    def _datum(self, ergebnis: BuchungsErgebnis | None) -> date:
        if ergebnis is not None and ergebnis.zeitpunkt is not None:
            return ergebnis.zeitpunkt.date()
        return self._heute()

    @staticmethod
    def _liefertag(paket: Belegpaket, fixtermin: Fixtermin | None) -> date | None:
        if fixtermin is not None and fixtermin.liefertag:
            return fixtermin.liefertag
        return paket.roh.liefertag

    # -- Entwürfe zu einer Buchung ----------------------------------------------------------

    def bestellung_weingut(
        self,
        paket: Belegpaket,
        ergebnis: BuchungsErgebnis | None,
        fixtermin: Fixtermin | None,
        anhaenge: Mapping[str, Path],
    ) -> EmailEntwurf:
        """"New order <Order No.> – <Kunde>" (englisch) an den Orders-Kontakt des Weinguts, Anhang Cde/Neu."""
        kontakt = self._kontakt_lieferant(paket)
        an, hinweise = self._empfaenger(kontakt, f"Orders-Kontakt {_lieferant_name(paket)}")
        datum = self._datum(ergebnis)
        anhang, hinweis = self._anhang("Cde", anhaenge, paket, datum)
        if hinweis:
            hinweise.append(hinweis)
        bestellnummer = ergebnis.einkauf.belegnummer if ergebnis is not None and ergebnis.einkauf is not None else None
        bestellnummer = _text(bestellnummer) or _kundenbestellnummer(paket)
        kunde = _kunde_name(paket)
        liefertag = self._liefertag(paket, fixtermin)
        spedition = self._spedition(paket, fixtermin)
        kontext = {
            "bestellnummer": bestellnummer,
            "kundenbestellnummer": _kundenbestellnummer(paket) if _kundenbestellnummer(paket) != bestellnummer else None,
            "kunde": kunde,
            "lieferant": _lieferant_name(paket),
            "gesamtmenge": paket.gesamtmenge,
            "positionen": _positionen(paket),
            "liefertag": liefertag,
            "shipment": paket.belegfelder.shipment,
            "spedition": spedition,
            "lieferadresse": _lieferadresse(paket),
            "anhang_name": anhang.name if anhang else None,
            "hinweis": "Revised order – this order replaces our previous order"
            + (f" {paket.revision_von or paket.roh.revision_von}." if (paket.revision_von or paket.roh.revision_von) else ".")
            if paket.revision_wirksam
            else None,
        }
        betreff = f"New order {bestellnummer} – {kunde}" if bestellnummer else f"New order – {kunde}"
        if paket.revision_wirksam:
            betreff = "Revised order" + betreff[len("New order") :]
        return self._entwurf(
            EntwurfTyp.BESTELLUNG_WEINGUT,
            vorlage="bestellung_weingut",
            kontakt=kontakt,
            sprache="en",
            an=an,
            cc=self._cc(),
            betreff=betreff,
            kontext=kontext,
            anhaenge=[anhang] if anhang else [],
            bezug=_bezug(
                bestellnummer=bestellnummer,
                kundenbestellnummer=_kundenbestellnummer(paket),
                kunde=kunde,
                lieferant=_lieferant_name(paket),
                firma_id=self.firma.id,
                kuerzel="Neu" if paket.revision_wirksam else "Cde",
            ),
            hinweise=hinweise,
        )

    def auftragskopie_kunde(
        self,
        paket: Belegpaket,
        ergebnis: BuchungsErgebnis | None,
        fixtermin: Fixtermin | None,
        anhaenge: Mapping[str, Path],
    ) -> EmailEntwurf:
        """"Ihre Bestellung <Nr> – Auftragskopie zur Information" (deutsch) an den Dispo-Kontakt des Kunden,
        CC Kollegin/cc_standard, Anhang Kopie/Neu Kopie. Die Prüfung auf verbotene Wörter macht der Aufrufer
        (:meth:`fuer_buchung` erzwingt sie)."""
        kontakt = self._kontakt_kunde(paket)
        an, hinweise = self._empfaenger(kontakt, f"Dispo-Kontakt {_kunde_name(paket)}")
        datum = self._datum(ergebnis)
        anhang, hinweis = self._anhang("Kopie", anhaenge, paket, datum)
        if hinweis:
            hinweise.append(hinweis)
        kundenbestellnummer = _kundenbestellnummer(paket)
        auftragsnummer = ergebnis.verkauf.belegnummer if ergebnis is not None and ergebnis.verkauf is not None else None
        kunde = _kunde_name(paket)
        nummer = kundenbestellnummer or _text(auftragsnummer)
        kontext = {
            "kundenbestellnummer": kundenbestellnummer,
            "auftragsnummer": _text(auftragsnummer) or None,
            "kunde": kunde,
            "lieferant": _lieferant_name(paket),
            "liefertag": self._liefertag(paket, fixtermin),
            "spedition": self._spedition(paket, fixtermin),
            "gesamtmenge": paket.gesamtmenge,
            "positionen": _positionen(paket),
            "hinweis": "Diese Kopie ersetzt die frühere Fassung Ihres Auftrags." if paket.revision_wirksam else None,
        }
        betreff = f"Ihre Bestellung {nummer} – Auftragskopie zur Information" if nummer else "Ihre Bestellung – Auftragskopie zur Information"
        return self._entwurf(
            EntwurfTyp.AUFTRAGSKOPIE_KUNDE,
            vorlage="auftragskopie_kunde",
            kontakt=kontakt,
            sprache=self._sprache(kontakt, "de"),
            an=an,
            cc=self._cc(),
            betreff=betreff,
            kontext=kontext,
            anhaenge=[anhang] if anhang else [],
            bezug=_bezug(
                bestellnummer=auftragsnummer,
                kundenbestellnummer=kundenbestellnummer,
                kunde=kunde,
                lieferant=_lieferant_name(paket),
                firma_id=self.firma.id,
                kuerzel="Neu Kopie" if paket.revision_wirksam else "Kopie",
            ),
            hinweise=hinweise,
        )

    def spedition_auftrag(
        self,
        paket: Belegpaket,
        ergebnis: BuchungsErgebnis | None,
        fixtermin: Fixtermin | None,
        anhaenge: Mapping[str, Path],
        *,
        paletten: str | None = None,
    ) -> EmailEntwurf | None:
        """"Neuer Auftrag <Kunde> – Abholung <Lieferant>" (deutsch) an das Postfach "Neue Aufträge" der
        Spedition; Anhänge Speditionsauftrag + Lieferschein. ``None``, wenn keine Spedition beteiligt ist
        (Selbstabholung)."""
        spedition = self._spedition(paket, fixtermin)
        if spedition is None:
            return None
        # TODO-PRUEFEN: In der echten Kontaktliste muss das Auftragspostfach der Spedition an Position/Name
        # "Neue Aufträge" (o. ä., siehe kontakte.zuordnung._ZWECK_MUSTER) erkennbar sein; sonst landet der
        # Entwurf beim ersten Disponenten oder ohne Empfänger (Hinweis).
        kontakt = self.kontakte.postfach_spedition(spedition, "neue_auftraege") if self.kontakte is not None else None
        an, hinweise = self._empfaenger(kontakt, f"Postfach 'Neue Aufträge' der Spedition {spedition}")
        datum = self._datum(ergebnis)
        anhang_sped, hinweis = self._anhang("Speditionsauftrag", anhaenge, paket, datum)
        if hinweis:
            hinweise.append(hinweis)
        mit_lieferschein = self.firma.lieferschein_erlaubt and (
            paket.lieferschein_erstellen or "Ls" in anhaenge or (ergebnis is not None and ergebnis.lieferschein is not None)
        )
        anhang_ls: Path | None = None
        if mit_lieferschein:
            anhang_ls, hinweis = self._anhang("Ls", anhaenge, paket, datum, pflicht=paket.lieferschein_erstellen)
            if hinweis:
                hinweise.append(hinweis)
        kunde = _kunde_name(paket)
        lieferant = _lieferant_name(paket)
        referenz = (fixtermin.bestellnummer if fixtermin else None) or (
            ergebnis.einkauf.belegnummer if ergebnis is not None and ergebnis.einkauf is not None else None
        )
        kontext = {
            "kunde": kunde,
            "lieferant": lieferant,
            "spedition": spedition,
            "abholadresse": _abholadresse(paket),
            "lieferadresse": _lieferadresse(paket),
            "liefertag": self._liefertag(paket, fixtermin),
            "verladedatum": fixtermin.verladedatum if fixtermin else None,
            "gesamtmenge": paket.gesamtmenge,
            "positionen": _positionen(paket),
            "paletten": paletten,
            "referenz": _text(referenz) or _kundenbestellnummer(paket),
            "mit_lieferschein": anhang_ls is not None,
            "hinweis": None,
        }
        pfade = [p for p in (anhang_sped, anhang_ls) if p is not None]
        return self._entwurf(
            EntwurfTyp.SPEDITION_AUFTRAG,
            vorlage="spedition_auftrag",
            kontakt=kontakt,
            sprache=self._sprache(kontakt, "de"),
            an=an,
            cc=self._cc(),
            betreff=f"Neuer Auftrag {kunde} – Abholung {lieferant}",
            kontext=kontext,
            anhaenge=pfade,
            bezug=_bezug(
                bestellnummer=referenz,
                kundenbestellnummer=_kundenbestellnummer(paket),
                kunde=kunde,
                lieferant=lieferant,
                spedition=spedition,
                firma_id=self.firma.id,
                kuerzel="Speditionsauftrag",
            ),
            hinweise=hinweise,
        )

    def lieferschein(
        self,
        paket: Belegpaket,
        ergebnis: BuchungsErgebnis | None,
        fixtermin: Fixtermin | None,
        anhaenge: Mapping[str, Path],
        *,
        an_kunde: bool = False,
    ) -> EmailEntwurf:
        """Begleitmail zum Lieferschein ("Ls") an das Weingut (Standard) oder den Kunden (``an_kunde``);
        Sprache nach Empfänger."""
        kontakt = self._kontakt_kunde(paket) if an_kunde else self._kontakt_lieferant(paket)
        rolle = "kunde" if an_kunde else "lieferant"
        an, hinweise = self._empfaenger(kontakt, f"Lieferschein-Empfänger ({rolle})")
        datum = self._datum(ergebnis)
        anhang, hinweis = self._anhang("Ls", anhaenge, paket, datum)
        if hinweis:
            hinweise.append(hinweis)
        nummer = _text(ergebnis.lieferschein.belegnummer) if ergebnis is not None and ergebnis.lieferschein is not None else ""
        kunde = _kunde_name(paket)
        sprache = self._sprache(kontakt, "en" if not an_kunde else "de")
        kontext = {
            "nummer": nummer or None,
            "kunde": kunde,
            "lieferant": _lieferant_name(paket),
            "kundenbestellnummer": _kundenbestellnummer(paket),
            "liefertag": self._liefertag(paket, fixtermin),
            "empfaenger_rolle": rolle,
            "hinweis": None,
        }
        if sprache == "en":
            betreff = f"Delivery note {nummer} – {kunde}" if nummer else f"Delivery note – {kunde}"
        else:
            betreff = f"Lieferschein {nummer} – {kunde}" if nummer else f"Lieferschein – {kunde}"
        return self._entwurf(
            EntwurfTyp.LIEFERSCHEIN,
            vorlage="lieferschein",
            kontakt=kontakt,
            sprache=sprache,
            an=an,
            cc=self._cc(),
            betreff=betreff,
            kontext=kontext,
            anhaenge=[anhang] if anhang else [],
            bezug=_bezug(
                bestellnummer=nummer,
                kundenbestellnummer=_kundenbestellnummer(paket),
                kunde=kunde,
                lieferant=_lieferant_name(paket),
                firma_id=self.firma.id,
                kuerzel="Ls",
            ),
            hinweise=hinweise,
        )

    @staticmethod
    def _weingut_pdfs(pdfs: Mapping[str, Path], nummer: int) -> dict[str, Path]:
        """PDFs des ``nummer``-ten Weinguts (ab 2): Schlüssel "Cde (2)", "Speditionsauftrag (2)" … aus
        ``BuchungsErgebnis.pdfs`` unter den Basis-Kürzeln "Cde"/"Speditionsauftrag"."""
        return {k: Path(pdfs[f"{k} ({nummer})"]) for k in ("Cde", "Speditionsauftrag") if f"{k} ({nummer})" in pdfs}

    @staticmethod
    def _ergebnis_fuer_weingut(ergebnis: BuchungsErgebnis | None, nummer: int) -> BuchungsErgebnis | None:
        """Kopie des Buchungsergebnisses, in der ``einkauf`` die Bestellung des ``nummer``-ten Weinguts ist
        (Bestellnummer im Betreff "New order …")."""
        if ergebnis is None:
            return None
        weitere = ergebnis.einkauf_weitere
        einkauf = weitere[nummer - 2] if 0 <= nummer - 2 < len(weitere) else None
        return ergebnis.model_copy(update={"einkauf": einkauf, "einkauf_weitere": [], "pdfs": {}})

    def fuer_buchung(
        self,
        paket: Belegpaket,
        ergebnis: BuchungsErgebnis | None,
        fixtermin: Fixtermin | None,
        anhaenge: Mapping[str, Path] | None = None,
        *,
        weitere_weingueter: Iterable[Belegpaket] = (),
    ) -> list[EmailEntwurf]:
        """Alle Entwürfe zu einer Buchung in Sendereihenfolge: [Bestellung ans Weingut] -> Auftragskopie an den
        Kunden -> [Speditionsauftrag] -> [Lieferschein].

        ``anhaenge`` (Dokumentkürzel -> PDF) ergänzt ``ergebnis.pdfs``. Die Auftragskopie wird nur erzeugt,
        wenn :func:`pruefe_auftragskopie` keine verbotenen Wörter findet; sonst Hinweis in
        ``self.hinweise`` und ``ergebnis.warnungen``.

        ``weitere_weingueter``: Teilpakete der weiteren Weingüter einer Bestellung mit Positionen mehrerer
        Weingüter (``lexware.buchen.einkaufs_teilpakete`` ohne das Hauptweingut, in Buchungsreihenfolge). Jedes
        Weingut bekommt seine eigene Bestellung (PDF "Cde (2)" …) und die Spedition je Abholung einen eigenen
        Speditionsauftrag (PDF "Speditionsauftrag (2)" …) – Geschäftsregel 2.
        """
        self.hinweise = []
        pdfs: dict[str, Path] = {}
        if ergebnis is not None:
            pdfs.update({k: Path(v) for k, v in ergebnis.pdfs.items()})
        pdfs.update({k: Path(v) for k, v in (anhaenge or {}).items()})
        entwuerfe: list[EmailEntwurf] = []

        bestellung_noetig = paket.einkauf_erforderlich and self.firma.einkauf_bestellung
        weitere = list(weitere_weingueter) if bestellung_noetig else []
        if bestellung_noetig or self.bestellung_ohne_einkauf:
            entwuerfe.append(self.bestellung_weingut(paket, ergebnis, fixtermin, pdfs))
            for nummer, teil in enumerate(weitere, start=2):
                entwuerfe.append(
                    self.bestellung_weingut(teil, self._ergebnis_fuer_weingut(ergebnis, nummer), fixtermin, self._weingut_pdfs(pdfs, nummer))
                )
        else:
            logger.info("Firma %s: keine Bestellung ans Weingut (keine Einkauf-Bestellung).", self.firma.id)

        kopie = self.auftragskopie_kunde(paket, ergebnis, fixtermin, pdfs)
        treffer = pruefe_auftragskopie(kopie, self.settings)
        if treffer:
            hinweis = (
                f"Auftragskopie für {_kunde_name(paket)} nicht erzeugt: verbotene Wörter ({', '.join(treffer)}) "
                "in Betreff, Text oder Anhangsname – bitte Vorlage/Dateinamen prüfen."
            )
            logger.error(hinweis)
            self.hinweise.append(hinweis)
            if ergebnis is not None:
                ergebnis.warnungen.append(hinweis)
        else:
            entwuerfe.append(kopie)

        sped = self.spedition_auftrag(paket, ergebnis, fixtermin, pdfs)
        if sped is not None:
            entwuerfe.append(sped)
            for nummer, teil in enumerate(weitere, start=2):
                weitere_sped = self.spedition_auftrag(
                    teil, self._ergebnis_fuer_weingut(ergebnis, nummer), fixtermin, self._weingut_pdfs(pdfs, nummer)
                )
                if weitere_sped is not None:
                    entwuerfe.append(weitere_sped)

        if paket.lieferschein_erstellen and self.firma.lieferschein_erlaubt:
            entwuerfe.append(self.lieferschein(paket, ergebnis, fixtermin, pdfs))
        elif paket.lieferschein_erstellen:
            hinweis = f"Firma {self.firma.id}: Lieferschein nicht erlaubt – kein Lieferschein-Entwurf."
            logger.info(hinweis)
            self.hinweise.append(hinweis)

        for e in entwuerfe:
            for h in e.hinweise:
                logger.warning("%s: %s", e.typ.value, h)
        return entwuerfe

    # -- Überwachungslauf ---------------------------------------------------------------------

    def nachfrage_lieferant(self, fixtermin: Fixtermin, kontakt: Kontakt | None) -> EmailEntwurf:
        """Freundliche englische Nachfrage nach Eingangsbestätigung und Verladedatum (Bezug Order No., Fixtermin)."""
        an, hinweise = self._empfaenger(kontakt, f"Orders-Kontakt {fixtermin.lieferant_kurz}")
        bestellnummer = _text(fixtermin.bestellnummer) or _text(fixtermin.kundenbestellnummer) or None
        kontext = {
            "bestellnummer": bestellnummer,
            "kundenbestellnummer": fixtermin.kundenbestellnummer,
            "kunde": fixtermin.kunde_kurz,
            "lieferant": fixtermin.lieferant_kurz,
            "auftragsdatum": fixtermin.auftragsdatum,
            "liefertag": fixtermin.liefertag,
            "verladedatum": fixtermin.verladedatum,
            "hinweis": None,
        }
        betreff = f"Order {bestellnummer} – {fixtermin.kunde_kurz}: confirmation and loading date" if bestellnummer else f"Our order for {fixtermin.kunde_kurz}: confirmation and loading date"
        return self._entwurf(
            EntwurfTyp.NACHFRAGE_LIEFERANT,
            vorlage="nachfrage_lieferant",
            kontakt=kontakt,
            sprache="en",
            an=an,
            cc=self._cc(),
            betreff=betreff,
            kontext=kontext,
            anhaenge=[],
            bezug=_bezug(
                bestellnummer=bestellnummer,
                kundenbestellnummer=fixtermin.kundenbestellnummer,
                kunde=fixtermin.kunde_kurz,
                lieferant=fixtermin.lieferant_kurz,
                liefertag=fixtermin.liefertag.isoformat(),
                firma_id=fixtermin.firma_id or self.firma.id,
            ),
            hinweise=hinweise,
        )

    def nachfrage_spedition(self, fixtermin: Fixtermin, sendung: Sendung | None, kontakt: Kontakt | None) -> EmailEntwurf:
        """Deutsche Statusanfrage mit "Our Ref" (Referenz der Spedition) an das Info-Postfach."""
        spedition = _text(sendung.spedition if sendung else None) or _text(fixtermin.spedition) or self.settings.kontakte.standard_spedition
        an, hinweise = self._empfaenger(kontakt, f"Postfach 'Sendungsnachfragen' der Spedition {spedition}")
        referenz = _text(sendung.referenz if sendung else None) or _text(fixtermin.tracking_referenz) or None
        kundenreferenz = _text(sendung.kundenreferenz if sendung else None) or _text(fixtermin.bestellnummer) or _text(fixtermin.kundenbestellnummer) or None
        if referenz is None:
            hinweise.append("Keine Referenz der Spedition (Our Ref) bekannt – bitte im Entwurf ergänzen.")
        kontext = {
            "lieferant": fixtermin.lieferant_kurz,
            "kunde": fixtermin.kunde_kurz,
            "referenz": referenz,
            "kundenreferenz": kundenreferenz,
            "liefertag": fixtermin.liefertag,
            "verladedatum": fixtermin.verladedatum,
            "status": sendung.status.value if sendung is not None and sendung.status.value != "unbekannt" else None,
            "spedition": spedition,
            "hinweis": None,
        }
        betreff = f"Statusanfrage Our Ref {referenz} – {fixtermin.lieferant_kurz} / {fixtermin.kunde_kurz}" if referenz else f"Statusanfrage – {fixtermin.lieferant_kurz} / {fixtermin.kunde_kurz}"
        return self._entwurf(
            EntwurfTyp.NACHFRAGE_SPEDITION,
            vorlage="nachfrage_spedition",
            kontakt=kontakt,
            sprache=self._sprache(kontakt, "de"),
            an=an,
            cc=self._cc(),
            betreff=betreff,
            kontext=kontext,
            anhaenge=[],
            bezug=_bezug(
                referenz=referenz,
                kundenreferenz=kundenreferenz,
                kunde=fixtermin.kunde_kurz,
                lieferant=fixtermin.lieferant_kurz,
                spedition=spedition,
                liefertag=fixtermin.liefertag.isoformat(),
                firma_id=fixtermin.firma_id or self.firma.id,
            ),
            hinweise=hinweise,
        )

    def erinnerung_intern(self, bericht: Ueberwachungsbericht) -> EmailEntwurf:
        """Zusammenfassung der fälligen Punkte (Ampel gelb/rot) an den Anwender selbst
        (``kommunikation.absender_email``)."""
        absender = _text(self.settings.kommunikation.absender_email)
        an = [absender] if absender else []
        hinweise = [] if absender else ["Keine eigene Adresse konfiguriert (kommunikation.absender_email) – Empfänger ergänzen."]
        punkte = [
            {
                "liefertag": p.fixtermin.liefertag,
                "lieferant": p.fixtermin.lieferant_kurz,
                "kunde": p.fixtermin.kunde_kurz,
                "ampel": p.ampel,
                "grund": p.grund,
                "aktionen": list(p.aktionen),
            }
            for p in bericht.punkte
            if p.ampel in ("gelb", "rot")
        ]
        zusammenfassung_teile = []
        if bericht.entwuerfe:
            zusammenfassung_teile.append(f"{len(bericht.entwuerfe)} Entwurf/Entwürfe liegen zur Durchsicht bereit.")
        if bericht.terminaenderungen:
            zusammenfassung_teile.append(f"{len(bericht.terminaenderungen)} Terminänderung(en) warten auf Freigabe.")
        if bericht.kontaktaenderungen:
            zusammenfassung_teile.append(f"{len(bericht.kontaktaenderungen)} Kontaktänderung(en) warten auf Freigabe.")
        if bericht.fehler:
            zusammenfassung_teile.append("Fehler im Lauf: " + "; ".join(bericht.fehler))
        datum = bericht.zeitpunkt.date() if isinstance(bericht.zeitpunkt, datetime) else self._heute()
        kontext = {
            "zeitpunkt": bericht.zeitpunkt,
            "zeitraum_bis": bericht.zeitraum_bis,
            "punkte": punkte,
            "zusammenfassung": "\n".join(zusammenfassung_teile) or None,
            "hinweis": None,
        }
        kontakt = Kontakt(
            gruppe=Kontaktgruppe.INTERN,
            firma=self.firma.name,
            name=self.settings.kommunikation.absender_name or None,
            email=absender or None,
        )
        return self._entwurf(
            EntwurfTyp.ERINNERUNG_INTERN,
            vorlage="erinnerung_intern",
            kontakt=kontakt if kontakt.name else None,
            sprache="de",
            an=an,
            betreff=f"Erinnerung {datum:%d.%m.%Y}: {len(punkte)} fällige Punkte",
            kontext=kontext,
            anhaenge=[],
            bezug=_bezug(datum=datum.isoformat(), punkte=str(len(punkte)), firma_id=self.firma.id),
            hinweise=hinweise,
        )

    # -- Prüfen und speichern -----------------------------------------------------------------

    def pruefe_auftragskopie(self, entwurf: EmailEntwurf) -> list[str]:
        """Siehe :func:`pruefe_auftragskopie`."""
        return pruefe_auftragskopie(entwurf, self.settings)

    def speichere(self, entwuerfe: Iterable[EmailEntwurf], mail: MailBackend) -> list[EmailEntwurf]:
        """Legt jeden Entwurf über das Backend an (``erstelle_entwurf``; nie senden). Fehler eines Entwurfs
        werden als Hinweis am Entwurf vermerkt, die übrigen trotzdem gespeichert.

        # TODO-PRUEFEN: Am echten Postfach prüfen, dass der Graph-Entwurf im Ordner "Entwürfe" mit Anhängen und
        # HTML-Text erscheint und dass eine ``.eml`` aus dem Fake-Backend (``X-Unsent: 1``, ``Message-ID``) im
        # Outlook-Build des Anwenders per Doppelklick als bearbeitbarer Entwurf öffnet (Recherche B.5.2).
        """
        ergebnis: list[EmailEntwurf] = []
        for entwurf in entwuerfe:
            try:
                ort = mail.erstelle_entwurf(entwurf)
                if not entwurf.speicherort:
                    entwurf.speicherort = ort
                if entwurf.erstellt_am is None:
                    entwurf.erstellt_am = datetime.now().astimezone()
                logger.info("Entwurf gespeichert (%s): %s -> %s", entwurf.typ.value, entwurf.betreff, entwurf.speicherort)
            except Exception as exc:  # ein Backend-Fehler darf die übrigen Entwürfe nicht verhindern
                hinweis = f"Entwurf konnte nicht gespeichert werden: {type(exc).__name__}: {exc}"
                logger.exception("%s (%s)", hinweis, entwurf.betreff)
                entwurf.hinweise.append(hinweis)
            ergebnis.append(entwurf)
        return ergebnis


def erstelle_entwuerfe(
    paket: Belegpaket,
    ergebnis: BuchungsErgebnis | None,
    settings: Settings,
    *,
    kontakte: Zuordner | None = None,
    fixtermin: Fixtermin | None = None,
    anhaenge: Mapping[str, Path] | None = None,
) -> list[EmailEntwurf]:
    """Kurzform für die Freigabe/den Buchungslauf: Firma aus ``paket.firma_id`` (sonst ``ergebnis.firma_id``),
    dann :meth:`Entwurfsfabrik.fuer_buchung`. ``ValueError`` ohne Firma."""
    firma_id = paket.firma_id or (ergebnis.firma_id if ergebnis is not None else None)
    if not firma_id:
        raise ValueError("Belegpaket ohne Firma: Entwürfe erst nach Festlegen der Firma möglich.")
    fabrik = Entwurfsfabrik(settings, kontakte, settings.firma(firma_id))
    return fabrik.fuer_buchung(paket, ergebnis, fixtermin, anhaenge)
