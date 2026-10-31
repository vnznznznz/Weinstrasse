#!/usr/bin/env python3
"""Statischer Seiten-Generator für weinstrasseadolph.de (Relaunch-Entwurf).

Aufruf:  python3 website/build.py            -> schreibt website/dist/
Vorschau: python3 -m http.server -d website/dist 8080

Inhalte liegen in website/daten/*.json, Gestaltung in website/templates und website/static.
Alle Links sind relativ, damit die Seite unter jedem Pfad (lokal, Vorschau, Hosting) läuft.
"""
from __future__ import annotations

import json
import re
import shutil
import sys
import unicodedata
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

HIER = Path(__file__).resolve().parent
DATEN = HIER / "daten"
DIST = HIER / "dist"

LAENDER = ["Italien", "Frankreich", "Spanien", "Portugal", "Bulgarien", "England", "Argentinien", "Australien"]
LAND_SLUG = {"Italien": "italien", "Frankreich": "frankreich", "Spanien": "spanien", "Portugal": "portugal",
             "Bulgarien": "bulgarien", "England": "england", "Argentinien": "argentinien", "Australien": "australien"}
LAND_FLAGGE = {"Italien": "🇮🇹", "Frankreich": "🇫🇷", "Spanien": "🇪🇸", "Portugal": "🇵🇹", "Bulgarien": "🇧🇬",
               "England": "🇬🇧", "Argentinien": "🇦🇷", "Australien": "🇦🇺"}


def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text or "x"


def lade(name: str, standard):
    pfad = DATEN / name
    if not pfad.exists():
        return standard
    return json.loads(pfad.read_text(encoding="utf-8"))


def preis(wert) -> str:
    if wert is None:
        return ""
    return f"{wert:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".") + " €"


def main() -> None:
    explizit_index = "--index" in sys.argv
    weingueter = lade("weingueter.json", [])
    produkte = lade("produkte.json", [])
    news = lade("aktuelles.json", [])
    texte = lade("texte.json", {})

    # Produkte an Weingüter hängen
    nach_slug = {w["slug"]: w for w in weingueter}
    for w in weingueter:
        w.setdefault("produkte", [])
    for p in produkte:
        p.setdefault("ve", 6)
        p["preis_text"] = preis(p.get("listenpreis"))
        p["uvp_text"] = preis(p.get("uvp"))
        w = nach_slug.get(p.get("weingut_slug"))
        if w is not None:
            w["produkte"].append(p)
            p["weingut_name"] = w["name"]
            p["weingut_url"] = f"weingueter/{w['land_slug']}/{w['slug']}/"
            p["land"] = w["land"]
    for w in weingueter:
        # Regionen in Reihenfolge des Auftretens
        regionen = []
        for p in w["produkte"]:
            r = p.get("region") or w.get("region_titel") or ""
            if r not in regionen:
                regionen.append(r)
        w["regionen"] = [(r, [p for p in w["produkte"] if (p.get("region") or w.get("region_titel") or "") == r]) for r in regionen]
        w["anzahl_produkte"] = len(w["produkte"])
        w["preis_ab"] = min([p["listenpreis"] for p in w["produkte"] if p.get("listenpreis")], default=None)
        w["preis_ab_text"] = preis(w["preis_ab"])
        w["auszeichnungen"] = sum(len(p.get("auszeichnungen") or []) for p in w["produkte"])

    nach_land = {l: [w for w in weingueter if w["land"] == l] for l in LAENDER}
    nach_land = {l: ws for l, ws in nach_land.items() if ws}

    env = Environment(loader=FileSystemLoader(HIER / "templates"), autoescape=select_autoescape(["html"]),
                      trim_blocks=True, lstrip_blocks=True)
    env.filters["preis"] = preis
    env.globals.update(LAENDER=LAENDER, LAND_FLAGGE=LAND_FLAGGE, nach_land=nach_land, texte=texte,
                       anzahl_weingueter=len(weingueter), anzahl_produkte=len(produkte))

    if DIST.exists():
        shutil.rmtree(DIST)
    DIST.mkdir(parents=True)
    shutil.copytree(HIER / "static", DIST / "static")

    # Daten für Katalog, Portal und Weinberater als JS (funktioniert auch per file://)
    daten_js = {
        "produkte": [{k: p.get(k) for k in ("id", "weingut_slug", "weingut_name", "weingut_url", "land", "region", "kategorie",
                                             "name", "jahrgang", "groesse_cl", "listenpreis", "uvp", "aktion", "aktionspreis",
                                             "zusatzsteuer", "rebsorte", "beschreibung", "auszeichnungen", "bio", "hinweis", "ve")}
                     for p in produkte],
        "weingueter": [{k: w.get(k) for k in ("slug", "name", "land", "land_slug", "logo", "hero", "mindestmenge", "mindestmenge_text",
                                               "frachtkosten", "abwicklung", "anzahl_produkte")} for w in weingueter],
    }
    (DIST / "static" / "js" / "daten.js").write_text(
        "window.WS_DATEN=" + json.dumps(daten_js, ensure_ascii=False) + ";", encoding="utf-8")

    def schreibe(pfad: str, vorlage: str, **ctx):
        tiefe = pfad.count("/") + 1 if pfad else 0
        root = "../" * tiefe if tiefe else "./"
        ziel = DIST / pfad / "index.html" if pfad else DIST / "index.html"
        ziel.parent.mkdir(parents=True, exist_ok=True)
        html = env.get_template(vorlage).render(root=root, pfad=pfad, **ctx)
        if explizit_index:
            # Für Hoster ohne Verzeichnis-Index (z.B. Artefakt-Vorschau): "ordner/" -> "ordner/index.html"
            html = re.sub(r'href="((?:\./|\.\./)[^"#?:]*?/|\./|\.\./)(#[^"]*)?"',
                          lambda m: 'href="' + m.group(1) + 'index.html' + (m.group(2) or '') + '"', html)
        ziel.write_text(html, encoding="utf-8")

    schreibe("", "index.html", weingueter=weingueter, news=news, produkte=produkte, seite="home")
    schreibe("weingueter", "weingueter.html", weingueter=weingueter, seite="weingueter")
    schreibe("katalog", "katalog.html", seite="katalog")
    schreibe("bestellen", "bestellen.html", seite="bestellen")
    schreibe("service", "service.html", seite="service")
    schreibe("ueber-uns", "ueber-uns.html", seite="ueber-uns")
    for i, w in enumerate(weingueter):
        zurueck = weingueter[i - 1] if i > 0 else weingueter[-1]
        weiter = weingueter[(i + 1) % len(weingueter)]
        schreibe(f"weingueter/{w['land_slug']}/{w['slug']}", "weingut.html", w=w, zurueck=zurueck, weiter=weiter, seite="weingueter")

    anzahl = sum(1 for _ in DIST.rglob("*.html"))
    groesse = sum(f.stat().st_size for f in DIST.rglob("*") if f.is_file()) / 1e6
    print(f"dist/ geschrieben: {anzahl} HTML-Seiten, {len(produkte)} Produkte, {len(weingueter)} Weingüter, {groesse:.1f} MB")


if __name__ == "__main__":
    main()
