"""Monatliche Meldungen aus Lexware-Belegdaten: Intrastat, Schaumweinsteuer, Verpackung (LUCID / Systembetreiber).

Alle Berechnungen lesen nur Exporte aus Lexware (CSV/XLSX) sowie die Pflegedaten in
``config/meldungen/``. Es wird nichts an Behörden übertragen: die Module erzeugen
Prüflisten und Ausfüllhilfen, die in IDEV, im Zoll-Portal und bei LUCID / dem
Systembetreiber eingegeben bzw. hochgeladen werden.
"""
