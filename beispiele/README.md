# Beispiel-Belege

Hier bitte echte (gern anonymisierte) Belege ablegen, mit denen die Erkennung getestet wird:

* `A_kundenbestellung/` – Kundenbestellungen als Text-PDF
* `B_eigene_bestellung/` – eigene Bestellungen an Weingüter (.msg/.eml aus Outlook oder PDF)
* `C_scan_preisliste/` – eingescannte Preislisten mit handschriftlichen Mengen (PDF/JPG/PNG)

Zu jedem Beleg optional eine gleichnamige `.soll.yaml` mit dem erwarteten Ergebnis, z. B.:

```yaml
firma: wik_leh
kunde: "3441"
lieferant: "72"
positionen:
  - artikelnummer: "1643"
    menge: 60
  - artikelnummer: "957"
    menge: 24
uebersprungen: ["DAA Neptun"]
```

Diese Soll-Dateien werden von den automatischen Tests gegen die Erkennung geprüft.
Layout, Spaltenaufbau und Formulierungen der Belege bitte **nicht** verändern.
