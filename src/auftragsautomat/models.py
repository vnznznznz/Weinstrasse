"""Gemeinsame Datenmodelle des Auftrags-Automaten.

Diese Modelle sind der Vertrag zwischen allen Teilmodulen:

    Extraktion  ->  RohBestellung
    Matching    ->  Belegpaket (RohBestellung + Zuordnungen + Prüfhinweise)
    Freigabe    ->  Belegpaket (vom Anwender bestätigt / korrigiert)
    Lexware     ->  BuchungsErgebnis

Alle Geldbeträge sind Decimal (netto, EUR), Mengen sind Decimal (Flaschen),
Volumen in Zentiliter (cl). Standardvolumen ist 75 cl.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, computed_field

STANDARD_VOLUMEN_CL = Decimal("75")


# ---------------------------------------------------------------------------
# Eingang / Extraktion
# ---------------------------------------------------------------------------


class Eingangstyp(StrEnum):
    """Die drei Eingangsarten aus der Spezifikation."""

    KUNDENBESTELLUNG_PDF = "A"  # Text-PDF mit Kundenbestellung
    EIGENE_BESTELLUNG_EMAIL = "B"  # Eigene Bestellung an das Weingut per E-Mail
    PREISLISTE_SCAN = "C"  # Eingescannte Preisliste mit handschriftlichen Mengen


class Extraktionsmethode(StrEnum):
    PDFPLUMBER = "pdfplumber"
    CLAUDE_VISION = "claude_vision"
    EMAIL = "email"
    MANUELL = "manuell"


class Schweregrad(StrEnum):
    INFO = "info"
    WARNUNG = "warnung"  # rot markieren, Buchung trotzdem möglich nach Bestätigung
    FEHLER = "fehler"  # blockiert die Buchung bis zur Korrektur


class Pruefcode(StrEnum):
    """Stabile Codes für Prüfhinweise (werden im Log/CSV ausgegeben)."""

    # Extraktion
    KONFIDENZ_NIEDRIG = "KONFIDENZ_NIEDRIG"
    DURCHGESTRICHEN = "DURCHGESTRICHEN"
    SUMMENVERMERK = "SUMMENVERMERK"
    FELD_FEHLT = "FELD_FEHLT"
    # Matching
    KEIN_ARTIKEL = "KEIN_ARTIKEL"
    ARTIKEL_UNSICHER = "ARTIKEL_UNSICHER"
    VARIANTE_MANUELL = "VARIANTE_MANUELL"  # 37,5 cl / 1,5 l / Holzkiste
    KEIN_KUNDE = "KEIN_KUNDE"
    KUNDE_UNSICHER = "KUNDE_UNSICHER"
    KEIN_LIEFERANT = "KEIN_LIEFERANT"
    LIEFERANT_UNSICHER = "LIEFERANT_UNSICHER"
    FIRMA_UNKLAR = "FIRMA_UNKLAR"
    # Prüfung
    PREIS_ABWEICHUNG = "PREIS_ABWEICHUNG"
    NULLPREIS_OHNE_ARTIKEL = "NULLPREIS_OHNE_ARTIKEL"
    SUMME_ABWEICHUNG = "SUMME_ABWEICHUNG"
    DUBLETTE = "DUBLETTE"
    MENGE_FEHLT = "MENGE_FEHLT"
    EK_FEHLT = "EK_FEHLT"
    VK_FEHLT = "VK_FEHLT"
    BELEGFELD_FEHLT = "BELEGFELD_FEHLT"  # Spedition / Weingut / Shipment / Kunde
    # Lexware
    LEXWARE = "LEXWARE"


class Pruefhinweis(BaseModel):
    code: Pruefcode
    schweregrad: Schweregrad
    text: str
    position_laufnr: int | None = None  # None = betrifft den ganzen Beleg
    details: dict[str, str] = Field(default_factory=dict)


class Summenvermerk(BaseModel):
    """Handschriftlicher oder gedruckter Summenvermerk, z. B. "180 R" oder "Gesamt 72 Fl."."""

    wert: Decimal
    einheit: str | None = None
    rohtext: str
    konfidenz: float | None = None


class RohPosition(BaseModel):
    """Eine Position so, wie sie aus dem Dokument gelesen wurde (noch ohne Zuordnung)."""

    laufnr: int
    bezeichnung: str
    jahrgang: int | None = None
    menge: Decimal | None = None
    einheit: str | None = None  # z. B. "Fl", "Kt", "Stk"
    preis: Decimal | None = None  # Einzelpreis netto laut Dokument
    volumen_cl: Decimal | None = None  # nur wenn im Dokument angegeben
    artikelnummer: str | None = None  # falls das Dokument eine Artikelnummer nennt
    hinweis: str | None = None  # z. B. "11+1"
    lieferadresse: str | None = None  # abweichende Lieferadresse je Position
    durchgestrichen: bool = False
    konfidenz_menge: float | None = None  # 0..1, nur bei Vision
    konfidenz_preis: float | None = None
    rohtext: str | None = None
    # Verpackungseinheiten (LEH-Bestellungen: "570 VE à 6" -> menge = 3420 Flaschen)
    menge_ve: Decimal | None = None  # bestellte Verpackungseinheiten laut Dokument
    ve_inhalt: Decimal | None = None  # Flaschen je Verpackungseinheit
    kundenartikelnummer: str | None = None  # Artikelnummer des Kunden (z. B. SPAR, Rheinberg)
    gtin: str | None = None


class RohBestellung(BaseModel):
    eingangstyp: Eingangstyp
    quelldatei: Path
    extraktionsmethode: Extraktionsmethode
    kunde_text: str | None = None  # Kundenname wie im Dokument
    kunde_adresse: str | None = None
    lieferant_text: str | None = None  # Weingut / Lieferant wie im Dokument
    belegnummer: str | None = None  # Belegnummer des Dokuments
    bestellnummer: str | None = None  # Kundenbestellnummer
    datum: date | None = None
    liefertag: date | None = None
    lieferadresse: str | None = None
    positionen: list[RohPosition] = Field(default_factory=list)
    summenvermerke: list[Summenvermerk] = Field(default_factory=list)
    hinweise: list[Pruefhinweis] = Field(default_factory=list)
    rohtext: str | None = None
    seiten: int | None = None
    kundennummer_dokument: str | None = None  # im Dokument genannte Kundennummer ("Kd-Nr. 3441")
    empfaenger_firma_text: str | None = None  # an welche unserer Firmen das Dokument adressiert ist
    ladeadresse: str | None = None  # Abholadresse beim Weingut (LEH-Bestellungen)
    lieferbedingung: str | None = None  # z. B. "Frei Haus", "ex cellar"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def gesamtmenge(self) -> Decimal:
        return sum((p.menge or Decimal(0)) for p in self.positionen if not p.durchgestrichen) or Decimal(0)


# ---------------------------------------------------------------------------
# Stammdaten (aus Lexware, nur lesend)
# ---------------------------------------------------------------------------


class Artikel(BaseModel):
    firma_id: str
    artikelnummer: str
    bezeichnung: str
    warengruppe: str | None = None  # entspricht dem Lieferanten / Weingut
    lieferantennummer: str | None = None
    volumen_cl: Decimal | None = None  # None = unbekannt, wird aus der Bezeichnung abgeleitet
    holzkiste: bool = False
    jahrgang: int | None = None
    ek_preis: Decimal | None = None  # Einkaufspreis netto
    vk_preis_1: Decimal | None = None  # Verkaufspreis Preisgruppe 1 netto
    einheit: str | None = None
    matchcode: str | None = None
    aktiv: bool = True

    @computed_field  # type: ignore[prop-decorator]
    @property
    def ist_sondervariante(self) -> bool:
        """37,5 cl, 1,5 l und Holzkisten dürfen NIE automatisch gewählt werden."""
        if self.holzkiste:
            return True
        if self.volumen_cl is None:
            return False
        return self.volumen_cl != STANDARD_VOLUMEN_CL


class Kunde(BaseModel):
    firma_id: str
    kundennummer: str
    name: str
    name2: str | None = None
    strasse: str | None = None
    plz: str | None = None
    ort: str | None = None
    land: str | None = None
    preisgruppe: int | None = None
    matchcode: str | None = None
    aktiv: bool = True
    email: str | None = None
    bemerkung: str | None = None  # Lexware-Bemerkung (enthält oft Dispo-Hinweise)
    ust_id: str | None = None


class Lieferant(BaseModel):
    firma_id: str
    lieferantennummer: str
    name: str
    name2: str | None = None
    ort: str | None = None
    land: str | None = None
    matchcode: str | None = None
    aktiv: bool = True
    email: str | None = None
    bemerkung: str | None = None


class Belegart(StrEnum):
    AUFTRAGSBESTAETIGUNG = "AB"
    LIEFERSCHEIN = "LS"
    RECHNUNG = "RE"
    BESTELLUNG = "BE"  # Einkauf
    SONSTIGE = "SONST"


class Beleg(BaseModel):
    """Bereits in Lexware vorhandener Beleg (für die Dublettenprüfung)."""

    firma_id: str
    belegart: Belegart
    belegnummer: str
    kundenbestellnummer: str | None = None
    partnernummer: str | None = None  # Kunden- oder Lieferantennummer
    datum: date | None = None
    netto: Decimal | None = None


# ---------------------------------------------------------------------------
# Matching / Zuordnung
# ---------------------------------------------------------------------------


class MatchStatus(StrEnum):
    SICHER = "sicher"
    UNSICHER = "unsicher"  # Kandidaten vorhanden, Anwender muss wählen
    KEIN_TREFFER = "kein_treffer"
    UEBERSPRUNGEN = "uebersprungen"  # z. B. 0,00 € ohne Stammartikel
    MANUELL = "manuell"  # vom Anwender in der Freigabe gesetzt


class ArtikelKandidat(BaseModel):
    artikel: Artikel
    score: float  # 0..100
    grund: str
    manuell_erforderlich: bool = False  # Sondervariante: nie automatisch


class KundeKandidat(BaseModel):
    kunde: Kunde
    score: float
    grund: str


class LieferantKandidat(BaseModel):
    lieferant: Lieferant
    score: float
    grund: str


class KundeZuordnung(BaseModel):
    kunde: Kunde | None = None
    kandidaten: list[KundeKandidat] = Field(default_factory=list)
    status: MatchStatus = MatchStatus.KEIN_TREFFER
    quelle: str | None = None  # "alias", "fuzzy", "manuell"


class LieferantZuordnung(BaseModel):
    lieferant: Lieferant | None = None
    kandidaten: list[LieferantKandidat] = Field(default_factory=list)
    status: MatchStatus = MatchStatus.KEIN_TREFFER
    quelle: str | None = None


class PositionsZuordnung(BaseModel):
    roh: RohPosition
    artikel: Artikel | None = None
    kandidaten: list[ArtikelKandidat] = Field(default_factory=list)
    status: MatchStatus = MatchStatus.KEIN_TREFFER
    quelle: str | None = None
    menge: Decimal = Decimal(0)  # ggf. in der Freigabe korrigiert
    ek_preis: Decimal | None = None  # aus Lexware (Artikelstamm)
    vk_preis: Decimal | None = None  # aus Lexware (Preisgruppe 1)
    preis_dokument: Decimal | None = None  # laut PDF, NIE als Buchungspreis verwenden
    hinweise: list[Pruefhinweis] = Field(default_factory=list)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def uebersprungen(self) -> bool:
        return self.status == MatchStatus.UEBERSPRUNGEN

    @computed_field  # type: ignore[prop-decorator]
    @property
    def preis_abweichung(self) -> Decimal | None:
        """VK laut Lexware minus Preis laut Dokument (None, wenn eines fehlt)."""
        if self.vk_preis is None or self.preis_dokument is None:
            return None
        return self.vk_preis - self.preis_dokument

    @computed_field  # type: ignore[prop-decorator]
    @property
    def netto_ek(self) -> Decimal:
        if self.uebersprungen or self.ek_preis is None:
            return Decimal(0)
        return (self.ek_preis * self.menge).quantize(Decimal("0.01"))

    @computed_field  # type: ignore[prop-decorator]
    @property
    def netto_vk(self) -> Decimal:
        if self.uebersprungen or self.vk_preis is None:
            return Decimal(0)
        return (self.vk_preis * self.menge).quantize(Decimal("0.01"))


class Belegfelder(BaseModel):
    """Felder, die in Lexware bei jedem Beleg ausgefüllt sein müssen
    (Spedition, Weingut, Shipment, Kunde)."""

    spedition: str | None = None
    weingut: str | None = None
    shipment: str | None = None
    kunde: str | None = None

    def fehlende(self) -> list[str]:
        return [name for name, wert in self.model_dump().items() if not wert]


class Belegpaket(BaseModel):
    """Ergebnis von Matching + Prüfung; Eingabe für Freigabe und Lexware-Schreiben."""

    roh: RohBestellung
    firma_id: str | None = None
    firma_quelle: str | None = None  # "alias", "stammdaten", "manuell", "eingang_ordner"
    kunde: KundeZuordnung = Field(default_factory=KundeZuordnung)
    lieferant: LieferantZuordnung = Field(default_factory=LieferantZuordnung)
    positionen: list[PositionsZuordnung] = Field(default_factory=list)
    belegfelder: Belegfelder = Field(default_factory=Belegfelder)
    hinweise: list[Pruefhinweis] = Field(default_factory=list)  # Belegebene
    einkauf_erforderlich: bool = True  # False bei Weinstrasse Agentur
    lieferschein_erstellen: bool = False  # nur nach Vorlage und Freigabe
    freigegeben: bool = False
    freigegeben_am: datetime | None = None
    freigegeben_von: str | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def alle_hinweise(self) -> list[Pruefhinweis]:
        alle = list(self.roh.hinweise) + list(self.hinweise)
        for p in self.positionen:
            alle.extend(p.hinweise)
        return alle

    @computed_field  # type: ignore[prop-decorator]
    @property
    def blockiert(self) -> bool:
        return any(h.schweregrad == Schweregrad.FEHLER for h in self.alle_hinweise)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def gesamtmenge(self) -> Decimal:
        return sum((p.menge for p in self.positionen if not p.uebersprungen), Decimal(0))

    @computed_field  # type: ignore[prop-decorator]
    @property
    def summe_netto_ek(self) -> Decimal:
        return sum((p.netto_ek for p in self.positionen), Decimal(0))

    @computed_field  # type: ignore[prop-decorator]
    @property
    def summe_netto_vk(self) -> Decimal:
        return sum((p.netto_vk for p in self.positionen), Decimal(0))

    def aktive_positionen(self) -> list[PositionsZuordnung]:
        return [p for p in self.positionen if not p.uebersprungen]


# ---------------------------------------------------------------------------
# Lexware-Schreiben / Protokoll
# ---------------------------------------------------------------------------


class Belegergebnis(BaseModel):
    belegart: Belegart
    belegnummer: str | None = None  # None im Dry Run
    netto: Decimal | None = None
    brutto: Decimal | None = None
    erfolgreich: bool
    dry_run: bool
    methode: Literal["opentrans", "ui_automation", "dry_run", "keine"] = "keine"
    fehler: str | None = None
    warnungen: list[str] = Field(default_factory=list)
    artefakt: Path | None = None  # z. B. erzeugte openTRANS-XML


class BuchungsErgebnis(BaseModel):
    firma_id: str
    quelldatei: Path
    dry_run: bool
    zeitpunkt: datetime
    einkauf: Belegergebnis | None = None
    verkauf: Belegergebnis | None = None
    lieferschein: Belegergebnis | None = None
    warnungen: list[str] = Field(default_factory=list)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def erfolgreich(self) -> bool:
        teile = [t for t in (self.einkauf, self.verkauf, self.lieferschein) if t is not None]
        return bool(teile) and all(t.erfolgreich for t in teile)


# ---------------------------------------------------------------------------
# Ausbaustufe 2: Fixtermine, Kontakte, Kommunikation, Tracking, Dossiers
# (Design: docs/AUSBAUSTUFE_2.md)
# ---------------------------------------------------------------------------


class Kontaktgruppe(StrEnum):
    LIEFERANT = "lieferant"
    SPEDITION = "spedition"
    KUNDE = "kunde"
    INTERN = "intern"


class Kontakt(BaseModel):
    """Eintrag aus der Kontaktliste (Ansprechpartner Abwicklung)."""

    gruppe: Kontaktgruppe
    firma: str  # Firmenname wie in der Kontaktliste
    name: str | None = None
    position: str | None = None  # z. B. "Orders", "Dispo", "Postfach – Neue Aufträge"
    email: str | None = None
    telefon: str | None = None
    produkt: str | None = None  # Kunden: zuständig für dieses Produkt / diesen Lieferanten
    spedition: str | None = None  # Kunden: Standard-Spedition
    land: str | None = None
    lexware_nummer: str | None = None  # zugeordnete Kunden-/Lieferantennummer
    sprache: Literal["de", "en"] = "de"
    anrede: str | None = None  # "Frau", "Herr", "Dear Ms", ...
    quelle: str | None = None  # Blatt/Zeile der Kontaktliste


class Fixtermin(BaseModel):
    """Eine Zeile der Fixterminüberwachung (verbindlicher Liefertermin)."""

    firma_id: str | None = None
    lieferant_kurz: str
    kunde_kurz: str
    liefertag: date
    auftragsdatum: date | None = None
    verladedatum: date | None = None
    lieferant_bestaetigt: bool = False
    spedition_bestaetigt: bool = False
    abgeholt: bool = False
    bemerkung: str | None = None
    bestellnummer: str | None = None
    kundenbestellnummer: str | None = None
    ab_nummer: str | None = None
    spedition: str | None = None
    tracking_referenz: str | None = None
    zeile: int | None = None  # Excel-Zeile (1-basiert); None = noch nicht eingetragen
    quelle: str | None = None


class SendungStatus(StrEnum):
    UNBEKANNT = "unbekannt"
    BEAUFTRAGT = "beauftragt"
    BESTAETIGT = "bestaetigt"
    ABHOLUNG_GEPLANT = "abholung_geplant"
    ABGEHOLT = "abgeholt"
    UNTERWEGS = "unterwegs"
    ZUSTELLUNG_GEPLANT = "zustellung_geplant"
    ZUGESTELLT = "zugestellt"
    VERSPAETET = "verspaetet"
    PROBLEM = "problem"


class Meilenstein(BaseModel):
    code: str
    text: str
    zeitpunkt: datetime | None = None
    ort: str | None = None
    geplant: bool = False  # True = geplanter, False = eingetretener Meilenstein


class Sendung(BaseModel):
    spedition: str
    referenz: str | None = None  # Referenz der Spedition (Hillebrand "Our Ref")
    kundenreferenz: str | None = None  # unsere Referenz ("Your Ref")
    status: SendungStatus = SendungStatus.UNBEKANNT
    puenktlich: bool | None = None
    geplante_abholung: date | None = None
    geplante_zustellung: date | None = None
    letzte_pruefung: datetime | None = None
    quelle: Literal["mail", "api", "public_tracking", "portal", "manuell", "keine"] = "keine"
    meilensteine: list[Meilenstein] = Field(default_factory=list)
    hinweise: list[str] = Field(default_factory=list)


class EntwurfTyp(StrEnum):
    BESTELLUNG_WEINGUT = "bestellung_weingut"
    AUFTRAGSKOPIE_KUNDE = "auftragskopie_kunde"  # nie "Auftragsbestätigung" (Haftung)
    SPEDITION_AUFTRAG = "spedition_auftrag"
    LIEFERSCHEIN = "lieferschein"
    NACHFRAGE_LIEFERANT = "nachfrage_lieferant"
    NACHFRAGE_SPEDITION = "nachfrage_spedition"
    ERINNERUNG_INTERN = "erinnerung_intern"


class EmailEntwurf(BaseModel):
    """Ein E-Mail-Entwurf. Wird gespeichert, NIE gesendet."""

    typ: EntwurfTyp
    an: list[str]
    betreff: str
    text_html: str
    text_plain: str
    cc: list[str] = Field(default_factory=list)
    anhaenge: list[Path] = Field(default_factory=list)
    absender: str | None = None
    bezug: dict[str, str] = Field(default_factory=dict)  # bestellnummer, kunde, lieferant, referenz
    erstellt_am: datetime | None = None
    speicherort: str | None = None  # Graph-Nachrichten-ID, Outlook-EntryID oder .eml-Pfad
    hinweise: list[str] = Field(default_factory=list)


class Ereignistyp(StrEnum):
    SPEDITION_BESTAETIGUNG = "spedition_bestaetigung"
    LIEFERANT_BESTAETIGUNG = "lieferant_bestaetigung"
    TERMINAENDERUNG = "terminaenderung"
    ABHOLUNG = "abholung"
    ZUSTELLUNG = "zustellung"
    KONTAKTAENDERUNG = "kontaktaenderung"
    SONSTIGES = "sonstiges"


class MailEreignis(BaseModel):
    """Aus einer eingehenden Mail erkanntes Ereignis."""

    typ: Ereignistyp
    absender: str
    betreff: str
    empfangen: datetime
    referenz: str | None = None  # z. B. Hillebrand "Our Ref"
    kundenreferenz: str | None = None  # z. B. "Your Ref" / Bestellnummer
    neues_datum: date | None = None
    text_auszug: str | None = None
    mail_id: str | None = None
    konfidenz: float = 1.0


class Terminaenderung(BaseModel):
    """Vorschlag, einen Fixtermin zu verschieben (Übernahme erst nach Freigabe)."""

    fixtermin: Fixtermin
    neues_datum: date
    grund: str
    quelle: MailEreignis | None = None
    uebernommen: bool = False


class KontaktAenderung(BaseModel):
    """Vorschlag, einen Kontakt zu ändern (Übernahme erst nach Freigabe)."""

    kontakt: Kontakt | None = None
    feld: str
    alt: str | None = None
    neu: str
    quelle: str
    uebernommen: bool = False


DossierDateityp = Literal[
    "deckblatt",
    "kundenbestellung",
    "bestellung",
    "auftragskopie",
    "speditionsauftrag",
    "lieferschein",
    "mail",
    "fixtermin",
    "index",
    "gesamt_pdf",
    "sonstiges",
]


class DossierDatei(BaseModel):
    name: str
    typ: DossierDateityp
    quelle: str | None = None
    hinzugefuegt: datetime | None = None
    groesse: int | None = None


class Dossier(BaseModel):
    """Vorgangsmappe im Ordner "vorangefertigte Dossiers" (ein Unterordner je Auftrag)."""

    name: str
    ordner: str  # lokaler Pfad oder OneDrive-/SharePoint-Pfad
    firma_id: str | None = None
    kunde_kurz: str | None = None
    lieferant_kurz: str | None = None
    bestellnummer: str | None = None
    kundenbestellnummer: str | None = None
    ab_nummer: str | None = None
    lieferschein_nummer: str | None = None
    fixtermin: date | None = None
    spedition: str | None = None
    tracking_referenz: str | None = None
    dateien: list[DossierDatei] = Field(default_factory=list)
    checkliste: dict[str, bool] = Field(default_factory=dict)
    offene_punkte: list[str] = Field(default_factory=list)
    vorgeschlagener_zielordner: str | None = None
    erstellt_am: datetime | None = None
    aktualisiert_am: datetime | None = None


Ampel = Literal["gruen", "gelb", "rot", "grau"]


class UeberwachungsPunkt(BaseModel):
    fixtermin: Fixtermin
    ampel: Ampel
    grund: str
    aktionen: list[str] = Field(default_factory=list)
    sendung: Sendung | None = None


class Ueberwachungsbericht(BaseModel):
    zeitpunkt: datetime
    zeitraum_bis: date
    punkte: list[UeberwachungsPunkt] = Field(default_factory=list)
    entwuerfe: list[EmailEntwurf] = Field(default_factory=list)
    terminaenderungen: list[Terminaenderung] = Field(default_factory=list)
    kontaktaenderungen: list[KontaktAenderung] = Field(default_factory=list)
    fehler: list[str] = Field(default_factory=list)
