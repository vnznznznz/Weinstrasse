from __future__ import annotations

from datetime import date

from postfach.termine import Aufgabe, anstehend, lade_aufgaben


def test_jaehrlich_mit_vorlauf():
    a = Aufgabe(name="ProWein", jaehrlich="03-15", vorlauf_tage=120)
    heute = date(2026, 12, 1)
    assert a.naechster_termin(heute) == date(2027, 3, 15)
    f = anstehend([a], heute=heute, tage=28)
    assert len(f) == 1 and f[0].in_tagen == 104


def test_jaehrlich_ausserhalb_vorlauf_nicht_faellig():
    a = Aufgabe(name="ProWein", jaehrlich="03-15", vorlauf_tage=30)
    assert anstehend([a], heute=date(2026, 10, 24), tage=28) == []


def test_monatlich_und_monatsende():
    a = Aufgabe(name="Provision", monatlich=31, vorlauf_tage=5)
    assert a.naechster_termin(date(2026, 2, 10)) == date(2026, 2, 28)
    assert a.naechster_termin(date(2026, 2, 28)) == date(2026, 2, 28)
    assert a.naechster_termin(date(2026, 3, 1)) == date(2026, 3, 31)


def test_woechentlich():
    a = Aufgabe(name="Wochenstart", woechentlich="Mo", vorlauf_tage=1)
    assert a.naechster_termin(date(2026, 10, 24)) == date(2026, 10, 26)  # Samstag -> Montag
    assert a.naechster_termin(date(2026, 10, 26)) == date(2026, 10, 26)


def test_einmalig_und_erledigt():
    a = Aufgabe(name="Böhling", einmalig="2026-11-02", vorlauf_tage=10)
    assert anstehend([a], heute=date(2026, 10, 24), tage=28)[0].in_tagen == 9
    a.erledigt_bis = "2026-11-02"
    assert anstehend([a], heute=date(2026, 10, 24), tage=28) == []
    assert a.naechster_termin(date(2026, 11, 3)) is None


def test_sortierung_und_dringend():
    aufgaben = [
        Aufgabe(name="B", einmalig="2026-10-30", vorlauf_tage=10),
        Aufgabe(name="A", einmalig="2026-10-25", vorlauf_tage=10),
    ]
    f = anstehend(aufgaben, heute=date(2026, 10, 24), tage=28)
    assert [x.aufgabe.name for x in f] == ["A", "B"]
    assert f[0].dringend and not f[1].dringend
    assert "morgen" in f[0].zeile()


def test_lade_aufgaben_aus_repo():
    aufgaben = lade_aufgaben()
    assert aufgaben, "postfach/wissen/wiederkehrend.yaml sollte Einträge haben"
    for a in aufgaben:
        assert a.naechster_termin(date(2026, 10, 24)) is not None or a.einmalig
