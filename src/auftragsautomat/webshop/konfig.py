"""Konfiguration des Online-Shops aus ``config/webshop.yaml``."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator

from auftragsautomat.config import projekt_stamm

Produkttyp = Literal["rot", "weiss", "rose", "schaum", "spirit", "alkoholfrei"]

#: Darstellung je Produkttyp im Frontend (Flaschen-Symbol und Kartenhintergrund).
DARSTELLUNG: dict[str, tuple[str, str]] = {
    "rot": ("red", "linear-gradient(160deg,#f3e3df,#e4c3bd)"),
    "weiss": ("white", "linear-gradient(160deg,#eef3e4,#d6e3c2)"),
    "rose": ("rose", "linear-gradient(160deg,#fbe7e4,#f1c9c4)"),
    "schaum": ("sparkling", "linear-gradient(160deg,#f2f1e7,#d8dcc7)"),
    "spirit": ("spirit", "linear-gradient(160deg,#f6ead9,#e6cfae)"),
    "alkoholfrei": ("zero", "linear-gradient(160deg,#e9eef5,#cfd9e6)"),
}


class LadenKonfig(BaseModel):
    name: str
    strasse: str
    plz: str
    ort: str
    email: str
    oeffnungszeiten: dict[int, tuple[str, str]] = Field(default_factory=dict)  # 1 = Montag


class Zone(BaseModel):
    id: str
    name: str
    gebuehr: Decimal = Decimal("0.00")
    plz: list[str] = Field(default_factory=list)

    @field_validator("plz", mode="before")
    @classmethod
    def _als_text(cls, werte: list[object]) -> list[str]:
        return [str(w).strip() for w in werte]


class Zeitfenster(BaseModel):
    id: str
    von: str
    bis: str
    tage: list[int] = Field(default_factory=lambda: [1, 2, 3, 4, 5])  # ISO-Wochentage, 1 = Montag

    @property
    def text(self) -> str:
        return f"{self.von.rstrip(':0').replace(':', '.') or '0'} – {self.bis.rstrip(':0').replace(':', '.') or '0'} Uhr"


class LieferungKonfig(BaseModel):
    mindestflaschen_lastenrad: int = 12
    bestellschluss_uhr: int = 12
    tage_voraus: int = 4
    versandkosten: Decimal = Decimal("6.90")
    versandkostenfrei_ab: Decimal = Decimal("120.00")
    zonen: list[Zone] = Field(default_factory=list)
    zeitfenster: list[Zeitfenster] = Field(default_factory=list)

    def zone_fuer(self, plz: str) -> Zone | None:
        plz = plz.strip()
        for zone in self.zonen:
            if plz in zone.plz:
                return zone
        return None

    def zeitfenster_nach_id(self, fenster_id: str) -> Zeitfenster | None:
        for fenster in self.zeitfenster:
            if fenster.id == fenster_id:
                return fenster
        return None


class MailKonfig(BaseModel):
    smtp_host: str | None = None
    smtp_port: int = 587
    benutzer: str | None = None
    passwort_env: str = "WEBSHOP_SMTP_PASSWORT"
    absender: str | None = None
    empfaenger_laden: str | None = None


class SortimentEintrag(BaseModel):
    artikelnummer: str
    name: str | None = None  # None = Bezeichnung aus dem Artikelstamm
    untertitel: str = ""
    typ: Produkttyp = "rot"
    land: str = ""
    badge: str = ""
    text: str = ""
    volumen_cl: Decimal | None = None  # None = aus dem Stamm, sonst 75
    preis_brutto: Decimal | None = None  # Beispielpreis, falls der Artikel nicht im Stamm steht
    jahrgang: int | None = None
    bild: str | None = None  # Pfad relativ zu website/, z. B. assets/img/produkte/zenas-primitivo.jpg


class WebshopKonfig(BaseModel):
    firma: str
    mwst_prozent: Decimal = Decimal("19")
    ausgabe_ordner: Path = Path("website/data")
    bestellungen_ordner: Path = Path("eingang/webshop")
    mail_ordner: Path = Path("logs/webshop/mails")
    laden: LadenKonfig
    lieferung: LieferungKonfig = Field(default_factory=LieferungKonfig)
    mail: MailKonfig = Field(default_factory=MailKonfig)
    sortiment: list[SortimentEintrag] = Field(default_factory=list)

    def absolut(self, stamm: Path) -> WebshopKonfig:
        """Relative Pfade gegen den Projektstamm auflösen."""
        for feld in ("ausgabe_ordner", "bestellungen_ordner", "mail_ordner"):
            pfad = getattr(self, feld)
            if not pfad.is_absolute():
                setattr(self, feld, (stamm / pfad).resolve())
        return self


def lade_webshop_konfig(stamm: Path | None = None, datei: Path | None = None) -> WebshopKonfig:
    """``config/webshop.yaml`` lesen; Pfade relativ zum Projektstamm."""
    stamm = stamm or projekt_stamm()
    datei = datei or stamm / "config" / "webshop.yaml"
    if not datei.exists():
        raise FileNotFoundError(f"Webshop-Konfiguration fehlt: {datei}")
    roh = yaml.safe_load(datei.read_text(encoding="utf-8")) or {}
    return WebshopKonfig.model_validate(roh).absolut(stamm)
