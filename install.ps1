#Requires -Version 5.1
<#
.SYNOPSIS
    语音识别服务 — Windows 一键安装程序
    Speech Recognition Service — Windows One-Click Installer

.DESCRIPTION
    支持 / Supports:
      - Windows 10 (Build 17763 / 1809+) and Windows 11
      - x64 CPU (Intel 9th gen+ / AMD Ryzen 1xxx+)
      - With or without a dedicated GPU
        * NVIDIA  → onnxruntime-gpu  (CUDA 11 or 12 autodetected)
        * Any GPU → onnxruntime-directml  (DirectX 12, NVIDIA/AMD/Intel)
        * CPU only → onnxruntime  (default, works everywhere)

    Steps performed:
      1. System compatibility check (OS / arch / VC++ runtime)
      2. GPU detection & acceleration choice
      3. Python 3.11 64-bit detection / installation
      4. Virtual environment creation
      5. Pip packages (GPU-aware)
      6. Vosk ASR model download
      7. openwakeword model pre-download
      8. Quick smoke test
#>

$ErrorActionPreference = "Stop"
$ProgressPreference    = "SilentlyContinue"   # faster Invoke-WebRequest

# ──────────────────────────────────────────────────────────────────────────────
# Colour helpers
# ──────────────────────────────────────────────────────────────────────────────
function Write-Step { param($msg) Write-Host "`n[>>>>] $msg" -ForegroundColor Cyan }
function Write-Ok   { param($msg) Write-Host "  [OK] $msg"   -ForegroundColor Green }
function Write-Warn { param($msg) Write-Host " [!!] $msg"   -ForegroundColor Yellow }
function Write-Fail { param($msg) Write-Host " [XX] $msg"   -ForegroundColor Red }
function Write-Info { param($msg) Write-Host "      $msg"   -ForegroundColor White }

# ──────────────────────────────────────────────────────────────────────────────
# Script root
# ──────────────────────────────────────────────────────────────────────────────
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

# ──────────────────────────────────────────────────────────────────────────────
# STEP 0 — System information banner
# ──────────────────────────────────────────────────────────────────────────────
$osInfo  = Get-WmiObject -Class Win32_OperatingSystem
$cpuInfo = Get-WmiObject -Class Win32_Processor | Select-Object -First 1
$gpuList = @(Get-WmiObject -Class Win32_VideoController |
              Where-Object { $_.AdapterRAM -gt 0 -or $_.Description -ne "" } |
              Select-Object -ExpandProperty Name)

$border = "=" * 62
Write-Host "`n$border" -ForegroundColor Cyan
Write-Host "  语音识别服务  /  Speech Recognition Service" -ForegroundColor Cyan
Write-Host "  Installer v2.0" -ForegroundColor Cyan
Write-Host $border -ForegroundColor Cyan
Write-Host "  OS  : $($osInfo.Caption) (Build $($osInfo.BuildNumber))"
Write-Host "  CPU : $($cpuInfo.Name.Trim())"
foreach ($g in $gpuList) { Write-Host "  GPU : $g" }
Write-Host $border -ForegroundColor Cyan

# ──────────────────────────────────────────────────────────────────────────────
# STEP 1 — OS compatibility check
# ──────────────────────────────────────────────────────────────────────────────
Write-Step "Checking OS compatibility"

[int]$build = $osInfo.BuildNumber
if ($build -lt 17763) {
    Write-Fail "Windows 10 Build 1809 (17763) or later is required."
    Write-Fail "Your build: $build.  Please update Windows and retry."
    pause; exit 1
}

if ($build -ge 22000) {
    Write-Ok "Windows 11 (Build $build) — fully supported."
} else {
    Write-Ok "Windows 10 (Build $build) — fully supported."
}

# Architecture
if ([System.Environment]::Is64BitOperatingSystem) {
    Write-Ok "64-bit OS detected."
} else {
    Write-Fail "32-bit Windows is not supported (vosk requires 64-bit)."
    pause; exit 1
}

# ──────────────────────────────────────────────────────────────────────────────
# STEP 2 — Visual C++ Redistributable check
# ──────────────────────────────────────────────────────────────────────────────
Write-Step "Checking Visual C++ 2015-2022 Redistributable (x64)"

function Test-VcRedist {
    # Check registry (VS 14.x covers 2015 / 2017 / 2019 / 2022)
    $regPaths = @(
        "HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64",
        "HKLM:\SOFTWARE\WOW6432Node\Microsoft\VisualStudio\14.0\VC\Runtimes\x64"
    )
    foreach ($p in $regPaths) {
        $r = Get-ItemProperty $p -ErrorAction SilentlyContinue
        if ($r -and [int]$r.Bld -ge 27) { return $true }
    }
    # Fallback: ARP scan
    $found = Get-ChildItem "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
                            "HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall" `
             -ErrorAction SilentlyContinue |
             Get-ItemProperty -ErrorAction SilentlyContinue |
             Where-Object {
                 ($_.DisplayName -match "Microsoft Visual C\+\+ 201[59]-202[0-9]") -and
                 ($_.DisplayName -match "x64")
             }
    return ($null -ne $found -and @($found).Count -gt 0)
}

if (Test-VcRedist) {
    Write-Ok "Visual C++ Redistributable is present."
} else {
    Write-Warn "Visual C++ Redistributable (x64) not found."
    Write-Info "sounddevice (PortAudio) requires MSVC runtime."
    Write-Info "Downloading and installing vc_redist.x64.exe ..."
    $vcUrl  = "https://aka.ms/vs/17/release/vc_redist.x64.exe"
    $vcPath = Join-Path $env:TEMP "vc_redist.x64.exe"
    try {
        Invoke-WebRequest -Uri $vcUrl -OutFile $vcPath -UseBasicParsing
        Start-Process -FilePath $vcPath -ArgumentList "/quiet /norestart" -Wait
        Remove-Item $vcPath -Force -ErrorAction SilentlyContinue
        Write-Ok "Visual C++ Redistributable installed."
    } catch {
        Write-Warn "Auto-install failed: $_"
        Write-Warn "Please download manually from: https://aka.ms/vs/17/release/vc_redist.x64.exe"
        Write-Warn "Install it, then re-run this installer."
        # Not fatal — sounddevice may still work if runtime is available some other way
    }
}

# ──────────────────────────────────────────────────────────────────────────────
# STEP 3 — GPU detection
# ──────────────────────────────────────────────────────────────────────────────
Write-Step "Detecting GPU for hardware acceleration"

$hasNvidia  = $false
$cudaVer    = $null        # e.g. [version]"12.4"
$hasAnyGPU  = ($gpuList.Count -gt 0)

# Test nvidia-smi
if (Get-Command "nvidia-smi" -ErrorAction SilentlyContinue) {
    try {
        $smiOut = & nvidia-smi 2>&1 | Out-String
        if ($LASTEXITCODE -eq 0) {
            $hasNvidia = $true
            if ($smiOut -match "CUDA Version:\s*(\d+\.\d+)") {
                $cudaVer = [version]$Matches[1]
            }
        }
    } catch {}
}

# Also try by name if nvidia-smi is not in PATH
if (-not $hasNvidia) {
    $hasNvidia = ($gpuList | Where-Object { $_ -match "NVIDIA|GeForce|Quadro|Tesla" }).Count -gt 0
}

$gpuChoice = "cpu"    # default

if ($hasNvidia) {
    $cudaStr = if ($cudaVer) { "CUDA $cudaVer" } else { "CUDA (version unknown)" }
    Write-Ok "NVIDIA GPU detected — $cudaStr available."
    Write-Host ""
    Write-Host "  GPU acceleration options for this machine:" -ForegroundColor White
    Write-Host "  [1] CUDA  — fastest NVIDIA acceleration (requires CUDA toolkit)"
    Write-Host "  [2] DirectML — uses GPU via DirectX 12 (no CUDA needed)"
    Write-Host "  [3] CPU only — no GPU, works everywhere"
    Write-Host ""
    $gpuAns = Read-Host "  Select GPU mode [1]"
    if (-not $gpuAns) { $gpuAns = "1" }
    switch ($gpuAns) {
        "1"     { $gpuChoice = "cuda" }
        "2"     { $gpuChoice = "dml" }
        default { $gpuChoice = "cpu" }
    }
} elseif ($hasAnyGPU) {
    Write-Ok "Non-NVIDIA GPU detected: $($gpuList -join ', ')"
    Write-Host ""
    Write-Host "  GPU acceleration options:" -ForegroundColor White
    Write-Host "  [1] DirectML — uses GPU via DirectX 12 (AMD / Intel supported)"
    Write-Host "  [2] CPU only — safe fallback"
    Write-Host ""
    $gpuAns = Read-Host "  Select GPU mode [1]"
    if (-not $gpuAns) { $gpuAns = "1" }
    $gpuChoice = if ($gpuAns -eq "1") { "dml" } else { "cpu" }
} else {
    Write-Ok "No discrete GPU detected — using CPU mode."
    $gpuChoice = "cpu"
}

switch ($gpuChoice) {
    "cuda" { $modeLabel = "NVIDIA CUDA" }
    "dml"  { $modeLabel = "DirectML (DirectX 12)" }
    "cpu"  { $modeLabel = "CPU only" }
}
Write-Ok "Acceleration mode: $modeLabel"

# ──────────────────────────────────────────────────────────────────────────────
# STEP 4 — Language / model selection
# ──────────────────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "  Select ASR language model:" -ForegroundColor White
Write-Host "  [1] 中文 (Chinese Mandarin) — vosk-model-small-cn-0.22  (~42 MB)  [default]"
Write-Host "  [2] English                 — vosk-model-small-en-us-0.15 (~40 MB)"
Write-Host "  [3] Both"
$langChoice = Read-Host "  Choice [1]"
if (-not $langChoice) { $langChoice = "1" }

$downloadCN = $langChoice -in @("1", "3")
$downloadEN = $langChoice -in @("2", "3")

# ──────────────────────────────────────────────────────────────────────────────
# STEP 5 — Python 3.11 64-bit detection / installation
# ──────────────────────────────────────────────────────────────────────────────
Write-Step "Checking Python installation"

$pythonExe = $null
foreach ($candidate in @("python", "python3", "py -3.11", "py -3", "py")) {
    try {
        $verOut = & $candidate.Split()[0] ($candidate.Split() | Select-Object -Skip 1) --version 2>&1
        # handle "py -3.11 --version" differently
        if ($candidate -like "py*") {
            $parts   = $candidate -split " "
            $verOut  = & $parts[0] $parts[1..($parts.Length-1)] --version 2>&1
        } else {
            $verOut = & $candidate --version 2>&1
        }
        if ($verOut -match "Python (\d+)\.(\d+)") {
            [int]$maj = $Matches[1]; [int]$min = $Matches[2]
            if ($maj -eq 3 -and $min -ge 8 -and $min -le 12) {
                # check 64-bit
                $exe = $candidate.Split()[0]
                $argRest = $candidate.Split() | Select-Object -Skip 1
                $bits = & $exe $argRest -c "import struct; print(struct.calcsize('P')*8)" 2>&1
                if ($bits -eq "64") {
                    $pythonExe = $candidate
                    Write-Ok "Found: Python $maj.$min (64-bit) — '$candidate'"
                    break
                } else {
                    Write-Warn "Found Python $maj.$min but 32-bit — skipping (vosk needs 64-bit)"
                }
            }
        }
    } catch { }
}

if (-not $pythonExe) {
    Write-Warn "Python 3.8–3.12 (64-bit) not found. Installing Python 3.11 via winget..."
    try {
        winget install --id Python.Python.3.11 --silent `
              --accept-package-agreements --accept-source-agreements 2>&1 | Out-Null
        # Refresh PATH
        $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" +
                    [System.Environment]::GetEnvironmentVariable("Path","User")
        $pythonExe = "python"
        Write-Ok "Python 3.11 installed."
    } catch {
        Write-Fail "winget failed: $_"
        Write-Fail "Please install Python 3.11 (64-bit) manually from:"
        Write-Fail "  https://www.python.org/downloads/release/python-3110/"
        Write-Fail "Make sure 'Add Python to PATH' is checked, then re-run install.bat."
        pause; exit 1
    }
}

# Resolve $pythonExe to a concrete executable path for venv
$pyParts = $pythonExe.Split()
$pyBin   = $pyParts[0]
$pyArgs  = if ($pyParts.Count -gt 1) { $pyParts[1..($pyParts.Count-1)] } else { @() }

# ──────────────────────────────────────────────────────────────────────────────
# STEP 6 — Virtual environment
# ──────────────────────────────────────────────────────────────────────────────
Write-Step "Setting up virtual environment (.venv)"

$venvDir = Join-Path $ScriptDir ".venv"
$pipExe  = Join-Path $venvDir "Scripts\pip.exe"
$pyExe   = Join-Path $venvDir "Scripts\python.exe"

if (-not (Test-Path $pyExe)) {
    & $pyBin $pyArgs -m venv $venvDir
    Write-Ok "Virtual environment created."
} else {
    Write-Ok "Virtual environment already exists."
}

# Upgrade pip silently
& $pipExe install --upgrade pip --quiet

# ──────────────────────────────────────────────────────────────────────────────
# STEP 7 — Install Python packages (GPU-aware)
# ──────────────────────────────────────────────────────────────────────────────
Write-Step "Installing Python packages — mode: $modeLabel"
Write-Info "(This may take several minutes on first run)"

# Strategy:
#   1. Install base packages + openwakeword (auto-installs onnxruntime CPU)
#   2. For GPU modes, uninstall onnxruntime and replace with GPU variant

$basePackages = @(
    "websockets>=12.0",
    "sounddevice>=0.4.6",
    "numpy>=1.24.0,<2.0.0",
    "vosk>=0.3.45",
    "openwakeword>=0.6.0"
)

Write-Info "Installing base packages..."
& $pipExe install $basePackages

if ($LASTEXITCODE -ne 0) {
    Write-Fail "Base package installation failed (exit code $LASTEXITCODE)."
    Write-Fail "Check your internet connection and retry."
    pause; exit 1
}

# Replace onnxruntime with GPU variant if needed
if ($gpuChoice -eq "cuda") {
    Write-Info "Switching to onnxruntime-gpu (CUDA) ..."

    # Determine compatible version based on CUDA runtime
    $ortGpuPkg = "onnxruntime-gpu>=1.17.0"    # default for CUDA 12+
    if ($cudaVer -and $cudaVer.Major -eq 11) {
        $ortGpuPkg = "onnxruntime-gpu==1.16.3" # last release supporting CUDA 11
        Write-Info "CUDA 11 detected — pinning onnxruntime-gpu to 1.16.3"
    }

    & $pipExe uninstall onnxruntime -y --quiet 2>&1 | Out-Null
    & $pipExe install $ortGpuPkg

    if ($LASTEXITCODE -ne 0) {
        Write-Warn "onnxruntime-gpu install failed — falling back to CPU onnxruntime."
        & $pipExe install "onnxruntime>=1.16.0" --quiet
        $gpuChoice = "cpu"; $modeLabel = "CPU only (fallback)"
    } else {
        Write-Ok "onnxruntime-gpu installed."
    }

} elseif ($gpuChoice -eq "dml") {
    Write-Info "Switching to onnxruntime-directml ..."
    & $pipExe uninstall onnxruntime -y --quiet 2>&1 | Out-Null
    & $pipExe install "onnxruntime-directml>=1.17.0"

    if ($LASTEXITCODE -ne 0) {
        Write-Warn "onnxruntime-directml install failed — falling back to CPU onnxruntime."
        & $pipExe install "onnxruntime>=1.16.0" --quiet
        $gpuChoice = "cpu"; $modeLabel = "CPU only (fallback)"
    } else {
        Write-Ok "onnxruntime-directml installed."
    }
}

Write-Ok "All packages installed."

# ──────────────────────────────────────────────────────────────────────────────
# STEP 8 — Download Vosk ASR model(s)
# ──────────────────────────────────────────────────────────────────────────────
Write-Step "Downloading Vosk ASR model(s)"

$modelDir = Join-Path $ScriptDir "models"
New-Item -ItemType Directory -Force -Path $modelDir | Out-Null

function Invoke-ModelDownload {
    param([string]$ModelName, [string]$Url)
    $dest    = Join-Path $modelDir $ModelName
    $zipPath = Join-Path $modelDir "$ModelName.zip"

    if (Test-Path $dest) {
        Write-Ok "Already downloaded: $ModelName"
        return $true
    }

    Write-Info "Downloading $ModelName ..."
    try {
        # Try curl first (available Win10 1803+) for progress display
        if (Get-Command "curl.exe" -ErrorAction SilentlyContinue) {
            & curl.exe -L --progress-bar -o $zipPath $Url
        } else {
            Invoke-WebRequest -Uri $Url -OutFile $zipPath -UseBasicParsing
        }
        Write-Info "Extracting $ModelName ..."
        Expand-Archive -Path $zipPath -DestinationPath $modelDir -Force
        Remove-Item $zipPath -Force
        Write-Ok "$ModelName ready."
        return $true
    } catch {
        Write-Fail "Download failed: $_"
        if (Test-Path $zipPath) { Remove-Item $zipPath -Force }
        return $false
    }
}

$cnModelName = "vosk-model-small-cn-0.22"
$enModelName = "vosk-model-small-en-us-0.15"

if ($downloadCN) {
    Invoke-ModelDownload `
        -ModelName $cnModelName `
        -Url       "https://alphacephei.com/vosk/models/vosk-model-small-cn-0.22.zip"
}
if ($downloadEN) {
    Invoke-ModelDownload `
        -ModelName $enModelName `
        -Url       "https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip"
}

# ── Update config.json model path ─────────────────────────────────────────────
$cfgPath = Join-Path $ScriptDir "config.json"
try {
    $cfg = Get-Content $cfgPath -Raw -Encoding UTF8 | ConvertFrom-Json

    # Choose model path
    if ($downloadCN) {
        $cfg.asr.model_path = "models/$cnModelName"
    } elseif ($downloadEN) {
        $cfg.asr.model_path = "models/$enModelName"
    }

    # If English-only, switch wake word defaults to English
    if ($downloadEN -and -not $downloadCN) {
        $cfg.wake_word.mode     = "openwakeword"
        $cfg.wake_word.keywords = @("hey_jarvis")
        Write-Info "config.json: set wake_word mode=openwakeword for English model."
    }

    $cfg | ConvertTo-Json -Depth 10 | Set-Content $cfgPath -Encoding UTF8
    Write-Ok "config.json updated (model_path = models/$($cfg.asr.model_path | Split-Path -Leaf))."
} catch {
    Write-Warn "Could not update config.json automatically: $_"
    Write-Warn "Please manually set asr.model_path in config.json."
}

# ──────────────────────────────────────────────────────────────────────────────
# STEP 9 — Pre-download openwakeword models
# ──────────────────────────────────────────────────────────────────────────────
Write-Step "Pre-downloading openwakeword models"

$owwPreload = @"
import sys, warnings, os
warnings.filterwarnings('ignore')
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'
try:
    import openwakeword
    openwakeword.utils.download_models()
    print('[OK] openwakeword models ready.')
except Exception as e:
    print(f'[WARN] {e}', file=sys.stderr)
    print('[INFO] Models will be downloaded on first run.')
"@

& $pyExe -c $owwPreload

# ──────────────────────────────────────────────────────────────────────────────
# STEP 10 — Smoke test
# ──────────────────────────────────────────────────────────────────────────────
Write-Step "Running smoke test"

$smokeTest = @"
import sys, os
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'
errors = []

def check(name, fn):
    try:
        fn()
        print(f'  [OK] {name}')
    except Exception as e:
        errors.append(f'{name}: {e}')
        print(f'  [!!] {name}: {e}')

check('websockets',   lambda: __import__('websockets'))
check('sounddevice',  lambda: __import__('sounddevice'))
check('numpy',        lambda: __import__('numpy'))
check('vosk',         lambda: __import__('vosk'))
check('openwakeword', lambda: __import__('openwakeword'))

# Check onnxruntime variant
try:
    import onnxruntime as ort
    providers = ort.get_available_providers()
    gpu_providers = [p for p in providers if p != 'CPUExecutionProvider']
    if gpu_providers:
        print(f'  [OK] onnxruntime GPU providers: {gpu_providers}')
    else:
        print(f'  [OK] onnxruntime (CPU only)')
except Exception as e:
    errors.append(f'onnxruntime: {e}')
    print(f'  [!!] onnxruntime: {e}')

# Check model file
import json, pathlib
cfg_path = pathlib.Path('config.json')
if cfg_path.exists():
    cfg = json.loads(cfg_path.read_text(encoding='utf-8'))
    model_path = cfg.get('asr', {}).get('model_path', '')
    if model_path and pathlib.Path(model_path).exists():
        print(f'  [OK] ASR model found: {model_path}')
    else:
        errors.append(f'ASR model not found at: {model_path}')
        print(f'  [!!] ASR model not found at: {model_path}')

sys.exit(1 if errors else 0)
"@

& $pyExe -c $smokeTest
$smokeOk = ($LASTEXITCODE -eq 0)

# ──────────────────────────────────────────────────────────────────────────────
# Done
# ──────────────────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host $border -ForegroundColor $(if ($smokeOk) { "Green" } else { "Yellow" })
if ($smokeOk) {
    Write-Host "  Installation complete!" -ForegroundColor Green
} else {
    Write-Host "  Installation finished with warnings." -ForegroundColor Yellow
    Write-Host "  Review the [!!] messages above before starting." -ForegroundColor Yellow
}
Write-Host $border -ForegroundColor $(if ($smokeOk) { "Green" } else { "Yellow" })
Write-Host ""
Write-Host "  Acceleration : $modeLabel"
Write-Host "  ASR model    : $($cfg.asr.model_path)"
Write-Host "  Wake mode    : $($cfg.wake_word.mode)  ($($cfg.wake_word.keywords -join ', '))"
Write-Host ""
Write-Host "  ► Start service   : double-click start.bat"
Write-Host "  ► List microphones: .venv\Scripts\python.exe list_devices.py"
Write-Host "  ► Edit settings   : config.json"
Write-Host "  ► Open UI         : open index.html in a browser after starting"
Write-Host ""
