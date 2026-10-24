"""Datenschutz: keine echten Personen-Adressen und keine echten Sendungsreferenzen im Repository.

Regression (Review Befund 7): Die Recherche-Doku enthielt die Login-Adresse des Anwenders und eine echte
Hillebrand-Referenz (über das öffentliche Tracking abrufbar). Erlaubt sind nur Platzhalter
(``<anwender>@<firma>.de``), Beispiel-Domains (``*.example``, ``example.org``) und Funktionspostfächer.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

STAMM = Path(__file__).resolve().parent.parent
TEXT_ENDUNGEN = {".md", ".py", ".yaml", ".yml", ".txt", ".j2", ".toml", ".csv", ".eml", ".json", ".html"}
AUSGENOMMEN = {".git", ".venv", "__pycache__", "logs", "archiv", "eingang"}

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})")
# Funktionspostfächer (keine Personen) und triviale Platzhalter, die in der Doku zitiert werden dürfen
_FUNKTIONSPOSTFAECHER = {"info", "def-order", "def-info", "a", "c", "e"}
_HILLEBRAND_REF = re.compile(r"\bDEMB(\d+)")
_HILLEBRAND_JOB = re.compile(r"\b(?:job)?DEM(\d{6,})")


def _textdateien(*ordner: str) -> list[Path]:
    dateien: list[Path] = []
    for name in ordner:
        basis = STAMM / name
        kandidaten = [basis] if basis.is_file() else basis.rglob("*")
        for pfad in kandidaten:
            if pfad.is_file() and pfad.suffix.lower() in TEXT_ENDUNGEN and not AUSGENOMMEN & set(pfad.relative_to(STAMM).parts):
                dateien.append(pfad)
    return dateien


def _ist_platzhalter_ref(ziffern: str) -> bool:
    """Test-/Doku-Referenzen: nur Nullen mit laufender Endziffer (00000, 00001 …) oder eine Ziffer wiederholt."""
    return ziffern.startswith("0000") or len(set(ziffern)) == 1


@pytest.mark.parametrize("ordner", ["docs", "config", "README.md", "scripts"])
def test_keine_persoenlichen_email_adressen(ordner: str):
    funde = []
    for pfad in _textdateien(ordner):
        for treffer in _EMAIL.finditer(pfad.read_text(encoding="utf-8", errors="ignore")):
            adresse, domain = treffer.group(0), treffer.group(1).lower()
            lokal = adresse.split("@", 1)[0].lower()
            if "example" in domain or lokal in _FUNKTIONSPOSTFAECHER:
                continue
            funde.append(f"{pfad.relative_to(STAMM)}: {adresse}")
    assert funde == []


@pytest.mark.parametrize("ordner", ["docs", "config", "src", "tests", "scripts", "README.md"])
def test_keine_echten_hillebrand_referenzen(ordner: str):
    funde = []
    for pfad in _textdateien(ordner):
        text = pfad.read_text(encoding="utf-8", errors="ignore")
        for muster in (_HILLEBRAND_REF, _HILLEBRAND_JOB):
            for treffer in muster.finditer(text):
                if not _ist_platzhalter_ref(treffer.group(1)):
                    funde.append(f"{pfad.relative_to(STAMM)}: {treffer.group(0)}")
    assert funde == []
