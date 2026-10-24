"""Firmen-Ermittlung: zu welchem Lexware-Mandanten (``config.Firma``) gehört ein Dokument?

Reihenfolge (docs/ARCHITEKTUR.md, Abschnitt ``matching/firma.py``):

1. **Eingangsordner** – liegt die Quelldatei unterhalb von ``settings.eingang_ordner`` in einem
   Unterordner, der ``Firma.eingang_unterordner`` entspricht (Groß-/Kleinschreibung egal, auch
   tiefer verschachtelt), ist das die Firma; Quelle ``"eingang_ordner"``. Relative Pfade und
   Dateien außerhalb des Eingangsordners (z. B. aus ``archiv/<firma>/``) werden anhand der
   Ordnernamen im Pfad erkannt.
1b. **Firmen-Gedächtnis** (``config/firmen_gedaechtnis.yaml``, gelernte Antworten aus der Freigabe,
   :mod:`auftragsautomat.matching.firmenwahl`): Regel zu Kunde + Lieferant, sonst zu Kunde
   allgemein (Kundentext, Adresse und Alias-Namen aus dem Dokument gegen die gelernten Kurznamen)
   -> Quelle ``"gedaechtnis"`` plus ``Pruefhinweis(FIRMA_GELERNT, INFO)`` ("gelernt am … von …").
   Nur, wenn ``settings.firmenwahl.gedaechtnis_vor_katalog`` an ist; sonst wird das Gedächtnis
   erst befragt, wenn der Katalog (2b) fragen müsste.
2. **Kunden-Alias** (``kunden.yaml``) im Kundentext, in der Kundenadresse oder zuletzt im
   Rohtext -> Quelle ``"alias_kunde"``.
2b. **Firmenkatalog** (``config/firmenkatalog.yaml``, Kundengruppen laut Anwender): Muster im
   Kundentext/in der Adresse -> Kundengruppe und mögliche Firmen (``FirmaErgebnis.kundengruppe``).
   Genau eine Firma (eigenständiger LEH; Fachhandel mit Lieferant laut Katalog) -> Quelle
   ``"katalog"``. Mehrere Firmen (Region, Kette, Großkunde: WIK LEH oder WOE; Fachhandel mit
   unbekanntem Lieferanten) und ``settings.firmenwahl.fragen_wenn_unklar`` -> **Ende der Suche**
   mit ``FirmaErgebnis(None, None, [FIRMA_UNKLAR WARNUNG "Bitte Firma wählen (Kundengruppe …): A
   oder B"], kandidaten=firmen, kundengruppe)``; die Stammdaten könnten den Kunden in beiden
   Firmen führen, die Entscheidung trifft der Anwender (Geschäftsregel 17) und das Tool merkt sie
   sich. Ist ``fragen_wenn_unklar`` aus, geht es wie bisher weiter.
3. **Lieferanten-Alias** (``lieferanten.yaml``) im Lieferantentext, über die Produktmarken der
   Positionen oder zuletzt im Rohtext -> Quelle ``"alias_lieferant"``.
4. **Stammdaten** – Kundensuche (:class:`~auftragsautomat.matching.partner.KundenMatcher`) in
   den Stämmen aller Firmen (``stammdaten.laden.lade_alle`` oder ein übergebener Lader); genau
   eine Firma mit sicherem Treffer -> Quelle ``"stammdaten"``.
5. Zuletzt der im Dokument genannte Empfänger (``RohBestellung.empfaenger_firma_text``, z. B.
   "Wein-Import-Kreis LEH") gegen Name/Lexware-Firmenname -> Quelle ``"empfaenger"``. Der
   Empfänger dient außerdem als Zünglein an der Waage, wenn Aliase oder Stammdaten mehrere
   Firmen liefern.

Sonst ``FirmaErgebnis(None, None)``; den blockierenden ``Pruefhinweis(FIRMA_UNKLAR, FEHLER)``
erzeugt ``matching.pruefung`` (mit ``details["kandidaten"]`` und ``details["kundengruppe"]``, damit
die Freigabe-UI die Auswahl anbieten kann). Widersprechen sich Eingangsordner und Kunden-Alias,
gewinnt der Ordner, das Ergebnis enthält aber einen ``Pruefhinweis(FIRMA_UNKLAR, WARNUNG)`` mit
beiden Werten (ein Lieferanten-Alias ist keine Gegenprobe: dasselbe Weingut liefert oft an mehrere
Mandanten). Sind Aliase oder Stammdaten mehrdeutig (mehrere Firmen), steht das als WARNUNG mit den
Kandidaten im Ergebnis.

:class:`FirmaErgebnis` lässt sich wie das in der Architektur beschriebene Tupel entpacken:
``firma_id, quelle = ermittle_firma(...)``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from auftragsautomat.config import Settings
from auftragsautomat.matching.aliase import AliasTabellen, enthaelt_als_wort, normalisiere_name
from auftragsautomat.matching.firmenwahl import Firmengedaechtnis, Firmenkatalog, Klassifikation
from auftragsautomat.matching.partner import KundenMatcher
from auftragsautomat.models import (
    Firmenregel,
    Kundengruppe,
    MatchStatus,
    Pruefcode,
    Pruefhinweis,
    RohBestellung,
    Schweregrad,
)
from auftragsautomat.stammdaten import laden
from auftragsautomat.stammdaten.basis import Stammdaten

__all__ = [
    "QUELLE_ALIAS_KUNDE",
    "QUELLE_ALIAS_LIEFERANT",
    "QUELLE_EINGANG_ORDNER",
    "QUELLE_EMPFAENGER",
    "QUELLE_GEDAECHTNIS",
    "QUELLE_KATALOG",
    "QUELLE_MANUELL",
    "QUELLE_STAMMDATEN",
    "FirmaErgebnis",
    "StammdatenLader",
    "ermittle_firma",
    "firma_aus_empfaenger",
    "firma_aus_ordner",
    "hinweis_gelernt",
]

logger = logging.getLogger(__name__)

QUELLE_EINGANG_ORDNER = "eingang_ordner"
QUELLE_GEDAECHTNIS = "gedaechtnis"
QUELLE_ALIAS_KUNDE = "alias_kunde"
QUELLE_KATALOG = "katalog"
QUELLE_ALIAS_LIEFERANT = "alias_lieferant"
QUELLE_STAMMDATEN = "stammdaten"
QUELLE_EMPFAENGER = "empfaenger"
QUELLE_MANUELL = "manuell"

#: Lädt die Stammdaten aller Firmen: ``firma_id -> Stammdaten`` (Standard: ``laden.lade_alle``).
StammdatenLader = Callable[[Settings], Mapping[str, Stammdaten]]


@dataclass(frozen=True)
class FirmaErgebnis:
    """Ergebnis der Firmen-Ermittlung.

    ``firma_id``/``quelle`` sind ``None``, wenn keine Firma eindeutig ist. ``hinweise`` enthält
    Warnungen (Ordner widerspricht Alias, mehrdeutige Kandidaten, Katalog fragt) und die Info
    ``FIRMA_GELERNT``, ``kandidaten`` alle Firmen, die in Frage kamen (für die Auswahl in der
    Freigabe-UI), ``kundengruppe`` die Einstufung laut Firmenkatalog (``None`` = kein Muster).
    """

    firma_id: str | None
    quelle: str | None
    hinweise: list[Pruefhinweis] = field(default_factory=list)
    kandidaten: tuple[str, ...] = ()
    kundengruppe: Kundengruppe | None = None

    def __iter__(self) -> Iterator[str | None]:
        """Erlaubt ``firma_id, quelle = ermittle_firma(...)`` (Tupel-Vertrag aus docs/ARCHITEKTUR.md)."""
        return iter((self.firma_id, self.quelle))

    @property
    def gefunden(self) -> bool:
        return self.firma_id is not None


# ---------------------------------------------------------------------------
# 1. Eingangsordner
# ---------------------------------------------------------------------------


def _norm_ordner(name: str) -> tuple[str, ...]:
    """Unterordner-Angabe -> Tupel normalisierter Pfadteile ("LEH/wik" -> ("leh", "wik"))."""
    teile = name.replace("\\", "/").split("/")
    return tuple(t.strip().casefold() for t in teile if t.strip())


def _unterordner_zuordnung(settings: Settings) -> dict[tuple[str, ...], str]:
    zuordnung: dict[tuple[str, ...], str] = {}
    for firma in settings.firmen.values():
        if not firma.eingang_unterordner:
            continue
        schluessel = _norm_ordner(firma.eingang_unterordner)
        if not schluessel:
            continue
        vorhanden = zuordnung.get(schluessel)
        if vorhanden is not None and vorhanden != firma.id:
            logger.warning(
                "Eingangs-Unterordner %r ist den Firmen %s und %s zugeordnet; %s gewinnt",
                firma.eingang_unterordner,
                vorhanden,
                firma.id,
                vorhanden,
            )
            continue
        zuordnung[schluessel] = firma.id
    return zuordnung


def _relativ_zum_eingang(datei: Path, eingang: Path) -> tuple[str, ...] | None:
    """Verzeichnisteile der Datei relativ zum Eingangsordner oder None, wenn sie nicht darunter liegt."""
    for a, b in ((datei, eingang), (_sicher_aufloesen(datei), _sicher_aufloesen(eingang))):
        try:
            relativ = a.relative_to(b)
        except ValueError:
            continue
        return tuple(t.casefold() for t in relativ.parent.parts)
    return None


def _sicher_aufloesen(pfad: Path) -> Path:
    try:
        return pfad.resolve()
    except (OSError, RuntimeError):
        return pfad


def _firma_am_anfang(teile: tuple[str, ...], zuordnung: dict[tuple[str, ...], str]) -> str | None:
    for schluessel, firma_id in sorted(zuordnung.items(), key=lambda e: -len(e[0])):
        if teile[: len(schluessel)] == schluessel:
            return firma_id
    return None


def _firma_irgendwo(teile: tuple[str, ...], zuordnung: dict[tuple[str, ...], str]) -> str | None:
    for schluessel, firma_id in sorted(zuordnung.items(), key=lambda e: -len(e[0])):
        n = len(schluessel)
        for i in range(len(teile) - n, -1, -1):  # von hinten: der nächste Ordner an der Datei zuerst
            if teile[i : i + n] == schluessel:
                return firma_id
    return None


def firma_aus_ordner(quelldatei: Path | str | None, settings: Settings) -> str | None:
    """Firma anhand des Unterordners der Quelldatei (``Firma.eingang_unterordner``).

    Zuerst relativ zu ``settings.eingang_ordner``, dann anhand eines Pfadteils mit dem Namen des
    Eingangsordners (relative Pfade wie ``eingang/wik_leh/x.pdf``), zuletzt anhand eines beliebigen
    Verzeichnisnamens im Pfad (z. B. ``archiv/wik_leh/2026/x.pdf``). ``None``, wenn nichts passt.
    """
    if not quelldatei:
        return None
    zuordnung = _unterordner_zuordnung(settings)
    if not zuordnung:
        logger.debug("Keine Firma hat einen eingang_unterordner konfiguriert")
        return None
    datei = Path(quelldatei)
    verzeichnisse = tuple(t.casefold() for t in datei.parent.parts)

    relativ = _relativ_zum_eingang(datei, Path(settings.eingang_ordner))
    if relativ is not None:
        firma_id = _firma_am_anfang(relativ, zuordnung)
        if firma_id:
            logger.info("Firma %s aus dem Eingangs-Unterordner von %s", firma_id, datei)
            return firma_id
        logger.debug("Datei %s liegt im Eingangsordner, aber in keinem Firmen-Unterordner", datei)
        return None

    eingang_name = Path(settings.eingang_ordner).name.casefold()
    for i, teil in enumerate(verzeichnisse):
        if teil == eingang_name:
            firma_id = _firma_am_anfang(verzeichnisse[i + 1 :], zuordnung)
            if firma_id:
                logger.info("Firma %s aus dem Pfad %s (Ordner '%s')", firma_id, datei, eingang_name)
                return firma_id

    firma_id = _firma_irgendwo(verzeichnisse, zuordnung)
    if firma_id:
        logger.info(
            "Firma %s aus einem Ordnernamen im Pfad %s (außerhalb des Eingangsordners)", firma_id, datei
        )
    return firma_id


# ---------------------------------------------------------------------------
# 2./3. Aliase
# ---------------------------------------------------------------------------


def _bekannte_firmen(firmen: list[str], settings: Settings, art: str) -> list[str]:
    bekannt: list[str] = []
    for firma_id in firmen:
        if firma_id in settings.firmen:
            if firma_id not in bekannt:
                bekannt.append(firma_id)
        else:
            logger.warning("%s-Alias verweist auf unbekannte Firma %r (nicht in firmen.yaml)", art, firma_id)
    return bekannt


def _firmen_aus_kunden_alias(
    roh: RohBestellung, aliase: AliasTabellen, settings: Settings
) -> tuple[list[str], str]:
    """Firmen, auf die ein Kunden-Alias im Dokument zeigt, und der getroffene Alias."""
    for text, woher in (
        (roh.kunde_text, "Kundentext"),
        (roh.kunde_adresse, "Kundenadresse"),
        (roh.rohtext, "Rohtext"),
    ):
        treffer = aliase.kunden_treffer(text)
        if not treffer:
            continue
        firmen = _bekannte_firmen([t.firma_id for t in treffer], settings, "Kunden")
        if firmen:
            logger.debug("Kunden-Alias %r im %s -> Firmen %s", treffer[0].alias, woher, firmen)
            return firmen, treffer[0].alias
    return [], ""


def _firmen_aus_lieferanten_alias(
    roh: RohBestellung, aliase: AliasTabellen, settings: Settings
) -> tuple[list[str], str]:
    """Firmen, auf die ein Lieferanten-Alias (Text) oder eine Produktmarke (Positionen) zeigt."""
    treffer = aliase.lieferanten_treffer(roh.lieferant_text)
    if treffer:
        firmen = _bekannte_firmen([t.firma_id for t in treffer], settings, "Lieferanten")
        if firmen:
            return firmen, treffer[0].alias

    firmen: list[str] = []
    marke = ""
    for position in roh.positionen:
        if position.durchgestrichen:
            continue
        marken_treffer = aliase.lieferant_fuer_marke(position.bezeichnung)
        if marken_treffer is None:
            continue
        marke = marke or marken_treffer.alias
        for firma_id in _bekannte_firmen([marken_treffer.firma_id], settings, "Produktmarken"):
            if firma_id not in firmen:
                firmen.append(firma_id)
    if firmen:
        return firmen, marke

    treffer = aliase.lieferanten_treffer(roh.rohtext)
    if treffer:
        firmen = _bekannte_firmen([t.firma_id for t in treffer], settings, "Lieferanten")
        if firmen:
            return firmen, treffer[0].alias
    return [], ""


# ---------------------------------------------------------------------------
# 4. Stammdaten
# ---------------------------------------------------------------------------


def _firmen_aus_stammdaten(
    roh: RohBestellung, settings: Settings, aliase: AliasTabellen, lader: StammdatenLader
) -> list[tuple[str, str]]:
    """(firma_id, kundennummer) je Firma mit sicherem Kundentreffer."""
    if not (roh.kunde_text or roh.kunde_adresse or roh.kundennummer_dokument):
        logger.info("Kein Kundentext im Dokument; Stammdaten-Suche nach der Firma entfällt")
        return []
    alle = lader(settings)
    treffer: list[tuple[str, str]] = []
    for firma_id, stammdaten in alle.items():
        matcher = KundenMatcher(stammdaten, aliase, settings.matching)
        zuordnung = matcher.finde(roh.kunde_text, roh.kunde_adresse, kundennummer=roh.kundennummer_dokument)
        if zuordnung.status == MatchStatus.SICHER and zuordnung.kunde is not None:
            treffer.append((firma_id, zuordnung.kunde.kundennummer))
    return treffer


# ---------------------------------------------------------------------------
# 5. Empfänger
# ---------------------------------------------------------------------------


def firma_aus_empfaenger(
    text: str | None, settings: Settings, kandidaten: list[str] | tuple[str, ...] | None = None
) -> str | None:
    """Firma, deren Name, Lexware-Firmenname oder ID im Empfängertext vorkommt (eindeutig, sonst None).

    Mit ``kandidaten`` wird nur unter diesen Firmen gesucht (Zünglein an der Waage bei Mehrdeutigkeit).
    """
    text_norm = normalisiere_name(text)
    if not text_norm:
        return None
    gefunden: list[str] = []
    for firma in settings.firmen.values():
        if kandidaten is not None and firma.id not in kandidaten:
            continue
        for name in (firma.lexware_firmenname, firma.name, firma.id):
            name_norm = normalisiere_name(name)
            if name_norm and (name_norm == text_norm or enthaelt_als_wort(text_norm, name_norm)):
                if firma.id not in gefunden:
                    gefunden.append(firma.id)
                break
    if len(gefunden) == 1:
        return gefunden[0]
    if len(gefunden) > 1:
        logger.info("Empfänger %r passt zu mehreren Firmen: %s", text, ", ".join(gefunden))
    return None


# ---------------------------------------------------------------------------
# 1b. Gedächtnis / 2b. Katalog
# ---------------------------------------------------------------------------


def _kunden_texte(roh: RohBestellung, aliase: AliasTabellen) -> list[str]:
    """Kandidatentexte für die Gedächtnis-Suche: Kundentext, Adresse, Anzeigename/Alias der Kunden-Aliase."""
    texte: list[str] = []
    for text in (roh.kunde_text, roh.kunde_adresse):
        if text and text.strip():
            texte.append(text)
    for quelle in (roh.kunde_text, roh.kunde_adresse):
        for treffer in aliase.kunden_treffer(quelle):
            texte.extend(t for t in (treffer.anzeigename, treffer.alias) if t)
    return list(dict.fromkeys(texte))


def _lieferanten_texte(roh: RohBestellung, aliase: AliasTabellen) -> list[str]:
    """Kandidatentexte für den Lieferanten: Lieferantentext, Alias-Namen, Produktmarken der Positionen."""
    texte: list[str] = []
    if roh.lieferant_text and roh.lieferant_text.strip():
        texte.append(roh.lieferant_text)
    for treffer in aliase.lieferanten_treffer(roh.lieferant_text):
        texte.extend(t for t in (treffer.anzeigename, treffer.alias) if t)
    for position in roh.positionen:
        if position.durchgestrichen:
            continue
        marke = aliase.lieferant_fuer_marke(position.bezeichnung)
        if marke is not None:
            texte.extend(t for t in (marke.anzeigename, marke.alias) if t)
    return list(dict.fromkeys(texte))


def hinweis_gelernt(regel: Firmenregel) -> Pruefhinweis:
    """``Pruefhinweis(FIRMA_GELERNT, INFO)`` zu einer Regel aus dem Firmen-Gedächtnis."""
    gelernt_am = regel.gelernt_am.strftime("%d.%m.%Y %H:%M") if regel.gelernt_am else ""
    return Pruefhinweis(
        code=Pruefcode.FIRMA_GELERNT,
        schweregrad=Schweregrad.INFO,
        text=(
            f"Firma {regel.firma_id} aus dem Firmen-Gedächtnis: gelernt am {gelernt_am or '-'} von "
            f"{regel.gelernt_von or '-'} (Kunde '{regel.kunde_kurz}', Lieferant "
            f"'{regel.lieferant_kurz or 'alle'}')"
        ),
        details={
            "firma_id": regel.firma_id,
            "gelernt_am": gelernt_am,
            "gelernt_von": regel.gelernt_von or "",
            "kunde": regel.kunde_kurz,
            "lieferant": regel.lieferant_kurz or "",
            "kundengruppe": regel.kundengruppe.value if regel.kundengruppe else "",
            "quelle": regel.quelle,
        },
    )


def _firma_aus_gedaechtnis(
    gedaechtnis: Firmengedaechtnis, kunden_texte: list[str], lieferanten_texte: list[str], settings: Settings
) -> Firmenregel | None:
    if not len(gedaechtnis) or not kunden_texte:
        return None
    regel = gedaechtnis.finde_fuer_texte(kunden_texte, lieferanten_texte)
    if regel is None:
        return None
    if regel.firma_id not in settings.firmen:
        logger.warning(
            "Firmen-Gedächtnis: Regel für Kunde %r verweist auf unbekannte Firma %r; ignoriert",
            regel.kunde_kurz,
            regel.firma_id,
        )
        return None
    logger.info(
        "Firma %s aus dem Firmen-Gedächtnis (Kunde %r, Lieferant %r, gelernt am %s von %s)",
        regel.firma_id,
        regel.kunde_kurz,
        regel.lieferant_kurz or "alle",
        regel.gelernt_am.strftime("%d.%m.%Y") if regel.gelernt_am else "-",
        regel.gelernt_von or "-",
    )
    return regel


def _klassifiziere(
    katalog: Firmenkatalog, roh: RohBestellung, lieferanten_texte: list[str], firma_kandidaten: list[str]
) -> Klassifikation:
    lieferant_text = next((t for t in lieferanten_texte if t), None)
    return katalog.klassifiziere(
        roh.kunde_text,
        roh.kunde_adresse,
        roh.kundennummer_dokument,
        lieferant_text=lieferant_text,
        firma_kandidaten=firma_kandidaten or None,
    )


def _firma_aus_lieferanten_katalog(
    katalog: Firmenkatalog, lieferanten_texte: list[str], firmen: list[str]
) -> str | None:
    """Fachhandel: erste Firma laut ``lieferanten``-Abschnitt, die zu den Firmen der Gruppe gehört."""
    for text in lieferanten_texte:
        firma = katalog.firma_fuer_lieferant(text)
        if firma is not None and firma in firmen:
            return firma
    return None


def _frage(klassifikation: Klassifikation, firmen: list[str]) -> Pruefhinweis:
    return _warnung(
        f"Bitte Firma wählen (Kundengruppe {klassifikation.kundengruppe.value}): {' oder '.join(firmen)}",
        kandidaten=", ".join(firmen),
        kundengruppe=klassifikation.kundengruppe.value,
        grund=klassifikation.grund,
    )


# ---------------------------------------------------------------------------
# Hauptfunktion
# ---------------------------------------------------------------------------


def _warnung(text: str, **details: str) -> Pruefhinweis:
    return Pruefhinweis(
        code=Pruefcode.FIRMA_UNKLAR, schweregrad=Schweregrad.WARNUNG, text=text, details=details
    )


def _eindeutig(
    firmen: list[str], roh: RohBestellung, settings: Settings, art: str, hinweise: list[Pruefhinweis]
) -> str | None:
    """Genau eine Firma -> diese; mehrere -> Empfänger entscheidet, sonst WARNUNG und None."""
    if not firmen:
        return None
    if len(firmen) == 1:
        return firmen[0]
    per_empfaenger = firma_aus_empfaenger(roh.empfaenger_firma_text, settings, firmen)
    if per_empfaenger:
        logger.info(
            "%s mehrdeutig (%s); Empfänger %r entscheidet für %s",
            art,
            ", ".join(firmen),
            roh.empfaenger_firma_text,
            per_empfaenger,
        )
        return per_empfaenger
    logger.warning("%s passt zu mehreren Firmen: %s", art, ", ".join(firmen))
    hinweise.append(
        _warnung(
            f"Firma nicht eindeutig: {art} passt zu mehreren Firmen ({', '.join(firmen)})",
            kandidaten=", ".join(firmen),
        )
    )
    return None


def ermittle_firma(
    roh: RohBestellung,
    settings: Settings,
    aliase: AliasTabellen,
    *,
    stammdaten_lader: StammdatenLader | None = None,
    datei: Path | None = None,
    katalog: Firmenkatalog | None = None,
    gedaechtnis: Firmengedaechtnis | None = None,
) -> FirmaErgebnis:
    """Ermittelt die Firma (Lexware-Mandant) zu einer Rohbestellung (Reihenfolge siehe Modul-Docstring).

    ``stammdaten_lader`` ersetzt ``stammdaten.laden.lade_alle`` (für Tests oder bereits geladene
    Stämme); ``datei`` überschreibt ``roh.quelldatei`` für die Ordner-Erkennung. ``katalog`` und
    ``gedaechtnis`` ersetzen das Laden aus ``settings.firmenwahl`` (Katalog- und Gedächtnisdatei).
    """
    hinweise: list[Pruefhinweis] = []
    kandidaten: list[str] = []
    quelldatei = datei if datei is not None else roh.quelldatei
    wahl = settings.firmenwahl
    katalog = katalog if katalog is not None else Firmenkatalog.aus_settings(settings)
    gedaechtnis = gedaechtnis if gedaechtnis is not None else Firmengedaechtnis.aus_settings(settings)
    kunden_texte = _kunden_texte(roh, aliase)
    lieferanten_texte = _lieferanten_texte(roh, aliase)

    # 1. Eingangsordner (Gegenprobe nur mit dem Kunden-Alias: Lieferanten liegen oft in mehreren Mandanten)
    firma_ordner = firma_aus_ordner(quelldatei, settings)
    firmen_kunde, alias_kunde = _firmen_aus_kunden_alias(roh, aliase, settings)
    # Katalog-Einstufung (Kundengruppe) wird in jedem Fall mitgegeben; mehrdeutige Kunden-Aliase
    # schränken die Firmen der Gruppe ein.
    klassifikation = _klassifiziere(katalog, roh, lieferanten_texte, firmen_kunde if len(firmen_kunde) > 1 else [])
    kundengruppe = None if klassifikation.kundengruppe is Kundengruppe.UNBEKANNT else klassifikation.kundengruppe
    if firma_ordner:
        kandidaten.append(firma_ordner)
        if firmen_kunde and firma_ordner not in firmen_kunde:
            kandidaten.extend(f for f in firmen_kunde if f not in kandidaten)
            firma_alias = ", ".join(firmen_kunde)
            logger.warning(
                "Eingangsordner sagt Firma %s, Kunden-Alias '%s' sagt %s; der Ordner gewinnt",
                firma_ordner,
                alias_kunde,
                firma_alias,
            )
            hinweise.append(
                _warnung(
                    f"Firma aus dem Eingangsordner ({firma_ordner}) widerspricht dem Kunden-Alias "
                    f"'{alias_kunde}' ({firma_alias}); der Ordner gewinnt, bitte prüfen",
                    firma_ordner=firma_ordner,
                    firma_alias=firma_alias,
                    alias=alias_kunde,
                    alias_quelle=QUELLE_ALIAS_KUNDE,
                )
            )
        return FirmaErgebnis(firma_ordner, QUELLE_EINGANG_ORDNER, hinweise, tuple(kandidaten), kundengruppe)

    # 1b. Firmen-Gedächtnis (gelernte Antwort schlägt Alias und Katalog)
    if wahl.gedaechtnis_vor_katalog:
        regel = _firma_aus_gedaechtnis(gedaechtnis, kunden_texte, lieferanten_texte, settings)
        if regel is not None:
            hinweise.append(hinweis_gelernt(regel))
            if regel.firma_id not in kandidaten:
                kandidaten.append(regel.firma_id)
            return FirmaErgebnis(
                regel.firma_id, QUELLE_GEDAECHTNIS, hinweise, tuple(kandidaten), regel.kundengruppe or kundengruppe
            )

    # 2. Kunden-Alias
    kandidaten.extend(f for f in firmen_kunde if f not in kandidaten)
    firma_kunde = _eindeutig(firmen_kunde, roh, settings, f"Kunden-Alias '{alias_kunde}'", hinweise)
    if firma_kunde:
        logger.info("Firma %s über Kunden-Alias '%s'", firma_kunde, alias_kunde)
        return FirmaErgebnis(firma_kunde, QUELLE_ALIAS_KUNDE, hinweise, tuple(kandidaten), kundengruppe)

    # 2b. Firmenkatalog (Kundengruppe -> Firma(en))
    if klassifikation.entscheidung != "unbekannt":
        firmen_katalog = [f for f in klassifikation.firmen if f in settings.firmen]
        firma_katalog: str | None = None
        if klassifikation.entscheidung == "eindeutig" and firmen_katalog:
            firma_katalog = firmen_katalog[0]
        elif klassifikation.entscheidung == "lieferant":
            firma_katalog = _firma_aus_lieferanten_katalog(katalog, lieferanten_texte, firmen_katalog)
            if firma_katalog is None:
                logger.info(
                    "Firmenkatalog: Kundengruppe %s entscheidet nach Lieferant, aber kein Lieferant im Katalog (%s)",
                    klassifikation.kundengruppe.value,
                    ", ".join(lieferanten_texte) or "kein Lieferantentext",
                )
        if firma_katalog:
            logger.info("Firma %s über den Firmenkatalog (%s)", firma_katalog, klassifikation.grund)
            if firma_katalog not in kandidaten:
                kandidaten.append(firma_katalog)
            return FirmaErgebnis(firma_katalog, QUELLE_KATALOG, hinweise, tuple(kandidaten), kundengruppe)

        kandidaten.extend(f for f in firmen_katalog if f not in kandidaten)
        if len(firmen_katalog) >= 2:
            if not wahl.gedaechtnis_vor_katalog:
                regel = _firma_aus_gedaechtnis(gedaechtnis, kunden_texte, lieferanten_texte, settings)
                if regel is not None:
                    hinweise.append(hinweis_gelernt(regel))
                    if regel.firma_id not in kandidaten:
                        kandidaten.append(regel.firma_id)
                    return FirmaErgebnis(
                        regel.firma_id,
                        QUELLE_GEDAECHTNIS,
                        hinweise,
                        tuple(kandidaten),
                        regel.kundengruppe or kundengruppe,
                    )
            if wahl.fragen_wenn_unklar:
                logger.warning(
                    "Firma für %s nicht automatisch wählbar (%s); Anwender muss zwischen %s wählen",
                    quelldatei,
                    klassifikation.grund,
                    " und ".join(firmen_katalog),
                )
                hinweise.append(_frage(klassifikation, firmen_katalog))
                return FirmaErgebnis(None, None, hinweise, tuple(kandidaten), kundengruppe)
            logger.info(
                "Firmenkatalog: %s; fragen_wenn_unklar ist aus, Suche geht mit Stammdaten/Empfänger weiter",
                klassifikation.grund,
            )

    # 3. Lieferanten-Alias / Produktmarke (nur, wenn kein Kunden-Alias getroffen hat)
    if not firmen_kunde:
        firmen_lieferant, alias_lieferant = _firmen_aus_lieferanten_alias(roh, aliase, settings)
        kandidaten.extend(f for f in firmen_lieferant if f not in kandidaten)
        firma_lieferant = _eindeutig(
            firmen_lieferant, roh, settings, f"Lieferanten-Alias/Produktmarke '{alias_lieferant}'", hinweise
        )
        if firma_lieferant:
            logger.info("Firma %s über Lieferanten-Alias/Produktmarke '%s'", firma_lieferant, alias_lieferant)
            return FirmaErgebnis(firma_lieferant, QUELLE_ALIAS_LIEFERANT, hinweise, tuple(kandidaten), kundengruppe)

    # 4. Stammdaten aller Firmen
    lader = stammdaten_lader if stammdaten_lader is not None else laden.lade_alle
    treffer = _firmen_aus_stammdaten(roh, settings, aliase, lader)
    firmen_stamm = [f for f, _ in treffer]
    for f in firmen_stamm:
        if f not in kandidaten:
            kandidaten.append(f)
    beschreibung = "Kunde " + ", ".join(f"{nr} in {f}" for f, nr in treffer) if treffer else "Kundensuche"
    firma_stamm = _eindeutig(firmen_stamm, roh, settings, beschreibung, hinweise)
    if firma_stamm:
        nummer = next(nr for f, nr in treffer if f == firma_stamm)
        logger.info("Firma %s über die Stammdaten (Kunde %s sicher)", firma_stamm, nummer)
        return FirmaErgebnis(firma_stamm, QUELLE_STAMMDATEN, hinweise, tuple(kandidaten), kundengruppe)

    # 5. Empfänger im Dokument
    firma_empfaenger = firma_aus_empfaenger(roh.empfaenger_firma_text, settings)
    if firma_empfaenger:
        logger.info("Firma %s über den Empfänger %r im Dokument", firma_empfaenger, roh.empfaenger_firma_text)
        if firma_empfaenger not in kandidaten:
            kandidaten.append(firma_empfaenger)
        return FirmaErgebnis(firma_empfaenger, QUELLE_EMPFAENGER, hinweise, tuple(kandidaten), kundengruppe)

    logger.warning(
        "Keine Firma für %s ermittelbar (Kunde %r, Lieferant %r)%s",
        quelldatei,
        roh.kunde_text,
        roh.lieferant_text,
        f"; Kandidaten: {', '.join(kandidaten)}" if kandidaten else "",
    )
    return FirmaErgebnis(None, None, hinweise, tuple(kandidaten), kundengruppe)
