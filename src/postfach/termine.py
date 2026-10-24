"""Wiederkehrende Aufgaben und Termine aus ``postfach/wissen/wiederkehrend.yaml``.

Jeder Eintrag hat genau eine Zeitangabe:

* ``jaehrlich: "MM-TT"``      – z. B. Messe, Jahresabschluss
* ``monatlich: TT``           – z. B. Provisionsabrechnung am 10.
* ``woechentlich: "Mo"``      – Mo Di Mi Do Fr Sa So
* ``einmalig: "JJJJ-MM-TT"``  – ein konkreter Termin (z. B. Steuerberater-Termin)

``vorlauf_tage`` sagt, wie viele Tage vorher die Aufgabe im Briefing auftauchen soll.
``aufgaben`` ist die Liste der Vorbereitungsschritte. ``quelle`` benennt, woher das Wissen stammt
(„JD“, „Kalender“, „Mail vom …“, „Annahme – bitte prüfen“).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

STANDARD_TERMINDATEI = Path(__file__).resolve().parents[2] / "postfach" / "wissen" / "wiederkehrend.yaml"
_WOCHENTAGE = {"mo": 0, "di": 1, "mi": 2, "do": 3, "fr": 4, "sa": 5, "so": 6}


class TerminFehler(ValueError):
    pass


@dataclass
class Aufgabe:
    name: str
    art: str = "aufgabe"
    vorlauf_tage: int = 14
    aufgaben: list[str] = field(default_factory=list)
    quelle: str = ""
    notiz: str = ""
    jaehrlich: str | None = None
    monatlich: int | None = None
    woechentlich: str | None = None
    einmalig: str | None = None
    erledigt_bis: str | None = None  # Datum, bis zu dem JD das Thema als erledigt markiert hat

    @classmethod
    def aus_dict(cls, roh: dict[str, Any]) -> Aufgabe:
        bekannt = set(cls.__dataclass_fields__)
        daten = {k: v for k, v in roh.items() if k in bekannt}
        if "name" not in daten:
            raise TerminFehler(f"Eintrag ohne 'name': {roh}")
        if daten.get("einmalig") is not None and not isinstance(daten["einmalig"], str):
            daten["einmalig"] = daten["einmalig"].isoformat()
        if daten.get("erledigt_bis") is not None and not isinstance(daten["erledigt_bis"], str):
            daten["erledigt_bis"] = daten["erledigt_bis"].isoformat()
        return cls(**daten)

    def naechster_termin(self, heute: date) -> date | None:
        if self.einmalig:
            t = date.fromisoformat(self.einmalig)
            return t if t >= heute else None
        if self.jaehrlich:
            monat, tag = (int(x) for x in self.jaehrlich.split("-"))
            for jahr in (heute.year, heute.year + 1):
                try:
                    kandidat = date(jahr, monat, min(tag, _monatsende(jahr, monat)))
                except ValueError as exc:
                    raise TerminFehler(f"{self.name}: ungültiges Datum {self.jaehrlich}") from exc
                if kandidat >= heute:
                    return kandidat
            return None
        if self.monatlich:
            for versatz in range(0, 3):
                jahr, monat = _plus_monate(heute.year, heute.month, versatz)
                kandidat = date(jahr, monat, min(int(self.monatlich), _monatsende(jahr, monat)))
                if kandidat >= heute:
                    return kandidat
            return None
        if self.woechentlich:
            ziel = _WOCHENTAGE.get(self.woechentlich.strip().lower()[:2])
            if ziel is None:
                raise TerminFehler(f"{self.name}: unbekannter Wochentag {self.woechentlich}")
            return heute + timedelta(days=(ziel - heute.weekday()) % 7)
        return None


@dataclass
class Faellig:
    aufgabe: Aufgabe
    termin: date
    in_tagen: int

    @property
    def dringend(self) -> bool:
        return self.in_tagen <= 3

    def zeile(self) -> str:
        wann = (
            "heute"
            if self.in_tagen == 0
            else ("morgen" if self.in_tagen == 1 else f"in {self.in_tagen} Tagen")
        )
        kopf = f"{self.termin.strftime('%d.%m.%Y')} ({wann}): {self.aufgabe.name}"
        if self.aufgabe.aufgaben:
            kopf += " – " + "; ".join(self.aufgabe.aufgaben)
        if self.aufgabe.quelle:
            kopf += f" [{self.aufgabe.quelle}]"
        return kopf


def _monatsende(jahr: int, monat: int) -> int:
    folge_jahr, folge_monat = _plus_monate(jahr, monat, 1)
    return (date(folge_jahr, folge_monat, 1) - timedelta(days=1)).day


def _plus_monate(jahr: int, monat: int, n: int) -> tuple[int, int]:
    index = (monat - 1) + n
    return jahr + index // 12, index % 12 + 1


def lade_aufgaben(pfad: Path | str | None = None) -> list[Aufgabe]:
    datei = Path(pfad) if pfad else STANDARD_TERMINDATEI
    if not datei.exists():
        return []
    try:
        import yaml  # PyYAML ist im Projekt ohnehin Abhängigkeit
    except ImportError as exc:  # pragma: no cover
        raise TerminFehler("PyYAML fehlt: pip install pyyaml") from exc
    roh = yaml.safe_load(datei.read_text(encoding="utf-8")) or []
    if isinstance(roh, dict):
        roh = roh.get("aufgaben", [])
    return [Aufgabe.aus_dict(e) for e in roh]


def anstehend(aufgaben: list[Aufgabe], *, heute: date, tage: int = 28) -> list[Faellig]:
    """Alle Aufgaben, deren Termin innerhalb von ``tage`` liegt oder deren Vorlauf begonnen hat."""
    ergebnis: list[Faellig] = []
    for a in aufgaben:
        termin = a.naechster_termin(heute)
        if termin is None:
            continue
        if a.erledigt_bis and date.fromisoformat(a.erledigt_bis) >= termin:
            continue
        in_tagen = (termin - heute).days
        if in_tagen <= max(tage, a.vorlauf_tage):
            ergebnis.append(Faellig(aufgabe=a, termin=termin, in_tagen=in_tagen))
    ergebnis.sort(key=lambda f: (f.termin, f.aufgabe.name))
    return ergebnis
