"""Zuordnung eingehender Mail-Ereignisse zu Fixterminen (docs/AUSBAUSTUFE_2.md, 2.6 Schritt 2).

Reihenfolge der Zuordnung (:func:`ordne_zu`):

1. **Referenz der Spedition** (``MailEreignis.referenz``, z. B. "Our Ref: DEMB00000"): gleich der
   ``tracking_referenz`` eines Fixtermins oder als Wort in seiner Bemerkung; sonst über ein Dossier
   mit dieser ``tracking_referenz``.
2. **Kundenreferenz / Bestellnummer** (``MailEreignis.kundenreferenz``, "Your Ref" bzw. unsere
   Order No.): gleich Bestellnummer, AB-Nummer oder Kundenbestellnummer eines Fixtermins (Vergleich
   ohne Leerzeichen und Trennzeichen), als Wort in der Bemerkung, oder über ein Dossier.
3. **Kurznamen im Text**: Lieferant- und Kunden-Kurzname (ohne Rechtsformen und Gattungswörter)
   als ganze Wörter in Betreff/Textauszug; mehrere Kandidaten -> der nächste Termin nach dem
   Empfangsdatum. Reine Textzuordnungen gelten nie als eindeutig (nur Vorschlag).

Ein Treffer ist **eindeutig** (``Zuordnung.eindeutig``), wenn er über Referenz oder Nummer kam, genau
ein Fixtermin passt und die Konfidenz des Ereignisses die Schwelle erreicht; nur dann setzt der
Überwachungslauf Häkchen automatisch. Mehrdeutige Treffer liefern ``fixtermin=None`` mit den
Kandidaten und einem Hinweis (``grund``).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from auftragsautomat.benennung import normalisiere
from auftragsautomat.models import Dossier, Fixtermin, MailEreignis
from auftragsautomat.termine.kurznamen import GENERISCHE_WOERTER, RECHTSFORMEN

logger = logging.getLogger(__name__)

__all__ = [
    "EINDEUTIGE_QUELLEN",
    "KONFIDENZ_TEXT",
    "MIN_WORTLAENGE",
    "Quelle",
    "Zuordnung",
    "dossier_fuer",
    "dossier_fuer_fixtermin",
    "kurzname_woerter",
    "norm_nummer",
    "ordne_ereignis_zu",
    "ordne_zu",
]

Quelle = Literal["referenz", "bestellnummer", "ab_nummer", "kundenbestellnummer", "bemerkung", "dossier", "text"]
#: Quellen, die eine automatische Übernahme (Häkchen) rechtfertigen.
EINDEUTIGE_QUELLEN: frozenset[str] = frozenset({"referenz", "bestellnummer", "ab_nummer", "kundenbestellnummer", "bemerkung", "dossier"})
#: Konfidenz der Textzuordnung je nach Treffer (Lieferant und Kunde / nur Kunde / nur Lieferant).
KONFIDENZ_TEXT: dict[str, float] = {"beide": 0.8, "kunde": 0.6, "lieferant": 0.5}
#: Kürzere Namensbestandteile werden für die Textsuche nicht verwendet.
MIN_WORTLAENGE = 3

_TRENNER_RE = re.compile(r"[\s\-_/.]+")
_WORT_RE = re.compile(r"[a-z0-9]+")


@dataclass
class Zuordnung:
    """Ergebnis von :func:`ordne_zu`."""

    fixtermin: Fixtermin | None
    quelle: Quelle | None = None
    konfidenz: float = 0.0
    grund: str = ""
    kandidaten: list[Fixtermin] = field(default_factory=list)
    dossier: Dossier | None = None
    eindeutig: bool = False

    @property
    def mehrdeutig(self) -> bool:
        return self.fixtermin is None and len(self.kandidaten) > 1


# ---------------------------------------------------------------------------
# Normalisierung
# ---------------------------------------------------------------------------


def norm_nummer(text: str | None) -> str | None:
    """Vergleichsform einer Nummer/Referenz: Leerzeichen und Trennzeichen entfernt, Großschreibung
    ("WIK-2026 0100" -> "WIK20260100"); leer -> ``None``."""
    if text is None:
        return None
    t = _TRENNER_RE.sub("", str(text)).strip().upper()
    return t or None


def _woerter(text: str | None) -> list[str]:
    return _WORT_RE.findall(normalisiere(text))


def _nummern_in(text: str | None) -> set[str]:
    """Alle Wort-Token eines Textes in Nummern-Vergleichsform (für Bemerkungen)."""
    if not text:
        return set()
    token = re.split(r"[\s,;:|()\[\]]+", str(text))
    return {n for n in (norm_nummer(t) for t in token) if n}


def kurzname_woerter(name: str | None) -> list[str]:
    """Signifikante Wörter eines Kurznamens (ohne Rechtsformen, Gattungswörter, kurze Token)."""
    ergebnis: list[str] = []
    for wort in _woerter(name):
        if len(wort) < MIN_WORTLAENGE or wort in RECHTSFORMEN or wort in GENERISCHE_WOERTER:
            continue
        if wort not in ergebnis:
            ergebnis.append(wort)
    return ergebnis


def _beschreibung(fixtermin: Fixtermin) -> str:
    return f"{fixtermin.lieferant_kurz} / {fixtermin.kunde_kurz} am {fixtermin.liefertag:%d.%m.%Y}"


# ---------------------------------------------------------------------------
# Dossiers
# ---------------------------------------------------------------------------


def _dossier_nummern(dossier: Dossier) -> set[str]:
    return {n for n in (norm_nummer(dossier.bestellnummer), norm_nummer(dossier.ab_nummer), norm_nummer(dossier.kundenbestellnummer)) if n}


def _fixtermin_nummern(fixtermin: Fixtermin) -> dict[str, str]:
    """Nummern des Fixtermins in Vergleichsform -> Quelle."""
    nummern: dict[str, str] = {}
    for feld in ("bestellnummer", "ab_nummer", "kundenbestellnummer"):
        n = norm_nummer(getattr(fixtermin, feld))
        if n and n not in nummern:
            nummern[n] = feld
    return nummern


def dossier_fuer_fixtermin(fixtermin: Fixtermin, dossiers: Iterable[Dossier]) -> Dossier | None:
    """Dossier zum Fixtermin über Referenz oder Nummern, sonst über Kurznamen + Liefertag."""
    liste = list(dossiers)
    ref = norm_nummer(fixtermin.tracking_referenz)
    nummern = set(_fixtermin_nummern(fixtermin))
    for d in liste:
        if ref and norm_nummer(d.tracking_referenz) == ref:
            return d
    for d in liste:
        if nummern and (_dossier_nummern(d) & nummern):
            return d
    lieferant, kunde = normalisiere(fixtermin.lieferant_kurz), normalisiere(fixtermin.kunde_kurz)
    for d in liste:
        if d.fixtermin == fixtermin.liefertag and normalisiere(d.lieferant_kurz) == lieferant and normalisiere(d.kunde_kurz) == kunde:
            return d
    return None


def _fixtermine_zu_dossier(dossier: Dossier, fixtermine: Sequence[Fixtermin]) -> list[Fixtermin]:
    """Fixtermine, die zu einem Dossier gehören (Nummern, Referenz oder Kurznamen + Liefertag)."""
    nummern = _dossier_nummern(dossier)
    ref = norm_nummer(dossier.tracking_referenz)
    treffer: list[Fixtermin] = []
    for ft in fixtermine:
        if ref and norm_nummer(ft.tracking_referenz) == ref:
            treffer.append(ft)
        elif nummern and (set(_fixtermin_nummern(ft)) & nummern):
            treffer.append(ft)
        elif (
            dossier.fixtermin is not None
            and ft.liefertag == dossier.fixtermin
            and normalisiere(ft.lieferant_kurz) == normalisiere(dossier.lieferant_kurz)
            and normalisiere(ft.kunde_kurz) == normalisiere(dossier.kunde_kurz)
        ):
            treffer.append(ft)
    return treffer


def dossier_fuer(ereignis: MailEreignis, fixtermin: Fixtermin | None, dossiers: Iterable[Dossier]) -> Dossier | None:
    """Dossier zu einem Ereignis: Referenz/Kundenreferenz des Ereignisses, sonst über den Fixtermin."""
    liste = list(dossiers)
    ref = norm_nummer(ereignis.referenz)
    kundenref = norm_nummer(ereignis.kundenreferenz)
    for d in liste:
        if ref and norm_nummer(d.tracking_referenz) == ref:
            return d
    for d in liste:
        if kundenref and kundenref in _dossier_nummern(d):
            return d
    if fixtermin is not None:
        return dossier_fuer_fixtermin(fixtermin, liste)
    return None


# ---------------------------------------------------------------------------
# Zuordnung
# ---------------------------------------------------------------------------


def _naechster(kandidaten: list[Fixtermin], bezug: date) -> tuple[Fixtermin | None, list[Fixtermin]]:
    """Der nächste Termin ab ``bezug`` (sonst der nächstliegende); mehrere am selben Tag -> mehrdeutig."""
    if not kandidaten:
        return None, []
    zukunft = [ft for ft in kandidaten if ft.liefertag >= bezug]
    pool = zukunft or kandidaten
    bester_tag = min(pool, key=lambda ft: abs((ft.liefertag - bezug).days)).liefertag
    gleich = [ft for ft in pool if ft.liefertag == bester_tag]
    if len(gleich) == 1:
        return gleich[0], gleich
    return None, gleich


def _eindeutig_aus(kandidaten: list[Fixtermin], bezug: date) -> tuple[Fixtermin | None, list[Fixtermin]]:
    """Genau ein Kandidat -> Treffer; mehrere -> bevorzugt der nächste Termin, sonst mehrdeutig."""
    if len(kandidaten) == 1:
        return kandidaten[0], kandidaten
    return _naechster(kandidaten, bezug)


def _ueber_referenz(ereignis: MailEreignis, fixtermine: Sequence[Fixtermin], bezug: date) -> Zuordnung | None:
    ref = norm_nummer(ereignis.referenz)
    if not ref:
        return None
    direkt = [ft for ft in fixtermine if norm_nummer(ft.tracking_referenz) == ref]
    if direkt:
        ft, kandidaten = _eindeutig_aus(direkt, bezug)
        grund = f"Referenz {ereignis.referenz} = Tracking-Referenz" if ft else f"Referenz {ereignis.referenz} passt zu mehreren Fixterminen"
        return Zuordnung(ft, "referenz", 1.0, grund, kandidaten)
    in_bemerkung = [ft for ft in fixtermine if ref in _nummern_in(ft.bemerkung)]
    if in_bemerkung:
        ft, kandidaten = _eindeutig_aus(in_bemerkung, bezug)
        grund = f"Referenz {ereignis.referenz} in der Bemerkung" if ft else f"Referenz {ereignis.referenz} steht in mehreren Bemerkungen"
        return Zuordnung(ft, "bemerkung", 0.95, grund, kandidaten)
    return None


def _ueber_nummer(ereignis: MailEreignis, fixtermine: Sequence[Fixtermin], bezug: date) -> Zuordnung | None:
    # TODO-PRUEFEN: An echten Hillebrand-Bestätigungen prüfen, welche unserer Nummern unter "Your Ref" steht
    # (Lexware-Bestellnummer aus dem Speditionsauftrag, AB-Nummer oder Kundenbestellnummer); hier werden alle
    # drei verglichen. Enthält "Your Ref" zusätzlich Text (z. B. Kundenname), ist das Muster in
    # config/tracking_hillebrand.yaml (kundenreferenz_regex) anzupassen.
    nummer = norm_nummer(ereignis.kundenreferenz)
    if not nummer:
        return None
    treffer: list[tuple[Fixtermin, str]] = []
    for ft in fixtermine:
        feld = _fixtermin_nummern(ft).get(nummer)
        if feld:
            treffer.append((ft, feld))
    if treffer:
        ft, kandidaten = _eindeutig_aus([t for t, _ in treffer], bezug)
        quelle: Quelle = next((f for t, f in treffer if ft is not None and t is ft), treffer[0][1])  # type: ignore[assignment]
        grund = f"Nummer {ereignis.kundenreferenz} = {quelle}" if ft else f"Nummer {ereignis.kundenreferenz} passt zu mehreren Fixterminen"
        return Zuordnung(ft, quelle, 1.0, grund, kandidaten)
    in_bemerkung = [ft for ft in fixtermine if nummer in _nummern_in(ft.bemerkung)]
    if in_bemerkung:
        ft, kandidaten = _eindeutig_aus(in_bemerkung, bezug)
        grund = f"Nummer {ereignis.kundenreferenz} in der Bemerkung" if ft else f"Nummer {ereignis.kundenreferenz} steht in mehreren Bemerkungen"
        return Zuordnung(ft, "bemerkung", 0.9, grund, kandidaten)
    return None


def _ueber_dossier(ereignis: MailEreignis, fixtermine: Sequence[Fixtermin], dossiers: Sequence[Dossier], bezug: date) -> Zuordnung | None:
    if not dossiers:
        return None
    dossier = dossier_fuer(ereignis, None, dossiers)
    if dossier is None:
        return None
    kandidaten = _fixtermine_zu_dossier(dossier, fixtermine)
    if not kandidaten:
        return Zuordnung(None, "dossier", 0.0, f"Dossier {dossier.name} gefunden, aber kein passender Fixtermin", [], dossier)
    ft, kandidaten = _eindeutig_aus(kandidaten, bezug)
    grund = f"über Dossier {dossier.name}" if ft else f"Dossier {dossier.name} passt zu mehreren Fixterminen"
    return Zuordnung(ft, "dossier", 0.95, grund, kandidaten, dossier)


def _ueber_text(ereignis: MailEreignis, fixtermine: Sequence[Fixtermin], bezug: date) -> Zuordnung | None:
    woerter = set(_woerter(f"{ereignis.betreff or ''} {ereignis.text_auszug or ''}"))
    if not woerter:
        return None
    bewertet: list[tuple[Fixtermin, str]] = []
    for ft in fixtermine:
        lw, kw_ = kurzname_woerter(ft.lieferant_kurz), kurzname_woerter(ft.kunde_kurz)
        lieferant_ok = bool(lw) and all(w in woerter for w in lw)
        kunde_ok = bool(kw_) and all(w in woerter for w in kw_)
        if lieferant_ok and kunde_ok:
            bewertet.append((ft, "beide"))
        elif kunde_ok:
            bewertet.append((ft, "kunde"))
        elif lieferant_ok:
            bewertet.append((ft, "lieferant"))
    if not bewertet:
        return None
    beste = max(KONFIDENZ_TEXT[art] for _, art in bewertet)
    art = next(a for a, k in KONFIDENZ_TEXT.items() if k == beste)
    kandidaten = [ft for ft, a in bewertet if a == art]
    ft, kandidaten = _naechster(kandidaten, bezug) if len(kandidaten) > 1 else (kandidaten[0], kandidaten)
    was = {"beide": "Lieferant und Kunde", "kunde": "nur der Kunde", "lieferant": "nur der Lieferant"}[art]
    grund = f"{was} im Text erkannt" if ft else f"{was} im Text erkannt, aber mehrere Fixtermine am selben Tag"
    return Zuordnung(ft, "text", beste, grund, kandidaten)


def ordne_zu(
    ereignis: MailEreignis,
    fixtermine: Iterable[Fixtermin],
    dossiers: Iterable[Dossier] = (),
    *,
    konfidenz_min: float = 0.5,
    text_suche: bool = True,
) -> Zuordnung:
    """Ordnet ein Mail-Ereignis einem Fixtermin zu (Referenz > Nummer > Dossier > Text).

    ``konfidenz_min`` ist die Schwelle für ``eindeutig`` (Konfidenz des Ereignisses multipliziert mit der
    Sicherheit der Zuordnung); darunter bleibt die Zuordnung ein Vorschlag. Bei Mehrdeutigkeit ist
    ``fixtermin`` ``None`` und ``kandidaten`` nennt die Möglichkeiten.
    """
    liste = list(fixtermine)
    dossier_liste = list(dossiers)
    bezug = ereignis.empfangen.date()
    ergebnis: Zuordnung | None = None
    nur_dossier: Zuordnung | None = None  # Dossier gefunden, aber kein Fixtermin dazu
    for schritt in (
        lambda: _ueber_referenz(ereignis, liste, bezug),
        lambda: _ueber_nummer(ereignis, liste, bezug),
        lambda: _ueber_dossier(ereignis, liste, dossier_liste, bezug),
        lambda: _ueber_text(ereignis, liste, bezug) if text_suche else None,
    ):
        ergebnis = schritt()
        if ergebnis is None:
            continue
        if ergebnis.fixtermin is not None or ergebnis.kandidaten:
            break
        if ergebnis.dossier is not None and nur_dossier is None:
            nur_dossier = ergebnis
        ergebnis = None
    if ergebnis is None:
        ergebnis = nur_dossier or Zuordnung(None, None, 0.0, "kein Fixtermin passt (weder Referenz, Nummer, Dossier noch Kurznamen)")
    gesamt = round(ergebnis.konfidenz * float(ereignis.konfidenz), 3)
    ergebnis.konfidenz = gesamt
    ergebnis.eindeutig = ergebnis.fixtermin is not None and ergebnis.quelle in EINDEUTIGE_QUELLEN and gesamt >= konfidenz_min
    if ergebnis.fixtermin is not None and ergebnis.dossier is None and dossier_liste:
        ergebnis.dossier = dossier_fuer(ereignis, ergebnis.fixtermin, dossier_liste)
    if ergebnis.fixtermin is not None:
        logger.info(
            "Ereignis %s (%s) -> %s [%s, Konfidenz %.2f%s]",
            ereignis.typ.value, ereignis.betreff, _beschreibung(ergebnis.fixtermin), ergebnis.quelle, gesamt,
            ", eindeutig" if ergebnis.eindeutig else "",
        )
    elif ergebnis.kandidaten:
        logger.warning("Ereignis %s (%s) mehrdeutig: %s", ereignis.typ.value, ereignis.betreff, "; ".join(_beschreibung(k) for k in ergebnis.kandidaten))
        ergebnis.grund = f"{ergebnis.grund}: " + "; ".join(_beschreibung(k) for k in ergebnis.kandidaten)
    else:
        logger.info("Ereignis %s (%s) ohne passenden Fixtermin", ereignis.typ.value, ereignis.betreff)
    return ergebnis


def ordne_ereignis_zu(ereignis: MailEreignis, fixtermine: Iterable[Fixtermin], dossiers: Iterable[Dossier] = ()) -> Fixtermin | None:
    """Kurzform von :func:`ordne_zu`: der zugeordnete Fixtermin oder ``None`` (mehrdeutig/kein Treffer)."""
    return ordne_zu(ereignis, fixtermine, dossiers).fixtermin
