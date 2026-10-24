"""Datenmodelle des Postfach-Assistenten (nur Standardbibliothek)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

Aktion = Literal["verschieben", "liegenlassen", "soft_delete", "ordner_fehlt"]
Antwort = Literal["ja", "nein", "pruefen"]

# Ordner-Erkennung an der Message-ID (Exchange-Store-Kennung im mittleren Teil der ID)
_ORDNER_KENNUNG = {
    "AAAAAAEMAAA": "Posteingang",
    "AAAAAAEJAAA": "Gesendete Elemente",
    "AAAAAAEPAAA": "Entwürfe",
    "AAAAAAEKAAA": "Gelöschte Elemente",
}


def parse_zeit(wert: str | datetime | None) -> datetime | None:
    """ISO-Zeitstempel (auch mit ``Z``) in ein zeitzonenbewusstes datetime wandeln."""
    if wert is None or wert == "":
        return None
    if isinstance(wert, datetime):
        return wert if wert.tzinfo else wert.replace(tzinfo=UTC)
    text = str(wert).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


@dataclass
class Mail:
    """Eine Mail, wie sie die Suche des Connectors liefert (plus optionale Zusatzinfos)."""

    id: str
    betreff: str = ""
    absender: str = ""
    absender_name: str = ""
    empfaenger: list[str] = field(default_factory=list)
    cc: list[str] = field(default_factory=list)
    empfangen: datetime | None = None
    gelesen: bool = True
    markiert: bool = False
    vorschau: str = ""
    anhaenge: bool = False
    wichtigkeit: str = "normal"
    kategorien: list[str] = field(default_factory=list)
    internet_message_id: str | None = None
    web_link: str | None = None
    konversation_id: str | None = None
    antwort_auf_eigene: bool | None = None  # wird von ``antworten.markiere_antworten`` gesetzt

    @classmethod
    def aus_suche(cls, roh: dict[str, Any]) -> Mail:
        """Aus einem Treffer von ``outlook_email_search`` (oder ``read_resource``) bauen."""

        def _liste(wert: Any) -> list[str]:
            if not wert:
                return []
            if isinstance(wert, str):
                return [wert]
            aus: list[str] = []
            for e in wert:
                if isinstance(e, dict):
                    adresse = e.get("address") or e.get("emailAddress", {}).get("address")
                    if adresse:
                        aus.append(str(adresse))
                elif e:
                    aus.append(str(e))
            return aus

        absender = roh.get("sender") or roh.get("from") or ""
        if isinstance(absender, dict):
            absender = absender.get("address") or absender.get("emailAddress", {}).get("address", "")
        kategorien = roh.get("categories") or roh.get("kategorien") or []
        return cls(
            id=str(roh.get("id") or roh.get("messageId") or ""),
            betreff=str(roh.get("subject") or roh.get("betreff") or ""),
            absender=str(absender or "").strip(),
            absender_name=str(roh.get("senderName") or roh.get("fromName") or ""),
            empfaenger=_liste(roh.get("recipients") or roh.get("toRecipients") or roh.get("to")),
            cc=_liste(roh.get("cc") or roh.get("ccRecipients")),
            empfangen=parse_zeit(roh.get("receivedDateTime") or roh.get("empfangen")),
            gelesen=bool(roh.get("isRead", roh.get("gelesen", True))),
            markiert=bool(roh.get("isFlagged", roh.get("markiert", False)))
            or str(roh.get("flagStatus", "")).lower() == "flagged",
            vorschau=str(roh.get("summary") or roh.get("bodyPreview") or roh.get("vorschau") or ""),
            anhaenge=bool(roh.get("hasAttachments", roh.get("anhaenge", False))),
            wichtigkeit=str(roh.get("importance") or roh.get("wichtigkeit") or "normal"),
            kategorien=[str(k) for k in kategorien],
            internet_message_id=roh.get("internetMessageId") or roh.get("internet_message_id"),
            web_link=roh.get("webLink") or roh.get("web_link"),
            konversation_id=roh.get("conversationId") or roh.get("konversation_id"),
            antwort_auf_eigene=roh.get("antwort_auf_eigene"),
        )

    @property
    def domain(self) -> str:
        return self.absender.rsplit("@", 1)[-1].lower() if "@" in self.absender else self.absender.lower()

    @property
    def ordner_laut_id(self) -> str | None:
        for kennung, name in _ORDNER_KENNUNG.items():
            if kennung in self.id:
                return name
        return None

    @property
    def text(self) -> str:
        return f"{self.betreff}\n{self.vorschau}"

    def alter_tage(self, heute: datetime | None = None) -> int | None:
        if self.empfangen is None:
            return None
        jetzt = heute or datetime.now(UTC)
        if jetzt.tzinfo is None:
            jetzt = jetzt.replace(tzinfo=UTC)
        return max(0, (jetzt - self.empfangen).days)


@dataclass
class Entscheidung:
    """Ergebnis der Klassifikation für eine Mail."""

    mail_id: str
    regel: str
    kategorie: str | None = None  # Name der Outlook-Kategorie (Farbe), falls zutreffend
    farbe: str | None = None
    ziel_ordner: str | None = None
    ziel_ordner_id: str | None = None
    aktion: Aktion = "liegenlassen"
    bleibt_grund: str | None = None  # gesetzt, wenn die Mail trotz Zielordner im Posteingang bleibt
    antwort: Antwort = "pruefen"
    antwort_grund: str = ""
    hinweise: list[str] = field(default_factory=list)
    betreff: str = ""
    absender: str = ""

    @property
    def wird_verschoben(self) -> bool:
        return self.aktion == "verschieben" and self.bleibt_grund is None

    def als_dict(self) -> dict[str, Any]:
        return asdict(self)
