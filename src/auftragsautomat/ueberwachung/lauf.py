"""Täglicher Überwachungslauf (docs/AUSBAUSTUFE_2.md, Abschnitt 2.6).

:class:`Ueberwachungslauf` führt die Schritte des Laufs aus und liefert einen
:class:`~auftragsautomat.models.Ueberwachungsbericht`:

1. Fixtermine der letzten 7 Tage bis ``ueberwachung.tage_voraus`` aus der Fixterminüberwachung lesen.
2. Posteingang der letzten ``mails_rueckblick_tage`` Tage lesen und Ereignisse erkennen
   (:func:`auftragsautomat.kommunikation.eingang.sammle_ereignisse`).
3. Jedes Ereignis einem Fixtermin zuordnen (:mod:`auftragsautomat.ueberwachung.zuordnung`):
   Bestätigungen der Spedition/des Lieferanten setzen das Häkchen (nur bei eindeutiger Zuordnung
   und ``bestaetigungen_automatisch_setzen``, sonst Vorschlag), die Referenz der Spedition wandert
   in die Bemerkung, Terminänderungen werden vorgeschlagen (Verschieben nur mit
   ``termine_automatisch_anpassen``), Kontaktänderungen aus Signaturen vorgeschlagen, die Mail als
   ``.eml`` ins Dossier gelegt (Bestätigung des Weinguts mit PDF-Anhang zusätzlich als "Oc").
4. Je Fixtermin die Sendung über die Tracking-Kette aktualisieren, mit
   :func:`auftragsautomat.ueberwachung.regeln.bewerte` beurteilen und die fälligen Maßnahmen als
   E-Mail-Entwürfe über die :class:`~auftragsautomat.kommunikation.entwuerfe.Entwurfsfabrik`
   erzeugen (Empfänger aus der Kontaktliste über den :class:`~auftragsautomat.kontakte.zuordnung.Zuordner`);
   die :class:`~auftragsautomat.ueberwachung.regeln.Drossel` verhindert Wiederholungen.
5. Entwürfe über das ``MailBackend`` speichern (nie senden; im Dry Run ``.eml`` über das Fake-Backend).
6. Erinnerung an den Anwender selbst, wenn es Punkte gibt; Bericht zurückgeben
   (:func:`auftragsautomat.ueberwachung.bericht.schreibe_bericht` schreibt ihn weg).

Jeder Teilschritt ist gegen Ausnahmen abgesichert: ein Fehler landet in ``bericht.fehler``, der Lauf
geht weiter. ``dry_run=True`` schreibt weder in die Excel noch in Dossiers oder die Merkdatei, legt
aber Entwürfe über das übergebene Backend an (für den Dry Run also ein Fake-Backend injizieren).
"""

from __future__ import annotations

import email.policy
import hashlib
import logging
import re
import tempfile
from collections.abc import Callable, Mapping
from datetime import date, datetime, time, timedelta
from email.message import EmailMessage
from email.utils import format_datetime, formataddr
from pathlib import Path
from typing import Any

from auftragsautomat.config import Settings
from auftragsautomat.dossier.erzeugung import DossierVerwaltung, mail_dateiname
from auftragsautomat.kommunikation.eingang import sammle_ereignisse
from auftragsautomat.kommunikation.entwuerfe import Entwurfsfabrik
from auftragsautomat.kontakte.aenderungen import aenderungen_aus_mail
from auftragsautomat.kontakte.zuordnung import Zuordner
from auftragsautomat.m365.basis import Mail, MailAnhang, MailBackend, html_zu_text
from auftragsautomat.models import (
    Dossier,
    EmailEntwurf,
    Ereignistyp,
    Fixtermin,
    KontaktAenderung,
    MailEreignis,
    Sendung,
    Terminaenderung,
    Ueberwachungsbericht,
    UeberwachungsPunkt,
)
from auftragsautomat.termine.register import BESTAETIGUNGSFELDER, FixterminRegister, RegisterFehler
from auftragsautomat.ueberwachung.regeln import (
    Bewertung,
    Drossel,
    Massnahme,
    Regelwerk,
    bewerte,
    tracking_abgeholt,
)
from auftragsautomat.ueberwachung.zuordnung import (
    Zuordnung,
    dossier_fuer,
    dossier_fuer_fixtermin,
    norm_nummer,
    ordne_zu,
)

logger = logging.getLogger(__name__)

__all__ = [
    "RUECKBLICK_TAGE",
    "TrackingKetteFabrik",
    "Ueberwachungslauf",
    "eml_aus_mail",
    "ueberwachen",
]

#: Fixtermine so viele Tage rückwirkend betrachten (überschrittene Termine, die noch nicht zugestellt sind).
RUECKBLICK_TAGE = 7
#: Beschriftung der Häkchen im Bericht.
_HAEKCHEN_TEXT: dict[str, str] = {"lieferant": "Lieferant bestätigt", "spedition": "Spedition bestätigt", "abgeholt": "Abgeholt"}
_MESSAGE_ID_RE = re.compile(r"^<[^<>@\s]+@[^<>@\s]+>$")
_UNSICHER_RE = re.compile(r"[^A-Za-z0-9._-]+")
_PDF_RE = re.compile(r"\.pdf$", re.IGNORECASE)

TrackingKetteFabrik = Callable[[list[MailEreignis]], Any]


# ---------------------------------------------------------------------------
# Hilfen
# ---------------------------------------------------------------------------


def _beschreibung(fixtermin: Fixtermin) -> str:
    return f"{fixtermin.lieferant_kurz} / {fixtermin.kunde_kurz} am {fixtermin.liefertag:%d.%m.%Y}"


def _ereignis_text(ereignis: MailEreignis) -> str:
    return f"{ereignis.typ.value} '{ereignis.betreff}' von {ereignis.absender} ({ereignis.empfangen:%d.%m.%Y})"


def _message_id(mail: Mail) -> str:
    kennung = (mail.kopf.id or "").strip()
    if _MESSAGE_ID_RE.match(kennung):
        return kennung
    sicher = _UNSICHER_RE.sub("-", kennung).strip("-") or hashlib.sha1(kennung.encode("utf-8")).hexdigest()[:16]
    return f"<{sicher}@posteingang.auftragsautomat.invalid>"


def eml_aus_mail(mail: Mail, *, empfangen: datetime | None = None) -> bytes:
    """Deterministische ``.eml`` einer gelesenen Posteingangsmail für das Dossier (``06_Mails/``).

    Kopfzeilen aus den Mail-Daten (``Date`` = Empfangszeit, ``Message-ID`` = Nachrichten-ID, sofern sie
    eine ist), Text und HTML als Alternative mit fester MIME-Grenze; Anhänge werden nicht eingebettet,
    sondern als ``X-Auftragsautomat-Anhang`` genannt (die Bestätigung des Weinguts legt der Lauf getrennt
    als "Oc" ab). Gleiche Mail -> gleiche Bytes, damit das Dossier sie beim zweiten Lauf überspringt.
    """
    zeit = empfangen or mail.kopf.empfangen
    nachricht = EmailMessage(policy=email.policy.SMTP)
    nachricht["Subject"] = mail.kopf.betreff or "(ohne Betreff)"
    if mail.kopf.absender:
        nachricht["From"] = formataddr((mail.kopf.absender_name or "", mail.kopf.absender))
    if mail.an:
        nachricht["To"] = ", ".join(mail.an)
    if mail.cc:
        nachricht["Cc"] = ", ".join(mail.cc)
    nachricht["Date"] = format_datetime(zeit if zeit.tzinfo is not None else zeit.astimezone())
    nachricht["Message-ID"] = _message_id(mail)
    nachricht["X-Auftragsautomat-Quelle"] = "posteingang"
    for anhang in mail.anhaenge:
        nachricht["X-Auftragsautomat-Anhang"] = anhang.name
    text = mail.text or html_zu_text(mail.html) or ""
    nachricht.set_content(text)
    if mail.html:
        nachricht.add_alternative(mail.html, subtype="html")
        grenze = hashlib.sha1(_message_id(mail).encode("utf-8")).hexdigest()[:24]
        nachricht.set_boundary(f"==auftragsautomat-{grenze}==")
    return nachricht.as_bytes()


def _ist_pdf(anhang: MailAnhang) -> bool:
    return bool(_PDF_RE.search(anhang.name or "")) or (anhang.content_type or "").lower() == "application/pdf"


def _fuer_tracking(ereignis: MailEreignis) -> MailEreignis:
    """Kopie des Ereignisses mit zeitzonenloser Empfangszeit (Ortszeit) für die Tracking-Kette.

    Graph und ``.eml``-Fixtures liefern Empfangszeiten mit Zeitzone, Termine aus Mailtexten sind zeitzonenlos.
    Die Tracking-Quellen sortieren seit der Integration über ``tracking.basis.zeit_schluessel`` und vertragen
    beides; die Umrechnung hier hält die Meilensteine einer Sendung trotzdem einheitlich in Ortszeit (Anzeige
    im Bericht). Die Dossier-Ablage (Schritt 3) arbeitet weiter mit der Originalzeit.
    """
    zeit = ereignis.empfangen
    if zeit.tzinfo is None:
        return ereignis
    return ereignis.model_copy(update={"empfangen": zeit.astimezone().replace(tzinfo=None)})


# ---------------------------------------------------------------------------
# Lauf
# ---------------------------------------------------------------------------


class Ueberwachungslauf:
    """Ein Überwachungslauf mit injizierten Bausteinen (Register, Kontakte, Entwurfsfabrik, Tracking, Dossiers).

    ``backends`` ist :class:`~auftragsautomat.m365.fabrik.Backends` (oder ein Objekt mit ``.mail``) bzw. direkt
    ein ``MailBackend``; der Lauf nutzt davon nur das Mail-Backend (Posteingang lesen, Entwürfe anlegen).
    ``entwurfsfabrik`` ist eine :class:`Entwurfsfabrik` oder eine Funktion ``firma_id -> Entwurfsfabrik``.
    ``tracking_kette_fabrik`` baut aus den Mail-Ereignissen die Tracking-Kette (z. B.
    ``lambda ereignisse: erzeuge_kette(settings, ereignisse)``); eine fertige Kette/Quelle mit ``aktualisiere``
    wird direkt verwendet. ``heute`` legt den Stichtag fest (Tests), ``jetzt`` die Zeitquelle.
    """

    def __init__(
        self,
        settings: Settings,
        backends: Any,
        register: FixterminRegister,
        kontakte: Zuordner | None,
        entwurfsfabrik: Entwurfsfabrik | Callable[[str | None], Entwurfsfabrik],
        tracking_kette_fabrik: TrackingKetteFabrik | Any | None,
        dossiers: DossierVerwaltung | None,
        *,
        heute: date | None = None,
        regelwerk: Regelwerk | None = None,
        drossel: Drossel | None = None,
        jetzt: Callable[[], datetime] | None = None,
    ) -> None:
        self.settings = settings
        self.backends = backends
        self.mail: MailBackend = backends if hasattr(backends, "erstelle_entwurf") else backends.mail
        self.register = register
        self.kontakte = kontakte
        self._fabrik = entwurfsfabrik
        self._kette_fabrik = tracking_kette_fabrik
        self.dossiers = dossiers
        self._heute = heute
        self._jetzt = jetzt
        self.regelwerk = regelwerk or Regelwerk.aus_settings(settings)
        self.drossel = drossel or Drossel.aus_settings(settings)
        self.hinweise: list[str] = []
        self.protokoll: list[str] = []
        self.bericht: Ueberwachungsbericht | None = None
        self._fabriken: dict[str | None, Entwurfsfabrik] = {}
        # Dry Run: Änderungen, die ein scharfer Lauf in die Excel schreiben würde (je Fixtermin-Schlüssel)
        self._simulation: dict[str, dict[str, Any]] = {}

    # -- Bausteine aus der Konfiguration ---------------------------------------------------------

    @classmethod
    def aus_settings(
        cls,
        settings: Settings,
        *,
        backends: Any | None = None,
        heute: date | None = None,
        firma_id: str | None = None,
        kontaktliste: Any | None = None,
        stammdaten: Mapping[str, Any] | None = None,
        tracking_kette_fabrik: TrackingKetteFabrik | None = None,
        regelwerk: Regelwerk | None = None,
        jetzt: Callable[[], datetime] | None = None,
    ) -> Ueberwachungslauf:
        """Verdrahtet den Lauf laut ``settings`` (Backends, Fixterminüberwachung, Kontaktliste, Dossiers, Tracking).

        ``firma_id`` wählt die Firma für Signatur und Absender der Nachfragen (Standard: erste Firma der
        Konfiguration – Fixtermine aus der Excel tragen keine Firma). Fehlt die Kontaktliste, läuft die
        Überwachung ohne Empfänger (Hinweis in den Entwürfen).
        """
        from auftragsautomat.kontakte.liste import kontaktliste_aus_settings
        from auftragsautomat.m365.fabrik import erzeuge_backends
        from auftragsautomat.stammdaten.laden import lade_alle
        from auftragsautomat.tracking.kette import erzeuge_kette

        if backends is None:
            backends = erzeuge_backends(settings)
        register = FixterminRegister(backends.excel, settings.m365.fixtermin_datei, bundesland=settings.ueberwachung.bundesland)
        zuordner: Zuordner | None = None
        try:
            liste = kontaktliste if kontaktliste is not None else kontaktliste_aus_settings(settings, backends.excel)
            if stammdaten is None:
                try:
                    stammdaten = lade_alle(settings)
                except Exception as exc:  # Stammdaten sind für die Überwachung optional
                    logger.warning("Stammdaten nicht geladen (%s: %s); Kontakte werden nur über Namen zugeordnet.", type(exc).__name__, exc)
                    stammdaten = None
            zuordner = Zuordner(liste, stammdaten, settings)
        except Exception as exc:
            logger.error("Kontaktliste nicht lesbar (%s: %s); Nachfragen bekommen keine Empfänger.", type(exc).__name__, exc)
        if not settings.firmen:
            raise ValueError("Keine Firma konfiguriert (config/firmen.yaml); der Überwachungslauf braucht eine Firma für Signatur und Absender.")
        firma = settings.firma(firma_id) if firma_id else next(iter(settings.firmen.values()))
        fabrik = Entwurfsfabrik(settings, zuordner, firma)
        dossiers = DossierVerwaltung.aus_settings(settings, datei_backend=backends.datei, jetzt=jetzt)
        kette_fabrik = tracking_kette_fabrik or (lambda ereignisse: erzeuge_kette(settings, ereignisse))
        return cls(settings, backends, register, zuordner, fabrik, kette_fabrik, dossiers, heute=heute, regelwerk=regelwerk, jetzt=jetzt)

    # -- Zeit ---------------------------------------------------------------------------------------

    @property
    def heute(self) -> date:
        if self._heute is not None:
            return self._heute
        return self._jetzt().date() if self._jetzt is not None else date.today()

    def _zeitpunkt(self) -> datetime:
        if self._jetzt is not None:
            return self._jetzt()
        jetzt = datetime.now().astimezone()
        if self._heute is not None and jetzt.date() != self._heute:
            return datetime.combine(self._heute, jetzt.time(), tzinfo=jetzt.tzinfo)
        return jetzt

    # -- Hilfen -------------------------------------------------------------------------------------

    def _fabrik_fuer(self, firma_id: str | None) -> Entwurfsfabrik:
        if isinstance(self._fabrik, Entwurfsfabrik):
            return self._fabrik
        if firma_id not in self._fabriken:
            self._fabriken[firma_id] = self._fabrik(firma_id)
        return self._fabriken[firma_id]

    @staticmethod
    def _fehlertext(was: str, exc: BaseException) -> str:
        return f"{was}: {type(exc).__name__}: {exc}"

    def _fehler(self, bericht: Ueberwachungsbericht, was: str, exc: BaseException) -> None:
        text = self._fehlertext(was, exc)
        logger.exception("Überwachungslauf – %s", text)
        if text not in bericht.fehler:
            bericht.fehler.append(text)

    def _hinweis(self, text: str) -> None:
        if text not in self.hinweise:
            self.hinweise.append(text)
        logger.info("Hinweis: %s", text)

    def _schritt(self, bericht: Ueberwachungsbericht, was: str, aktion: Callable[[], Any], ersatz: Any) -> Any:
        try:
            return aktion()
        except Exception as exc:
            self._fehler(bericht, was, exc)
            return ersatz

    @staticmethod
    def _schluessel(fixtermin: Fixtermin) -> str:
        if fixtermin.zeile is not None:
            return f"zeile:{fixtermin.zeile}"
        return f"{fixtermin.lieferant_kurz}|{fixtermin.kunde_kurz}|{fixtermin.liefertag.isoformat()}"

    def _simuliere(self, fixtermin: Fixtermin, aenderung: Mapping[str, Any]) -> None:
        """Dry Run: merkt sich, was ein scharfer Lauf in die Zeile schreiben würde (Excel bleibt unverändert)."""
        self._simulation.setdefault(self._schluessel(fixtermin), {}).update(aenderung)

    def _sicht(self, fixtermin: Fixtermin) -> Fixtermin:
        """Die Zeile so, wie sie nach einem scharfen Lauf aussähe (Dry Run); ohne Simulation unverändert.

        Damit bewertet der Dry Run wie der scharfe Lauf: ein Weingut, dessen Bestätigung gerade eingegangen ist,
        bekommt auch im Dry Run keine Nachfrage. Der Bericht zeigt weiter den echten Stand der Excel.
        """
        aenderung = self._simulation.get(self._schluessel(fixtermin))
        return fixtermin.model_copy(update=aenderung) if aenderung else fixtermin

    @staticmethod
    def _ersetze(fixtermine: list[Fixtermin], alt: Fixtermin, neu: Fixtermin) -> None:
        for i, ft in enumerate(fixtermine):
            if ft is alt or (ft.zeile is not None and ft.zeile == alt.zeile):
                fixtermine[i] = neu
                return
        fixtermine.append(neu)

    # -- Schritt 3: Ereignisse -------------------------------------------------------------------------

    def _verarbeite_ereignis(
        self,
        ereignis: MailEreignis,
        fixtermine: list[Fixtermin],
        dossiers: list[Dossier],
        bericht: Ueberwachungsbericht,
        notizen: dict[str, list[str]],
        dry_run: bool,
    ) -> None:
        k = self.settings.ueberwachung
        text = _ereignis_text(ereignis)
        if ereignis.typ == Ereignistyp.KONTAKTAENDERUNG:
            self._kontaktaenderungen(ereignis, bericht)
        zuordnung: Zuordnung = ordne_zu(
            ereignis, fixtermine, dossiers, konfidenz_min=self.regelwerk.zuordnung_konfidenz_min, text_suche=self.regelwerk.text_zuordnung
        )
        ft = zuordnung.fixtermin
        if ft is None:
            if zuordnung.kandidaten:
                self._hinweis(f"Mehrdeutig, nichts geändert: {text} – {zuordnung.grund}")
            elif ereignis.typ not in (Ereignistyp.KONTAKTAENDERUNG, Ereignistyp.SONSTIGES):
                self._hinweis(f"Nicht zugeordnet: {text} – {zuordnung.grund}")
            if zuordnung.dossier is not None:
                self._mail_ins_dossier(zuordnung.dossier, ereignis, {}, dry_run, [])
            return

        notiz = notizen.setdefault(self._schluessel(ft), [])
        automatisch = k.bestaetigungen_automatisch_setzen and zuordnung.eindeutig
        herkunft = f"{text}; Zuordnung: {zuordnung.grund}"
        checkliste: dict[str, bool] = {}
        typ = ereignis.typ
        if typ == Ereignistyp.SPEDITION_BESTAETIGUNG:
            ft = self._setze_haekchen(ft, "spedition", herkunft, automatisch, dry_run, fixtermine, notiz)
            checkliste["spedition_bestaetigt"] = True
            ft = self._referenz_eintragen(ft, ereignis, automatisch, dry_run, fixtermine, notiz)
            if ereignis.neues_datum is not None and ereignis.neues_datum != ft.liefertag:
                grund = f"Spedition meldet Anlieferung am {ereignis.neues_datum:%d.%m.%Y} (Zeitfenster laut Transporeon); Fixtermin {ft.liefertag:%d.%m.%Y}"
                ft = self._terminvorschlag(ft, ereignis, grund, bericht, notizen, notiz, fixtermine, dry_run, zuordnung.eindeutig)
        elif typ == Ereignistyp.LIEFERANT_BESTAETIGUNG:
            ft = self._setze_haekchen(ft, "lieferant", herkunft, automatisch, dry_run, fixtermine, notiz)
            checkliste["lieferant_bestaetigt"] = True
            if ereignis.neues_datum is not None:
                ft = self._verladedatum(ft, ereignis, automatisch, dry_run, fixtermine, notiz)
        elif typ == Ereignistyp.TERMINAENDERUNG:
            if ereignis.neues_datum is None:
                notiz.append(f"Terminänderung ohne verwertbares Datum gemeldet: {text}")
                self._hinweis(f"Terminänderung ohne Datum: {text} – bitte Mail prüfen ({_beschreibung(ft)})")
            elif ereignis.neues_datum != ft.liefertag:
                grund = f"Terminänderung laut Mail: {ereignis.betreff} ({ereignis.absender}, {ereignis.empfangen:%d.%m.%Y})"
                ft = self._terminvorschlag(ft, ereignis, grund, bericht, notizen, notiz, fixtermine, dry_run, zuordnung.eindeutig)
            else:
                notiz.append(f"Terminänderung bestätigt den Fixtermin {ft.liefertag:%d.%m.%Y}: {text}")
        elif typ == Ereignistyp.ABHOLUNG:
            ft = self._setze_haekchen(ft, "abgeholt", herkunft, automatisch, dry_run, fixtermine, notiz)
            checkliste["abgeholt"] = True
        elif typ == Ereignistyp.ZUSTELLUNG:
            checkliste["zugestellt"] = True
            notiz.append(f"Zustellung gemeldet: {text}")
        elif typ == Ereignistyp.SONSTIGES:
            notiz.append(f"Mail der Spedition: {text}")
        else:
            notiz.append(f"Mail: {text}")

        dossier = zuordnung.dossier or dossier_fuer(ereignis, ft, dossiers)
        if dossier is not None:
            self._mail_ins_dossier(dossier, ereignis, checkliste, dry_run, notiz)

    def _setze_haekchen(
        self,
        ft: Fixtermin,
        feld: str,
        herkunft: str,
        automatisch: bool,
        dry_run: bool,
        fixtermine: list[Fixtermin],
        notiz: list[str],
    ) -> Fixtermin:
        feldname = BESTAETIGUNGSFELDER[feld]
        label = _HAEKCHEN_TEXT[feld]
        if getattr(ft, feldname):
            notiz.append(f"'{label}' war bereits gesetzt ({herkunft})")
            return ft
        if not automatisch:
            notiz.append(f"Vorschlag: Häkchen '{label}' setzen ({herkunft})")
            self._hinweis(f"Vorschlag für {_beschreibung(ft)}: Häkchen '{label}' setzen ({herkunft})")
            return ft
        if dry_run:
            notiz.append(f"Dry Run: Häkchen '{label}' würde gesetzt ({herkunft})")
            self._simuliere(ft, {feldname: True})
            return ft
        neu = self.register.setze_bestaetigung(ft, feld, True)  # type: ignore[arg-type]
        self._ersetze(fixtermine, ft, neu)
        self.protokoll.append(f"{_beschreibung(neu)}: Häkchen '{label}' gesetzt ({herkunft})")
        notiz.append(f"Häkchen '{label}' gesetzt ({herkunft})")
        return neu

    def _referenz_eintragen(
        self,
        ft: Fixtermin,
        ereignis: MailEreignis,
        automatisch: bool,
        dry_run: bool,
        fixtermine: list[Fixtermin],
        notiz: list[str],
    ) -> Fixtermin:
        referenz = (ereignis.referenz or "").strip()
        if not referenz:
            return ft
        vorhanden = norm_nummer(ft.tracking_referenz)
        if vorhanden == norm_nummer(referenz):
            return ft
        if vorhanden is not None:
            self._hinweis(f"{_beschreibung(ft)}: Referenz {referenz} aus der Mail weicht von der eingetragenen {ft.tracking_referenz} ab – bitte prüfen")
            notiz.append(f"Referenz {referenz} weicht von {ft.tracking_referenz} ab")
            return ft
        if not automatisch:
            notiz.append(f"Vorschlag: Referenz {referenz} in die Bemerkung eintragen")
            return ft
        if dry_run:
            notiz.append(f"Dry Run: Referenz {referenz} würde in die Bemerkung eingetragen")
            self._simuliere(ft, {"tracking_referenz": referenz})
            return ft
        neu = self.register.trage_ein(ft.model_copy(update={"tracking_referenz": referenz}))
        self._ersetze(fixtermine, ft, neu)
        self.protokoll.append(f"{_beschreibung(neu)}: Referenz {referenz} in die Bemerkung eingetragen")
        notiz.append(f"Referenz {referenz} in die Bemerkung eingetragen")
        return neu

    def _verladedatum(
        self,
        ft: Fixtermin,
        ereignis: MailEreignis,
        automatisch: bool,
        dry_run: bool,
        fixtermine: list[Fixtermin],
        notiz: list[str],
    ) -> Fixtermin:
        datum = ereignis.neues_datum
        assert datum is not None
        if ft.verladedatum == datum:
            return ft
        if ft.liefertag is not None and datum > ft.liefertag:
            # unplausibel: Verladung nach dem Fixtermin – nie automatisch eintragen, nur melden
            notiz.append(f"Verladedatum laut Lieferant {datum:%d.%m.%Y} liegt nach dem Fixtermin {ft.liefertag:%d.%m.%Y} – nicht eingetragen")
            self._hinweis(
                f"{_beschreibung(ft)}: Verladedatum laut Lieferant {datum:%d.%m.%Y} liegt nach dem Fixtermin "
                f"{ft.liefertag:%d.%m.%Y} – nicht eingetragen, bitte prüfen (ggf. Termin verschieben)"
            )
            return ft
        if ft.verladedatum is not None:
            notiz.append(f"Verladedatum laut Lieferant {datum:%d.%m.%Y} weicht von der Excel ({ft.verladedatum:%d.%m.%Y}) ab")
            self._hinweis(f"{_beschreibung(ft)}: Verladedatum laut Lieferant {datum:%d.%m.%Y}, in der Excel {ft.verladedatum:%d.%m.%Y} – bitte prüfen")
            return ft
        if not automatisch:
            notiz.append(f"Vorschlag: Verladedatum {datum:%d.%m.%Y} laut Lieferant eintragen")
            return ft
        if dry_run:
            notiz.append(f"Dry Run: Verladedatum {datum:%d.%m.%Y} würde eingetragen")
            self._simuliere(ft, {"verladedatum": datum})
            return ft
        neu = self.register.setze_verladedatum(ft, datum)
        self._ersetze(fixtermine, ft, neu)
        self.protokoll.append(f"{_beschreibung(neu)}: Verladedatum {datum:%d.%m.%Y} laut Lieferant eingetragen")
        notiz.append(f"Verladedatum {datum:%d.%m.%Y} laut Lieferant eingetragen")
        return neu

    def _terminvorschlag(
        self,
        ft: Fixtermin,
        ereignis: MailEreignis,
        grund: str,
        bericht: Ueberwachungsbericht,
        notizen: dict[str, list[str]],
        notiz: list[str],
        fixtermine: list[Fixtermin],
        dry_run: bool,
        eindeutig: bool,
    ) -> Fixtermin:
        neues_datum = ereignis.neues_datum
        assert neues_datum is not None
        for vorhanden in bericht.terminaenderungen:
            if vorhanden.neues_datum == neues_datum and self._schluessel(vorhanden.fixtermin) == self._schluessel(ft):
                notiz.append(f"Terminänderung auf {neues_datum:%d.%m.%Y} bereits vorgemerkt")
                return ft
        vorschlag = Terminaenderung(fixtermin=ft, neues_datum=neues_datum, grund=grund, quelle=ereignis)
        k = self.settings.ueberwachung
        if k.termine_automatisch_anpassen and eindeutig and not dry_run:
            try:
                neu = self.register.verschiebe(ft, neues_datum, grund)
            except RegisterFehler as exc:
                self._fehler(bericht, f"Termin {_beschreibung(ft)} verschieben", exc)
                notiz.append(f"Terminänderung auf {neues_datum:%d.%m.%Y} nicht übernommen ({exc}); bleibt Vorschlag")
            else:
                vorschlag.uebernommen = True
                vorschlag.fixtermin = neu
                self._ersetze(fixtermine, ft, neu)
                notiz.append(f"Fixtermin von {ft.liefertag:%d.%m.%Y} auf {neues_datum:%d.%m.%Y} verschoben ({grund})")
                notizen[self._schluessel(neu)] = notizen.pop(self._schluessel(ft), notiz)
                self.protokoll.append(f"{_beschreibung(ft)} -> {neues_datum:%d.%m.%Y} verschoben ({grund})")
                bericht.terminaenderungen.append(vorschlag)
                return neu
        else:
            zusatz = "" if eindeutig else " (Zuordnung nicht eindeutig)"
            modus = "Dry Run" if dry_run and k.termine_automatisch_anpassen else "Vorschlag"
            notiz.append(f"{modus}: Terminänderung {ft.liefertag:%d.%m.%Y} -> {neues_datum:%d.%m.%Y}{zusatz} – {grund}")
        bericht.terminaenderungen.append(vorschlag)
        return ft

    def _kontaktaenderungen(self, ereignis: MailEreignis, bericht: Ueberwachungsbericht) -> None:
        if self.kontakte is None:
            self._hinweis(f"Kontaktänderung gemeldet, aber keine Kontaktliste geladen: {_ereignis_text(ereignis)}")
            return
        if not ereignis.mail_id:
            self._hinweis(f"Kontaktänderung ohne Mail-ID, Signatur nicht auswertbar: {_ereignis_text(ereignis)}")
            return
        mail = self.mail.lese_mail(ereignis.mail_id)
        neue: list[KontaktAenderung] = aenderungen_aus_mail(mail, self.kontakte.liste)
        if not neue:
            self._hinweis(f"Kontaktänderung gemeldet, aber keine Abweichung zur Kontaktliste erkannt: {_ereignis_text(ereignis)}")
        for a in neue:
            kennung = (a.feld, a.neu, a.kontakt.quelle if a.kontakt else None)
            if any((v.feld, v.neu, v.kontakt.quelle if v.kontakt else None) == kennung for v in bericht.kontaktaenderungen):
                continue
            bericht.kontaktaenderungen.append(a)

    def _mail_ins_dossier(
        self,
        dossier: Dossier,
        ereignis: MailEreignis,
        checkliste: Mapping[str, bool],
        dry_run: bool,
        notiz: list[str],
    ) -> None:
        if self.dossiers is None or not ereignis.mail_id:
            return
        if dry_run:
            notiz.append(f"Dry Run: Mail würde im Dossier {dossier.name} abgelegt")
            return
        name = mail_dateiname(ereignis.empfangen, ereignis.typ.value)
        schon_da = any(d.name == name for d in dossier.dateien)
        haekchen_neu = {k: v for k, v in checkliste.items() if dossier.checkliste.get(k) != v}
        if schon_da and not haekchen_neu:
            notiz.append(f"Mail liegt bereits im Dossier {dossier.name}")
            return
        mail = self.mail.lese_mail(ereignis.mail_id)
        with tempfile.TemporaryDirectory(prefix="auftragsautomat_ueberwachung_") as tmp:
            pfad = Path(tmp) / f"{ereignis.typ.value}.eml"
            pfad.write_bytes(eml_aus_mail(mail, empfangen=ereignis.empfangen))
            self.dossiers.ergaenze_mail(dossier, pfad, typ=ereignis.typ.value, checkliste=haekchen_neu or None)
        notiz.append(f"Mail im Dossier {dossier.name} abgelegt ({name.rsplit('/', 1)[-1]})")
        self.protokoll.append(f"Dossier {dossier.name}: Mail {ereignis.typ.value} vom {ereignis.empfangen:%d.%m.%Y} abgelegt")
        if ereignis.typ == Ereignistyp.LIEFERANT_BESTAETIGUNG:
            self._oc_anhang(dossier, mail, notiz)

    def _oc_anhang(self, dossier: Dossier, mail: Mail, notiz: list[str]) -> None:
        """PDF-Anhang der Weingut-Bestätigung als Beleg "Oc" ins Dossier (Name über ``benennung.name_fuer_paket``)."""
        assert self.dossiers is not None
        pdfs = [a for a in mail.anhaenge if _ist_pdf(a) and not a.inline]
        if not pdfs:
            return
        paket = self.dossiers.lade_paket(dossier)
        if paket is None:
            notiz.append(f"Anhang {pdfs[0].name} nicht als Oc abgelegt: Dossier {dossier.name} ohne paket.json")
            return
        with tempfile.TemporaryDirectory(prefix="auftragsautomat_oc_") as tmp:
            ordner = Path(tmp)
            try:
                lokal = self.mail.speichere_anhang(mail.kopf.id, pdfs[0].id, ordner / pdfs[0].name)
                self.dossiers.erstelle_oder_aktualisiere(paket, None, lexware_pdfs={"Oc": Path(lokal)}, dossier=dossier)
            except Exception as exc:
                notiz.append(f"Anhang {pdfs[0].name} nicht als Oc abgelegt ({type(exc).__name__}: {exc})")
                logger.warning("Dossier %s: Oc-Anhang %s nicht übernommen: %s", dossier.name, pdfs[0].name, exc)
                return
        notiz.append(f"Bestätigung des Weinguts ({pdfs[0].name}) als Oc im Dossier {dossier.name} abgelegt")
        self.protokoll.append(f"Dossier {dossier.name}: Oc aus Anhang {pdfs[0].name} abgelegt")

    # -- Schritt 4: Fixtermine -------------------------------------------------------------------------

    def _kette(self, ereignisse: list[MailEreignis]) -> Any | None:
        if self._kette_fabrik is None:
            return None
        if hasattr(self._kette_fabrik, "aktualisiere"):
            return self._kette_fabrik
        return self._kette_fabrik([_fuer_tracking(e) for e in ereignisse])

    def _sendung_fuer(self, ft: Fixtermin, dossiers: list[Dossier]) -> Sendung | None:
        referenz = (ft.tracking_referenz or "").strip() or None
        spedition = (ft.spedition or "").strip() or None
        if referenz is None:
            dossier = dossier_fuer_fixtermin(ft, dossiers)
            if dossier is not None:
                referenz = (dossier.tracking_referenz or "").strip() or None
                spedition = spedition or (dossier.spedition or "").strip() or None
        kundenreferenz = (ft.bestellnummer or ft.kundenbestellnummer or ft.ab_nummer or "").strip() or None
        if referenz is None and kundenreferenz is None:
            return None
        return Sendung(spedition=spedition or self.settings.kontakte.standard_spedition, referenz=referenz, kundenreferenz=kundenreferenz)

    def _tracking(self, ft: Fixtermin, kette: Any | None, dossiers: list[Dossier], bericht: Ueberwachungsbericht) -> Sendung | None:
        sendung = self._sendung_fuer(ft, dossiers)
        if sendung is None or kette is None:
            return sendung
        try:
            return kette.aktualisiere(sendung)
        except Exception as exc:
            self._fehler(bericht, f"Tracking {_beschreibung(ft)} ({sendung.referenz or sendung.kundenreferenz})", exc)
            sendung.hinweise.append(self._fehlertext("Tracking fehlgeschlagen", exc))
            return sendung

    def _abholung_aus_tracking(self, ft: Fixtermin, sendung: Sendung | None, dry_run: bool, fixtermine: list[Fixtermin], notiz: list[str]) -> Fixtermin:
        if ft.abgeholt or not tracking_abgeholt(sendung):
            return ft
        assert sendung is not None
        herkunft = f"Tracking {sendung.quelle}: {sendung.status.value}, Referenz {sendung.referenz or sendung.kundenreferenz}"
        automatisch = self.settings.ueberwachung.bestaetigungen_automatisch_setzen and bool(sendung.referenz)
        return self._setze_haekchen(ft, "abgeholt", herkunft, automatisch, dry_run, fixtermine, notiz)

    def _entwurf_fuer(self, massnahme: Massnahme, ft: Fixtermin, sendung: Sendung | None) -> EmailEntwurf | None:
        fabrik = self._fabrik_fuer(ft.firma_id)
        if massnahme.typ == "nachfrage_lieferant":
            kontakt = self.kontakte.kontakt_fuer_lieferant(ft.lieferant_kurz) if self.kontakte is not None else None
            return fabrik.nachfrage_lieferant(ft, kontakt)
        if massnahme.typ in ("nachfrage_spedition", "status_spedition"):
            spedition = (sendung.spedition if sendung is not None else None) or ft.spedition or self.settings.kontakte.standard_spedition
            kontakt = self.kontakte.postfach_spedition(spedition, "status") if self.kontakte is not None else None
            return fabrik.nachfrage_spedition(ft, sendung, kontakt)
        return None

    def _bewerte_fixtermin(
        self,
        ft: Fixtermin,
        kette: Any | None,
        dossiers: list[Dossier],
        notizen: dict[str, list[str]],
        bericht: Ueberwachungsbericht,
        fixtermine: list[Fixtermin],
        dry_run: bool,
    ) -> tuple[UeberwachungsPunkt, list[tuple[EmailEntwurf, str, Fixtermin]]]:
        heute = self.heute
        notiz = list(notizen.get(self._schluessel(ft), []))
        sendung = self._tracking(self._sicht(ft), kette, dossiers, bericht)
        ft = self._abholung_aus_tracking(ft, sendung, dry_run, fixtermine, notiz)
        sicht = self._sicht(ft)  # Dry Run: bewerten wie nach dem scharfen Lauf (ohne zu schreiben)
        bewertung: Bewertung = bewerte(sicht, sendung, heute, konfig=self.settings.ueberwachung, regelwerk=self.regelwerk)
        aktionen: list[str] = []
        geplant: list[tuple[EmailEntwurf, str, Fixtermin]] = []
        for m in bewertung.massnahmen:
            if m.typ in ("nachfrage_lieferant", "nachfrage_spedition", "status_spedition"):
                if not self.drossel.erlaubt(m.typ, ft, heute):
                    letzte = self.drossel.letzte(m.typ, ft)
                    aktionen.append(f"{m.bezeichnung} gedrosselt (zuletzt {letzte:%d.%m.%Y}, erneut nach {self.drossel.erneut_nach_werktagen} Werktagen)")
                    continue
                entwurf = self._entwurf_fuer(m, sicht, sendung)
                if entwurf is None:
                    continue
                geplant.append((entwurf, m.typ, ft))
                aktionen.append(f"{m.bezeichnung}: Entwurf '{entwurf.betreff}'" + (f" an {', '.join(entwurf.an)}" if entwurf.an else " (ohne Empfänger)"))
                self.drossel.merke(m.typ, ft, heute)
            elif m.typ == "tracking":
                if sendung is None:
                    aktionen.append("Tracking: keine Referenz bekannt")
                elif sendung.quelle != "keine":
                    aktionen.append(f"Tracking: {sendung.status.value} ({sendung.quelle})")
                elif sendung.hinweise:
                    aktionen.append(f"Tracking: kein Stand ({sendung.hinweise[-1]})")
                else:
                    aktionen.append("Tracking: kein Stand")
            else:
                aktionen.append(m.bezeichnung)
        aktionen.extend(notiz)
        punkt = UeberwachungsPunkt(fixtermin=ft, ampel=bewertung.ampel, grund=bewertung.grund, aktionen=aktionen, sendung=sendung)
        return punkt, geplant

    # -- Ablauf ---------------------------------------------------------------------------------------

    def ausfuehren(self, dry_run: bool = True) -> Ueberwachungsbericht:
        """Führt den Lauf aus und liefert den Bericht (``self.bericht``); ``self.hinweise`` ergänzt ihn."""
        heute = self.heute
        k = self.settings.ueberwachung
        bericht = Ueberwachungsbericht(zeitpunkt=self._zeitpunkt(), zeitraum_bis=heute + timedelta(days=k.tage_voraus))
        self.hinweise, self.protokoll = [], []
        self._simulation = {}
        ab = heute - timedelta(days=RUECKBLICK_TAGE)
        logger.info("Überwachungslauf %s (%s): Fixtermine %s bis %s", heute.strftime("%d.%m.%Y"), "Dry Run" if dry_run else "scharf", ab.strftime("%d.%m.%Y"), bericht.zeitraum_bis.strftime("%d.%m.%Y"))

        # 1) Fixtermine
        fixtermine: list[Fixtermin] = list(self._schritt(bericht, "Fixterminüberwachung lesen", lambda: self.register.eintraege(ab, bericht.zeitraum_bis), []))

        # 2) Mails
        # TODO-PRUEFEN: Am echten Postfach prüfen, dass GraphMailBackend.suche_mails mit ``seit`` alle Mails der
        # letzten mails_rueckblick_tage liefert (Posteingang inkl. Unterordner? Shared Mailbox über m365.postfach)
        # und dass die Nachrichten-IDs aus suche_mails für lese_mail/speichere_anhang stabil bleiben (Graph-IDs
        # ändern sich beim Verschieben in einen anderen Ordner).
        seit = datetime.combine(heute - timedelta(days=k.mails_rueckblick_tage), time.min).astimezone()
        kontaktliste = self.kontakte.liste if self.kontakte is not None else None
        ereignisse: list[MailEreignis] = list(
            self._schritt(bericht, "Posteingang lesen", lambda: sammle_ereignisse(self.mail, self.settings, seit=seit, kontaktliste=kontaktliste), [])
        )
        dossiers: list[Dossier] = list(self._schritt(bericht, "Dossiers lesen", self.dossiers.liste, [])) if self.dossiers is not None else []

        # 3) Ereignisse zuordnen
        notizen: dict[str, list[str]] = {}
        for ereignis in ereignisse:
            try:
                self._verarbeite_ereignis(ereignis, fixtermine, dossiers, bericht, notizen, dry_run)
            except Exception as exc:
                self._fehler(bericht, f"Ereignis {_ereignis_text(ereignis)}", exc)

        # 4) Fixtermine bewerten
        kette = self._schritt(bericht, "Tracking-Kette aufbauen", lambda: self._kette(ereignisse), None)
        geplant: list[tuple[EmailEntwurf, str, Fixtermin]] = []
        for ft in list(fixtermine):
            try:
                punkt, neue = self._bewerte_fixtermin(ft, kette, dossiers, notizen, bericht, fixtermine, dry_run)
            except Exception as exc:
                self._fehler(bericht, f"Fixtermin {_beschreibung(ft)} bewerten", exc)
                punkt = UeberwachungsPunkt(fixtermin=ft, ampel="rot", grund=self._fehlertext("Bewertung fehlgeschlagen", exc), aktionen=list(notizen.get(self._schluessel(ft), [])))
                neue = []
            bericht.punkte.append(punkt)
            geplant.extend(neue)

        # 5) Entwürfe speichern (nie senden)
        # TODO-PRUEFEN: Am echten Tenant prüfen, dass die Nachfragen als Entwürfe im Ordner "Entwürfe" erscheinen
        # (Graph: POST /me/messages ohne /send) und dass die Erinnerung an kommunikation.absender_email als
        # Entwurf an sich selbst sinnvoll ist (Alternative: Aufgabe/Teams-Nachricht – Rückfrage beim Anwender).
        if geplant:
            fabrik = self._fabrik_fuer(geplant[0][2].firma_id)
            gespeichert = self._schritt(bericht, "Entwürfe speichern", lambda: fabrik.speichere([e for e, _, _ in geplant], self.mail), [])
            for entwurf, typ, ft in geplant:
                if any(e is entwurf for e in gespeichert) and entwurf.speicherort:
                    bericht.entwuerfe.append(entwurf)
                else:
                    self._hinweis(f"Entwurf '{entwurf.betreff}' ({typ}) für {_beschreibung(ft)} wurde nicht gespeichert; Drossel nicht gesetzt")
                    self.drossel.vergiss(typ, ft)
        if not dry_run:
            self._schritt(bericht, "Merkdatei schreiben", self.drossel.speichere, None)

        # 6) Erinnerung intern
        if any(p.ampel in ("gelb", "rot") for p in bericht.punkte) or bericht.terminaenderungen or bericht.kontaktaenderungen or bericht.fehler:
            fabrik = self._fabrik_fuer(None)
            erinnerung = self._schritt(bericht, "Erinnerung erzeugen", lambda: fabrik.erinnerung_intern(bericht), None)
            if erinnerung is not None:
                gespeichert = self._schritt(bericht, "Erinnerung speichern", lambda: fabrik.speichere([erinnerung], self.mail), [])
                if gespeichert:
                    bericht.entwuerfe.append(erinnerung)

        self.bericht = bericht
        logger.info(
            "Überwachungslauf fertig: %d Fixtermine (%s), %d Entwürfe, %d Terminänderungen, %d Kontaktänderungen, %d Fehler, %d Hinweise",
            len(bericht.punkte), self._ampel_zaehler(bericht), len(bericht.entwuerfe), len(bericht.terminaenderungen), len(bericht.kontaktaenderungen), len(bericht.fehler), len(self.hinweise),
        )
        return bericht

    @staticmethod
    def _ampel_zaehler(bericht: Ueberwachungsbericht) -> str:
        zaehler: dict[str, int] = {}
        for p in bericht.punkte:
            zaehler[p.ampel] = zaehler.get(p.ampel, 0) + 1
        return ", ".join(f"{a}: {n}" for a, n in sorted(zaehler.items())) or "keine"


def ueberwachen(
    settings: Settings,
    *,
    dry_run: bool | None = None,
    heute: date | None = None,
    backends: Any | None = None,
    firma_id: str | None = None,
    berichte_ordner: Path | None = None,
    **weitere: Any,
) -> tuple[Ueberwachungsbericht, Path]:
    """Kompletter Lauf laut Konfiguration (für CLI/Aufgabenplanung): verdrahten, ausführen, Bericht schreiben.

    ``dry_run`` (Standard ``settings.dry_run``) lässt Excel, Dossiers und Merkdatei unverändert. Liefert den
    Bericht und den Pfad der Markdown-Datei (``ueberwachung.berichte_ordner``).
    """
    from auftragsautomat.ueberwachung.bericht import schreibe_bericht

    lauf = Ueberwachungslauf.aus_settings(settings, backends=backends, heute=heute, firma_id=firma_id, **weitere)
    bericht = lauf.ausfuehren(settings.dry_run if dry_run is None else dry_run)
    pfad = schreibe_bericht(bericht, berichte_ordner or settings.ueberwachung.berichte_ordner, hinweise=lauf.hinweise, protokoll=lauf.protokoll)
    return bericht, pfad
