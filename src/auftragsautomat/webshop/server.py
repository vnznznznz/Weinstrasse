"""Kleiner HTTP-Server für den Shop: liefert ``website/`` aus und nimmt ``POST /api/bestellung`` an.

Nur Standardbibliothek. Für den Produktivbetrieb hinter einen Reverse-Proxy mit TLS (z. B. Caddy,
nginx) stellen; der Server selbst spricht nur HTTP.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime
from functools import partial
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from auftragsautomat.webshop.bestellung import BestellFehler, Produktkatalog, nimm_bestellung_an
from auftragsautomat.webshop.konfig import WebshopKonfig

logger = logging.getLogger(__name__)

MAX_BODY = 64 * 1024
_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
}


class ShopHandler(SimpleHTTPRequestHandler):
    """Statische Dateien aus ``website_ordner`` plus die Bestell-API."""

    def __init__(self, *args, konfig: WebshopKonfig, website_ordner: Path, katalog: Produktkatalog, lock: threading.Lock, **kw):
        self.konfig = konfig
        self.katalog = katalog
        self.lock = lock
        super().__init__(*args, directory=str(website_ordner), **kw)

    # ------------------------------------------------------------------ Hilfen
    def log_message(self, format: str, *args) -> None:  # noqa: A002 - Signatur der Basisklasse
        logger.debug("%s - %s", self.address_string(), format % args)

    def end_headers(self) -> None:
        for name, wert in _HEADERS.items():
            self.send_header(name, wert)
        super().end_headers()

    def _json(self, status: HTTPStatus, daten: dict) -> None:
        roh = json.dumps(daten, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(roh)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(roh)

    # ------------------------------------------------------------------ Routen
    def do_GET(self) -> None:  # noqa: N802 - Name der Basisklasse
        if self.path.split("?", 1)[0] == "/api/status":
            self._json(HTTPStatus.OK, {"ok": True, "produkte": len(self.katalog.produkte)})
            return
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        if self.path.split("?", 1)[0] != "/api/bestellung":
            self._json(HTTPStatus.NOT_FOUND, {"fehler": "Unbekannter Pfad."})
            return
        laenge = int(self.headers.get("Content-Length") or 0)
        if laenge <= 0 or laenge > MAX_BODY:
            self._json(HTTPStatus.BAD_REQUEST, {"fehler": "Ungültige Anfrage."})
            return
        try:
            roh = json.loads(self.rfile.read(laenge).decode("utf-8"))
            if not isinstance(roh, dict):
                raise ValueError("kein Objekt")
        except (ValueError, UnicodeDecodeError):
            self._json(HTTPStatus.BAD_REQUEST, {"fehler": "Die Bestellung konnte nicht gelesen werden."})
            return
        try:
            with self.lock:  # Bestellnummern fortlaufend, keine Doppelvergabe
                annahme = nimm_bestellung_an(roh, self.konfig, self.katalog, jetzt=datetime.now())
        except BestellFehler as exc:
            self._json(HTTPStatus.UNPROCESSABLE_ENTITY, {"fehler": str(exc)})
            return
        except Exception:  # noqa: BLE001 - dem Kunden nie einen Traceback zeigen
            logger.exception("Bestellung konnte nicht verarbeitet werden")
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"fehler": "Interner Fehler. Bitte per E-Mail bestellen."})
            return
        b = annahme.bestellung
        self._json(
            HTTPStatus.CREATED,
            {
                "nummer": b.nummer,
                "gesamt": float(b.gesamt),
                "lieferkosten": float(b.lieferkosten),
                "lieferart": b.lieferart,
                "termin": b.zeitfenster_text,
                "mail_gesendet": annahme.gesendet,
            },
        )


def erzeuge_server(konfig: WebshopKonfig, website_ordner: Path, host: str = "127.0.0.1", port: int = 8080) -> ThreadingHTTPServer:
    katalog = Produktkatalog.aus_datei(konfig.ausgabe_ordner / "produkte.json")
    handler = partial(ShopHandler, konfig=konfig, website_ordner=website_ordner, katalog=katalog, lock=threading.Lock())
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


def starte(konfig: WebshopKonfig, website_ordner: Path, host: str = "127.0.0.1", port: int = 8080) -> None:  # pragma: no cover
    server = erzeuge_server(konfig, website_ordner, host, port)
    logger.info("Webshop läuft auf http://%s:%d (Ctrl+C beendet)", host, server.server_address[1])
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
