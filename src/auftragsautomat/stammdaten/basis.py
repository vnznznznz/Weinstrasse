"""Schnittstelle für Stammdatenquellen (Lexware, NUR LESEND).

Implementierungen: csv_quelle.CsvStammdatenQuelle, odbc_quelle.OdbcStammdatenQuelle.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol, runtime_checkable

from auftragsautomat.models import Artikel, Beleg, Kunde, Lieferant


@runtime_checkable
class StammdatenQuelle(Protocol):
    firma_id: str

    def artikel(self) -> list[Artikel]: ...

    def kunden(self) -> list[Kunde]: ...

    def lieferanten(self) -> list[Lieferant]: ...

    def belege(self) -> list[Beleg]:
        """Vorhandene Belege für die Dublettenprüfung. Darf [] liefern, wenn nicht verfügbar."""
        ...


class Stammdaten:
    """In-Memory-Stamm einer Firma mit Indizes für schnellen Zugriff."""

    def __init__(
        self,
        firma_id: str,
        artikel: Iterable[Artikel] = (),
        kunden: Iterable[Kunde] = (),
        lieferanten: Iterable[Lieferant] = (),
        belege: Iterable[Beleg] = (),
    ) -> None:
        self.firma_id = firma_id
        self.artikel: list[Artikel] = list(artikel)
        self.kunden: list[Kunde] = list(kunden)
        self.lieferanten: list[Lieferant] = list(lieferanten)
        self.belege: list[Beleg] = list(belege)
        self.artikel_nach_nummer: dict[str, Artikel] = {a.artikelnummer: a for a in self.artikel}
        self.kunden_nach_nummer: dict[str, Kunde] = {k.kundennummer: k for k in self.kunden}
        self.lieferanten_nach_nummer: dict[str, Lieferant] = {
            lf.lieferantennummer: lf for lf in self.lieferanten
        }

    @classmethod
    def aus_quelle(cls, quelle: StammdatenQuelle) -> Stammdaten:
        return cls(
            firma_id=quelle.firma_id,
            artikel=quelle.artikel(),
            kunden=quelle.kunden(),
            lieferanten=quelle.lieferanten(),
            belege=quelle.belege(),
        )

    def __repr__(self) -> str:
        return (
            f"Stammdaten({self.firma_id}: {len(self.artikel)} Artikel, {len(self.kunden)} Kunden, "
            f"{len(self.lieferanten)} Lieferanten, {len(self.belege)} Belege)"
        )
