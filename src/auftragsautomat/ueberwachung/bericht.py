"""Bericht des Überwachungslaufs: Markdown für den Anwender, JSON für die Streamlit-Seite "Heute".

:func:`schreibe_bericht` legt ``ueberwachung_<JJJJ-MM-TT>.md`` und ``ueberwachung_<JJJJ-MM-TT>.json`` in
``settings.ueberwachung.berichte_ordner`` ab (ein zweiter Lauf am selben Tag ersetzt beide Dateien).
Die JSON-Datei ist ``Ueberwachungsbericht.model_dump_json`` plus die Zusatzfelder ``hinweise`` und
``protokoll`` des Laufs; :func:`lade_letzten_bericht` liest den jüngsten Bericht wieder ein
(:class:`~auftragsautomat.models.Ueberwachungsbericht`, Zusatzfelder über :func:`lade_letzten_bericht_mit_zusatz`).
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from collections.abc import Iterable, Sequence
from datetime import date, datetime
from pathlib import Path

from pydantic import ValidationError

from auftragsautomat.kontakte.aenderungen import bericht as kontakt_bericht
from auftragsautomat.models import (
    Ampel,
    EmailEntwurf,
    Terminaenderung,
    Ueberwachungsbericht,
    UeberwachungsPunkt,
)

logger = logging.getLogger(__name__)

__all__ = [
    "AMPEL_REIHENFOLGE",
    "AMPEL_TEXT",
    "DATEI_PRAEFIX",
    "bericht_dateiname",
    "bericht_markdown",
    "lade_bericht",
    "lade_letzten_bericht",
    "lade_letzten_bericht_mit_zusatz",
    "schreibe_bericht",
]

DATEI_PRAEFIX = "ueberwachung_"
#: Reihenfolge der Ampel in der Tabelle (dringend zuerst).
AMPEL_REIHENFOLGE: tuple[Ampel, ...] = ("rot", "gelb", "gruen", "grau")
#: Anzeigetext je Ampel.
AMPEL_TEXT: dict[str, str] = {"rot": "ROT", "gelb": "GELB", "gruen": "GRÜN", "grau": "GRAU"}
_DATEI_RE = re.compile(rf"^{DATEI_PRAEFIX}(\d{{4}}-\d{{2}}-\d{{2}})\.json$")


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------


def _zelle(text: object) -> str:
    """Zelltext für eine Markdown-Tabelle (ohne Zeilenumbrüche und Pipes)."""
    t = "" if text is None else str(text)
    return t.replace("|", "/").replace("\r", " ").replace("\n", " ").strip()


def _datum(wert: date | datetime | None) -> str:
    if wert is None:
        return "–"
    return wert.strftime("%d.%m.%Y")


def _zeit(wert: datetime | None) -> str:
    return wert.strftime("%d.%m.%Y %H:%M") if wert is not None else "–"


def _sortiert(punkte: Sequence[UeberwachungsPunkt]) -> list[UeberwachungsPunkt]:
    rang = {a: i for i, a in enumerate(AMPEL_REIHENFOLGE)}
    return sorted(punkte, key=lambda p: (rang.get(p.ampel, len(rang)), p.fixtermin.liefertag, p.fixtermin.lieferant_kurz, p.fixtermin.kunde_kurz))


def _tracking_text(punkt: UeberwachungsPunkt) -> str:
    s = punkt.sendung
    if s is None:
        return "–"
    teile = [s.status.value]
    if s.referenz:
        teile.append(f"Ref. {s.referenz}")
    if s.quelle != "keine":
        teile.append(f"Quelle {s.quelle}")
    if s.geplante_zustellung:
        teile.append(f"Zustellung {_datum(s.geplante_zustellung)}")
    return ", ".join(teile)


def _entwurf_zeile(entwurf: EmailEntwurf) -> str:
    an = ", ".join(entwurf.an) if entwurf.an else "ohne Empfänger"
    teile = [f"**{entwurf.typ.value}** – {entwurf.betreff} → {an}"]
    if entwurf.speicherort:
        teile.append(f"Entwurf: `{entwurf.speicherort}`")
    for h in entwurf.hinweise:
        teile.append(f"Hinweis: {h}")
    return "- " + "; ".join(teile)


def _terminaenderung_zeile(t: Terminaenderung) -> str:
    ft = t.fixtermin
    status = "übernommen" if t.uebernommen else "Vorschlag, Freigabe nötig"
    quelle = f" (Mail: {t.quelle.betreff}, {t.quelle.absender}, {_datum(t.quelle.empfangen)})" if t.quelle is not None else ""
    return f"- [{status}] {ft.lieferant_kurz} / {ft.kunde_kurz}: {_datum(ft.liefertag)} → {_datum(t.neues_datum)} – {t.grund}{quelle}"


def bericht_markdown(
    bericht: Ueberwachungsbericht,
    *,
    hinweise: Iterable[str] = (),
    protokoll: Iterable[str] = (),
) -> str:
    """Der Bericht als Markdown: Zusammenfassung, Ampel-Tabelle, Maßnahmen je Fixtermin, Entwürfe,
    Terminänderungen, Kontaktänderungen, Fehler und Hinweise."""
    hinweise = list(hinweise)
    protokoll = list(protokoll)
    zaehler = {a: sum(1 for p in bericht.punkte if p.ampel == a) for a in AMPEL_REIHENFOLGE}
    zeilen: list[str] = [
        f"# Überwachungslauf {_datum(bericht.zeitpunkt)}",
        "",
        f"Stand: {_zeit(bericht.zeitpunkt)} · Zeitraum bis {_datum(bericht.zeitraum_bis)} · "
        f"{len(bericht.punkte)} Fixtermine ({', '.join(f'{AMPEL_TEXT[a]} {n}' for a, n in zaehler.items() if n)})"
        + (" · keine Fixtermine im Zeitraum" if not bericht.punkte else ""),
        "",
        "## Ampel",
        "",
    ]
    if bericht.punkte:
        zeilen += [
            "| Ampel | Liefertag | Lieferant | Kunde | Lief. best. | Sped. best. | Abgeholt | Tracking | Grund |",
            "|---|---|---|---|:-:|:-:|:-:|---|---|",
        ]
        for p in _sortiert(bericht.punkte):
            ft = p.fixtermin
            zeilen.append(
                "| "
                + " | ".join(
                    [
                        AMPEL_TEXT.get(p.ampel, p.ampel),
                        _datum(ft.liefertag),
                        _zelle(ft.lieferant_kurz),
                        _zelle(ft.kunde_kurz),
                        "x" if ft.lieferant_bestaetigt else "",
                        "x" if ft.spedition_bestaetigt else "",
                        "x" if ft.abgeholt else "",
                        _zelle(_tracking_text(p)),
                        _zelle(p.grund),
                    ]
                )
                + " |"
            )
    else:
        zeilen.append("Keine Fixtermine im betrachteten Zeitraum.")
    zeilen += ["", "## Maßnahmen und Vermerke je Fixtermin", ""]
    mit_aktionen = [p for p in _sortiert(bericht.punkte) if p.aktionen]
    if mit_aktionen:
        for p in mit_aktionen:
            ft = p.fixtermin
            zeilen.append(f"### {_datum(ft.liefertag)} – {ft.lieferant_kurz} / {ft.kunde_kurz} ({AMPEL_TEXT.get(p.ampel, p.ampel)})")
            if ft.bestellnummer or ft.ab_nummer or ft.kundenbestellnummer:
                nummern = [f"Best.-Nr {ft.bestellnummer}" if ft.bestellnummer else None, f"AB {ft.ab_nummer}" if ft.ab_nummer else None, f"Kundenbestellnr. {ft.kundenbestellnummer}" if ft.kundenbestellnummer else None]
                zeilen.append(", ".join(n for n in nummern if n) + (f", Zeile {ft.zeile}" if ft.zeile else ""))
            zeilen.extend(f"- {a}" for a in p.aktionen)
            zeilen.append("")
    else:
        zeilen += ["Keine Maßnahmen fällig.", ""]
    zeilen += ["## E-Mail-Entwürfe (nur gespeichert, nie gesendet)", ""]
    zeilen += [_entwurf_zeile(e) for e in bericht.entwuerfe] or ["Keine Entwürfe erzeugt."]
    zeilen += ["", "## Terminänderungen", ""]
    zeilen += [_terminaenderung_zeile(t) for t in bericht.terminaenderungen] or ["Keine Terminänderungen gemeldet."]
    zeilen += ["", "## Kontaktänderungen", "", kontakt_bericht(list(bericht.kontaktaenderungen))]
    zeilen += ["", "## Fehler im Lauf", ""]
    zeilen += [f"- {f}" for f in bericht.fehler] or ["Keine Fehler."]
    zeilen += ["", "## Hinweise", ""]
    zeilen += [f"- {h}" for h in hinweise] or ["Keine Hinweise."]
    if protokoll:
        zeilen += ["", "## Änderungen durch den Lauf", ""]
        zeilen += [f"- {p}" for p in protokoll]
    return "\n".join(zeilen).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Dateien
# ---------------------------------------------------------------------------


def bericht_dateiname(zeitpunkt: datetime | date) -> str:
    """Dateistamm ``ueberwachung_<JJJJ-MM-TT>`` (ohne Endung)."""
    tag = zeitpunkt.date() if isinstance(zeitpunkt, datetime) else zeitpunkt
    return f"{DATEI_PRAEFIX}{tag.isoformat()}"


def _schreibe_atomar(ziel: Path, text: str) -> None:
    ziel.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{ziel.stem}_", suffix=ziel.suffix, dir=str(ziel.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, ziel)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def schreibe_bericht(
    bericht: Ueberwachungsbericht,
    ordner: Path | str,
    *,
    hinweise: Iterable[str] = (),
    protokoll: Iterable[str] = (),
) -> Path:
    """Schreibt Markdown und JSON nach ``ordner`` (atomar) und liefert den Pfad der Markdown-Datei."""
    ordner = Path(ordner)
    stamm = bericht_dateiname(bericht.zeitpunkt)
    hinweise, protokoll = list(hinweise), list(protokoll)
    md = ordner / f"{stamm}.md"
    _schreibe_atomar(md, bericht_markdown(bericht, hinweise=hinweise, protokoll=protokoll))
    daten = json.loads(bericht.model_dump_json())
    daten["hinweise"] = hinweise
    daten["protokoll"] = protokoll
    _schreibe_atomar(ordner / f"{stamm}.json", json.dumps(daten, ensure_ascii=False, indent=2) + "\n")
    logger.info("Überwachungsbericht geschrieben: %s (+ .json)", md)
    return md


def lade_bericht(pfad: Path | str) -> Ueberwachungsbericht | None:
    """Liest eine JSON-Berichtsdatei; ``None`` bei fehlender oder kaputter Datei (Warnung)."""
    ergebnis = lade_letzten_bericht_mit_zusatz(Path(pfad).parent, datei=Path(pfad))
    return ergebnis[0] if ergebnis is not None else None


def _berichtsdateien(ordner: Path) -> list[Path]:
    if not ordner.is_dir():
        return []
    dateien = [p for p in ordner.iterdir() if p.is_file() and _DATEI_RE.match(p.name)]
    return sorted(dateien, key=lambda p: (p.name, p.stat().st_mtime))


def lade_letzten_bericht_mit_zusatz(ordner: Path | str, *, datei: Path | None = None) -> tuple[Ueberwachungsbericht, list[str], list[str]] | None:
    """Jüngster Bericht aus ``ordner`` (nach Datum im Dateinamen) mit ``hinweise`` und ``protokoll``;
    ``None``, wenn es keinen lesbaren Bericht gibt."""
    ordner = Path(ordner)
    kandidaten = [datei] if datei is not None else list(reversed(_berichtsdateien(ordner)))
    for pfad in kandidaten:
        if not pfad.is_file():
            continue
        try:
            daten = json.loads(pfad.read_text(encoding="utf-8"))
            hinweise = [str(h) for h in daten.pop("hinweise", []) or []]
            protokoll = [str(p) for p in daten.pop("protokoll", []) or []]
            return Ueberwachungsbericht.model_validate(daten), hinweise, protokoll
        except (OSError, json.JSONDecodeError, ValidationError, AttributeError) as exc:
            logger.warning("Bericht %s nicht lesbar: %s", pfad, exc)
    return None


def lade_letzten_bericht(ordner: Path | str) -> Ueberwachungsbericht | None:
    """Jüngster Bericht aus ``ordner`` als :class:`Ueberwachungsbericht` (für die Streamlit-Seite "Heute")."""
    ergebnis = lade_letzten_bericht_mit_zusatz(ordner)
    return ergebnis[0] if ergebnis is not None else None
