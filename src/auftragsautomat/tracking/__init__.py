"""Sendungsverfolgung Hillebrand (Stufenmodell, docs/AUSBAUSTUFE_2.md 2.5).

Stufen: ``hillebrand_mail`` (primär) -> ``hillebrand_api`` (empfohlen) -> ``hillebrand_public``
(ohne Login) -> ``hillebrand_portal`` (nur Rückfall). Alle Quellen erfüllen
``basis.TrackingQuelle`` und liefern ``models.Sendung``; ``kette.TrackingKette`` befragt sie in
der Reihenfolge aus ``settings.tracking.quellen``.
"""

from auftragsautomat.tracking.basis import (
    HillebrandKonfig,
    KonfigFehler,
    TrackingQuelle,
    lade_hillebrand_konfig,
    status_aus_meilensteinen,
)
from auftragsautomat.tracking.hillebrand_api import ApiQuelle, HillebrandApi
from auftragsautomat.tracking.hillebrand_mail import MailQuelle, erkenne_ereignis, erkenne_ereignisse
from auftragsautomat.tracking.hillebrand_portal import PortalQuelle, profilverzeichnis
from auftragsautomat.tracking.hillebrand_public import PublicTrackingQuelle, parse_public_tracking_html
from auftragsautomat.tracking.kette import TrackingKette, erzeuge_kette

__all__ = [
    "ApiQuelle",
    "HillebrandApi",
    "HillebrandKonfig",
    "KonfigFehler",
    "MailQuelle",
    "PortalQuelle",
    "PublicTrackingQuelle",
    "TrackingKette",
    "TrackingQuelle",
    "erkenne_ereignis",
    "erkenne_ereignisse",
    "erzeuge_kette",
    "lade_hillebrand_konfig",
    "parse_public_tracking_html",
    "profilverzeichnis",
    "status_aus_meilensteinen",
]
