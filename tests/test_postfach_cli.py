from __future__ import annotations

import json
from pathlib import Path

from postfach.cli import main

JD = "j.duenschede@weinstrasseadolph.de"


def _mails() -> list[dict]:
    return [
        {
            "id": "A1AAAAAAEMAAA",
            "subject": "Invoice 118",
            "sender": "export@lustau.es",
            "recipients": [JD],
            "receivedDateTime": "2026-10-20T10:00:00Z",
            "isRead": True,
            "summary": "",
        },
        {
            "id": "A2AAAAAAEMAAA",
            "subject": "Invoice 119",
            "sender": "export@lustau.es",
            "recipients": [JD],
            "receivedDateTime": "2026-10-20T10:00:00Z",
            "isRead": False,
            "summary": "",
        },
        {
            "id": "A3AAAAAAEMAAA",
            "subject": "RE: Labels",
            "sender": "export@vilano.com",
            "recipients": [JD],
            "receivedDateTime": "2026-10-22T10:00:00Z",
            "isRead": True,
            "summary": "",
        },
        {
            "id": "A4AAAAAAEMAAA",
            "subject": "Undeliverable: x",
            "sender": "MAILER-DAEMON@x.de",
            "recipients": [JD],
            "receivedDateTime": "2026-10-22T10:00:00Z",
            "isRead": True,
            "summary": "",
        },
        {"nextOffset": 15, "totalResultCount": 4},
    ]


def test_regeln(capsys):
    assert main(["regeln"]) == 0
    assert "13 Regeln" in capsys.readouterr().out


def test_klassifizieren_plan_und_json(tmp_path: Path, capsys):
    mails = tmp_path / "mails.json"
    mails.write_text(json.dumps(_mails()), encoding="utf-8")
    gesendet = tmp_path / "gesendet.json"
    gesendet.write_text(
        json.dumps(
            [
                {
                    "id": "S1AAAAAAEJAAA",
                    "subject": "Labels",
                    "sender": JD,
                    "recipients": ["export@vilano.com"],
                    "receivedDateTime": "2026-10-21T10:00:00Z",
                }
            ]
        ),
        encoding="utf-8",
    )

    assert (
        main(["klassifizieren", str(mails), "--gesendet", str(gesendet), "--heute", "2026-10-24", "--plan"])
        == 0
    )
    out = capsys.readouterr().out
    assert "### Kommissionen (1 Mails)" in out
    assert '"A1AAAAAAEMAAA"' in out
    assert "A2AAAAAAEMAAA" not in out.split("## Löschen")[0]  # ungelesen wird nicht verschoben
    assert "Antwort auf deine Mail" in out
    assert "A4AAAAAAEMAAA" in out.split("## Löschen")[1]

    assert (
        main(["--json", "klassifizieren", str(mails), "--gesendet", str(gesendet), "--heute", "2026-10-24"])
        == 0
    )
    daten = json.loads(capsys.readouterr().out)
    assert len(daten) == 4
    assert daten[2]["bleibt_grund"].startswith("Antwort")


def test_antworten_befehl(tmp_path: Path, capsys):
    e = tmp_path / "e.json"
    g = tmp_path / "g.json"
    e.write_text(json.dumps(_mails()), encoding="utf-8")
    g.write_text(
        json.dumps(
            [
                {
                    "id": "S1",
                    "subject": "Labels",
                    "sender": JD,
                    "recipients": ["export@vilano.com"],
                    "receivedDateTime": "2026-10-21T10:00:00Z",
                }
            ]
        ),
        encoding="utf-8",
    )
    assert main(["antworten", str(e), str(g)]) == 0
    assert "1 Antworten" in capsys.readouterr().out


def test_zustand_und_briefing(tmp_path: Path, capsys):
    z = tmp_path / "zustand.json"
    assert main(["--zustand", str(z), "zustand", "lauf", "entwuerfe", "--notiz", "test"]) == 0
    assert (
        main(
            [
                "--zustand",
                str(z),
                "zustand",
                "entwurf",
                "<m1>",
                "D1",
                "--betreff",
                "Re: Invoice",
                "--an",
                "export@lustau.es",
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "--zustand",
                str(z),
                "zustand",
                "antwort",
                "--betreff",
                "RE: Labels",
                "--von",
                "export@vilano.com",
            ]
        )
        == 0
    )
    assert main(["--zustand", str(z), "zustand", "bearbeitet", "<m1>", "<m2>"]) == 0
    daten = json.loads(z.read_text(encoding="utf-8"))
    assert "entwuerfe" in daten["laeufe"] and daten["entwuerfe"]["<m1>"]["entwurf_id"] == "D1"
    assert daten["bearbeitet"] == ["<m1>", "<m2>"]
    capsys.readouterr()

    assert main(["--zustand", str(z), "briefing-geruest", "--heute", "2026-10-26"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("# Briefing Montag, 26.10.2026 (KW 44)")
    assert "export@vilano.com: RE: Labels" in out
    assert "an export@lustau.es: Re: Invoice" in out


def test_anstehend_mit_eigener_datei(tmp_path: Path, capsys):
    datei = tmp_path / "w.yaml"
    datei.write_text(
        "- name: Test\n  einmalig: 2026-10-25\n  vorlauf_tage: 5\n  aufgaben: [A, B]\n", encoding="utf-8"
    )
    assert main(["anstehend", "--heute", "2026-10-24", "--datei", str(datei)]) == 0
    assert "!! 25.10.2026 (morgen): Test – A; B" in capsys.readouterr().out


def test_fehlende_regeldatei(capsys):
    assert main(["--regeln", "/nicht/da.json", "regeln"]) == 1
    assert "Fehler" in capsys.readouterr().err
