"""Datenmodelle der Meldungen (Intrastat, Schaumweinsteuer, Verpackung)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field, computed_field

from auftragsautomat.models import Pruefhinweis


class Meldemonat(BaseModel):
    jahr: int
    monat: int = Field(ge=1, le=12)

    @classmethod
    def parse(cls, text: str) -> Meldemonat:
        """'2026-10' oder '10/2026' oder '10.2026'."""
        t = text.strip().replace("/", "-").replace(".", "-")
        a, b = t.split("-")
        if len(a) == 4:
            return cls(jahr=int(a), monat=int(b))
        return cls(jahr=int(b), monat=int(a))

    @property
    def erster_tag(self) -> date:
        return date(self.jahr, self.monat, 1)

    @property
    def letzter_tag(self) -> date:
        folge = self.folgemonat()
        return date(folge.jahr, folge.monat, 1).fromordinal(date(folge.jahr, folge.monat, 1).toordinal() - 1)

    def folgemonat(self, n: int = 1) -> Meldemonat:
        idx = self.jahr * 12 + (self.monat - 1) + n
        return Meldemonat(jahr=idx // 12, monat=idx % 12 + 1)

    def enthaelt(self, tag: date | None) -> bool:
        return tag is not None and tag.year == self.jahr and tag.month == self.monat

    def __str__(self) -> str:
        return f"{self.jahr:04d}-{self.monat:02d}"


class Richtung(StrEnum):
    EINGANG = "eingang"  # Wareneingang aus anderem EU-Mitgliedstaat
    VERSENDUNG = "versendung"  # Lieferung in anderen EU-Mitgliedstaat
    INLAND = "inland"  # deutscher Partner, nicht Intrastat-relevant
    DRITTLAND = "drittland"  # außerhalb der EU (Zoll, nicht Intrastat)


class Belegtyp(StrEnum):
    EINKAUF = "einkauf"  # Eingangsrechnung / Wareneingang
    VERKAUF = "verkauf"  # Ausgangsrechnung / Lieferschein
    GUTSCHRIFT_EINKAUF = "gutschrift_einkauf"
    GUTSCHRIFT_VERKAUF = "gutschrift_verkauf"


class Erzeugnisart(StrEnum):
    """Verbrauchsteuerliche Einordnung (SchaumwZwStG)."""

    STILLWEIN = "stillwein"  # Weinsteuer: Satz 0, keine Steueranmeldung
    PERLWEIN = "perlwein"  # Frizzante <= 2,5 bar ohne Schaumweinstopfen: nicht Schaumwein
    SCHAUMWEIN = "schaumwein"  # >= 3 bar oder Schaumweinstopfen (§ 1 Abs. 2 SchaumwZwStG)
    ZWISCHENERZEUGNIS = "zwischenerzeugnis"  # z. B. Portwein, Sherry, Marsala
    SONSTIGES = "sonstiges"  # Gläser, Werbemittel ... keine Verbrauchsteuer


class Material(StrEnum):
    """Materialarten exakt wie im LUCID-Datenmeldeformular (Stand August 2026)."""

    GLAS = "Glas"
    PPK = "PPK"
    EISENMETALLE = "Eisenmetalle"
    ALUMINIUM = "Aluminium"
    GETRAENKEKARTON = "Getränkekartonverpackungen"
    SONSTIGE_VERBUNDE = "Sonstige Verbundverpackungen"
    KUNSTSTOFFE = "Kunststoffe"
    SONSTIGE_MATERIALIEN = "Sonstige Materialien"


EU_MITGLIEDSTAATEN: frozenset[str] = frozenset(
    "AT BE BG CY CZ DE DK EE ES FI FR GR HR HU IE IT LT LU LV MT NL PL PT RO SE SI SK".split()
)

BUNDESLAENDER: dict[str, str] = {
    "01": "Schleswig-Holstein",
    "02": "Hamburg",
    "03": "Niedersachsen",
    "04": "Bremen",
    "05": "Nordrhein-Westfalen",
    "06": "Hessen",
    "07": "Rheinland-Pfalz",
    "08": "Baden-Württemberg",
    "09": "Bayern",
    "10": "Saarland",
    "11": "Berlin",
    "12": "Brandenburg",
    "13": "Mecklenburg-Vorpommern",
    "14": "Sachsen",
    "15": "Sachsen-Anhalt",
    "16": "Thüringen",
}

VERKEHRSZWEIGE: dict[str, str] = {
    "1": "Seeverkehr",
    "2": "Eisenbahnverkehr",
    "3": "Straßenverkehr",
    "4": "Luftverkehr",
    "5": "Postsendungen",
    "7": "Stationäre Transporteinrichtungen",
    "8": "Binnenschifffahrt",
    "9": "Eigener Antrieb",
}

ARTEN_DES_GESCHAEFTS: dict[str, str] = {
    "11": "Endgültiger Kauf/Verkauf (ausgenommen direkter Handel mit privaten Verbrauchern)",
    "12": "Direkter Handel mit/durch private Verbraucher (einschl. Fernverkauf)",
    "21": "Rücksendung von Waren",
    "22": "Ersatz für zurückgesandte Waren",
    "23": "Ersatz für nicht zurückgesandte Waren",
    "31": "Beförderung in/aus ein Lager",
    "32": "Ansichts-/Probesendung, Konsignations-/Kommissionsgeschäft",
    "33": "Finanzierungsleasing",
    "34": "Eigentumsübertragung ohne finanzielle Gegenleistung",
    "99": "Sonstige Warenverkehre",
}


# ---------------------------------------------------------------------------
# Eingabedaten
# ---------------------------------------------------------------------------


class Bewegung(BaseModel):
    """Eine Belegposition aus dem Lexware-Export (Eingangs- oder Ausgangsrechnung)."""

    firma_id: str
    belegtyp: Belegtyp
    belegnummer: str
    belegdatum: date
    lieferdatum: date | None = None  # physische Warenbewegung; fehlt -> Belegdatum
    partnernummer: str | None = None
    partnername: str | None = None
    partner_land: str = "DE"  # ISO-2
    partner_ustid: str | None = None
    artikelnummer: str
    bezeichnung: str | None = None
    menge: Decimal  # Flaschen (negativ bei Gutschrift)
    netto_eur: Decimal  # Positionsnetto in EUR (negativ bei Gutschrift)
    fracht_eur: Decimal = Decimal(0)  # separat berechnete Fracht, anteilig je Position
    quelldatei: str | None = None
    zeile: int | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def bewegungsdatum(self) -> date:
        return self.lieferdatum or self.belegdatum

    @computed_field  # type: ignore[prop-decorator]
    @property
    def richtung(self) -> Richtung:
        land = (self.partner_land or "DE").upper()
        if land == "DE":
            return Richtung.INLAND
        if land not in EU_MITGLIEDSTAATEN:
            return Richtung.DRITTLAND
        if self.belegtyp in (Belegtyp.EINKAUF, Belegtyp.GUTSCHRIFT_EINKAUF):
            return Richtung.EINGANG
        return Richtung.VERSENDUNG

    @computed_field  # type: ignore[prop-decorator]
    @property
    def ist_gutschrift(self) -> bool:
        return self.belegtyp in (Belegtyp.GUTSCHRIFT_EINKAUF, Belegtyp.GUTSCHRIFT_VERKAUF) or self.menge < 0


class VerpackungsKomponente(BaseModel):
    bezeichnung: str  # z. B. "Flasche", "Etikett", "Kapsel"
    material: Material
    gewicht_g: Decimal
    systembeteiligungspflichtig: bool = True  # False: Transportverpackung (B2B), nicht an LUCID melden


class VerpackungsProfil(BaseModel):
    id: str
    beschreibung: str | None = None
    komponenten: list[VerpackungsKomponente] = Field(default_factory=list)

    def gewicht_je_material_g(
        self, *, nur_systembeteiligungspflichtig: bool = True
    ) -> dict[Material, Decimal]:
        summe: dict[Material, Decimal] = {}
        for k in self.komponenten:
            if nur_systembeteiligungspflichtig and not k.systembeteiligungspflichtig:
                continue
            summe[k.material] = summe.get(k.material, Decimal(0)) + k.gewicht_g
        return summe


class ArtikelMeldedaten(BaseModel):
    """Pflegedaten je Artikel, die Lexware nicht kennt (config/meldungen/artikel_meldedaten.csv)."""

    firma_id: str | None = None  # None = gilt in allen Firmen
    artikelnummer: str
    warennummer: str | None = None  # 8-stellige KN-Warennummer, z. B. 22041015 (Prosecco)
    ursprungsland: str | None = None  # ISO-2
    volumen_cl: Decimal | None = None
    alkohol_vol: Decimal | None = None
    erzeugnisart: Erzeugnisart = Erzeugnisart.STILLWEIN
    ueberdruck_bar: Decimal | None = None
    schaumweinstopfen: bool = False
    dichte_kg_je_l: Decimal | None = None  # Standard aus der Konfiguration
    verpackung_profil: str | None = None
    einheit_je_position: Decimal = Decimal(1)  # Flaschen je Lexware-Mengeneinheit (Karton 6 -> 6)


# ---------------------------------------------------------------------------
# Ergebnisse
# ---------------------------------------------------------------------------


class IntrastatPosition(BaseModel):
    richtung: Richtung
    warennummer: str
    partner_mitgliedstaat: str  # Versendungs- (Eingang) bzw. Bestimmungsmitgliedstaat (Versendung)
    ursprungsland: str
    bundesland: str  # Bestimmungsbundesland (Eingang) bzw. Ursprungsbundesland (Versendung)
    art_des_geschaefts: str
    verkehrszweig: str
    partner_ustid: str | None = None  # nur Versendung
    eigenmasse_kg: int
    besondere_masseinheit: int | None  # Liter bei Kapitel 2204
    rechnungsbetrag_eur: int
    statistischer_wert_eur: int | None
    flaschen: Decimal
    liter: Decimal
    belegnummern: list[str] = Field(default_factory=list)
    warenbezeichnung: str | None = None


class IntrastatMeldung(BaseModel):
    firma_id: str
    monat: Meldemonat
    richtung: Richtung
    positionen: list[IntrastatPosition] = Field(default_factory=list)
    hinweise: list[Pruefhinweis] = Field(default_factory=list)
    meldepflichtig: bool | None = None  # None = Schwelle noch nicht geklärt

    @computed_field  # type: ignore[prop-decorator]
    @property
    def fehlanzeige(self) -> bool:
        return not self.positionen

    @computed_field  # type: ignore[prop-decorator]
    @property
    def summe_rechnungsbetrag(self) -> int:
        return sum(p.rechnungsbetrag_eur for p in self.positionen)


class Steuerklasse(StrEnum):
    SCHAUMWEIN_AB_6 = "schaumwein_ab_6_vol"  # 136 EUR/hl
    SCHAUMWEIN_UNTER_6 = "schaumwein_unter_6_vol"  # 51 EUR/hl
    ZWISCHEN_BIS_15 = "zwischenerzeugnis_bis_15_vol"  # 102 EUR/hl
    ZWISCHEN_UEBER_15 = "zwischenerzeugnis_ueber_15_vol"  # 153 EUR/hl
    ZWISCHEN_SCHAEUMEND = "zwischenerzeugnis_schaeumend"  # 136 EUR/hl


class SteuerZeile(BaseModel):
    klasse: Steuerklasse
    steuersatz_eur_je_hl: Decimal
    flaschen: Decimal
    liter: Decimal
    steuer_eur: Decimal
    belegnummern: list[str] = Field(default_factory=list)


class SteuerBeleg(BaseModel):
    belegnummer: str
    datum: date
    partnername: str | None
    partner_land: str
    artikelnummer: str
    bezeichnung: str | None
    klasse: Steuerklasse | None
    flaschen: Decimal
    liter: Decimal
    alkohol_vol: Decimal | None
    steuer_eur: Decimal


class SchaumweinsteuerMeldung(BaseModel):
    firma_id: str
    monat: Meldemonat
    zeilen: list[SteuerZeile] = Field(default_factory=list)
    belege: list[SteuerBeleg] = Field(default_factory=list)
    hinweise: list[Pruefhinweis] = Field(default_factory=list)
    abgabe_bis: date | None = None
    faellig_am: date | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def steuer_gesamt_eur(self) -> Decimal:
        return sum((z.steuer_eur for z in self.zeilen), Decimal("0.00"))

    @computed_field  # type: ignore[prop-decorator]
    @property
    def liter_gesamt(self) -> Decimal:
        return sum((z.liter for z in self.zeilen), Decimal(0))


class MaterialMenge(BaseModel):
    material: Material
    kg: Decimal  # 3 Nachkommastellen


class VerpackungsMeldung(BaseModel):
    firma_id: str
    monat: Meldemonat
    mengen_monat: list[MaterialMenge] = Field(default_factory=list)
    mengen_jahr_bis_monat: list[MaterialMenge] = Field(default_factory=list)
    flaschen_monat: Decimal = Decimal(0)
    flaschen_jahr_bis_monat: Decimal = Decimal(0)
    hochrechnung_jahr: list[MaterialMenge] = Field(default_factory=list)
    hinweise: list[Pruefhinweis] = Field(default_factory=list)
    systembetreiber: str | None = None
    lucid_registrierungsnummer: str | None = None

    def kg(self, material: Material, *, jahr: bool = False) -> Decimal:
        quelle = self.mengen_jahr_bis_monat if jahr else self.mengen_monat
        for m in quelle:
            if m.material == material:
                return m.kg
        return Decimal("0.000")


class Frist(BaseModel):
    meldung: str
    bezeichnung: str
    datum: date
    empfaenger: str
    hinweis: str | None = None


class Monatsmeldungen(BaseModel):
    firma_id: str
    monat: Meldemonat
    intrastat_eingang: IntrastatMeldung
    intrastat_versendung: IntrastatMeldung
    schaumweinsteuer: SchaumweinsteuerMeldung
    verpackung: VerpackungsMeldung
    fristen: list[Frist] = Field(default_factory=list)
    hinweise: list[Pruefhinweis] = Field(default_factory=list)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def alle_hinweise(self) -> list[Pruefhinweis]:
        return (
            list(self.hinweise)
            + self.intrastat_eingang.hinweise
            + self.intrastat_versendung.hinweise
            + self.schaumweinsteuer.hinweise
            + self.verpackung.hinweise
        )
