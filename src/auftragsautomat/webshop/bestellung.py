"""Serverseitige Prüfung und Ablage einer Shop-Bestellung.

Das Frontend schickt Artikelnummern, Mengen, Lieferart und Kundendaten. Preise, Liefergebühr und
Summen werden hier neu berechnet (nie aus dem Browser übernommen). Jede angenommene Bestellung wird
als JSON unter ``eingang/webshop/`` abgelegt und als zwei E-Mail-Entwürfe (Kunde, Laden) geschrieben;
gesendet wird nur, wenn SMTP konfiguriert ist.
"""

from __future__ import annotations

import json
import logging
import os
import re
import smtplib
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from email.message import EmailMessage
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

from auftragsautomat.webshop.konfig import WebshopKonfig, Zeitfenster

logger = logging.getLogger(__name__)

_CENT = Decimal("0.01")
_PLZ = re.compile(r"^\d{5}$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
Lieferart = Literal["lastenrad", "abholung", "versand"]


class BestellFehler(ValueError):
    """Fachlicher Fehler in der Bestellung; der Text ist für den Kunden gedacht."""


class Position(BaseModel):
    artikelnummer: str = Field(min_length=1, max_length=40)
    menge: int = Field(ge=1, le=99)


class Kunde(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    email: str = Field(max_length=120)
    telefon: str = Field(default="", max_length=40)
    strasse: str = Field(default="", max_length=160)
    plz: str = Field(default="", max_length=10)
    ort: str = Field(default="", max_length=80)
    anmerkung: str = Field(default="", max_length=500)

    @field_validator("name", "email", "telefon", "strasse", "plz", "ort", "anmerkung", mode="before")
    @classmethod
    def _trim(cls, wert: object) -> object:
        return wert.strip() if isinstance(wert, str) else wert

    @field_validator("email")
    @classmethod
    def _email_form(cls, wert: str) -> str:
        if not _EMAIL.match(wert):
            raise ValueError("keine gültige E-Mail-Adresse")
        return wert


class BestellAnfrage(BaseModel):
    """Was das Frontend schickt."""

    lieferart: Lieferart
    positionen: list[Position] = Field(min_length=1, max_length=60)
    kunde: Kunde
    zeitfenster: str | None = None  # "JJJJ-MM-TT|<fenster_id>" (Lastenrad) bzw. "JJJJ-MM-TT" (Abholung)
    alter_bestaetigt: bool = False
    agb_akzeptiert: bool = False


class BestellPosition(BaseModel):
    artikelnummer: str
    name: str
    menge: int
    einzelpreis: Decimal
    gesamt: Decimal


class Bestellung(BaseModel):
    """Geprüfte, ausgepreiste Bestellung (wird als JSON abgelegt)."""

    nummer: str
    eingegangen: datetime
    firma: str
    lieferart: Lieferart
    zone: str | None = None
    liefertag: date | None = None
    zeitfenster_text: str | None = None
    positionen: list[BestellPosition]
    flaschen: int
    zwischensumme: Decimal
    lieferkosten: Decimal
    gesamt: Decimal
    mwst_prozent: Decimal
    kunde: Kunde
    alter_bestaetigt: bool
    agb_akzeptiert: bool


@dataclass
class Produktkatalog:
    """Produkte aus ``produkte.json`` (id -> Produkt)."""

    produkte: dict[str, dict]

    @classmethod
    def aus_datei(cls, pfad: Path) -> Produktkatalog:
        daten = json.loads(pfad.read_text(encoding="utf-8"))
        return cls({p["id"]: p for p in daten.get("produkte", [])})

    def preis(self, artikelnummer: str) -> Decimal:
        return Decimal(str(self.produkte[artikelnummer]["preis"])).quantize(_CENT)


# ---------------------------------------------------------------------------
# Termine
# ---------------------------------------------------------------------------


def fruehester_liefertag(jetzt: datetime, bestellschluss_uhr: int) -> date:
    """Nächster Werktag; nach Bestellschluss der übernächste."""
    tag = jetzt.date() + timedelta(days=1 if jetzt.time() < time(bestellschluss_uhr) else 2)
    while tag.isoweekday() > 5:
        tag += timedelta(days=1)
    return tag


def angebotene_tage(jetzt: datetime, bestellschluss_uhr: int, tage_voraus: int) -> list[date]:
    tage: list[date] = []
    tag = fruehester_liefertag(jetzt, bestellschluss_uhr)
    while len(tage) < tage_voraus:
        if tag.isoweekday() <= 5:
            tage.append(tag)
        tag += timedelta(days=1)
    return tage


def _parse_datum(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise BestellFehler("Ungültiges Datum für den Liefertermin.") from exc


# ---------------------------------------------------------------------------
# Prüfung
# ---------------------------------------------------------------------------


def _fenster_text(fenster: Zeitfenster, tag: date) -> str:
    return f"{tag.strftime('%d.%m.%Y')}, {fenster.von}–{fenster.bis} Uhr"


def pruefe_bestellung(
    anfrage: BestellAnfrage,
    konfig: WebshopKonfig,
    katalog: Produktkatalog,
    *,
    jetzt: datetime,
    nummer: str,
) -> Bestellung:
    """Fachliche Prüfung und Auspreisung. Wirft ``BestellFehler`` mit kundenlesbarem Text."""
    lf = konfig.lieferung
    if not anfrage.alter_bestaetigt:
        raise BestellFehler("Bitte bestätigen Sie, dass Sie mindestens 18 Jahre alt sind.")
    if not anfrage.agb_akzeptiert:
        raise BestellFehler("Bitte akzeptieren Sie die AGB und die Widerrufsbelehrung.")

    mengen: dict[str, int] = {}
    for pos in anfrage.positionen:
        if pos.artikelnummer not in katalog.produkte:
            raise BestellFehler(f"Artikel {pos.artikelnummer} ist nicht (mehr) im Sortiment.")
        mengen[pos.artikelnummer] = mengen.get(pos.artikelnummer, 0) + pos.menge
    positionen = [
        BestellPosition(
            artikelnummer=nr,
            name=katalog.produkte[nr]["name"],
            menge=menge,
            einzelpreis=katalog.preis(nr),
            gesamt=(katalog.preis(nr) * menge).quantize(_CENT),
        )
        for nr, menge in mengen.items()
    ]
    flaschen = sum(p.menge for p in positionen)
    zwischensumme = sum((p.gesamt for p in positionen), Decimal(0)).quantize(_CENT)

    kunde = anfrage.kunde
    zone_id: str | None = None
    liefertag: date | None = None
    fenster_text: str | None = None
    lieferkosten = Decimal("0.00")
    tage = angebotene_tage(jetzt, lf.bestellschluss_uhr, lf.tage_voraus)

    if anfrage.lieferart == "lastenrad":
        if flaschen < lf.mindestflaschen_lastenrad:
            raise BestellFehler(f"Die Lastenrad-Lieferung ist ab {lf.mindestflaschen_lastenrad} Flaschen möglich.")
        if not _PLZ.match(kunde.plz):
            raise BestellFehler("Bitte eine fünfstellige Postleitzahl angeben.")
        zone = lf.zone_fuer(kunde.plz)
        if zone is None:
            raise BestellFehler("Diese Postleitzahl liegt außerhalb unseres Lastenrad-Gebiets.")
        if not kunde.strasse:
            raise BestellFehler("Bitte Straße und Hausnummer angeben.")
        if not anfrage.zeitfenster or "|" not in anfrage.zeitfenster:
            raise BestellFehler("Bitte ein Zeitfenster wählen.")
        tag_text, fenster_id = anfrage.zeitfenster.split("|", 1)
        liefertag = _parse_datum(tag_text)
        fenster = lf.zeitfenster_nach_id(fenster_id)
        if fenster is None or liefertag.isoweekday() not in fenster.tage:
            raise BestellFehler("Dieses Zeitfenster gibt es an dem Tag nicht.")
        if liefertag not in tage:
            raise BestellFehler("Der Liefertag liegt außerhalb der angebotenen Tage. Bitte neu wählen.")
        zone_id = zone.id
        lieferkosten = zone.gebuehr.quantize(_CENT)
        fenster_text = _fenster_text(fenster, liefertag)
        if not kunde.ort:
            kunde = kunde.model_copy(update={"ort": konfig.laden.ort})
    elif anfrage.lieferart == "abholung":
        if not anfrage.zeitfenster:
            raise BestellFehler("Bitte einen Abholtag wählen.")
        liefertag = _parse_datum(anfrage.zeitfenster.split("|", 1)[0])
        if liefertag.isoweekday() not in konfig.laden.oeffnungszeiten or liefertag < tage[0]:
            raise BestellFehler("An diesem Tag ist der Laden geschlossen oder der Tag liegt zu früh.")
        von, bis = konfig.laden.oeffnungszeiten[liefertag.isoweekday()]
        fenster_text = f"{liefertag.strftime('%d.%m.%Y')}, {von}–{bis} Uhr (Abholung)"
    else:  # versand
        if not (kunde.strasse and _PLZ.match(kunde.plz) and kunde.ort):
            raise BestellFehler("Für den Versand brauchen wir Straße, Postleitzahl und Ort.")
        lieferkosten = Decimal("0.00") if zwischensumme >= lf.versandkostenfrei_ab else lf.versandkosten.quantize(_CENT)

    return Bestellung(
        nummer=nummer,
        eingegangen=jetzt,
        firma=konfig.firma,
        lieferart=anfrage.lieferart,
        zone=zone_id,
        liefertag=liefertag,
        zeitfenster_text=fenster_text,
        positionen=positionen,
        flaschen=flaschen,
        zwischensumme=zwischensumme,
        lieferkosten=lieferkosten,
        gesamt=(zwischensumme + lieferkosten).quantize(_CENT, rounding=ROUND_HALF_UP),
        mwst_prozent=konfig.mwst_prozent,
        kunde=kunde,
        alter_bestaetigt=True,
        agb_akzeptiert=True,
    )


# ---------------------------------------------------------------------------
# Ablage, Nummer, E-Mail
# ---------------------------------------------------------------------------


def naechste_nummer(ordner: Path, jetzt: datetime) -> str:
    """``WS-JJMMTT-NNN``, fortlaufend je Tag anhand der bereits abgelegten Dateien."""
    praefix = f"WS-{jetzt:%y%m%d}-"
    vorhanden = [p.stem for p in ordner.glob(f"{praefix}*.json")] if ordner.exists() else []
    nummern = [int(s[len(praefix):]) for s in vorhanden if s[len(praefix):].isdigit()]
    return f"{praefix}{(max(nummern) + 1 if nummern else 1):03d}"


def speichere_bestellung(bestellung: Bestellung, ordner: Path) -> Path:
    ordner.mkdir(parents=True, exist_ok=True)
    ziel = ordner / f"{bestellung.nummer}.json"
    ziel.write_text(bestellung.model_dump_json(indent=2), encoding="utf-8")
    return ziel


def _betrag(wert: Decimal) -> str:
    return f"{wert:.2f}".replace(".", ",") + " €"


def bestelltext(bestellung: Bestellung, konfig: WebshopKonfig) -> str:
    lieferart = {
        "lastenrad": "Lastenrad-Lieferung",
        "abholung": "Abholung im Laden",
        "versand": "Paketversand",
    }[bestellung.lieferart]
    zeilen = [f"Bestellung {bestellung.nummer} vom {bestellung.eingegangen:%d.%m.%Y %H:%M}", ""]
    zeilen += [f"{p.menge} x {p.name}  à {_betrag(p.einzelpreis)}  =  {_betrag(p.gesamt)}" for p in bestellung.positionen]
    zeilen += [
        "",
        f"Zwischensumme: {_betrag(bestellung.zwischensumme)}",
        f"{lieferart}: {_betrag(bestellung.lieferkosten) if bestellung.lieferkosten else 'kostenlos'}",
        f"Gesamt inkl. {bestellung.mwst_prozent:.0f} % MwSt.: {_betrag(bestellung.gesamt)}",
        "",
    ]
    if bestellung.zeitfenster_text:
        zeilen.append(f"Termin: {bestellung.zeitfenster_text}")
    k = bestellung.kunde
    zeilen += [k.name]
    if bestellung.lieferart != "abholung":
        zeilen += [k.strasse, f"{k.plz} {k.ort}".strip()]
    zeilen += [str(k.email)]
    if k.telefon:
        zeilen.append(k.telefon)
    if k.anmerkung:
        zeilen += ["", f"Anmerkung: {k.anmerkung}"]
    zeilen += ["", konfig.laden.name, konfig.laden.strasse, f"{konfig.laden.plz} {konfig.laden.ort}"]
    return "\n".join(zeilen) + "\n"


def erzeuge_mails(bestellung: Bestellung, konfig: WebshopKonfig) -> tuple[EmailMessage, EmailMessage]:
    """(Bestätigung an den Kunden, Meldung an den Laden)."""
    absender = konfig.mail.absender or konfig.laden.email
    text = bestelltext(bestellung, konfig)
    kunde = EmailMessage()
    kunde["From"] = absender
    kunde["To"] = str(bestellung.kunde.email)
    kunde["Subject"] = f"Ihre Bestellung {bestellung.nummer} bei der Weinstrasse"
    kunde.set_content(
        f"Guten Tag {bestellung.kunde.name},\n\nvielen Dank für Ihre Bestellung. Hier die Übersicht:\n\n{text}\n"
        "Alkoholische Getränke übergeben wir nur an Personen ab 18 Jahren.\n\nHerzliche Grüße\nIhre Weinstrasse\n"
    )
    laden = EmailMessage()
    laden["From"] = absender
    laden["To"] = konfig.mail.empfaenger_laden or konfig.laden.email
    laden["Subject"] = f"Neue Shop-Bestellung {bestellung.nummer} ({bestellung.lieferart}, {bestellung.flaschen} Fl.)"
    laden.set_content(text)
    return kunde, laden


def schreibe_mail_entwuerfe(mails: tuple[EmailMessage, EmailMessage], bestellung: Bestellung, ordner: Path) -> list[Path]:
    ordner.mkdir(parents=True, exist_ok=True)
    pfade: list[Path] = []
    for name, mail in zip(("kunde", "laden"), mails, strict=True):
        ziel = ordner / f"{bestellung.nummer}_{name}.eml"
        ziel.write_bytes(bytes(mail))
        pfade.append(ziel)
    return pfade


def sende_mails(mails: tuple[EmailMessage, EmailMessage], konfig: WebshopKonfig) -> bool:
    """Per SMTP senden, wenn konfiguriert. False = nicht konfiguriert (nur Entwürfe)."""
    m = konfig.mail
    if not m.smtp_host:
        return False
    passwort = os.environ.get(m.passwort_env, "")
    with smtplib.SMTP(m.smtp_host, m.smtp_port, timeout=20) as smtp:
        smtp.starttls()
        if m.benutzer:
            smtp.login(m.benutzer, passwort)
        for mail in mails:
            smtp.send_message(mail)
    return True


# ---------------------------------------------------------------------------
# Anfragen (Concierge: Firmenfeiern, Präsente, Verkostungen)
# ---------------------------------------------------------------------------

Anlass = Literal["firmenfeier", "praesente", "verkostung", "privat", "sonstiges"]


class AnfrageDaten(BaseModel):
    """Unverbindliche Anfrage über das Concierge-Formular."""

    anlass: Anlass
    name: str = Field(min_length=2, max_length=120)
    email: str = Field(max_length=120)
    telefon: str = Field(default="", max_length=40)
    firma: str = Field(default="", max_length=120)
    personen: int | None = Field(default=None, ge=1, le=5000)
    datum: str = Field(default="", max_length=40)
    budget: str = Field(default="", max_length=40)
    nachricht: str = Field(default="", max_length=2000)
    datenschutz: bool = False

    @field_validator("name", "email", "telefon", "firma", "datum", "budget", "nachricht", mode="before")
    @classmethod
    def _trim(cls, wert: object) -> object:
        return wert.strip() if isinstance(wert, str) else wert

    @field_validator("email")
    @classmethod
    def _email_form(cls, wert: str) -> str:
        if not _EMAIL.match(wert):
            raise ValueError("keine gültige E-Mail-Adresse")
        return wert


class Anfrage(AnfrageDaten):
    nummer: str
    eingegangen: datetime


ANLASS_TEXT = {
    "firmenfeier": "Firmenfeier / Event",
    "praesente": "Präsente",
    "verkostung": "Verkostung",
    "privat": "Private Feier",
    "sonstiges": "Sonstiges",
}


def naechste_anfragenummer(ordner: Path, jetzt: datetime) -> str:
    praefix = f"ANF-{jetzt:%y%m%d}-"
    vorhanden = [p.stem for p in ordner.glob(f"{praefix}*.json")] if ordner.exists() else []
    nummern = [int(s[len(praefix):]) for s in vorhanden if s[len(praefix):].isdigit()]
    return f"{praefix}{(max(nummern) + 1 if nummern else 1):03d}"


def anfragetext(anfrage: Anfrage) -> str:
    zeilen = [
        f"Anfrage {anfrage.nummer} vom {anfrage.eingegangen:%d.%m.%Y %H:%M}",
        f"Anlass: {ANLASS_TEXT[anfrage.anlass]}",
        f"Name: {anfrage.name}" + (f" ({anfrage.firma})" if anfrage.firma else ""),
        f"E-Mail: {anfrage.email}",
    ]
    if anfrage.telefon:
        zeilen.append(f"Telefon: {anfrage.telefon}")
    if anfrage.personen:
        zeilen.append(f"Personen: {anfrage.personen}")
    if anfrage.datum:
        zeilen.append(f"Termin: {anfrage.datum}")
    if anfrage.budget:
        zeilen.append(f"Budget: {anfrage.budget}")
    if anfrage.nachricht:
        zeilen += ["", anfrage.nachricht]
    return "\n".join(zeilen) + "\n"


@dataclass
class AnfrageAnnahme:
    anfrage: Anfrage
    datei: Path
    mail_entwurf: Path
    gesendet: bool


def nimm_anfrage_an(roh: dict, konfig: WebshopKonfig, *, jetzt: datetime | None = None, senden: bool = True) -> AnfrageAnnahme:
    """Concierge-Anfrage validieren, ablegen (``<bestellungen_ordner>/anfragen/``) und an den Laden melden."""
    jetzt = jetzt or datetime.now()
    try:
        daten = AnfrageDaten.model_validate(roh)
    except ValidationError as exc:
        erster = exc.errors()[0]
        ort = ".".join(str(t) for t in erster.get("loc", ()))
        raise BestellFehler(f"Ungültige Angabe bei '{ort}': {erster.get('msg', 'unbekannt')}.") from exc
    if not daten.datenschutz:
        raise BestellFehler("Bitte bestätigen Sie die Datenschutzhinweise.")
    ordner = konfig.bestellungen_ordner / "anfragen"
    anfrage = Anfrage(**daten.model_dump(), nummer=naechste_anfragenummer(ordner, jetzt), eingegangen=jetzt)
    ordner.mkdir(parents=True, exist_ok=True)
    datei = ordner / f"{anfrage.nummer}.json"
    datei.write_text(anfrage.model_dump_json(indent=2), encoding="utf-8")
    absender = konfig.mail.absender or konfig.laden.email
    mail = EmailMessage()
    mail["From"] = absender
    mail["To"] = konfig.mail.empfaenger_laden or konfig.laden.email
    mail["Reply-To"] = anfrage.email
    mail["Subject"] = f"Concierge-Anfrage {anfrage.nummer}: {ANLASS_TEXT[anfrage.anlass]}"
    mail.set_content(anfragetext(anfrage))
    konfig.mail_ordner.mkdir(parents=True, exist_ok=True)
    entwurf = konfig.mail_ordner / f"{anfrage.nummer}_laden.eml"
    entwurf.write_bytes(bytes(mail))
    gesendet = False
    if senden and konfig.mail.smtp_host:
        try:
            m = konfig.mail
            with smtplib.SMTP(m.smtp_host, m.smtp_port, timeout=20) as smtp:
                smtp.starttls()
                if m.benutzer:
                    smtp.login(m.benutzer, os.environ.get(m.passwort_env, ""))
                smtp.send_message(mail)
            gesendet = True
        except (OSError, smtplib.SMTPException):
            logger.exception("Anfrage %s: E-Mail-Versand fehlgeschlagen, Entwurf liegt vor", anfrage.nummer)
    logger.info("Anfrage %s angenommen (%s)", anfrage.nummer, anfrage.anlass)
    return AnfrageAnnahme(anfrage=anfrage, datei=datei, mail_entwurf=entwurf, gesendet=gesendet)


@dataclass
class Annahme:
    bestellung: Bestellung
    datei: Path
    mail_entwuerfe: list[Path]
    gesendet: bool


def nimm_bestellung_an(
    roh: dict,
    konfig: WebshopKonfig,
    katalog: Produktkatalog,
    *,
    jetzt: datetime | None = None,
    senden: bool = True,
) -> Annahme:
    """Kompletter Ablauf: validieren, prüfen, nummerieren, ablegen, Mails schreiben (und ggf. senden)."""
    jetzt = jetzt or datetime.now()
    try:
        anfrage = BestellAnfrage.model_validate(roh)
    except ValidationError as exc:
        erster = exc.errors()[0]
        ort = ".".join(str(t) for t in erster.get("loc", ()))
        raise BestellFehler(f"Ungültige Angabe bei '{ort}': {erster.get('msg', 'unbekannt')}.") from exc
    nummer = naechste_nummer(konfig.bestellungen_ordner, jetzt)
    bestellung = pruefe_bestellung(anfrage, konfig, katalog, jetzt=jetzt, nummer=nummer)
    datei = speichere_bestellung(bestellung, konfig.bestellungen_ordner)
    mails = erzeuge_mails(bestellung, konfig)
    entwuerfe = schreibe_mail_entwuerfe(mails, bestellung, konfig.mail_ordner)
    gesendet = False
    if senden:
        try:
            gesendet = sende_mails(mails, konfig)
        except (OSError, smtplib.SMTPException):
            logger.exception("Bestellung %s: E-Mail-Versand fehlgeschlagen, Entwürfe liegen vor", nummer)
    logger.info("Bestellung %s angenommen (%s, %d Fl., %s)", nummer, bestellung.lieferart, bestellung.flaschen, bestellung.gesamt)
    return Annahme(bestellung=bestellung, datei=datei, mail_entwuerfe=entwuerfe, gesendet=gesendet)
