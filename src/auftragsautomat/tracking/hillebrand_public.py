"""Stufe 2b (optional, ohne Login): öffentliche Track&Trace-Seite von Hillebrand Gori.

``https://my.hillebrandgori.com/public-tracking/<Suchbegriff>`` (``public_tracking_url`` in
``config/tracking_hillebrand.yaml``) zeigt für Sendungsreferenz, B/L- oder Containernummer eine
Timeline mit Status, Herkunft und Ziel – ohne Dokumente und ohne Benachrichtigungen. Die Seite ist
eine Angular-App hinter Cloudflare; ein Abruf ohne echten Browser liefert eine Challenge-Seite
("Just a moment…"). Deshalb lädt der Standard-Lader die Seite mit Playwright (Chromium, lazy
import), wartet auf die Timeline und speichert einen Screenshot nach
``settings.tracking.screenshots_ordner``. Tests injizieren einen Lader ``lade_seite(url) -> html``.

Das HTML wird mit ``html.parser`` aus der Standardbibliothek gelesen (BeautifulSoup ist nicht
installiert); die Selektoren (einfaches CSS: Tag, ``.klasse``, ``#id``, ``[attr=wert]``,
Nachfahren per Leerzeichen, Kinder per ``>``) und Regex-Muster kommen aus der YAML
(``selektoren.public``). Cloudflare-Challenge oder Login-Seite -> Sendung unverändert + Hinweis.
"""

from __future__ import annotations

import importlib.util
import logging
import re
from collections.abc import Callable
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Literal

from auftragsautomat.config import TrackingKonfig
from auftragsautomat.models import Meilenstein, Sendung, SendungStatus
from auftragsautomat.tracking.basis import (
    HillebrandKonfig,
    SelektorSatz,
    finde_datum_tolerant,
    ist_bekannt,
    jetzt,
    kompiliere,
    lade_hillebrand_konfig,
    mit_hinweis,
    status_aus_meilensteinen,
    status_mit_puenktlichkeit,
)

logger = logging.getLogger(__name__)

__all__ = [
    "Knoten",
    "LaderFehler",
    "PublicTrackingQuelle",
    "SeitenTyp",
    "erkenne_seitentyp",
    "parse_html",
    "parse_public_tracking_html",
    "playwright_verfuegbar",
    "waehle",
]

SeitenTyp = Literal["daten", "challenge", "login", "leer"]
LadeSeite = Callable[[str], str]


class LaderFehler(RuntimeError):
    """Die Seite konnte nicht geladen werden (Playwright fehlt, Timeout, Browserfehler)."""


def playwright_verfuegbar() -> bool:
    """Ist das Paket ``playwright`` installiert (ohne es zu importieren)?"""
    try:
        return importlib.util.find_spec("playwright") is not None
    except (ImportError, ValueError):
        return False


# ---------------------------------------------------------------------------
# Kleiner DOM mit CSS-Auswahl (Standardbibliothek)
# ---------------------------------------------------------------------------

_LEERE_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
_BLOCK_TAGS = {
    "p", "div", "li", "tr", "br", "h1", "h2", "h3", "h4", "h5", "h6", "table", "ul", "ol", "section",
    "article", "header", "footer", "dd", "dt", "dl", "blockquote", "pre", "hr",
}  # fmt: skip


class Knoten:
    """Ein Element des HTML-Baums (Tag, Attribute, Kinder, Textteile)."""

    __slots__ = ("attrs", "eltern", "kinder", "tag", "texte")

    def __init__(self, tag: str, attrs: dict[str, str] | None = None, eltern: Knoten | None = None) -> None:
        self.tag = tag.lower()
        self.attrs = attrs or {}
        self.eltern = eltern
        self.kinder: list[Knoten | str] = []

    @property
    def id(self) -> str | None:
        return self.attrs.get("id")

    @property
    def klassen(self) -> set[str]:
        return set((self.attrs.get("class") or "").split())

    def elemente(self) -> list[Knoten]:
        return [k for k in self.kinder if isinstance(k, Knoten)]

    def alle(self) -> list[Knoten]:
        """Alle Nachfahren in Dokumentreihenfolge (ohne den Knoten selbst)."""
        ergebnis: list[Knoten] = []
        stapel = list(reversed(self.elemente()))
        while stapel:
            k = stapel.pop()
            ergebnis.append(k)
            stapel.extend(reversed(k.elemente()))
        return ergebnis

    def text(self) -> str:
        """Sichtbarer Text; Blockelemente erzeugen Zeilenumbrüche, Leerraum wird normalisiert."""
        teile: list[str] = []
        self._sammle(teile)
        roh = "".join(teile).replace("\xa0", " ")
        zeilen = [re.sub(r"[ \t\r\f\v]+", " ", z).strip() for z in roh.split("\n")]
        return re.sub(r"\n{2,}", "\n", "\n".join(zeilen)).strip()

    def _sammle(self, teile: list[str]) -> None:
        if self.tag in ("script", "style", "template", "noscript"):
            return
        block = self.tag in _BLOCK_TAGS
        if block:
            teile.append("\n")
        for k in self.kinder:
            if isinstance(k, str):
                teile.append(k)
            else:
                k._sammle(teile)
        if block:
            teile.append("\n")
        elif self.tag == "td" or self.tag == "th":
            teile.append(" ")

    def __repr__(self) -> str:  # pragma: no cover - nur für Debugging
        return f"<Knoten {self.tag} {self.attrs}>"


# Elemente, die ein Browser implizit schließt, wenn ein gleichartiges Start-Tag folgt
# (Schlüssel = neues Tag, Wert = (zu schließende Tags, Grenzen, an denen die Suche endet)).
_IMPLIZIT_SCHLIESSEN: dict[str, tuple[set[str], set[str]]] = {
    "li": ({"li"}, {"ul", "ol", "menu"}),
    "p": ({"p"}, {"div", "section", "article", "body", "li", "td", "th", "blockquote"}),
    "tr": ({"tr", "td", "th"}, {"table", "tbody", "thead", "tfoot"}),
    "td": ({"td", "th"}, {"tr", "table"}),
    "th": ({"td", "th"}, {"tr", "table"}),
    "dt": ({"dt", "dd"}, {"dl"}),
    "dd": ({"dt", "dd"}, {"dl"}),
    "option": ({"option"}, {"select", "datalist", "optgroup"}),
}


class _DomBauer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.wurzel = Knoten("#dokument")
        self._aktuell = self.wurzel

    def _schliesse_implizit(self, tag: str) -> None:
        regel = _IMPLIZIT_SCHLIESSEN.get(tag)
        if regel is None:
            return
        zu_schliessen, grenzen = regel
        knoten: Knoten | None = self._aktuell
        while knoten is not None and knoten is not self.wurzel:
            if knoten.tag in grenzen:
                return
            if knoten.tag in zu_schliessen:
                self._aktuell = knoten.eltern or self.wurzel
                return
            knoten = knoten.eltern

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._schliesse_implizit(tag.lower())
        knoten = Knoten(tag, {k.lower(): (v or "") for k, v in attrs}, self._aktuell)
        self._aktuell.kinder.append(knoten)
        if tag.lower() not in _LEERE_TAGS:
            self._aktuell = knoten

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._aktuell.kinder.append(Knoten(tag, {k.lower(): (v or "") for k, v in attrs}, self._aktuell))

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _LEERE_TAGS:
            return
        knoten: Knoten | None = self._aktuell
        while knoten is not None and knoten is not self.wurzel:
            if knoten.tag == tag:
                self._aktuell = knoten.eltern or self.wurzel
                return
            knoten = knoten.eltern
        # unpassendes End-Tag: ignorieren (tolerant wie Browser)

    def handle_data(self, data: str) -> None:
        if data:
            self._aktuell.kinder.append(data)


def parse_html(html: str | None) -> Knoten:
    """Baut aus HTML einen kleinen Baum (tolerant gegenüber fehlenden End-Tags)."""
    bauer = _DomBauer()
    bauer.feed(html or "")
    bauer.close()
    return bauer.wurzel


_EINFACH_RE = re.compile(
    r"^(?P<tag>[a-zA-Z][\w-]*|\*)?(?P<rest>(?:[.#][\w-]+|\[[^\]]+\])*)$",
)
_TEIL_RE = re.compile(r"[.#][\w-]+|\[[^\]]+\]")


def _passt_einfach(knoten: Knoten, einfach: str) -> bool:
    treffer = _EINFACH_RE.match(einfach)
    if treffer is None:
        raise ValueError(f"Selektor nicht unterstützt: {einfach!r}")
    tag = treffer.group("tag")
    if tag and tag != "*" and knoten.tag != tag.lower():
        return False
    for teil in _TEIL_RE.findall(treffer.group("rest") or ""):
        if teil.startswith("."):
            if teil[1:] not in knoten.klassen:
                return False
        elif teil.startswith("#"):
            if knoten.id != teil[1:]:
                return False
        else:
            inhalt = teil[1:-1].strip()
            if "=" in inhalt:
                name, wert = inhalt.split("=", 1)
                wert = wert.strip().strip("\"'")
                if knoten.attrs.get(name.strip().lower()) != wert:
                    return False
            elif inhalt.lower() not in knoten.attrs:
                return False
    return True


def _tokenisiere(selektor: str) -> list[tuple[str, str]]:
    """"a > b c" -> [("", "a"), (">", "b"), (" ", "c")]."""
    teile = re.split(r"\s*(>)\s*|\s+", selektor.strip())
    tokens: list[tuple[str, str]] = []
    kombinator = ""
    for teil in teile:
        if teil is None or teil == "":
            continue
        if teil == ">":
            kombinator = ">"
            continue
        tokens.append((kombinator, teil))
        kombinator = " "
    return tokens


def waehle(wurzel: Knoten, selektor: str | None) -> list[Knoten]:
    """Elemente, die auf den CSS-Selektor passen (Dokumentreihenfolge, ohne Doppelte).

    Unterstützt: Tag, ``.klasse``, ``#id``, ``[attr]``, ``[attr=wert]``, Verkettung (``li.done``),
    Nachfahren (Leerzeichen), Kinder (``>``) und mehrere Selektoren mit Komma.
    """
    if not selektor or not selektor.strip():
        return []
    ergebnis: list[Knoten] = []
    gesehen: set[int] = set()
    for einzel in selektor.split(","):
        tokens = _tokenisiere(einzel)
        if not tokens:
            continue
        for knoten in wurzel.alle():
            if _passt_kette(knoten, tokens) and id(knoten) not in gesehen:
                gesehen.add(id(knoten))
                ergebnis.append(knoten)
    ergebnis.sort(key=lambda k: _position(wurzel, k))
    return ergebnis


def _position(wurzel: Knoten, knoten: Knoten) -> int:
    for i, k in enumerate(wurzel.alle()):
        if k is knoten:
            return i
    return -1


def _passt_kette(knoten: Knoten, tokens: list[tuple[str, str]]) -> bool:
    kombinator, einfach = tokens[-1]
    if not _passt_einfach(knoten, einfach):
        return False
    if len(tokens) == 1:
        return True
    rest = tokens[:-1]
    if kombinator == ">":
        eltern = knoten.eltern
        return eltern is not None and eltern.tag != "#dokument" and _passt_kette(eltern, rest)
    vorfahr = knoten.eltern
    while vorfahr is not None and vorfahr.tag != "#dokument":
        if _passt_kette(vorfahr, rest):
            return True
        vorfahr = vorfahr.eltern
    return False


# ---------------------------------------------------------------------------
# Seite auswerten
# ---------------------------------------------------------------------------


def erkenne_seitentyp(html: str | None, konfig: HillebrandKonfig, *, url: str | None = None) -> SeitenTyp:
    """Enthält die Seite Sendungsdaten, eine Cloudflare-Challenge, die Okta-Anmeldung oder nichts?"""
    if konfig.ist_challenge(html):
        return "challenge"
    if konfig.ist_login_seite(html, url):
        return "login"
    if not html or not parse_html(html).text():
        return "leer"
    return "daten"


_GEPLANT_KLASSEN = re.compile(r"\b(?:planned|estimated|expected|future|upcoming|pending|todo)\b", re.I)
_ERLEDIGT_KLASSEN = re.compile(r"\b(?:done|completed|actual|confirmed|past|reached)\b", re.I)
_CODE_UNBEKANNT = "timeline"


def _meilenstein_aus_eintrag(eintrag: Knoten, konfig: HillebrandKonfig, bezug: datetime | None) -> Meilenstein | None:
    text = eintrag.text()
    kurz = " ".join(text.split())
    if not kurz:
        return None
    muster = konfig.muster_fuer_text(kurz)
    klassen = " ".join(sorted(eintrag.klassen))
    # TODO-PRUEFEN: Kennzeichnung geplanter Timeline-Einträge (Klassen/Texte) am echten Portal prüfen.
    if _ERLEDIGT_KLASSEN.search(klassen):
        geplant = False
    elif _GEPLANT_KLASSEN.search(klassen):
        geplant = True
    elif muster is not None:
        geplant = muster.geplant or konfig.ist_geplant_text(kurz)
    else:
        geplant = konfig.ist_geplant_text(kurz)
    zeitpunkt = finde_datum_tolerant(kurz, bezug)
    ort = None
    for kind in eintrag.alle():
        if any(re.search(r"location|place|city|origin|destination", k, re.I) for k in kind.klassen):
            ort = " ".join(kind.text().split()) or None
            break
    return Meilenstein(
        code=muster.code if muster is not None else _CODE_UNBEKANNT,
        text=kurz[:300],
        zeitpunkt=zeitpunkt,
        ort=ort,
        geplant=geplant,
    )


def _status_text(wurzel: Knoten, seite_text: str, sel: SelektorSatz, konfig: HillebrandKonfig) -> SendungStatus | None:
    if sel.status:
        for knoten in waehle(wurzel, sel.status):
            status = konfig.status_fuer_text(knoten.text())
            if status is not None:
                return status
    if sel.status_regex:
        treffer = kompiliere(sel.status_regex).search(seite_text)
        if treffer is not None:
            gruppe = treffer.groupdict().get("status") or treffer.group(0)
            status = konfig.status_fuer_text(gruppe)
            if status is not None:
                return status
    return None


def parse_public_tracking_html(
    html: str | None,
    konfig: HillebrandKonfig | None = None,
    *,
    selektoren: SelektorSatz | None = None,
    bezug: datetime | None = None,
) -> tuple[SendungStatus, list[Meilenstein]]:
    """Status und Meilensteine aus dem HTML der Tracking-Seite (Public Tracking oder Portal).

    ``selektoren``: Standard ``konfig.selektoren.public``; das Portal übergibt ``…portal``.
    ``bezug``: Datum, aus dem bei Kurzdaten ohne Jahr das Jahr ergänzt wird.
    Liefert ``(unbekannt, [])``, wenn die Seite keine erkennbaren Sendungsdaten enthält.
    """
    konfig = konfig or lade_hillebrand_konfig()
    sel = selektoren or konfig.selektoren.public
    wurzel = parse_html(html)
    seite_text = wurzel.text()

    meilensteine: list[Meilenstein] = []
    eintraege = waehle(wurzel, sel.eintrag) if sel.eintrag else []
    if not eintraege and sel.timeline:
        # Rückfall: Zeilen der Timeline als Einträge lesen
        for timeline in waehle(wurzel, sel.timeline):
            eintraege.extend(k for k in timeline.alle() if k.tag in ("li", "tr") or "item" in " ".join(k.klassen))
            if not eintraege:
                eintraege.extend(timeline.elemente())
    for eintrag in eintraege:
        m = _meilenstein_aus_eintrag(eintrag, konfig, bezug)
        if m is not None:
            meilensteine.append(m)
    meilensteine.sort(key=lambda m: (m.zeitpunkt or datetime.min, m.geplant))

    status_text = _status_text(wurzel, seite_text, sel, konfig)
    puenktlich: bool | None = None
    basis = SendungStatus.UNBEKANNT
    if status_text == SendungStatus.PROBLEM:
        return SendungStatus.PROBLEM, meilensteine
    if status_text == SendungStatus.VERSPAETET:
        puenktlich = False
    elif status_text is not None:
        basis = status_text
    status = status_aus_meilensteinen(meilensteine, konfig, basis=basis)
    if puenktlich is False and not ist_bekannt(status):
        status = SendungStatus.VERSPAETET
    return status_mit_puenktlichkeit(status, puenktlich), meilensteine


# ---------------------------------------------------------------------------
# Quelle
# ---------------------------------------------------------------------------


def _dateiname_sicher(text: str) -> str:
    return re.sub(r"[^\w.-]+", "_", text).strip("_")[:80] or "seite"


class PublicTrackingQuelle:
    """Tracking-Quelle über die öffentliche Track&Trace-Seite (ohne Login, reduzierter Datensatz)."""

    name = "public_tracking"

    def __init__(
        self,
        konfig: HillebrandKonfig | None = None,
        *,
        lade_seite: LadeSeite | None = None,
        tracking: TrackingKonfig | None = None,
    ) -> None:
        self._konfig = konfig or lade_hillebrand_konfig()
        self._lade_seite = lade_seite
        self._tracking = tracking or TrackingKonfig()

    @property
    def konfig(self) -> HillebrandKonfig:
        return self._konfig

    def verfuegbar(self) -> bool:
        return self._lade_seite is not None or playwright_verfuegbar()

    def url_fuer(self, suchbegriff: str) -> str:
        return self._konfig.public_url(suchbegriff)

    def aktualisiere(self, sendung: Sendung) -> Sendung:
        try:
            return self._aktualisiere(sendung)
        except LaderFehler as exc:
            logger.warning("Public Tracking: %s", exc)
            return mit_hinweis(sendung, f"Public Tracking: {exc}")
        except Exception as exc:  # nie nach außen
            logger.exception("Public Tracking: unerwarteter Fehler")
            return mit_hinweis(sendung, f"Public Tracking: unerwarteter Fehler {type(exc).__name__}: {exc}")

    def _aktualisiere(self, sendung: Sendung) -> Sendung:
        if not self.verfuegbar():
            return mit_hinweis(sendung, "Public Tracking nicht verfügbar: Playwright ist nicht installiert")
        # TODO-PRUEFEN: Welcher Suchbegriff trifft (DEMB…, jobDEM…, Kundenreferenz)? Recherche 1.3.
        suchbegriffe: list[str] = []
        for begriff in (sendung.referenz, sendung.kundenreferenz):
            b = (begriff or "").strip()
            if b and b not in suchbegriffe:
                suchbegriffe.append(b)
        if not suchbegriffe:
            return mit_hinweis(sendung, "Public Tracking: Sendung ohne Referenz, Suche nicht möglich")

        hinweise = list(sendung.hinweise)
        for begriff in suchbegriffe:
            url = self.url_fuer(begriff)
            html = self._lade(url)
            typ = erkenne_seitentyp(html, self._konfig, url=url)
            if typ == "challenge":
                return _mit_hinweisen(sendung, hinweise, "Public Tracking: Cloudflare-Challenge statt Sendungsdaten")
            if typ == "login":
                return _mit_hinweisen(sendung, hinweise, "Public Tracking: Anmeldeseite statt Sendungsdaten")
            status, meilensteine = parse_public_tracking_html(html, self._konfig, bezug=jetzt())
            if ist_bekannt(status) or meilensteine:
                return self._ergebnis(sendung, begriff, status, meilensteine, hinweise)
            hinweise.append(f"Public Tracking: keine Sendungsdaten zu {begriff}")
        return sendung.model_copy(update={"hinweise": hinweise, "letzte_pruefung": jetzt()})

    def _ergebnis(
        self, sendung: Sendung, begriff: str, status: SendungStatus, meilensteine: list[Meilenstein], hinweise: list[str]
    ) -> Sendung:
        if not ist_bekannt(status):
            hinweise.append(f"Public Tracking: Timeline zu {begriff} gelesen, Status nicht zuordenbar")
        geplante_abholung = sendung.geplante_abholung
        geplante_zustellung = sendung.geplante_zustellung
        for m in meilensteine:
            if m.zeitpunkt is None:
                continue
            if m.code.startswith("shpsupdep"):
                geplante_abholung = m.zeitpunkt.date()
            elif m.code.startswith("shpcusarr"):
                geplante_zustellung = m.zeitpunkt.date()
        return sendung.model_copy(
            update={
                "status": status,
                "meilensteine": meilensteine or list(sendung.meilensteine),
                "geplante_abholung": geplante_abholung,
                "geplante_zustellung": geplante_zustellung,
                "quelle": "public_tracking",
                "letzte_pruefung": jetzt(),
                "hinweise": hinweise,
            }
        )

    # -- Laden ---------------------------------------------------------------

    def _lade(self, url: str) -> str:
        if self._lade_seite is not None:
            try:
                return self._lade_seite(url) or ""
            except LaderFehler:
                raise
            except Exception as exc:
                raise LaderFehler(f"Seite {url} konnte nicht geladen werden: {type(exc).__name__}: {exc}") from exc
        return self._lade_mit_playwright(url)

    def _lade_mit_playwright(self, url: str) -> str:
        """Standard-Lader: Chromium per Playwright (headless, sofern ``settings.tracking.sichtbar`` aus)."""
        try:
            from playwright.sync_api import Error as PlaywrightError
            from playwright.sync_api import TimeoutError as PlaywrightTimeout
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise LaderFehler("Playwright ist nicht installiert (pip install playwright; playwright install chromium)") from exc
        browser_konfig = self._konfig.browser
        timeout_ms = int(browser_konfig.timeout_sekunden * 1000)
        sel = self._konfig.selektoren.public
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=not self._tracking.sichtbar)
                try:
                    seite = browser.new_page(
                        viewport={"width": browser_konfig.viewport_breite, "height": browser_konfig.viewport_hoehe}
                    )
                    seite.set_default_timeout(timeout_ms)
                    seite.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                    if sel.warten_auf:
                        try:
                            seite.wait_for_selector(sel.warten_auf, timeout=timeout_ms)
                        except PlaywrightTimeout:
                            logger.info("Public Tracking: '%s' nicht innerhalb %s s erschienen", sel.warten_auf, browser_konfig.timeout_sekunden)
                    self._screenshot(seite, url)
                    return seite.content()
                finally:
                    browser.close()
        except (PlaywrightTimeout, PlaywrightError) as exc:
            raise LaderFehler(f"Browserfehler beim Laden von {url}: {exc}") from exc

    def _screenshot(self, seite: object, url: str) -> Path | None:
        ordner = Path(self._tracking.screenshots_ordner) / "public"
        try:
            ordner.mkdir(parents=True, exist_ok=True)
            ziel = ordner / f"{jetzt():%Y%m%d_%H%M%S}_{_dateiname_sicher(url.rsplit('/', 1)[-1])}.png"
            seite.screenshot(path=str(ziel), full_page=True)  # type: ignore[attr-defined]
            return ziel
        except Exception as exc:  # Screenshot ist Komfort, nie Abbruchgrund
            logger.info("Screenshot nicht möglich: %s", exc)
            return None


def _mit_hinweisen(sendung: Sendung, hinweise: list[str], hinweis: str) -> Sendung:
    neue = list(hinweise)
    if hinweis not in neue:
        neue.append(hinweis)
    return sendung.model_copy(update={"hinweise": neue, "letzte_pruefung": jetzt()})
