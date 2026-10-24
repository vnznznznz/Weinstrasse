"""Laden und Prüfen von ``postfach/regeln.json`` (Format der Datei bleibt unverändert)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

STANDARD_REGELDATEI = Path(__file__).resolve().parents[2] / "postfach" / "regeln.json"


class RegelFehler(ValueError):
    """Die Regeldatei ist unvollständig oder widersprüchlich."""


@dataclass
class Kategorie:
    name: str
    farbe: str
    ordner: str
    abwicklung_ordner: str | None = None


@dataclass
class Regel:
    name: str
    kategorie: str | None = None
    ordner: str | None = None
    aktion: str | None = None  # z. B. "soft_delete"
    absender_oder_empfaenger: list[str] = field(default_factory=list)
    absender_domains: list[str] = field(default_factory=list)
    absender_muster: list[str] = field(default_factory=list)
    zusatz_absender: list[str] = field(default_factory=list)
    zusatz_absender_domains: list[str] = field(default_factory=list)
    nur_lieferanten: bool = False
    stichworte: list[str] = field(default_factory=list)
    abwicklung_stichworte: list[str] = field(default_factory=list)
    betreff_beginnt: list[str] = field(default_factory=list)
    sonderordner: dict[str, str] | None = None
    ausnahme_ordner: list[str] = field(default_factory=list)
    hinweis: str | None = None
    beispiele: list[str] = field(default_factory=list)

    @classmethod
    def aus_dict(cls, roh: dict[str, Any]) -> Regel:
        bekannt = {f for f in cls.__dataclass_fields__}
        daten = {k: v for k, v in roh.items() if k in bekannt}
        return cls(**daten)


@dataclass
class Regelwerk:
    mailbox: str
    stand: str
    grundsaetze: dict[str, Any]
    ordner: dict[str, str | None]
    kategorien: dict[str, Kategorie]
    regeln: list[Regel]
    lieferanten_domains: list[str]
    lieferanten_absender_einzeln: list[str]
    technik: dict[str, Any]
    quelle: Path | None = None

    # ------------------------------------------------------------------ Zugriff
    def ordner_id(self, name: str | None) -> str | None:
        if not name:
            return None
        return self.ordner.get(name)

    def kategorie_fuer_ordner(self, ordner: str) -> Kategorie | None:
        for k in self.kategorien.values():
            if k.ordner == ordner or k.abwicklung_ordner == ordner:
                return k
        return None

    @property
    def eigene_domain(self) -> str:
        return self.mailbox.rsplit("@", 1)[-1].lower()

    @property
    def ausnahme_alter_tage(self) -> int:
        return int(self.grundsaetze.get("ausnahme_aelter_als_tage", 60))

    @property
    def ungelesen_bleibt(self) -> bool:
        return bool(self.grundsaetze.get("ungelesen_markiert_bleiben_im_posteingang", True))

    # ------------------------------------------------------------------ Prüfung
    def pruefen(self) -> list[str]:
        """Liefert Warnungen (keine Exceptions) zu fehlenden IDs oder Verweisen."""
        meldungen: list[str] = []
        for name, kat in self.kategorien.items():
            for ordner in (kat.ordner, kat.abwicklung_ordner):
                if ordner and ordner not in self.ordner:
                    meldungen.append(
                        f"Kategorie '{name}': Ordner '{ordner}' ist unter 'ordner' nicht aufgeführt."
                    )
                elif ordner and not self.ordner.get(ordner):
                    meldungen.append(
                        f"Kategorie '{name}': Ordner '{ordner}' hat noch keine ID (Mails bleiben liegen)."
                    )
        for regel in self.regeln:
            if regel.kategorie and regel.kategorie not in self.kategorien:
                meldungen.append(f"Regel '{regel.name}': Kategorie '{regel.kategorie}' unbekannt.")
            ziel = regel.ordner or (regel.sonderordner or {}).get("ordner")
            if ziel and ziel not in self.ordner:
                meldungen.append(
                    f"Regel '{regel.name}': Ordner '{ziel}' ist unter 'ordner' nicht aufgeführt."
                )
            elif ziel and ziel != "Posteingang" and not self.ordner.get(ziel):
                meldungen.append(
                    f"Regel '{regel.name}': Ordner '{ziel}' hat noch keine ID (Mails bleiben liegen)."
                )
            if not any(
                [
                    regel.absender_oder_empfaenger,
                    regel.absender_domains,
                    regel.absender_muster,
                    regel.nur_lieferanten,
                    regel.stichworte,
                    regel.betreff_beginnt,
                    regel.zusatz_absender,
                    regel.zusatz_absender_domains,
                    regel.name == "Rest",
                ]
            ):
                meldungen.append(f"Regel '{regel.name}': keine Bedingung, würde nie greifen.")
        if self.grundsaetze.get("senden_erlaubt", False):
            meldungen.append(
                "Grundsatz 'senden_erlaubt' ist true – der Assistent sendet trotzdem nie selbst."
            )
        return meldungen


def lade_regelwerk(pfad: Path | str | None = None) -> Regelwerk:
    datei = Path(pfad) if pfad else STANDARD_REGELDATEI
    try:
        roh = json.loads(datei.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RegelFehler(f"Regeldatei nicht gefunden: {datei}") from exc
    except json.JSONDecodeError as exc:
        raise RegelFehler(f"Regeldatei {datei} ist kein gültiges JSON: {exc}") from exc

    for pflicht in ("mailbox", "ordner", "kategorien", "regeln_in_reihenfolge"):
        if pflicht not in roh:
            raise RegelFehler(f"Regeldatei {datei}: Abschnitt '{pflicht}' fehlt.")

    kategorien = {
        name: Kategorie(
            name=name,
            farbe=str(k.get("farbe", "")),
            ordner=str(k.get("ordner", "")),
            abwicklung_ordner=k.get("abwicklung_ordner"),
        )
        for name, k in roh["kategorien"].items()
    }
    return Regelwerk(
        mailbox=str(roh["mailbox"]),
        stand=str(roh.get("stand", "")),
        grundsaetze=dict(roh.get("grundsaetze", {})),
        ordner=dict(roh["ordner"]),
        kategorien=kategorien,
        regeln=[Regel.aus_dict(r) for r in roh["regeln_in_reihenfolge"]],
        lieferanten_domains=[d.lower() for d in roh.get("lieferanten_domains", [])],
        lieferanten_absender_einzeln=[a.lower() for a in roh.get("lieferanten_absender_einzeln", [])],
        technik=dict(roh.get("technik", {})),
        quelle=datei,
    )
