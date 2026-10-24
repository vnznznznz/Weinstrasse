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


class FormularKonfig(BaseModel):
    """Namen der Lexware-Druckformulare je Belegart (Lexware: Drucken -> Formular wählen).

    ``None`` = Lexware-Standardformular. Laut dokumentiertem Ablauf: WIK/WOE drucken die
    Einkauf-Bestellung mit "WIK EN" zweimal (Cde an das Weingut und Speditionsauftrag) und die
    AB mit "WIK AB_LS"; die Weinstrasse Agentur druckt das Angebot mit "WS_EN" (Cde und
    Speditionsauftrag) und die AB mit "WS_AB" (= Auftragskopie).
    """

    einkauf_en: str | None = None  # Einkauf-Bestellung: Cde + Speditionsauftrag
    angebot_en: str | None = None  # Angebot (Weinstrasse Agentur): Cde + Speditionsauftrag
    verkauf_ab: str | None = None  # Auftragsbestätigung (für den Kunden = Auftragskopie)
    lieferschein: str | None = None
    speditionsauftrag_kopien: int = 2  # EN-Formular zweimal: Cde + Speditionsauftrag


class Firma(BaseModel):
    id: str
    name: str
    lexware_firmenname: str  # exakter Name in der Lexware-Firmenauswahl
    kuerzel: str = ""  # Kurzname im Dateinamen/Ablagepfad (WS, WIK, WOE); leer = aus der ID abgeleitet
    einkauf_bestellung: bool = True  # Weinstrasse Agentur: False
    stammdaten: StammdatenKonfig = Field(default_factory=StammdatenKonfig)
    opentrans: OpenTransKonfig = Field(default_factory=OpenTransKonfig)
    belegfelder_standard: dict[str, str] = Field(default_factory=dict)  # z. B. spedition
    eingang_unterordner: str | None = None  # Eingang/<unterordner> -> Firma
    # Verkaufsweg laut dokumentiertem Lexware-Ablauf (docs/AUSBAUSTUFE_2.md, Abschnitt 5):
    #   angebot_weiterfuehren: Angebot anlegen -> "Weiterführen" -> AB (Lieferdatum/AB-Text löschen)
    #   angebot_wandeln:       Angebot anlegen -> "Auftrag wandeln" -> AB
    #   direkt_ab:             AB direkt anlegen (ohne Angebot)
    verkauf_weg: Literal["angebot_weiterfuehren", "angebot_wandeln", "direkt_ab"] = "angebot_weiterfuehren"
    lieferdatum_in_ab_loeschen: bool = True  # Lieferdatum in der AB leeren (Fixtermin steht in der Excel)
    ab_text_loeschen: bool = True  # Standard-AB-Text leeren
    bestellnummer_in_nachbemerkung: bool = True  # Kundenbestellnummer in die Nachbemerkung der Einkauf-Bestellung
    lieferschein_erlaubt: bool = True  # Weinstrasse Agentur: False (kein Lieferschein)
    formulare: FormularKonfig = Field(default_factory=FormularKonfig)
    kundengruppen: list[str] = Field(default_factory=list)  # Kundengruppen laut Katalog, die hier abgewickelt werden
    ablage_ordner: str | None = None  # Unterordner unter "Buchhaltung/Aufträge"; None = kuerzel

    @property
    def kurz(self) -> str:
        """Kürzel für Dateinamen/Ablage (``kuerzel`` oder aus der ID: wik_leh -> WIK)."""
        if self.kuerzel:
            return self.kuerzel
        return self.id.split("_")[0].upper()


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
    # PDF-Ausgabe der Belege (Cde, Kopie, Ls) – nie auf Papier, nie per E-Mail aus Lexware:
    #   keine:    keine PDF aus Lexware (nur Dry-Run-Vorschau)
    #   drucker:  Lexware-Druckdialog mit PDF-Drucker (pdf_drucker), Dateiname laut Benennungsschema
    #   export:   Lexware-Funktion "Exportieren als PDF" (sofern im Belegfenster vorhanden)
    pdf_ausgabe: Literal["keine", "drucker", "export"] = "keine"
    pdf_drucker: str = "Microsoft Print to PDF"
    pdf_ausgabe_ordner: Path = Path("logs/pdf")


class BenennungKonfig(BaseModel):
    """Benennungsschema der Dateien ("Titulierung").

    Weinstrasse Agentur: ``Lieferant – Kunde – Kürzel – Datum`` (Kunden in
    ``kunden_mit_auftragsnummer`` zusätzlich mit Auftragsnummer);
    WIK/WOE: ``Lieferant – WIK|WOE – Bestell-Nr – Kürzel – Datum``;
    Spedition: ``Spedition – Speditionsauftrag|Speditionsbestätigung|Lieferschein – Lieferant – Kunde|Bestell-Nr – Datum``.
    """

    trenner: str = " – "
    datumsformat: str = "%d.%m.%Y"
    kunden_mit_auftragsnummer: list[str] = Field(default_factory=lambda: ["Spar"])
    ersatzzeichen: str = "-"  # für Zeichen, die in Dateinamen verboten sind (/ \ : * ? " < > |)
    max_laenge: int = 180


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


class RabatteKonfig(BaseModel):
    """Aktionszeiträume (Excel in OneDrive) und hochgeladene Rabattlisten (PDF)."""

    aktiv: bool = True
    aktionen_datei: DateiRefKonfig = Field(default_factory=lambda: DateiRefKonfig(blatt="Aktionen"))
    # Spaltenzuordnung "unser Feld" -> "Spaltenüberschrift in der Excel" (Groß-/Kleinschreibung egal)
    spalten: dict[str, str] = Field(
        default_factory=lambda: {
            "kunde_kurz": "Kunde",
            "lieferant_kurz": "Lieferant",
            "artikel_text": "Artikel",
            "artikelnummern": "Artikelnummer",
            "von": "Von",
            "bis": "Bis",
            "rabatt_prozent": "Rabatt %",
            "rabatt_eur": "Rabatt EUR",
            "aktionspreis": "Aktionspreis",
            "bezeichnung": "Aktion",
            "bemerkung": "Bemerkung",
            "firma_id": "Firma",
        }
    )
    rabatt_pdf_ordner: Path = Path("eingang/rabatte")  # hierhin legt die Freigabe-UI hochgeladene PDFs
    gedaechtnis_datei: Path = Path("config/aktionen.yaml")  # aus PDFs extrahierte Aktionen (nach Freigabe)
    automatisch_anwenden: bool = True  # sichere Treffer direkt als rabatt_prozent eintragen (Anzeige rot/gelb)
    bezug: Literal["liefertag", "auftragsdatum"] = "liefertag"
    toleranz_tage: int = 0  # Zeitraum um x Tage großzügiger auslegen (dann RABATT_UNSICHER)
    max_rabatt_prozent: Decimal = Decimal("50")  # Plausibilitätsgrenze: darüber FEHLER statt Vorschlag


class FirmenwahlKonfig(BaseModel):
    """Firma nach Katalog (Kundengruppen) wählen, Antworten des Anwenders je Kunde + Lieferant merken."""

    katalog_datei: Path = Path("config/firmenkatalog.yaml")
    gedaechtnis_datei: Path = Path("config/firmen_gedaechtnis.yaml")
    merken: bool = True  # Antwort aus der Freigabe je Kunde + Lieferant speichern
    fragen_wenn_unklar: bool = True  # mehrere Firmen möglich -> Pruefhinweis FIRMA_UNKLAR (FEHLER) bis zur Wahl
    gedaechtnis_vor_katalog: bool = True  # gelernte Regel schlägt den Katalog
    # Gelernte Regeln auch tolerant wiedererkennen (Kurzname als Wort im Kundentext). Bei Region/Kette/
    # Großkunde nie über den Matchcode, nur über die gelernten Namen; False = nur exakte Schlüssel.
    gedaechtnis_tolerant: bool = True


class AblageKonfig(BaseModel):
    """Fertige, benannte Dateien in OneDrive einsortieren (nur nach Freigabe).

    Vorgabe des Anwenders (Korrektur vom 24.10.2026): Aufträge der **Weinstrasse Agentur** kommen in
    den Ordner **"Aufträge mit Provision"**, alle Aufträge der anderen Firmen (WIK FH & SEH, WIK LEH,
    WOE) in **"Aufträge ohne Provision"**. ``auftraege_ordner`` ist der OneDrive-Ordner, der diese
    beiden Ordner enthält. ``struktur`` ist der Zielpfad darunter; der Platzhalter ``{provision}``
    wird je Firma durch einen der beiden Ordnernamen ersetzt (weitere Platzhalter: ``{firma}`` =
    Kürzel, ``{jahr}``, ``{monat}``, ``{lieferant}``, ``{kunde}``).
    """

    aktiv: bool = False  # erst nach Freigabe des Benennungsschemas durch den Anwender einschalten
    auftraege_ordner: DateiRefKonfig = Field(default_factory=DateiRefKonfig)  # OneDrive-Ordner mit beiden Provisionsordnern
    ordner_mit_provision: str = "Aufträge mit Provision"  # Weinstrasse Agentur
    ordner_ohne_provision: str = "Aufträge ohne Provision"  # alle anderen Firmen
    firmen_mit_provision: list[str] = Field(default_factory=lambda: ["weinstrasse_agentur"])
    struktur: str = "{provision}"  # Zielpfad unter auftraege_ordner; z. B. "{provision}" oder "{provision}/{jahr}"
    lokaler_ordner: Path = Path("logs/ablage")  # Dry Run / Backend "fake"
    nur_scharf: bool = True  # im Dry Run nur protokollieren, nichts kopieren
    vorhandene_ueberschreiben: bool = False  # Namenskollision -> " (2)" anhängen statt überschreiben
    kundenbestellung_ablegen: bool = True  # das Eingangsdokument als "Kd" mit ablegen

    def provisionsordner(self, firma_id: str) -> str:
        """Ordnername je Firma: "Aufträge mit Provision" (Weinstrasse Agentur) oder "... ohne Provision"."""
        return self.ordner_mit_provision if firma_id in self.firmen_mit_provision else self.ordner_ohne_provision


class EingangKonfig(BaseModel):
    """Der Eingang des Tools: der OneDrive-Ordner "Eingang", in den Kunden-/Verkäuferaufträge gelegt werden.

    Vorgabe des Anwenders: Die Kundenaufträge liegen im OneDrive-Ordner **"Eingang"**.
    ``Settings.eingang_ordner`` zeigt auf den lokal synchronisierten Pfad dieses Ordners (oder
    ``onedrive_ordner`` auf den Ordner in Graph); der Watcher, der Upload in der Freigabe-UI und
    die Verarbeitung nutzen denselben Ordner.
    """

    ordner_name: str = "Eingang"  # Name des OneDrive-Ordners

    upload_erlaubt: bool = True  # Freigabe-UI: "Auftrag (PDF) hochladen" -> Datei landet im Eingang
    upload_max_mb: int = 50
    upload_dateitypen: list[str] = Field(default_factory=lambda: [".pdf", ".msg", ".eml", ".png", ".jpg", ".jpeg"])
    upload_in_firmen_unterordner: bool = True  # bei gewählter Firma nach eingang/<firma>/ speichern
    onedrive_ordner: DateiRefKonfig = Field(default_factory=DateiRefKonfig)  # optional: Eingang in OneDrive (Graph)
    als_kundenbestellung_ablegen: bool = True  # Eingangsdokument nach Freigabe als "Kd" mit in den Provisionsordner ablegen


class DossierKonfig(BaseModel):
    ordner_name: str = "vorangefertigte Dossiers"
    lokaler_ordner: Path = Path("logs/dossiers")  # Dry Run / Backend "fake"
    gesamt_pdf: bool = False
    verschieben_erlauben: bool = False
    # Veraltet und ohne Wirkung (nur noch geduldet, damit ältere settings.yaml gültig bleiben): der
    # Zielordner-Vorschlag im Deckblatt folgt der Ablage (AblageKonfig.struktur/provisionsordner).
    ablage_mit_provision: str = "Aufträge mit Provision"
    ablage_ohne_provision: str = "Aufträge ohne Provision"
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
    # Prozess laut Lexware-Doku, Aktionen/Rabatte, Firmenwahl, Ablage
    benennung: BenennungKonfig = Field(default_factory=BenennungKonfig)
    eingang: EingangKonfig = Field(default_factory=EingangKonfig)
    rabatte: RabatteKonfig = Field(default_factory=RabatteKonfig)
    firmenwahl: FirmenwahlKonfig = Field(default_factory=FirmenwahlKonfig)
    ablage: AblageKonfig = Field(default_factory=AblageKonfig)

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
    settings.lexware.pdf_ausgabe_ordner = _absolut(settings.lexware.pdf_ausgabe_ordner, stamm)  # type: ignore[assignment]
    settings.rabatte.rabatt_pdf_ordner = _absolut(settings.rabatte.rabatt_pdf_ordner, stamm)  # type: ignore[assignment]
    settings.rabatte.gedaechtnis_datei = _absolut(settings.rabatte.gedaechtnis_datei, stamm)  # type: ignore[assignment]
    settings.firmenwahl.katalog_datei = _absolut(settings.firmenwahl.katalog_datei, stamm)  # type: ignore[assignment]
    settings.firmenwahl.gedaechtnis_datei = _absolut(settings.firmenwahl.gedaechtnis_datei, stamm)  # type: ignore[assignment]
    settings.ablage.lokaler_ordner = _absolut(settings.ablage.lokaler_ordner, stamm)  # type: ignore[assignment]
    k = settings.kommunikation
    for feld in ("vorlagen_ordner", "signaturen_datei", "anrede_datei", "entwuerfe_ordner"):
        setattr(k, feld, _absolut(getattr(k, feld), stamm))
    settings.tracking.hillebrand_konfig = _absolut(settings.tracking.hillebrand_konfig, stamm)  # type: ignore[assignment]
    settings.tracking.screenshots_ordner = _absolut(settings.tracking.screenshots_ordner, stamm)  # type: ignore[assignment]
    settings.ueberwachung.merkdatei = _absolut(settings.ueberwachung.merkdatei, stamm)  # type: ignore[assignment]
    settings.ueberwachung.berichte_ordner = _absolut(settings.ueberwachung.berichte_ordner, stamm)  # type: ignore[assignment]
    settings.dossier.lokaler_ordner = _absolut(settings.dossier.lokaler_ordner, stamm)  # type: ignore[assignment]
    for ref in (
        settings.m365.fixtermin_datei,
        settings.m365.kontaktliste_datei,
        settings.m365.dossier_ordner,
        settings.rabatte.aktionen_datei,
        settings.ablage.auftraege_ordner,
        settings.eingang.onedrive_ordner,
    ):
        if ref.lokaler_pfad is not None:
            ref.lokaler_pfad = _absolut(ref.lokaler_pfad, stamm)
    return settings
