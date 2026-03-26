#Requires -Version 5.1
<#
.SYNOPSIS
    离线部署包构建脚本
    Builds a fully self-contained portable folder — copy it to any Windows
    machine and run start_whisper.bat without any prior installation.

.DESCRIPTION
    输出文件夹包含:
      python\      Python 3.11 嵌入式运行时 + 所有 pip 依赖
      models\      Vosk 中文模型 + Whisper 模型 HuggingFace 本地缓存
      *.py         应用程序源码
      config.json  配置文件（已自动调整路径）
      start_whisper.bat / start_vosk.bat / check_env.bat

    目标机器系统要求:
      - Windows 10 Build 1809+ / Windows 11 (x64)
      - Visual C++ 2015-2022 Redistributable (x64)
        如未安装: https://aka.ms/vs/17/release/vc_redist.x64.exe

.PARAMETER WhisperModel
    Whisper 模型大小 (默认: small)
    可选: tiny | base | small | medium | large-v3

.PARAMETER GPU
    GPU 加速模式 (默认: none)
    none  — 纯 CPU（兼容所有机器）
    cuda  — NVIDIA CUDA（需目标机器有 CUDA 12）
    dml   — DirectML / DirectX 12（AMD / Intel / NVIDIA，无需 CUDA）

.PARAMETER IncludeVosk
    是否下载并打包 Vosk 中文模型（默认: $true）

.PARAMETER OutputDir
    输出目录（默认: .\dist\SpeechReco-Offline）

.EXAMPLE
    .\build_offline.ps1
    .\build_offline.ps1 -WhisperModel base -GPU none
    .\build_offline.ps1 -WhisperModel small -GPU cuda -OutputDir D:\deploy
#>

param(
    [string] $WhisperModel = "small",
    [ValidateSet("none","cuda","dml")]
    [string] $GPU          = "none",
    [bool]   $IncludeVosk  = $true,
    [string] $OutputDir    = ""
)

$ErrorActionPreference = "Stop"
$ProgressPreference    = "SilentlyContinue"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$null = & chcp 65001 2>&1

function Write-Step { param($n, $msg) Write-Host "`n[$n] $msg" -ForegroundColor Cyan }
function Write-Ok   { param($msg) Write-Host "  [OK] $msg"   -ForegroundColor Green }
function Write-Warn { param($msg) Write-Host " [!!] $msg"    -ForegroundColor Yellow }
function Write-Fail { param($msg) Write-Host " [XX] $msg"    -ForegroundColor Red; exit 1 }
function Write-Info { param($msg) Write-Host "      $msg" }

# ── Paths & versions ──────────────────────────────────────────────────────────
$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $OutputDir) {
    $OutputDir = Join-Path $ScriptDir "dist\SpeechReco-Offline"
}

$PY_VER     = "3.11.9"
$PY_ZIP     = "python-$PY_VER-embed-amd64.zip"
$PY_URL     = "https://www.python.org/ftp/python/$PY_VER/$PY_ZIP"
$GETPIP_URL = "https://bootstrap.pypa.io/get-pip.py"
$HF_MIRROR  = "https://hf-mirror.com"    # faster in China

$CN_MODEL   = "vosk-model-small-cn-0.22"
$CN_URL     = "https://alphacephei.com/vosk/models/$CN_MODEL.zip"

$PythonDir  = Join-Path $OutputDir "python"
$ModelsDir  = Join-Path $OutputDir "models"
$HFCacheDir = Join-Path $ModelsDir "hf"
$PyExe      = Join-Path $PythonDir "python.exe"

$border = "=" * 66
Write-Host ""
Write-Host $border -ForegroundColor Cyan
Write-Host "  离线部署包构建器  /  Offline Deployment Builder" -ForegroundColor Cyan
Write-Host $border -ForegroundColor Cyan
Write-Host "  输出目录      : $OutputDir"
Write-Host "  Whisper 模型  : $WhisperModel"
Write-Host "  GPU 模式      : $GPU"
Write-Host "  包含 Vosk     : $IncludeVosk"
Write-Host $border -ForegroundColor Cyan

# ── STEP 1: Output directory ───────────────────────────────────────────────────
Write-Step 1 "准备输出目录"
if (Test-Path $OutputDir) {
    $ans = Read-Host "  '$OutputDir' 已存在，是否覆盖重建? [y/N]"
    if ($ans -notmatch "^[yY]") { Write-Host "已取消。"; exit 0 }
    Remove-Item $OutputDir -Recurse -Force
}
foreach ($d in @($OutputDir, $PythonDir, $ModelsDir, $HFCacheDir)) {
    New-Item -ItemType Directory -Force -Path $d | Out-Null
}
Write-Ok "目录创建完成"

# ── STEP 2: Python 3.11 embeddable ────────────────────────────────────────────
Write-Step 2 "下载 Python $PY_VER 嵌入式运行环境"
$zipTemp = Join-Path $env:TEMP $PY_ZIP
if (-not (Test-Path $zipTemp)) {
    Write-Info "下载 $PY_URL ..."
    try {
        if (Get-Command curl.exe -ErrorAction SilentlyContinue) {
            & curl.exe -L --progress-bar -o $zipTemp $PY_URL
        } else {
            Invoke-WebRequest -Uri $PY_URL -OutFile $zipTemp -UseBasicParsing
        }
    } catch { Write-Fail "Python 下载失败：$_" }
} else {
    Write-Info "使用本地缓存：$zipTemp"
}
Write-Info "解压..."
Expand-Archive -Path $zipTemp -DestinationPath $PythonDir -Force

# Enable Lib\site-packages and make parent dir (app root) importable
$pthFile = Join-Path $PythonDir "python311._pth"
$pth = Get-Content $pthFile -Raw
# Remove the commented-out "import site" line and add our config
$pth = $pth -replace "#\s*import site", "import site"
Add-Content $pthFile "`nLib\site-packages`n.."
Write-Ok "Python $PY_VER 嵌入式运行时就绪"

# ── STEP 3: pip ────────────────────────────────────────────────────────────────
Write-Step 3 "安装 pip"
$getPipTmp = Join-Path $PythonDir "get-pip.py"
try {
    if (Get-Command curl.exe -ErrorAction SilentlyContinue) {
        & curl.exe -L --silent -o $getPipTmp $GETPIP_URL
    } else {
        Invoke-WebRequest -Uri $GETPIP_URL -OutFile $getPipTmp -UseBasicParsing
    }
} catch { Write-Fail "get-pip.py 下载失败：$_" }

& $PyExe $getPipTmp --quiet
if ($LASTEXITCODE -ne 0) { Write-Fail "pip 安装失败" }
Remove-Item $getPipTmp -Force -ErrorAction SilentlyContinue
Write-Ok "pip 安装成功"

# ── STEP 4: Python packages ────────────────────────────────────────────────────
Write-Step 4 "安装 Python 依赖包（首次约需 5–15 分钟）"

$pkgs = @(
    "websockets>=12.0",
    "sounddevice>=0.4.6",
    "numpy>=1.24.0,<2.0.0",
    "faster-whisper>=1.0.0",
    "ctranslate2>=4.0.0",
    "vosk>=0.3.45",
    "openwakeword>=0.6.0"
)

foreach ($pkg in $pkgs) {
    Write-Info "  pip install $pkg"
    & $PyExe -m pip install $pkg --quiet
    if ($LASTEXITCODE -ne 0) {
        Write-Warn "安装 $pkg 时出错（继续）"
    }
}

# GPU variant of onnxruntime
switch ($GPU) {
    "cuda" {
        Write-Info "  安装 CUDA 支持（nvidia-cudnn-cu12）..."
        & $PyExe -m pip install "nvidia-cudnn-cu12>=8.9" --quiet
        & $PyExe -m pip uninstall onnxruntime -y --quiet 2>&1 | Out-Null
        & $PyExe -m pip install "onnxruntime-gpu>=1.17.0" --quiet
        if ($LASTEXITCODE -ne 0) {
            Write-Warn "onnxruntime-gpu 安装失败，保留 CPU 版本"
            & $PyExe -m pip install "onnxruntime>=1.16.0" --quiet
        }
    }
    "dml" {
        Write-Info "  安装 DirectML 支持..."
        & $PyExe -m pip uninstall onnxruntime -y --quiet 2>&1 | Out-Null
        & $PyExe -m pip install "onnxruntime-directml>=1.17.0" --quiet
        if ($LASTEXITCODE -ne 0) {
            Write-Warn "onnxruntime-directml 安装失败，保留 CPU 版本"
            & $PyExe -m pip install "onnxruntime>=1.16.0" --quiet
        }
    }
}
Write-Ok "Python 依赖包安装完成"

# ── STEP 5: Whisper model ──────────────────────────────────────────────────────
Write-Step 5 "下载 Whisper 模型（$WhisperModel）到本地缓存"
Write-Info "缓存目录: $HFCacheDir"

$env:HF_HOME     = $HFCacheDir
$env:HF_ENDPOINT = $HF_MIRROR

$dlScript = @"
import os, sys
os.environ['HF_HOME']     = r'$($HFCacheDir -replace "\\","\\")'
os.environ['HF_ENDPOINT'] = '$HF_MIRROR'
sys.stdout.reconfigure(encoding='utf-8')
try:
    from faster_whisper import WhisperModel
    print('  正在下载/验证 Whisper $WhisperModel 模型 ...')
    m = WhisperModel('$WhisperModel', device='cpu', compute_type='int8')
    print('  Whisper 模型下载完成。')
    del m
except Exception as e:
    print(f'  [警告] {e}', file=sys.stderr)
    print('  模型将在首次启动服务时自动下载。')
"@

& $PyExe -c $dlScript
Write-Ok "Whisper 模型准备完成"

# ── STEP 6: Vosk model ─────────────────────────────────────────────────────────
if ($IncludeVosk) {
    Write-Step 6 "下载 Vosk 中文模型（$CN_MODEL）"
    $voskDest = Join-Path $ModelsDir $CN_MODEL
    $voskZip  = Join-Path $ModelsDir "$CN_MODEL.zip"

    if (Test-Path $voskDest) {
        Write-Ok "模型已存在：$CN_MODEL"
    } else {
        Write-Info "下载 $CN_URL ..."
        $ok = $true
        try {
            if (Get-Command curl.exe -ErrorAction SilentlyContinue) {
                & curl.exe -L --progress-bar -o $voskZip $CN_URL
                $ok = $LASTEXITCODE -eq 0
            } else {
                Invoke-WebRequest -Uri $CN_URL -OutFile $voskZip -UseBasicParsing
            }
        } catch { $ok = $false; Write-Warn "下载失败：$_" }

        if ($ok -and (Test-Path $voskZip)) {
            Write-Info "解压..."
            Expand-Archive -Path $voskZip -DestinationPath $ModelsDir -Force
            Remove-Item $voskZip -Force
            Write-Ok "Vosk 中文模型就绪"
        } else {
            Write-Warn "Vosk 模型下载失败。可手动下载后放入 models\ 目录。"
        }
    }

    # Pre-fetch openwakeword built-in models
    Write-Info "预下载 openwakeword 内置模型..."
    & $PyExe -c @"
import warnings; warnings.filterwarnings('ignore')
try:
    import openwakeword; openwakeword.utils.download_models()
    print('  openwakeword 模型已就绪。')
except Exception as e:
    print(f'  [跳过] {e}')
"@ 2>$null
} else {
    Write-Step 6 "跳过 Vosk 模型（IncludeVosk=False）"
}

# ── STEP 7: Application files ──────────────────────────────────────────────────
Write-Step 7 "复制应用程序源文件"

$appFiles = @(
    "server.py", "engine.py",
    "server_whisper.py", "engine_whisper.py",
    "index.html", "config.json",
    "list_devices.py", "check_env.py",
    "verify_install.py"
)
foreach ($f in $appFiles) {
    $src = Join-Path $ScriptDir $f
    if (Test-Path $src) {
        Copy-Item $src (Join-Path $OutputDir $f) -Force
        Write-Info "  $f"
    } else {
        Write-Warn "  文件不存在（跳过）：$f"
    }
}

# Patch config.json for the portable layout
$cfgOut = Join-Path $OutputDir "config.json"
try {
    $cfg = Get-Content $cfgOut -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($IncludeVosk) {
        $cfg.asr.model_path = "models/$CN_MODEL"
    }
    $cfg.whisper.model  = $WhisperModel
    # Persist: HF_HOME will be set in start_whisper.bat
    $cfg | ConvertTo-Json -Depth 10 | Set-Content $cfgOut -Encoding UTF8
    Write-Ok "config.json 路径已更新"
} catch {
    Write-Warn "config.json 自动更新失败：$_"
}

# ── STEP 8: Launch scripts ─────────────────────────────────────────────────────
Write-Step 8 "生成启动脚本"

# start_whisper.bat
Set-Content (Join-Path $OutputDir "start_whisper.bat") @"
@echo off
chcp 65001 >nul
setlocal
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
:: 指向本地 HuggingFace 模型缓存（离线模式）
set HF_HOME=%~dp0models\hf
set HF_ENDPOINT=https://hf-mirror.com
cd /d "%~dp0"
title 语音识别服务 (Whisper) - ws://127.0.0.1:8766
echo.
echo  ================================================
echo    语音识别服务 (Whisper)   端口 8766
echo    Whisper 模型: $WhisperModel
echo    Press Ctrl+C 停止服务
echo  ================================================
echo.
python\python.exe server_whisper.py
echo.
echo 服务已停止。
pause
"@ -Encoding UTF8

# start_vosk.bat
Set-Content (Join-Path $OutputDir "start_vosk.bat") @"
@echo off
chcp 65001 >nul
setlocal
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
cd /d "%~dp0"
title 语音识别服务 (Vosk) - ws://127.0.0.1:8765
echo.
echo  ================================================
echo    语音识别服务 (Vosk)   端口 8765
echo    Press Ctrl+C 停止服务
echo  ================================================
echo.
python\python.exe server.py
echo.
echo 服务已停止。
pause
"@ -Encoding UTF8

# check_env.bat
Set-Content (Join-Path $OutputDir "check_env.bat") @"
@echo off
chcp 65001 >nul
setlocal
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
set HF_HOME=%~dp0models\hf
cd /d "%~dp0"
title 环境检测
python\python.exe check_env.py
pause
"@ -Encoding UTF8

Write-Ok "start_whisper.bat / start_vosk.bat / check_env.bat 已生成"

# ── STEP 9: README ─────────────────────────────────────────────────────────────
$buildTime = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
Set-Content (Join-Path $OutputDir "README.txt") @"
语音识别服务 — 离线部署包
Speech Recognition Service — Offline Portable Package
======================================================

快速使用 / Quick Start:
  1. 将本文件夹整体复制到目标机器
  2. 双击 check_env.bat  — 检查依赖环境
  3. 双击 start_whisper.bat — 启动 Whisper 服务（推荐）
     或  start_vosk.bat     — 启动 Vosk 轻量服务
  4. 用浏览器打开 index.html，连接 ws://127.0.0.1:8766

WebSocket 地址:
  Whisper 服务: ws://127.0.0.1:8766
  Vosk    服务: ws://127.0.0.1:8765

系统要求:
  - Windows 10 (Build 1809+) 或 Windows 11，64-bit
  - Visual C++ 2015-2022 Redistributable (x64)
    下载: https://aka.ms/vs/17/release/vc_redist.x64.exe
  - 麦克风设备

目录说明:
  python\          Python $PY_VER 嵌入式运行时 + 所有依赖包
  models\hf\       Whisper $WhisperModel 模型本地缓存
  models\$CN_MODEL\  Vosk 中文语音模型
  config.json      配置文件（可编辑）
  index.html       Web 控制台

构建信息:
  Whisper 模型 : $WhisperModel
  GPU 模式     : $GPU
  构建时间     : $buildTime
"@ -Encoding UTF8

# ── Summary ────────────────────────────────────────────────────────────────────
$totalMB = [int]((Get-ChildItem $OutputDir -Recurse -File |
                  Measure-Object -Property Length -Sum).Sum / 1MB)

Write-Host ""
Write-Host $border -ForegroundColor Green
Write-Host "  构建完成！  Build successful!" -ForegroundColor Green
Write-Host $border -ForegroundColor Green
Write-Host ""
Write-Host "  输出目录 : $OutputDir"
Write-Host "  总大小   : ${totalMB} MB"
Write-Host ""
Write-Host "  部署步骤:"
Write-Host "    1. 将 '$OutputDir' 整个文件夹复制到目标机器"
Write-Host "    2. 运行 check_env.bat 验证环境"
Write-Host "    3. 运行 start_whisper.bat 启动服务"
Write-Host ""
