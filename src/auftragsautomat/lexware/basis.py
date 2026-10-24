"""Schnittstelle zum Schreiben nach Lexware.

Drei Implementierungen:
  * ``opentrans.OpenTransSchreiber``  – Verkauf-AB über die Standard-Shopschnittstelle (XML-Import)
  * ``ui_automation.UiSchreiber``     – alle Belege per pywinauto (Einkauf, Angebot, AB, Lieferschein, PDF)
  * ``dry_run.DryRunSchreiber``       – Simulation ohne Lexware (Linux, Tests, CLI ohne ``--scharf``)

Alle müssen den Dry Run unterstützen: alles ausführen bis kurz vor "Speichern", dann abbrechen und
ein Belegergebnis mit ``dry_run=True`` liefern – auch beim Angebot.

Ablauf laut Lexware-Doku (docs/AUSBAUSTUFE_2.md, Abschnitt 5; Geschäftsregeln 13-16, 18):

    [Einkauf-Bestellung je Weingut]  ->  Angebot  ->  Weiterführen / Auftrag wandeln  ->  AB  ->  [Lieferschein]
          + Nachbemerkung                 + Rabatte      (Lieferdatum, AB-Text leeren)       (PDF "Kopie")   (PDF "Ls")
          + PDF "Cde" + "Speditionsauftrag"

Die PDF-Ausgabe (:meth:`LexwareSchreiber.pdf_ausgeben`) läuft nur nach ``settings.lexware.pdf_ausgabe``
(``drucker`` = PDF-Drucker mit Dateinamen-Dialog, ``export`` = Lexware-Export, ``keine``): nie Papier,
nie E-Mail aus Lexware. Dateinamen kommen ausschließlich aus ``benennung.py`` (Titulierung); der
Schreiber baut daraus nur den Pfad ``settings.lexware.pdf_ausgabe_ordner / dateiname``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Protocol

from auftragsautomat.config import Firma, Settings
from auftragsautomat.models import Belegergebnis, Belegpaket

__all__ = [
    "ANGEBOTSWEGE",
    "LexwareSchreiber",
    "PdfModus",
    "Verkaufsweg",
    "nachbemerkung_einkauf",
    "pdf_zielpfad",
    "unterstuetzt_angebot",
    "unterstuetzt_pdf",
    "weg_beschreibung",
]

#: Wege vom Angebot zur Auftragsbestätigung (``Firma.verkauf_weg`` ohne ``direkt_ab``).
Verkaufsweg = Literal["angebot_weiterfuehren", "angebot_wandeln"]
ANGEBOTSWEGE: tuple[str, ...] = ("angebot_weiterfuehren", "angebot_wandeln")
PdfModus = Literal["keine", "drucker", "export"]

_WEG_TEXTE = {
    "angebot_weiterfuehren": "Angebot -> Weiterführen -> Auftragsbestätigung",
    "angebot_wandeln": "Angebot -> Auftrag wandeln -> Auftragsbestätigung",
    "direkt_ab": "Auftragsbestätigung direkt (ohne Angebot)",
}


class LexwareSchreiber(Protocol):
    def schreibe_verkauf_ab(self, paket: Belegpaket, *, dry_run: bool = True) -> Belegergebnis:
        """Legt die Auftragsbestätigung im Verkauf direkt an (Kunde = Endkunde, Preise = VK Preisgruppe 1).

        Weg ``direkt_ab``; die Wege über das Angebot nutzen :meth:`schreibe_angebot` und :meth:`angebot_zu_ab`.
        """
        ...

    def schreibe_einkauf_bestellung(self, paket: Belegpaket, *, dry_run: bool = True) -> Belegergebnis:
        """Legt die Bestellung im Einkauf an (Lieferant = Weingut, Preise = EK, Feld Customer = Endkunde).

        WIK/WOE: die Kundenbestellnummer kommt in die Nachbemerkung (``Firma.bestellnummer_in_nachbemerkung``,
        Text über :func:`nachbemerkung_einkauf`; Ergebnis in ``Belegergebnis.nachbemerkung``).
        """
        ...

    def auftrag_weiterfuehren_lieferschein(
        self, paket: Belegpaket, ab_belegnummer: str, *, dry_run: bool = True
    ) -> Belegergebnis:
        """Optional: Lieferschein aus der AB ("Auftrag weiterführen"). Nur nach Freigabe und ``Firma.lieferschein_erlaubt``."""
        ...

    def schreibe_angebot(self, paket: Belegpaket, *, dry_run: bool = True) -> Belegergebnis:
        """Legt das Angebot im Verkauf an (``Belegart.ANGEBOT``): Kunde, Positionen mit VK Preisgruppe 1 und
        Positionsrabatt in % aus ``PositionsZuordnung.rabatt_prozent`` (Geschäftsregel 16). Dry Run bricht vor
        "Speichern" ab."""
        ...

    def angebot_zu_ab(
        self,
        angebot: Belegergebnis,
        paket: Belegpaket,
        *,
        weg: Verkaufsweg = "angebot_weiterfuehren",
        dry_run: bool = True,
    ) -> Belegergebnis:
        """Führt das Angebot zur Auftragsbestätigung weiter ("Weiterführen") oder wandelt es ("Auftrag wandeln").

        In der AB werden Lieferdatum (``Firma.lieferdatum_in_ab_loeschen``) und AB-Text (``Firma.ab_text_loeschen``)
        geleert. Ergebnis: ``Belegergebnis(Belegart.AUFTRAGSBESTAETIGUNG)``.
        """
        ...

    def pdf_ausgeben(
        self, beleg: Belegergebnis, *, formular: str | None, dateiname: str, dry_run: bool = True
    ) -> Path | None:
        """Gibt den gespeicherten Beleg als PDF aus (Formular laut ``Firma.formulare``, Name laut Titulierung).

        Liefert den Pfad ``settings.lexware.pdf_ausgabe_ordner / dateiname`` oder ``None`` (nicht möglich,
        ``pdf_ausgabe = keine``). Dry Run: nichts wird erzeugt, der geplante Pfad wird geliefert.
        Hinweise landen in ``beleg.warnungen``.
        """
        ...


def unterstuetzt_angebot(schreiber: object) -> bool:
    """True, wenn der Schreiber den Weg Angebot -> AB kennt (``schreibe_angebot`` und ``angebot_zu_ab``)."""
    return callable(getattr(schreiber, "schreibe_angebot", None)) and callable(getattr(schreiber, "angebot_zu_ab", None))


def unterstuetzt_pdf(schreiber: object) -> bool:
    """True, wenn der Schreiber :meth:`LexwareSchreiber.pdf_ausgeben` implementiert."""
    return callable(getattr(schreiber, "pdf_ausgeben", None))


def nachbemerkung_einkauf(paket: Belegpaket, firma: Firma) -> str | None:
    """Text der Nachbemerkung der Einkauf-Bestellung: die Kundenbestellnummer, wenn die Firma das vorsieht.

    Laut Ablaufplan (WIK/WOE) "kommt die Bestellnummer des Kunden in die Nachbemerkung". Ohne
    Kundenbestellnummer oder bei ``bestellnummer_in_nachbemerkung = False`` (Weinstrasse Agentur) ``None``.
    """
    # TODO-PRUEFEN: ob die Nachbemerkung nur die Nummer oder einen Vorsatz ("Customer order no.") tragen soll;
    # das EN-Formular druckt die Nachbemerkung vermutlich als Schlusstext.
    if not firma.bestellnummer_in_nachbemerkung:
        return None
    nummer = (paket.roh.bestellnummer or "").strip()
    return nummer or None


def pdf_zielpfad(settings: Settings, dateiname: str) -> Path:
    """Geplanter Pfad einer PDF: ``settings.lexware.pdf_ausgabe_ordner / dateiname`` (nur der Name zählt)."""
    name = Path(dateiname).name or "beleg.pdf"
    return Path(settings.lexware.pdf_ausgabe_ordner) / name


def weg_beschreibung(weg: str) -> str:
    """Lesbarer Text zu ``Firma.verkauf_weg`` für Protokoll und Bericht."""
    return _WEG_TEXTE.get(weg, weg)
