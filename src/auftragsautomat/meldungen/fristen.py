"""Fristenkalender für die drei Meldungen eines Bezugsmonats."""

from __future__ import annotations

from datetime import date, timedelta

from auftragsautomat.config import Firma
from auftragsautomat.meldungen import schaumweinsteuer
from auftragsautomat.meldungen.modelle import Frist, Meldemonat


def _ostersonntag(jahr: int) -> date:
    a = jahr % 19
    b, c = divmod(jahr, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7  # noqa: E741
    m = (a + 11 * h + 22 * l) // 451
    monat = (h + l - 7 * m + 114) // 31
    tag = (h + l - 7 * m + 114) % 31 + 1
    return date(jahr, monat, tag)


def bundesweite_feiertage(jahr: int) -> set[date]:
    """Bundeseinheitliche Feiertage (Destatis rechnet in Arbeitstagen Mo–Fr ohne diese Tage)."""
    ostern = _ostersonntag(jahr)
    return {
        date(jahr, 1, 1),
        ostern - timedelta(days=2),  # Karfreitag
        ostern + timedelta(days=1),  # Ostermontag
        date(jahr, 5, 1),
        ostern + timedelta(days=39),  # Christi Himmelfahrt
        ostern + timedelta(days=50),  # Pfingstmontag
        date(jahr, 10, 3),
        date(jahr, 12, 25),
        date(jahr, 12, 26),
    }


def n_ter_arbeitstag(ab: date, n: int) -> date:
    """n-ter Arbeitstag (Mo–Fr, ohne bundesweite Feiertage) ab dem Datum ``ab`` (einschließlich)."""
    tag = ab
    zaehler = 0
    while True:
        if tag.weekday() < 5 and tag not in bundesweite_feiertage(tag.year):
            zaehler += 1
            if zaehler == n:
                return tag
        tag += timedelta(days=1)


def intrastat_frist(monat: Meldemonat) -> date:
    """10. Arbeitstag nach Ablauf des Bezugsmonats (Leitfaden 2026, Abschnitt 1.6)."""
    folge = monat.folgemonat(1)
    return n_ter_arbeitstag(date(folge.jahr, folge.monat, 1), 10)


def fristen_fuer(monat: Meldemonat, firma: Firma) -> list[Frist]:
    v = firma.meldungen.verpackung
    fristen = [
        Frist(
            meldung="intrastat",
            bezeichnung=f"Intrastat Eingang/Versendung {monat} (bzw. Fehlanzeige)",
            datum=intrastat_frist(monat),
            empfaenger="Statistisches Bundesamt – IDEV (idev-intrahandel@destatis.de)",
            hinweis="10. Arbeitstag nach dem Bezugsmonat; keine Fristverlängerung möglich.",
        ),
        Frist(
            meldung="schaumweinsteuer",
            bezeichnung=f"Steueranmeldung Schaumwein {monat} (Formular 2401, Zoll-Portal)",
            datum=schaumweinsteuer.abgabefrist(monat),
            empfaenger=f"Hauptzollamt {firma.meldungen.schaumwein.hauptzollamt or '(bitte eintragen)'}",
            hinweis="§ 15 Abs. 1 SchaumwZwStG: Abgabe bis zum 10. des Folgemonats.",
        ),
        Frist(
            meldung="schaumweinsteuer",
            bezeichnung=f"Zahlung Schaumweinsteuer {monat}",
            datum=schaumweinsteuer.faelligkeit(monat),
            empfaenger="Bundeskasse (Hauptzollamt)",
            hinweis="Fällig am 5. Tag des zweiten auf die Entstehung folgenden Monats.",
        ),
    ]
    if (
        v.meldeturnus == "monatlich"
        or (v.meldeturnus == "quartal" and monat.monat % 3 == 0)
        or (v.meldeturnus == "halbjahr" and monat.monat in (6, 12))
    ):
        folge = monat.folgemonat(1)
        fristen.append(
            Frist(
                meldung="verpackung",
                bezeichnung=f"Unterjährige Mengenmeldung Verpackung bis {monat} (Systembetreiber + LUCID spiegeln)",
                datum=date(folge.jahr, folge.monat, 15),
                empfaenger=f"{v.systembetreiber or 'Systembetreiber'} Kundenportal; danach LUCID 'Unterjährige Mengenmeldung'",
                hinweis="Frist laut Systembeteiligungsvertrag (hier: 15. des Folgemonats angenommen – bitte prüfen).",
            )
        )
    if monat.monat == 12:
        fristen.append(
            Frist(
                meldung="verpackung",
                bezeichnung=f"Jahresabschluss-Mengenmeldung {monat.jahr} (Ist-Mengen)",
                datum=date(monat.jahr + 1, 5, 15),
                empfaenger=f"{v.systembetreiber or 'Systembetreiber'}; LUCID 'Jahresabschlussmengenmeldung' (bis 01.06.)",
                hinweis="Der Grüne Punkt: bis 15.05. des Folgejahres; LUCID bis 01.06., danach Nachtragsmengenmeldung.",
            )
        )
    if monat.monat in (10, 11, 12):
        fristen.append(
            Frist(
                meldung="verpackung",
                bezeichnung=f"Initiale Planmengenmeldung {monat.jahr + 1}",
                datum=date(monat.jahr, 12, 31),
                empfaenger=f"{v.systembetreiber or 'Systembetreiber'} (Vertrag/Planmenge); LUCID 'Initiale Planmengenmeldung'",
                hinweis="Hochrechnung aus den Ist-Mengen des laufenden Jahres liegt der Verpackungsmeldung bei.",
            )
        )
    return sorted(fristen, key=lambda f: f.datum)
