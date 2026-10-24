"""Tests für m365.graph und m365.adressierung mit injiziertem Fake-Transport (kein Netz)."""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from auftragsautomat.config import DateiRefKonfig
from auftragsautomat.m365 import adressierung, graph
from auftragsautomat.m365.basis import M365Fehler, M365Konflikt, M365NichtAngemeldet, M365NichtGefunden
from auftragsautomat.m365.graph import (
    ANHANG_KLEIN_MAX,
    DATEI_KLEIN_MAX,
    UPLOAD_CHUNK,
    GraphClient,
    GraphDateiBackend,
    GraphExcelBackend,
    GraphMailBackend,
)
from auftragsautomat.models import EmailEntwurf, EntwurfTyp

# ---------------------------------------------------------------- Fake-Transport


@dataclass
class Anfrage:
    methode: str
    url: str
    headers: dict[str, str]
    json: Any
    data: bytes | None
    params: dict[str, str] | None

    @property
    def pfad(self) -> str:
        return self.url.replace(graph.GRAPH_BASIS, "")


Antwort = tuple[int, Any, dict[str, str]]
Regel = tuple[str, str, Any]  # Methode, URL-Regex, Antwort oder Funktion(Anfrage) -> Antwort


def ok(body: Any = None, status: int = 200, **headers: str) -> Antwort:
    return status, {} if body is None else body, headers


def fehler(status: int, code: str, inner: str | None = None, **headers: str) -> Antwort:
    body: dict[str, Any] = {"error": {"code": code, "message": f"Meldung zu {code}"}}
    if inner:
        body["error"]["innerError"] = {"code": inner}
    return status, body, headers


class FakeTransport:
    """Zeichnet Anfragen auf; antwortet nach Regeln (Methode + URL-Regex), sonst aus einer Warteschlange."""

    def __init__(self) -> None:
        self.anfragen: list[Anfrage] = []
        self.regeln: list[Regel] = []
        self.warteschlange: list[Antwort] = []

    def regel(self, methode: str, muster: str, antwort: Any) -> None:
        self.regeln.append((methode.upper(), muster, antwort))

    def __call__(self, methode: str, url: str, *, headers=None, json=None, data=None, params=None, stream=False):
        anfrage = Anfrage(methode.upper(), url, dict(headers or {}), json, data, dict(params) if params else None)
        self.anfragen.append(anfrage)
        for m, muster, antwort in self.regeln:
            if m == anfrage.methode and re.search(muster, url):
                status, body, kopf = antwort(anfrage) if callable(antwort) else antwort
                break
        else:
            status, body, kopf = self.warteschlange.pop(0) if self.warteschlange else (200, {}, {})
        kopf = dict(kopf)
        if isinstance(body, bytes):
            kopf.setdefault("Content-Type", "application/octet-stream")
            return status, kopf, body
        kopf.setdefault("Content-Type", "application/json; odata.metadata=minimal")
        return status, kopf, json_bytes(body)

    def letzte(self, methode: str | None = None) -> Anfrage:
        kandidaten = [a for a in self.anfragen if methode is None or a.methode == methode.upper()]
        return kandidaten[-1]

    def methoden_und_pfade(self) -> list[tuple[str, str]]:
        return [(a.methode, a.pfad) for a in self.anfragen]


def json_bytes(body: Any) -> bytes:
    return json.dumps(body).encode("utf-8")


class FakeUhr:
    def __init__(self) -> None:
        self.zeit = 1000.0
        self.pausen: list[float] = []

    def __call__(self) -> float:
        return self.zeit

    def schlaf(self, sekunden: float) -> None:
        self.pausen.append(round(sekunden, 3))
        self.zeit += sekunden


@pytest.fixture()
def transport() -> FakeTransport:
    return FakeTransport()


@pytest.fixture()
def uhr() -> FakeUhr:
    return FakeUhr()


@pytest.fixture()
def client(transport: FakeTransport, uhr: FakeUhr) -> GraphClient:
    return GraphClient(lambda: "TOKEN", sende=transport, anfragen_pro_sekunde=0, schlaf=uhr.schlaf, uhr=uhr)


REF = DateiRefKonfig(drive_id="b!drive", item_id="01ITEM", blatt="Fixterminüberwachung")
MAPPE = "/drives/b!drive/items/01ITEM/workbook"


# ---------------------------------------------------------------- GraphClient


def test_client_setzt_token_und_liest_json(client: GraphClient, transport: FakeTransport):
    transport.warteschlange.append(ok({"displayName": "Frau Beispiel"}))
    daten = client.get("/me", params={"$select": "displayName"})
    assert daten == {"displayName": "Frau Beispiel"}
    anfrage = transport.letzte()
    assert anfrage.url == f"{graph.GRAPH_BASIS}/me"
    assert anfrage.headers["Authorization"] == "Bearer TOKEN" and anfrage.headers["Accept"] == "application/json"
    assert anfrage.params == {"$select": "displayName"}
    # absolute URLs (nextLink, uploadUrl) bleiben unverändert
    transport.warteschlange.append(ok({"value": []}))
    client.get("https://graph.microsoft.com/v1.0/me/messages?$skiptoken=abc")
    assert transport.letzte().url.endswith("$skiptoken=abc")


def test_client_liefert_rohdaten_und_leeren_body(client: GraphClient, transport: FakeTransport):
    transport.warteschlange.append((200, b"%PDF-1.4", {"Content-Type": "application/pdf"}))
    assert client.anfrage("GET", "/x", roh=True).daten == b"%PDF-1.4"
    transport.warteschlange.append((204, b"", {}))
    assert client.post("/x/closeSession", json={}) == b""
    transport.warteschlange.append((200, b"", {"Content-Type": "application/json"}))
    assert client.get("/leer") == {}


def test_ratenbegrenzung_wartet_zwischen_anfragen(transport: FakeTransport, uhr: FakeUhr):
    client = GraphClient(lambda: "T", sende=transport, anfragen_pro_sekunde=2, schlaf=uhr.schlaf, uhr=uhr)
    transport.warteschlange.extend([ok({}), ok({}), ok({})])
    client.get("/a")
    uhr.zeit += 0.1
    client.get("/b")
    uhr.zeit += 1.0
    client.get("/c")
    assert uhr.pausen == [0.4]  # nur zwischen a und b musste gewartet werden


def test_wiederholung_bei_429_mit_retry_after(client: GraphClient, transport: FakeTransport, uhr: FakeUhr):
    transport.warteschlange.extend([fehler(429, "TooManyRequests", **{"Retry-After": "3"}), ok({"id": 1})])
    assert client.get("/x") == {"id": 1}
    assert uhr.pausen == [3.0] and len(transport.anfragen) == 2


def test_wiederholung_bei_503_und_504_exponentiell(client: GraphClient, transport: FakeTransport, uhr: FakeUhr):
    transport.warteschlange.extend([fehler(503, "ServiceUnavailable"), fehler(504, "GatewayTimeout", "gatewayTimeoutUncategorized"), ok({"id": 2})])
    assert client.post("/x/createSession", json={"persistChanges": True}) == {"id": 2}
    assert uhr.pausen == [2.0, 4.0] and len(transport.anfragen) == 3


def test_wiederholung_bricht_nach_max_versuchen_ab(transport: FakeTransport, uhr: FakeUhr):
    client = GraphClient(lambda: "T", sende=transport, anfragen_pro_sekunde=0, max_versuche=3, schlaf=uhr.schlaf, uhr=uhr)
    transport.warteschlange.extend([fehler(429, "TooManyRequests")] * 5)
    with pytest.raises(M365Fehler) as info:
        client.get("/x")
    assert info.value.status == 429 and len(transport.anfragen) == 3 and uhr.pausen == [2.0, 4.0]


def test_konflikt_wird_nicht_wiederholt(client: GraphClient, transport: FakeTransport, uhr: FakeUhr):
    transport.warteschlange.append(fehler(409, "Conflict", "accessConflict", **{"Retry-After": "120"}))
    with pytest.raises(M365Konflikt) as info:
        client.patch("/x", json={})
    assert info.value.wartezeit_sekunden == 120 and info.value.inner_code == "accessConflict"
    assert len(transport.anfragen) == 1 and uhr.pausen == []
    transport.warteschlange.append(fehler(423, "resourceLocked"))
    with pytest.raises(M365Konflikt) as info2:
        client.put("/y", data=b"x")
    assert info2.value.wartezeit_sekunden == 600


def test_fehlerabbildung_404_401_403(client: GraphClient, transport: FakeTransport):
    transport.warteschlange.append(fehler(404, "itemNotFound"))
    with pytest.raises(M365NichtGefunden):
        client.get("/x")
    transport.warteschlange.append(fehler(401, "InvalidAuthenticationToken"))
    with pytest.raises(M365NichtAngemeldet):
        client.get("/x")
    transport.warteschlange.append(fehler(403, "accessDenied"))
    with pytest.raises(M365Fehler) as info:
        client.get("/x")
    assert type(info.value) is M365Fehler and info.value.status == 403 and "Meldung zu accessDenied" in str(info.value)


def test_seiten_folgt_nextlink(client: GraphClient, transport: FakeTransport):
    transport.warteschlange.extend([
        ok({"value": [1, 2], "@odata.nextLink": "https://graph.microsoft.com/v1.0/x?$skiptoken=s"}),
        ok({"value": [3]}),
    ])
    assert list(client.seiten("/x", params={"$top": "2"})) == [1, 2, 3]
    assert transport.anfragen[0].params == {"$top": "2"} and transport.anfragen[1].params is None
    transport.warteschlange.append(ok({"value": [1, 2, 3], "@odata.nextLink": "https://x/weiter"}))
    assert list(client.seiten("/x", maximal=2)) == [1, 2] and len(transport.anfragen) == 3


# ---------------------------------------------------------------- Excel


def test_excel_sitzung_header_und_schliessen(client: GraphClient, transport: FakeTransport):
    excel = GraphExcelBackend(client)
    transport.regel("POST", r"/createSession$", ok({"id": "SITZUNG-1", "persistChanges": True}, 201))
    transport.regel("GET", r"/worksheets$", ok({"value": [{"name": "Fixterminüberwachung"}, {"name": "Ressourcen für Auto DropDown"}]}))
    transport.regel("POST", r"/closeSession$", (204, b"", {}))
    with excel.sitzung(REF):
        with excel.sitzung(REF):  # verschachtelt: keine zweite Session
            assert excel.blattnamen(REF) == ["Fixterminüberwachung", "Ressourcen für Auto DropDown"]
    pfade = transport.methoden_und_pfade()
    assert pfade[0] == ("POST", f"{MAPPE}/createSession") and transport.anfragen[0].json == {"persistChanges": True}
    assert pfade[1][1].startswith(f"{MAPPE}/worksheets") and transport.anfragen[1].headers["workbook-session-id"] == "SITZUNG-1"
    assert pfade[2] == ("POST", f"{MAPPE}/closeSession") and transport.anfragen[2].headers["workbook-session-id"] == "SITZUNG-1"
    assert len(pfade) == 3


def test_excel_lese_bereich_kodiert_blatt_und_adresse(client: GraphClient, transport: FakeTransport):
    excel = GraphExcelBackend(client)
    transport.regel("GET", r"/range\(", ok({"address": "Fixterminüberwachung!D7:K7", "values": [["Cide", "Rheinberg", 46313, "", True, False, False, "AB 1"]]}))
    werte = excel.lese_bereich(REF, "Fixterminüberwachung", "D7:K7")
    assert werte == [["Cide", "Rheinberg", 46313, "", True, False, False, "AB 1"]]
    assert transport.letzte().pfad == f"{MAPPE}/worksheets/Fixtermin%C3%BCberwachung/range(address='D7:K7')"
    assert "workbook-session-id" not in transport.letzte().headers  # sessionless erlaubt
    excel.lese_bereich(REF, "Ressourcen für Auto DropDown", "'Ressourcen für Auto DropDown'!A1:B2")
    assert transport.letzte().pfad == (
        f"{MAPPE}/worksheets/Ressourcen%20f%C3%BCr%20Auto%20DropDown/"
        "range(address=''Ressourcen%20f%C3%BCr%20Auto%20DropDown'!A1:B2')"
    )


def test_excel_lese_benutzten_bereich(client: GraphClient, transport: FakeTransport):
    excel = GraphExcelBackend(client)
    transport.regel("GET", r"/usedRange\(valuesOnly=true\)$", ok({"address": "Aktionen!A1:C3", "values": [["Kunde", "Von", "Bis"], ["Spar", 46300, 46330], ["", "", ""]]}))
    adresse, werte = excel.lese_benutzten_bereich(REF, "Aktionen")
    assert adresse == "Aktionen!A1:C3" and werte[1] == ["Spar", 46300, 46330]
    assert transport.letzte().params == {"$select": "address,values"}


def test_excel_schreibe_bereich_patch_body(client: GraphClient, transport: FakeTransport):
    excel = GraphExcelBackend(client)
    transport.regel("PATCH", r"/range\(", ok({"address": "x"}))
    excel.schreibe_bereich(
        REF, "Fixterminüberwachung", "D7:K7",
        [["Cide", "Rheinberg", date(2026, 10, 18), None, True, False, Decimal("12.50"), Decimal("3")]],
    )
    anfrage = transport.letzte("PATCH")
    assert anfrage.pfad == f"{MAPPE}/worksheets/Fixtermin%C3%BCberwachung/range(address='D7:K7')"
    assert anfrage.json == {
        "values": [["Cide", "Rheinberg", 46313.0, None, True, False, 12.5, 3]],
        "numberFormat": [[None, None, "DD.MM.YYYY", None, None, None, None, None]],
    }
    assert anfrage.headers["Content-Type"] == "application/json"


def test_excel_schreibe_bereich_ohne_datum_ohne_numberformat_und_mit_eigenem_format(client: GraphClient, transport: FakeTransport):
    excel = GraphExcelBackend(client)
    transport.regel("PATCH", r"/range\(", ok({}))
    excel.schreibe_bereich(REF, "Blatt", "A1:B1", [["x", ""]])
    assert transport.letzte("PATCH").json == {"values": [["x", ""]]}
    excel.schreibe_bereich(REF, "Blatt", "A1:B2", [[datetime(2026, 1, 2, 8, 30), 1], [None, None]], zahlenformat=[["TT.MM.JJJJ", None], [None, "0.00"]])
    body = transport.letzte("PATCH").json
    assert body["values"] == [[pytest.approx(46024.354166667), 1], [None, None]]
    assert body["numberFormat"] == [["TT.MM.JJJJ", None], [None, "0.00"]]


def test_excel_schreibe_bereich_prueft_form(client: GraphClient, transport: FakeTransport):
    excel = GraphExcelBackend(client)
    with pytest.raises(ValueError, match="Bereich"):
        excel.schreibe_bereich(REF, "Blatt", "D7:K7", [["nur", "zwei"]])
    with pytest.raises(ValueError, match="zahlenformat"):
        excel.schreibe_bereich(REF, "Blatt", "A1:B1", [["a", "b"]], zahlenformat=[["x"]])
    transport.regel("PATCH", r"/range\(", ok({}))
    excel.schreibe_bereich(REF, "Blatt", "D7:K7", [["alle gleich"]])  # single input copy
    assert transport.letzte("PATCH").json == {"values": [["alle gleich"]]}
    assert transport.anfragen  # keine Anfrage für die fehlerhaften Aufrufe davor
    assert len(transport.anfragen) == 1


def test_excel_session_wird_bei_invalid_session_erneuert(client: GraphClient, transport: FakeTransport):
    excel = GraphExcelBackend(client)
    sitzungen = iter(["S1", "S2"])
    transport.regel("POST", r"/createSession$", lambda a: ok({"id": next(sitzungen)}, 201))
    transport.regel("POST", r"/closeSession$", (204, b"", {}))
    patch_antworten = [fehler(404, "InvalidSession", "invalidSessionNotFound"), ok({"address": "ok"})]
    transport.regel("PATCH", r"/range\(", lambda a: patch_antworten.pop(0))
    with excel.sitzung(REF):
        excel.schreibe_bereich(REF, "Blatt", "A1", [["x"]])
    pfade = [p for _, p in transport.methoden_und_pfade()]
    assert [p.rsplit("/", 1)[-1] for p in pfade] == ["createSession", "range(address='A1')", "createSession", "range(address='A1')", "closeSession"]
    assert transport.anfragen[1].headers["workbook-session-id"] == "S1"
    assert transport.anfragen[3].headers["workbook-session-id"] == "S2"
    assert transport.anfragen[4].headers["workbook-session-id"] == "S2"


def test_excel_invalid_session_access_conflict_wird_nicht_erneuert(client: GraphClient, transport: FakeTransport):
    excel = GraphExcelBackend(client)
    transport.regel("POST", r"/createSession$", ok({"id": "S1"}, 201))
    transport.regel("POST", r"/closeSession$", (204, b"", {}))
    transport.regel("PATCH", r"/range\(", fehler(409, "InvalidSession", "invalidSessionAccessConflict"))
    with pytest.raises(M365Konflikt), excel.sitzung(REF):
        excel.schreibe_bereich(REF, "Blatt", "A1", [["x"]])
    assert [p.rsplit("/", 1)[-1] for _, p in transport.methoden_und_pfade()] == ["createSession", "range(address='A1')", "closeSession"]


def test_excel_ohne_session_wird_invalid_session_nicht_erneuert(client: GraphClient, transport: FakeTransport):
    excel = GraphExcelBackend(client)
    transport.regel("GET", r"/range\(", fehler(404, "ItemNotFound", "itemNotFound"))
    with pytest.raises(M365NichtGefunden):
        excel.lese_bereich(REF, "Blatt", "A1")
    assert len(transport.anfragen) == 1


def test_excel_loest_web_url_einmal_auf(client: GraphClient, transport: FakeTransport):
    ref = DateiRefKonfig(web_url="https://contoso.sharepoint.com/sites/Buchhaltung/Freigegebene%20Dokumente/General/Datei.xlsx?web=1")
    transport.regel("GET", r"/shares/u!", ok({"id": "ITEM-X", "parentReference": {"driveId": "DRIVE-X"}}))
    transport.regel("GET", r"/worksheets$", ok({"value": [{"name": "A"}]}))
    excel = GraphExcelBackend(client)
    assert excel.blattnamen(ref) == ["A"]
    assert excel.blattnamen(ref) == ["A"]
    pfade = transport.methoden_und_pfade()
    assert pfade[0][1].startswith("/shares/u!") and transport.anfragen[0].headers["Prefer"] == "redeemSharingLinkIfNecessary"
    assert pfade[1][1].startswith("/drives/DRIVE-X/items/ITEM-X/workbook/worksheets")
    assert sum(1 for _, p in pfade if p.startswith("/shares")) == 1
    with pytest.raises(M365Fehler, match="web_url"):
        excel.blattnamen(DateiRefKonfig(lokaler_pfad=Path("/x.xlsx")))


# ---------------------------------------------------------------- Mail


def entwurf(tmp_path: Path, *anhaenge: Path) -> EmailEntwurf:
    return EmailEntwurf(
        typ=EntwurfTyp.BESTELLUNG_WEINGUT,
        an=["orders@example.org"],
        cc=["kollegin@example.org"],
        betreff="New order B-4711 – Beispielkunde",
        text_html="<p>Dear Ms Beispiel,</p><p>please find attached our order.</p>",
        text_plain="Dear Ms Beispiel,\n\nplease find attached our order.",
        anhaenge=list(anhaenge),
    )


def test_mail_entwurf_mit_kleinem_anhang(client: GraphClient, transport: FakeTransport, tmp_path: Path):
    pdf = tmp_path / "Beispiel – WIK – B-4711 – Cde – 18.10.2026.pdf"
    pdf.write_bytes(b"%PDF-1.4 klein")
    transport.regel("POST", r"/me/messages$", ok({"id": "MSG-1", "isDraft": True, "webLink": "https://outlook.example/x"}, 201))
    transport.regel("POST", r"/me/messages/MSG-1/attachments$", ok({"id": "ATT-1"}, 201))
    mail = GraphMailBackend(client)
    e = entwurf(tmp_path, pdf)
    assert mail.erstelle_entwurf(e) == "MSG-1"
    assert e.speicherort == "MSG-1" and e.erstellt_am is not None
    erstellen = transport.anfragen[0]
    assert erstellen.json["subject"] == e.betreff
    assert erstellen.json["body"] == {"contentType": "HTML", "content": e.text_html}
    assert erstellen.json["toRecipients"] == [{"emailAddress": {"address": "orders@example.org"}}]
    assert erstellen.json["ccRecipients"] == [{"emailAddress": {"address": "kollegin@example.org"}}]
    assert erstellen.json["internetMessageHeaders"] == [{"name": "x-auftragsautomat-typ", "value": "bestellung_weingut"}]
    assert "send" not in " ".join(p for _, p in transport.methoden_und_pfade())
    anhang = transport.anfragen[1].json
    assert anhang["@odata.type"] == "#microsoft.graph.fileAttachment" and anhang["name"] == pdf.name
    assert anhang["contentType"] == "application/pdf"
    assert base64.b64decode(anhang["contentBytes"]) == b"%PDF-1.4 klein"


def test_mail_entwurf_grosser_anhang_per_upload_session(client: GraphClient, transport: FakeTransport, tmp_path: Path):
    gross = tmp_path / "scan.pdf"
    gross.write_bytes(b"x" * (ANHANG_KLEIN_MAX + 10))
    transport.regel("POST", r"/users/logistik@example.org/messages$", ok({"id": "MSG-2"}, 201))
    transport.regel("POST", r"/attachments/createUploadSession$", ok({"uploadUrl": "https://outlook.office.com/upload/abc", "nextExpectedRanges": ["0-"]}, 201))
    puts: list[Anfrage] = []

    def put_antwort(anfrage: Anfrage) -> Antwort:
        puts.append(anfrage)
        start, ende, gesamt = re.match(r"bytes (\d+)-(\d+)/(\d+)", anfrage.headers["Content-Range"]).groups()
        if int(ende) + 1 >= int(gesamt):
            return 201, b"", {"Location": "https://outlook.office.com/attachments/ATT-2"}
        return ok({"nextExpectedRanges": [f"{int(ende) + 1}-"]})

    transport.regel("PUT", r"^https://outlook.office.com/upload/abc$", put_antwort)
    mail = GraphMailBackend(client, postfach="logistik@example.org")
    mail.erstelle_entwurf(entwurf(tmp_path, gross))
    sitzung = transport.anfragen[1]
    assert sitzung.json == {"AttachmentItem": {"attachmentType": "file", "name": "scan.pdf", "size": ANHANG_KLEIN_MAX + 10}}
    assert len(puts) == 1 and all("Authorization" not in p.headers for p in puts)
    assert puts[0].headers["Content-Range"] == f"bytes 0-{ANHANG_KLEIN_MAX + 9}/{ANHANG_KLEIN_MAX + 10}"
    assert puts[0].headers["Content-Length"] == str(ANHANG_KLEIN_MAX + 10) and len(puts[0].data) == ANHANG_KLEIN_MAX + 10
    assert puts[0].headers["Content-Type"] == "application/octet-stream"


def test_upload_chunks_in_reihenfolge_und_kleiner_4mb(client: GraphClient, transport: FakeTransport):
    daten = bytes(range(256)) * 20000  # ~5 MB -> zwei Chunks
    puts: list[Anfrage] = []

    def put_antwort(anfrage: Anfrage) -> Antwort:
        puts.append(anfrage)
        start, ende, gesamt = (int(x) for x in re.match(r"bytes (\d+)-(\d+)/(\d+)", anfrage.headers["Content-Range"]).groups())
        assert anfrage.data == daten[start : ende + 1]
        if ende + 1 >= gesamt:
            return ok({"id": "ITEM-NEU"}, 201)
        return ok({"nextExpectedRanges": [f"{ende + 1}-{gesamt - 1}"]})

    transport.regel("PUT", r"^https://up/x$", put_antwort)
    ergebnis = graph._chunks_hochladen(client, "https://up/x", daten)
    assert ergebnis["id"] == "ITEM-NEU"
    assert [p.headers["Content-Range"] for p in puts] == [f"bytes 0-{UPLOAD_CHUNK - 1}/{len(daten)}", f"bytes {UPLOAD_CHUNK}-{len(daten) - 1}/{len(daten)}"]
    assert all(len(p.data) < 4 * 1024 * 1024 for p in puts) and UPLOAD_CHUNK % (320 * 1024) == 0


def test_mail_entwurf_fehlender_anhang_wird_vorher_abgelehnt(client: GraphClient, transport: FakeTransport, tmp_path: Path):
    with pytest.raises(M365Fehler, match="Anhang nicht gefunden"):
        GraphMailBackend(client).erstelle_entwurf(entwurf(tmp_path, tmp_path / "fehlt.pdf"))
    assert transport.anfragen == []


def test_mail_suche_mit_filter_und_orderby(client: GraphClient, transport: FakeTransport):
    nachrichten = [
        {"id": "M1", "subject": "Auftragsbestätigung; Your Ref: B-4711; Our Ref: DEMB00000", "from": {"emailAddress": {"address": "orders@example.org", "name": "Spedition Beispiel"}}, "receivedDateTime": "2026-10-18T10:00:00.1234567Z", "hasAttachments": True},
        {"id": "M2", "subject": "Zweite", "from": {"emailAddress": {"address": "orders@example.org"}}, "receivedDateTime": "2026-10-17T09:00:00Z", "hasAttachments": False},
    ]
    transport.regel("GET", r"/mailFolders/inbox/messages", ok({"value": nachrichten}))
    mail = GraphMailBackend(client)
    seit = datetime(2026, 10, 11, 0, 0, tzinfo=UTC)
    koepfe = mail.suche_mails(seit=seit, absender="Orders@Example.org", maximal=10)
    anfrage = transport.letzte()
    assert anfrage.params["$filter"] == "receivedDateTime ge 2026-10-11T00:00:00Z and from/emailAddress/address eq 'Orders@Example.org'"
    assert anfrage.params["$orderby"] == "receivedDateTime desc" and anfrage.params["$top"] == "10"
    assert "$search" not in anfrage.params and anfrage.headers["Prefer"] == 'IdType="ImmutableId"'
    assert [k.id for k in koepfe] == ["M1", "M2"]
    assert koepfe[0].empfangen == datetime(2026, 10, 18, 10, 0, 0, 123456, tzinfo=UTC)
    assert koepfe[0].absender_name == "Spedition Beispiel" and koepfe[0].hat_anhaenge and koepfe[0].ordner == "inbox"
    assert "DEMB00000" in koepfe[0].betreff


def test_mail_suche_mit_search_filtert_clientseitig(client: GraphClient, transport: FakeTransport):
    nachrichten = [
        {"id": "M1", "subject": "Our Ref: DEMB00000", "from": {"emailAddress": {"address": "a@example.org"}}, "receivedDateTime": "2026-10-18T10:00:00Z"},
        {"id": "M2", "subject": "Our Ref: DEMB00001", "from": {"emailAddress": {"address": "b@example.org"}}, "receivedDateTime": "2026-10-18T11:00:00Z"},
        {"id": "M3", "subject": "Our Ref: DEMB00002", "from": {"emailAddress": {"address": "a@example.org"}}, "receivedDateTime": "2026-09-01T11:00:00Z"},
    ]
    transport.regel("GET", r"/mailFolders/Spedition/messages", ok({"value": nachrichten}))
    koepfe = GraphMailBackend(client).suche_mails(seit=datetime(2026, 10, 11, tzinfo=UTC), absender="a@example.org", betreff_enthaelt='Our "Ref"', ordner="Spedition")
    params = transport.letzte().params
    assert params["$search"] == '"subject:Our \\"Ref\\""' and "$orderby" not in params and "$filter" not in params
    assert [k.id for k in koepfe] == ["M1"] and koepfe[0].ordner == "Spedition"


def test_mail_suche_naive_zeit_wird_als_lokal_interpretiert(client: GraphClient, transport: FakeTransport):
    transport.regel("GET", r"/messages", ok({"value": []}))
    GraphMailBackend(client).suche_mails(seit=datetime(2026, 10, 11, 8, 0))
    erwartet = datetime(2026, 10, 11, 8, 0).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert transport.letzte().params["$filter"] == f"receivedDateTime ge {erwartet}"


def test_mail_lese_mail_und_anhaenge(client: GraphClient, transport: FakeTransport):
    transport.regel("GET", r"/me/messages/M1$", ok({
        "id": "M1", "subject": "Betreff", "from": {"emailAddress": {"address": "a@example.org"}},
        "receivedDateTime": "2026-10-18T10:00:00Z", "hasAttachments": True,
        "body": {"contentType": "html", "content": "<html><body><p>Hallo Frau Beispiel,</p><p>Our Ref: DEMB00000</p></body></html>"},
        "toRecipients": [{"emailAddress": {"address": "logistik@example.org"}}],
        "ccRecipients": [{"emailAddress": {"address": "kollegin@example.org"}}, {"emailAddress": {}}],
    }))
    transport.regel("GET", r"/me/messages/M1/attachments", ok({"value": [
        {"id": "A1", "name": "Bestätigung.pdf", "size": 1234, "contentType": "application/pdf", "isInline": False},
        {"id": "A2", "name": "logo.png", "size": 10, "contentType": "image/png", "isInline": True},
    ]}))
    m = GraphMailBackend(client).lese_mail("M1")
    assert m.kopf.id == "M1" and m.html and m.html.startswith("<html>")
    assert m.text == "Hallo Frau Beispiel,\n\nOur Ref: DEMB00000"
    assert m.an == ["logistik@example.org"] and m.cc == ["kollegin@example.org"]
    assert [(a.id, a.name, a.inline) for a in m.anhaenge] == [("A1", "Bestätigung.pdf", False), ("A2", "logo.png", True)]
    assert transport.anfragen[0].params["$select"].startswith("id,subject,from") and "body" in transport.anfragen[0].params["$select"]


def test_mail_speichere_anhang(client: GraphClient, transport: FakeTransport, tmp_path: Path):
    transport.regel("GET", r"/attachments/A1/\$value$", (200, b"%PDF-roh", {"Content-Type": "application/pdf"}))
    transport.regel("GET", r"/attachments/A1$", ok({"name": "Best/ätigung.pdf"}))
    mail = GraphMailBackend(client)
    ordner = tmp_path / "anhaenge"
    ordner.mkdir()
    pfad = mail.speichere_anhang("M1", "A1", ordner)
    assert pfad == ordner / "Best_ätigung.pdf" and pfad.read_bytes() == b"%PDF-roh"
    datei = mail.speichere_anhang("M1", "A1", tmp_path / "neu" / "x.pdf")
    assert datei.read_bytes() == b"%PDF-roh"
    assert transport.anfragen[0].pfad == "/me/messages/M1/attachments/A1" and transport.anfragen[-1].pfad.endswith("/A1/$value")


# ---------------------------------------------------------------- Dateien

DREF = DateiRefKonfig(drive_id="b!drive", item_id="01ORDNER")
WURZEL = "/drives/b!drive/items/01ORDNER"


def test_datei_existiert_und_pfadkodierung(client: GraphClient, transport: FakeTransport):
    transport.regel("GET", rf"{re.escape(WURZEL)}:/Dossiers/Auftr%C3%A4ge%202026/Deckblatt%23.pdf$", ok({"id": "X"}))
    transport.regel("GET", r".*", fehler(404, "itemNotFound"))
    datei = GraphDateiBackend(client)
    assert datei.existiert(DREF, "Dossiers/Aufträge 2026/Deckblatt#.pdf")
    assert transport.letzte().params == {"$select": "id,name"}
    assert not datei.existiert(DREF, "gibt/es/nicht")


def test_datei_ordner_anlegen_idempotent_und_verschachtelt(client: GraphClient, transport: FakeTransport):
    vorhanden = {"": {"id": "01ORDNER", "folder": {}}, "Aufträge ohne Provision": {"id": "F1", "name": "Aufträge ohne Provision", "folder": {}}}

    def rel_pfad(anfrage: Anfrage) -> str:
        return adressierung.unquote(anfrage.pfad.replace(WURZEL, "").replace(":/children", "").replace("/children", "").lstrip(":/"))

    def get(anfrage: Anfrage) -> Antwort:
        rel = rel_pfad(anfrage)
        if rel in vorhanden:
            return ok(vorhanden[rel])
        return fehler(404, "itemNotFound")

    def post(anfrage: Anfrage) -> Antwort:
        assert anfrage.json["folder"] == {} and anfrage.json["@microsoft.graph.conflictBehavior"] == "fail"
        name = anfrage.json["name"]
        eltern = rel_pfad(anfrage)
        rel = f"{eltern}/{name}" if eltern else name
        vorhanden[rel] = {"id": f"ID-{name}", "name": name, "folder": {}}
        if rel == "Aufträge ohne Provision/WIK":
            return fehler(409, "nameAlreadyExists")  # Wettlauf: ein Kollege hat den Ordner gerade angelegt
        return ok(vorhanden[rel], 201)

    transport.regel("GET", r".*", get)
    transport.regel("POST", r"/children$", post)
    datei = GraphDateiBackend(client)
    assert datei.ordner_anlegen(DREF, "Aufträge ohne Provision/WIK/2026") == "ID-2026"
    posts = [a.json["name"] for a in transport.anfragen if a.methode == "POST"]
    assert posts == ["WIK", "2026"]  # "Aufträge ohne Provision" existierte bereits; nach 409 wurde WIK per GET geholt
    gets = [rel_pfad(a) for a in transport.anfragen if a.methode == "GET"]
    assert gets.count("Aufträge ohne Provision/WIK") == 2
    assert datei.ordner_anlegen(DREF, "") == "01ORDNER"
    assert datei.ordner_anlegen(DREF, "Aufträge ohne Provision/WIK/2026") == "ID-2026"  # idempotent, keine weiteren POSTs
    assert len([a for a in transport.anfragen if a.methode == "POST"]) == 2


def test_datei_ordner_anlegen_lehnt_datei_ab(client: GraphClient, transport: FakeTransport):
    transport.regel("GET", r"items/01ORDNER\?", ok({"id": "01ORDNER"}))
    transport.regel("GET", r":/Deckblatt.pdf", ok({"id": "D", "name": "Deckblatt.pdf", "file": {}}))
    with pytest.raises(M365Fehler, match="als Datei"):
        GraphDateiBackend(client).ordner_anlegen(DREF, "Deckblatt.pdf")


def test_datei_liste(client: GraphClient, transport: FakeTransport):
    transport.regel("GET", r":/Dossiers:/children$", ok({"value": [
        {"id": "A", "name": "Beispiel – WIK – B-4711", "folder": {"childCount": 3}, "lastModifiedDateTime": "2026-10-18T10:00:00Z"},
        {"id": "B", "name": "index.json", "file": {}, "size": 42},
    ]}))
    eintraege = GraphDateiBackend(client).liste(DREF, "Dossiers/")
    assert [(e.name, e.pfad, e.ist_ordner, e.groesse, e.id) for e in eintraege] == [
        ("Beispiel – WIK – B-4711", "Dossiers/Beispiel – WIK – B-4711", True, None, "A"),
        ("index.json", "Dossiers/index.json", False, 42, "B"),
    ]
    assert eintraege[0].geaendert == datetime(2026, 10, 18, 10, tzinfo=UTC)
    assert transport.letzte().params["$select"].startswith("id,name,size,folder")


def test_datei_hochladen_klein_und_gross(client: GraphClient, transport: FakeTransport, tmp_path: Path):
    klein = tmp_path / "Kd.pdf"
    klein.write_bytes(b"%PDF-klein")
    transport.regel("PUT", r":/Auftr%C3%A4ge%20ohne%20Provision/Kd.pdf:/content$", ok({"id": "ITEM-KLEIN"}, 201))
    datei = GraphDateiBackend(client)
    assert datei.hochladen(klein, DREF, "Aufträge ohne Provision/") == "ITEM-KLEIN"
    put = transport.letzte("PUT")
    assert put.data == b"%PDF-klein" and put.params == {"@microsoft.graph.conflictBehavior": "replace"}
    assert put.headers["Content-Type"] == "application/pdf" and put.headers["Authorization"] == "Bearer TOKEN"

    gross = tmp_path / "Scan.pdf"
    gross.write_bytes(b"g" * (DATEI_KLEIN_MAX + 1))
    transport.regel("POST", r":/Dossiers/Scan.pdf:/createUploadSession$", ok({"uploadUrl": "https://up/sess"}))
    puts: list[Anfrage] = []

    def put_antwort(anfrage: Anfrage) -> Antwort:
        puts.append(anfrage)
        ende = int(re.match(r"bytes \d+-(\d+)/", anfrage.headers["Content-Range"]).group(1))
        return ok({"id": "ITEM-GROSS"}, 201) if ende + 1 >= DATEI_KLEIN_MAX + 1 else ok({"nextExpectedRanges": [f"{ende + 1}-"]})

    transport.regel("PUT", r"^https://up/sess$", put_antwort)
    assert datei.hochladen(gross, DREF, "Dossiers/Scan.pdf") == "ITEM-GROSS"
    sitzung = transport.letzte("POST")
    assert sitzung.json == {"item": {"@microsoft.graph.conflictBehavior": "replace", "name": "Scan.pdf"}}
    assert len(puts) == 2 and all("Authorization" not in p.headers for p in puts)
    assert sum(len(p.data) for p in puts) == DATEI_KLEIN_MAX + 1


def test_datei_herunterladen_und_text(client: GraphClient, transport: FakeTransport, tmp_path: Path):
    transport.regel("GET", r":/Dossiers/index.json:/content$", (200, b'{"a": 1}', {"Content-Type": "application/json"}))
    transport.regel("PUT", r":/Dossiers/07_Fixtermin.txt:/content$", ok({"id": "TXT"}))
    datei = GraphDateiBackend(client)
    ziel = datei.herunterladen(DREF, "Dossiers/index.json", tmp_path)
    assert ziel == tmp_path / "index.json" and ziel.read_bytes() == b'{"a": 1}'
    assert "Accept" not in transport.letzte("GET").headers
    assert datei.schreibe_text(DREF, "Dossiers/07_Fixtermin.txt", "KW 44 | Cide | Beispiel ä") == "TXT"
    put = transport.letzte("PUT")
    assert put.data == "KW 44 | Cide | Beispiel ä".encode() and put.headers["Content-Type"] == "text/plain; charset=utf-8"
    with pytest.raises(M365Fehler, match="Lokale Datei fehlt"):
        datei.hochladen(tmp_path / "nix.pdf", DREF, "x/")


# ---------------------------------------------------------------- Adressierung


def test_kodiere_sharing_url_beispiel_aus_graph_doku():
    assert adressierung.kodiere_sharing_url("https://onedrive.live.com/redir?resid=1231244193912!12&authKey=1201919!12921!1") == (
        "u!aHR0cHM6Ly9vbmVkcml2ZS5saXZlLmNvbS9yZWRpcj9yZXNpZD0xMjMxMjQ0MTkzOTEyITEyJmF1dGhLZXk9MTIwMTkxOSExMjkyMSEx"
    )
    token = adressierung.kodiere_sharing_url("https://contoso.sharepoint.com/sites/Buchhaltung/Freigegebene%20Dokumente/General/Auftr%C3%A4ge/Fixtermin%C3%BCberwachung.xlsx?web=1")
    assert token.startswith("u!") and "=" not in token and "/" not in token and "+" not in token


def test_kodiere_pfad():
    assert adressierung.kodiere_pfad("/General/Aufträge/Fixterminüberwachung.xlsx") == "General/Auftr%C3%A4ge/Fixtermin%C3%BCberwachung.xlsx"
    assert adressierung.kodiere_pfad("A B/C#D/bereits%20kodiert") == "A%20B/C%23D/bereits%20kodiert"


@pytest.mark.parametrize(
    "url,hostname,site,pfad",
    [
        ("https://contoso.sharepoint.com/sites/Buchhaltung/Freigegebene%20Dokumente/General/Auftr%C3%A4ge/Datei.xlsx?web=1", "contoso.sharepoint.com", "/sites/Buchhaltung", "Freigegebene Dokumente/General/Aufträge/Datei.xlsx"),
        ("https://contoso.sharepoint.com/:x:/r/sites/Buchhaltung/Freigegebene%20Dokumente/Datei.xlsx?d=w1&csf=1&web=1", "contoso.sharepoint.com", "/sites/Buchhaltung", "Freigegebene Dokumente/Datei.xlsx"),
        ("https://contoso-my.sharepoint.com/personal/frau_beispiel_example_org/Documents/Ordner/", "contoso-my.sharepoint.com", "/personal/frau_beispiel_example_org", "Documents/Ordner"),
        ("https://contoso.sharepoint.com/Shared%20Documents/x.xlsx", "contoso.sharepoint.com", "", "Shared Documents/x.xlsx"),
        ("https://contoso.sharepoint.com/teams/Logistik", "contoso.sharepoint.com", "/teams/Logistik", ""),
    ],
)
def test_zerlege_web_url(url: str, hostname: str, site: str, pfad: str):
    teile = adressierung.zerlege_web_url(url)
    assert (teile.hostname, teile.site_pfad, teile.pfad) == (hostname, site, pfad)
    assert teile.site_url == f"https://{hostname}{site}"
    with pytest.raises(M365Fehler):
        adressierung.zerlege_web_url("kein-url")


def test_aufloesen_ueber_site(client: GraphClient, transport: FakeTransport):
    transport.regel("GET", r"/sites/contoso.sharepoint.com:/sites/Buchhaltung$", ok({"id": "contoso.sharepoint.com,1,2"}))
    transport.regel("GET", r"/sites/contoso.sharepoint.com,1,2/drives$", ok({"value": [
        {"id": "DRV-A", "name": "Sonstiges", "webUrl": "https://contoso.sharepoint.com/sites/Buchhaltung/Sonstiges"},
        {"id": "DRV-B", "name": "Dokumente", "webUrl": "https://contoso.sharepoint.com/sites/Buchhaltung/Freigegebene%20Dokumente"},
    ]}))
    transport.regel("GET", r"/drives/DRV-B/root:/General/Auftr%C3%A4ge/Fixtermin%C3%BCberwachung.xlsx$", ok({"id": "ITEM-FIX"}))
    url = "https://contoso.sharepoint.com/sites/Buchhaltung/Freigegebene%20Dokumente/General/Auftr%C3%A4ge/Fixtermin%C3%BCberwachung.xlsx?web=1"
    assert adressierung.aufloesen_ueber_site(client, url) == ("DRV-B", "ITEM-FIX")
    transport.regel("GET", r"/drives/DRV-B/root$", ok({"id": "ROOT-B"}))
    assert adressierung.aufloesen_ueber_site(client, "https://contoso.sharepoint.com/sites/Buchhaltung/Freigegebene%20Dokumente") == ("DRV-B", "ROOT-B")
    with pytest.raises(M365NichtGefunden, match="Dokumentbibliothek"):
        adressierung.aufloesen_ueber_site(client, "https://contoso.sharepoint.com/sites/Buchhaltung/Andere/x.xlsx")


def test_aufloesen_idempotent_shares_und_rueckfall(client: GraphClient, transport: FakeTransport):
    fertig = DateiRefKonfig(drive_id="D", item_id="I", web_url="https://x/y")
    assert adressierung.aufloesen(fertig, client) == fertig and transport.anfragen == []
    with pytest.raises(M365Fehler, match="web_url"):
        adressierung.aufloesen(DateiRefKonfig(), client)

    ref = DateiRefKonfig(web_url="https://contoso.sharepoint.com/sites/Buchhaltung/Freigegebene%20Dokumente/General/Datei.xlsx", blatt="Aktionen")
    transport.regel("GET", r"/shares/", ok({"id": "ITEM-S", "parentReference": {"driveId": "DRV-S"}}))
    ergebnis = adressierung.aufloesen(ref, client)
    assert (ergebnis.drive_id, ergebnis.item_id, ergebnis.blatt, ergebnis.web_url) == ("DRV-S", "ITEM-S", "Aktionen", ref.web_url)
    assert ref.drive_id is None  # Original unverändert

    transport.regeln.clear()
    transport.regel("GET", r"/shares/", fehler(400, "invalidRequest"))
    transport.regel("GET", r"/sites/contoso.sharepoint.com:/sites/Buchhaltung$", ok({"id": "S1"}))
    transport.regel("GET", r"/sites/S1/drives$", ok({"value": [{"id": "DRV-B", "webUrl": "https://contoso.sharepoint.com/sites/Buchhaltung/Freigegebene Dokumente"}]}))
    transport.regel("GET", r"/drives/DRV-B/root:/General/Datei.xlsx$", ok({"id": "ITEM-B"}))
    ergebnis2 = adressierung.aufloesen(ref, client)
    assert (ergebnis2.drive_id, ergebnis2.item_id) == ("DRV-B", "ITEM-B")

    transport.regeln.clear()
    transport.regel("GET", r".*", fehler(404, "itemNotFound"))
    with pytest.raises(M365Fehler, match="konnte nicht aufgelöst werden"):
        adressierung.aufloesen(ref, client)


def test_standard_transport_nutzt_requests_session():
    class FakeResponse:
        status_code = 200
        headers = {"Content-Type": "application/json"}
        content = b"{}"

    class FakeSession:
        def __init__(self) -> None:
            self.aufrufe: list[tuple[Any, ...]] = []

        def request(self, method, url, **kw):
            self.aufrufe.append((method, url, kw))
            return FakeResponse()

    sitzung = FakeSession()
    sende = graph.requests_transport(sitzung, zeitlimit=5)
    status, headers, body = sende("GET", "https://graph/x", headers={"A": "b"}, json=None, data=None, params={"q": "1"}, stream=False)
    assert (status, body) == (200, b"{}") and headers["Content-Type"] == "application/json"
    assert sitzung.aufrufe[0][2]["timeout"] == 5 and sitzung.aufrufe[0][2]["params"] == {"q": "1"}
    client = GraphClient(lambda: "T", sende=sende)
    assert client.get("/x") == {}
    assert isinstance(GraphClient(lambda: "T")._sende, Callable)  # Standard: requests.Session
    assert field is not None
