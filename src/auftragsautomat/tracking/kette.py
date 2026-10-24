"""Stufenkette: mehrere Tracking-Quellen in konfigurierter Reihenfolge befragen.

``settings.tracking.quellen`` (Standard ``["mail", "api", "public_tracking"]``, ``"portal"`` nur
als Rückfall) bestimmt die Reihenfolge. Die **erste Quelle mit einem Ergebnis besser als
"unbekannt" gewinnt**; die Hinweise aller befragten Quellen werden gesammelt. Eine Quelle, die
einen **älteren Stand** meldet als die Sendung schon hat (z. B. nur die Bestätigungsmail, obwohl
die API gestern "abgeholt" lieferte), gewinnt nicht: ihre Meilensteine werden übernommen, der
weiter fortgeschrittene Status bleibt, und die nächste Quelle wird befragt. "verspaetet" und
"problem" sind Ampelzustände und gelten immer als neues Ergebnis.

Vertrag mit den Quellen: Eine Quelle setzt bei Erfolg ``Sendung.quelle`` auf ihren ``name``;
im Fehlerfall gibt sie die Sendung unverändert (plus Hinweis) zurück. Daran erkennt die Kette,
ob eine Quelle etwas gefunden hat.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping

from auftragsautomat.config import Settings
from auftragsautomat.models import MailEreignis, Sendung, SendungStatus
from auftragsautomat.tracking.basis import (
    HillebrandKonfig,
    TrackingQuelle,
    ist_bekannt,
    ist_fortschritt,
    jetzt,
    lade_hillebrand_konfig,
    vereinige_meilensteine,
)

logger = logging.getLogger(__name__)

__all__ = ["TrackingKette", "erzeuge_kette"]

_AMPEL = (SendungStatus.VERSPAETET, SendungStatus.PROBLEM)


class TrackingKette:
    """Reihenfolge der Quellen laut Konfiguration; erste Quelle mit bekanntem Status gewinnt."""

    name = "kette"

    def __init__(self, quellen: Iterable[TrackingQuelle]) -> None:
        self._quellen = list(quellen)

    @property
    def quellen(self) -> list[TrackingQuelle]:
        return list(self._quellen)

    @property
    def namen(self) -> list[str]:
        return [q.name for q in self._quellen]

    def verfuegbar(self) -> bool:
        return any(_verfuegbar(q) for q in self._quellen)

    def aktualisiere(self, sendung: Sendung) -> Sendung:
        """Befragt die Quellen der Reihe nach; liefert immer eine Kopie der Sendung.

        Jede Quelle bekommt eine Kopie mit ``quelle="keine"``. Ein Ergebnis zählt nur, wenn die
        Quelle ``quelle`` auf ihren eigenen Namen gesetzt hat **und** der Status bekannt ist; eine
        unverändert zurückgegebene Sendung (Fehlerfall mit Hinweis) gilt nicht als Ergebnis, auch
        wenn sie schon einen Status aus einem früheren Lauf trägt.
        """
        hinweise = list(sendung.hinweise)
        rueckschritt: Sendung | None = None
        eingabe = sendung.model_copy(update={"quelle": "keine"})

        def merke(neue: Iterable[str]) -> None:
            for h in neue:
                if h not in hinweise:
                    hinweise.append(h)

        for quelle in self._quellen:
            if not _verfuegbar(quelle):
                merke([f"Quelle '{quelle.name}' nicht verfügbar"])
                continue
            try:
                ergebnis = quelle.aktualisiere(eingabe)
            except Exception as exc:  # Quellen dürfen nicht werfen; zur Sicherheit trotzdem fangen
                logger.exception("Quelle '%s' hat eine Exception geworfen", quelle.name)
                merke([f"Quelle '{quelle.name}': {type(exc).__name__}: {exc}"])
                continue
            merke(ergebnis.hinweise)
            if ergebnis.quelle != quelle.name or not ist_bekannt(ergebnis.status):
                continue
            if (
                ist_fortschritt(sendung.status, ergebnis.status)
                and sendung.status not in _AMPEL
                and ergebnis.status not in _AMPEL
            ):
                merke(
                    [
                        f"Quelle '{quelle.name}' meldet '{ergebnis.status.value}', "
                        f"bisheriger Stand '{sendung.status.value}' bleibt"
                    ]
                )
                if rueckschritt is None:
                    rueckschritt = ergebnis
                continue
            logger.info("Tracking %s: Quelle '%s' liefert %s", sendung.referenz, quelle.name, ergebnis.status.value)
            return ergebnis.model_copy(update={"hinweise": hinweise})

        if rueckschritt is not None:
            return sendung.model_copy(
                update={
                    "meilensteine": vereinige_meilensteine(sendung.meilensteine, rueckschritt.meilensteine),
                    "hinweise": hinweise,
                    "letzte_pruefung": jetzt(),
                }
            )
        return sendung.model_copy(update={"hinweise": hinweise, "letzte_pruefung": jetzt()})


def _verfuegbar(quelle: TrackingQuelle) -> bool:
    try:
        return bool(quelle.verfuegbar())
    except Exception as exc:
        logger.warning("Quelle '%s': verfuegbar() fehlgeschlagen: %s", getattr(quelle, "name", "?"), exc)
        return False


def erzeuge_kette(
    settings: Settings,
    mail_ereignisse: Iterable[MailEreignis] | None = None,
    *,
    konfig: HillebrandKonfig | None = None,
    sende: Callable | None = None,
    umgebung: Mapping[str, str] | None = None,
    lade_seite_public: Callable[[str], str] | None = None,
    lade_seite_portal: Callable | None = None,
) -> TrackingKette:
    """Baut die Kette laut ``settings.tracking.quellen`` (unbekannte Namen werden übersprungen).

    ``mail_ereignisse``: bereits erkannte Ereignisse aus dem Postfach (``hillebrand_mail.erkenne_ereignisse``).
    Die Keyword-Argumente dienen Tests und dem Dry Run (Fake-Transport, Fake-Lader).
    """
    from auftragsautomat.tracking.hillebrand_api import ApiQuelle, HillebrandApi
    from auftragsautomat.tracking.hillebrand_mail import MailQuelle
    from auftragsautomat.tracking.hillebrand_portal import PortalQuelle
    from auftragsautomat.tracking.hillebrand_public import PublicTrackingQuelle

    konfig = konfig or lade_hillebrand_konfig(settings.tracking.hillebrand_konfig)
    quellen: list[TrackingQuelle] = []
    for name in settings.tracking.quellen:
        if name == "mail":
            quellen.append(MailQuelle(mail_ereignisse or [], konfig))
        elif name == "api":
            api = HillebrandApi(konfig, settings.tracking, sende=sende, umgebung=umgebung)
            quellen.append(ApiQuelle(api, konfig))
        elif name == "public_tracking":
            quellen.append(PublicTrackingQuelle(konfig, lade_seite=lade_seite_public, tracking=settings.tracking))
        elif name == "portal":
            quellen.append(PortalQuelle(konfig, settings, lade_seite=lade_seite_portal))
        else:
            logger.warning("Unbekannte Tracking-Quelle '%s' in settings.tracking.quellen übersprungen", name)
    return TrackingKette(quellen)
