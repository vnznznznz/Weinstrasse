"""Gemeinsame Grundlagen der Microsoft-365-Zugriffsschicht (``m365``).

Drei austauschbare Backends implementieren dieselben Protokolle (Auswahl über
``settings.m365.backend``, Erzeugung in :func:`auftragsautomat.m365.fabrik.erzeuge_backends`):

* ``graph.py``  – Microsoft Graph (empfohlen; die Excel-API arbeitet serverseitig auf der
                  Originaldatei, Entwürfe landen direkt im Postfach, Dateien in OneDrive/SharePoint).
* ``lokal.py``  – klassisches Outlook/Excel per COM auf dem Windows-PC, Dateien über den
                  OneDrive-synchronisierten Pfad.
* ``fake.py``   – In-Memory-Backend für Tests und den Dry Run unter Linux (``.eml``-Entwürfe,
                  CSV-Vorschau der Excel-Änderungen, lokaler Ordner).

Protokolle
----------
* :class:`ExcelBackend`: Bereiche einer Arbeitsmappe lesen/schreiben (Fixterminüberwachung,
  Kontaktliste, Aktionszeiträume).
* :class:`MailBackend`:  Entwürfe anlegen (**nie senden**), Posteingang durchsuchen, Mails und
  Anhänge lesen.
* :class:`DateiBackend`: Ordner/Dateien (Dossiers, Ablage "Aufträge mit/ohne Provision") anlegen,
  auflisten, hoch- und herunterladen.

Dateien werden über :class:`auftragsautomat.config.DateiRefKonfig` adressiert (Graph:
``drive_id``/``item_id`` bzw. ``web_url``; lokal/fake: ``lokaler_pfad``). Werte-Matrizen sind
zeilenweise Listen (``werte[zeile][spalte]``). Beim Schreiben bedeutet ``None`` "Zelle unverändert
lassen" und ``""`` "Zelle leeren" (Semantik der Graph-Excel-API, von allen Backends übernommen).
Beim Lesen liefern alle Backends für leere Zellen ``""`` (wie Graph). Datumswerte liefert Graph
als Excel-Serienzahl (:func:`serienzahl_zu_datum`), die COM- und Fake-Backends als
``date``/``datetime``; Aufrufer behandeln beides.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from auftragsautomat.config import DateiRefKonfig
from auftragsautomat.models import EmailEntwurf

__all__ = [
    "DateiBackend",
    "DateiInfo",
    "EXCEL_EPOCHE",
    "ExcelBackend",
    "M365Fehler",
    "M365Konflikt",
    "M365NichtAngemeldet",
    "M365NichtGefunden",
    "Mail",
    "MailAnhang",
    "MailBackend",
    "MailKopf",
    "Zellbereich",
    "datei_schluessel",
    "datum_zu_serienzahl",
    "excel_sitzung",
    "html_zu_text",
    "parse_adresse",
    "serienzahl_zu_datum",
    "sicherer_dateiname",
    "spaltenbuchstabe",
    "spaltenindex",
]


# ---------------------------------------------------------------------------
# Fehlerklassen
# ---------------------------------------------------------------------------


class M365Fehler(Exception):
    """Allgemeiner Fehler der Zugriffsschicht (Graph-Antwort, COM-Fehler, fehlende Konfiguration)."""

    def __init__(
        self,
        meldung: str,
        *,
        status: int | None = None,
        code: str | None = None,
        inner_code: str | None = None,
        wartezeit_sekunden: float | None = None,
    ) -> None:
        super().__init__(meldung)
        self.meldung = meldung
        self.status = status
        self.code = code
        self.inner_code = inner_code
        self.wartezeit_sekunden = wartezeit_sekunden

    def __str__(self) -> str:
        teile = [self.meldung]
        if self.status is not None:
            teile.append(f"HTTP {self.status}")
        if self.code:
            teile.append(f"Code {self.code}")
        if self.inner_code:
            teile.append(f"Detail {self.inner_code}")
        return " | ".join(teile)


class M365Konflikt(M365Fehler):
    """Die Datei ist gesperrt oder wird gerade von jemand anderem bearbeitet (``accessConflict``,
    ``invalidSessionAccessConflict``, HTTP 423, Excel schreibgeschützt). Laut Graph-Doku NICHT sofort
    wiederholen; ``wartezeit_sekunden`` ist die empfohlene Pause bis zum nächsten Versuch."""


class M365NichtGefunden(M365Fehler):
    """Datei, Ordner, Blatt, Nachricht oder Anhang existiert nicht (HTTP 404, ``itemNotFound``)."""


class M365NichtAngemeldet(M365Fehler):
    """Kein gültiges Token im Cache (Anmeldung per CLI ``m365-anmelden`` nötig) oder HTTP 401."""


# ---------------------------------------------------------------------------
# Datenmodelle
# ---------------------------------------------------------------------------


class MailKopf(BaseModel):
    """Kopfdaten einer Nachricht (Ergebnis von ``suche_mails``)."""

    id: str  # Graph-Nachrichten-ID, Outlook-EntryID oder Fake-ID
    betreff: str = ""
    absender: str = ""  # E-Mail-Adresse
    absender_name: str | None = None
    empfangen: datetime
    hat_anhaenge: bool = False
    ordner: str | None = None


class MailAnhang(BaseModel):
    id: str
    name: str
    groesse: int | None = None
    content_type: str | None = None
    inline: bool = False


class Mail(BaseModel):
    """Vollständige Nachricht (Ergebnis von ``lese_mail``)."""

    kopf: MailKopf
    text: str | None = None
    html: str | None = None
    an: list[str] = Field(default_factory=list)
    cc: list[str] = Field(default_factory=list)
    anhaenge: list[MailAnhang] = Field(default_factory=list)


class DateiInfo(BaseModel):
    """Eintrag einer Ordnerliste (``DateiBackend.liste``)."""

    name: str
    pfad: str  # relativ zur Datei-Referenz, mit "/" getrennt
    ist_ordner: bool = False
    groesse: int | None = None
    geaendert: datetime | None = None
    id: str | None = None


# ---------------------------------------------------------------------------
# Protokolle
# ---------------------------------------------------------------------------


@runtime_checkable
class ExcelBackend(Protocol):
    """Lesen und Schreiben von Zellbereichen. Formeln werden nie geschrieben; Spalten mit Formeln
    (z. B. A–C der Fixterminüberwachung) adressiert der Aufrufer schlicht nicht."""

    def blattnamen(self, datei: DateiRefKonfig) -> list[str]: ...

    def lese_bereich(self, datei: DateiRefKonfig, blatt: str, adresse: str) -> list[list[Any]]:
        """Werte des Bereichs (z. B. ``"A5:K9"``) als Matrix; leere Zellen als ``""``."""
        ...

    def lese_benutzten_bereich(self, datei: DateiRefKonfig, blatt: str) -> tuple[str, list[list[Any]]]:
        """Adresse (``"Blatt!A1:K120"``) und Werte des benutzten Bereichs."""
        ...

    def schreibe_bereich(
        self,
        datei: DateiRefKonfig,
        blatt: str,
        adresse: str,
        werte: list[list[Any | None]],
        *,
        zahlenformat: list[list[str | None]] | None = None,
    ) -> None:
        """Schreibt die Matrix in den Bereich. ``None`` = Zelle unverändert lassen, ``""`` = leeren.
        ``zahlenformat`` (gleiche Form) setzt Excel-Formatcodes, ``None`` = Format unverändert."""
        ...


@runtime_checkable
class MailBackend(Protocol):
    """Entwürfe anlegen und Posteingang lesen. Es wird NIE gesendet."""

    def erstelle_entwurf(self, entwurf: EmailEntwurf) -> str:
        """Legt den Entwurf an und liefert den Speicherort (Graph-ID, EntryID oder ``.eml``-Pfad)."""
        ...

    def suche_mails(
        self,
        *,
        seit: datetime,
        absender: str | None = None,
        betreff_enthaelt: str | None = None,
        ordner: str | None = None,
        maximal: int = 50,
    ) -> list[MailKopf]: ...

    def lese_mail(self, mail_id: str) -> Mail: ...

    def speichere_anhang(self, mail_id: str, anhang_id: str, ziel: Path) -> Path:
        """Speichert den Anhang unter ``ziel`` (Datei) bzw. in ``ziel`` (vorhandener Ordner)."""
        ...


@runtime_checkable
class DateiBackend(Protocol):
    """Ordner und Dateien unterhalb einer Datei-Referenz (Dossiers, Ablage "Aufträge mit/ohne Provision")."""

    def existiert(self, ref: DateiRefKonfig, relativ: str = "") -> bool: ...

    def ordner_anlegen(self, ref: DateiRefKonfig, relativ: str) -> str:
        """Legt den Ordner (auch verschachtelt) an, idempotent; liefert ID bzw. Pfad."""
        ...

    def liste(self, ref: DateiRefKonfig, relativ: str = "") -> list[DateiInfo]: ...

    def hochladen(self, lokal: Path, ref: DateiRefKonfig, relativ: str) -> str:
        """Lädt die lokale Datei nach ``relativ`` hoch (Pfad einschließlich Dateiname; endet ``relativ``
        mit "/" oder ist leer, wird der lokale Dateiname angehängt). Vorhandene Dateien werden ersetzt."""
        ...

    def herunterladen(self, ref: DateiRefKonfig, relativ: str, ziel: Path) -> Path: ...

    def schreibe_text(self, ref: DateiRefKonfig, relativ: str, text: str) -> str:
        """Schreibt UTF-8-Text als Datei; liefert ID bzw. Pfad."""
        ...


# ---------------------------------------------------------------------------
# Hilfsfunktionen für Zelladressen
# ---------------------------------------------------------------------------


_ADRESSE_RE = re.compile(
    r"^(?:(?P<blatt>'[^']+'|[^!']+)!)?"
    r"\$?(?P<c1>[A-Za-z]{1,3})\$?(?P<r1>\d+)"
    r"(?::\$?(?P<c2>[A-Za-z]{1,3})\$?(?P<r2>\d+))?$"
)


@dataclass(frozen=True)
class Zellbereich:
    """Ein rechteckiger Bereich, 1-basiert (Zeile 1 = Excel-Zeile 1, Spalte 1 = A)."""

    zeile_von: int
    spalte_von: int
    zeile_bis: int
    spalte_bis: int
    blatt: str | None = None

    @property
    def zeilen(self) -> int:
        return self.zeile_bis - self.zeile_von + 1

    @property
    def spalten(self) -> int:
        return self.spalte_bis - self.spalte_von + 1

    @property
    def adresse(self) -> str:
        """Adresse ohne Blattnamen, z. B. ``"D7:K7"`` (Einzelzelle: ``"D7"``)."""
        von = f"{spaltenbuchstabe(self.spalte_von)}{self.zeile_von}"
        if self.zeilen == 1 and self.spalten == 1:
            return von
        return f"{von}:{spaltenbuchstabe(self.spalte_bis)}{self.zeile_bis}"

    def mit_blatt(self) -> str:
        """Adresse mit Blattnamen wie Graph sie liefert (``Blatt!A1:K9``; Namen mit Sonderzeichen in
        einfachen Anführungszeichen)."""
        if not self.blatt:
            return self.adresse
        name = self.blatt if re.fullmatch(r"[A-Za-z0-9_]+", self.blatt) else f"'{self.blatt}'"
        return f"{name}!{self.adresse}"


def spaltenbuchstabe(index: int) -> str:
    """1 -> "A", 26 -> "Z", 27 -> "AA"."""
    if index < 1:
        raise ValueError(f"Spaltenindex muss >= 1 sein, nicht {index}")
    buchstaben = ""
    while index > 0:
        index, rest = divmod(index - 1, 26)
        buchstaben = chr(ord("A") + rest) + buchstaben
    return buchstaben


def spaltenindex(buchstaben: str) -> int:
    """"A" -> 1, "Z" -> 26, "AA" -> 27."""
    wert = 0
    for zeichen in buchstaben.strip().upper():
        if not "A" <= zeichen <= "Z":
            raise ValueError(f"Ungültige Spaltenbezeichnung '{buchstaben}'")
        wert = wert * 26 + (ord(zeichen) - ord("A") + 1)
    if wert == 0:
        raise ValueError("Leere Spaltenbezeichnung")
    return wert


def parse_adresse(adresse: str) -> Zellbereich:
    """Zerlegt ``"D7:K7"``, ``"D7"``, ``"$D$7:$K$7"`` oder ``"'Blatt'!D7:K7"`` in einen ``Zellbereich``."""
    treffer = _ADRESSE_RE.match(adresse.strip())
    if treffer is None:
        raise ValueError(f"Ungültige Zelladresse '{adresse}'")
    blatt = treffer.group("blatt")
    if blatt and blatt.startswith("'") and blatt.endswith("'"):
        blatt = blatt[1:-1]
    z1 = int(treffer.group("r1"))
    s1 = spaltenindex(treffer.group("c1"))
    z2 = int(treffer.group("r2")) if treffer.group("r2") else z1
    s2 = spaltenindex(treffer.group("c2")) if treffer.group("c2") else s1
    if z1 < 1 or z2 < 1:
        raise ValueError(f"Zeilen beginnen bei 1: '{adresse}'")
    return Zellbereich(min(z1, z2), min(s1, s2), max(z1, z2), max(s1, s2), blatt)


# ---------------------------------------------------------------------------
# Datumswerte (Excel-Serienzahlen)
# ---------------------------------------------------------------------------

#: Tag 0 des 1900-Datumssystems von Excel (Serienzahl 1 = 1.1.1900; Excel zählt den
#: nicht existierenden 29.2.1900 mit, deshalb liegt der Nullpunkt auf dem 30.12.1899).
EXCEL_EPOCHE = date(1899, 12, 30)


def datum_zu_serienzahl(wert: date | datetime) -> float:
    """Wandelt ein Datum in die Excel-Serienzahl (1900-System) um; Uhrzeiten als Nachkommateil.

    Graph nimmt in ``values`` nur JSON-Typen an. Ein ISO-Text würde als Text abgelegt, sofern die
    Zelle nicht schon ein Datumsformat trägt (Recherche ``graph_excel_outlook.md``, Abschnitt 1.10);
    die Serienzahl plus ``numberFormat`` ist der dokumentierte Weg (Doku-Beispiel: ``"1/1/2016"`` mit
    ``numberFormat``). Datumswerte vor dem 1.3.1900 werden nicht unterstützt (Excel-Schaltjahrfehler).
    """
    if isinstance(wert, datetime):
        tag = wert.date()
        bruchteil = (wert - datetime.combine(tag, datetime.min.time(), tzinfo=wert.tzinfo)).total_seconds()
        return (tag - EXCEL_EPOCHE).days + bruchteil / 86400
    return float((wert - EXCEL_EPOCHE).days)


def serienzahl_zu_datum(wert: Any) -> date | datetime | None:
    """Wandelt eine Excel-Serienzahl (``int``/``float``/``Decimal``/Zahl als Text) in ``date`` bzw.
    ``datetime`` (bei Nachkommateil) um. ``date``/``datetime`` werden durchgereicht, Leeres und
    Unpassendes ergibt ``None``."""
    if wert is None or wert == "":
        return None
    if isinstance(wert, datetime):
        return wert
    if isinstance(wert, date):
        return wert
    if isinstance(wert, bool):
        return None
    try:
        zahl = float(Decimal(str(wert).strip().replace(",", ".")))
    except (ArithmeticError, ValueError):
        return None
    if zahl < 61 or zahl > 2_958_465:  # vor 1.3.1900 bzw. nach 31.12.9999
        return None
    tage = int(zahl)
    rest = zahl - tage
    tag = EXCEL_EPOCHE + timedelta(days=tage)
    if rest < 1e-9:
        return tag
    return datetime.combine(tag, datetime.min.time()) + timedelta(seconds=round(rest * 86400))


# ---------------------------------------------------------------------------
# Weitere Hilfen
# ---------------------------------------------------------------------------


def datei_schluessel(ref: DateiRefKonfig) -> str:
    """Stabiler Schlüssel einer Datei-Referenz (für Sitzungs-/Cache-Tabellen der Backends)."""
    if ref.drive_id and ref.item_id:
        return f"{ref.drive_id}/{ref.item_id}"
    if ref.web_url:
        return ref.web_url
    if ref.lokaler_pfad is not None:
        return str(ref.lokaler_pfad)
    return "standard"


@contextmanager
def excel_sitzung(backend: Any, datei: DateiRefKonfig) -> Iterator[None]:
    """Bündelt mehrere Excel-Zugriffe in einer Sitzung, falls das Backend ``sitzung`` anbietet
    (Graph: Workbook-Session, lokal: Arbeitsmappe bleibt offen); sonst ohne Wirkung."""
    sitzung = getattr(backend, "sitzung", None)
    if sitzung is None:
        yield
        return
    with sitzung(datei):
        yield


_UNGUELTIGE_DATEINAMENZEICHEN = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


def sicherer_dateiname(name: str, *, ersatz: str = "_", maximal: int = 150) -> str:
    """Entfernt Zeichen, die in Windows/OneDrive-Dateinamen verboten sind, und kürzt.

    Nur für technische Dateien der Zugriffsschicht (``.eml``-Entwürfe, Vorschau-CSV). Namen von
    Belegen, Mail-Anhängen, Dossier-Dateien und Ablage kommen ausschließlich aus ``benennung.py``.
    """
    bereinigt = _UNGUELTIGE_DATEINAMENZEICHEN.sub(ersatz, name).strip(" .")
    bereinigt = re.sub(r"\s+", " ", bereinigt)
    return (bereinigt or "datei")[:maximal]


class _TextSammler(HTMLParser):
    _BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "ul", "ol"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.teile: list[str] = []
        self._ignorieren = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style", "head"):
            self._ignorieren += 1
        elif tag in self._BLOCK:
            self.teile.append("\n")
        elif tag == "td":
            self.teile.append("\t")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "head"):
            self._ignorieren = max(0, self._ignorieren - 1)
        elif tag in self._BLOCK:
            self.teile.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._ignorieren:
            self.teile.append(data)


def html_zu_text(html: str | None) -> str:
    """Einfache HTML-zu-Text-Umwandlung (Absätze/Zeilenumbrüche bleiben als Zeilen erhalten)."""
    if not html:
        return ""
    sammler = _TextSammler()
    sammler.feed(html)
    sammler.close()
    text = "".join(sammler.teile).replace("\r\n", "\n").replace("\xa0", " ")
    zeilen = [re.sub(r"[ \t]+", " ", zeile).strip() for zeile in text.split("\n")]
    text = "\n".join(zeilen)
    return re.sub(r"\n{3,}", "\n\n", text).strip()
