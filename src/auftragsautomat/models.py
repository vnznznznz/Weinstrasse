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
    MEHRERE_LIEFERANTEN = "MEHRERE_LIEFERANTEN"  # Positionen mehrerer Weingüter -> je Weingut eine Bestellung
    FIRMA_UNKLAR = "FIRMA_UNKLAR"
    FIRMA_GELERNT = "FIRMA_GELERNT"  # Firma aus dem Firmen-Gedächtnis (Kunde + Lieferant) übernommen
    REVISION = "REVISION"  # "Revised order": ersetzt eine frühere Bestellung (Cde + AB neu)
    # Prüfung
    PREIS_ABWEICHUNG = "PREIS_ABWEICHUNG"
    NULLPREIS_OHNE_ARTIKEL = "NULLPREIS_OHNE_ARTIKEL"
    SUMME_ABWEICHUNG = "SUMME_ABWEICHUNG"
    DUBLETTE = "DUBLETTE"
    MENGE_FEHLT = "MENGE_FEHLT"
    EK_FEHLT = "EK_FEHLT"
    VK_FEHLT = "VK_FEHLT"
    BELEGFELD_FEHLT = "BELEGFELD_FEHLT"  # Spedition / Weingut / Shipment / Kunde
    # Aktionen / Rabatte
    RABATT_VORSCHLAG = "RABATT_VORSCHLAG"  # Aktionsrabatt laut Aktionszeiträumen/Rabattliste vorgeschlagen
    RABATT_ABWEICHUNG = "RABATT_ABWEICHUNG"  # Rabatt laut Dokument weicht von der Aktion ab
    RABATT_UNSICHER = "RABATT_UNSICHER"  # Aktion passt nur unscharf (Artikeltext/Kunde), Anwender entscheidet
    # Lexware / Ablage
    LEXWARE = "LEXWARE"
    ABLAGE = "ABLAGE"  # Ablage in OneDrive (Buchhaltung/Aufträge) nicht möglich


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
    rabatt_prozent: Decimal | None = None  # Rabatt laut Dokument (z. B. Aktionsrabatt); nur Hinweis, nie Buchungspreis
    rabatt_text: str | None = None  # Rohtext des Rabatts ("Aktion -10 %", "Naturalrabatt 11+1")


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
    ist_revision: bool = False  # "Revised order": ersetzt eine frühere Bestellung (-> Cde + AB neu, Kürzel "Neu")
    revision_von: str | None = None  # Bestell-/Belegnummer der ersetzten Bestellung, falls genannt
    aktionshinweis: str | None = None  # Aktionshinweis laut Dokument ("Aktion KW 45", "Promo")

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
    ANGEBOT = "AN"  # Zwischenschritt im Verkauf (Angebot -> weiterführen/wandeln -> AB)
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
# Aktionen / Rabatte (Aktionszeiträume-Excel, hochgeladene Rabattlisten-PDF)
# ---------------------------------------------------------------------------


class Aktion(BaseModel):
    """Ein Aktionszeitraum mit Rabatt, aus der Aktionszeiträume-Excel oder einer Rabattlisten-PDF.

    Trifft eine Aktion auf eine Position zu (Kunde, Lieferant/Artikel, Zeitraum), wird der Rabatt
    als ``PositionsZuordnung.rabatt_prozent`` vorgeschlagen und nach Freigabe in den Auftrag
    übernommen. Der VK bleibt Preisgruppe 1, der Rabatt ist ein Positionsrabatt in Prozent.
    """

    aktion_id: str
    bezeichnung: str | None = None  # z. B. "Herbstaktion Edeka Rhein-Ruhr"
    firma_id: str | None = None  # None = gilt in allen Firmen
    kunde_kurz: str | None = None  # Kunde / Kundengruppe / Region wie in der Excel; None = alle Kunden
    kundennummern: list[str] = Field(default_factory=list)  # optional: konkrete Lexware-Kundennummern
    lieferant_kurz: str | None = None  # Weingut / Lieferant; None = alle
    artikelnummern: list[str] = Field(default_factory=list)  # konkrete Artikel; leer = alle des Lieferanten
    artikel_text: str | None = None  # Freitext-Artikel aus der PDF ("Primitivo 0,75 l")
    von: date
    bis: date
    rabatt_prozent: Decimal | None = None
    rabatt_eur: Decimal | None = None  # alternativ: Betrag je Flasche (wird in % auf VK umgerechnet)
    aktionspreis: Decimal | None = None  # alternativ: Aktions-VK je Flasche (wird in % auf VK umgerechnet)
    bezug: Literal["liefertag", "auftragsdatum"] = "liefertag"  # welches Datum in den Zeitraum fallen muss
    quelle: str  # "excel:<Blatt>:<Zeile>" oder "pdf:<Datei>:<Seite>"
    konfidenz: float | None = None  # nur bei PDF-Extraktion
    bemerkung: str | None = None
    aktiv: bool = True

    def gilt_am(self, tag: date | None) -> bool:
        return self.aktiv and tag is not None and self.von <= tag <= self.bis


class AktionTreffer(BaseModel):
    """Eine Aktion, die auf eine Position (oder den ganzen Beleg) zutrifft."""

    aktion: Aktion
    position_laufnr: int | None = None  # None = Beleg-Ebene (alle Positionen des Lieferanten)
    rabatt_prozent: Decimal | None = None  # tatsächlich vorgeschlagener Prozentsatz (ggf. aus EUR/Aktionspreis)
    grund: str
    sicher: bool = True  # False -> Pruefhinweis RABATT_UNSICHER, Anwender entscheidet
    angewendet: bool = False  # in der Freigabe übernommen


class Rabattliste(BaseModel):
    """Ergebnis eines Rabattlisten-Uploads (PDF) in der Freigabe-UI."""

    quelldatei: Path
    extraktionsmethode: Extraktionsmethode
    aktionen: list[Aktion] = Field(default_factory=list)
    hinweise: list[Pruefhinweis] = Field(default_factory=list)
    rohtext: str | None = None
    eingelesen_am: datetime | None = None


# ---------------------------------------------------------------------------
# Firmenwahl: Katalog (Kundengruppen) + Gedächtnis (Kunde + Lieferant -> Firma)
# ---------------------------------------------------------------------------


class Kundengruppe(StrEnum):
    """Kundengruppen laut Katalog; sie entscheiden, über welche Firma ein Auftrag läuft."""

    FACHHANDEL = "fachhandel"  # Weinstrasse Agentur oder WIK FH & SEH (je nach Lieferant/Katalog)
    EIGENSTAENDIGER_LEH = "eigenstaendiger_leh"  # selbständige Einzelhändler -> WIK FH & SEH
    REGION = "region"  # Einkaufsregionen (z. B. Rhein-Ruhr, Minden) -> WIK LEH oder WOE
    KETTE = "kette"  # große Supermarktketten -> WIK LEH oder WOE
    GROSSKUNDE = "grosskunde"  # -> WIK LEH oder WOE
    UNBEKANNT = "unbekannt"


class Firmenregel(BaseModel):
    """Gelernte Zuordnung "Kunde (+ Lieferant/Produkt) -> Firma" aus der Freigabe.

    Fragt das Tool nach der Firma (z. B. Region: WIK LEH oder WOE?), merkt es sich die Antwort je
    Kunde und Lieferant; beim nächsten Auftrag dieses Kunden mit diesem Lieferanten wird sie
    übernommen (Quelle ``"gedaechtnis"``, Pruefhinweis FIRMA_GELERNT als Info).
    """

    kunde_kurz: str  # normalisierter Kundenname / Alias
    lieferant_kurz: str | None = None  # None = alle Lieferanten/Produkte dieses Kunden
    firma_id: str
    kundengruppe: Kundengruppe | None = None
    gelernt_am: datetime | None = None
    gelernt_von: str | None = None
    quelle: Literal["freigabe", "katalog", "manuell"] = "freigabe"
    bemerkung: str | None = None


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
    rabatt_prozent: Decimal | None = None  # Positionsrabatt in % (Aktion); None/0 = kein Rabatt
    rabatt_quelle: str | None = None  # "aktion:<aktion_id>", "dokument", "manuell"
    hinweise: list[Pruefhinweis] = Field(default_factory=list)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def uebersprungen(self) -> bool:
        return self.status == MatchStatus.UEBERSPRUNGEN

    @computed_field  # type: ignore[prop-decorator]
    @property
    def vk_preis_rabattiert(self) -> Decimal | None:
        """VK Preisgruppe 1 abzüglich Positionsrabatt (None, wenn kein VK)."""
        if self.vk_preis is None:
            return None
        if not self.rabatt_prozent:
            return self.vk_preis
        faktor = (Decimal(100) - self.rabatt_prozent) / Decimal(100)
        return (self.vk_preis * faktor).quantize(Decimal("0.0001"))

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
        preis = self.vk_preis_rabattiert
        if self.uebersprungen or preis is None:
            return Decimal(0)
        return (preis * self.menge).quantize(Decimal("0.01"))


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
    lieferschein_erstellen: bool = False  # nur nach Vorlage und Freigabe (und Firma.lieferschein_erlaubt)
    ist_revision: bool = False  # Revised Order: Bestellung + AB neu anlegen, Benennung "Neu" / "Neu Kopie"
    revision_von: str | None = None  # ersetzte Bestell-/Belegnummer
    aktionen: list[AktionTreffer] = Field(default_factory=list)  # zutreffende Aktionen (Anzeige in der Freigabe)
    firma_merken: bool = False  # Freigabe: gewählte Firma für Kunde + Lieferant ins Firmen-Gedächtnis schreiben
    kundengruppe: Kundengruppe | None = None  # laut Katalog ermittelt
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
    partner: str | None = None  # z. B. "87 Cantine Colosi" (Lieferant der Einkauf-Bestellung)
    pdf: Path | None = None  # erzeugte PDF (Formular laut Firma.formulare, Name laut Benennungsschema)
    nachbemerkung: str | None = None  # z. B. Bestellnummer des Kunden (WIK/WOE: in der Nachbemerkung)


class AblageErgebnis(BaseModel):
    """Ablage einer fertigen Datei in OneDrive unter Buchhaltung/Aufträge (nur nach Freigabe)."""

    datei: Path  # lokale Quelle
    kuerzel: str  # Dokumentkürzel laut Benennungsschema (Kd, Cde, Kopie, Oc, Neu, Ls, ...)
    zielordner: str  # Zielordner relativ zu "Aufträge", z. B. "WIK/2026/Cantine Colosi"
    zielname: str  # Dateiname laut Benennungsschema
    abgelegt: bool
    dry_run: bool
    speicherort: str | None = None  # Graph-Item-ID, lokaler Pfad oder None im Dry Run
    fehler: str | None = None


class BuchungsErgebnis(BaseModel):
    firma_id: str
    quelldatei: Path
    dry_run: bool
    zeitpunkt: datetime
    angebot: Belegergebnis | None = None  # Zwischenschritt Angebot (Weg "angebot_weiterfuehren"/"angebot_wandeln")
    einkauf: Belegergebnis | None = None
    verkauf: Belegergebnis | None = None
    lieferschein: Belegergebnis | None = None
    warnungen: list[str] = Field(default_factory=list)
    # Weitere Einkauf-Bestellungen, wenn die Positionen zu mehreren Weingütern gehören
    # (je Weingut eine eigene Bestellung; ``einkauf`` ist die des Hauptlieferanten).
    einkauf_weitere: list[Belegergebnis] = Field(default_factory=list)
    protokoll: Path | None = None  # geschriebene protokoll.csv
    archiv: Path | None = None  # Zielpfad der archivierten Quelldatei (nur scharf und erfolgreich)
    pdfs: dict[str, Path] = Field(default_factory=dict)  # erzeugte PDFs je Dokumentkürzel ("Cde", "Kopie", "Ls", ...)
    ablage: list[AblageErgebnis] = Field(default_factory=list)  # Ablage in OneDrive Buchhaltung/Aufträge
    ist_revision: bool = False

    def alle_einkaeufe(self) -> list[Belegergebnis]:
        """Einkauf-Bestellung des Hauptlieferanten plus die weiteren (Reihenfolge wie angelegt)."""
        return ([self.einkauf] if self.einkauf is not None else []) + list(self.einkauf_weitere)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def erfolgreich(self) -> bool:
        teile = [t for t in (self.angebot, self.einkauf, self.verkauf, self.lieferschein) if t is not None]
        teile.extend(self.einkauf_weitere)
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
    "angebot",
    "auftragskopie",
    "auftragsbestaetigung_lieferant",  # Oc: Bestätigung des Weinguts
    "revision",  # Revised Order (Neu / Neu Kopie)
    "speditionsauftrag",
    "speditionsbestaetigung",
    "lieferschein",
    "reklamation",
    "rabattliste",
    "mail",
    "fixtermin",
    "index",
    "gesamt_pdf",
    "sonstiges",
]

#: Dokumentkürzel laut Benennungsschema ("Titulierung") – sie stehen im Dateinamen jedes Belegs.
Dokumentkuerzel = Literal["Kd", "Cde", "Kopie", "Oc", "Neu", "Neu Kopie", "Rekl", "Ls", "Speditionsauftrag", "Speditionsbestätigung"]

DOKUMENTKUERZEL: dict[str, str] = {
    "Kd": "Kundenbestellung (Eingang vom Kunden)",
    "Cde": "Bestellung an den Lieferanten (Commande)",
    "Kopie": "Auftragskopie an den Kunden (nie 'Auftragsbestätigung')",
    "Oc": "Auftragsbestätigung des Lieferanten (Order confirmation)",
    "Neu": "Revised Order: neue Bestellung an den Lieferanten",
    "Neu Kopie": "Revised Order: neue Auftragskopie an den Kunden",
    "Rekl": "Reklamation",
    "Ls": "Lieferschein",
    "Speditionsauftrag": "Auftrag an die Spedition",
    "Speditionsbestätigung": "Bestätigung der Spedition",
}

#: Zuordnung Dokumentkürzel -> Dossier-Dateityp
KUERZEL_DATEITYP: dict[str, DossierDateityp] = {
    "Kd": "kundenbestellung",
    "Cde": "bestellung",
    "Kopie": "auftragskopie",
    "Oc": "auftragsbestaetigung_lieferant",
    "Neu": "revision",
    "Neu Kopie": "revision",
    "Rekl": "reklamation",
    "Ls": "lieferschein",
    "Speditionsauftrag": "speditionsauftrag",
    "Speditionsbestätigung": "speditionsbestaetigung",
}


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
