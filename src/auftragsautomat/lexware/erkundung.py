"""Lexware-Oberfläche erkunden: Control-Baum NUR LESEND auslesen und mit den Control-Bezeichnern abgleichen.

CLI: ``auftragsautomat lexware-erkunden [--ziel] [--tiefe] [--alle-fenster] [--backend uia|win32|beide]
[--abgleich] [--aus-datei baum.json]``.

Zweck: Die Bezeichner in ``config/lexware_controls.yaml`` (``LexwareControls``) sind großteils Annahmen
(``TODO-LEXWARE``). Dieser Befehl liest am laufenden Lexware den Control-Baum aus und zeigt je Eintrag,
ob er gefunden wurde, fehlt oder mehrdeutig ist – mit den ähnlichsten Kandidaten als Vorschlag.

Sicherheitsregeln (hart):

* Nur lesen: kein Klick, kein Tastendruck, kein Fokus, kein Schließen, kein Start von Lexware. Gelesen
  werden ausschließlich ``element_info`` (Typ, Name, automation_id, Klasse, control_id), ``children()``,
  ``rectangle()``, ``is_visible()``, ``is_enabled()`` und ``friendly_class_name()``. Werte werden nicht
  gezielt gelesen (kein ``get_value``/``window_text``); beim win32-Backend ist der "Name" eines
  Eingabefelds allerdings dessen Text, und Listen-/Tabellenzeilen tragen ihren Inhalt im Namen.
* Nur andocken: läuft der Lexware-Prozess nicht, bricht die Erkundung mit "Bitte Lexware starten und die
  gewünschte Firma öffnen" ab (``LexwareNichtGestartet``).
* Die Ausgabe kann trotzdem Kundendaten enthalten (Fenstertitel, Beschriftungen, Listeneinträge geöffneter
  Fenster). Sie gehört nach ``logs/`` (nicht im Git) und wird nicht weitergegeben.

Ausgabe (Ordner ``--ziel``): ``baum.json`` (maschinenlesbar, Grundlage für ``--aus-datei``), ``baum.txt``
(eingerückt) und mit ``--abgleich`` ``abgleich.md``.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from rapidfuzz import fuzz
from rapidfuzz import utils as fuzz_utils

from auftragsautomat.lexware import instanz
from auftragsautomat.lexware.instanz import PROZESSNAME_STANDARD, LexwareNichtGestartet
from auftragsautomat.lexware.ui_automation import (
    ControlSpec,
    Fenster,
    FensterNichtGefunden,
    LexwareControls,
)

if TYPE_CHECKING:
    from auftragsautomat.config import Settings

logger = logging.getLogger(__name__)

__all__ = [
    "BAUM_FORMAT",
    "DATENSCHUTZ_HINWEIS",
    "MAX_ELEMENTE_STANDARD",
    "TIEFE_STANDARD",
    "Abgleich",
    "AbgleichEintrag",
    "ElementDaten",
    "Erkundung",
    "ErkundungsFehler",
    "ErkundungsQuelle",
    "FensterBaum",
    "Status",
    "Vorschlag",
    "erfasse_fenster",
    "erkunde",
    "gleiche_ab",
    "lade_erkundung",
    "schreibe_abgleich_md",
    "schreibe_baum_json",
    "schreibe_baum_txt",
    "standard_zielordner",
    "todo_eintraege",
]

BAUM_FORMAT = "auftragsautomat-lexware-erkundung/1"
TIEFE_STANDARD = 12
MAX_ELEMENTE_STANDARD = 20000
MAX_FEHLERMELDUNGEN = 20
VORSCHLAEGE_ANZAHL = 3
VORSCHLAG_MINDESTPUNKTE = 50.0
TYP_BONUS = 10.0
DATENSCHUTZ_HINWEIS = (
    "Die Dateien können Kundendaten aus geöffneten Lexware-Fenstern enthalten. "
    "Sie liegen unter logs/ (nicht im Git) – nicht weitergeben."
)
NICHT_GESTARTET_TEXT = "Bitte Lexware starten und die gewünschte Firma öffnen."


class ErkundungsFehler(Exception):
    """Erkundung bzw. Abgleich nicht möglich (z. B. baum.json unlesbar)."""


class ErkundungsQuelle(Protocol):
    """Was die Erkundung vom Backend braucht (``PywinautoBackend`` erfüllt das; Tests nutzen Fakes)."""

    backend_name: str
    controls: LexwareControls

    def prozess_laeuft(self) -> bool: ...

    def aktive_firma(self) -> str: ...

    def lese_fenster_fuer_erkundung(self, *, alle_fenster: bool = False, timeout: float | None = None) -> list[Fenster]: ...


# ---------------------------------------------------------------------------
# Datenmodell
# ---------------------------------------------------------------------------


@dataclass
class ElementDaten:
    """Ein gelesenes Control. ``pfad`` = Kindindizes ab dem Fenster ("0" = Fenster, "0.3.1" = …)."""

    pfad: str
    ebene: int
    control_type: str | None = None
    name: str | None = None
    auto_id: str | None = None
    class_name: str | None = None
    control_id: int | None = None
    sichtbar: bool | None = None
    aktiviert: bool | None = None
    rechteck: list[int] | None = None
    # friendly_class_name() (nur Anzeige/Orientierung). ``control_type`` ist dagegen genau der Wert, den
    # pywinauto bei ``child_window(control_type=…)`` vergleicht (win32: meist leer -> None).
    klasse_freundlich: str | None = None

    @property
    def typ(self) -> str | None:
        """Typ zur Anzeige und für Menü-/Spaltenprüfung: control_type, sonst friendly_class_name."""
        return self.control_type or self.klasse_freundlich

    @classmethod
    def aus_dict(cls, daten: dict[str, Any]) -> ElementDaten:
        bekannt = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in daten.items() if k in bekannt})


@dataclass
class FensterBaum:
    backend: str
    titel: str
    art: str
    elemente: list[ElementDaten] = field(default_factory=list)
    uebersprungen: int = 0  # Controls, deren element_info nicht lesbar war (samt Unterbaum ausgelassen)
    lesefehler: int = 0  # einzelne Eigenschaften/children(), die eine Ausnahme warfen
    fehlermeldungen: list[str] = field(default_factory=list)
    abgeschnitten: bool = False

    @property
    def fehler(self) -> int:
        return self.uebersprungen + self.lesefehler

    def als_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "titel": self.titel,
            "art": self.art,
            "anzahl": len(self.elemente),
            "uebersprungen": self.uebersprungen,
            "lesefehler": self.lesefehler,
            "fehlermeldungen": list(self.fehlermeldungen),
            "abgeschnitten": self.abgeschnitten,
            "elemente": [asdict(e) for e in self.elemente],
        }

    @classmethod
    def aus_dict(cls, daten: dict[str, Any]) -> FensterBaum:
        return cls(
            backend=str(daten.get("backend") or "uia"),
            titel=str(daten.get("titel") or ""),
            art=str(daten.get("art") or "fenster"),
            elemente=[ElementDaten.aus_dict(e) for e in daten.get("elemente") or []],
            uebersprungen=int(daten.get("uebersprungen") or 0),
            lesefehler=int(daten.get("lesefehler") or 0),
            fehlermeldungen=[str(m) for m in daten.get("fehlermeldungen") or []],
            abgeschnitten=bool(daten.get("abgeschnitten")),
        )


@dataclass
class Erkundung:
    """Ergebnis einer Erkundung (bzw. aus ``baum.json`` geladen)."""

    zeitpunkt: str
    backends: list[str]
    prozessname: str
    prozess_laeuft: bool
    hauptfenster_titel: str | None
    aktive_firma: str | None
    firma_id: str | None
    tiefe: int
    alle_fenster: bool
    max_elemente: int
    fenster: list[FensterBaum] = field(default_factory=list)
    backend_fehler: dict[str, str] = field(default_factory=dict)
    hinweis: str = DATENSCHUTZ_HINWEIS

    @property
    def anzahl_elemente(self) -> int:
        return sum(len(f.elemente) for f in self.fenster)

    @property
    def fehler(self) -> int:
        return sum(f.fehler for f in self.fenster)

    @property
    def abgeschnitten(self) -> bool:
        return any(f.abgeschnitten for f in self.fenster)

    def fenster_von(self, backend: str) -> list[FensterBaum]:
        return [f for f in self.fenster if f.backend == backend]

    def als_dict(self) -> dict[str, Any]:
        return {
            "format": BAUM_FORMAT,
            "kopf": {
                "zeitpunkt": self.zeitpunkt,
                "backends": list(self.backends),
                "prozessname": self.prozessname,
                "prozess_laeuft": self.prozess_laeuft,
                "hauptfenster_titel": self.hauptfenster_titel,
                "fenster_titel": [f"[{f.backend}] {f.titel}" for f in self.fenster],
                "aktive_firma": self.aktive_firma,
                "firma_id": self.firma_id,
                "anzahl_elemente": self.anzahl_elemente,
                "fehler": self.fehler,
                "abgeschnitten": self.abgeschnitten,
                "max_elemente": self.max_elemente,
                "tiefe": self.tiefe,
                "alle_fenster": self.alle_fenster,
                "backend_fehler": dict(self.backend_fehler),
                "hinweis": self.hinweis,
            },
            "fenster": [f.als_dict() for f in self.fenster],
        }

    @classmethod
    def aus_dict(cls, daten: dict[str, Any]) -> Erkundung:
        if not isinstance(daten, dict) or daten.get("format") != BAUM_FORMAT:
            raise ErkundungsFehler(f"keine Erkundungsdatei im Format {BAUM_FORMAT!r} (baum.json einer früheren Erkundung erwartet)")
        kopf = daten.get("kopf") or {}
        fenster = [FensterBaum.aus_dict(f) for f in daten.get("fenster") or []]
        backends = [str(b) for b in kopf.get("backends") or []] or sorted({f.backend for f in fenster})
        return cls(
            zeitpunkt=str(kopf.get("zeitpunkt") or ""),
            backends=backends,
            prozessname=str(kopf.get("prozessname") or PROZESSNAME_STANDARD),
            prozess_laeuft=bool(kopf.get("prozess_laeuft")),
            hauptfenster_titel=kopf.get("hauptfenster_titel"),
            aktive_firma=kopf.get("aktive_firma"),
            firma_id=kopf.get("firma_id"),
            tiefe=int(kopf.get("tiefe") or TIEFE_STANDARD),
            alle_fenster=bool(kopf.get("alle_fenster")),
            max_elemente=int(kopf.get("max_elemente") or MAX_ELEMENTE_STANDARD),
            fenster=fenster,
            backend_fehler={str(k): str(v) for k, v in (kopf.get("backend_fehler") or {}).items()},
        )


# ---------------------------------------------------------------------------
# Baum erfassen (nur lesend)
# ---------------------------------------------------------------------------


class _Budget:
    """Gemeinsame Obergrenze der Elemente über alle Fenster und Backends."""

    def __init__(self, maximum: int) -> None:
        self.maximum = max(int(maximum), 1)
        self.verbraucht = 0

    @property
    def erschoepft(self) -> bool:
        return self.verbraucht >= self.maximum


_FEHLT = object()


class _Leser:
    """Liest Eigenschaften fehlertolerant; jede Ausnahme (außer fehlendem Attribut) wird gezählt."""

    def __init__(self, baum: FensterBaum) -> None:
        self.baum = baum

    def _melde(self, pfad: str, was: str, exc: BaseException) -> None:
        self.baum.lesefehler += 1
        if len(self.baum.fehlermeldungen) < MAX_FEHLERMELDUNGEN:
            self.baum.fehlermeldungen.append(f"{pfad}: {was}: {type(exc).__name__}: {exc}")

    def attribut(self, objekt: Any, name: str, pfad: str) -> Any:
        try:
            return getattr(objekt, name)
        except AttributeError:
            return _FEHLT
        except Exception as exc:  # noqa: BLE001 - COMError, ElementNotAvailable, …
            self._melde(pfad, name, exc)
            return _FEHLT

    def aufruf(self, objekt: Any, name: str, pfad: str) -> Any:
        try:
            methode = getattr(objekt, name)
        except AttributeError:
            return _FEHLT
        except Exception as exc:  # noqa: BLE001
            self._melde(pfad, name, exc)
            return _FEHLT
        try:
            return methode()
        except Exception as exc:  # noqa: BLE001
            self._melde(pfad, f"{name}()", exc)
            return _FEHLT


def _text(wert: Any) -> str | None:
    if wert is _FEHLT or wert is None:
        return None
    text = str(wert)
    return text if text != "" else None


def _ganzzahl(wert: Any) -> int | None:
    if wert is _FEHLT or wert is None or isinstance(wert, bool):
        return None
    try:
        return int(wert)
    except (TypeError, ValueError):
        return None


def _wahrheit(wert: Any) -> bool | None:
    if wert is _FEHLT or wert is None:
        return None
    return bool(wert)


def _rechteck(wert: Any) -> list[int] | None:
    if wert is _FEHLT or wert is None:
        return None
    try:
        return [int(wert.left), int(wert.top), int(wert.right), int(wert.bottom)]
    except Exception:  # noqa: BLE001
        pass
    try:
        werte = [int(v) for v in wert]
    except Exception:  # noqa: BLE001
        return None
    return werte if len(werte) == 4 else None


def _lies_element(leser: _Leser, wrapper: Any, pfad: str, ebene: int) -> ElementDaten | None:
    """Liest ein Control. ``None``, wenn schon ``element_info`` nicht lesbar ist (Control übersprungen)."""
    try:
        info = wrapper.element_info
    except Exception as exc:  # noqa: BLE001
        leser.baum.uebersprungen += 1
        if len(leser.baum.fehlermeldungen) < MAX_FEHLERMELDUNGEN:
            leser.baum.fehlermeldungen.append(f"{pfad}: element_info: {type(exc).__name__}: {exc}")
        return None
    # control_type unverfälscht speichern: pywinauto vergleicht child_window(control_type=…) mit genau
    # diesem Wert (win32: HwndElementInfo.control_type ist außer bei WinForms leer). Der friendly_class_name
    # steht getrennt in ``klasse_freundlich`` – sonst meldet der Abgleich unter win32 falsche Treffer.
    control_type = _text(leser.attribut(info, "control_type", pfad))
    klasse_freundlich = _text(leser.aufruf(wrapper, "friendly_class_name", pfad))
    auto_id = _text(leser.attribut(info, "automation_id", pfad))
    if auto_id is None:
        auto_id = _text(leser.attribut(info, "auto_id", pfad))
    rechteck = _rechteck(leser.aufruf(wrapper, "rectangle", pfad))
    if rechteck is None:
        rechteck = _rechteck(leser.attribut(info, "rectangle", pfad))
    sichtbar = _wahrheit(leser.aufruf(wrapper, "is_visible", pfad))
    aktiviert = _wahrheit(leser.aufruf(wrapper, "is_enabled", pfad))
    return ElementDaten(
        pfad=pfad,
        ebene=ebene,
        control_type=control_type,
        name=_text(leser.attribut(info, "name", pfad)),
        auto_id=auto_id,
        class_name=_text(leser.attribut(info, "class_name", pfad)),
        control_id=_ganzzahl(leser.attribut(info, "control_id", pfad)),
        sichtbar=sichtbar,
        aktiviert=aktiviert,
        rechteck=rechteck,
        klasse_freundlich=klasse_freundlich,
    )


def erfasse_fenster(
    fenster: Fenster,
    *,
    backend: str,
    tiefe: int = TIEFE_STANDARD,
    max_elemente: int = MAX_ELEMENTE_STANDARD,
    budget: _Budget | None = None,
) -> FensterBaum:
    """Liest den Control-Baum eines Fensters bis ``tiefe`` (Fenster = Ebene 0), vorwärts in Baumreihenfolge.

    Nur lesend. Controls, die beim Lesen Ausnahmen werfen, werden übersprungen bzw. gezählt; bei
    Erreichen der Obergrenze wird abgebrochen und ``abgeschnitten`` gesetzt.
    """
    budget = budget or _Budget(max_elemente)
    baum = FensterBaum(backend=backend, titel=fenster.titel or "", art=fenster.art or "fenster")
    leser = _Leser(baum)
    tiefe = max(int(tiefe), 0)
    stapel: list[tuple[Any, str, int]] = [(fenster.roh, "0", 0)]
    while stapel:
        if budget.erschoepft:
            baum.abgeschnitten = True
            break
        wrapper, pfad, ebene = stapel.pop()
        element = _lies_element(leser, wrapper, pfad, ebene)
        if element is None:
            continue
        baum.elemente.append(element)
        budget.verbraucht += 1
        if ebene >= tiefe:
            continue
        kinder = leser.aufruf(wrapper, "children", pfad)
        if kinder is _FEHLT or kinder is None:
            continue
        try:
            kinder = list(kinder)
        except Exception as exc:  # noqa: BLE001
            leser._melde(pfad, "children()", exc)
            continue
        for index in range(len(kinder) - 1, -1, -1):
            stapel.append((kinder[index], f"{pfad}.{index}", ebene + 1))
    return baum


def _nicht_gestartet_text(prozessname: str) -> str:
    return (
        f"Lexware läuft nicht (Prozess {prozessname} nicht gefunden). {NICHT_GESTARTET_TEXT} "
        "Falls Lexware doch läuft: Prozessnamen im Task-Manager (Details) prüfen und PROZESSNAME in "
        "config/lexware_controls.yaml anpassen."
    )


def _prozess_kandidaten() -> list[str]:  # pragma: no cover - nur Windows
    """Laufende Prozesse, die nach Lexware aussehen (Hinweis, wenn PROZESSNAME nicht passt)."""
    try:
        namen = instanz._prozessliste_windows()
    except Exception:  # noqa: BLE001
        return []
    return sorted({n for n in namen if n and re.search(r"(?i)lex|framework", n)})


def _standard_quelle(settings: Settings, backend_name: str) -> ErkundungsQuelle:
    """``PywinautoBackend`` (wirft ``NichtAufWindows``/``BackendNichtVerfuegbar`` außerhalb von Windows)."""
    from auftragsautomat.lexware.ui_automation import PywinautoBackend

    return PywinautoBackend(settings, backend_name=backend_name)


def _firma_aus_titel(settings: Settings, titel: str | None) -> str | None:
    if not titel:
        return None
    treffer = [f.id for f in settings.firmen.values() if f.lexware_firmenname and f.lexware_firmenname.casefold() in titel.casefold()]
    return treffer[0] if len(treffer) == 1 else None


def erkunde(
    settings: Settings,
    *,
    backends: Sequence[str],
    tiefe: int = TIEFE_STANDARD,
    alle_fenster: bool = False,
    max_elemente: int = MAX_ELEMENTE_STANDARD,
    quelle_fabrik: Callable[[str], ErkundungsQuelle] | None = None,
    jetzt: datetime | None = None,
) -> Erkundung:
    """Dockt an das laufende Lexware an und liest die Control-Bäume (je Backend). Nur lesend.

    Raises:
        NichtAufWindows / BackendNichtVerfuegbar: aus ``PywinautoBackend`` (kein Windows, kein pywinauto).
        LexwareNichtGestartet: Prozess läuft nicht bzw. kein Hauptfenster gefunden.
    """
    if not backends:
        raise ValueError("mindestens ein Backend (uia/win32) angeben")
    fabrik = quelle_fabrik or (lambda name: _standard_quelle(settings, name))
    erste = fabrik(backends[0])
    prozessname = str(getattr(getattr(erste, "controls", None), "PROZESSNAME", PROZESSNAME_STANDARD))
    if not erste.prozess_laeuft():
        text = _nicht_gestartet_text(prozessname)
        kandidaten = _prozess_kandidaten() if quelle_fabrik is None else []
        if kandidaten:
            text += f" Gefundene ähnliche Prozesse: {', '.join(kandidaten)}."
        raise LexwareNichtGestartet(text)

    timeout = min(max(float(settings.lexware.maximale_wartezeit_sekunden), 1.0), 10.0)
    budget = _Budget(max_elemente)
    erkundung = Erkundung(
        zeitpunkt=(jetzt or datetime.now()).isoformat(timespec="seconds"),
        backends=list(backends),
        prozessname=prozessname,
        prozess_laeuft=True,
        hauptfenster_titel=None,
        aktive_firma=None,
        firma_id=None,
        tiefe=int(tiefe),
        alle_fenster=bool(alle_fenster),
        max_elemente=budget.maximum,
    )
    for name in backends:
        try:
            quelle = erste if name == backends[0] else fabrik(name)
            liste = quelle.lese_fenster_fuer_erkundung(alle_fenster=alle_fenster, timeout=timeout)
        except FensterNichtGefunden as exc:
            logger.warning("Backend %s: %s", name, exc)
            erkundung.backend_fehler[name] = str(exc)
            continue
        except Exception as exc:  # noqa: BLE001 - COMError u. a.: anderes Backend trotzdem auswerten
            logger.warning("Backend %s nicht lesbar: %s: %s", name, type(exc).__name__, exc)
            erkundung.backend_fehler[name] = f"{type(exc).__name__}: {exc}"
            continue
        if erkundung.aktive_firma is None:
            try:
                erkundung.aktive_firma = instanz.aktive_firma(quelle)  # type: ignore[arg-type]
            except Exception as exc:  # noqa: BLE001 - nur Information für den Bericht
                logger.info("Aktive Firma nicht ermittelbar: %s", exc)
        for fenster in liste:
            if erkundung.hauptfenster_titel is None and fenster.art == "haupt":
                erkundung.hauptfenster_titel = fenster.titel
            logger.info("Lese %s-Baum von %s", name, fenster)
            erkundung.fenster.append(erfasse_fenster(fenster, backend=name, tiefe=tiefe, budget=budget))
    if not erkundung.fenster:
        details = "; ".join(f"{k}: {v}" for k, v in erkundung.backend_fehler.items())
        raise LexwareNichtGestartet(
            f"Lexware-Hauptfenster nicht gefunden (Titel ~ {settings.lexware.fenster_titel_regex!r}). "
            f"{NICHT_GESTARTET_TEXT} Sonst settings.lexware.fenster_titel_regex prüfen. ({details})"
        )
    erkundung.firma_id = _firma_aus_titel(settings, erkundung.aktive_firma or erkundung.hauptfenster_titel)
    return erkundung


# ---------------------------------------------------------------------------
# Dateien
# ---------------------------------------------------------------------------


def standard_zielordner(log_ordner: Path, jetzt: datetime | None = None) -> Path:
    """``<log_ordner>/lexware_erkundung/<JJJJ-MM-TT_HHMMSS>`` (bei Kollision mit Zähler)."""
    basis = Path(log_ordner) / "lexware_erkundung"
    stempel = (jetzt or datetime.now()).strftime("%Y-%m-%d_%H%M%S")
    ziel = basis / stempel
    zaehler = 2
    while ziel.exists():
        ziel = basis / f"{stempel}_{zaehler}"
        zaehler += 1
    return ziel


def schreibe_baum_json(erkundung: Erkundung, ziel: Path) -> Path:
    ziel = Path(ziel)
    ziel.parent.mkdir(parents=True, exist_ok=True)
    ziel.write_text(json.dumps(erkundung.als_dict(), ensure_ascii=False, indent=1), encoding="utf-8")
    return ziel


def lade_erkundung(pfad: Path) -> Erkundung:
    pfad = Path(pfad)
    if not pfad.is_file():
        raise ErkundungsFehler(f"Datei {pfad} nicht gefunden")
    try:
        daten = json.loads(pfad.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ErkundungsFehler(f"{pfad} ist keine lesbare JSON-Datei: {exc}") from exc
    try:
        return Erkundung.aus_dict(daten)
    except ErkundungsFehler as exc:
        raise ErkundungsFehler(f"{pfad}: {exc}") from exc
    except (TypeError, ValueError) as exc:
        raise ErkundungsFehler(f"{pfad}: unerwarteter Aufbau: {exc}") from exc


def _kuerze(text: str | None, laenge: int = 120) -> str:
    if text is None:
        return ""
    text = " ".join(str(text).split())
    return text if len(text) <= laenge else text[: laenge - 1] + "…"


def _typ_anzeige(e: ElementDaten) -> str:
    if e.control_type:
        return e.control_type
    return f"[{e.klasse_freundlich}]" if e.klasse_freundlich else "?"


def _element_zeile(e: ElementDaten) -> str:
    teile = [_typ_anzeige(e)]
    if e.name is not None:
        teile.append(json.dumps(_kuerze(e.name), ensure_ascii=False))
    if e.auto_id is not None:
        teile.append(f"auto_id={e.auto_id!r}")
    if e.class_name is not None:
        teile.append(f"class={e.class_name!r}")
    if e.control_id is not None:
        teile.append(f"id={e.control_id}")
    if e.sichtbar is False:
        teile.append("[unsichtbar]")
    if e.aktiviert is False:
        teile.append("[deaktiviert]")
    if e.rechteck is not None:
        teile.append("(" + ",".join(str(v) for v in e.rechteck) + ")")
    teile.append(f"#{e.pfad}")
    return " ".join(teile)


def _kopfzeilen(erkundung: Erkundung) -> list[str]:
    zeilen = [
        f"Zeitpunkt:      {erkundung.zeitpunkt}",
        f"Backend(s):     {', '.join(erkundung.backends)}",
        f"Prozess:        {erkundung.prozessname} ({'läuft' if erkundung.prozess_laeuft else 'nicht gefunden'})",
        f"Hauptfenster:   {erkundung.hauptfenster_titel or '-'}",
        f"Aktive Firma:   {erkundung.aktive_firma or '-'}" + (f" (Firma-ID {erkundung.firma_id})" if erkundung.firma_id else ""),
        f"Fenster:        {len(erkundung.fenster)}" + ("" if erkundung.alle_fenster else " (nur Hauptfenster; --alle-fenster für Dialoge)"),
        f"Elemente:       {erkundung.anzahl_elemente} (Tiefe {erkundung.tiefe}, Fehler beim Lesen {erkundung.fehler})",
    ]
    if erkundung.abgeschnitten:
        zeilen.append(f"ABGESCHNITTEN:  Obergrenze von {erkundung.max_elemente} Elementen erreicht – Tiefe verringern oder einzelne Fenster erkunden")
    for name, text in erkundung.backend_fehler.items():
        zeilen.append(f"Backend {name}: {text}")
    zeilen.append(f"HINWEIS: {erkundung.hinweis}")
    return zeilen


def schreibe_baum_txt(erkundung: Erkundung, ziel: Path) -> Path:
    ziel = Path(ziel)
    ziel.parent.mkdir(parents=True, exist_ok=True)
    zeilen = ["Lexware-Erkundung (nur lesend)", *_kopfzeilen(erkundung), ""]
    for f in erkundung.fenster:
        kopf = f"=== [{f.backend}] {f.art} {f.titel!r}: {len(f.elemente)} Elemente"
        if f.fehler:
            kopf += f", {f.uebersprungen} übersprungen, {f.lesefehler} Lesefehler"
        if f.abgeschnitten:
            kopf += ", ABGESCHNITTEN"
        zeilen.append(kopf + " ===")
        for e in f.elemente:
            zeilen.append("  " * e.ebene + _element_zeile(e))
        for meldung in f.fehlermeldungen:
            zeilen.append(f"  ! {meldung}")
        zeilen.append("")
    ziel.write_text("\n".join(zeilen), encoding="utf-8")
    return ziel


# ---------------------------------------------------------------------------
# Abgleich mit config/lexware_controls.yaml
# ---------------------------------------------------------------------------


class Status(StrEnum):
    GEFUNDEN = "gefunden"
    NICHT_GEFUNDEN = "nicht gefunden"
    MEHRDEUTIG = "mehrdeutig"
    NICHT_PRUEFBAR = "nicht prüfbar"


@dataclass
class Treffer:
    fenster: str
    element: ElementDaten


@dataclass
class Vorschlag:
    punkte: float
    fenster: str
    element: ElementDaten

    def yaml_wert(self) -> str:
        """Vorschlag im Format von config/lexware_controls.yaml (zum Übernehmen nach Prüfung)."""
        teile: list[str] = []
        e = self.element
        if e.name:
            teile.append(f"title: {json.dumps(e.name, ensure_ascii=False)}")
        if e.auto_id:
            teile.append(f"auto_id: {json.dumps(e.auto_id, ensure_ascii=False)}")
        if e.control_type:
            teile.append(f"control_type: {json.dumps(e.control_type, ensure_ascii=False)}")
        if not e.name and not e.auto_id and e.class_name:
            teile.append(f"class_name: {json.dumps(e.class_name, ensure_ascii=False)}")
        return "{" + ", ".join(teile) + "}"


@dataclass
class AbgleichEintrag:
    name: str
    art: str  # control | fenstertitel | spalte | menue | text | regex | taste | prozess
    soll: str
    status: Status
    treffer: list[Treffer] = field(default_factory=list)
    vorschlaege: list[Vorschlag] = field(default_factory=list)
    bemerkung: str = ""
    todo: str | None = None


@dataclass
class Abgleich:
    backend: str
    eintraege: list[AbgleichEintrag]

    def anzahl(self, status: Status) -> int:
        return sum(1 for e in self.eintraege if e.status == status)

    def bilanz_text(self) -> str:
        return (
            f"{self.anzahl(Status.GEFUNDEN)} gefunden, {self.anzahl(Status.NICHT_GEFUNDEN)} fehlen, "
            f"{self.anzahl(Status.MEHRDEUTIG)} mehrdeutig, {self.anzahl(Status.NICHT_PRUEFBAR)} nicht prüfbar"
        )


_TODO_SCHLUESSEL = re.compile(r"^\s+([A-Z][A-Z0-9_]*)\s*:")


def todo_eintraege(controls_datei: Path | None) -> dict[str, str]:
    """``TODO-LEXWARE``-Einträge der YAML-Datei: Bezeichner -> Kommentartext (Kommentar davor oder dahinter)."""
    if controls_datei is None or not Path(controls_datei).is_file():
        return {}
    ergebnis: dict[str, str] = {}
    offen: str | None = None
    for zeile in Path(controls_datei).read_text(encoding="utf-8").splitlines():
        rein = zeile.strip()
        if rein.startswith("#"):
            if "TODO-LEXWARE" in rein:
                offen = rein.lstrip("#").strip()
            continue
        treffer = _TODO_SCHLUESSEL.match(zeile)
        if treffer is None:
            if not rein:
                offen = None
            continue
        name = treffer.group(1)
        inline = zeile.split(" #", 1)[1].strip() if " #" in zeile else ""
        if "TODO-LEXWARE" in inline:
            ergebnis[name] = inline
        elif offen is not None:
            ergebnis[name] = offen
        offen = None
    return ergebnis


def _gleich(a: str | None, b: str | None) -> bool:
    return (a or "").casefold() == (b or "").casefold()


def _spec_passt(spec: ControlSpec, e: ElementDaten) -> bool:
    """Wie pywinauto ``child_window``: title exakt, title_re mit ``re.match``, auto_id/class_name exakt."""
    if spec.title is not None and (e.name or "") != spec.title:
        return False
    if spec.title_re is not None:
        try:
            if re.match(spec.title_re, e.name or "") is None:
                return False
        except re.error:
            return False
    if spec.auto_id is not None and (e.auto_id or "") != spec.auto_id:
        return False
    if spec.class_name is not None and (e.class_name or "") != spec.class_name:
        return False
    if spec.control_type is not None and (e.control_type or "") != spec.control_type:
        return False  # pywinauto vergleicht exakt (win32: control_type meist leer -> kein Treffer)
    return True


def _hat_kriterium(spec: ControlSpec) -> bool:
    return any(v is not None for v in (spec.title, spec.title_re, spec.auto_id, spec.class_name, spec.control_type))


def _sichtbar(e: ElementDaten) -> bool:
    return e.sichtbar is not False


def _suchbegriffe_aus_regex(regex: str) -> list[str]:
    roh = re.sub(r"\\[sdwSDW]|\\.", " ", regex)
    teile = [re.sub(r"[.*+?^$()\[\]{}]", " ", t) for t in roh.split("|")]
    return [" ".join(t.split()) for t in teile if t.strip() and len(" ".join(t.split())) >= 2]


def _suchbegriffe(spec: ControlSpec) -> list[str]:
    begriffe: list[str] = []
    if spec.title:
        begriffe.append(spec.title)
    if spec.title_re:
        begriffe.extend(_suchbegriffe_aus_regex(spec.title_re))
    if spec.auto_id:
        begriffe.append(spec.auto_id)
    if spec.class_name:
        begriffe.append(spec.class_name)
    return begriffe


# Controls, die etwas auslösen, was nur nach ausdrücklichem Ja geschehen darf. Ein Kandidat aus einer dieser
# Gruppen wird nur vorgeschlagen, wenn der gesuchte Bezeichner selbst zu derselben Gruppe gehört – sonst
# könnte ein übernommener Vorschlag z. B. WEITER auf "Speichern" legen.
KRITISCHE_AKTIONEN: dict[str, re.Pattern[str]] = {
    "speichern": re.compile(r"speicher|save", re.I),
    "drucken": re.compile(r"druck|print", re.I),
    "mail/senden": re.compile(r"mail|senden|versend|send", re.I),
    "löschen": re.compile(r"lösch|losch|loesch|delete|entfern", re.I),
    "buchen": re.compile(r"buchen|festschreib|fertigstell", re.I),
    "weiterführen": re.compile(r"weiterf(ü|ue|u)hr", re.I),
}


def _kritische_gruppen(texte: Iterable[str | None]) -> set[str]:
    return {gruppe for gruppe, muster in KRITISCHE_AKTIONEN.items() for t in texte if t and muster.search(t)}


class _Katalog:
    """Elemente eines Backends (dedupliziert für die Ähnlichkeitssuche)."""

    def __init__(self, baeume: Iterable[FensterBaum]) -> None:
        self.baeume = list(baeume)
        self.alle: list[tuple[str, ElementDaten]] = [(b.titel, e) for b in self.baeume for e in b.elemente]
        eindeutig: dict[tuple[Any, ...], tuple[str, ElementDaten]] = {}
        for titel, e in self.alle:
            if not (e.name or e.auto_id or e.class_name):
                continue
            schluessel = (e.name, e.auto_id, e.class_name, e.control_type)
            eindeutig.setdefault(schluessel, (titel, e))
        self.eindeutig = list(eindeutig.values())
        self._vorbereitet = [
            (titel, e, [fuzz_utils.default_process(_kuerze(t, 200)) for t in (e.name, e.auto_id, e.class_name) if t])
            for titel, e in self.eindeutig
        ]

    def vorschlaege(
        self,
        begriffe: Sequence[str],
        *,
        control_type: str | None = None,
        nur_typ: str | None = None,
        anzahl: int = VORSCHLAEGE_ANZAHL,
        bezeichner: str | None = None,
    ) -> list[Vorschlag]:
        """Die ähnlichsten Elemente (rapidfuzz WRatio über name/auto_id/class_name); bei gleichem control_type
        zählen für die Reihenfolge ``TYP_BONUS`` Punkte dazu (angezeigt wird die reine Ähnlichkeit).

        Kandidaten mit kritischer Aktion (``KRITISCHE_AKTIONEN``: Speichern, Drucken, Mail, …) erscheinen
        nur, wenn ``bezeichner``/``begriffe`` zur selben Gruppe gehören.
        """
        erlaubt = _kritische_gruppen([bezeichner, *begriffe])

        def zulaessig(e: ElementDaten) -> bool:
            return _kritische_gruppen([e.name, e.auto_id]) <= erlaubt

        anfragen = [fuzz_utils.default_process(b) for b in begriffe if b and b.strip()]
        ergebnis: list[Vorschlag] = []
        bewertet: list[tuple[float, Vorschlag]] = []
        if not anfragen:
            if control_type:  # Spec nur mit control_type: Elemente ähnlichen Typs vorschlagen
                for titel, e in self.eindeutig:
                    if e.control_type and zulaessig(e):
                        punkte = float(fuzz.WRatio(control_type, e.control_type, processor=fuzz_utils.default_process))
                        if punkte >= VORSCHLAG_MINDESTPUNKTE:
                            ergebnis.append(Vorschlag(round(punkte, 1), titel, e))
            ergebnis.sort(key=lambda v: (-v.punkte, v.element.pfad))
            return ergebnis[:anzahl]
        for titel, e, texte in self._vorbereitet:
            if nur_typ is not None and not _gleich(nur_typ, e.typ):
                continue
            if not zulaessig(e):
                continue
            punkte = 0.0
            for anfrage in anfragen:
                for text in texte:
                    if text:
                        punkte = max(punkte, float(fuzz.WRatio(anfrage, text)))
            if punkte < VORSCHLAG_MINDESTPUNKTE:
                continue
            rang = punkte + (TYP_BONUS if control_type and _gleich(control_type, e.control_type) else 0.0)
            bewertet.append((rang, Vorschlag(round(punkte, 1), titel, e)))
        bewertet.sort(key=lambda r: (-r[0], r[1].fenster, r[1].element.pfad))
        return [v for _rang, v in bewertet[:anzahl]]


_MEHRFACH_ERLAUBT = {"DIALOG_TEXT"}  # Backend liest alle passenden Controls (z. B. alle Texte eines Dialogs)


def _ohne_control_type_treffer(spec: ControlSpec, katalog: _Katalog) -> ElementDaten | None:
    """Erstes sichtbare Element, das ohne das control_type-Kriterium passen würde (Hinweis für win32)."""
    if spec.control_type is None:
        return None
    rest = dataclasses.replace(spec, control_type=None)
    if not _hat_kriterium(rest):
        return None
    for baum in katalog.baeume:
        for e in baum.elemente:
            if e.ebene > 0 and _sichtbar(e) and _spec_passt(rest, e):
                return e
    return None


def _pruefe_control(name: str, spec: ControlSpec, katalog: _Katalog) -> AbgleichEintrag:
    eintrag = AbgleichEintrag(name=name, art="control", soll=spec.beschreibung(), status=Status.NICHT_GEFUNDEN)
    if not _hat_kriterium(spec):
        eintrag.status = Status.NICHT_PRUEFBAR
        eintrag.bemerkung = "keine Suchkriterien"
        return eintrag
    je_fenster: dict[int, list[Treffer]] = {}
    unsichtbar = 0
    for index, baum in enumerate(katalog.baeume):
        for e in baum.elemente:
            if e.ebene == 0 or not _spec_passt(spec, e):
                continue  # Ebene 0 ist das Fenster selbst; child_window sucht darunter
            if not _sichtbar(e):
                unsichtbar += 1
                continue
            je_fenster.setdefault(index, []).append(Treffer(baum.titel, e))
    eintrag.treffer = [t for liste in je_fenster.values() for t in liste]
    if not eintrag.treffer:
        ohne_typ = _ohne_control_type_treffer(spec, katalog)
        if ohne_typ is not None:
            eintrag.bemerkung = (
                f"passt nur ohne control_type={spec.control_type!r} (Element meldet "
                f"control_type={ohne_typ.control_type!r}; win32 kennt control_type meist nicht) – "
                "control_type in der YAML weglassen oder anderes Backend"
            )
        elif unsichtbar:
            eintrag.bemerkung = f"nur unsichtbar vorhanden ({unsichtbar}×)"
        else:
            eintrag.bemerkung = "Fenster mit diesem Control war beim Erkunden evtl. nicht geöffnet (--alle-fenster)"
        eintrag.vorschlaege = katalog.vorschlaege(_suchbegriffe(spec), control_type=spec.control_type, bezeichner=name)
        return eintrag
    groesste = max(len(liste) for liste in je_fenster.values())
    if name in _MEHRFACH_ERLAUBT:
        eintrag.status = Status.GEFUNDEN
        eintrag.bemerkung = f"{groesste} Treffer je Fenster; mehrere erlaubt (alle werden gelesen)"
        return eintrag
    if spec.found_index is not None:
        if groesste > spec.found_index:
            eintrag.status = Status.GEFUNDEN
            eintrag.bemerkung = f"found_index={spec.found_index} wählt Treffer {spec.found_index + 1} von {groesste}"
        else:
            eintrag.bemerkung = f"found_index={spec.found_index}, aber nur {groesste} Treffer je Fenster"
        return eintrag
    if groesste > 1:
        eintrag.status = Status.MEHRDEUTIG
        eintrag.bemerkung = f"{groesste} Treffer in einem Fenster – auto_id oder found_index ergänzen"
        return eintrag
    eintrag.status = Status.GEFUNDEN
    if len(je_fenster) > 1:
        eintrag.bemerkung = f"je einmal in {len(je_fenster)} Fenstern"
    return eintrag


def _namens_treffer(katalog: _Katalog, text: str, *, typ: str | None = None) -> list[Treffer]:
    return [
        Treffer(titel, e)
        for titel, e in katalog.alle
        if e.ebene > 0 and _gleich(e.name, text) and (typ is None or _gleich(typ, e.typ))
    ]


def _pruefe_fenstertitel(name: str, regex: str, katalog: _Katalog) -> AbgleichEintrag:
    eintrag = AbgleichEintrag(name=name, art="fenstertitel", soll=regex, status=Status.NICHT_GEFUNDEN)
    try:
        muster = re.compile(regex, re.I)
    except re.error as exc:
        eintrag.bemerkung = f"ungültiger Regex: {exc}"
        return eintrag
    for baum in katalog.baeume:
        if muster.search(baum.titel or ""):
            wurzel = baum.elemente[0] if baum.elemente else ElementDaten(pfad="0", ebene=0, name=baum.titel)
            eintrag.treffer.append(Treffer(baum.titel, wurzel))
    if len(eintrag.treffer) == 1:
        eintrag.status = Status.GEFUNDEN
    elif len(eintrag.treffer) > 1:
        eintrag.status = Status.MEHRDEUTIG
        eintrag.bemerkung = "passt auf mehrere Fenster"
    else:
        eintrag.bemerkung = "kein erfasstes Fenster passt (Dialog beim Erkunden geöffnet? --alle-fenster)"
    return eintrag


def _pruefe_spalte(name: str, text: str, katalog: _Katalog) -> AbgleichEintrag:
    eintrag = AbgleichEintrag(name=name, art="spalte", soll=text, status=Status.NICHT_GEFUNDEN)
    eintrag.treffer = _namens_treffer(katalog, text)
    if eintrag.treffer:
        eintrag.status = Status.GEFUNDEN
        kopf = [t for t in eintrag.treffer if _gleich(t.element.typ, "HeaderItem")]
        if kopf:
            eintrag.treffer = kopf
        else:
            typen = sorted({t.element.typ or "?" for t in eintrag.treffer})
            eintrag.bemerkung = f"nur als {', '.join(typen)} gefunden, nicht als Spaltenkopf (HeaderItem) – prüfen"
    else:
        eintrag.bemerkung = "Liste beim Erkunden evtl. nicht sichtbar"
        eintrag.vorschlaege = katalog.vorschlaege([text], control_type="HeaderItem", bezeichner=name)
    return eintrag


def _pruefe_menue(name: str, pfad: Sequence[str], katalog: _Katalog) -> AbgleichEintrag:
    eintrag = AbgleichEintrag(name=name, art="menue", soll=" -> ".join(pfad), status=Status.NICHT_GEFUNDEN)
    if not pfad:
        eintrag.status = Status.NICHT_PRUEFBAR
        return eintrag
    fehlend: list[str] = []
    for teil in pfad:
        treffer = _namens_treffer(katalog, teil, typ="MenuItem")
        if treffer:
            eintrag.treffer.append(treffer[0])
        else:
            fehlend.append(teil)
    if not fehlend:
        eintrag.status = Status.GEFUNDEN
    elif pfad[0] in fehlend:
        eintrag.bemerkung = f"Menüeintrag {pfad[0]!r} nicht gefunden"
        eintrag.vorschlaege = katalog.vorschlaege([pfad[0]], control_type="MenuItem", nur_typ="MenuItem", bezeichner=name)
    else:
        eintrag.status = Status.NICHT_PRUEFBAR
        eintrag.bemerkung = (
            f"erste Ebene vorhanden; {', '.join(repr(f) for f in fehlend)} erst nach Aufklappen sichtbar – "
            "von Hand prüfen (Menü öffnen ist hier verboten)"
        )
    return eintrag


def _pruefe_text(name: str, wert: str, katalog: _Katalog, *, art: str, regex: bool = False) -> AbgleichEintrag:
    eintrag = AbgleichEintrag(name=name, art=art, soll=wert, status=Status.NICHT_PRUEFBAR)
    if regex:
        try:
            muster = re.compile(wert, re.I)
        except re.error as exc:
            eintrag.bemerkung = f"ungültiger Regex: {exc}"
            return eintrag
        eintrag.treffer = [Treffer(t, e) for t, e in katalog.alle if e.ebene > 0 and e.name and muster.search(e.name)][:5]
    else:
        eintrag.treffer = _namens_treffer(katalog, wert)[:5]
    if eintrag.treffer:
        eintrag.status = Status.GEFUNDEN
    else:
        eintrag.bemerkung = "Wert erscheint erst in geöffneten Listen/Assistentenseiten"
    return eintrag


def gleiche_ab(
    erkundung: Erkundung,
    controls: LexwareControls,
    *,
    backend: str,
    controls_datei: Path | None = None,
    fenster_titel_regex: str | None = None,
) -> Abgleich:
    """Gleicht jeden Bezeichner aus ``controls`` mit dem Baum eines Backends ab.

    ``fenster_titel_regex`` (settings.lexware.fenster_titel_regex) ersetzt wie im ``PywinautoBackend``
    den Standard von ``HAUPTFENSTER_TITEL_REGEX``.
    """
    katalog = _Katalog(erkundung.fenster_von(backend))
    todos = todo_eintraege(controls_datei)
    standard = LexwareControls()
    eintraege: list[AbgleichEintrag] = []
    for f in fields(LexwareControls):
        name = f.name
        wert = getattr(controls, name)
        if isinstance(wert, ControlSpec):
            eintrag = _pruefe_control(name, wert, katalog)
        elif isinstance(wert, tuple):
            eintrag = _pruefe_menue(name, wert, katalog)
        elif name == "PROZESSNAME":
            laeuft = erkundung.prozess_laeuft and _gleich(erkundung.prozessname, str(wert))
            eintrag = AbgleichEintrag(
                name=name, art="prozess", soll=str(wert), status=Status.GEFUNDEN if laeuft else Status.NICHT_PRUEFBAR,
                bemerkung="" if laeuft else f"Erkundung lief mit Prozessname {erkundung.prozessname!r}",
            )
        elif name.endswith("_TITEL_REGEX"):
            regex = str(wert)
            if name == "HAUPTFENSTER_TITEL_REGEX" and fenster_titel_regex and regex == standard.HAUPTFENSTER_TITEL_REGEX:
                regex = fenster_titel_regex
            eintrag = _pruefe_fenstertitel(name, regex, katalog)
        elif name.startswith("SPALTE_"):
            eintrag = _pruefe_spalte(name, str(wert), katalog)
        elif name.startswith("TASTE_"):
            eintrag = AbgleichEintrag(name=name, art="taste", soll=str(wert), status=Status.NICHT_PRUEFBAR,
                                      bemerkung="Tastenkürzel – nicht ohne Bedienung prüfbar")
        elif name.endswith("_REGEX"):
            eintrag = _pruefe_text(name, str(wert), katalog, art="regex", regex=True)
        else:
            eintrag = _pruefe_text(name, str(wert), katalog, art="text")
        eintrag.todo = todos.get(name)
        eintraege.append(eintrag)
    return Abgleich(backend=backend, eintraege=eintraege)


def _md(text: str | None) -> str:
    return _kuerze(text or "", 80).replace("|", "\\|").replace("`", "'")


def _ort(t: Treffer | Vorschlag) -> str:
    e = t.element
    teile = [_typ_anzeige(e), json.dumps(_kuerze(e.name, 60), ensure_ascii=False) if e.name else "(ohne Namen)"]
    if e.auto_id:
        teile.append(f"auto_id={e.auto_id!r}")
    if e.class_name:
        teile.append(f"class={e.class_name!r}")
    return f"{' '.join(teile)} – Fenster {_md(t.fenster)!r}, Pfad {e.pfad}"


def _abgleich_abschnitt(abgleich: Abgleich) -> list[str]:
    z: list[str] = [f"## Backend `{abgleich.backend}`", "", f"**Bilanz:** {abgleich.bilanz_text()}", ""]
    nicht = [e for e in abgleich.eintraege if e.status == Status.NICHT_GEFUNDEN]
    mehr = [e for e in abgleich.eintraege if e.status == Status.MEHRDEUTIG]
    gef = [e for e in abgleich.eintraege if e.status == Status.GEFUNDEN]
    offen = [e for e in abgleich.eintraege if e.status == Status.NICHT_PRUEFBAR]

    z += [f"### Nicht gefunden ({len(nicht)})", ""]
    if not nicht:
        z += ["Keine.", ""]
    for e in nicht:
        z.append(f"#### `{e.name}`" + (" (TODO-LEXWARE)" if e.todo else ""))
        z.append(f"- Soll: `{_md(e.soll)}`")
        if e.bemerkung:
            z.append(f"- Hinweis: {e.bemerkung}")
        if e.vorschlaege:
            z.append("- Ähnlichste Kandidaten:")
            for i, v in enumerate(e.vorschlaege, 1):
                zeile = f"  {i}. {v.punkte:.0f} Punkte: {_ort(v)}"
                if e.art == "control":
                    zeile += f"  \n     Vorschlag: `{e.name}: {v.yaml_wert()}`"
                z.append(zeile)
        else:
            z.append("- Keine ähnlichen Kandidaten im Baum.")
        z.append("")

    z += [f"### Mehrdeutig ({len(mehr)})", ""]
    if not mehr:
        z += ["Keine.", ""]
    for e in mehr:
        z.append(f"#### `{e.name}`" + (" (TODO-LEXWARE)" if e.todo else ""))
        z.append(f"- Soll: `{_md(e.soll)}`")
        if e.bemerkung:
            z.append(f"- Hinweis: {e.bemerkung}")
        for t in e.treffer[:5]:
            z.append(f"  - {_ort(t)}")
        if len(e.treffer) > 5:
            z.append(f"  - … und {len(e.treffer) - 5} weitere")
        z.append("")

    z += [f"### Gefunden ({len(gef)})", "", "| Bezeichner | Soll | Fundstelle | Hinweis |", "|---|---|---|---|"]
    for e in gef:
        ort = _ort(e.treffer[0]) if e.treffer else ""
        z.append(f"| `{e.name}` | `{_md(e.soll)}` | {_md(ort)} | {_md(e.bemerkung)} |")
    z.append("")

    z += [f"### Nicht prüfbar ({len(offen)})", "", "| Bezeichner | Wert | Grund |", "|---|---|---|"]
    for e in offen:
        z.append(f"| `{e.name}` | `{_md(e.soll)}` | {_md(e.bemerkung)} |")
    z.append("")

    todo = [e for e in abgleich.eintraege if e.todo]
    z += [f"### TODO-LEXWARE-Einträge ({len(todo)})", "", "| Bezeichner | Status | Kommentar in der YAML |", "|---|---|---|"]
    for e in todo:
        z.append(f"| `{e.name}` | {e.status.value} | {_md(e.todo)} |")
    z.append("")
    return z


def schreibe_abgleich_md(
    erkundung: Erkundung,
    abgleiche: Sequence[Abgleich],
    ziel: Path,
    *,
    controls_datei: Path | None = None,
    quelle: Path | None = None,
) -> Path:
    ziel = Path(ziel)
    ziel.parent.mkdir(parents=True, exist_ok=True)
    z: list[str] = ["# Abgleich Lexware-Controls", ""]
    z.append(f"> **HINWEIS:** {erkundung.hinweis}")
    z.append("")
    z += ["```", *_kopfzeilen(erkundung)[:-1], "```", ""]
    z.append(f"- Control-Datei: `{controls_datei}`" if controls_datei else "- Control-Datei: Standardwerte (LexwareControls)")
    if quelle is not None:
        z.append(f"- Baum aus Datei: `{quelle}`")
    z.append(f"- Erstellt: {datetime.now().isoformat(timespec='seconds')}")
    z.append("")
    z += [
        "Vorgehen: Fehlende/mehrdeutige Einträge in `config/lexware_controls.yaml` korrigieren (Vorschläge vorher "
        "im Baum `baum.txt` prüfen), dann den Abgleich offline wiederholen: "
        "`auftragsautomat lexware-erkunden --aus-datei <baum.json>`. Controls aus Dialogen/Assistent werden nur "
        "gefunden, wenn das Fenster beim Erkunden offen war (`--alle-fenster`; Assistent nur öffnen, nie speichern).",
        "",
    ]
    for abgleich in abgleiche:
        z += _abgleich_abschnitt(abgleich)
    ziel.write_text("\n".join(z), encoding="utf-8")
    return ziel
