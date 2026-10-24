"""Ablage fertiger, benannter Dateien in den OneDrive-Ordner "Aufträge" (nur nach Freigabe).

Öffentliche Einstiegspunkte: :func:`lege_ab` (Dateien eines gebuchten Auftrags kopieren),
:func:`zielordner` (Zielpfad aus ``ablage.struktur``), :func:`freier_name` (Namenskollision
-> " (2)", " (3)" ...), :func:`quellen` (Vorschau der Dateien, die abgelegt würden) und
:func:`ablage_ref` (OneDrive-Ordner laut Konfiguration bzw. lokaler Rückfall).
"""

from auftragsautomat.ablage.ablage import (
    KUERZEL_KUNDENBESTELLUNG,
    PLATZHALTER,
    AblageFehler,
    AblageQuelle,
    ablage_ref,
    freier_name,
    lege_ab,
    platzhalter_werte,
    quellen,
    zielordner,
)

__all__ = [
    "KUERZEL_KUNDENBESTELLUNG",
    "PLATZHALTER",
    "AblageFehler",
    "AblageQuelle",
    "ablage_ref",
    "freier_name",
    "lege_ab",
    "platzhalter_werte",
    "quellen",
    "zielordner",
]
