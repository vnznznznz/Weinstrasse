"""Kommandozeile: ``PYTHONPATH=src python3 -m postfach <befehl>``.

Befehle
-------
``regeln``                         Regeldatei prüfen, Ordner ohne ID auflisten
``klassifizieren MAILS.json``      Entscheidungen je Mail (``--plan`` für fertige Verschiebe-Gruppen)
``antworten EINGANG.json GESENDET.json``  Antworten auf eigene Mails markieren (schreibt JSON)
``anstehend [--tage N]``           fällige wiederkehrende Aufgaben
``briefing-geruest``               Gerüst für das Morgen-Briefing (Datum, KW, Termine, Platzhalter)
``zustand zeigen|lauf NAME|bearbeitet ID...|entwurf MAIL ENTWURF``

Alle Ausgaben deutsch. ``--json`` liefert maschinenlesbare Ausgabe.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import UTC, date, datetime
from pathlib import Path

from postfach import __version__
from postfach.antworten import markiere_antworten
from postfach.klassifikator import klassifiziere_alle
from postfach.modelle import Entscheidung, Mail
from postfach.regeln import RegelFehler, lade_regelwerk
from postfach.termine import TerminFehler, anstehend, lade_aufgaben
from postfach.zustand import Zustand

BATCH = 5  # Höchstzahl Mails je Verschiebe-Aufruf des Connectors


def _lade_mails(pfad: str) -> list[Mail]:
    roh = json.loads(Path(pfad).read_text(encoding="utf-8"))
    if isinstance(roh, dict):
        roh = roh.get("mails") or roh.get("results") or roh.get("value") or []
    mails: list[Mail] = []
    for eintrag in roh:
        if not isinstance(eintrag, dict) or not (eintrag.get("id") or eintrag.get("messageId")):
            continue  # Paginierungszeilen wie {"nextOffset": 15} überspringen
        mails.append(Mail.aus_suche(eintrag))
    return mails


def _heute(arg: str | None) -> datetime:
    if not arg:
        return datetime.now(UTC)
    return (
        datetime.fromisoformat(arg).replace(tzinfo=UTC)
        if "T" in arg
        else datetime.combine(date.fromisoformat(arg), datetime.min.time(), UTC)
    )


# ---------------------------------------------------------------------------- Befehle
def befehl_regeln(args: argparse.Namespace) -> int:
    rw = lade_regelwerk(args.regeln)
    meldungen = rw.pruefen()
    if args.json:
        print(
            json.dumps(
                {
                    "mailbox": rw.mailbox,
                    "stand": rw.stand,
                    "regeln": [r.name for r in rw.regeln],
                    "meldungen": meldungen,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    print(
        f"Regelwerk für {rw.mailbox} (Stand {rw.stand}): {len(rw.regeln)} Regeln, {len(rw.kategorien)} Farben, {len(rw.lieferanten_domains)} Lieferanten-Domains."
    )
    for r in rw.regeln:
        print(f"  - {r.name}")
    if meldungen:
        print("Hinweise:")
        for m in meldungen:
            print(f"  ! {m}")
    else:
        print("Keine Hinweise.")
    return 0


def befehl_klassifizieren(args: argparse.Namespace) -> int:
    rw = lade_regelwerk(args.regeln)
    mails = _lade_mails(args.mails)
    if args.gesendet:
        markiere_antworten(mails, _lade_mails(args.gesendet))
    behalten: set[str] = set()
    if args.behalten:
        behalten = {
            z.strip() for z in Path(args.behalten).read_text(encoding="utf-8").splitlines() if z.strip()
        }
    entscheidungen = klassifiziere_alle(mails, rw, behalten_ids=behalten, heute=_heute(args.heute))
    if args.json:
        print(json.dumps([e.als_dict() for e in entscheidungen], ensure_ascii=False, indent=2))
        return 0
    if args.plan:
        _drucke_plan(entscheidungen, mails)
        return 0
    for e in entscheidungen:
        ziel = e.ziel_ordner or "—"
        status = "bleibt: " + e.bleibt_grund if e.bleibt_grund else e.aktion
        print(
            f"[{e.regel}] {e.absender} | {e.betreff[:60]!r} → {ziel} ({status}) | Entwurf: {e.antwort} ({e.antwort_grund})"
        )
        for h in e.hinweise:
            print(f"      · {h}")
    return 0


def _drucke_plan(entscheidungen: list[Entscheidung], mails: list[Mail]) -> None:
    nach_mail = {m.id: m for m in mails}
    gruppen: dict[tuple[str, str], list[Entscheidung]] = defaultdict(list)
    loeschen: list[Entscheidung] = []
    bleiben: list[Entscheidung] = []
    entwuerfe: dict[str, list[Entscheidung]] = defaultdict(list)
    for e in entscheidungen:
        if e.aktion == "soft_delete" and not e.bleibt_grund:
            loeschen.append(e)
        elif e.wird_verschoben and e.ziel_ordner and e.ziel_ordner_id:
            gruppen[(e.ziel_ordner, e.ziel_ordner_id)].append(e)
        else:
            bleiben.append(e)
        entwuerfe[e.antwort].append(e)

    print("## Verschieben (je Aufruf höchstens 5 IDs)")
    gesamt = 0
    for (ordner, ordner_id), liste in sorted(gruppen.items()):
        print(f"\n### {ordner} ({len(liste)} Mails)\nmoveToFolderId: {ordner_id}")
        for i in range(0, len(liste), BATCH):
            ids = [e.mail_id for e in liste[i : i + BATCH]]
            print("messageIds: " + json.dumps(ids))
        for e in liste:
            print(f"  - {e.absender}: {e.betreff[:70]}")
        gesamt += len(liste)
    print(f"\nSumme zu verschieben: {gesamt}")

    print("\n## Löschen (soft, nur freigegebene Regeln) – outlook_batch_delete_messages")
    if loeschen:
        print("messageIds: " + json.dumps([e.mail_id for e in loeschen]))
        for e in loeschen:
            print(f"  - [{e.regel}] {e.absender}: {e.betreff[:70]}")
    else:
        print("  keine")

    print("\n## Bleibt im Posteingang")
    for e in bleiben:
        grund = e.bleibt_grund or ("Regel „" + e.regel + "“" if e.aktion == "liegenlassen" else e.aktion)
        print(f"  - {e.absender}: {e.betreff[:60]} ({grund})")

    print("\n## Antwort-Entwürfe")
    for stufe in ("ja", "pruefen"):
        liste = entwuerfe.get(stufe, [])
        print(f"\n### {stufe} ({len(liste)})")
        for e in liste:
            m = nach_mail.get(e.mail_id)
            ungelesen = "ungelesen" if m and not m.gelesen else "gelesen"
            print(f"  - {e.absender}: {e.betreff[:60]} ({e.antwort_grund}, {ungelesen})")

    hinweise = [(e, h) for e in entscheidungen for h in e.hinweise]
    if hinweise:
        print("\n## Hinweise")
        for e, h in hinweise:
            print(f"  - {e.absender}: {h}")


def befehl_antworten(args: argparse.Namespace) -> int:
    eingang = _lade_mails(args.eingang)
    gesendet = _lade_mails(args.gesendet)
    markiere_antworten(eingang, gesendet)
    treffer = [m for m in eingang if m.antwort_auf_eigene]
    unklar = [m for m in eingang if m.antwort_auf_eigene is None]
    if args.json:
        print(
            json.dumps(
                {"antworten": [m.id for m in treffer], "unklar": [m.id for m in unklar]},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    print(
        f"{len(treffer)} Antworten auf deine Mails erkannt, {len(unklar)} unklar (Antwort-Betreff ohne gefundene Ursprungsmail)."
    )
    for m in treffer:
        print(f"  ✓ {m.absender}: {m.betreff[:70]}")
    for m in unklar:
        print(f"  ? {m.absender}: {m.betreff[:70]}")
    return 0


def befehl_anstehend(args: argparse.Namespace) -> int:
    heute = date.fromisoformat(args.heute) if args.heute else date.today()
    faellig = anstehend(lade_aufgaben(args.datei), heute=heute, tage=args.tage)
    if args.json:
        print(
            json.dumps(
                [
                    {
                        "name": f.aufgabe.name,
                        "termin": f.termin.isoformat(),
                        "in_tagen": f.in_tagen,
                        "aufgaben": f.aufgabe.aufgaben,
                        "quelle": f.aufgabe.quelle,
                        "art": f.aufgabe.art,
                    }
                    for f in faellig
                ],
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if not faellig:
        print(f"Nichts Wiederkehrendes in den nächsten {args.tage} Tagen.")
        return 0
    for f in faellig:
        print(("!! " if f.dringend else "   ") + f.zeile())
    return 0


def befehl_briefing_geruest(args: argparse.Namespace) -> int:
    heute = date.fromisoformat(args.heute) if args.heute else date.today()
    wochentage = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
    kw = heute.isocalendar()[1]
    faellig = anstehend(lade_aufgaben(args.datei), heute=heute, tage=args.tage)
    zustand = Zustand(args.zustand) if args.zustand else Zustand()
    antworten = zustand.daten.get("antworten_erhalten", [])[-15:]
    entwuerfe = list(zustand.daten.get("entwuerfe", {}).values())[-15:]
    zeilen = [
        f"# Briefing {wochentage[heute.weekday()]}, {heute.strftime('%d.%m.%Y')} (KW {kw})",
        "",
        "## Heute",
        "<Termine aus dem Kalender (outlook_calendar_search, heute), je Zeile: Uhrzeit – Titel – mit wem>",
        "",
        "## Zuerst erledigen (max. 5)",
        "<Fristen, Geld, Behörden, Steuerberater, Kunden die warten – aus Posteingang und offene_vorgaenge.md>",
        "",
        "## Antworten erhalten (seit gestern)",
    ]
    zeilen += [f"- {a['von']}: {a['betreff']}" for a in antworten] or [
        "<keine im Zustand vermerkt – per Suche prüfen>"
    ]
    zeilen += ["", "## Entwürfe, die auf dich warten"]
    zeilen += [f"- an {e.get('an', '?')}: {e.get('betreff', '')}" for e in entwuerfe] or [
        "<Ordner Entwürfe prüfen>"
    ]
    zeilen += ["", f"## Anstehend (nächste {args.tage} Tage)"]
    zeilen += [
        ("- **" if f.dringend else "- ") + f.zeile() + ("**" if f.dringend else "") for f in faellig
    ] or ["- nichts Wiederkehrendes fällig"]
    zeilen += [
        "",
        "## Diese Woche im Blick",
        "<Kalender nächste 7 Tage, Zusagen aus gesendeten Mails, Lieferungen/Abholungen>",
        "",
        "## Neu gelernt / Fragen an dich",
        "<neue Absender, unklare Zuordnungen, höchstens 3 Fragen mit Vorschlag>",
    ]
    print("\n".join(zeilen))
    return 0


def befehl_zustand(args: argparse.Namespace) -> int:
    z = Zustand(args.zustand) if args.zustand else Zustand()
    if args.unterbefehl == "zeigen":
        print(json.dumps(z.daten, ensure_ascii=False, indent=2))
        return 0
    if args.unterbefehl == "lauf":
        z.merke_lauf(args.name, notiz=args.notiz or "")
    elif args.unterbefehl == "bearbeitet":
        z.merke_bearbeitet(args.ids)
    elif args.unterbefehl == "entwurf":
        z.merke_entwurf(args.mail, args.entwurf, betreff=args.betreff or "", an=args.an or "")
    elif args.unterbefehl == "antwort":
        z.merke_antwort_erhalten(args.betreff or "", args.von or "", web_link=args.link)
    pfad = z.speichern()
    print(f"Zustand gespeichert: {pfad}")
    return 0


# ---------------------------------------------------------------------------- Parser
def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="postfach", description="Postfach-Assistent – deterministischer Kern")
    p.add_argument("--version", action="version", version=f"postfach {__version__}")
    p.add_argument("--regeln", help="Pfad zu regeln.json (Standard: postfach/regeln.json)")
    p.add_argument("--zustand", help="Pfad zu zustand.json")
    p.add_argument("--json", action="store_true", help="maschinenlesbare Ausgabe")
    sub = p.add_subparsers(dest="befehl", required=True)

    sub.add_parser("regeln", help="Regeldatei prüfen").set_defaults(func=befehl_regeln)

    k = sub.add_parser("klassifizieren", help="Mails nach Regeln einordnen")
    k.add_argument("mails", help="JSON-Datei mit Suchtreffern des Connectors")
    k.add_argument("--gesendet", help="JSON-Datei mit gesendeten Mails (für Antwort-Erkennung)")
    k.add_argument("--behalten", help="Textdatei mit Mail-IDs, die im Posteingang bleiben")
    k.add_argument("--heute", help="Stichtag (ISO), Standard jetzt")
    k.add_argument("--plan", action="store_true", help="fertige Verschiebe-Gruppen à 5 IDs ausgeben")
    k.set_defaults(func=befehl_klassifizieren)

    a = sub.add_parser("antworten", help="Antworten auf eigene Mails erkennen")
    a.add_argument("eingang")
    a.add_argument("gesendet")
    a.set_defaults(func=befehl_antworten)

    n = sub.add_parser("anstehend", help="fällige wiederkehrende Aufgaben")
    n.add_argument("--tage", type=int, default=28)
    n.add_argument("--heute")
    n.add_argument("--datei", help="Pfad zu wiederkehrend.yaml")
    n.set_defaults(func=befehl_anstehend)

    b = sub.add_parser("briefing-geruest", help="Gerüst für das Morgen-Briefing")
    b.add_argument("--tage", type=int, default=28)
    b.add_argument("--heute")
    b.add_argument("--datei")
    b.set_defaults(func=befehl_briefing_geruest)

    z = sub.add_parser("zustand", help="Laufzustand lesen/schreiben")
    zs = z.add_subparsers(dest="unterbefehl", required=True)
    zs.add_parser("zeigen")
    zl = zs.add_parser("lauf")
    zl.add_argument("name")
    zl.add_argument("--notiz")
    zb = zs.add_parser("bearbeitet")
    zb.add_argument("ids", nargs="+")
    ze = zs.add_parser("entwurf")
    ze.add_argument("mail")
    ze.add_argument("entwurf")
    ze.add_argument("--betreff")
    ze.add_argument("--an")
    za = zs.add_parser("antwort")
    za.add_argument("--betreff")
    za.add_argument("--von")
    za.add_argument("--link")
    z.set_defaults(func=befehl_zustand)
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return int(args.func(args))
    except (RegelFehler, TerminFehler, FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"Fehler: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
