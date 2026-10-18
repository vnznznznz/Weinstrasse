"""Datenquellen der Meldungen: Lexware-Belegexport (CSV/XLSX), Artikel-Meldedaten, Verpackungsprofile.

Alles nur lesend. Der Belegexport kommt aus Lexware premium (Berichte/Belegliste
"Export nach MS Excel" bzw. Datei -> Export, ASCII). Die Spaltennamen des Exports
werden über ``BewegungenExport.spalten`` auf unsere Feldnamen abgebildet.
"""

from __future__ import annotations

import csv
from collections.abc import Iterable
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

import yaml

from auftragsautomat.config import BewegungenExport, Firma, Settings
from auftragsautomat.meldungen.modelle import (
    ArtikelMeldedaten,
    Belegtyp,
    Bewegung,
    Erzeugnisart,
    Material,
    Meldemonat,
    VerpackungsKomponente,
    VerpackungsProfil,
)

PFLICHTSPALTEN_BEWEGUNG = ("belegtyp", "belegnummer", "belegdatum", "artikelnummer", "menge", "netto_eur")
OPTIONALE_SPALTEN_BEWEGUNG = (
    "lieferdatum",
    "partnernummer",
    "partnername",
    "partner_land",
    "partner_ustid",
    "bezeichnung",
    "fracht_eur",
)


class QuellenFehler(ValueError):
    """Kaputte oder unvollständige Eingabedatei."""


# ---------------------------------------------------------------------------
# Hilfsfunktionen für deutsche Zahlen und Daten
# ---------------------------------------------------------------------------


def parse_dezimal(text: str | float | int | Decimal | None) -> Decimal | None:
    if text is None:
        return None
    if isinstance(text, Decimal):
        return text
    if isinstance(text, int | float):
        return Decimal(str(text))
    t = str(text).strip().replace("€", "").replace(" ", "")
    if not t:
        return None
    if "," in t and "." in t:
        t = t.replace(".", "").replace(",", ".")
    elif "," in t:
        t = t.replace(",", ".")
    try:
        return Decimal(t)
    except InvalidOperation as exc:
        raise QuellenFehler(f"Keine Zahl: {text!r}") from exc


def parse_datum(text: str | date | datetime | None) -> date | None:
    if text is None:
        return None
    if isinstance(text, datetime):
        return text.date()
    if isinstance(text, date):
        return text
    t = str(text).strip()
    if not t:
        return None
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d.%m.%y", "%d/%m/%Y", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(t, fmt).date()
        except ValueError:
            continue
    raise QuellenFehler(f"Kein Datum: {text!r}")


def parse_bool(text: str | bool | None, standard: bool = False) -> bool:
    if text is None or text == "":
        return standard
    if isinstance(text, bool):
        return text
    return str(text).strip().lower() in ("1", "ja", "true", "x", "y", "wahr")


# ---------------------------------------------------------------------------
# Belegexport (Bewegungen)
# ---------------------------------------------------------------------------


def _lies_tabelle(pfad: Path, konfig: BewegungenExport) -> list[dict[str, object]]:
    if not pfad.exists():
        raise QuellenFehler(f"Belegexport nicht gefunden: {pfad}")
    if pfad.suffix.lower() in (".xlsx", ".xlsm"):
        from openpyxl import load_workbook

        wb = load_workbook(pfad, read_only=True, data_only=True)
        ws = wb.active
        zeilen = list(ws.iter_rows(values_only=True))
        wb.close()
        if not zeilen:
            return []
        kopf = [str(z).strip() if z is not None else "" for z in zeilen[0]]
        return [dict(zip(kopf, z, strict=False)) for z in zeilen[1:] if any(v not in (None, "") for v in z)]
    with pfad.open("r", encoding=konfig.kodierung, newline="") as f:
        leser = csv.DictReader(f, delimiter=konfig.trennzeichen)
        return [dict(z) for z in leser if any((v or "").strip() for v in z.values() if isinstance(v, str))]


def bewegungen_pfad(firma: Firma, monat: Meldemonat, settings: Settings) -> Path:
    muster = firma.meldungen.bewegungen.muster.format(firma=firma.id, jahr=monat.jahr, monat=monat.monat)
    pfad = Path(muster)
    if not pfad.is_absolute() and settings.projekt_stamm is not None:
        pfad = settings.projekt_stamm / pfad
    return pfad


def lade_bewegungen(pfad: Path, firma: Firma) -> list[Bewegung]:
    """Liest den Belegexport einer Firma. Unbekannte Spalten werden ignoriert."""
    konfig = firma.meldungen.bewegungen
    tabelle = _lies_tabelle(pfad, konfig)
    if not tabelle:
        return []
    spalten = {
        name: konfig.spalten.get(name, name) for name in PFLICHTSPALTEN_BEWEGUNG + OPTIONALE_SPALTEN_BEWEGUNG
    }
    kopf = set(tabelle[0].keys())
    fehlend = [spalten[n] for n in PFLICHTSPALTEN_BEWEGUNG if spalten[n] not in kopf]
    if fehlend:
        raise QuellenFehler(
            f"{pfad.name}: Pflichtspalten fehlen: {', '.join(fehlend)}. Vorhanden: {', '.join(sorted(kopf))}. "
            "Spaltenzuordnung in config/firmen.yaml unter meldungen.bewegungen.spalten anpassen."
        )

    def wert(zeile: dict[str, object], name: str) -> object:
        return zeile.get(spalten[name])

    ergebnis: list[Bewegung] = []
    for nr, zeile in enumerate(tabelle, start=2):
        belegtyp_roh = str(wert(zeile, "belegtyp") or "").strip()
        belegtyp_text = konfig.belegtyp_werte.get(belegtyp_roh.upper(), belegtyp_roh.lower())
        try:
            belegtyp = Belegtyp(belegtyp_text)
        except ValueError as exc:
            raise QuellenFehler(
                f"{pfad.name} Zeile {nr}: unbekannter Belegtyp {belegtyp_roh!r}. "
                "Zuordnung unter meldungen.bewegungen.belegtyp_werte ergänzen."
            ) from exc
        menge = parse_dezimal(wert(zeile, "menge")) or Decimal(0)
        netto = parse_dezimal(wert(zeile, "netto_eur")) or Decimal(0)
        if belegtyp in (Belegtyp.GUTSCHRIFT_EINKAUF, Belegtyp.GUTSCHRIFT_VERKAUF):
            menge, netto = -abs(menge), -abs(netto)
        land = str(wert(zeile, "partner_land") or "DE").strip().upper()[:2] or "DE"
        ergebnis.append(
            Bewegung(
                firma_id=firma.id,
                belegtyp=belegtyp,
                belegnummer=str(wert(zeile, "belegnummer") or "").strip(),
                belegdatum=parse_datum(wert(zeile, "belegdatum")) or date.min,
                lieferdatum=parse_datum(wert(zeile, "lieferdatum")),
                partnernummer=_text(wert(zeile, "partnernummer")),
                partnername=_text(wert(zeile, "partnername")),
                partner_land=land,
                partner_ustid=_text(wert(zeile, "partner_ustid")),
                artikelnummer=str(wert(zeile, "artikelnummer") or "").strip(),
                bezeichnung=_text(wert(zeile, "bezeichnung")),
                menge=menge,
                netto_eur=netto,
                fracht_eur=parse_dezimal(wert(zeile, "fracht_eur")) or Decimal(0),
                quelldatei=pfad.name,
                zeile=nr,
            )
        )
    return ergebnis


def _text(wert: object) -> str | None:
    if wert is None:
        return None
    t = str(wert).strip()
    if t.endswith(".0") and t[:-2].isdigit():  # Excel macht aus "3441" gern 3441.0
        t = t[:-2]
    return t or None


def filtere_monat(bewegungen: Iterable[Bewegung], monat: Meldemonat) -> list[Bewegung]:
    return [b for b in bewegungen if monat.enthaelt(b.bewegungsdatum)]


# ---------------------------------------------------------------------------
# Artikel-Meldedaten
# ---------------------------------------------------------------------------

SPALTEN_MELDEDATEN = (
    "firma_id",
    "artikelnummer",
    "warennummer",
    "ursprungsland",
    "volumen_cl",
    "alkohol_vol",
    "erzeugnisart",
    "ueberdruck_bar",
    "schaumweinstopfen",
    "dichte_kg_je_l",
    "verpackung_profil",
    "einheit_je_position",
)


class Meldedaten:
    """Index der Artikel-Meldedaten: erst firmenspezifisch, dann firmenübergreifend."""

    def __init__(self, eintraege: Iterable[ArtikelMeldedaten]) -> None:
        self.eintraege = list(eintraege)
        self._index: dict[tuple[str | None, str], ArtikelMeldedaten] = {
            (e.firma_id, e.artikelnummer): e for e in self.eintraege
        }

    def finde(self, firma_id: str, artikelnummer: str) -> ArtikelMeldedaten | None:
        return self._index.get((firma_id, artikelnummer)) or self._index.get((None, artikelnummer))

    def __len__(self) -> int:
        return len(self.eintraege)


def lade_meldedaten(pfad: Path) -> Meldedaten:
    if not pfad.exists():
        return Meldedaten([])
    with pfad.open("r", encoding="utf-8-sig", newline="") as f:
        leser = csv.DictReader(f, delimiter=";")
        eintraege = []
        for nr, z in enumerate(leser, start=2):
            if not (z.get("artikelnummer") or "").strip() or (z.get("firma_id") or "").lstrip().startswith(
                "#"
            ):
                continue  # Leer- und Kommentarzeilen
            try:
                eintraege.append(
                    ArtikelMeldedaten(
                        firma_id=(z.get("firma_id") or "").strip() or None,
                        artikelnummer=z["artikelnummer"].strip(),
                        warennummer=(z.get("warennummer") or "").replace(" ", "").strip() or None,
                        ursprungsland=(z.get("ursprungsland") or "").strip().upper() or None,
                        volumen_cl=parse_dezimal(z.get("volumen_cl")),
                        alkohol_vol=parse_dezimal(z.get("alkohol_vol")),
                        erzeugnisart=Erzeugnisart((z.get("erzeugnisart") or "stillwein").strip().lower()),
                        ueberdruck_bar=parse_dezimal(z.get("ueberdruck_bar")),
                        schaumweinstopfen=parse_bool(z.get("schaumweinstopfen")),
                        dichte_kg_je_l=parse_dezimal(z.get("dichte_kg_je_l")),
                        verpackung_profil=(z.get("verpackung_profil") or "").strip() or None,
                        einheit_je_position=parse_dezimal(z.get("einheit_je_position")) or Decimal(1),
                    )
                )
            except (ValueError, KeyError) as exc:
                raise QuellenFehler(f"{pfad.name} Zeile {nr}: {exc}") from exc
    return Meldedaten(eintraege)


def schreibe_meldedaten_vorlage(pfad: Path, artikelnummern: Iterable[tuple[str, str, str | None]]) -> int:
    """Ergänzt fehlende Artikel als Leerzeilen (firma_id, artikelnummer, bezeichnung) in der Pflegedatei."""
    vorhanden = lade_meldedaten(pfad)
    neue = sorted({(f, a, b) for f, a, b in artikelnummern if vorhanden.finde(f, a) is None})
    if not neue:
        return 0
    pfad.parent.mkdir(parents=True, exist_ok=True)
    existiert = pfad.exists()
    with pfad.open("a", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter=";")
        if not existiert:
            w.writerow(SPALTEN_MELDEDATEN + ("bezeichnung_hinweis",))
        for firma_id, artikelnummer, bezeichnung in neue:
            w.writerow(
                [firma_id, artikelnummer, "", "", "", "", "stillwein", "", "", "", "", "1", bezeichnung or ""]
            )
    return len(neue)


# ---------------------------------------------------------------------------
# Verpackungsprofile und Warennummern
# ---------------------------------------------------------------------------


def lade_verpackungsprofile(pfad: Path) -> dict[str, VerpackungsProfil]:
    if not pfad.exists():
        return {}
    roh = yaml.safe_load(pfad.read_text(encoding="utf-8")) or {}
    profile: dict[str, VerpackungsProfil] = {}
    for pid, daten in (roh.get("profile") or {}).items():
        komponenten = [
            VerpackungsKomponente(
                bezeichnung=k["bezeichnung"],
                material=Material(k["material"]),
                gewicht_g=Decimal(str(k["gewicht_g"])),
                systembeteiligungspflichtig=bool(k.get("systembeteiligungspflichtig", True)),
            )
            for k in daten.get("komponenten", [])
        ]
        profile[pid] = VerpackungsProfil(
            id=pid, beschreibung=daten.get("beschreibung"), komponenten=komponenten
        )
    return profile


def lade_warennummern(pfad: Path) -> dict[str, dict[str, str]]:
    """Nachschlagetabelle Warennummer -> {bezeichnung, besondere_masseinheit, hinweis}."""
    if not pfad.exists():
        return {}
    roh = yaml.safe_load(pfad.read_text(encoding="utf-8")) or {}
    return {str(k).replace(" ", ""): dict(v or {}) for k, v in (roh.get("warennummern") or {}).items()}
