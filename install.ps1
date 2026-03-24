#Requires -Version 5.1
<#
.SYNOPSIS
    One-click installer for the Speech Recognition WebSocket Service.
.DESCRIPTION
    - Checks for Python 3.8+ (installs via winget if missing)
    - Creates a Python virtual environment
    - Installs pip dependencies
    - Downloads the Vosk ASR model
    - Pre-downloads openwakeword models
#>

$ErrorActionPreference = "Stop"
$ProgressPreference    = "SilentlyContinue"   # speeds up Invoke-WebRequest

# ─── Colour helpers ────────────────────────────────────────────────────────────
function Write-Step  { param($msg) Write-Host "`n>>> $msg" -ForegroundColor Cyan }
function Write-Ok    { param($msg) Write-Host "    $msg" -ForegroundColor Green }
function Write-Warn  { param($msg) Write-Host "    [WARN] $msg" -ForegroundColor Yellow }
function Write-Fail  { param($msg) Write-Host "    [ERR]  $msg" -ForegroundColor Red }

# ─── Banner ───────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "═══════════════════════════════════════════════════" -ForegroundColor Cyan
Write-Host "   Speech Recognition Service  —  Installer        " -ForegroundColor Cyan
Write-Host "═══════════════════════════════════════════════════" -ForegroundColor Cyan

# ─── Script root ──────────────────────────────────────────────────────────────
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

# ─── Language selection ────────────────────────────────────────────────────────
Write-Host ""
Write-Host "Select ASR language model to download:" -ForegroundColor White
Write-Host "  1) Chinese (Mandarin) — vosk-model-small-cn-0.22  (~42 MB)"
Write-Host "  2) English            — vosk-model-small-en-us-0.15 (~40 MB)"
Write-Host "  3) Both"
$langChoice = Read-Host "Enter choice [1]"
if (-not $langChoice) { $langChoice = "1" }

$downloadCN = $langChoice -in @("1","3")
$downloadEN = $langChoice -in @("2","3")

# ─── 1. Python check ──────────────────────────────────────────────────────────
Write-Step "Checking Python installation"

$pythonExe = $null
foreach ($candidate in @("python", "python3", "py")) {
    try {
        $verOutput = & $candidate --version 2>&1
        if ($verOutput -match "Python (\d+)\.(\d+)") {
            $major = [int]$Matches[1]; $minor = [int]$Matches[2]
            if ($major -eq 3 -and $minor -ge 8) {
                # Prefer 64-bit
                $arch = & $candidate -c "import struct; print(struct.calcsize('P')*8)" 2>&1
                if ($arch -eq "64") {
                    $pythonExe = $candidate
                    Write-Ok "Found: $verOutput (64-bit)"
                    break
                } else {
                    Write-Warn "Found $verOutput but it is 32-bit (vosk requires 64-bit). Skipping."
                }
            }
        }
    } catch { }
}

if (-not $pythonExe) {
    Write-Warn "Python 3.8+ (64-bit) not found. Attempting install via winget..."
    try {
        winget install --id Python.Python.3.11 --silent `
            --accept-package-agreements --accept-source-agreements
        # Refresh PATH
        $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" +
                    [System.Environment]::GetEnvironmentVariable("Path","User")
        $pythonExe = "python"
        Write-Ok "Python installed via winget."
    } catch {
        Write-Fail "winget install failed. Please install Python 3.8+ (64-bit) from:"
        Write-Fail "  https://www.python.org/downloads/"
        Write-Fail "Ensure 'Add Python to PATH' is checked during setup."
        pause
        exit 1
    }
}

# ─── 2. Virtual environment ────────────────────────────────────────────────────
Write-Step "Setting up virtual environment (.venv)"

$venvDir  = Join-Path $ScriptDir ".venv"
$pipExe   = Join-Path $venvDir "Scripts\pip.exe"
$pyExe    = Join-Path $venvDir "Scripts\python.exe"

if (-not (Test-Path $pyExe)) {
    & $pythonExe -m venv $venvDir
    Write-Ok "Virtual environment created."
} else {
    Write-Ok "Virtual environment already exists."
}

# ─── 3. Install dependencies ───────────────────────────────────────────────────
Write-Step "Installing Python packages (may take a few minutes)"

& $pipExe install --upgrade pip --quiet
& $pipExe install -r (Join-Path $ScriptDir "requirements.txt")
Write-Ok "Packages installed."

# ─── 4. Download Vosk model(s) ────────────────────────────────────────────────
Write-Step "Downloading ASR model(s)"

$modelDir = Join-Path $ScriptDir "models"
New-Item -ItemType Directory -Force -Path $modelDir | Out-Null

function Download-VoskModel {
    param(
        [string]$ModelName,
        [string]$Url
    )
    $dest = Join-Path $modelDir $ModelName
    if (Test-Path $dest) {
        Write-Ok "Already downloaded: $ModelName"
        return
    }
    $zipPath = Join-Path $modelDir "$ModelName.zip"
    Write-Host "    Downloading $ModelName ..." -NoNewline
    try {
        Invoke-WebRequest -Uri $Url -OutFile $zipPath -UseBasicParsing
        Write-Host " done." -ForegroundColor Green
        Write-Host "    Extracting..." -NoNewline
        Expand-Archive -Path $zipPath -DestinationPath $modelDir -Force
        Remove-Item $zipPath -Force
        Write-Host " done." -ForegroundColor Green
        Write-Ok "$ModelName ready."
    } catch {
        Write-Fail "Download failed: $_"
        Write-Fail "Check your internet connection and try again."
        if (Test-Path $zipPath) { Remove-Item $zipPath -Force }
    }
}

if ($downloadCN) {
    Download-VoskModel `
        -ModelName "vosk-model-small-cn-0.22" `
        -Url "https://alphacephei.com/vosk/models/vosk-model-small-cn-0.22.zip"
}
if ($downloadEN) {
    Download-VoskModel `
        -ModelName "vosk-model-small-en-us-0.15" `
        -Url "https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip"

    # Update config.json to point to English model when only EN is downloaded
    if (-not $downloadCN) {
        $cfg = Get-Content (Join-Path $ScriptDir "config.json") -Raw | ConvertFrom-Json
        $cfg.asr.model_path = "models/vosk-model-small-en-us-0.15"
        $cfg | ConvertTo-Json -Depth 10 | Set-Content (Join-Path $ScriptDir "config.json") -Encoding UTF8
        Write-Ok "config.json updated to use English model."
    }
}

# ─── 5. Pre-download openwakeword models ──────────────────────────────────────
Write-Step "Pre-downloading wake word models"

$preDownload = @"
import sys, warnings
warnings.filterwarnings('ignore')
try:
    import openwakeword
    openwakeword.utils.download_models()
    print('Wake word models ready.')
except Exception as e:
    print(f'Warning: {e}', file=sys.stderr)
    print('Wake word models will be downloaded on first run.')
"@

& $pyExe -c $preDownload

# ─── Done ─────────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "═══════════════════════════════════════════════════" -ForegroundColor Green
Write-Host "   Installation complete!                          " -ForegroundColor Green
Write-Host "═══════════════════════════════════════════════════" -ForegroundColor Green
Write-Host ""
Write-Host "  Start the service  : double-click start.bat"
Write-Host "  List audio devices : .venv\Scripts\python.exe list_devices.py"
Write-Host "  Configuration      : edit config.json"
Write-Host ""
