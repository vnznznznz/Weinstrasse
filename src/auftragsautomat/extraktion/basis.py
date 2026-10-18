"""Schnittstelle der Extraktoren.

Jeder Extraktor nimmt eine Datei und liefert eine RohBestellung. Die Erkennung,
welcher Extraktor zuständig ist, liegt in ``erkennung.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from auftragsautomat.models import RohBestellung


class Extraktor(Protocol):
    def kann_verarbeiten(self, datei: Path) -> bool: ...

    def extrahiere(self, datei: Path) -> RohBestellung: ...
