"""Stufe 1 (primär, sofort): Sendungsstatus aus Hillebrand-Mails.

Belegtes Muster (einziges bisher beobachtetes) ist die Auftragsbestätigung mit Betreff
``Auftragsbestätigung; Your Ref: <unsere Referenz>; Our Ref: DEMB…[jobDEM…]`` und einem
Lieferzeitfenster "laut Transporeon … am <Datum>" im Text. Meilenstein-Mails des Portals
(Abholung/Zustellung) werden über die englischen Textvorlagen der API erkannt
(``meilenstein_muster`` in ``config/tracking_hillebrand.yaml``); sobald echte Beispiele
vorliegen, werden nur die Muster in der YAML nachgezogen.

Öffentliche Funktionen:

* :func:`erkenne_ereignis` – eine Mail -> :class:`~auftragsautomat.models.MailEreignis` oder ``None``.
  Die Mail ist entweder ein Tupel ``(betreff, absender, text, empfangen[, mail_id])``, ein
  ``m365.basis.Mail`` (``kopf.betreff``/``kopf.absender``/``kopf.empfangen``, ``text``/``html``),
  ein beliebiges Objekt mit den Attributen ``betreff``, ``absender``, ``text``, ``empfangen``
  (auch ``extraktion.email_bestellung.EmailInhalt`` mit ``datum``) oder ein Dict.
* :func:`erkenne_ereignisse` – mehrere Mails, nur erkannte Ereignisse.
* :func:`erkenne_ereignis_aus_datei` – eine ``.eml``-/``.txt``-Datei (z. B. aus dem Dossier).
* :class:`MailQuelle` – :class:`~auftragsautomat.tracking.basis.TrackingQuelle`, die Ereignisse
  der Sendung über ``referenz`` ("Our Ref") oder ``kundenreferenz`` ("Your Ref") zuordnet und
  Status/Meilensteine ableitet.

Postfächer der Spedition stehen nicht im Code: Absender werden nur über die Domain-Liste der
YAML erkannt; die Adressen für Nachfragen liefert die Kontaktliste.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

from auftragsautomat.extraktion.zahlen import finde_datum
from auftragsautomat.models import Ereignistyp, MailEreignis, Meilenstein, Sendung, SendungStatus
from auftragsautomat.tracking.basis import (
    HillebrandKonfig,
    finde_datum_tolerant,
    ist_bekannt,
    ist_fortschritt,
    jetzt,
    kompiliere,
    lade_hillebrand_konfig,
    mit_hinweis,
    normalisiere_referenz,
    parse_datum_mit_bezug,
    status_aus_meilensteinen,
    vereinige_meilensteine,
    zeit_schluessel,
)

logger = logging.getLogger(__name__)

__all__ = [
    "CODE_BESTAETIGUNG",
    "MailQuelle",
    "erkenne_ereignis",
    "erkenne_ereignis_aus_datei",
    "erkenne_ereignisse",
    "meilensteine_aus_ereignissen",
]

CODE_BESTAETIGUNG = "bestaetigung"  # eigener Code für die Auftragsbestätigung der Spedition
_EREIGNIS_CODE: dict[Ereignistyp, str] = {
    Ereignistyp.ABHOLUNG: "shpsupdepcnf",
    Ereignistyp.ZUSTELLUNG: "shpcusarrcnf",
    Ereignistyp.TERMINAENDERUNG: "shpcusarrplnchg",
}
_ADRESSE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", re.I)
_AUSZUG_LAENGE = 300


# ---------------------------------------------------------------------------
# Mail-Felder lesen (Tupel, Dict oder Objekt)
# ---------------------------------------------------------------------------


def _erstes_attribut(obj: Any, *namen: str) -> Any:
    for name in namen:
        if hasattr(obj, name):
            wert = getattr(obj, name)
            if wert is not None and wert != "":
                return wert
    return None


def _als_text(wert: Any) -> str:
    if wert is None:
        return ""
    if isinstance(wert, bytes):
        return wert.decode("utf-8", errors="replace")
    return str(wert)


def _absender_text(wert: Any) -> str:
    """Absender als Text ("Name <adresse>"), auch aus Adresse-Objekten (``.adresse``/``.name``)."""
    if wert is None:
        return ""
    if isinstance(wert, str):
        return wert
    adresse = _erstes_attribut(wert, "adresse", "email", "address")
    name = _erstes_attribut(wert, "name", "anzeige")
    if adresse:
        return f"{name} <{adresse}>" if name and name != adresse else str(adresse)
    return str(wert)


def _als_datetime(wert: Any) -> datetime:
    if isinstance(wert, datetime):
        return wert
    if isinstance(wert, date):
        return datetime.combine(wert, time.min)
    if isinstance(wert, str):
        try:
            return datetime.fromisoformat(wert.replace("Z", "+00:00"))
        except ValueError:
            gefunden = finde_datum(wert)
            if gefunden:
                return datetime.combine(gefunden[0], time.min)
    return jetzt()


def _html_zu_text(html: Any) -> str:
    from auftragsautomat.extraktion.email_bestellung import html_zu_text

    return html_zu_text(_als_text(html))


def _mail_felder(mail: Any) -> tuple[str, str, str, datetime, str | None]:
    """Liefert (betreff, absender, text, empfangen, mail_id) aus Tupel, Dict oder Objekt."""
    if isinstance(mail, (tuple, list)):
        if len(mail) < 4:
            raise ValueError("Mail-Tupel braucht (betreff, absender, text, empfangen)")
        betreff, absender, text, empfangen = mail[:4]
        mail_id = mail[4] if len(mail) > 4 else None
        return (
            _als_text(betreff),
            _absender_text(absender),
            _als_text(text),
            _als_datetime(empfangen),
            str(mail_id) if mail_id is not None else None,
        )
    if isinstance(mail, dict):
        text = _als_text(mail.get("text") or mail.get("body"))
        if not text.strip() and (mail.get("html") or mail.get("text_html")):
            text = _html_zu_text(mail.get("html") or mail.get("text_html"))
        mail_id = mail.get("id") or mail.get("mail_id")
        return (
            _als_text(mail.get("betreff") or mail.get("subject")),
            _absender_text(mail.get("absender") or mail.get("von") or mail.get("from")),
            text,
            _als_datetime(mail.get("empfangen") or mail.get("datum") or mail.get("received")),
            str(mail_id) if mail_id is not None else None,
        )
    # m365.basis.Mail: Kopfdaten liegen unter ``kopf`` (MailKopf), Inhalt unter ``text``/``html``
    kopf = getattr(mail, "kopf", None)
    quelle_kopf = kopf if kopf is not None else mail
    betreff = _als_text(_erstes_attribut(quelle_kopf, "betreff", "subject"))
    absender = _absender_text(_erstes_attribut(quelle_kopf, "absender", "von", "sender", "from_"))
    if kopf is not None and getattr(kopf, "absender_name", None) and "<" not in absender and absender:
        absender = f"{kopf.absender_name} <{absender}>"
    text = _als_text(_erstes_attribut(mail, "text", "text_plain", "body"))
    if not text.strip():
        html = _erstes_attribut(mail, "html", "text_html")
        if html:
            text = _html_zu_text(html)
    empfangen = _als_datetime(_erstes_attribut(quelle_kopf, "empfangen", "datum", "received", "gesendet"))
    mail_id = _erstes_attribut(quelle_kopf, "id", "mail_id", "message_id", "nachrichten_id")
    if mail_id is None and kopf is None:
        quelldatei = _erstes_attribut(mail, "quelldatei")
        mail_id = str(quelldatei) if quelldatei is not None else None
    return betreff, absender, text, empfangen, (str(mail_id) if mail_id is not None else None)


def _adresse_aus(absender: str) -> str | None:
    treffer = _ADRESSE.search(absender or "")
    return treffer.group(0).lower() if treffer else None


# ---------------------------------------------------------------------------
# Erkennung
# ---------------------------------------------------------------------------


def _suche(muster: str | None, *texte: str) -> re.Match[str] | None:
    if not muster:
        return None
    regex = kompiliere(muster)
    for t in texte:
        if t:
            treffer = regex.search(t)
            if treffer:
                return treffer
    return None


def _gruppe(treffer: re.Match[str] | None, name: str) -> str | None:
    if treffer is None or name not in treffer.groupdict():
        return None
    wert = treffer.group(name)
    return wert.strip() if wert else None


def _datum_in(text: str | None, bezug: datetime) -> date | None:
    """Erstes Datum im Text (deutsch, ISO oder englisch "22 Oct 2026"); "27.10." bekommt das Jahr aus dem Bezug."""
    gefunden = finde_datum_tolerant(text, bezug)
    return gefunden.date() if gefunden else None


def _satz_um(text: str, treffer: re.Match[str]) -> str:
    """Der Satz/Absatz rund um die Fundstelle (für Datum und Textauszug)."""
    anfang = max(text.rfind("\n", 0, treffer.start()), text.rfind(". ", 0, treffer.start()))
    anfang = anfang + 1 if anfang >= 0 else 0
    ende_kandidaten = [i for i in (text.find("\n", treffer.end()), text.find(". ", treffer.end())) if i >= 0]
    ende = min(ende_kandidaten) if ende_kandidaten else len(text)
    return text[anfang:ende].strip()


def _auszug(text: str, laenge: int = _AUSZUG_LAENGE) -> str | None:
    t = " ".join((text or "").split())
    if not t:
        return None
    return t if len(t) <= laenge else t[: laenge - 1] + "…"


def erkenne_ereignis(mail: Any, konfig: HillebrandKonfig | None = None) -> MailEreignis | None:
    """Erkennt aus einer Mail ein Spedition-Ereignis (Bestätigung, Terminänderung, Abholung, Zustellung).

    ``mail``: Tupel ``(betreff, absender, text, empfangen[, mail_id])``, ``m365.basis.Mail``, Dict
    oder ein Objekt mit diesen Attributen. Liefert ``None``, wenn die Mail weder von einer bekannten
    Hillebrand-Domain kommt noch eine Hillebrand-Referenz enthält.
    """
    konfig = konfig or lade_hillebrand_konfig()
    try:
        betreff, absender, text, empfangen, mail_id = _mail_felder(mail)
    except ValueError as exc:
        logger.warning("Mail kann nicht gelesen werden: %s", exc)
        return None
    adresse = _adresse_aus(absender)
    bekannt = konfig.ist_absender_bekannt(adresse)

    # Referenzen ("Our Ref", "Your Ref") – Betreff vor Text
    konfidenz = 1.0 if bekannt else 0.8
    ref_treffer = _suche(konfig.referenz_regex, betreff, text)
    referenz = _gruppe(ref_treffer, "referenz")
    if referenz is None:
        frei = _suche(konfig.referenz_frei_regex, betreff, text)
        referenz = _gruppe(frei, "referenz")
        if referenz is not None:
            # ohne "Our Ref"-Präfix: von bekannter Domain noch gut, sonst nur Vermutung
            konfidenz = min(konfidenz, 0.85 if bekannt else 0.6)
    kundenreferenz = _gruppe(_suche(konfig.kundenreferenz_regex, betreff, text), "kundenref")

    if not bekannt and referenz is None:
        return None

    typ: Ereignistyp | None = None
    neues_datum: date | None = None
    auszug: str | None = None

    best = _suche(konfig.betreff_regex.bestaetigung, betreff)
    if best is not None:
        typ = Ereignistyp.SPEDITION_BESTAETIGUNG
        referenz = _gruppe(best, "referenz") or referenz
        kundenreferenz = _gruppe(best, "kundenref") or kundenreferenz
        if bekannt:
            konfidenz = 1.0
        fenster = _suche(konfig.zeitfenster_regex, text)
        if fenster is not None:
            neues_datum = parse_datum_mit_bezug(_gruppe(fenster, "datum"), empfangen)
            auszug = _satz_um(text, fenster)
    else:
        aenderung = _suche(konfig.terminaenderung_regex, text, betreff)
        betreff_aenderung = _suche(konfig.betreff_regex.terminaenderung, betreff)
        fenster = _suche(konfig.zeitfenster_regex, text)
        if aenderung is not None:
            typ = Ereignistyp.TERMINAENDERUNG
            neues_datum = parse_datum_mit_bezug(_gruppe(aenderung, "datum"), empfangen)
            auszug = _satz_um(aenderung.string, aenderung)
        elif betreff_aenderung is not None or fenster is not None:
            typ = Ereignistyp.TERMINAENDERUNG
            if fenster is not None:
                neues_datum = parse_datum_mit_bezug(_gruppe(fenster, "datum"), empfangen)
                auszug = _satz_um(text, fenster)
            else:
                neues_datum = _datum_in(text, empfangen)
            konfidenz = min(konfidenz, 0.8)
        else:
            muster = konfig.muster_fuer_text(betreff) or konfig.muster_fuer_text(text)
            if muster is not None:
                typ = muster.ereignis
                regex = kompiliere(muster.muster)
                treffer = regex.search(text) or regex.search(betreff)  # Text zuerst: enthält Datum und Ort
                if treffer is not None:
                    auszug = _satz_um(treffer.string, treffer)
                if typ == Ereignistyp.TERMINAENDERUNG:
                    neues_datum = _datum_in(auszug, empfangen) or _datum_in(text, empfangen)
                konfidenz = min(konfidenz, 0.9)
            elif referenz is not None or kundenreferenz is not None:
                typ = Ereignistyp.SONSTIGES
                konfidenz = min(konfidenz, 0.5)

    if typ is None:
        return None
    ereignis = MailEreignis(
        typ=typ,
        absender=adresse or absender.strip() or "unbekannt",
        betreff=betreff.strip(),
        empfangen=empfangen,
        referenz=normalisiere_referenz(referenz),
        kundenreferenz=kundenreferenz,
        neues_datum=neues_datum,
        text_auszug=auszug or _auszug(text),
        mail_id=mail_id,
        konfidenz=konfidenz,
    )
    logger.debug("Mail-Ereignis erkannt: %s %s (%s)", ereignis.typ, ereignis.referenz, ereignis.betreff)
    return ereignis


def erkenne_ereignisse(mails: Iterable[Any], konfig: HillebrandKonfig | None = None) -> list[MailEreignis]:
    """Alle erkannten Ereignisse aus mehreren Mails (nicht erkannte werden übersprungen)."""
    konfig = konfig or lade_hillebrand_konfig()
    ergebnis: list[MailEreignis] = []
    for mail in mails:
        try:
            ereignis = erkenne_ereignis(mail, konfig)
        except Exception as exc:  # eine kaputte Mail darf den Lauf nicht stoppen
            logger.warning("Mail übersprungen (%s): %s", type(exc).__name__, exc)
            continue
        if ereignis is not None:
            ergebnis.append(ereignis)
    return ergebnis


def erkenne_ereignis_aus_datei(pfad: Path | str, konfig: HillebrandKonfig | None = None) -> MailEreignis | None:
    """Liest eine ``.eml``/``.txt``-Datei (Standardbibliothek) und erkennt das Ereignis."""
    from auftragsautomat.extraktion.email_bestellung import EmailFehler, lese_email

    try:
        inhalt = lese_email(Path(pfad))
    except (EmailFehler, OSError, ValueError) as exc:
        logger.warning("%s", exc)
        return None
    ereignis = erkenne_ereignis(inhalt, konfig)
    if ereignis is not None and ereignis.mail_id is None:
        ereignis = ereignis.model_copy(update={"mail_id": str(pfad)})
    return ereignis


# ---------------------------------------------------------------------------
# Quelle
# ---------------------------------------------------------------------------


def _zeitpunkt(datum: date | None, sonst: datetime) -> datetime:
    return datetime.combine(datum, time.min) if datum is not None else sonst


def meilensteine_aus_ereignissen(ereignisse: Iterable[MailEreignis], konfig: HillebrandKonfig) -> list[Meilenstein]:
    """Mail-Ereignisse -> Meilensteine (chronologisch, Doppelte entfernt)."""
    meilensteine: list[Meilenstein] = []
    for e in sorted(ereignisse, key=lambda x: zeit_schluessel(x.empfangen)):
        if e.typ == Ereignistyp.SPEDITION_BESTAETIGUNG:
            meilensteine.append(
                Meilenstein(
                    code=CODE_BESTAETIGUNG,
                    text=f"Auftragsbestätigung der Spedition: {e.betreff}",
                    zeitpunkt=e.empfangen,
                )
            )
            if e.neues_datum is not None:
                meilensteine.append(
                    Meilenstein(
                        code="shpcusarrpln",
                        text=f"Anlieferung laut Transporeon am {e.neues_datum:%d.%m.%Y}",
                        zeitpunkt=_zeitpunkt(e.neues_datum, e.empfangen),
                        geplant=True,
                    )
                )
        elif e.typ == Ereignistyp.TERMINAENDERUNG:
            if e.neues_datum is not None:
                meilensteine.append(
                    Meilenstein(
                        code=_EREIGNIS_CODE[Ereignistyp.TERMINAENDERUNG],
                        text=f"Neuer Liefertermin laut Spedition: {e.neues_datum:%d.%m.%Y}",
                        zeitpunkt=_zeitpunkt(e.neues_datum, e.empfangen),
                        geplant=True,
                    )
                )
        elif e.typ in (Ereignistyp.ABHOLUNG, Ereignistyp.ZUSTELLUNG):
            muster = konfig.muster_fuer_text(e.text_auszug) or konfig.muster_fuer_text(e.betreff)
            code = muster.code if muster is not None and muster.ereignis == e.typ else _EREIGNIS_CODE[e.typ]
            datum = _datum_in(e.text_auszug, e.empfangen)
            meilensteine.append(
                Meilenstein(code=code, text=e.text_auszug or e.betreff, zeitpunkt=_zeitpunkt(datum, e.empfangen))
            )
        elif e.typ == Ereignistyp.SONSTIGES:
            muster = konfig.muster_fuer_text(e.betreff) or konfig.muster_fuer_text(e.text_auszug)
            if muster is None:
                continue
            datum = _datum_in(e.text_auszug, e.empfangen) if muster.geplant else None
            meilensteine.append(
                Meilenstein(
                    code=muster.code,
                    text=e.text_auszug or e.betreff,
                    zeitpunkt=_zeitpunkt(datum, e.empfangen),
                    geplant=muster.geplant,
                )
            )
    return vereinige_meilensteine([], meilensteine)


class MailQuelle:
    """Tracking-Quelle aus bereits erkannten Mail-Ereignissen (z. B. Posteingang der letzten 7 Tage)."""

    name = "mail"

    def __init__(self, ereignisse: Iterable[MailEreignis], konfig: HillebrandKonfig | None = None) -> None:
        self._ereignisse = list(ereignisse)
        self._konfig = konfig or lade_hillebrand_konfig()

    @property
    def ereignisse(self) -> list[MailEreignis]:
        return list(self._ereignisse)

    def verfuegbar(self) -> bool:
        return True

    def passende_ereignisse(self, sendung: Sendung) -> list[MailEreignis]:
        """Ereignisse mit gleicher Referenz ("Our Ref") oder gleicher Kundenreferenz ("Your Ref")."""
        referenz = normalisiere_referenz(sendung.referenz)
        kundenref = _norm_kundenref(sendung.kundenreferenz)
        treffer = [
            e
            for e in self._ereignisse
            if (referenz is not None and normalisiere_referenz(e.referenz) == referenz)
            or (kundenref is not None and _norm_kundenref(e.kundenreferenz) == kundenref)
        ]
        treffer.sort(key=lambda e: zeit_schluessel(e.empfangen))
        return treffer

    def aktualisiere(self, sendung: Sendung) -> Sendung:
        try:
            return self._aktualisiere(sendung)
        except Exception as exc:  # nie nach außen
            logger.exception("Mail-Quelle: unerwarteter Fehler")
            return mit_hinweis(sendung, f"Mail-Auswertung fehlgeschlagen: {type(exc).__name__}: {exc}")

    def _aktualisiere(self, sendung: Sendung) -> Sendung:
        if not sendung.referenz and not sendung.kundenreferenz:
            return mit_hinweis(sendung, "Mail-Quelle: Sendung hat weder Referenz noch Kundenreferenz")
        ereignisse = self.passende_ereignisse(sendung)
        if not ereignisse:
            bezug = sendung.referenz or sendung.kundenreferenz
            return mit_hinweis(sendung, f"Keine Hillebrand-Mail zu {bezug} gefunden")

        neue = meilensteine_aus_ereignissen(ereignisse, self._konfig)
        meilensteine = vereinige_meilensteine(sendung.meilensteine, neue)
        basis = sendung.status if ist_bekannt(sendung.status) and sendung.quelle == "mail" else SendungStatus.UNBEKANNT
        if any(e.typ == Ereignistyp.SPEDITION_BESTAETIGUNG for e in ereignisse) and not ist_fortschritt(
            basis, SendungStatus.BESTAETIGT
        ):
            basis = SendungStatus.BESTAETIGT
        status = status_aus_meilensteinen(meilensteine, self._konfig, puenktlich=sendung.puenktlich, basis=basis)

        referenz = sendung.referenz
        kundenreferenz = sendung.kundenreferenz
        geplante_zustellung = sendung.geplante_zustellung
        geplante_abholung = sendung.geplante_abholung
        for e in ereignisse:
            if referenz is None and e.referenz:
                referenz = e.referenz
            if kundenreferenz is None and e.kundenreferenz:
                kundenreferenz = e.kundenreferenz
            if e.typ in (Ereignistyp.SPEDITION_BESTAETIGUNG, Ereignistyp.TERMINAENDERUNG) and e.neues_datum:
                geplante_zustellung = e.neues_datum  # chronologisch: die letzte Mail gewinnt
        for m in meilensteine:
            if m.geplant and m.code.lower().startswith("shpsupdep") and m.zeitpunkt is not None:
                geplante_abholung = m.zeitpunkt.date()

        hinweise = list(sendung.hinweise)
        unsichere = [e for e in ereignisse if e.konfidenz < 0.8]
        if unsichere:
            hinweis = f"{len(unsichere)} Mail-Ereignis(se) mit geringer Konfidenz (bitte prüfen)"
            if hinweis not in hinweise:
                hinweise.append(hinweis)
        return sendung.model_copy(
            update={
                "referenz": referenz,
                "kundenreferenz": kundenreferenz,
                "status": status,
                "meilensteine": meilensteine,
                "geplante_zustellung": geplante_zustellung,
                "geplante_abholung": geplante_abholung,
                "quelle": "mail",
                "letzte_pruefung": jetzt(),
                "hinweise": hinweise,
            }
        )


def _norm_kundenref(text: str | None) -> str | None:
    if text is None:
        return None
    t = re.sub(r"\s+", "", str(text)).strip().upper()
    return t or None
