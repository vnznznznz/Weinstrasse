"""Postfach-Assistent für j.duenschede@weinstrasseadolph.de.

Deterministischer Kern der Anwendung: Regeln aus ``postfach/regeln.json`` auf Mails anwenden,
Antworten auf eigene Mails erkennen, Laufzustand führen, wiederkehrende Termine berechnen.
Die Verbindung zum Postfach (Microsoft-365-Connector) und das Formulieren von Entwürfen und
Briefings übernimmt der Agent anhand der Skills in ``.claude/skills/postfach-*``.
"""

from __future__ import annotations

__version__ = "0.1.0"
