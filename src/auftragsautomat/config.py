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
    kodierung: str = "cp1252"  # Zeichensatz der ODBC-Verbindung (SQL Anywhere/Windows-1252)
    timeout_sekunden: float = 15.0  # Verbindungs-Timeout


class StammdatenKonfig(BaseModel):
    typ: Literal["csv", "odbc"] = "csv"
    csv: CsvQuellen | None = None
    odbc: OdbcQuelle | None = None


class OpenTransKonfig(BaseModel):
    import_ordner: Path | None = None  # Ordner, den die Lexware-Shopschnittstelle einliest
    lizenziert: bool | None = None  # None = noch nicht geprüft
    waehrung: str = "EUR"
    mwst_satz: Decimal = Decimal("19")
    # TODO-LEXWARE: Firmen-GUID für REMARK origin_company_id je Firma ermitteln (catalog.xml-Export).
    firmen_guid: str | None = None
    preistyp: Literal["net_list", "gros_list"] = "net_list"  # Nettofirma: net_list, Bruttofirma: gros_list
    steuergebiet: Literal["Merchant", "EU", "Non_EU"] = "Merchant"  # REMARK type=tax_area


class Firma(BaseModel):
    id: str
    name: str
    lexware_firmenname: str  # exakter Name in der Lexware-Firmenauswahl
    einkauf_bestellung: bool = True  # Weinstrasse Agentur: False
    stammdaten: StammdatenKonfig = Field(default_factory=StammdatenKonfig)
    opentrans: OpenTransKonfig = Field(default_factory=OpenTransKonfig)
    belegfelder_standard: dict[str, str] = Field(default_factory=dict)  # z. B. spedition
    eingang_unterordner: str | None = None  # Eingang/<unterordner> -> Firma


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
    firmenwechsel_erlaubt: bool = False  # darf die UI-Automation die Lexware-Firma wechseln?
    backend: Literal["uia", "win32"] = "uia"  # pywinauto-Backend
    controls_datei: Path | None = None  # Standard: config/lexware_controls.yaml


class DateiRefKonfig(BaseModel):
    """Verweis auf eine Datei in OneDrive/SharePoint (Graph) oder lokal (synchronisierter Pfad)."""

    web_url: str | None = None  # Adresszeile aus dem Browser (wird einmalig in drive_id/item_id aufgelöst)
    drive_id: str | None = None
    item_id: str | None = None
    lokaler_pfad: Path | None = None  # OneDrive-synchronisierter Pfad (für Backend "lokal"/"fake")
    blatt: str | None = None  # Standard-Arbeitsblatt (Excel)


class M365Konfig(BaseModel):
    backend: Literal["graph", "lokal", "fake"] = "fake"
    tenant_id: str | None = None
    client_id: str | None = None
    autoritaet: str | None = None  # Standard: https://login.microsoftonline.com/<tenant_id>
    bereiche: list[str] = Field(
        default_factory=lambda: [
            "User.Read",
            "Files.ReadWrite",
            "Sites.Read.All",
            "Mail.ReadWrite",
            "Mail.Read",
        ]
    )
    token_cache: Path | None = None  # Standard: %APPDATA%/Auftragsautomat/token_cache.bin
    postfach: str | None = None  # None = angemeldeter Benutzer (/me); sonst Shared Mailbox
    fixtermin_datei: DateiRefKonfig = Field(default_factory=lambda: DateiRefKonfig(blatt="Fixterminüberwachung"))
    kontaktliste_datei: DateiRefKonfig = Field(default_factory=DateiRefKonfig)
    dossier_ordner: DateiRefKonfig = Field(default_factory=DateiRefKonfig)  # Ordner "vorangefertigte Dossiers"
    anfragen_pro_sekunde: float = 4.0


class KommunikationKonfig(BaseModel):
    absender_name: str = ""  # wird in Signaturen eingesetzt (config/signaturen.yaml)
    absender_email: str | None = None
    cc_standard: list[str] = Field(default_factory=list)
    vorlagen_ordner: Path = Path("config/vorlagen")
    signaturen_datei: Path = Path("config/signaturen.yaml")
    anrede_datei: Path = Path("config/anrede.yaml")
    entwuerfe_ordner: Path = Path("logs/entwuerfe")  # .eml-Ablage im Dry Run / Backend "fake"
    # Begriffe, die in Betreff/Text/Dateiname der Auftragskopie nicht vorkommen dürfen (Haftung)
    verbotene_woerter_auftragskopie: list[str] = Field(
        default_factory=lambda: ["Auftragsbestätigung", "Auftragsbestaetigung", "bestätigen", "bestaetigen", "verbindlich"]
    )


class TrackingKonfig(BaseModel):
    quellen: list[Literal["mail", "api", "public_tracking", "portal"]] = Field(
        default_factory=lambda: ["mail", "api", "public_tracking"]
    )
    hillebrand_konfig: Path = Path("config/tracking_hillebrand.yaml")
    api_key_env: str = "HILLEBRAND_API_KEY"
    api_secret_env: str = "HILLEBRAND_API_SECRET"
    api_benutzer_env: str = "HILLEBRAND_API_BENUTZER"
    api_passwort_env: str = "HILLEBRAND_API_PASSWORT"
    chrome_profil: Path | None = None  # Standard: %LOCALAPPDATA%/Auftragsautomat/chrome-profil (eigenes Profil!)
    sichtbar: bool = True  # Portal nur sichtbar automatisieren
    screenshots_ordner: Path = Path("logs/tracking")


class UeberwachungKonfig(BaseModel):
    tage_voraus: int = 21
    mails_rueckblick_tage: int = 7
    nachfrage_lieferant_werktage: int = 10  # ohne Lieferantenbestätigung ab x Werktagen vor Fixtermin
    nachfrage_spedition_werktage: int = 5
    erneut_nach_werktagen: int = 2  # dieselbe Nachfrage höchstens alle x Werktage
    bestaetigungen_automatisch_setzen: bool = True  # nur bei eindeutiger Zuordnung
    termine_automatisch_anpassen: bool = False  # sonst Vorschlag zur Freigabe
    bundesland: str = "NW"  # Feiertage für Werktagsrechnung
    merkdatei: Path = Path("logs/nachfragen.json")
    berichte_ordner: Path = Path("logs/ueberwachung")


class KontakteKonfig(BaseModel):
    automatisch_uebernehmen: bool = False  # Änderungsvorschläge nur nach Freigabe
    standard_spedition: str = "Hillebrand"


class DossierKonfig(BaseModel):
    ordner_name: str = "vorangefertigte Dossiers"
    lokaler_ordner: Path = Path("logs/dossiers")  # Dry Run / Backend "fake"
    gesamt_pdf: bool = False
    verschieben_erlauben: bool = False
    ablage_mit_provision: str = "Auftragsablage (mit Provision)"
    ablage_ohne_provision: str = "Auftragsablage (ohne Provision)"
    firmen_mit_provision: list[str] = Field(default_factory=lambda: ["weinstrasse_agentur"])


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
    # Ausbaustufe 2
    m365: M365Konfig = Field(default_factory=M365Konfig)
    kommunikation: KommunikationKonfig = Field(default_factory=KommunikationKonfig)
    tracking: TrackingKonfig = Field(default_factory=TrackingKonfig)
    ueberwachung: UeberwachungKonfig = Field(default_factory=UeberwachungKonfig)
    kontakte: KontakteKonfig = Field(default_factory=KontakteKonfig)
    dossier: DossierKonfig = Field(default_factory=DossierKonfig)

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
    settings.lexware.screenshots_ordner = _absolut(settings.lexware.screenshots_ordner, stamm)
    settings.lexware.controls_datei = _absolut(settings.lexware.controls_datei, stamm)
    k = settings.kommunikation
    for feld in ("vorlagen_ordner", "signaturen_datei", "anrede_datei", "entwuerfe_ordner"):
        setattr(k, feld, _absolut(getattr(k, feld), stamm))
    settings.tracking.hillebrand_konfig = _absolut(settings.tracking.hillebrand_konfig, stamm)  # type: ignore[assignment]
    settings.tracking.screenshots_ordner = _absolut(settings.tracking.screenshots_ordner, stamm)  # type: ignore[assignment]
    settings.ueberwachung.merkdatei = _absolut(settings.ueberwachung.merkdatei, stamm)  # type: ignore[assignment]
    settings.ueberwachung.berichte_ordner = _absolut(settings.ueberwachung.berichte_ordner, stamm)  # type: ignore[assignment]
    settings.dossier.lokaler_ordner = _absolut(settings.dossier.lokaler_ordner, stamm)  # type: ignore[assignment]
    for ref in (settings.m365.fixtermin_datei, settings.m365.kontaktliste_datei, settings.m365.dossier_ordner):
        if ref.lokaler_pfad is not None:
            ref.lokaler_pfad = _absolut(ref.lokaler_pfad, stamm)
    return settings
