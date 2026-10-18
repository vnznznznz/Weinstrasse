# Auftrags-Automat für Lexware premium

Python-Werkzeuge rund um Lexware premium (vier Firmen): Bestell-PDFs einlesen und buchen
(im Aufbau, siehe `docs/ARCHITEKTUR.md`) sowie die **monatlichen Meldungen** Intrastat,
Schaumweinsteuer und Verpackung (Grüner Punkt / LUCID) aus den Lexware-Belegen
(`docs/MELDUNGEN.md`).

```bash
pip install -e ".[dev]"
auftragsautomat meldungen pruefen   --monat 2026-10 --vorlage   # Export + Meldedaten prüfen
auftragsautomat meldungen erstellen --monat 2026-10             # CSV/XLSX/Checkliste erzeugen
auftragsautomat meldungen fristen   --monat 2026-10
pytest && ruff check src tests
```
