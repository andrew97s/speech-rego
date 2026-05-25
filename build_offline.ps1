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

.PARAMETER IncludeOWW
    是否安装 openwakeword 唤醒词包（默认: $false）
    关闭可节省约 150 MB（省去 scipy + onnxruntime 等依赖）
    默认唤醒词模式为 vosk/whisper，无需 openwakeword

.PARAMETER BundleNvidiaCuda
    是否将 nvidia-cublas / nvidia-cudnn 等 CUDA 运行库打包进部署包（默认: $false）
    关闭可节省约 800 MB；目标机器需自行安装 NVIDIA CUDA Toolkit 12
    启用可完全离线使用 CUDA：-BundleNvidiaCuda $true

.PARAMETER OutputDir
    输出目录（默认: .\dist\SpeechReco-Offline）

.EXAMPLE
    .\build_offline.ps1 -WhisperModel tiny -GPU none -IncludeVosk $false
        # 约最小体积：CPU + tiny 模型 + 无 Vosk + 自动裁剪 onnxruntime 等
    .\build_offline.ps1 -WhisperModel small -GPU cuda -BundleNvidiaCuda $true
        # 目标机 NVIDIA + 完整 CUDA 离线（体积最大）
    .\build_offline.ps1 -PruneUnusedDeps $false
        # 保留 pip 拉取的全部依赖（调试用）
    .\build_offline.ps1 -WhisperModel small -GPU cuda -OutputDir D:\deploy
#>

param(
    [string] $WhisperModel    = "small",
    [ValidateSet("none","cuda","dml","auto")]
    [string] $GPU             = "auto",
    [bool]   $IncludeVosk     = $true,
    [bool]   $IncludeOWW      = $false,   # openwakeword 唤醒词（默认关闭；默认用 vosk/whisper 唤醒词，无需额外依赖）
    [bool]   $BundleNvidiaCuda = $false, # 是否打包 nvidia-* CUDA 运行库（约 800MB）；false=目标机需自行安装 CUDA Toolkit
    [bool]   $PruneUnusedDeps  = $true,  # 构建后卸载 onnxruntime / hf_xet / HF CLI 等运行时不需要的包
    [string] $OutputDir       = ""
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

# cmd.exe cannot run .bat files saved as UTF-8 with BOM; use ASCII + CRLF only.
function Write-CmdBat {
    param([string]$Path, [string]$Content)
    $text = ($Content.TrimEnd() -replace "`r?`n", "`r`n") + "`r`n"
    $enc  = New-Object System.Text.ASCIIEncoding
    [System.IO.File]::WriteAllText($Path, $text, $enc)
}

# webrtcvad has no win_amd64 wheel on PyPI; embed Python lacks Include/Python.h.
# Build with host Python 3.11 headers + python311.lib, or copy a prebuilt extension.
function Install-EmbeddedWebRtcVad {
    param(
        [string] $PyExe,
        [string] $PythonDir,
        [string] $SiteDir
    )

    $hostVer = $null
    try {
        $hostVer = & py -3.11 -c "import sys; print('%d.%d.%d' % sys.version_info[:3])" 2>$null
    } catch { }

    if (-not $hostVer -or -not ($hostVer -match '^3\.11\.')) {
        Write-Fail @"
webrtcvad 需要在本机构建或复制扩展，但找不到 Python 3.11。
请安装 https://www.python.org/downloads/windows/ （勾选 py launcher），然后重试 build_offline。
"@
    }

    $hostPrefix = (& py -3.11 -c "import sys; print(sys.base_prefix)" 2>$null).Trim()
    $hostInclude = Join-Path $hostPrefix "Include"
    $hostLibs    = Join-Path $hostPrefix "libs"
    $pyLib       = Join-Path $hostLibs "python311.lib"

    if (-not (Test-Path (Join-Path $hostInclude "Python.h"))) {
        Write-Fail "本机 Python 3.11 缺少 Include\Python.h，请重装完整版 Python（非 embed 包）。"
    }

    $embedLibs = Join-Path $PythonDir "libs"
    if (-not (Test-Path $embedLibs)) {
        New-Item -ItemType Directory -Force -Path $embedLibs | Out-Null
    }
    if ((Test-Path $pyLib) -and -not (Test-Path (Join-Path $embedLibs "python311.lib"))) {
        Copy-Item $pyLib (Join-Path $embedLibs "python311.lib") -Force
        Write-Info "  已复制 python311.lib 到嵌入式 Python（供编译 C 扩展）"
    }

    Write-Info "  尝试在嵌入式 Python 中编译 webrtcvad（使用本机 3.11 头文件）..."
    $prevInclude = $env:INCLUDE
    $env:INCLUDE = "$hostInclude;$prevInclude"
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    & $PyExe -m pip install 'webrtcvad>=2.0.10' --no-cache-dir 2>&1 | Out-Null
    $ErrorActionPreference = $prevEap
    $env:INCLUDE = $prevInclude

    $pyd = Get-ChildItem $SiteDir -Filter "_webrtcvad*.pyd" -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($LASTEXITCODE -eq 0 -and $pyd) {
        return
    }

    Write-Warn "  嵌入式 pip 编译失败，改从本机 Python 3.11 复制 webrtcvad 扩展..."
    & py -3.11 -m pip install 'webrtcvad>=2.0.10' -q 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Fail "本机 pip 安装 webrtcvad 失败。请确认已安装 Visual C++ Build Tools 后重试。"
    }

    $hostSite = (& py -3.11 -c "import webrtcvad, os; print(os.path.dirname(webrtcvad.__file__))" 2>$null).Trim()
    if (-not $hostSite -or -not (Test-Path $hostSite)) {
        Write-Fail "无法定位本机 webrtcvad 包路径。"
    }

    foreach ($name in @("webrtcvad.py")) {
        $src = Join-Path $hostSite $name
        if (Test-Path $src) { Copy-Item $src $SiteDir -Force }
    }
    Get-ChildItem $hostSite -Filter "_webrtcvad*.pyd" -ErrorAction SilentlyContinue | ForEach-Object {
        Copy-Item $_.FullName $SiteDir -Force
    }
    $hostSiteParent = Split-Path $hostSite -Parent
    Get-ChildItem $hostSiteParent -Filter "webrtcvad-*.dist-info" -Directory -ErrorAction SilentlyContinue |
        ForEach-Object {
            $dest = Join-Path $SiteDir $_.Name
            if (Test-Path $dest) { Remove-Item $dest -Recurse -Force }
            Copy-Item $_.FullName $SiteDir -Recurse -Force
        }

    $pyd = Get-ChildItem $SiteDir -Filter "_webrtcvad*.pyd" -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $pyd) {
        Write-Fail "复制 webrtcvad 后仍找不到 _webrtcvad*.pyd。"
    }
    Write-Info "  已从本机 Python 复制 $($pyd.Name)"
}

# ── GPU auto-detection ────────────────────────────────────────────────────────
# Runs before directory creation so detection result is shown in the banner.
if ($GPU -eq "auto") {
    Write-Host "`n[GPU] Auto-detecting GPU ..." -ForegroundColor Cyan
    $detected = "none"

    # Query all GPU adapters via WMI
    $gpuList = @(Get-WmiObject -Class Win32_VideoController -ErrorAction SilentlyContinue |
                 Where-Object { $_.Name -ne "" } |
                 Select-Object Name, DriverVersion)

    foreach ($g in $gpuList) {
        Write-Info "  Found: $($g.Name)  driver $($g.DriverVersion)"
    }

    # Check for NVIDIA with CUDA 12 capable driver (Windows driver 527.41+ = WDDM 3.1.x)
    $nvidiaGpu = $gpuList | Where-Object {
        $_.Name -match "NVIDIA|GeForce|Quadro|Tesla"
    } | Select-Object -First 1

    if ($nvidiaGpu) {
        $drvParts = ($nvidiaGpu.DriverVersion -split "\.")
        # Windows WDDM driver: last two segments encode the real driver version
        # e.g. "31.0.15.5154" -> 15*100 + (5154/100) ~ 527 -> CUDA 12 ok
        try {
            $seg3 = [int]$drvParts[2]   # e.g. 15
            $seg4 = [int]$drvParts[3]   # e.g. 5154
            # Approximate NVIDIA driver version: seg3 * 100 + floor(seg4 / 100)
            $approxDrv = $seg3 * 100 + [math]::Floor($seg4 / 100)
            Write-Info "  NVIDIA driver ~$approxDrv"
            if ($approxDrv -ge 527) {
                $detected = "cuda"
                Write-Ok "  NVIDIA driver >= 527 -> CUDA 12 supported -> selecting cuda mode"
            } else {
                $detected = "dml"
                Write-Warn "  NVIDIA driver < 527 (CUDA 12 needs 527+) -> falling back to dml"
            }
        } catch {
            $detected = "dml"
            Write-Warn "  Could not parse driver version -> falling back to dml"
        }
    } elseif ($gpuList.Count -gt 0) {
        # Non-NVIDIA GPU (AMD / Intel) — use DirectML
        $detected = "dml"
        Write-Ok "  Non-NVIDIA GPU detected -> selecting dml mode"
    } else {
        Write-Info "  No discrete GPU found -> cpu mode"
    }

    $GPU = $detected
}

# cuda 离线包默认把 nvidia-cublas 等打进包内；仅当显式传入 -BundleNvidiaCuda $false 时不打包
if ($GPU -eq "cuda" -and -not $PSBoundParameters.ContainsKey("BundleNvidiaCuda")) {
    $BundleNvidiaCuda = $true
}

$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $OutputDir) {
    $OutputDir = Join-Path $ScriptDir "dist\SpeechReco-Offline"
}

# Persistent model cache — lives next to the script, NEVER deleted on rebuild.
# Re-running the build reuses cached models (no re-download).
$CacheDir      = Join-Path $ScriptDir ".offline-cache"
$WModelCacheHF = Join-Path $CacheDir "hf"         # HuggingFace / Whisper cache
$VoskCacheDir  = Join-Path $CacheDir "vosk"        # Vosk model cache
$OWWCacheDir   = Join-Path $CacheDir "oww-models"  # openwakeword 模型缓存
$PipCacheDir   = Join-Path $CacheDir "pip-cache"   # pip wheel 缓存（自动被 pip 使用）

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
Write-Host "  模型缓存      : $CacheDir"
Write-Host "  Whisper 模型  : $WhisperModel"
Write-Host "  GPU 模式      : $GPU"
Write-Host "  包含 Vosk     : $IncludeVosk"
Write-Host "  包含 OWW      : $IncludeOWW"
Write-Host "  打包 NVIDIA   : $BundleNvidiaCuda"
Write-Host $border -ForegroundColor Cyan

# ── STEP 1: Output directory ───────────────────────────────────────────────────
Write-Step 1 "准备输出目录"

# Ensure persistent cache dirs exist (never removed)
foreach ($d in @($CacheDir, $WModelCacheHF, $VoskCacheDir, $OWWCacheDir, $PipCacheDir)) {
    New-Item -ItemType Directory -Force -Path $d | Out-Null
}
# pip respects PIP_CACHE_DIR automatically — all pip install calls use the cache
$env:PIP_CACHE_DIR   = $PipCacheDir
# 国内 PyPI 镜像，大幅提升下载速度
$env:PIP_INDEX_URL   = "https://pypi.tuna.tsinghua.edu.cn/simple"
$env:PIP_TRUSTED_HOST = "pypi.tuna.tsinghua.edu.cn"

if (Test-Path $OutputDir) {
    $ans = Read-Host "  '$OutputDir' 已存在，是否覆盖重建? [y/N]"
    if ($ans -notmatch "^[yY]") { Write-Host "已取消。"; exit 0 }
    Remove-Item $OutputDir -Recurse -Force
}
foreach ($d in @($OutputDir, $PythonDir, $ModelsDir, $HFCacheDir)) {
    New-Item -ItemType Directory -Force -Path $d | Out-Null
}
Write-Ok "目录创建完成（模型缓存: $CacheDir）"

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
# Cache get-pip.py in TEMP so rebuild doesn't re-download it
$getPipCache = Join-Path $env:TEMP "get-pip.py"
if (-not (Test-Path $getPipCache)) {
    Write-Info "下载 get-pip.py ($GETPIP_URL) ..."
    try {
        if (Get-Command curl.exe -ErrorAction SilentlyContinue) {
            & curl.exe -L --progress-bar -o $getPipCache $GETPIP_URL
        } else {
            Invoke-WebRequest -Uri $GETPIP_URL -OutFile $getPipCache -UseBasicParsing
        }
    } catch { Write-Fail "get-pip.py 下载失败：$_" }
} else {
    Write-Info "使用本地缓存：$getPipCache"
}
Copy-Item $getPipCache (Join-Path $PythonDir "get-pip.py") -Force

Write-Info "运行 get-pip.py ..."
& $PyExe (Join-Path $PythonDir "get-pip.py")
if ($LASTEXITCODE -ne 0) { Write-Fail "pip 安装失败" }
Remove-Item (Join-Path $PythonDir "get-pip.py") -Force -ErrorAction SilentlyContinue
Write-Ok "pip 安装成功"

# ── STEP 4: Python packages ────────────────────────────────────────────────────
Write-Step 4 "安装 Python 依赖包（首次约需 5–15 分钟）"

$pkgs = @(
    'websockets>=12.0',
    'sounddevice>=0.4.6',
    'numpy>=1.24.0,<2.0.0',
    'faster-whisper>=1.0.0',
    'ctranslate2>=4.0.0',
    'vosk>=0.3.45',
    'zhconv>=1.4.3'
)

foreach ($pkg in $pkgs) {
    Write-Info "  pip install $pkg"
    & $PyExe -m pip install $pkg --prefer-binary
    if ($LASTEXITCODE -ne 0) {
        Write-Warn "安装 $pkg 时出错（继续）"
    }
}

# webrtcvad：PyPI 无 Windows wheel；见 Install-EmbeddedWebRtcVad（约 1MB）
Write-Info "  install webrtcvad (speech end detection)"
$siteDirEarly = Join-Path $PythonDir "Lib\site-packages"
Install-EmbeddedWebRtcVad -PyExe $PyExe -PythonDir $PythonDir -SiteDir $siteDirEarly
$wvCheck = Join-Path $env:TEMP "wv_check_$PID.py"
@'
import sys
sys.path.insert(0, r'__SCRIPT_DIR__')
from speech_vad import create_webrtc_vad
assert create_webrtc_vad(2) is not None, "webrtcvad import failed"
print("webrtcvad ok")
'@.Replace('__SCRIPT_DIR__', ($ScriptDir -replace '\\', '/')) |
    Set-Content $wvCheck -Encoding ASCII
& $PyExe $wvCheck
if ($LASTEXITCODE -ne 0) {
    Remove-Item $wvCheck -Force -ErrorAction SilentlyContinue
    Write-Fail "webrtcvad 已安装但无法 import，请查看上方错误。"
}
Remove-Item $wvCheck -Force -ErrorAction SilentlyContinue
Write-Ok "webrtcvad 已安装并校验"

# openwakeword: 先尝试 --only-binary（最快，无需编译）；
# 若失败（microvad 等 native dep 无 wheel），回退 --no-deps 只装核心包，VAD 禁用但唤醒词仍可用。
if ($IncludeOWW) {
    Write-Info "  pip install openwakeword>=0.6.0"
    & $PyExe -m pip install 'openwakeword>=0.6.0' --only-binary=:all: 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Write-Ok "  openwakeword 安装成功"
    } else {
        Write-Warn "  全量安装失败（microvad 无 wheel）→ 回退安装核心包（VAD 禁用，唤醒词仍可用）"
        & $PyExe -m pip install 'openwakeword>=0.6.0' --no-deps --prefer-binary --quiet
        # openwakeword 核心运行时依赖（不含 microvad/VAD）
        & $PyExe -m pip install 'tqdm' 'requests' 'scipy' --prefer-binary --quiet
        if ($LASTEXITCODE -ne 0) {
            Write-Warn "  openwakeword 安装失败，唤醒词功能将不可用"
        }
    }
} else {
    Write-Info "  跳过 openwakeword（IncludeOWW=False）"
}

# onnxruntime: faster-whisper 元数据依赖，但 speech-rego 使用 vad_filter=False，无需安装（约 350+ MB）
Write-Info "  跳过 onnxruntime / onnxruntime-gpu（项目未启用 Whisper 内置 VAD）"

switch ($GPU) {
    "cuda" {
        if ($BundleNvidiaCuda) {
            # nvidia-* packages are NOT on Tsinghua mirror — must use official PyPI.
            # Install explicitly so ctranslate2 can find cublas64_12.dll etc.
            # in site-packages\nvidia\*\bin\ at runtime.
            Write-Info "  安装 CUDA 运行库（约 800 MB）..."
            $nvPkgs = @(
                'nvidia-cuda-runtime-cu12',
                'nvidia-cublas-cu12'
                # nvidia-cudnn-cu12 不需要：ctranslate2 只用 cuBLAS，不用 cuDNN
            )
            foreach ($nvp in $nvPkgs) {
                Write-Info "    pip install $nvp"
                & $PyExe -m pip install $nvp --prefer-binary `
                    --index-url https://pypi.tuna.tsinghua.edu.cn/simple
                if ($LASTEXITCODE -ne 0) { Write-Warn "    $nvp 安装失败" }
            }
            # Verify DLLs landed in site-packages\nvidia\
            $nvDir = Join-Path $PythonDir "Lib\site-packages\nvidia"
            if (Test-Path $nvDir) {
                $dlls = @(Get-ChildItem $nvDir -Recurse -Filter "*.dll" -ErrorAction SilentlyContinue)
                Write-Ok "  CUDA DLL 已打包：$($dlls.Count) 个文件"
            } else {
                Write-Warn "  site-packages\nvidia\ 不存在"
            }
        } else {
            Write-Info "  跳过 nvidia-* CUDA 运行库打包（节省约 800 MB）"
            Write-Warn "  目标机器需自行安装 NVIDIA CUDA Toolkit 12："
            Write-Warn "    https://developer.nvidia.com/cuda-downloads"
        }
    }
    "dml" {
        Write-Info "  dml 模式：Whisper 推理仍走 ctranslate2；未安装 onnxruntime-directml（本项目不需要）"
    }
}
Write-Ok "Python 依赖包安装完成"

# ── STEP 4.5: 清理 site-packages，删除运行时不需要的文件 ─────────────────────────
Write-Step "4.5" "清理 site-packages（删除缓存/测试/工具包）"

$siteDir = Join-Path $PythonDir "Lib\site-packages"

# 删除 __pycache__ 目录（所有包）
$cacheDirs = @(Get-ChildItem $siteDir -Include "__pycache__" -Recurse -Directory -ErrorAction SilentlyContinue)
foreach ($d in $cacheDirs) { Remove-Item $d.FullName -Recurse -Force -ErrorAction SilentlyContinue }
Write-Info "  已删除 $($cacheDirs.Count) 个 __pycache__ 目录"

# 删除 *.pyc / *.pyo 字节码文件
$pycFiles = @(Get-ChildItem $siteDir -Include "*.pyc","*.pyo" -Recurse -File -ErrorAction SilentlyContinue)
foreach ($f in $pycFiles) { Remove-Item $f.FullName -Force -ErrorAction SilentlyContinue }
Write-Info "  已删除 $($pycFiles.Count) 个 .pyc/.pyo 字节码文件"

# 删除各包内的 tests / test 目录（numpy、scipy 等测试数据较大）
$testDirs = @(Get-ChildItem $siteDir -Include "tests","test" -Recurse -Directory -Depth 2 -ErrorAction SilentlyContinue)
foreach ($d in $testDirs) { Remove-Item $d.FullName -Recurse -Force -ErrorAction SilentlyContinue }
Write-Info "  已删除 $($testDirs.Count) 个测试目录"

# 卸载 speech-rego 运行时不需要的第三方包（须在删除 pip 之前；最终以删目录为准）
if ($PruneUnusedDeps) {
    Write-Info "  裁剪冗余依赖（onnxruntime / HF 下载加速 / CLI 等）..."
    $pipMod = Join-Path $siteDir "pip"
    if (Test-Path $pipMod) {
        $prevEap = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            foreach ($pipName in @(
                    'onnxruntime', 'onnxruntime-gpu', 'onnxruntime-directml',
                    'hf-xet', 'onnx'
                )) {
                & $PyExe -m pip uninstall $pipName -y --quiet 2>&1 | Out-Null
            }
        } finally {
            $ErrorActionPreference = $prevEap
        }
    }
    $pruneDirs = @(
        'onnxruntime', 'hf_xet', 'typer', 'rich', 'pygments', 'shellingham',
        'annotated_doc', 'markdown_it', 'mdurl', 'colorama', 'flatbuffers',
        'google', 'onnx'
    )
    foreach ($dirName in $pruneDirs) {
        $dirPath = Join-Path $siteDir $dirName
        if (Test-Path $dirPath) {
            Remove-Item $dirPath -Recurse -Force -ErrorAction SilentlyContinue
            Write-Info "    已删除目录 $dirName"
        }
        Get-ChildItem $siteDir -Filter "$dirName-*.dist-info" -Directory -ErrorAction SilentlyContinue |
            ForEach-Object { Remove-Item $_.FullName -Recurse -Force -ErrorAction SilentlyContinue }
    }
    Get-ChildItem $siteDir -Directory -Filter "onnxruntime*.dist-info" -ErrorAction SilentlyContinue |
        ForEach-Object { Remove-Item $_.FullName -Recurse -Force -ErrorAction SilentlyContinue }
    # 验证核心 import
    $verifyPy = Join-Path $env:TEMP "speech_reco_verify_$PID.py"
    @'
import sys
mods = ("websockets", "sounddevice", "numpy", "faster_whisper", "ctranslate2", "vosk", "zhconv")
failed = []
for m in mods:
    try:
        __import__(m)
    except Exception as e:
        failed.append("%s: %s" % (m, e))
try:
    from speech_vad import create_webrtc_vad
    if create_webrtc_vad(2) is None:
        failed.append("webrtcvad: create_webrtc_vad returned None")
except Exception as e:
    failed.append("speech_vad: %s" % e)
if failed:
    print("IMPORT_FAIL")
    for x in failed:
        print(x)
    sys.exit(1)
print("IMPORT_OK")
'@ | Set-Content $verifyPy -Encoding ASCII
    & $PyExe $verifyPy
    $verifyOk = $LASTEXITCODE -eq 0
    Remove-Item $verifyPy -Force -ErrorAction SilentlyContinue
    if ($verifyOk) { Write-Ok "  核心包 import 校验通过" }
    else { Write-Warn "  裁剪后 import 校验失败，请检查是否误删依赖" }
}

# 删除 pip / wheel；保留 setuptools（webrtcvad 等运行时可能需要 pkg_resources）
foreach ($pkg in @("pip", "wheel", "_distutils_hack")) {
    $pkgPath = Join-Path $siteDir $pkg
    if (Test-Path $pkgPath) {
        Remove-Item $pkgPath -Recurse -Force -ErrorAction SilentlyContinue
        Write-Info "  已删除 $pkg"
    }
    $distInfo = @(Get-ChildItem $siteDir -Filter "${pkg}-*.dist-info" -Directory -ErrorAction SilentlyContinue)
    foreach ($d in $distInfo) { Remove-Item $d.FullName -Recurse -Force -ErrorAction SilentlyContinue }
}

foreach ($exe in @("pip.exe","pip3.exe","pip3.11.exe","wheel.exe","easy_install.exe","easy_install-3.11.exe")) {
    $exePath = Join-Path $PythonDir "Scripts\$exe"
    if (Test-Path $exePath) { Remove-Item $exePath -Force -ErrorAction SilentlyContinue }
}

$afterMB = [int]((Get-ChildItem $PythonDir -Recurse -File -ErrorAction SilentlyContinue |
                  Measure-Object -Property Length -Sum).Sum / 1MB)
Write-Ok "清理完成，Python 目录当前大小：${afterMB} MB"

# ── STEP 5: Whisper model ──────────────────────────────────────────────────────
Write-Step 5 "下载 Whisper 模型（$WhisperModel）"
Write-Info "持久缓存目录: $WModelCacheHF"

# Complete cache = snapshot contains model.bin (refs-only / blobs-only is incomplete)
$modelKey   = "models--Systran--faster-whisper-$WhisperModel"
$modelHub   = Join-Path $WModelCacheHF "hub\$modelKey"
$modelSnap  = Join-Path $modelHub "snapshots"

function Test-WhisperSnapshotComplete {
    param([string]$SnapshotsDir)
    if (-not (Test-Path $SnapshotsDir)) { return $false }
    foreach ($snap in Get-ChildItem $SnapshotsDir -Directory -ErrorAction SilentlyContinue) {
        foreach ($w in @("model.bin", "model.safetensors")) {
            $p = Join-Path $snap.FullName $w
            if ((Test-Path $p) -and ((Get-Item $p).Length -gt 1MB)) { return $true }
        }
    }
    return $false
}

if (Test-WhisperSnapshotComplete $modelSnap) {
    Write-Ok "Whisper 模型已在缓存中（含权重文件），跳过下载：$modelKey"
} else {
    if (Test-Path $modelHub) {
        Write-Warn "发现不完整的模型缓存（无 model.bin），正在删除后重新下载..."
        Remove-Item $modelHub -Recurse -Force -ErrorAction SilentlyContinue
    }
    if ($WhisperModel -eq "large-v3") {
        Write-Info "large-v3 约 3GB，下载可能需要 20-60 分钟，请保持网络畅通..."
    } else {
        Write-Info "缓存未命中，开始下载（首次约需数分钟）..."
    }

    $env:HF_HOME                  = $WModelCacheHF
    $env:HF_ENDPOINT              = $HF_MIRROR
    $env:HUGGINGFACE_HUB_ENDPOINT = $HF_MIRROR
    $env:HF_HUB_OFFLINE           = "0"

    $dlPy = Join-Path $env:TEMP "wh_dl_$PID.py"
    @'
import os, sys, glob
os.environ["HF_HOME"] = "__HFDIR__"
os.environ["HF_ENDPOINT"] = "__HFEP__"
os.environ["HUGGINGFACE_HUB_ENDPOINT"] = "__HFEP__"
os.environ.pop("HF_HUB_OFFLINE", None)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
model = "__MODEL__"
repo = f"Systran/faster-whisper-{model}"
try:
    from faster_whisper import WhisperModel
    print(f"  Downloading {model} via faster-whisper ...", flush=True)
    WhisperModel(model, device="cpu", compute_type="int8")
except Exception as e:
    print(f"  faster-whisper load failed: {e}", file=sys.stderr)
    try:
        from huggingface_hub import snapshot_download
        print(f"  Retrying snapshot_download({repo}) ...", flush=True)
        snapshot_download(repo_id=repo)
    except Exception as e2:
        print(f"  snapshot_download failed: {e2}", file=sys.stderr)
        sys.exit(1)
hub = os.path.join("__HFDIR__", "hub", f"models--Systran--faster-whisper-{model}", "snapshots", "*")
for p in glob.glob(os.path.join(hub, "model.bin")) + glob.glob(os.path.join(hub, "model.safetensors")):
    if os.path.getsize(p) > 1_000_000:
        print(f"  OK: {p} ({os.path.getsize(p) // (1024*1024)} MB)", flush=True)
        sys.exit(0)
print("  ERROR: model.bin not found after download", file=sys.stderr)
sys.exit(1)
'@ | Set-Content $dlPy -Encoding UTF8

    $hfEscaped = $WModelCacheHF -replace '\\', '\\\\'
    (Get-Content $dlPy -Raw -Encoding UTF8) `
        -replace '__HFDIR__', $hfEscaped `
        -replace '__HFEP__',  $HF_MIRROR `
        -replace '__MODEL__', $WhisperModel |
        Set-Content $dlPy -Encoding UTF8

    & $PyExe $dlPy
    if ($LASTEXITCODE -ne 0) {
        Write-Fail "Whisper 模型 '$WhisperModel' 下载失败。请检查网络、代理或 HF 镜像 ($HF_MIRROR)。"
    }
    Remove-Item $dlPy -Force -ErrorAction SilentlyContinue
    if (-not (Test-WhisperSnapshotComplete $modelSnap)) {
        Write-Fail "下载结束但 snapshot 仍无 model.bin，请重试 build_offline.bat $WhisperModel"
    }
    Write-Ok "Whisper 模型下载完成：$WhisperModel"
}

# ── 将 Whisper 模型以 HF hub 缓存结构打包进 models\hf\
# 方案：只复制 snapshot 文件（不含 blobs 重复副本），同时生成最小化的
# HF hub 目录结构（refs/main + snapshots/<hash>/），让 faster-whisper
# 通过 HF_HOME 直接定位模型，无需任何路径魔法。
$hfPackDir    = Join-Path $ModelsDir "hf"
$snapshotBase = Join-Path $WModelCacheHF "hub\$modelKey\snapshots"

if (Test-Path $snapshotBase) {
    $latestSnap = Get-ChildItem $snapshotBase -Directory |
                  Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($latestSnap) {
        $weightFile = @("model.bin", "model.safetensors") | ForEach-Object {
            Join-Path $latestSnap.FullName $_
        } | Where-Object { Test-Path $_ } | Select-Object -First 1
        if (-not $weightFile) {
            Write-Fail @"
Whisper 模型 '$WhisperModel' 下载不完整（snapshot 中无 model.bin）。
请删除缓存后重试:
  Remove-Item -Recurse -Force '$modelKey' -ErrorAction SilentlyContinue
  (位于 $WModelCacheHF\hub\)
然后重新运行 build_offline.bat $WhisperModel cuda
"@
        }
        $snapHash   = $latestSnap.Name
        $destSnap   = Join-Path $hfPackDir "hub\$modelKey\snapshots\$snapHash"
        $destRefs   = Join-Path $hfPackDir "hub\$modelKey\refs"
        New-Item -ItemType Directory -Force -Path $destSnap | Out-Null
        New-Item -ItemType Directory -Force -Path $destRefs | Out-Null
        Write-Info "打包 Whisper 模型到 models\hf\ (snapshot 含 model.bin)..."
        # Copy-Item 会复制实体文件；robocopy 默认可能只复制符号链接导致缺 model.bin
        Copy-Item -Path (Join-Path $latestSnap.FullName '*') -Destination $destSnap -Recurse -Force
        Set-Content (Join-Path $destRefs "main") $snapHash -Encoding ASCII -NoNewline
        Set-Content (Join-Path $ModelsDir "bundled_whisper_model.txt") $WhisperModel -Encoding ASCII -NoNewline
        $modelFiles = @(Get-ChildItem $destSnap -Recurse -File -ErrorAction SilentlyContinue)
        $modelSizeMB = [int](($modelFiles | Measure-Object -Property Length -Sum).Sum / 1MB)
        Write-Ok "Whisper 模型已打包（$($modelFiles.Count) 个文件，${modelSizeMB} MB）"
    } else {
        Write-Fail "找不到 Whisper snapshot 目录: $snapshotBase`n请检查 STEP 5 下载是否成功。"
    }
} else {
    Write-Fail "缓存中未找到模型: $modelKey`n请检查网络/HF 镜像后重新构建。"
}

# ── STEP 6: Vosk model ─────────────────────────────────────────────────────────
if ($IncludeVosk) {
    Write-Step 6 "下载 Vosk 中文模型（$CN_MODEL）"

    $voskCached = Join-Path $VoskCacheDir $CN_MODEL   # persistent cache location
    $voskDest   = Join-Path $ModelsDir $CN_MODEL      # output package location
    $voskZip    = Join-Path $VoskCacheDir "$CN_MODEL.zip"
       Write-Ok "Vosk 模型缓存：$voskCached"
    if (Test-Path $voskCached) {
        Write-Ok "Vosk 模型已在缓存中，跳过下载：$CN_MODEL"
    } else {
        Write-Info "缓存未命中，开始下载 $CN_URL ..."
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
            Write-Info "解压到缓存..."
            Expand-Archive -Path $voskZip -DestinationPath $VoskCacheDir -Force
            Remove-Item $voskZip -Force
            Write-Ok "Vosk 中文模型已缓存"
        } else {
            Write-Warn "Vosk 模型下载失败。可手动下载后放入 models\ 目录。"
        }
    }

    # Copy from persistent cache into output package
    if (Test-Path $voskCached) {
        Write-Info "从缓存复制 Vosk 模型到输出包..."
        robocopy $voskCached $voskDest /E /NFL /NDL /NJH /NJS /NC /NS /NP | Out-Null
        Write-Ok "Vosk 中文模型已复制到 models\$CN_MODEL\"
    }

    # Pre-fetch openwakeword built-in models (only if OWW was installed)
    if ($IncludeOWW) {
        $owwPkgModels = Join-Path $PythonDir "Lib\site-packages\openwakeword\resources\models"

        # Check persistent cache first
        $owwCached = @(Get-ChildItem $OWWCacheDir -Filter "*.onnx" -ErrorAction SilentlyContinue)
        if ($owwCached.Count -gt 0) {
            Write-Ok "openwakeword 模型已缓存（$($owwCached.Count) 个），跳过下载"
            # Restore from cache into package — create dir if pip didn't include it
            New-Item -ItemType Directory -Force -Path $owwPkgModels | Out-Null
            robocopy $OWWCacheDir $owwPkgModels /E /NFL /NDL /NJH /NJS /NC /NS /NP | Out-Null
            Write-Ok "openwakeword 模型已从缓存复制到包内"
        } else {
            Write-Info "下载 openwakeword 内置模型（从 GitHub，首次约需 1-2 分钟）..."
            # Download to default package location (openwakeword reads models from there)
            $owwPy = Join-Path $env:TEMP "oww_dl_$PID.py"
            @'
import warnings; warnings.filterwarnings("ignore")
try:
    import openwakeword
    openwakeword.utils.download_models()
    import os, glob
    pkg_dir = os.path.dirname(openwakeword.__file__)
    models_dir = os.path.join(pkg_dir, "resources", "models")
    n = len(glob.glob(os.path.join(models_dir, "*.onnx")))
    print("  openwakeword: %d models in %s" % (n, models_dir))
except Exception as e:
    print("  [skip] " + str(e))
'@ | Set-Content $owwPy -Encoding UTF8
            & $PyExe $owwPy
            Remove-Item $owwPy -Force -ErrorAction SilentlyContinue

            # Back up downloaded models to persistent cache
            if (Test-Path $owwPkgModels) {
                $downloaded = @(Get-ChildItem $owwPkgModels -Filter "*.onnx" -ErrorAction SilentlyContinue)
                if ($downloaded.Count -gt 0) {
                    robocopy $owwPkgModels $OWWCacheDir /E /NFL /NDL /NJH /NJS /NC /NS /NP | Out-Null
                    Write-Ok "openwakeword 模型已缓存（$($downloaded.Count) 个）"
                } else {
                    Write-Warn "openwakeword 模型下载后仍为空，唤醒词将回退到 Whisper 模式"
                }
            }
        }
    }
} else {
    Write-Step 6 "跳过 Vosk 模型（IncludeVosk=False）"
}

# ── STEP 7: Application files ──────────────────────────────────────────────────
Write-Step 7 "复制应用程序源文件"

$appFiles = @(
    "server.py", "engine.py",
    "server_whisper.py", "engine_whisper.py", "whisper_local.py", "speech_vad.py",
    "text_postprocess.py", "wake_word_match.py", "wake_detectors.py",
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
    # Use the standard HF model name — HF_HOME in start_whisper.bat points
    # to the bundled models\hf\ cache, so no network access is needed.
    $cfg.whisper.model = [string]$WhisperModel
    $json = $cfg | ConvertTo-Json -Depth 12
    $utf8 = New-Object System.Text.UTF8Encoding $true
    [System.IO.File]::WriteAllText($cfgOut, $json, $utf8)
    Write-Ok "config.json 已写入 whisper.model=$WhisperModel"
} catch {
    Write-Warn "config.json 自动更新失败：$_"
}

# ── STEP 8: Launch scripts ─────────────────────────────────────────────────────
Write-Step 8 "生成启动脚本"

# start_whisper.bat (ASCII only -- cmd.exe breaks on UTF-8 BOM / UTF-8 comments)
Write-CmdBat (Join-Path $OutputDir "start_whisper.bat") @"
@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
:: HF_HOME must be quoted (paths with spaces)
set "HF_HOME=%~dp0models\hf"
set HF_HUB_OFFLINE=1
set HF_DATASETS_OFFLINE=1
cd /d "%~dp0"
:: Add nvidia pip DLL dirs to PATH for ctranslate2 CUDA
for /d %%P in ("%~dp0python\Lib\site-packages\nvidia\*") do (
    if exist "%%P\bin\" set "PATH=%%P\bin;!PATH!"
)
title Speech Reco Whisper ws://127.0.0.1:8766
echo.
echo  ================================================
echo    Speech Recognition (Whisper)  port 8766
echo    Model: $WhisperModel
echo    Press Ctrl+C to stop
echo  ================================================
echo.
echo y | "%~dp0python\python.exe" "%~dp0server_whisper.py"
echo.
echo Service stopped.
pause
"@

Write-CmdBat (Join-Path $OutputDir "start_vosk.bat") @"
@echo off
chcp 65001 >nul
setlocal
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
cd /d "%~dp0"
title Speech Reco Vosk ws://127.0.0.1:8765
echo.
echo  ================================================
echo    Speech Recognition (Vosk)  port 8765
echo    Press Ctrl+C to stop
echo  ================================================
echo.
echo y | "%~dp0python\python.exe" "%~dp0server.py"
echo.
echo Service stopped.
pause
"@

Write-CmdBat (Join-Path $OutputDir "check_env.bat") @"
@echo off
chcp 65001 >nul
setlocal
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
set "HF_HOME=%~dp0models\hf"
set HF_HUB_OFFLINE=1
cd /d "%~dp0"
title Environment check
"%~dp0python\python.exe" "%~dp0check_env.py"
pause
"@

Write-Ok "start_whisper.bat / start_vosk.bat / check_env.bat 已生成"

# STEP 9: README (from UTF-8 template file -- avoids Chinese here-strings in this .ps1)
$buildTime    = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
$readmeOut    = Join-Path $OutputDir "README.txt"
$readmeTpl    = Join-Path $ScriptDir "installer_assets\offline_package_README.txt"
$utf8NoBom    = New-Object System.Text.UTF8Encoding $false
if (Test-Path -LiteralPath $readmeTpl) {
    $readmeText = [System.IO.File]::ReadAllText($readmeTpl, $utf8NoBom)
    $readmeText = $readmeText.Replace("{WhisperModel}", $WhisperModel).
        Replace("{GPU}", $GPU).Replace("{PY_VER}", $PY_VER).
        Replace("{CN_MODEL}", $CN_MODEL).Replace("{BuildTime}", $buildTime)
    [System.IO.File]::WriteAllText($readmeOut, $readmeText, $utf8NoBom)
} else {
    $fallback = @(
        "Speech Recognition - Offline Package"
        "Whisper model: $WhisperModel"
        "GPU: $GPU"
        "Built: $buildTime"
    ) -join "`r`n"
    [System.IO.File]::WriteAllText($readmeOut, $fallback + "`r`n", $utf8NoBom)
}

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
