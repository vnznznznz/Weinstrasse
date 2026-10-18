"""Konfiguration laden (config/settings.yaml, config/firmen.yaml, config/aliase/*.yaml).

Alle Pfade in den YAML-Dateien sind relativ zum Projektstamm (dem Ordner, der
``config/`` enthält), sofern sie nicht absolut sind.
"""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

DEFAULT_PROJEKT_STAMM = Path(__file__).resolve().parents[2]


def projekt_stamm() -> Path:
    """Projektstamm: Umgebungsvariable AUFTRAGSAUTOMAT_HOME oder der Repo-Ordner."""
    env = os.environ.get("AUFTRAGSAUTOMAT_HOME")
    return Path(env).resolve() if env else DEFAULT_PROJEKT_STAMM


class CsvQuellen(BaseModel):
    artikel: Path
    kunden: Path
    lieferanten: Path
    belege: Path | None = None  # optional: Belegexport für die Dublettenprüfung
    trennzeichen: str = ";"
    kodierung: str = "cp1252"  # Lexware exportiert standardmäßig Windows-1252
    # Zuordnung "unser Feldname" -> "Spaltenname im Export". Fehlt ein Eintrag,
    # wird der Feldname selbst als Spaltenname erwartet.
    spalten_artikel: dict[str, str] = Field(default_factory=dict)
    spalten_kunden: dict[str, str] = Field(default_factory=dict)
    spalten_lieferanten: dict[str, str] = Field(default_factory=dict)
    spalten_belege: dict[str, str] = Field(default_factory=dict)


class OdbcQuelle(BaseModel):
    dsn: str | None = None
    verbindung: str | None = None  # alternativ: vollständiger Connection-String
    benutzer: str | None = None
    passwort_env: str | None = None  # Name der Umgebungsvariable mit dem Passwort
    nur_lesend: Literal[True] = True  # bewusst nicht abschaltbar
    # SQL-Abfragen sind konfigurierbar, weil die Lexware-Tabellennamen je Version abweichen.
    sql_artikel: str | None = None
    sql_kunden: str | None = None
    sql_lieferanten: str | None = None
    sql_belege: str | None = None


class StammdatenKonfig(BaseModel):
    typ: Literal["csv", "odbc"] = "csv"
    csv: CsvQuellen | None = None
    odbc: OdbcQuelle | None = None


class OpenTransKonfig(BaseModel):
    import_ordner: Path | None = None  # Ordner, den die Lexware-Shopschnittstelle einliest
    lizenziert: bool | None = None  # None = noch nicht geprüft
    waehrung: str = "EUR"
    mwst_satz: Decimal = Decimal("19")


class SchaumweinKonfig(BaseModel):
    """Verbrauchsteuerlicher Status der Firma (vom Hauptzollamt erteilt)."""

    status: Literal["keiner", "zertifizierter_empfaenger", "registrierter_empfaenger", "steuerlager"] = (
        "keiner"
    )
    verbrauchsteuernummer: str | None = None  # z. B. DE 1234567 (Erlaubnis), auf dem Erlaubnisschein
    hauptzollamt: str | None = None
    # "eingang": Steuer entsteht mit Empfang (zertifizierter/registrierter Empfänger)
    # "entnahme": Steuer entsteht mit Entnahme aus dem Steuerlager (= Verkauf/Lieferung)
    steuerentstehung: Literal["eingang", "entnahme"] = "eingang"


class VerpackungKonfig(BaseModel):
    systembetreiber: str | None = None  # z. B. "Der Grüne Punkt – Duales System Deutschland GmbH"
    vertragsnummer: str | None = None
    lucid_registrierungsnummer: str | None = None  # DE... aus LUCID
    meldeturnus: Literal["monatlich", "quartal", "halbjahr", "jaehrlich"] = "jaehrlich"
    # Nur Lieferungen an deutsche Kunden gelten als Inverkehrbringen in Deutschland.
    # Lieferungen an Endverbraucher (B2C) sind immer systembeteiligungspflichtig; bei
    # Lieferungen an den Handel entscheidet das Verpackungsprofil je Komponente.
    nur_inland: bool = True


class IntrastatKonfig(BaseModel):
    bundesland: str = "05"  # Schlüssel laut Anhang 1 Leitfaden (05 = Nordrhein-Westfalen)
    ustid: str | None = None
    materialnummer_eingang: str | None = None  # Destatis-Materialnummer (Meldernummer) Eingang
    materialnummer_versendung: str | None = None
    meldepflichtig_eingang: bool | None = None  # None = Schwelle (3 Mio. EUR) noch nicht geprüft
    meldepflichtig_versendung: bool | None = None  # Schwelle 1 Mio. EUR
    art_des_geschaefts_standard: str = "11"
    verkehrszweig_standard: str = "3"  # Straßenverkehr
    # Statistischer Wert = frei deutsche Grenze. Bei verbrauchsteuerpflichtigen Waren immer
    # anzugeben; wir füllen ihn standardmäßig aus Rechnungsbetrag + Frachtzuschlag.
    statistischer_wert_angeben: bool = True
    frachtzuschlag_prozent: Decimal = Decimal("0")  # wenn Fracht nicht separat bekannt ist


class BewegungenExport(BaseModel):
    """Lexware-Export der Belegpositionen eines Monats (CSV oder XLSX)."""

    muster: str = "daten/{firma}/belegpositionen_{jahr}-{monat:02d}.csv"
    trennzeichen: str = ";"
    kodierung: str = "cp1252"
    spalten: dict[str, str] = Field(default_factory=dict)  # unser Feldname -> Spaltenname im Export
    belegtyp_werte: dict[str, str] = Field(
        default_factory=lambda: {
            "ER": "einkauf",
            "WE": "einkauf",
            "BE": "einkauf",
            "RE": "verkauf",
            "LS": "verkauf",
            "GS": "gutschrift_verkauf",
            "EG": "gutschrift_einkauf",
        }
    )


class FirmaMeldungen(BaseModel):
    aktiv: bool = True
    intrastat: IntrastatKonfig = Field(default_factory=IntrastatKonfig)
    schaumwein: SchaumweinKonfig = Field(default_factory=SchaumweinKonfig)
    verpackung: VerpackungKonfig = Field(default_factory=VerpackungKonfig)
    bewegungen: BewegungenExport = Field(default_factory=BewegungenExport)


class Firma(BaseModel):
    id: str
    name: str
    lexware_firmenname: str  # exakter Name in der Lexware-Firmenauswahl
    einkauf_bestellung: bool = True  # Weinstrasse Agentur: False
    stammdaten: StammdatenKonfig = Field(default_factory=StammdatenKonfig)
    opentrans: OpenTransKonfig = Field(default_factory=OpenTransKonfig)
    belegfelder_standard: dict[str, str] = Field(default_factory=dict)  # z. B. spedition
    eingang_unterordner: str | None = None  # Eingang/<unterordner> -> Firma
    meldungen: FirmaMeldungen = Field(default_factory=FirmaMeldungen)


class MatchingKonfig(BaseModel):
    schwelle_sicher: float = 90.0  # ab diesem Score automatisch zuordnen
    schwelle_kandidat: float = 60.0  # ab diesem Score als Kandidat anzeigen
    standard_volumen_cl: Decimal = Decimal("75")
    preis_toleranz_eur: Decimal = Decimal("0.00")  # Abweichung > Toleranz => Warnung
    konfidenz_schwelle: float = 0.80  # Vision: darunter Prüfhinweis


class ClaudeKonfig(BaseModel):
    modell: str = "claude-opus-5-5"
    max_tokens: int = 16000
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    api_key_env: str = "ANTHROPIC_API_KEY"


class LexwareKonfig(BaseModel):
    exe_pfad: Path | None = None
    fenster_titel_regex: str = r".*Lexware.*"
    wartezeit_sekunden: float = 1.5
    maximale_wartezeit_sekunden: float = 30.0
    screenshots_ordner: Path | None = None


class MeldungenKonfig(BaseModel):
    ausgabe_ordner: Path = Path("meldungen")
    artikel_meldedaten: Path = Path("config/meldungen/artikel_meldedaten.csv")
    verpackungen: Path = Path("config/meldungen/verpackungen.yaml")
    warennummern: Path = Path("config/meldungen/warennummern.yaml")
    dichte_kg_je_l: Decimal = Decimal("0.99")  # Wein, für die Eigenmasse (ohne Flasche)
    # Steuersätze SchaumwZwStG (§ 2, § 30) in EUR je Hektoliter. TODO-ZOLL: bei Gesetzesänderung anpassen.
    steuersaetze_eur_je_hl: dict[str, Decimal] = Field(
        default_factory=lambda: {
            "schaumwein_ab_6_vol": Decimal("136.00"),
            "schaumwein_unter_6_vol": Decimal("51.00"),
            "zwischenerzeugnis_bis_15_vol": Decimal("102.00"),
            "zwischenerzeugnis_ueber_15_vol": Decimal("153.00"),
            "zwischenerzeugnis_schaeumend": Decimal("136.00"),
        }
    )
    intrastat_schwelle_eingang_eur: Decimal = Decimal("3000000")
    intrastat_schwelle_versendung_eur: Decimal = Decimal("1000000")
    # Spaltenreihenfolge der CSV-Datei für den Import ins IDEV-Formular. Semikolon, ohne Kopfzeile.
    # TODO-DESTATIS: gegen die Musterdatei aus dem IDEV-Formular "Intrahandel" prüfen.
    idev_spalten_eingang: list[str] = Field(
        default_factory=lambda: [
            "warennummer",
            "partner_mitgliedstaat",
            "ursprungsland",
            "bundesland",
            "art_des_geschaefts",
            "verkehrszweig",
            "eigenmasse_kg",
            "besondere_masseinheit",
            "rechnungsbetrag_eur",
            "statistischer_wert_eur",
        ]
    )
    idev_spalten_versendung: list[str] = Field(
        default_factory=lambda: [
            "warennummer",
            "partner_mitgliedstaat",
            "ursprungsland",
            "bundesland",
            "art_des_geschaefts",
            "verkehrszweig",
            "eigenmasse_kg",
            "besondere_masseinheit",
            "rechnungsbetrag_eur",
            "statistischer_wert_eur",
            "partner_ustid",
        ]
    )
    idev_kopfzeile: bool = True


class Settings(BaseModel):
    eingang_ordner: Path = Path("eingang")
    archiv_ordner: Path = Path("archiv")
    log_ordner: Path = Path("logs")
    xml_ausgabe_ordner: Path = Path("logs/opentrans")
    dry_run: bool = True  # Standard: NICHT scharf
    matching: MatchingKonfig = Field(default_factory=MatchingKonfig)
    claude: ClaudeKonfig = Field(default_factory=ClaudeKonfig)
    lexware: LexwareKonfig = Field(default_factory=LexwareKonfig)
    firmen: dict[str, Firma] = Field(default_factory=dict)
    alias_ordner: Path = Path("config/aliase")
    meldungen: MeldungenKonfig = Field(default_factory=MeldungenKonfig)
    projekt_stamm: Path | None = None  # wird von lade_settings gesetzt

    def firma(self, firma_id: str) -> Firma:
        try:
            return self.firmen[firma_id]
        except KeyError as exc:
            raise KeyError(f"Unbekannte Firma '{firma_id}'. Bekannt: {', '.join(self.firmen)}") from exc


def _absolut(pfad: Path | None, stamm: Path) -> Path | None:
    if pfad is None:
        return None
    pfad = Path(pfad)
    return pfad if pfad.is_absolute() else (stamm / pfad).resolve()


def lade_settings(stamm: Path | None = None) -> Settings:
    stamm = stamm or projekt_stamm()
    settings_datei = stamm / "config" / "settings.yaml"
    firmen_datei = stamm / "config" / "firmen.yaml"

    roh: dict = {}
    if settings_datei.exists():
        roh = yaml.safe_load(settings_datei.read_text(encoding="utf-8")) or {}
    firmen_roh: dict = {}
    if firmen_datei.exists():
        firmen_roh = (yaml.safe_load(firmen_datei.read_text(encoding="utf-8")) or {}).get("firmen", {})

    firmen: dict[str, Firma] = {}
    for firma_id, daten in firmen_roh.items():
        daten = dict(daten or {})
        daten.setdefault("id", firma_id)
        firma = Firma.model_validate(daten)
        if firma.stammdaten.csv is not None:
            csv = firma.stammdaten.csv
            csv.artikel = _absolut(csv.artikel, stamm)  # type: ignore[assignment]
            csv.kunden = _absolut(csv.kunden, stamm)  # type: ignore[assignment]
            csv.lieferanten = _absolut(csv.lieferanten, stamm)  # type: ignore[assignment]
            csv.belege = _absolut(csv.belege, stamm)
        if firma.opentrans.import_ordner is not None:
            firma.opentrans.import_ordner = _absolut(firma.opentrans.import_ordner, stamm)
        firmen[firma_id] = firma

    settings = Settings.model_validate({**roh, "firmen": firmen})
    for feld in ("eingang_ordner", "archiv_ordner", "log_ordner", "xml_ausgabe_ordner", "alias_ordner"):
        setattr(settings, feld, _absolut(getattr(settings, feld), stamm))
    for feld in ("ausgabe_ordner", "artikel_meldedaten", "verpackungen", "warennummern"):
        setattr(settings.meldungen, feld, _absolut(getattr(settings.meldungen, feld), stamm))
    settings.projekt_stamm = stamm
    return settings
