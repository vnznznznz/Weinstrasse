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
    # Meldungen (Intrastat / Schaumweinsteuer / Verpackung)
    MELDEDATEN_FEHLEN = "MELDEDATEN_FEHLEN"  # Artikel ohne Warennummer, Volumen, Alkohol, Verpackungsprofil
    MELDUNG_SCHWELLE = "MELDUNG_SCHWELLE"  # Intrastat-Schwelle / Meldepflicht ungeklärt
    MELDUNG_GUTSCHRIFT = "MELDUNG_GUTSCHRIFT"  # Rücksendung/Gutschrift: gesondert prüfen (AdG 21)
    MELDUNG_PRUEFEN = "MELDUNG_PRUEFEN"  # fachlich zu prüfender Einzelfall
    MELDUNG_FRIST = "MELDUNG_FRIST"  # Frist überschritten oder nah


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


class Lieferant(BaseModel):
    firma_id: str
    lieferantennummer: str
    name: str
    name2: str | None = None
    ort: str | None = None
    matchcode: str | None = None
    aktiv: bool = True


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
