from __future__ import annotations

from datetime import UTC, datetime

from postfach.antworten import ist_antwort_betreff, markiere_antworten, normalisiere_betreff
from postfach.modelle import Mail

JD = "j.duenschede@weinstrasseadolph.de"


def test_normalisiere_betreff():
    assert normalisiere_betreff("AW: Re: WG:  Etiketten  2026") == "etiketten 2026"
    assert normalisiere_betreff("RE[2]: Order") == "order"
    assert normalisiere_betreff("Etiketten") == "etiketten"


def test_ist_antwort_betreff():
    assert ist_antwort_betreff("AW: Etiketten")
    assert ist_antwort_betreff("Re: Etiketten")
    assert not ist_antwort_betreff("WG: Etiketten")
    assert not ist_antwort_betreff("Etiketten")


def test_antwort_auf_eigene_mail_wird_erkannt():
    gesendet = [
        Mail(
            id="S1",
            betreff="Etiketten Jahrgang 2025",
            absender=JD,
            empfaenger=["export@vilano.com"],
            empfangen=datetime(2026, 10, 20, tzinfo=UTC),
        ),
    ]
    eingang = [
        Mail(
            id="E1",
            betreff="RE: Etiketten Jahrgang 2025",
            absender="export@vilano.com",
            empfaenger=[JD],
            empfangen=datetime(2026, 10, 22, tzinfo=UTC),
        ),
        Mail(
            id="E2",
            betreff="RE: Etiketten Jahrgang 2025",
            absender="someone@else.com",
            empfaenger=[JD],
            empfangen=datetime(2026, 10, 22, tzinfo=UTC),
        ),
        Mail(
            id="E3",
            betreff="Re: Alte Sache",
            absender="x@y.com",
            empfaenger=[JD],
            empfangen=datetime(2026, 10, 22, tzinfo=UTC),
        ),
        Mail(
            id="E4",
            betreff="Neue Anfrage",
            absender="x@y.com",
            empfaenger=[JD],
            empfangen=datetime(2026, 10, 22, tzinfo=UTC),
        ),
    ]
    markiere_antworten(eingang, gesendet)
    assert eingang[0].antwort_auf_eigene is True
    assert eingang[1].antwort_auf_eigene is False
    assert eingang[2].antwort_auf_eigene is None  # Antwort-Betreff, Ursprung nicht gefunden
    assert eingang[3].antwort_auf_eigene is False


def test_antwort_von_kollegen_des_empfaengers_gleiche_domain():
    gesendet = [
        Mail(
            id="S1",
            betreff="Order 4711",
            absender=JD,
            empfaenger=["a@lustau.es"],
            empfangen=datetime(2026, 10, 20, tzinfo=UTC),
        )
    ]
    eingang = [
        Mail(
            id="E1",
            betreff="Re: Order 4711",
            absender="b@lustau.es",
            empfaenger=[JD],
            empfangen=datetime(2026, 10, 21, tzinfo=UTC),
        )
    ]
    markiere_antworten(eingang, gesendet)
    assert eingang[0].antwort_auf_eigene is True
