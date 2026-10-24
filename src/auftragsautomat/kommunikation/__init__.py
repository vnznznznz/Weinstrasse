"""E-Mail-Entwürfe im Hausstil (nie senden) und Erkennung eingehender Mails (docs/AUSBAUSTUFE_2.md, 2.4).

* ``vorlagen``  – Jinja2-Vorlagen (``config/vorlagen/*.html.j2`` + ``*.txt.j2``), Filter, Signaturen
  (``config/signaturen.yaml``).
* ``anrede``    – Anrede ("Hallo Frau Beispiel!", "Dear Ms Example,") und Grußformel (``config/anrede.yaml``).
* ``benennung`` – Anhangsnamen über die Titulierung (``auftragsautomat.benennung``).
* ``entwuerfe`` – :class:`Entwurfsfabrik`: Bestellung ans Weingut, Auftragskopie an den Kunden,
  Speditionsauftrag, Lieferschein, Nachfragen, interne Erinnerung; Prüfung der Auftragskopie auf
  verbotene Wörter; Speichern über ein ``MailBackend``.
* ``eingang``   – :func:`klassifiziere` / :func:`sammle_ereignisse`: Speditions- und
  Lieferantenbestätigungen, Terminänderungen, Kontaktänderungs-Marker aus eingehenden Mails.
"""

from auftragsautomat.kommunikation.anrede import (
    AnredeRegeln,
    anrede_regeln_fuer_zuordner,
    baue_anrede,
    grussformel,
    lade_anrede_regeln,
)
from auftragsautomat.kommunikation.benennung import (
    anhangsname,
    auftragskopie_pdf,
    bestellung_pdf,
    firmenkuerzel,
    lieferschein_pdf,
    speditionsauftrag_pdf,
)
from auftragsautomat.kommunikation.eingang import klassifiziere, klassifiziere_alle, sammle_ereignisse
from auftragsautomat.kommunikation.entwuerfe import Entwurfsfabrik, erstelle_entwuerfe, pruefe_auftragskopie
from auftragsautomat.kommunikation.vorlagen import (
    SignaturBlock,
    SignaturKatalog,
    Vorlagen,
    VorlagenFehler,
    lade_signaturen,
    rendere,
    signatur_fuer,
    vorlagen_aus_settings,
)

__all__ = [
    "AnredeRegeln",
    "Entwurfsfabrik",
    "SignaturBlock",
    "SignaturKatalog",
    "Vorlagen",
    "VorlagenFehler",
    "anhangsname",
    "anrede_regeln_fuer_zuordner",
    "auftragskopie_pdf",
    "baue_anrede",
    "bestellung_pdf",
    "erstelle_entwuerfe",
    "firmenkuerzel",
    "grussformel",
    "klassifiziere",
    "klassifiziere_alle",
    "lade_anrede_regeln",
    "lade_signaturen",
    "lieferschein_pdf",
    "pruefe_auftragskopie",
    "rendere",
    "sammle_ereignisse",
    "signatur_fuer",
    "speditionsauftrag_pdf",
    "vorlagen_aus_settings",
]
