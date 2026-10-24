"""Laufzustand in ``postfach/zustand.json``: letzte Läufe, bearbeitete Mails, erstellte Entwürfe."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

STANDARD_ZUSTANDSDATEI = Path(__file__).resolve().parents[2] / "postfach" / "zustand.json"
_MAX_MERKEN = 3000


class Zustand:
    def __init__(self, pfad: Path | str | None = None) -> None:
        self.pfad = Path(pfad) if pfad else STANDARD_ZUSTANDSDATEI
        self.daten: dict[str, Any] = {
            "laeufe": {},
            "bearbeitet": [],
            "entwuerfe": {},
            "antworten_erhalten": [],
        }
        if self.pfad.exists():
            try:
                self.daten.update(json.loads(self.pfad.read_text(encoding="utf-8")))
            except json.JSONDecodeError:
                pass  # kaputte Datei: mit leerem Zustand weiterarbeiten, beim Speichern überschreiben

    # ------------------------------------------------------------------ Läufe
    def letzter_lauf(self, name: str) -> datetime | None:
        wert = self.daten["laeufe"].get(name, {}).get("zeit")
        if not wert:
            return None
        return datetime.fromisoformat(wert)

    def merke_lauf(self, name: str, *, zeit: datetime | None = None, notiz: str = "") -> None:
        zeit = zeit or datetime.now(UTC)
        self.daten["laeufe"][name] = {"zeit": zeit.isoformat(), "notiz": notiz}

    # ------------------------------------------------------------------ Mails
    def ist_bearbeitet(self, schluessel: str) -> bool:
        return schluessel in self.daten["bearbeitet"]

    def merke_bearbeitet(self, schluessel: str | list[str]) -> None:
        neue = [schluessel] if isinstance(schluessel, str) else list(schluessel)
        for s in neue:
            if s and s not in self.daten["bearbeitet"]:
                self.daten["bearbeitet"].append(s)
        if len(self.daten["bearbeitet"]) > _MAX_MERKEN:
            self.daten["bearbeitet"] = self.daten["bearbeitet"][-_MAX_MERKEN:]

    def merke_entwurf(
        self, mail_schluessel: str, entwurf_id: str, *, betreff: str = "", an: str = ""
    ) -> None:
        self.daten["entwuerfe"][mail_schluessel] = {
            "entwurf_id": entwurf_id,
            "betreff": betreff,
            "an": an,
            "erstellt": datetime.now(UTC).isoformat(),
        }

    def hat_entwurf(self, mail_schluessel: str) -> bool:
        return mail_schluessel in self.daten["entwuerfe"]

    def merke_antwort_erhalten(self, betreff: str, von: str, *, web_link: str | None = None) -> None:
        self.daten["antworten_erhalten"].append(
            {"betreff": betreff, "von": von, "web_link": web_link, "zeit": datetime.now(UTC).isoformat()}
        )
        self.daten["antworten_erhalten"] = self.daten["antworten_erhalten"][-200:]

    # ------------------------------------------------------------------ Speichern
    def speichern(self) -> Path:
        self.pfad.parent.mkdir(parents=True, exist_ok=True)
        self.pfad.write_text(json.dumps(self.daten, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return self.pfad
