#!/usr/bin/env python3
"""Kundenzugänge für das Bestellportal verwalten.

Die Einkaufspreise liegen auf der Website nur verschlüsselt vor (AES-256-GCM). Der Preisschlüssel
(daten/schluessel.key, nicht im Repository) wird je Kunde mit einem aus Kundennummer-Passwort
abgeleiteten Schlüssel (PBKDF2-SHA256) "eingepackt" und in daten/zugaenge.json abgelegt. Die Website
enthält damit kein Passwort und keinen Klartext-Preis; ohne gültige Anmeldung bleiben die Preise
unlesbar. Einen Zugang entfernen = Eintrag löschen und neu bauen.

Beispiele:
  python3 website/scripts/zugang.py anlegen --kundennummer 10001 --firma "Weinhandlung Muster" --passwort "geheim"
  python3 website/scripts/zugang.py anlegen --kundennummer 10002 --firma "Vinothek Beispiel"   (Passwort wird erzeugt)
  python3 website/scripts/zugang.py liste
  python3 website/scripts/zugang.py entfernen --kundennummer 10001
  python3 website/scripts/zugang.py schluessel-neu     (alle Zugänge müssen danach neu angelegt werden)
Danach immer: python3 website/build.py
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import secrets
import sys
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

HIER = Path(__file__).resolve().parent.parent
DATEN = HIER / "daten"
SCHLUESSEL = DATEN / "schluessel.key"
ZUGAENGE = DATEN / "zugaenge.json"
ITERATIONEN = 250_000


def b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def lade_schluessel(erzeugen: bool = True) -> bytes:
    if SCHLUESSEL.exists():
        return bytes.fromhex(SCHLUESSEL.read_text().strip())
    if not erzeugen:
        raise SystemExit("Kein Preisschlüssel vorhanden (daten/schluessel.key).")
    key = AESGCM.generate_key(bit_length=256)
    SCHLUESSEL.write_text(key.hex() + "\n")
    os.chmod(SCHLUESSEL, 0o600)
    print(f"Neuer Preisschlüssel erzeugt: {SCHLUESSEL} (sicher aufbewahren, nicht ins Repository)")
    return key


def lade_zugaenge() -> list[dict]:
    if ZUGAENGE.exists():
        return json.loads(ZUGAENGE.read_text(encoding="utf-8"))
    return []


def speichere_zugaenge(z: list[dict]) -> None:
    ZUGAENGE.write_text(json.dumps(z, ensure_ascii=False, indent=1), encoding="utf-8")


def kek(passwort: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", passwort.encode("utf-8"), salt, ITERATIONEN, 32)


def anlegen(kundennummer: str, firma: str, passwort: str | None, ansprechpartner: str = "") -> None:
    master = lade_schluessel()
    zugaenge = [z for z in lade_zugaenge() if z["kundennummer"].lower() != kundennummer.lower()]
    if not passwort:
        passwort = "-".join(secrets.token_urlsafe(4) for _ in range(3))
        print(f"Erzeugtes Passwort für {kundennummer}: {passwort}")
    salt = secrets.token_bytes(16)
    iv = secrets.token_bytes(12)
    eingepackt = AESGCM(kek(passwort, salt)).encrypt(iv, master, kundennummer.lower().encode())
    zugaenge.append({"kundennummer": kundennummer, "firma": firma, "ansprechpartner": ansprechpartner,
                     "salt": b64(salt), "iv": b64(iv), "schluessel": b64(eingepackt), "iterationen": ITERATIONEN})
    zugaenge.sort(key=lambda z: z["kundennummer"])
    speichere_zugaenge(zugaenge)
    print(f"Zugang {kundennummer} ({firma}) angelegt. Jetzt: python3 website/build.py")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="befehl", required=True)
    a = sub.add_parser("anlegen", help="Zugang anlegen oder Passwort neu setzen")
    a.add_argument("--kundennummer", required=True)
    a.add_argument("--firma", required=True)
    a.add_argument("--ansprechpartner", default="")
    a.add_argument("--passwort", help="leer lassen, um ein Passwort erzeugen zu lassen")
    e = sub.add_parser("entfernen", help="Zugang löschen")
    e.add_argument("--kundennummer", required=True)
    sub.add_parser("liste", help="alle Zugänge anzeigen")
    sub.add_parser("schluessel-neu", help="Preisschlüssel erneuern (alle Zugänge verfallen)")
    args = ap.parse_args()
    if args.befehl == "anlegen":
        anlegen(args.kundennummer, args.firma, args.passwort, args.ansprechpartner)
    elif args.befehl == "entfernen":
        rest = [z for z in lade_zugaenge() if z["kundennummer"].lower() != args.kundennummer.lower()]
        speichere_zugaenge(rest)
        print(f"Zugang {args.kundennummer} entfernt. Jetzt: python3 website/build.py")
    elif args.befehl == "liste":
        for z in lade_zugaenge():
            print(f"{z['kundennummer']:>10}  {z['firma']}  {z.get('ansprechpartner','')}")
        print(f"{len(lade_zugaenge())} Zugänge")
    elif args.befehl == "schluessel-neu":
        if SCHLUESSEL.exists():
            SCHLUESSEL.unlink()
        speichere_zugaenge([])
        lade_schluessel()
        print("Alle Zugänge entfernt. Bitte neu anlegen und bauen.")


if __name__ == "__main__":
    sys.exit(main())
