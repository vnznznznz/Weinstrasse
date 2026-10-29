<#
.SYNOPSIS
    Richtet den Auftrags-Automaten auf dem Windows-PC mit Lexware premium ein.

.DESCRIPTION
    Prueft Windows, Projektordner und Python 3.13, legt die Python-Umgebung .venv an, installiert
    das Projekt mit den Extras windows, tracking und dev, fuehrt die Tests aus und prueft, ob
    Lexware (Framework.exe) laeuft. Zum Schluss werden die naechsten Schritte ausgegeben.

    Das Skript installiert nichts ausserhalb von .venv und aendert keine Systemeinstellungen.
    Ausnahme nur mit -MitBrowser: Playwright laedt den Browser fuer die Sendungsverfolgung in das
    Benutzerprofil (%LOCALAPPDATA%\ms-playwright).
    Es kann beliebig oft ausgefuehrt werden (idempotent) und bricht bei Fehlern mit einer klaren
    Meldung ab.

    Kompatibel mit Windows PowerShell 5.1. Die Datei enthaelt bewusst nur ASCII-Zeichen
    (PowerShell 5.1 liest Skripte ohne BOM als ANSI).

.PARAMETER OhneTests
    Tests (python -m pytest -q) ueberspringen.

.PARAMETER MitBrowser
    Zusaetzlich "python -m playwright install chromium" ausfuehren (Sendungsverfolgung).

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\windows\einrichten.ps1

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\windows\einrichten.ps1 -OhneTests
#>
[CmdletBinding()]
param(
    [switch]$OhneTests,
    [switch]$MitBrowser
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

# ------------------------------------------------------------------ Hilfsfunktionen

function Schreibe-Schritt([string]$Text) {
    Write-Host ''
    Write-Host "==> $Text" -ForegroundColor Cyan
}

function Schreibe-Ok([string]$Text) {
    Write-Host "    OK: $Text" -ForegroundColor Green
}

function Schreibe-Hinweis([string]$Text) {
    Write-Host "    Hinweis: $Text" -ForegroundColor Yellow
}

function Abbruch([string]$Text) {
    Write-Host ''
    Write-Host "FEHLER: $Text" -ForegroundColor Red
    Write-Host 'Einrichtung abgebrochen. Hilfe: docs\EINRICHTUNG_WINDOWS.md, Abschnitt "Fehlerbehebung".' -ForegroundColor Red
    exit 1
}

# Fuehrt ein externes Programm aus und liefert den Exit-Code.
# PowerShell 5.1 wandelt Ausgaben auf stderr (z. B. pip-Warnungen) bei ErrorActionPreference=Stop
# in Fehler um; deshalb hier voruebergehend 'Continue' und Auswertung ueber $LASTEXITCODE.
function Starte-Programm {
    param(
        [Parameter(Mandatory = $true)][string]$Datei,
        [string[]]$Argumente = @()
    )
    $vorher = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        # Ausgabe direkt anzeigen, damit sie nicht im Rueckgabewert der Funktion landet
        & $Datei @Argumente | Out-Host
        return $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $vorher
    }
}

# Wie Starte-Programm, liefert aber die Ausgabe (stdout) als Text; $null bei Fehler.
function Lies-Ausgabe {
    param(
        [Parameter(Mandatory = $true)][string]$Datei,
        [string[]]$Argumente = @()
    )
    $vorher = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $ausgabe = & $Datei @Argumente 2>$null
        if ($LASTEXITCODE -ne 0) { return $null }
        return (($ausgabe | Out-String).Trim())
    }
    catch {
        return $null
    }
    finally {
        $ErrorActionPreference = $vorher
    }
}

# Prueft einen Python-Aufruf; liefert z. B. "3 13 64" (Major, Minor, Bitness) oder $null.
$PythonPruefCode = 'import sys, struct; print(sys.version_info[0], sys.version_info[1], struct.calcsize(chr(80)) * 8)'

function Pruefe-Python {
    param(
        [Parameter(Mandatory = $true)][string]$Datei,
        [string[]]$Vorargumente = @()
    )
    $text = Lies-Ausgabe -Datei $Datei -Argumente ($Vorargumente + @('-c', $PythonPruefCode))
    if (-not $text) { return $null }
    $teile = ($text -split '\s+')
    if ($teile.Count -lt 3) { return $null }
    return [pscustomobject]@{
        Major   = [int]$teile[0]
        Minor   = [int]$teile[1]
        Bitness = [int]$teile[2]
    }
}

function Ist-Administrator {
    $identitaet = [Security.Principal.WindowsIdentity]::GetCurrent()
    $prinzipal = New-Object Security.Principal.WindowsPrincipal($identitaet)
    return $prinzipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

# ------------------------------------------------------------------ 1. Windows

Schreibe-Schritt 'Pruefe Betriebssystem'
if ($env:OS -ne 'Windows_NT') {
    Abbruch 'Dieses Skript laeuft nur unter Windows.'
}
Schreibe-Ok ("Windows, PowerShell {0}" -f $PSVersionTable.PSVersion)

# ------------------------------------------------------------------ 2. Projektordner

Schreibe-Schritt 'Pruefe Projektordner'
# Das Skript liegt in <Projekt>\scripts\windows\
$Projekt = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
if (-not (Test-Path -LiteralPath (Join-Path $Projekt 'pyproject.toml'))) {
    Abbruch "pyproject.toml nicht gefunden in '$Projekt'. Das Skript muss unter scripts\windows\ im Projektordner liegen."
}
Set-Location -LiteralPath $Projekt
Schreibe-Ok "Projektordner: $Projekt"
if ($Projekt -match 'OneDrive') {
    Schreibe-Hinweis 'Der Projektordner liegt in OneDrive. Empfohlen ist ein Ordner ausserhalb, z. B. C:\Auftragsautomat.'
}

# ------------------------------------------------------------------ 3. Python 3.13

Schreibe-Schritt 'Suche Python 3.13'
$PythonDatei = $null
$PythonVorargumente = @()
$gefunden = $null

if (Get-Command 'py' -ErrorAction SilentlyContinue) {
    $info = Pruefe-Python -Datei 'py' -Vorargumente @('-3.13')
    if ($info -and $info.Major -eq 3 -and $info.Minor -eq 13) {
        $PythonDatei = 'py'
        $PythonVorargumente = @('-3.13')
        $gefunden = $info
    }
}
if (-not $PythonDatei -and (Get-Command 'python' -ErrorAction SilentlyContinue)) {
    $info = Pruefe-Python -Datei 'python'
    if ($info -and $info.Major -eq 3 -and $info.Minor -eq 13) {
        $PythonDatei = 'python'
        $gefunden = $info
    }
    elseif ($info) {
        Schreibe-Hinweis ("'python' ist Version {0}.{1}, benoetigt wird 3.13." -f $info.Major, $info.Minor)
    }
}
if (-not $PythonDatei) {
    Abbruch 'Python 3.13 nicht gefunden. Installieren: python.org (64 bit, "Add python.exe to PATH" ankreuzen) oder "winget install Python.Python.3.13"; danach PowerShell neu oeffnen.'
}
Schreibe-Ok ("Python {0}.{1} ({2} bit) ueber '{3} {4}'" -f $gefunden.Major, $gefunden.Minor, $gefunden.Bitness, $PythonDatei, ($PythonVorargumente -join ' '))

# ------------------------------------------------------------------ 4. .venv

Schreibe-Schritt 'Python-Umgebung .venv'
$VenvPython = Join-Path $Projekt '.venv\Scripts\python.exe'
if (Test-Path -LiteralPath $VenvPython) {
    $venvInfo = Pruefe-Python -Datei $VenvPython
    if (-not $venvInfo) {
        Abbruch "Die vorhandene .venv ist defekt ($VenvPython startet nicht). Ordner .venv umbenennen (z. B. .venv_alt) und das Skript erneut ausfuehren."
    }
    if ($venvInfo.Major -ne 3 -or $venvInfo.Minor -ne 13) {
        Abbruch ("Die vorhandene .venv nutzt Python {0}.{1}. Ordner .venv umbenennen (z. B. .venv_alt) und das Skript erneut ausfuehren." -f $venvInfo.Major, $venvInfo.Minor)
    }
    Schreibe-Ok ("vorhanden (Python {0}.{1}, {2} bit)" -f $venvInfo.Major, $venvInfo.Minor, $venvInfo.Bitness)
}
else {
    $code = Starte-Programm -Datei $PythonDatei -Argumente ($PythonVorargumente + @('-m', 'venv', '.venv'))
    if ($code -ne 0 -or -not (Test-Path -LiteralPath $VenvPython)) {
        Abbruch "Anlegen der .venv fehlgeschlagen (Exit-Code $code)."
    }
    Schreibe-Ok 'angelegt'
}

# ------------------------------------------------------------------ 5. Pakete installieren

Schreibe-Schritt 'Installiere das Projekt in .venv (Extras windows, tracking, dev)'
$code = Starte-Programm -Datei $VenvPython -Argumente @('-m', 'pip', 'install', '--disable-pip-version-check', '--upgrade', 'pip')
if ($code -ne 0) {
    Abbruch "pip konnte nicht aktualisiert werden (Exit-Code $code). Internetverbindung pruefen."
}
$code = Starte-Programm -Datei $VenvPython -Argumente @('-m', 'pip', 'install', '--disable-pip-version-check', '-e', '.[windows,tracking,dev]')
if ($code -ne 0) {
    Abbruch "Installation der Pakete fehlgeschlagen (Exit-Code $code). Meldung oben lesen; haeufig hilft ein erneuter Aufruf."
}
Schreibe-Ok 'Pakete installiert'

Schreibe-Schritt 'Pruefe pywinauto (Lexware-Bedienung)'
$code = Starte-Programm -Datei $VenvPython -Argumente @('-c', 'import pywinauto, win32api; print(pywinauto.__version__)')
if ($code -ne 0) {
    Abbruch 'pywinauto/pywin32 nicht importierbar. Abhilfe: .venv\Scripts\python -m pip install --force-reinstall pywin32 pywinauto'
}
Schreibe-Ok 'pywinauto importierbar'

if ($MitBrowser) {
    Schreibe-Schritt 'Lade Browser fuer die Sendungsverfolgung (Playwright, Chromium)'
    $code = Starte-Programm -Datei $VenvPython -Argumente @('-m', 'playwright', 'install', 'chromium')
    if ($code -ne 0) {
        Abbruch "playwright install chromium fehlgeschlagen (Exit-Code $code)."
    }
    Schreibe-Ok 'Browser geladen'
}

# ------------------------------------------------------------------ 6. Tests

if ($OhneTests) {
    Schreibe-Schritt 'Tests uebersprungen (-OhneTests)'
}
else {
    Schreibe-Schritt 'Fuehre Tests aus (python -m pytest -q)'
    $code = Starte-Programm -Datei $VenvPython -Argumente @('-m', 'pytest', '-q')
    if ($code -ne 0) {
        Abbruch "Tests fehlgeschlagen (Exit-Code $code). Ausgabe oben an Claude geben."
    }
    Schreibe-Ok 'Tests gruen'
}

# ------------------------------------------------------------------ 7. Lexware

Schreibe-Schritt 'Pruefe, ob Lexware laeuft (Framework.exe)'
$istAdmin = Ist-Administrator
$prozesse = @(Get-Process -Name 'Framework' -ErrorAction SilentlyContinue)
if ($prozesse.Count -eq 0) {
    Schreibe-Hinweis 'Lexware laeuft nicht. Bitte Lexware selbst starten (nur einmal) und die gewuenschte Firma oeffnen. Das Tool startet Lexware nie.'
}
else {
    Schreibe-Ok ("Lexware laeuft ({0} Prozess(e) Framework.exe)" -f $prozesse.Count)
    if ($prozesse.Count -gt 1) {
        Schreibe-Hinweis 'Mehr als ein Framework.exe-Prozess. Bitte pruefen, ob Lexware nur einmal geoeffnet ist.'
    }
    foreach ($p in $prozesse) {
        $pfad = $null
        try { $pfad = $p.Path } catch { $pfad = $null }
        if ($pfad) {
            Write-Host "    Pfad: $pfad"
        }
        elseif (-not $istAdmin) {
            Schreibe-Hinweis 'Pfad von Framework.exe nicht lesbar - Lexware laeuft vermutlich als Administrator. Dann Claude bzw. das Terminal ebenfalls "Als Administrator ausfuehren".'
        }
    }
}
if ($istAdmin) {
    Schreibe-Hinweis 'Diese PowerShell laeuft als Administrator. Fuer die Arbeit mit Claude nur noetig, wenn auch Lexware als Administrator laeuft.'
}

# ------------------------------------------------------------------ 8. Naechste Schritte

$Programm = Join-Path $Projekt '.venv\Scripts\auftragsautomat.exe'
$erkundenVorhanden = $false
if (Test-Path -LiteralPath $Programm) {
    $hilfe = Lies-Ausgabe -Datei $Programm -Argumente @('lexware-erkunden', '--help')
    $erkundenVorhanden = [bool]$hilfe
}

Write-Host ''
Write-Host 'Einrichtung abgeschlossen.' -ForegroundColor Green
Write-Host ''
Write-Host 'Naechste Schritte:'
Write-Host '  1. Lexware starten (nur einmal), gewuenschte Firma oeffnen, offene Belege schliessen.'
Write-Host '  2. Claude Code in diesem Ordner oeffnen und eingeben:'
Write-Host '       Lies CLAUDE.md und starte die Lexware-Kalibrierung mit Phase 0.'
Write-Host '  3. Oberflaeche erkunden und mit config\lexware_controls.yaml abgleichen (nur lesend):'
Write-Host '       .venv\Scripts\auftragsautomat lexware-erkunden --alle-fenster --abgleich'
if (-not $erkundenVorhanden) {
    Schreibe-Hinweis 'Der Befehl lexware-erkunden ist in diesem Stand noch nicht verfuegbar (Projekt aktualisieren: git pull).'
}
Write-Host ''
Write-Host 'Anleitung: docs\EINRICHTUNG_WINDOWS.md'
exit 0
