"""Erkennen, ob eine eingehende Mail die Antwort auf eine von JD gesendete Mail ist.

Der Connector liefert keine ``In-Reply-To``-Kopfzeilen. Wir vergleichen deshalb den
normalisierten Betreff (ohne „AW:/Re:/WG:“-Präfixe) mit den gesendeten Mails der letzten
Wochen und prüfen, dass der Absender der eingehenden Mail Empfänger der gesendeten war.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import datetime

from postfach.modelle import Mail

_PRAEFIX = re.compile(
    r"^\s*((aw|re|antw|antwort|r|rif|ris|sv|vs|wg|fw|fwd|tr|i|rv|ref|r\.)\s*(\[\d+\])?\s*:\s*)+",
    re.IGNORECASE,
)
_ANTWORT_PRAEFIX = re.compile(r"^\s*(aw|re|antw|antwort|r|rif|ris|sv|vs|ref)\s*(\[\d+\])?\s*:", re.IGNORECASE)


def normalisiere_betreff(betreff: str) -> str:
    text = _PRAEFIX.sub("", betreff or "")
    text = re.sub(r"\s+", " ", text).strip().lower()
    return text


def ist_antwort_betreff(betreff: str) -> bool:
    return bool(_ANTWORT_PRAEFIX.match(betreff or ""))


def _adressen(mail: Mail) -> set[str]:
    return {a.lower() for a in [*mail.empfaenger, *mail.cc] if a}


def markiere_antworten(eingang: Iterable[Mail], gesendet: Iterable[Mail]) -> list[Mail]:
    """Setzt ``antwort_auf_eigene`` an den eingehenden Mails und gibt sie zurück."""
    gesendet_liste = list(gesendet)
    index: dict[str, list[Mail]] = {}
    for g in gesendet_liste:
        index.setdefault(normalisiere_betreff(g.betreff), []).append(g)

    ergebnis: list[Mail] = []
    for m in eingang:
        m.antwort_auf_eigene = False
        schluessel = normalisiere_betreff(m.betreff)
        if not schluessel:
            ergebnis.append(m)
            continue
        for g in index.get(schluessel, []):
            empfaenger = _adressen(g)
            absender_passt = m.absender.lower() in empfaenger or any(
                m.domain and e.endswith("@" + m.domain) for e in empfaenger
            )
            zeit_passt = _vorher(g.empfangen, m.empfangen)
            if absender_passt and zeit_passt:
                m.antwort_auf_eigene = True
                break
        if not m.antwort_auf_eigene and schluessel not in index and ist_antwort_betreff(m.betreff):
            # Betreff ist eine Antwort, aber wir finden die gesendete Mail nicht (älter als Suchfenster?)
            m.antwort_auf_eigene = None
        ergebnis.append(m)
    return ergebnis


def _vorher(a: datetime | None, b: datetime | None) -> bool:
    if a is None or b is None:
        return True
    return a <= b
