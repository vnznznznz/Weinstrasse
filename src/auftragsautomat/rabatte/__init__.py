"""Aktionen und Rabatte (docs/AUSBAUSTUFE_2.md Abschnitt 7, Geschäftsregel 16).

* :mod:`~auftragsautomat.rabatte.aktionen_excel` – Aktionszeiträume aus der Excel in OneDrive.
* :mod:`~auftragsautomat.rabatte.rabattliste_pdf` – Rabattlisten-PDF (pdfplumber, sonst Claude Vision)
  und das Gedächtnis ``config/aktionen.yaml``.
* :mod:`~auftragsautomat.rabatte.anwenden` – Treffer suchen, Prozent umrechnen, Positionsrabatt eintragen.
"""

from auftragsautomat.rabatte.aktionen_excel import AktionenLadeErgebnis, lese_aktionen
from auftragsautomat.rabatte.anwenden import (
    alle_aktionen,
    bezugsdatum,
    finde_aktionen,
    leere_cache,
    rabatt_prozent_aus,
    wende_an,
)
from auftragsautomat.rabatte.rabattliste_pdf import (
    RabattlisteFehler,
    lade_gespeicherte_aktionen,
    lese_rabattliste,
    speichere_aktionen,
)

__all__ = [
    "AktionenLadeErgebnis",
    "RabattlisteFehler",
    "alle_aktionen",
    "bezugsdatum",
    "finde_aktionen",
    "lade_gespeicherte_aktionen",
    "leere_cache",
    "lese_aktionen",
    "lese_rabattliste",
    "rabatt_prozent_aus",
    "speichere_aktionen",
    "wende_an",
]
