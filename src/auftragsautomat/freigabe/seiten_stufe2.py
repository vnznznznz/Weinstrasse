"""Seiten und Abschnitte der Ausbaustufe 2 in der Freigabe-Oberfläche (docs/AUSBAUSTUFE_2.md, docs/ARCHITEKTUR.md).

Die Logik liegt in Funktionen ohne Oberfläche (testbar ohne Streamlit): Upload in den Eingang
(:func:`upload_speichern_und_pruefen`), Firmenwahl bei ``FIRMA_UNKLAR`` (:func:`firmenwahl_kandidaten`,
:func:`firma_waehlen`), Rabatt-Spalte (:func:`setze_rabatt_manuell`, :func:`rabatt_farbe`), Bericht der
Überwachung (:func:`lade_bericht`, :func:`ampel_zeilen`, :func:`pruefe_jetzt`,
:func:`terminaenderung_uebernehmen`, :func:`kontaktaenderung_uebernehmen`), Dossiers
(:func:`dossier_zeilen`), Rabatte (:func:`rabattliste_einlesen`, :func:`aktionen_aus_tabelle`,
:func:`aktionen_uebernehmen`) und Einstellungen (:func:`einstellungen_zeilen`).

Die ``zeige_*``-Funktionen zeichnen mit Streamlit und bekommen einen :class:`UiKontext` mit den
Rückrufen der App (Meldung vormerken, Paket setzen/speichern, Benutzername, Dry-Run-Schalter), damit
``app.py`` die Seiten nur noch aufruft. Es wird nie gesendet, nie gelöscht; Schreibzugriffe auf Excel
und Postfach gibt es nur im scharfen Modus (Dry-Run-Schalter aus), sonst Vorschauen.
"""

from __future__ import annotations

import html
import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

from auftragsautomat import benennung
from auftragsautomat.config import Settings, projekt_stamm
from auftragsautomat.eingang import (
    UploadAbgelehnt,
    erlaubte_endungen,
    freier_pfad,
    schreibe_atomar,
    speichere_upload,
)
from auftragsautomat.extraktion.zahlen import parse_datum, parse_dezimal
from auftragsautomat.freigabe import zustand
from auftragsautomat.lexware import nachbereitung
from auftragsautomat.matching.pipeline import setze_firma
from auftragsautomat.models import (
    Aktion,
    Belegpaket,
    BuchungsErgebnis,
    Dossier,
    Fixtermin,
    KontaktAenderung,
    PositionsZuordnung,
    Pruefcode,
    Pruefhinweis,
    Rabattliste,
    Schweregrad,
    Terminaenderung,
    Ueberwachungsbericht,
)
from auftragsautomat.protokoll import formatiere_betrag, formatiere_datum, formatiere_menge
from auftragsautomat.watcher import verarbeite_standard

__all__ = [
    "RABATT_CODES",
    "SEITEN",
    "SS_SEITE",
    "Firmenwahl",
    "UiKontext",
    "UploadErgebnis",
    "aktionen_aus_tabelle",
    "aktionen_uebernehmen",
    "aktionen_zeilen",
    "aktionen_zu_tabelle",
    "ampel_zeilen",
    "checklisten_fortschritt",
    "dossier_zeilen",
    "einstellungen_zeilen",
    "firma_waehlen",
    "firmenwahl_kandidaten",
    "gelernte_firma",
    "kontaktaenderung_uebernehmen",
    "lade_bericht",
    "pruefe_jetzt",
    "rabatt_farbe",
    "rabatt_hinweise",
    "rabatt_tooltip",
    "rabattliste_einlesen",
    "setze_rabatt_manuell",
    "terminaenderung_uebernehmen",
    "upload_speichern_und_pruefen",
    "zeige_dateien_und_ablage",
    "zeige_firmenwahl",
    "zeige_nachbereitung",
    "zeige_seite_dossiers",
    "zeige_seite_einstellungen",
    "zeige_seite_heute",
    "zeige_seite_rabatte",
    "zeige_upload",
]

logger = logging.getLogger(__name__)

#: Seiten der Oberfläche (Seitenleiste, ``st.radio``).
SEITEN: tuple[str, ...] = ("Freigabe", "Heute", "Dossiers", "Rabatte", "Einstellungen")
SS_SEITE = "seite"
SS_ABGELEHNT = "vorschlaege_abgelehnt"
SS_RABATTLISTE = "rabattliste"
SS_RABATTLISTE_PFAD = "rabattliste_pfad"

GELB = "#b35c00"
ROT = "#c00000"
GRUEN = "#1a7f37"
GRAU = "#6b7280"
AMPEL_FARBE: dict[str, str] = {"rot": ROT, "gelb": GELB, "gruen": GRUEN, "grau": GRAU}
AMPEL_TEXT: dict[str, str] = {"rot": "ROT", "gelb": "GELB", "gruen": "GRÜN", "grau": "GRAU"}
#: Rabatt-Hinweise: gelb = Vorschlag (Info), rot = unsicher/Abweichung (Warnung) oder Vorschlag mit FEHLER.
RABATT_CODES: frozenset[Pruefcode] = frozenset({Pruefcode.RABATT_VORSCHLAG, Pruefcode.RABATT_UNSICHER, Pruefcode.RABATT_ABWEICHUNG})
_RABATT_ROT: frozenset[Pruefcode] = frozenset({Pruefcode.RABATT_UNSICHER, Pruefcode.RABATT_ABWEICHUNG})
_RABATTLISTE_ENDUNGEN: tuple[str, ...] = (".pdf", ".png", ".jpg", ".jpeg")
#: Spalten der editierbaren Aktionstabelle (Seite "Rabatte").
_TABELLE_SPALTEN: tuple[str, ...] = (
    "uebernehmen", "aktion_id", "bezeichnung", "firma_id", "kunde_kurz", "lieferant_kurz", "artikel_text",
    "artikelnummern", "von", "bis", "rabatt_prozent", "rabatt_eur", "aktionspreis", "konfidenz", "quelle",
)


@dataclass
class UiKontext:
    """Rückrufe der App für die Seitenfunktionen (damit ``seiten_stufe2`` nicht ``app`` importieren muss)."""

    melde: Callable[[str, str], None]
    setze_paket: Callable[[Belegpaket, Path | None], None]
    speichere: Callable[[Belegpaket], Path | None]
    benutzer: Callable[[], str]
    dry_run: Callable[[], bool]


def _fehlertext(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# Upload in den Eingang
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UploadErgebnis:
    datei: Path  # gespeicherte Eingangsdatei
    paket: Belegpaket
    paket_pfad: Path


def _paket_zur_datei(ordner: Path, datei: Path) -> tuple[Belegpaket, Path] | None:
    for pfad in zustand.liste_pakete(ordner):
        if zustand.beschreibe_paket(pfad).quelldatei == datei.name:
            return zustand.lade_paket(pfad), pfad
    return None


def upload_speichern_und_pruefen(
    dateiname: str,
    inhalt: bytes,
    settings: Settings,
    *,
    firma_id: str | None = None,
    paket_ordner: Path | None = None,
) -> UploadErgebnis:
    """Hochgeladene Datei in den Eingang legen (``eingang.speichere_upload``) und sofort verarbeiten
    (``watcher.verarbeite_standard``: extrahieren, prüfen, Paket speichern – nie buchen).

    ``firma_id`` (ausdrückliche Auswahl im Upload-Bereich, ``None`` = automatisch ermitteln) bestimmt den
    Firmen-Unterordner. Die Firma wird nie still erzwungen: weicht die Vorgabe von der Firma ab, die das
    Tool ohne Vorgabe ermitteln würde (oder von der Firma des Pakets), bekommt das Paket einen blockierenden
    ``FIRMA_UNKLAR``-Hinweis mit beiden Kandidaten (Auswahl in der Freigabe, Geschäftsregeln 1 und 17).
    Verstöße gegen Typ/Größe ergeben :class:`UploadAbgelehnt`.
    """
    datei = speichere_upload(dateiname, inhalt, settings, firma_id=firma_id)
    ordner = Path(paket_ordner) if paket_ordner is not None else zustand.paket_ordner(settings)
    verarbeite_standard(settings, paket_ordner=ordner)(datei)
    geladen = _paket_zur_datei(ordner, datei)
    if geladen is None:
        raise RuntimeError(f"Paket zu {datei.name} wurde nach der Verarbeitung nicht gefunden (Ordner {ordner}).")
    paket, pfad = geladen
    if firma_id:
        hinweis = _firmen_abweichung(paket, firma_id, settings)
        if hinweis is not None:
            logger.warning("Upload %s: %s", datei.name, hinweis.text)
            paket.hinweise.append(hinweis)
            pfad = zustand.speichere_paket(paket, ordner, dateiname=pfad.name)
    return UploadErgebnis(datei=datei, paket=paket, paket_pfad=pfad)


def _firmen_abweichung(paket: Belegpaket, vorgabe: str, settings: Settings) -> Pruefhinweis | None:
    """``FIRMA_UNKLAR`` (FEHLER), wenn die Upload-Vorgabe nicht zur Firmenermittlung ohne Vorgabe passt."""
    from auftragsautomat.matching.aliase import AliasTabellen
    from auftragsautomat.matching.firma import ermittle_firma
    from auftragsautomat.matching.pruefung import QUELLE_UPLOAD_VORGABE

    # Ermittlung wie ohne Vorgabe: Dateiname ohne Firmen-Unterordner
    ermittelt = ermittle_firma(
        paket.roh, settings, AliasTabellen.lade(settings.alias_ordner), datei=Path(Path(paket.roh.quelldatei).name)
    )
    kandidaten = [vorgabe]
    if paket.firma_id and paket.firma_id != vorgabe:
        kandidaten.append(paket.firma_id)
    if ermittelt.firma_id:
        if ermittelt.firma_id != vorgabe:
            kandidaten.append(ermittelt.firma_id)
    elif ermittelt.kandidaten and vorgabe not in ermittelt.kandidaten:
        kandidaten.extend(ermittelt.kandidaten)  # Katalog "fragen": die Vorgabe ist keine der möglichen Firmen
    kandidaten = list(dict.fromkeys(k for k in kandidaten if k))
    if len(kandidaten) < 2:
        return None
    andere = ", ".join(kandidaten[1:])
    return Pruefhinweis(
        code=Pruefcode.FIRMA_UNKLAR,
        schweregrad=Schweregrad.FEHLER,
        text=(
            f"Firma beim Upload vorgegeben ({vorgabe}), das Dokument spricht für {andere} – "
            "bitte Firma wählen; ohne eindeutige Firma keine Buchung"
        ),
        details={"kandidaten": ", ".join(kandidaten), "vorgabe": vorgabe, "quelle": QUELLE_UPLOAD_VORGABE},
    )


def zeige_upload(settings: Settings, kontext: UiKontext) -> bool:
    """Seitenleiste "Auftrag hochladen"; True, wenn ein Paket entstanden ist (dann ``st.rerun``)."""
    if not settings.eingang.upload_erlaubt:
        return False
    st.subheader("Auftrag hochladen")
    typen = [e.lstrip(".") for e in erlaubte_endungen(settings)]
    # TODO-PRUEFEN: Upload im echten Browser (OneDrive-synchronisierter Eingang): OneDrive darf die temporäre
    # .upload_*.tmp nicht hochladen; die fertige Datei soll vom Watcher nur einmal verarbeitet werden.
    datei = st.file_uploader(
        f"Kundenauftrag ({', '.join(typen).upper()}; max. {settings.eingang.upload_max_mb} MB)",
        type=typen or None,
        key="upload_datei",
        help=f"Die Datei wird in den Eingang ({settings.eingang.ordner_name}) gelegt und sofort geprüft; gebucht wird erst nach Freigabe.",
    )
    # Eigene Auswahl (nicht die Firma des gerade geöffneten Pakets): Standard ist die automatische Ermittlung.
    firmen = ["", *settings.firmen]
    st.selectbox(
        "Firma für den Upload",
        firmen,
        index=0,
        key="upload_firma",
        format_func=lambda v: f"{v} – {settings.firmen[v].name}" if v in settings.firmen else "automatisch ermitteln",
        help="Nur wählen, wenn die Firma sicher feststeht; weicht das Dokument davon ab, muss die Firma in der Freigabe bestätigt werden.",
    )
    if st.button("Hochladen & prüfen", key="upload_verarbeiten", disabled=datei is None, width="stretch"):
        if datei is None:
            kontext.melde("warning", "Bitte zuerst eine Datei auswählen.")
            return True
        firma_id = st.session_state.get("upload_firma") or None
        try:
            with st.spinner(f"Speichere und prüfe {datei.name} …"):
                ergebnis = upload_speichern_und_pruefen(datei.name, datei.getvalue(), settings, firma_id=firma_id)
        except UploadAbgelehnt as exc:
            kontext.melde("error", f"Upload abgelehnt: {exc}")
            return True
        except Exception as exc:  # noqa: BLE001 - Extraktion/Matching: Ursache anzeigen, App läuft weiter
            logger.exception("Upload %s fehlgeschlagen", datei.name)
            kontext.melde("error", f"Upload von {datei.name} fehlgeschlagen: {_fehlertext(exc)}")
            return True
        kontext.setze_paket(ergebnis.paket, ergebnis.paket_pfad)
        status = "BLOCKIERT – bitte korrigieren" if ergebnis.paket.blockiert else "freigabefähig"
        kontext.melde(
            "success",
            f"{datei.name} im Eingang gespeichert ({ergebnis.datei}) und geprüft: Firma {ergebnis.paket.firma_id or '?'}, "
            f"{len(ergebnis.paket.positionen)} Positionen, {status}.",
        )
        return True
    return False


# ---------------------------------------------------------------------------
# Firmenwahl (FIRMA_UNKLAR mit Kandidaten, FIRMA_GELERNT)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Firmenwahl:
    kandidaten: list[str]
    kundengruppe: str | None
    text: str
    blockiert: bool


def firmenwahl_kandidaten(paket: Belegpaket) -> Firmenwahl | None:
    """Kandidaten aus dem ``FIRMA_UNKLAR``-Hinweis (``details["kandidaten"]``), FEHLER vor WARNUNG; ``None`` ohne Kandidaten."""
    treffer = [h for h in paket.alle_hinweise if h.code == Pruefcode.FIRMA_UNKLAR and h.details.get("kandidaten")]
    if not treffer:
        return None
    treffer.sort(key=lambda h: 0 if h.schweregrad == Schweregrad.FEHLER else 1)
    hinweis = treffer[0]
    kandidaten: list[str] = []
    for teil in str(hinweis.details["kandidaten"]).split(","):
        k = teil.strip()
        if k and k not in kandidaten:
            kandidaten.append(k)
    if not kandidaten:
        return None
    return Firmenwahl(
        kandidaten=kandidaten,
        kundengruppe=hinweis.details.get("kundengruppe") or None,
        text=hinweis.text,
        blockiert=hinweis.schweregrad == Schweregrad.FEHLER,
    )


def firma_waehlen(paket: Belegpaket, firma_id: str, settings: Settings, *, merken: bool, benutzer: str | None) -> Belegpaket:
    """Firma setzen und neu zuordnen; mit ``merken`` wird die Wahl je Kunde + Lieferant ins Firmen-Gedächtnis geschrieben."""
    return setze_firma(paket, firma_id, settings, merken=merken, benutzer=benutzer)


def gelernte_firma(paket: Belegpaket) -> Pruefhinweis | None:
    """Der ``FIRMA_GELERNT``-Hinweis (Firma aus dem Firmen-Gedächtnis), falls vorhanden."""
    return next((h for h in paket.alle_hinweise if h.code == Pruefcode.FIRMA_GELERNT), None)


def zeige_firmenwahl(paket: Belegpaket, settings: Settings, kontext: UiKontext) -> bool:
    """Info "gelernt am/von" und – bei FIRMA_UNKLAR mit Kandidaten – Auswahl + Häkchen "merken"; True = Paket geändert."""
    gelernt = gelernte_firma(paket)
    if gelernt is not None:
        d = gelernt.details
        st.info(
            f"Firma {d.get('firma_id') or paket.firma_id} aus dem Firmen-Gedächtnis – gelernt am {d.get('gelernt_am') or '-'} "
            f"von {d.get('gelernt_von') or '-'} (Kunde „{d.get('kunde') or '-'}“, Lieferant „{d.get('lieferant') or 'alle'}“"
            + (f", Kundengruppe {d.get('kundengruppe')}" if d.get("kundengruppe") else "")
            + ")."
        )
    wahl = firmenwahl_kandidaten(paket)
    if wahl is None:
        return False
    labels = {k: f"{k} – {settings.firmen[k].name}" if k in settings.firmen else k for k in wahl.kandidaten}
    (st.error if wahl.blockiert else st.warning)(
        f"Firma wählen{f' (Kundengruppe {wahl.kundengruppe})' if wahl.kundengruppe else ''}: {wahl.text}"
    )
    spalten = st.columns([2, 2, 1])
    vorauswahl = wahl.kandidaten.index(paket.firma_id) if paket.firma_id in wahl.kandidaten else 0
    firma_id = spalten[0].selectbox(
        "Firma", wahl.kandidaten, index=vorauswahl, key="firmenwahl_firma", format_func=lambda v: labels.get(v, v)
    )
    merken = spalten[1].checkbox(
        "für diesen Kunden und Lieferanten merken",
        value=settings.firmenwahl.merken,
        key="firmenwahl_merken",
        disabled=not settings.firmenwahl.merken,
        help="Beim nächsten Auftrag dieses Kunden mit diesem Lieferanten wird die Firma automatisch übernommen (FIRMA_GELERNT).",
    )
    if spalten[2].button("Firma übernehmen", key="firmenwahl_uebernehmen", type="primary", width="stretch"):
        try:
            with st.spinner(f"Ordne Firma {firma_id} zu und prüfe neu …"):
                neu = firma_waehlen(paket, firma_id, settings, merken=bool(merken), benutzer=kontext.benutzer())
        except Exception as exc:  # noqa: BLE001
            logger.exception("Firmenwahl %s fehlgeschlagen", firma_id)
            kontext.melde("error", f"Firma konnte nicht auf {firma_id} gesetzt werden: {_fehlertext(exc)}")
            return True
        kontext.setze_paket(neu, None)
        kontext.speichere(neu)
        text = f"Firma {firma_id} übernommen; Paket neu zugeordnet und geprüft."
        if merken and settings.firmenwahl.merken:
            text += f" Die Wahl wurde für Kunde und Lieferant gemerkt ({Path(settings.firmenwahl.gedaechtnis_datei).name})."
        kontext.melde("success", text)
        return True
    return False


# ---------------------------------------------------------------------------
# Rabatt-Spalte
# ---------------------------------------------------------------------------


def rabatt_hinweise(position: PositionsZuordnung) -> list[Pruefhinweis]:
    return [h for h in position.hinweise if h.code in RABATT_CODES]


def rabatt_farbe(position: PositionsZuordnung) -> str | None:
    """Farbe der Rabatt-Zelle: rot bei RABATT_UNSICHER/RABATT_ABWEICHUNG (oder Vorschlag mit FEHLER), gelb bei RABATT_VORSCHLAG."""
    hinweise = rabatt_hinweise(position)
    if any(h.code in _RABATT_ROT or h.schweregrad == Schweregrad.FEHLER for h in hinweise):
        return ROT
    if hinweise:
        return GELB
    return None


def rabatt_tooltip(position: PositionsZuordnung) -> str:
    quelle = position.rabatt_quelle
    if quelle:
        if quelle.startswith("aktion:"):
            text = f"Quelle: Aktion {quelle[len('aktion:'):]}"
        elif quelle == "manuell":
            text = "Quelle: manuell gesetzt"
        else:
            text = f"Quelle: {quelle}"
    else:
        text = "Kein Positionsrabatt (Quelle leer)"
    hinweise = rabatt_hinweise(position)
    if hinweise:
        text += "\n" + "\n".join(f"[{h.code.value}] {h.text}" for h in hinweise)
    return text


def setze_rabatt_manuell(position: PositionsZuordnung, wert: Decimal | None) -> bool:
    """Positionsrabatt in % aus der Oberfläche übernehmen (Quelle "manuell"); True, wenn sich etwas geändert hat.

    ``None`` oder 0 entfernt den Rabatt (bleibt als manuelle Entscheidung markiert, damit die erneute
    Prüfung keinen Aktionsrabatt mehr vorschlägt). Werte über 100 oder unter 0 -> ``ValueError``.
    """
    neu = None if wert is None or wert == 0 else Decimal(wert)
    if neu is not None and (neu < 0 or neu > 100):
        raise ValueError(f"Position {position.roh.laufnr}: Rabatt {neu} % ist nicht zulässig (0 bis 100).")
    alt = position.rabatt_prozent
    if (alt or Decimal(0)) == (neu or Decimal(0)):
        return False
    logger.info("Position %s: Rabatt %s -> %s %% (manuell)", position.roh.laufnr, alt, neu)
    position.rabatt_prozent = neu
    position.rabatt_quelle = "manuell"
    return True


# ---------------------------------------------------------------------------
# Nach dem Buchen: Dateinamen, Ablage, Nachbereitung
# ---------------------------------------------------------------------------


def _ablage_status(a: Any) -> str:
    if a.dry_run:
        return "nur protokolliert (Dry Run)" + (f" – {a.fehler}" if a.fehler else "")
    if a.abgelegt:
        return "abgelegt"
    return f"NICHT abgelegt: {a.fehler or '?'}"


def zeige_dateien_und_ablage(ergebnis: BuchungsErgebnis) -> None:
    """Erzeugte Dateinamen (Titulierung) und Ablage-Ergebnisse der Buchung."""
    if ergebnis.pdfs:
        st.markdown("**Erzeugte Dateien (Namen laut Titulierung)**" + (" – Dry Run: nur geplant" if ergebnis.dry_run else ""))
        for kuerzel, pfad in ergebnis.pdfs.items():
            st.markdown(f"- {html.escape(kuerzel)}: `{html.escape(Path(pfad).name)}`", unsafe_allow_html=True)
    if ergebnis.ablage:
        st.markdown("**Ablage (OneDrive „Aufträge mit/ohne Provision“)**")
        zeilen = [
            {"Kürzel": a.kuerzel, "Zielordner": a.zielordner, "Zielname": a.zielname, "Status": _ablage_status(a)}
            for a in ergebnis.ablage
        ]
        st.dataframe(pd.DataFrame(zeilen), hide_index=True, width="stretch")


def zeige_nachbereitung(paket: Belegpaket, settings: Settings) -> None:
    """Nachbereitung der letzten Buchung (Fixtermin-Zeile, Entwürfe, Dossier-Pfad, Hinweise) aus der gespeicherten JSON."""
    nb = nachbereitung.lade_nachbereitung(paket, settings)
    if nb is None:
        return
    st.subheader("Nachbereitung" + (" (Dry Run – Vorschauen)" if nb.dry_run else ""))
    ft = nb.fixtermin
    if ft is not None:
        text = (
            f"**Fixtermin:** {ft.liefertag:%d.%m.%Y} | {ft.lieferant_kurz} | {ft.kunde_kurz} | Auftrag {formatiere_datum(ft.auftragsdatum)}"
            f" | Spedition {ft.spedition or '-'}"
        )
        if ft.zeile is not None:
            text += f" → Zeile {ft.zeile}" + (" (Vorschau, nicht geschrieben)" if nb.dry_run else " eingetragen")
        else:
            text += " → nicht eingetragen (siehe Hinweise)"
        st.markdown(text)
        if nb.vorschau_csv:
            st.caption(f"Vorschau der Excel-Änderungen: {nb.vorschau_csv}")
    else:
        st.markdown("**Fixtermin:** keiner (kein Liefertag im Dokument)")
    if nb.entwuerfe:
        st.markdown(f"**E-Mail-Entwürfe ({len(nb.entwuerfe)}, nicht gesendet)**")
        for e in nb.entwuerfe:
            an = ", ".join(e.an) or "(kein Empfänger)"
            st.markdown(f"- {e.typ.value}: „{e.betreff}“ an {an}" + (f" → `{e.speicherort}`" if e.speicherort else " (nicht gespeichert)"))
            for h in e.hinweise:
                st.caption(f"Hinweis: {h}")
    else:
        st.markdown("**E-Mail-Entwürfe:** keine")
    if nb.dossier is not None:
        st.markdown(f"**Dossier:** {nb.dossier.name} → `{nb.dossier.ordner}`")
        if nb.dossier.offene_punkte:
            with st.expander(f"Offene Punkte im Dossier ({len(nb.dossier.offene_punkte)})"):
                for p in nb.dossier.offene_punkte:
                    st.markdown(f"- {p}")
    else:
        st.markdown("**Dossier:** keines angelegt")
    if nb.hinweise:
        with st.expander(f"Hinweise der Nachbereitung ({len(nb.hinweise)})"):
            for h in nb.hinweise:
                st.markdown(f"- {h}")
    if nb.speicherort:
        st.caption(f"Nachbereitung gespeichert: {nb.speicherort}")


# ---------------------------------------------------------------------------
# Seite "Heute"
# ---------------------------------------------------------------------------


def lade_bericht(settings: Settings) -> tuple[Ueberwachungsbericht, list[str], list[str]] | None:
    """Letzter Überwachungsbericht mit Hinweisen und Protokoll (``ueberwachung.berichte_ordner``)."""
    from auftragsautomat.ueberwachung.bericht import lade_letzten_bericht_mit_zusatz

    return lade_letzten_bericht_mit_zusatz(Path(settings.ueberwachung.berichte_ordner))


def ampel_zeilen(bericht: Ueberwachungsbericht) -> list[dict[str, Any]]:
    """Zeilen der Ampel-Tabelle (rot > gelb > grün > grau, dann Liefertag)."""
    rang = {"rot": 0, "gelb": 1, "gruen": 2, "grau": 3}
    zeilen: list[dict[str, Any]] = []
    for p in sorted(bericht.punkte, key=lambda x: (rang.get(x.ampel, 9), x.fixtermin.liefertag)):
        ft = p.fixtermin
        zeilen.append(
            {
                "Ampel": AMPEL_TEXT.get(p.ampel, p.ampel),
                "Liefertag": ft.liefertag.strftime("%d.%m.%Y"),
                "Lieferant": ft.lieferant_kurz,
                "Kunde": ft.kunde_kurz,
                "Lieferant bestätigt": ft.lieferant_bestaetigt,
                "Spedition bestätigt": ft.spedition_bestaetigt,
                "Abgeholt": ft.abgeholt,
                "Tracking": p.sendung.status.value if p.sendung is not None else "",
                "Grund": p.grund,
                "Maßnahmen": "; ".join(p.aktionen),
                "Bemerkung": ft.bemerkung or "",
            }
        )
    return zeilen


def pruefe_jetzt(settings: Settings, *, dry_run: bool, nur_lesen: bool = False) -> tuple[Ueberwachungsbericht, Path]:
    """Überwachungslauf ausführen und den Bericht schreiben (Dry Run: Mails/Excel lesen, Entwürfe nur als .eml)."""
    from auftragsautomat.ueberwachung.lauf import ueberwachen

    backends = nachbereitung.backends_fuer_ueberwachung(settings, scharf=not dry_run, nur_lesen=nur_lesen)
    return ueberwachen(settings, dry_run=dry_run, backends=backends)


def terminaenderung_uebernehmen(
    aenderung: Terminaenderung, settings: Settings, *, dry_run: bool, backends: Any | None = None
) -> Fixtermin:
    """Vorschlag übernehmen: Fixtermin in der Excel verschieben (Dry Run: nur Vorschau-CSV)."""
    from auftragsautomat.termine.register import FixterminRegister

    backends = backends or nachbereitung.erzeuge_backends_fuer(settings, dry_run=dry_run)
    register = FixterminRegister(backends.excel, settings.m365.fixtermin_datei, bundesland=settings.ueberwachung.bundesland)
    neu = register.verschiebe(aenderung.fixtermin, aenderung.neues_datum, aenderung.grund)
    aenderung.uebernommen = True
    if dry_run and hasattr(backends.excel, "vorschau_csv"):
        backends.excel.vorschau_csv(Path(settings.log_ordner) / nachbereitung.VORSCHAU_CSV, anhaengen=True)
    logger.info("Terminänderung übernommen (%s): %s -> %s", "Dry Run" if dry_run else "scharf", aenderung.fixtermin.liefertag, neu.liefertag)
    return neu


def kontaktaenderung_uebernehmen(
    aenderung: KontaktAenderung, settings: Settings, *, dry_run: bool, backends: Any | None = None
) -> bool:
    """Vorschlag übernehmen: Zelle der Kontaktliste schreiben (freigegeben; Dry Run: nur Protokoll)."""
    from auftragsautomat.kontakte.aenderungen import uebernehme

    backends = backends or nachbereitung.erzeuge_backends_fuer(settings, dry_run=dry_run)
    return uebernehme(aenderung, backends.excel, settings.m365.kontaktliste_datei, settings=settings, freigegeben=True)


def _abgelehnt() -> set[str]:
    return st.session_state.setdefault(SS_ABGELEHNT, set())


def _ampel_metriken(bericht: Ueberwachungsbericht) -> None:
    zaehler = {a: 0 for a in ("rot", "gelb", "gruen", "grau")}
    for p in bericht.punkte:
        zaehler[p.ampel] = zaehler.get(p.ampel, 0) + 1
    spalten = st.columns(5)
    for spalte, ampel in zip(spalten[:4], ("rot", "gelb", "gruen", "grau"), strict=True):
        spalte.metric(AMPEL_TEXT[ampel], zaehler[ampel])
    spalten[4].metric("Entwürfe", len(bericht.entwuerfe))


def _zeige_vorschlaege_termine(bericht: Ueberwachungsbericht, settings: Settings, kontext: UiKontext) -> bool:
    offen = [(i, t) for i, t in enumerate(bericht.terminaenderungen) if f"termin:{i}" not in _abgelehnt()]
    if not offen:
        return False
    st.markdown("**Vorschläge: Terminänderung**")
    for i, t in offen:
        spalten = st.columns([4, 1, 1])
        text = (
            f"{t.fixtermin.lieferant_kurz} / {t.fixtermin.kunde_kurz}: {t.fixtermin.liefertag:%d.%m.%Y} → "
            f"**{t.neues_datum:%d.%m.%Y}** ({t.grund})"
        )
        spalten[0].markdown(text + (" – übernommen" if t.uebernommen else ""))
        if t.uebernommen:
            continue
        if spalten[1].button("Übernehmen", key=f"heute_termin_{i}_ok", width="stretch"):
            dry_run = kontext.dry_run()
            try:
                neu = terminaenderung_uebernehmen(t, settings, dry_run=dry_run)
            except Exception as exc:  # noqa: BLE001 - RegisterFehler, M365Fehler
                logger.exception("Terminänderung konnte nicht übernommen werden")
                kontext.melde("error", f"Termin nicht verschoben: {_fehlertext(exc)}")
            else:
                kontext.melde(
                    "success" if not dry_run else "info",
                    f"Fixtermin {t.fixtermin.lieferant_kurz} / {t.fixtermin.kunde_kurz} auf {neu.liefertag:%d.%m.%Y} verschoben"
                    + (" (Dry Run: nur Vorschau in logs/fixtermine_vorschau.csv)" if dry_run else f" (Zeile {neu.zeile})."),
                )
                _abgelehnt().add(f"termin:{i}")
            return True
        if spalten[2].button("Ablehnen", key=f"heute_termin_{i}_nein", width="stretch"):
            _abgelehnt().add(f"termin:{i}")
            kontext.melde("info", f"Terminänderung für {t.fixtermin.lieferant_kurz} / {t.fixtermin.kunde_kurz} abgelehnt (Excel unverändert).")
            return True
    return False


def _zeige_vorschlaege_kontakte(bericht: Ueberwachungsbericht, settings: Settings, kontext: UiKontext) -> bool:
    offen = [(i, k) for i, k in enumerate(bericht.kontaktaenderungen) if f"kontakt:{i}" not in _abgelehnt()]
    if not offen:
        return False
    st.markdown("**Vorschläge: Kontaktänderung**")
    for i, k in offen:
        spalten = st.columns([4, 1, 1])
        wer = f"{k.kontakt.firma} / {k.kontakt.name or '-'}" if k.kontakt is not None else "neuer Kontakt"
        spalten[0].markdown(f"{wer}: {k.feld} „{k.alt or ''}“ → **{k.neu}** ({k.quelle})" + (" – übernommen" if k.uebernommen else ""))
        if k.uebernommen:
            continue
        if spalten[1].button("Übernehmen", key=f"heute_kontakt_{i}_ok", width="stretch"):
            dry_run = kontext.dry_run()
            try:
                geschrieben = kontaktaenderung_uebernehmen(k, settings, dry_run=dry_run)
            except Exception as exc:  # noqa: BLE001 - KontakteFehler, M365Fehler
                logger.exception("Kontaktänderung konnte nicht übernommen werden")
                kontext.melde("error", f"Kontaktänderung nicht übernommen: {_fehlertext(exc)}")
            else:
                kontext.melde(
                    "success" if geschrieben and not dry_run else "info",
                    f"Kontaktänderung {k.feld} für {wer} " + ("übernommen" if geschrieben else "nicht geschrieben") + (" (Dry Run: nur Protokoll)" if dry_run else "."),
                )
                _abgelehnt().add(f"kontakt:{i}")
            return True
        if spalten[2].button("Ablehnen", key=f"heute_kontakt_{i}_nein", width="stretch"):
            _abgelehnt().add(f"kontakt:{i}")
            kontext.melde("info", f"Kontaktänderung für {wer} abgelehnt (Kontaktliste unverändert).")
            return True
    return False


def zeige_seite_heute(settings: Settings, kontext: UiKontext) -> bool:
    """Seite "Heute": letzter Überwachungsbericht, Button "Jetzt prüfen", Vorschläge mit Übernehmen/Ablehnen."""
    st.header("Heute – Fixtermine und Überwachung")
    dry_run = kontext.dry_run()
    spalten = st.columns([1, 3])
    if spalten[0].button("Jetzt prüfen" + (" (Dry Run)" if dry_run else " (SCHARF)"), key="heute_pruefen", type="primary", width="stretch"):
        try:
            with st.spinner("Überwachungslauf …"):
                bericht, pfad = pruefe_jetzt(settings, dry_run=dry_run)
        except Exception as exc:  # noqa: BLE001 - Backend/Konfiguration
            logger.exception("Überwachungslauf fehlgeschlagen")
            kontext.melde("error", f"Überwachungslauf fehlgeschlagen: {_fehlertext(exc)}")
        else:
            st.session_state[SS_ABGELEHNT] = set()
            kontext.melde(
                "success",
                f"Überwachung ausgeführt ({'Dry Run' if dry_run else 'SCHARF'}): {len(bericht.punkte)} Fixtermine, "
                f"{len(bericht.entwuerfe)} Entwürfe, {len(bericht.fehler)} Fehler. Bericht: {pfad}",
            )
        return True
    spalten[1].caption(
        "Dry Run: Mails und Excel werden gelesen, Entwürfe nur als .eml gespeichert, nichts in der Excel geändert. "
        "SCHARF (Schalter in der Seitenleiste): Häkchen/Referenzen in die Fixterminüberwachung, Entwürfe im Postfach."
    )
    geladen = lade_bericht(settings)
    if geladen is None:
        st.info(f"Noch kein Überwachungsbericht in {settings.ueberwachung.berichte_ordner} – bitte „Jetzt prüfen“ ausführen.")
        return False
    bericht, hinweise, protokoll = geladen
    st.caption(f"Bericht vom {bericht.zeitpunkt:%d.%m.%Y %H:%M} – Fixtermine bis {bericht.zeitraum_bis:%d.%m.%Y}")
    _ampel_metriken(bericht)
    zeilen = ampel_zeilen(bericht)
    if zeilen:
        st.dataframe(pd.DataFrame(zeilen), hide_index=True, width="stretch")
    else:
        st.info("Keine Fixtermine im Zeitraum.")
    mit_massnahmen = [p for p in bericht.punkte if p.aktionen]
    if mit_massnahmen:
        with st.expander(f"Maßnahmen je Fixtermin ({len(mit_massnahmen)})", expanded=True):
            for p in mit_massnahmen:
                farbe = AMPEL_FARBE.get(p.ampel, GRAU)
                st.markdown(
                    f"<span style='color:{farbe};font-weight:600'>{AMPEL_TEXT.get(p.ampel, p.ampel)}</span> "
                    f"{p.fixtermin.liefertag:%d.%m.%Y} {html.escape(p.fixtermin.lieferant_kurz)} / {html.escape(p.fixtermin.kunde_kurz)}: "
                    f"{html.escape(p.grund)}<br><small>{html.escape('; '.join(p.aktionen))}</small>",
                    unsafe_allow_html=True,
                )
    if bericht.entwuerfe:
        with st.expander(f"Entwürfe ({len(bericht.entwuerfe)}, nicht gesendet)", expanded=True):
            for e in bericht.entwuerfe:
                st.markdown(f"- {e.typ.value}: „{e.betreff}“ an {', '.join(e.an) or '(kein Empfänger)'} → `{e.speicherort or '-'}`")
    geaendert = _zeige_vorschlaege_termine(bericht, settings, kontext)
    if not geaendert:
        geaendert = _zeige_vorschlaege_kontakte(bericht, settings, kontext)
    if bericht.fehler:
        for f in bericht.fehler:
            st.error(f)
    if hinweise or protokoll:
        with st.expander(f"Hinweise ({len(hinweise)}) und Änderungen ({len(protokoll)})"):
            for h in hinweise:
                st.markdown(f"- {h}")
            for p in protokoll:
                st.markdown(f"- Änderung: {p}")
    return geaendert


# ---------------------------------------------------------------------------
# Seite "Dossiers"
# ---------------------------------------------------------------------------


def checklisten_fortschritt(dossier: Dossier) -> tuple[int, int]:
    """(erledigte, alle) Punkte der Checkliste."""
    return sum(1 for v in dossier.checkliste.values() if v), len(dossier.checkliste)


def dossier_zeilen(dossiers: Iterable[Dossier]) -> list[dict[str, Any]]:
    zeilen: list[dict[str, Any]] = []
    for d in dossiers:
        erledigt, alle = checklisten_fortschritt(d)
        zeilen.append(
            {
                "Name": d.name,
                "Firma": d.firma_id or "",
                "Kunde": d.kunde_kurz or "",
                "Lieferant": d.lieferant_kurz or "",
                "Bestellnr.": d.bestellnummer or d.kundenbestellnummer or "",
                "Fixtermin": d.fixtermin.strftime("%d.%m.%Y") if d.fixtermin else "",
                "Checkliste": f"{erledigt}/{alle}" if alle else "",
                "Offene Punkte": len(d.offene_punkte),
                "Zielordner-Vorschlag": d.vorgeschlagener_zielordner or "",
                "Pfad": d.ordner,
            }
        )
    return zeilen


def zeige_seite_dossiers(settings: Settings, kontext: UiKontext) -> None:
    """Seite "Dossiers": Liste der vorangefertigten Dossiers mit Details."""
    st.header("Dossiers – vorangefertigte Vorgangsmappen")
    dry_run = kontext.dry_run()
    try:
        verwaltung = nachbereitung.dossier_verwaltung(settings, dry_run=dry_run)
        dossiers = verwaltung.liste()
    except Exception as exc:  # noqa: BLE001 - Backend/Konfiguration
        logger.exception("Dossiers konnten nicht gelesen werden")
        st.error(f"Dossiers konnten nicht gelesen werden: {_fehlertext(exc)}")
        return
    ort = verwaltung.wurzel.lokaler_pfad or verwaltung.wurzel.web_url or settings.dossier.ordner_name
    st.caption(f"Ordner: {ort} ({'lokal / Dry Run' if dry_run or settings.m365.backend == 'fake' else 'OneDrive'})")
    if not dossiers:
        st.info("Noch keine Dossiers – sie entstehen nach dem Buchen (Dry Run: lokal unter dossier.lokaler_ordner).")
        return
    st.dataframe(pd.DataFrame(dossier_zeilen(dossiers)), hide_index=True, width="stretch")
    for d in dossiers:
        erledigt, alle = checklisten_fortschritt(d)
        with st.expander(f"{d.name} – Checkliste {erledigt}/{alle}, {len(d.offene_punkte)} offene Punkte"):
            st.markdown(f"**Pfad:** `{d.ordner}`  \n**Zielordner-Vorschlag:** {d.vorgeschlagener_zielordner or '-'}")
            if d.checkliste:
                st.markdown("**Checkliste:** " + ", ".join(f"{'☑' if v else '☐'} {k}" for k, v in d.checkliste.items()))
            if d.dateien:
                st.markdown("**Dateien:** " + ", ".join(f"`{f.name}`" for f in d.dateien))
            for p in d.offene_punkte:
                st.markdown(f"- offen: {p}")
            if settings.dossier.verschieben_erlauben:
                st.caption("Verschieben in die Auftragsablage ist aktiviert (dossier.verschieben_erlauben) – noch nicht umgesetzt; bitte manuell einsortieren.")


# ---------------------------------------------------------------------------
# Seite "Rabatte"
# ---------------------------------------------------------------------------


def aktionen_zeilen(
    aktionen: Iterable[Aktion], *, heute: date | None = None, firma_id: str | None = None, nur_gueltige: bool = True
) -> list[dict[str, Any]]:
    """Zeilen der Aktionsübersicht (gültig am ``heute``; ``firma_id`` filtert auf Firma oder "alle")."""
    tag = heute or date.today()
    zeilen: list[dict[str, Any]] = []
    for a in sorted(aktionen, key=lambda x: (x.von, x.bis, x.aktion_id)):
        if nur_gueltige and not a.gilt_am(tag):
            continue
        if firma_id and a.firma_id not in (None, firma_id):
            continue
        zeilen.append(
            {
                "ID": a.aktion_id,
                "Aktion": a.bezeichnung or "",
                "Firma": a.firma_id or "alle",
                "Kunde": a.kunde_kurz or "alle",
                "Lieferant": a.lieferant_kurz or "alle",
                "Artikel": ", ".join(a.artikelnummern) or (a.artikel_text or ""),
                "Von": a.von.strftime("%d.%m.%Y"),
                "Bis": a.bis.strftime("%d.%m.%Y"),
                "Rabatt %": formatiere_menge(a.rabatt_prozent),
                "Rabatt €": formatiere_betrag(a.rabatt_eur),
                "Aktionspreis": formatiere_betrag(a.aktionspreis),
                "Quelle": a.quelle,
            }
        )
    return zeilen


def rabattliste_einlesen(dateiname: str, inhalt: bytes, settings: Settings, *, jetzt: datetime | None = None) -> tuple[Path, Rabattliste]:
    """Hochgeladene Rabattliste nach ``rabatte.rabatt_pdf_ordner`` speichern (atomar) und einlesen."""
    from auftragsautomat.rabatte import lese_rabattliste

    name = Path(dateiname or "").name
    endung = Path(name).suffix.lower()
    if endung not in _RABATTLISTE_ENDUNGEN:
        raise UploadAbgelehnt(f"Dateityp '{endung or 'ohne Endung'}' wird für Rabattlisten nicht unterstützt (erlaubt: {', '.join(_RABATTLISTE_ENDUNGEN)}).")
    if not inhalt:
        raise UploadAbgelehnt("Die Datei ist leer.")
    stamm = benennung.bereinige(Path(name).stem, settings) or "Rabattliste"
    ordner = Path(settings.rabatte.rabatt_pdf_ordner)
    ordner.mkdir(parents=True, exist_ok=True)
    ziel = freier_pfad(ordner, f"{stamm}{endung}", jetzt=jetzt or datetime.now())
    schreibe_atomar(ziel, inhalt)
    logger.info("Rabattliste gespeichert: %s", ziel)
    return ziel, lese_rabattliste(ziel, settings)


def aktionen_zu_tabelle(aktionen: Iterable[Aktion]) -> list[dict[str, Any]]:
    """Aktionen als editierbare Zeilen (Decimal -> float, Listen -> Text, Häkchen "uebernehmen")."""
    zeilen: list[dict[str, Any]] = []
    for a in aktionen:
        zeilen.append(
            {
                "uebernehmen": True,
                "aktion_id": a.aktion_id,
                "bezeichnung": a.bezeichnung or "",
                "firma_id": a.firma_id or "",
                "kunde_kurz": a.kunde_kurz or "",
                "lieferant_kurz": a.lieferant_kurz or "",
                "artikel_text": a.artikel_text or "",
                "artikelnummern": ", ".join(a.artikelnummern),
                "von": a.von,
                "bis": a.bis,
                "rabatt_prozent": float(a.rabatt_prozent) if a.rabatt_prozent is not None else None,
                "rabatt_eur": float(a.rabatt_eur) if a.rabatt_eur is not None else None,
                "aktionspreis": float(a.aktionspreis) if a.aktionspreis is not None else None,
                "konfidenz": a.konfidenz,
                "quelle": a.quelle,
            }
        )
    return zeilen


def _leer(wert: Any) -> bool:
    if wert is None:
        return True
    if isinstance(wert, str):
        return not wert.strip()
    try:
        return bool(pd.isna(wert))
    except (TypeError, ValueError):
        return False


def _datum_aus(wert: Any) -> date | None:
    if _leer(wert):
        return None
    if isinstance(wert, datetime):
        return wert.date()
    if isinstance(wert, date):
        return wert
    if hasattr(wert, "to_pydatetime"):
        return wert.to_pydatetime().date()
    return parse_datum(str(wert))


def _decimal_aus(wert: Any) -> Decimal | None:
    if _leer(wert):
        return None
    if isinstance(wert, Decimal):
        return wert
    if isinstance(wert, bool):
        return None
    if isinstance(wert, int | float):
        return Decimal(str(wert))
    try:
        return parse_dezimal(str(wert))
    except (InvalidOperation, ValueError):
        return None


def _text_aus(wert: Any) -> str | None:
    if _leer(wert):
        return None
    text = str(wert).strip()
    return text or None


def aktionen_aus_tabelle(zeilen: Sequence[Mapping[str, Any]], vorlage: Iterable[Aktion]) -> tuple[list[Aktion], list[str]]:
    """Editierte Zeilen zurück in ``Aktion``-Objekte; nur Zeilen mit Häkchen "uebernehmen".

    Felder, die die Tabelle nicht zeigt (``bezug``, ``bemerkung``, ``kundennummern``, ``aktiv``), kommen aus der
    ``vorlage`` (gleiche ``aktion_id``). Ungültige Zeilen (kein Datum, Bis vor Von, kein Rabatt) werden
    mit Meldung übersprungen.
    """
    nach_id = {a.aktion_id: a for a in vorlage}
    aktionen: list[Aktion] = []
    fehler: list[str] = []
    for nr, zeile in enumerate(zeilen, start=1):
        if "uebernehmen" in zeile and not bool(zeile.get("uebernehmen")):
            continue
        aktion_id = _text_aus(zeile.get("aktion_id")) or f"ui:{nr}"
        alt = nach_id.get(aktion_id)
        von, bis = _datum_aus(zeile.get("von")), _datum_aus(zeile.get("bis"))
        if von is None or bis is None:
            fehler.append(f"Zeile {nr} ({aktion_id}): Von/Bis fehlt oder ist kein Datum – übersprungen.")
            continue
        if bis < von:
            fehler.append(f"Zeile {nr} ({aktion_id}): Bis ({bis:%d.%m.%Y}) liegt vor Von ({von:%d.%m.%Y}) – übersprungen.")
            continue
        rabatt_prozent = _decimal_aus(zeile.get("rabatt_prozent"))
        rabatt_eur = _decimal_aus(zeile.get("rabatt_eur"))
        aktionspreis = _decimal_aus(zeile.get("aktionspreis"))
        if rabatt_prozent is None and rabatt_eur is None and aktionspreis is None:
            fehler.append(f"Zeile {nr} ({aktion_id}): kein Rabatt (% / € / Aktionspreis) – übersprungen.")
            continue
        artikelnummern = [t.strip() for t in str(zeile.get("artikelnummern") or "").replace(";", ",").split(",") if t.strip()]
        quelle = _text_aus(zeile.get("quelle")) or (alt.quelle if alt is not None else "ui:rabatte")
        try:
            aktionen.append(
                Aktion(
                    aktion_id=aktion_id,
                    bezeichnung=_text_aus(zeile.get("bezeichnung")),
                    firma_id=_text_aus(zeile.get("firma_id")),
                    kunde_kurz=_text_aus(zeile.get("kunde_kurz")),
                    kundennummern=list(alt.kundennummern) if alt is not None else [],
                    lieferant_kurz=_text_aus(zeile.get("lieferant_kurz")),
                    artikelnummern=artikelnummern,
                    artikel_text=_text_aus(zeile.get("artikel_text")),
                    von=von,
                    bis=bis,
                    rabatt_prozent=rabatt_prozent,
                    rabatt_eur=rabatt_eur,
                    aktionspreis=aktionspreis,
                    bezug=alt.bezug if alt is not None else "liefertag",
                    quelle=quelle,
                    konfidenz=alt.konfidenz if alt is not None else None,
                    bemerkung=alt.bemerkung if alt is not None else None,
                    aktiv=alt.aktiv if alt is not None else True,
                )
            )
        except (ValueError, TypeError) as exc:
            fehler.append(f"Zeile {nr} ({aktion_id}): ungültig ({exc}) – übersprungen.")
    return aktionen, fehler


def aktionen_uebernehmen(aktionen: Iterable[Aktion], settings: Settings) -> Path:
    """Aktionen in das Gedächtnis ``rabatte.gedaechtnis_datei`` schreiben (zusammenführen) und den Cache leeren."""
    from auftragsautomat.rabatte import leere_cache, speichere_aktionen

    ziel = speichere_aktionen(list(aktionen), Path(settings.rabatte.gedaechtnis_datei))
    leere_cache()
    return ziel


def zeige_seite_rabatte(settings: Settings, kontext: UiKontext) -> bool:
    """Seite "Rabatte": gültige Aktionen heute, Upload "Rabattliste (PDF)" -> editierbare Tabelle -> "Übernehmen"."""
    from auftragsautomat.rabatte import alle_aktionen

    st.header("Rabatte – Aktionszeiträume und Rabattlisten")
    if not settings.rabatte.aktiv:
        st.warning("Aktionsrabatte sind in den Einstellungen abgeschaltet (rabatte.aktiv = false).")
    try:
        aktionen = alle_aktionen(settings, cache=False)
    except Exception as exc:  # noqa: BLE001 - alle_aktionen fängt selbst; Sicherheitsnetz
        logger.exception("Aktionen konnten nicht geladen werden")
        st.error(f"Aktionen konnten nicht geladen werden: {_fehlertext(exc)}")
        aktionen = []
    spalten = st.columns([1, 1, 2])
    stichtag = spalten[0].date_input("Stichtag", value=date.today(), key="rabatte_stichtag", format="DD.MM.YYYY")
    alle = spalten[1].checkbox("auch abgelaufene/künftige", key="rabatte_alle", value=False)
    zeilen = aktionen_zeilen(aktionen, heute=stichtag if isinstance(stichtag, date) else None, nur_gueltige=not alle)
    st.subheader(f"Aktionen ({len(zeilen)} von {len(aktionen)}{'' if alle else f' gültig am {stichtag:%d.%m.%Y}' if isinstance(stichtag, date) else ''})")
    if zeilen:
        st.dataframe(pd.DataFrame(zeilen), hide_index=True, width="stretch")
    else:
        st.info(
            "Keine Aktionen – Quellen: Aktionszeiträume-Excel (rabatte.aktionen_datei) und Gedächtnis "
            f"({Path(settings.rabatte.gedaechtnis_datei).name})."
        )

    st.subheader("Rabattliste (PDF) hochladen")
    datei = st.file_uploader("Rabattliste", type=[e.lstrip(".") for e in _RABATTLISTE_ENDUNGEN], key="rabatte_datei")
    if st.button("Einlesen", key="rabatte_einlesen", disabled=datei is None, width="stretch"):
        if datei is None:
            kontext.melde("warning", "Bitte zuerst eine Rabattliste auswählen.")
            return True
        try:
            with st.spinner(f"Lese {datei.name} …"):
                pfad, liste = rabattliste_einlesen(datei.name, datei.getvalue(), settings)
        except UploadAbgelehnt as exc:
            kontext.melde("error", f"Upload abgelehnt: {exc}")
            return True
        except Exception as exc:  # noqa: BLE001 - kaputte PDF, API-Fehler
            logger.exception("Rabattliste konnte nicht gelesen werden")
            kontext.melde("error", f"Rabattliste konnte nicht gelesen werden: {_fehlertext(exc)}")
            return True
        st.session_state[SS_RABATTLISTE] = liste
        st.session_state[SS_RABATTLISTE_PFAD] = str(pfad)
        kontext.melde("success", f"{datei.name} gespeichert ({pfad}) und gelesen: {len(liste.aktionen)} Aktionen ({liste.extraktionsmethode.value}).")
        return True

    liste: Rabattliste | None = st.session_state.get(SS_RABATTLISTE)
    if liste is None:
        return False
    st.markdown(f"**Eingelesen:** `{st.session_state.get(SS_RABATTLISTE_PFAD, liste.quelldatei)}` – {len(liste.aktionen)} Aktionen; bitte prüfen und korrigieren.")
    for h in liste.hinweise:
        (st.error if h.schweregrad == Schweregrad.FEHLER else st.warning if h.schweregrad == Schweregrad.WARNUNG else st.info)(
            f"[{h.code.value}] {h.text}"
        )
    if not liste.aktionen:
        return False
    tabelle = pd.DataFrame(aktionen_zu_tabelle(liste.aktionen), columns=list(_TABELLE_SPALTEN))
    # TODO-PRUEFEN: Im echten Browser prüfen, dass Datums- und Zahlenspalten des data_editor nach dem Bearbeiten als
    # date/float zurückkommen (AppTest kann den Editor nicht bedienen; aktionen_aus_tabelle nimmt auch Text/Timestamp).
    bearbeitet = st.data_editor(
        tabelle,
        key="rabatte_editor",
        hide_index=True,
        width="stretch",
        num_rows="fixed",
        column_config={
            "uebernehmen": st.column_config.CheckboxColumn("Übernehmen"),
            "aktion_id": st.column_config.TextColumn("ID", disabled=True),
            "bezeichnung": "Aktion",
            "firma_id": "Firma",
            "kunde_kurz": "Kunde",
            "lieferant_kurz": "Lieferant",
            "artikel_text": "Artikel",
            "artikelnummern": "Artikelnummern",
            "von": st.column_config.DateColumn("Von", format="DD.MM.YYYY"),
            "bis": st.column_config.DateColumn("Bis", format="DD.MM.YYYY"),
            "rabatt_prozent": st.column_config.NumberColumn("Rabatt %", min_value=0.0, max_value=100.0, step=0.5),
            "rabatt_eur": st.column_config.NumberColumn("Rabatt €", min_value=0.0, step=0.01),
            "aktionspreis": st.column_config.NumberColumn("Aktionspreis", min_value=0.0, step=0.01),
            "konfidenz": st.column_config.NumberColumn("Konfidenz", disabled=True),
            "quelle": st.column_config.TextColumn("Quelle", disabled=True),
        },
    )
    if st.button("Übernehmen", key="rabatte_uebernehmen", type="primary", width="stretch"):
        zeilen_neu = bearbeitet.to_dict("records") if hasattr(bearbeitet, "to_dict") else list(bearbeitet)
        neue, fehler = aktionen_aus_tabelle(zeilen_neu, liste.aktionen)
        for f in fehler:
            kontext.melde("warning", f)
        if not neue:
            kontext.melde("error", "Keine gültige Aktion zum Übernehmen.")
            return True
        try:
            ziel = aktionen_uebernehmen(neue, settings)
        except Exception as exc:  # noqa: BLE001 - Schreibfehler
            logger.exception("Aktionen konnten nicht gespeichert werden")
            kontext.melde("error", f"Aktionen nicht gespeichert: {_fehlertext(exc)}")
            return True
        st.session_state.pop(SS_RABATTLISTE, None)
        st.session_state.pop(SS_RABATTLISTE_PFAD, None)
        kontext.melde("success", f"{len(neue)} Aktionen in {ziel} übernommen; sie gelten ab sofort bei der Prüfung neuer Aufträge.")
        return True
    return False


# ---------------------------------------------------------------------------
# Seite "Einstellungen"
# ---------------------------------------------------------------------------


def _ref_text(ref: Any) -> str:
    if ref.lokaler_pfad is not None:
        return f"lokal: {ref.lokaler_pfad}"
    if ref.web_url:
        return f"Graph: {ref.web_url}"
    if ref.drive_id and ref.item_id:
        return f"Graph: {ref.drive_id}/{ref.item_id}"
    return "nicht konfiguriert"


def einstellungen_zeilen(settings: Settings, *, anmeldestatus: str | None = None) -> list[tuple[str, str]]:
    """Übersicht der wichtigsten Einstellungen (nur Anzeige, nichts wird geändert)."""
    zeilen: list[tuple[str, str]] = [
        ("Projektstamm", str(projekt_stamm())),
        ("Dry Run (settings.dry_run)", "ja" if settings.dry_run else "nein – scharfe Läufe möglich"),
        ("Eingang", f"{settings.eingang_ordner} (OneDrive-Ordner „{settings.eingang.ordner_name}“)"),
        ("Upload erlaubt", f"{'ja' if settings.eingang.upload_erlaubt else 'nein'} ({', '.join(erlaubte_endungen(settings))}, max. {settings.eingang.upload_max_mb} MB)"),
        ("Pakete", str(zustand.paket_ordner(settings))),
        ("Microsoft 365 Backend", settings.m365.backend + (f" – {anmeldestatus}" if anmeldestatus else "")),
        ("Fixterminüberwachung", f"{_ref_text(settings.m365.fixtermin_datei)} (Blatt {settings.m365.fixtermin_datei.blatt or '-'})"),
        ("Kontaktliste", _ref_text(settings.m365.kontaktliste_datei)),
        ("Dossier-Ordner", f"{_ref_text(settings.m365.dossier_ordner)}; Dry Run: {settings.dossier.lokaler_ordner}"),
        ("Entwürfe (Dry Run)", str(settings.kommunikation.entwuerfe_ordner)),
        ("Absender", f"{settings.kommunikation.absender_name or '-'} <{settings.kommunikation.absender_email or '-'}>; CC: {', '.join(settings.kommunikation.cc_standard) or '-'}"),
        ("Ablage", f"{'aktiv' if settings.ablage.aktiv else 'aus'}; Ordner {_ref_text(settings.ablage.auftraege_ordner)}; Struktur {settings.ablage.struktur}; "
                   f"mit Provision: {', '.join(settings.ablage.firmen_mit_provision) or '-'}"),
        ("Überwachung", f"{settings.ueberwachung.tage_voraus} Tage voraus, Mails {settings.ueberwachung.mails_rueckblick_tage} Tage, "
                         f"Nachfrage Lieferant ab {settings.ueberwachung.nachfrage_lieferant_werktage} / Spedition ab {settings.ueberwachung.nachfrage_spedition_werktage} Werktagen; "
                         f"Berichte: {settings.ueberwachung.berichte_ordner}"),
        ("Tracking", f"Quellen: {', '.join(settings.tracking.quellen) or '-'}; Standard-Spedition {settings.kontakte.standard_spedition}"),
        ("Rabatte", f"{'aktiv' if settings.rabatte.aktiv else 'aus'}; Excel {_ref_text(settings.rabatte.aktionen_datei)}; Gedächtnis {settings.rabatte.gedaechtnis_datei}; "
                    f"automatisch anwenden: {'ja' if settings.rabatte.automatisch_anwenden else 'nein'}; Bezug {settings.rabatte.bezug}"),
        ("Firmenwahl", f"Katalog {settings.firmenwahl.katalog_datei}; Gedächtnis {settings.firmenwahl.gedaechtnis_datei}; "
                       f"merken: {'ja' if settings.firmenwahl.merken else 'nein'}; fragen wenn unklar: {'ja' if settings.firmenwahl.fragen_wenn_unklar else 'nein'}"),
        ("Lexware PDF-Ausgabe", f"{settings.lexware.pdf_ausgabe} ({settings.lexware.pdf_drucker}) nach {settings.lexware.pdf_ausgabe_ordner}"),
    ]
    for firma in settings.firmen.values():
        formulare = firma.formulare
        zeilen.append(
            (
                f"Firma {firma.id}",
                f"{firma.name} (Kürzel {firma.kurz}); Verkaufsweg {firma.verkauf_weg}; Einkauf: {'ja' if firma.einkauf_bestellung else 'nein'}; "
                f"Lieferschein: {'erlaubt' if firma.lieferschein_erlaubt else 'nein'}; Formulare EN {formulare.einkauf_en or formulare.angebot_en or '-'}, "
                f"AB {formulare.verkauf_ab or '-'}; Kundengruppen: {', '.join(firma.kundengruppen) or '-'}; Ablage {firma.ablage_ordner or firma.kurz}",
            )
        )
    return zeilen


def zeige_seite_einstellungen(settings: Settings, kontext: UiKontext) -> None:
    """Seite "Einstellungen": Konfiguration anzeigen (Änderungen nur in config/settings.yaml)."""
    from auftragsautomat.m365.fabrik import pruefe_anmeldung

    st.header("Einstellungen")
    st.caption("Nur Anzeige – Änderungen in config/settings.yaml und config/firmen.yaml; danach die Oberfläche neu laden.")
    try:
        status = pruefe_anmeldung(settings)
    except Exception as exc:  # noqa: BLE001 - Status ist Komfort
        status = f"Status nicht ermittelbar: {_fehlertext(exc)}"
    zeilen = einstellungen_zeilen(settings, anmeldestatus=status)
    st.table(pd.DataFrame(zeilen, columns=["Einstellung", "Wert"]))
    st.caption(f"Dry-Run-Schalter der Oberfläche: {'an' if kontext.dry_run() else 'aus (SCHARF)'}")
