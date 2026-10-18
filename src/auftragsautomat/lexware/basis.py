"""Schnittstelle zum Schreiben nach Lexware.

Zwei Wege:
  * opentrans.OpenTransSchreiber   – Verkauf-AB über die Standard-Shopschnittstelle (XML-Import)
  * ui_automation.UiSchreiber      – Einkauf-Bestellung (und Fallback für alles) per pywinauto

Beide müssen den Dry Run unterstützen: alles ausführen bis kurz vor "Speichern",
dann abbrechen und ein Belegergebnis mit dry_run=True liefern.
"""

from __future__ import annotations

from typing import Protocol

from auftragsautomat.models import Belegergebnis, Belegpaket


class LexwareSchreiber(Protocol):
    def schreibe_verkauf_ab(self, paket: Belegpaket, *, dry_run: bool = True) -> Belegergebnis:
        """Legt die Auftragsbestätigung im Verkauf an (Kunde = Endkunde, Preise = VK Preisgruppe 1)."""
        ...

    def schreibe_einkauf_bestellung(self, paket: Belegpaket, *, dry_run: bool = True) -> Belegergebnis:
        """Legt die Bestellung im Einkauf an (Lieferant = Weingut, Preise = EK, Feld Customer = Endkunde)."""
        ...

    def auftrag_weiterfuehren_lieferschein(
        self, paket: Belegpaket, ab_belegnummer: str, *, dry_run: bool = True
    ) -> Belegergebnis:
        """Optional: Lieferschein aus der AB ("Auftrag weiterführen"). Nur nach Freigabe."""
        ...
