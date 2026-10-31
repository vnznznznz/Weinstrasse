#!/usr/bin/env python3
"""Führt die Rohdaten (Web-Scrape der heutigen Weingut-Seiten, Katalog-Extraktion, Bilder)
zu website/daten/weingueter.json zusammen. Danach kann die Datei von Hand gepflegt werden;
das Skript überschreibt nur Felder, die leer sind, wenn --sanft übergeben wird."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

HIER = Path(__file__).resolve().parent.parent
DATEN = HIER / "daten"
LAND_NAME = {"italien": "Italien", "frankreich": "Frankreich", "spanien": "Spanien", "portugal": "Portugal",
             "bulgarien": "Bulgarien", "england": "England", "argentinien": "Argentinien", "australien": "Australien"}
NAMEN = {"la-bollina-sueditalien": "La Bollina Süditalien", "grappa": "Grappa: Ca' de Monte, Malgrà, Tenuta Carretta",
         "portweine": "Caves Messias: Portwein & Spirituosen", "schaumweine": "Caves Messias: Schaumweine",
         "spirituosen": "Spirituosen: Caballero Group", "lou-gat": "Lou Gat (Maison Marcellin)"}


def saeubere(text: str) -> str:
    """Entfernt Navigations- und Störer-Reste aus dem ersten Absatz der alten Seiten."""
    for marke in ("HIGHLIGHTS", "Katalog anfordern", "Highlights"):
        text = text.split(marke)[0]
    text = re.sub(r"(?<=[a-zäöüß.!?])(?=[A-ZÄÖÜ][a-zäöü])", " ", text)
    # angehängte Ortsangabe/Navigation nach dem letzten Satz entfernen
    text = re.sub(r"(\.)\s+[^.]{0,80}?,\s+(Frankreich|Italien|Spanien|Portugal|Bulgarien|England|Argentinien|Australien)\b.*$", r"\1", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def main() -> None:
    web = json.loads((DATEN / "roh" / "weingueter_web.json").read_text(encoding="utf-8"))
    bilder = {b["slug"]: b for b in json.loads((DATEN / "roh" / "weingueter_imgs.json").read_text(encoding="utf-8"))}
    kat_pfad = DATEN / "katalog_weingueter.json"
    katalog = {k["weingut_slug"]: k for k in json.loads(kat_pfad.read_text(encoding="utf-8"))} if kat_pfad.exists() else {}
    bestehend = {}
    if (DATEN / "weingueter.json").exists() and "--sanft" in sys.argv:
        bestehend = {w["slug"]: w for w in json.loads((DATEN / "weingueter.json").read_text(encoding="utf-8"))}

    aus = []
    for w in web:
        slug = w["slug"]
        bl = w["blocks"]
        name = next((t for tag, t in bl if tag == "h1"), slug)
        name = NAMEN.get(slug, name)
        ort, inhaber = "", ""
        besonderheiten, claim, absaetze = [], "", []
        for tag, t in bl:
            if tag == "h3" and t.startswith("Wo?"):
                rest = t[3:].strip()
                m = re.split(r"(Inhaber|Inhaberin|Eigentümer|Besitzer|Winemaker|Weinmacher|Önologe|Önologin|Kellermeister|Kellermeisterin|Geschäftsführer|Familie|Leitung|Gründer|Direktor|Chef)\s*:", rest, maxsplit=1)
                ort = m[0].strip(" ,")
                if len(m) >= 3:
                    inhaber = (m[1] + ": " + m[2]).strip()
                # Reste aus angehängten Elementen abschneiden (z.B. "FrankreichWeinguts...")
                ort = re.split(r"(?<=[a-zäöüß])(?=[A-ZÄÖÜ])", ort)[0].strip(" ,")
                inhaber = re.split(r"(?<=[a-zäöüß.])(?=[A-ZÄÖÜ][a-zäöü]+:?)", inhaber)[0].strip() if inhaber else ""
            elif tag == "h3" and "●" in t:
                besonderheiten = [x.strip(" .") for x in t.split("●") if x.strip()]
            elif tag == "h3" and t not in ("Weingut", "Besonderheiten", "HIGHLIGHTS") and not claim and len(t) > 20:
                claim = t
            elif tag == "p":
                absaetze.append(t)
        k = katalog.get(slug, {})
        b = bilder.get(slug, {})
        eintrag = {
            "slug": slug,
            "name": name,
            "land": LAND_NAME[w["land"]],
            "land_slug": w["land"],
            "url_alt": w["url"],
            "logo": f"logos/{slug}.webp",
            "hero": f"weingueter/{b['hero']}" if b.get("hero") else "",
            "galerie": [f"weingueter/{g}" for g in b.get("gallery", [])],
            "ort": ort,
            "inhaber": inhaber,
            "claim": k.get("claim") or claim,
            "region_titel": k.get("region_titel") or "",
            "besonderheiten": besonderheiten,
            "portraet": k.get("portraet") or saeubere(absaetze[0] if absaetze else ""),
            "absaetze": absaetze,
            "mindestmenge": k.get("mindestmenge_flaschen") or 120,
            "mindestmenge_text": k.get("mindestmenge_text") or "",
            "frachtkosten": k.get("frachtkosten") or [],
            "service": k.get("service") or "",
            "abwicklung": k.get("abwicklung") or "Weinstrasse",
            "katalogseiten": k.get("katalogseiten") or "",
            "neuentdeckung": bool(k.get("neuentdeckung")),
        }
        if slug in bestehend:
            alt = bestehend[slug]
            for feld, wert in eintrag.items():
                if alt.get(feld) in (None, "", [], 0):
                    alt[feld] = wert
            eintrag = alt
        aus.append(eintrag)
    reihenfolge = ["Italien", "Frankreich", "Spanien", "Portugal", "Bulgarien", "England", "Argentinien", "Australien"]
    aus.sort(key=lambda e: (reihenfolge.index(e["land"]), e["name"].lower()))
    (DATEN / "weingueter.json").write_text(json.dumps(aus, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{len(aus)} Weingüter geschrieben, {sum(1 for e in aus if e['portraet'])} mit Porträt, "
          f"{sum(1 for e in aus if e['hero'])} mit Hero-Bild, {sum(1 for e in aus if e['slug'] in katalog)} mit Katalogdaten")


if __name__ == "__main__":
    main()
