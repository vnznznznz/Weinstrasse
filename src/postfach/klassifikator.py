"""Regeln auf Mails anwenden: Farbe/Zielordner, Verschieben oder Liegenlassen, Antwort-Entwurf ja/nein.

Reihenfolge der Entscheidung je Mail:

1. Sichere Nein-Fälle für Entwürfe (eigene Mails, Systemabsender, Abwesenheitsnotizen …).
2. Regeln aus ``regeln.json`` in Dateireihenfolge – die erste passende gewinnt.
3. Hat die Mail bereits eine Farbe (Outlook-Kategorie), bestimmt die Farbe den Zielordner;
   die Regel liefert dann nur Hinweise (z. B. „Farbe sagt Intern, Regel sagt Kommissionen“).
4. Behalten-Prüfung: ungelesen/markiert (bis ``ausnahme_aelter_als_tage``), Antwort auf eigene
   Mail, Bewerbungen und ausdrücklich zu behaltende IDs bleiben im Posteingang.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Iterable
from datetime import datetime

from postfach.modelle import Antwort, Entscheidung, Mail
from postfach.regeln import Regel, Regelwerk

_SYSTEM_ABSENDER = (
    "noreply",
    "no-reply",
    "no_reply",
    "donotreply",
    "do-not-reply",
    "mailer-daemon",
    "postmaster",
    "notification",
    "notifications",
    "newsletter",
    "bounce",
    "alert",
    "calendar-notification",
)
_ABWESENHEIT = (
    "abwesenheit",
    "out of office",
    "automatische antwort",
    "automatic reply",
    "autoreply",
    "auto-reply",
    "automatische benachrichtigung",
    "fuera de la oficina",
    "risposta automatica",
    "réponse automatique",
)
_NEWSLETTER_SPUREN = (
    "unsubscribe",
    "abmelden",
    "abbestellen",
    "im browser ansehen",
    "view in browser",
    "in einem web-browser",
    "manage preferences",
    "newsletter",
    "se désabonner",
    "darse de baja",
)
_WEITERLEITUNG = re.compile(r"^\s*(wg|fwd?|tr|i|rv)\s*:", re.IGNORECASE)
_BEWERBUNG = re.compile(r"\bbewerbung", re.IGNORECASE)
_EINLADUNG = re.compile(
    r"^(eingeladen|invitation|zusage|absage|accepted|declined|tentative|mit vorbehalt)\b", re.IGNORECASE
)
_SPEDITEUR_AUTOMAT = re.compile(
    r"(auftragsbestätigung|auftragsbestaetigung|order confirmation);\s*your ref", re.IGNORECASE
)


def _falte(text: str) -> str:
    """Kleinschreibung und Umlaute auflösen, damit „Aufträge“ auf „Auftrag“ passt."""
    tabelle = str.maketrans(
        {"ä": "a", "ö": "o", "ü": "u", "ß": "ss", "é": "e", "è": "e", "á": "a", "ó": "o", "í": "i"}
    )
    return text.lower().translate(tabelle)


def enthaelt_stichwort(text: str, stichworte: Iterable[str]) -> str | None:
    """Erstes passendes Stichwort zurückgeben (Wortanfang; kurze Wörter nur als ganzes Wort + Plural)."""
    gefaltet = _falte(text)
    for wort in stichworte:
        w = _falte(wort.strip())
        if not w:
            continue
        if " " in w:
            muster = re.escape(w)
        elif len(w) <= 5:
            muster = rf"\b{re.escape(w)}(s|es|en|e|n|i)?\b"
        else:
            muster = rf"\b{re.escape(w)}"
        if re.search(muster, gefaltet):
            return wort
    return None


def _domain_passt(adresse: str, domains: Iterable[str]) -> str | None:
    adresse = adresse.lower()
    domain = adresse.rsplit("@", 1)[-1] if "@" in adresse else adresse
    for d in domains:
        d = d.lower().strip()
        if not d:
            continue
        if "." in d:
            if domain == d or domain.endswith("." + d) or adresse.endswith("@" + d):
                return d
        elif d in domain:  # Fragmente wie „boucheundpartner“ oder „sesa“
            return d
    return None


def _adresse_passt(adresse: str, eintraege: Iterable[str]) -> str | None:
    adresse = adresse.lower()
    for e in eintraege:
        e = e.lower().strip()
        if not e:
            continue
        if "@" in e:
            if adresse == e:
                return e
        elif _domain_passt(adresse, [e]):
            return e
    return None


def _muster_passt(mail: Mail, muster: Iterable[str]) -> str | None:
    kandidaten = [mail.absender.lower(), mail.absender_name.lower()]
    for m in muster:
        m = m.lower().strip()
        if not m:
            continue
        for k in kandidaten:
            if ("*" in m or "?" in m) and fnmatch.fnmatch(k, m):
                return m
            if "*" not in m and "?" not in m and m in k:
                return m
    return None


class Klassifikator:
    def __init__(self, regelwerk: Regelwerk, *, heute: datetime | None = None) -> None:
        self.rw = regelwerk
        self.heute = heute
        self._lieferanten_domains = set(regelwerk.lieferanten_domains)
        self._lieferanten_einzeln = set(regelwerk.lieferanten_absender_einzeln)
        self._steuerberater_adressen = [
            a for r in regelwerk.regeln if r.kategorie == "Steuerberater" for a in r.absender_oder_empfaenger
        ]
        # Ausdrücklich genannte Einzeladressen (z. B. info@weinstrasseadolph.de → Marketing) schlagen
        # reine Domain-Treffer anderer Regeln, auch wenn die Domain-Regel früher steht.
        self._explizite_adressen: dict[str, str] = {}
        for r in regelwerk.regeln:
            for a in [*r.zusatz_absender, *r.absender_oder_empfaenger]:
                if "@" in a:
                    self._explizite_adressen.setdefault(a.lower(), r.name)

    # ------------------------------------------------------------------ Hilfen
    def ist_lieferant(self, mail: Mail) -> bool:
        if mail.absender.lower() in self._lieferanten_einzeln:
            return True
        return _domain_passt(mail.absender, self._lieferanten_domains) is not None

    def ist_eigene(self, mail: Mail) -> bool:
        return mail.absender.lower() == self.rw.mailbox.lower() or mail.absender.upper().startswith("/O=")

    def _regel_passt(self, regel: Regel, mail: Mail) -> tuple[bool, str | None]:
        """Liefert (passt, zielordner_override)."""
        if regel.name == "Rest":
            return True, None
        beteiligte = [mail.absender, *mail.empfaenger, *mail.cc]
        treffer = False
        if regel.absender_oder_empfaenger and any(
            _adresse_passt(a, regel.absender_oder_empfaenger) for a in beteiligte
        ):
            treffer = True
        if regel.absender_domains and _domain_passt(mail.absender, regel.absender_domains):
            beansprucht = self._explizite_adressen.get(mail.absender.lower())
            if beansprucht is None or beansprucht == regel.name:
                treffer = True
        if regel.absender_muster and _muster_passt(mail, regel.absender_muster):
            treffer = True
        if regel.betreff_beginnt and any(
            mail.betreff.lower().lstrip().startswith(b.lower()) for b in regel.betreff_beginnt
        ):
            treffer = True
        zusatz = bool(regel.zusatz_absender and _adresse_passt(mail.absender, regel.zusatz_absender)) or bool(
            regel.zusatz_absender_domains and _domain_passt(mail.absender, regel.zusatz_absender_domains)
        )
        if regel.nur_lieferanten:
            absender_ok = self.ist_lieferant(mail) or zusatz
            if not absender_ok:
                return False, None
            if regel.stichworte and not zusatz and enthaelt_stichwort(mail.text, regel.stichworte) is None:
                return False, None
            treffer = True
        elif regel.stichworte and not treffer:
            # Regeln ohne Absenderbindung (z. B. Quintessenzio): Stichwort allein reicht
            treffer = enthaelt_stichwort(mail.text, regel.stichworte) is not None
        if not treffer and zusatz:
            treffer = True
        if not treffer:
            return False, None
        if regel.sonderordner and enthaelt_stichwort(
            mail.text, [regel.sonderordner.get("betreff_enthaelt", "")]
        ):
            return True, regel.sonderordner.get("ordner")
        return True, None

    def _ausnahme_loeschen(self, regel: Regel, mail: Mail) -> bool:
        if "Steuerberater" in regel.ausnahme_ordner:
            text = mail.text.lower()
            if any(a.lower() in text for a in self._steuerberater_adressen):
                return True
        return False

    # ------------------------------------------------------------------ Kern
    def klassifiziere(self, mail: Mail, *, behalten_ids: set[str] | None = None) -> Entscheidung:
        e = Entscheidung(mail_id=mail.id, regel="Rest", betreff=mail.betreff, absender=mail.absender)
        regel_treffer: Regel | None = None
        ziel_override: str | None = None
        for regel in self.rw.regeln:
            passt, override = self._regel_passt(regel, mail)
            if not passt:
                continue
            if regel.aktion == "soft_delete" and self._ausnahme_loeschen(regel, mail):
                e.hinweise.append(f"Regel '{regel.name}' übersprungen: Ausnahme Steuerberater.")
                continue
            regel_treffer, ziel_override = regel, override
            break

        if regel_treffer is None:
            regel_treffer = Regel(name="Rest", ordner="Posteingang")
        e.regel = regel_treffer.name

        # Zielordner laut Regel
        ziel: str | None = None
        kategorie = self.rw.kategorien.get(regel_treffer.kategorie) if regel_treffer.kategorie else None
        if regel_treffer.aktion == "soft_delete":
            e.aktion = "soft_delete"
        elif ziel_override:
            ziel = ziel_override
        elif kategorie:
            ziel = kategorie.ordner
            if kategorie.abwicklung_ordner and regel_treffer.abwicklung_stichworte:
                wort = enthaelt_stichwort(mail.text, regel_treffer.abwicklung_stichworte)
                if wort:
                    ziel = kategorie.abwicklung_ordner
                    e.hinweise.append(f"Abwicklungs-Stichwort „{wort}“ → {ziel}.")
        elif regel_treffer.ordner:
            ziel = regel_treffer.ordner
        if kategorie:
            e.kategorie, e.farbe = kategorie.name, kategorie.farbe

        # Vorhandene Farbe gewinnt (von Server-Regel oder JD gesetzt)
        farbe_ziel = self._ziel_aus_kategorien(mail, regel_treffer)
        if farbe_ziel and e.aktion != "soft_delete":
            kat_name, farbe_ordner = farbe_ziel
            if ziel and farbe_ordner != ziel and e.regel not in ("Rest",):
                e.hinweise.append(
                    f"Farbe „{kat_name}“ → {farbe_ordner}, Regel „{e.regel}“ → {ziel}. Farbe gewinnt."
                )
            ziel = farbe_ordner
            e.kategorie = kat_name
            e.farbe = self.rw.kategorien[kat_name].farbe

        if e.aktion != "soft_delete":
            if ziel and ziel != "Posteingang":
                e.ziel_ordner = ziel
                e.ziel_ordner_id = self.rw.ordner_id(ziel)
                e.aktion = "verschieben" if e.ziel_ordner_id else "ordner_fehlt"
                if e.aktion == "ordner_fehlt":
                    e.hinweise.append(f"Ordner „{ziel}“ hat noch keine ID – Mail bleibt im Posteingang.")
            else:
                e.aktion = "liegenlassen"

        if regel_treffer.hinweis and mail.domain in ("duenschede.eu",):
            e.hinweise.append(regel_treffer.hinweis)
        if not self.ist_lieferant(mail) and self._sieht_aus_wie_weingut(mail) and e.regel in ("Rest",):
            e.hinweise.append("Neuer Absender, sieht nach Weingut/Lieferant aus – Liste prüfen.")

        self._behalten_pruefen(mail, e, behalten_ids or set())
        e.antwort, e.antwort_grund = self._antwort_entscheiden(mail, e)
        return e

    def _ziel_aus_kategorien(self, mail: Mail, regel: Regel) -> tuple[str, str] | None:
        for kat_name in mail.kategorien:
            kat = self.rw.kategorien.get(kat_name)
            if not kat:
                continue
            ziel = kat.ordner
            if kat.abwicklung_ordner:
                woerter = next(
                    (r.abwicklung_stichworte for r in self.rw.regeln if r.kategorie == kat.name), []
                )
                if woerter and enthaelt_stichwort(mail.text, woerter):
                    ziel = kat.abwicklung_ordner
            return kat.name, ziel
        return None

    def _sieht_aus_wie_weingut(self, mail: Mail) -> bool:
        spuren = (
            "bodega",
            "cantina",
            "cantine",
            "chateau",
            "château",
            "domaine",
            "quinta",
            "weingut",
            "vinos",
            "vini",
            "wine",
            "cave",
            "azienda",
            "champagne",
            "adega",
        )
        kandidat = f"{mail.absender} {mail.absender_name}".lower()
        return any(s in kandidat for s in spuren) and mail.domain != self.rw.eigene_domain

    def _behalten_pruefen(self, mail: Mail, e: Entscheidung, behalten_ids: set[str]) -> None:
        if e.aktion not in ("verschieben", "soft_delete"):
            return
        if mail.id in behalten_ids or (mail.internet_message_id and mail.internet_message_id in behalten_ids):
            e.bleibt_grund = "ausdrücklich behalten (offener Punkt)"
            return
        if e.aktion == "soft_delete":
            return
        alter = mail.alter_tage(self.heute)
        jung = alter is None or alter <= self.rw.ausnahme_alter_tage
        if self.rw.ungelesen_bleibt and jung:
            if not mail.gelesen:
                e.bleibt_grund = "ungelesen"
                return
            if mail.markiert:
                e.bleibt_grund = "markiert"
                return
        if mail.antwort_auf_eigene:
            e.bleibt_grund = "Antwort auf deine Mail – wartet auf dich"
            return
        if _BEWERBUNG.search(mail.betreff):
            e.bleibt_grund = "Bewerbung (bleibt bis Freigabe)"

    def _antwort_entscheiden(self, mail: Mail, e: Entscheidung) -> tuple[Antwort, str]:
        absender = mail.absender.lower()
        lokal = absender.split("@", 1)[0]
        text = mail.text.lower()
        if e.aktion == "soft_delete":
            return "nein", "wird gelöscht"
        if self.ist_eigene(mail):
            return "nein", "eigene Mail"
        if not absender:
            return "nein", "kein Absender"
        if any(s in lokal for s in _SYSTEM_ABSENDER) or any(
            s in absender for s in ("mailer-daemon", "odspnotify")
        ):
            return "nein", "Systemabsender"
        if any(s in text for s in _ABWESENHEIT) or _EINLADUNG.match(mail.betreff.strip()):
            return "nein", "Abwesenheitsnotiz/Termin-Benachrichtigung"
        if _SPEDITEUR_AUTOMAT.search(mail.betreff):
            return "nein", "automatische Auftragsbestätigung"
        if e.regel.startswith("Privat"):
            if mail.domain == "duenschede.eu":
                return "pruefen", "Familie – nur bei echter Frage"
            return "nein", "privater Newsletter/Systemmail"
        if any(s in mail.vorschau.lower() for s in _NEWSLETTER_SPUREN):
            return "nein", "Newsletter"
        if e.kategorie == "Marketing und Promos" and enthaelt_stichwort(
            mail.text, ["Newsletter", "Mailing", "WeTransfer"]
        ):
            return "nein", "Newsletter/Mailing"
        empfaenger = [r.lower() for r in mail.empfaenger]
        direkt = self.rw.mailbox.lower() in empfaenger or not empfaenger
        if e.regel == "Steuerberater":
            return ("ja", "Steuerberater") if direkt else ("pruefen", "Steuerberater, nur in Kopie")
        if e.regel == "Kommissionen":
            return "ja", "Lieferant, Abrechnung"
        if e.regel in ("Allgemeine Kommunikation", "LEH-Kunden", "Quintessenzio"):
            return ("ja", "Partner fragt direkt") if direkt else ("pruefen", "nur in Kopie")
        if e.regel == "Marketing":
            return "pruefen", "Lieferanten-Marketing – nur bei konkreter Frage"
        if e.regel == "Spediteure":
            return ("ja", "Spediteur fragt") if direkt else ("pruefen", "Spediteur, nur in Kopie")
        if e.regel == "Bank und Versicherung":
            return "pruefen", "Bank/Versicherung – meist Information"
        if e.regel == "Intern":
            if _WEITERLEITUNG.match(mail.betreff):
                return "pruefen", "Weiterleitung von Kollege – Auftrag prüfen"
            return ("ja", "Kollege schreibt direkt") if direkt else ("pruefen", "intern, nur in Kopie")
        # Rest: Kunden, unbekannte Absender
        if mail.domain == self.rw.eigene_domain:
            return "pruefen", "intern (ohne Regel)"
        return ("ja", "externer Absender fragt direkt") if direkt else ("pruefen", "extern, nur in Kopie")


def klassifiziere_alle(
    mails: Iterable[Mail],
    regelwerk: Regelwerk,
    *,
    behalten_ids: set[str] | None = None,
    heute: datetime | None = None,
) -> list[Entscheidung]:
    k = Klassifikator(regelwerk, heute=heute)
    return [k.klassifiziere(m, behalten_ids=behalten_ids) for m in mails]
