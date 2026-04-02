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
    .\build_offline.ps1                                  # 最小包（CPU/DML，无 OWW）
    .\build_offline.ps1 -WhisperModel base -GPU none     # 纯 CPU 最小包
    .\build_offline.ps1 -GPU cuda -BundleNvidiaCuda $true  # 完整 CUDA 离线包
    .\build_offline.ps1 -WhisperModel small -GPU cuda -OutputDir D:\deploy
#>

param(
    [string] $WhisperModel    = "small",
    [ValidateSet("none","cuda","dml","auto")]
    [string] $GPU             = "auto",
    [bool]   $IncludeVosk     = $true,
    [bool]   $IncludeOWW      = $false,   # openwakeword 唤醒词（默认关闭；默认用 vosk/whisper 唤醒词，无需额外依赖）
    [bool]   $BundleNvidiaCuda = $false,  # 是否打包 nvidia-* CUDA 运行库（约 800MB）；false=目标机需自行安装 CUDA Toolkit
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
    'vosk>=0.3.45'
)

foreach ($pkg in $pkgs) {
    Write-Info "  pip install $pkg"
    & $PyExe -m pip install $pkg --prefer-binary
    if ($LASTEXITCODE -ne 0) {
        Write-Warn "安装 $pkg 时出错（继续）"
    }
}

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

# GPU variant of onnxruntime
switch ($GPU) {
    "cuda" {
        if ($BundleNvidiaCuda) {
            # nvidia-* packages are NOT on Tsinghua mirror — must use official PyPI.
            # Install explicitly so ctranslate2 can find cublas64_12.dll etc.
            # in site-packages\nvidia\*\bin\ at runtime.
            Write-Info "  安装 CUDA 运行库（从官方 PyPI，约 800 MB）..."
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
        & $PyExe -m pip uninstall onnxruntime -y --quiet 2>&1 | Out-Null
        Write-Info "  安装 onnxruntime-gpu..."
        & $PyExe -m pip install 'onnxruntime-gpu>=1.17.0' --prefer-binary
        if ($LASTEXITCODE -ne 0) {
            Write-Warn "onnxruntime-gpu install failed, keeping CPU version"
            & $PyExe -m pip install 'onnxruntime>=1.16.0' --prefer-binary --quiet
        }
    }
    "dml" {
        Write-Info "  Installing DirectML support..."
        & $PyExe -m pip uninstall onnxruntime -y --quiet 2>&1 | Out-Null
        & $PyExe -m pip install 'onnxruntime-directml>=1.17.0' --prefer-binary --quiet
        if ($LASTEXITCODE -ne 0) {
            Write-Warn "onnxruntime-directml install failed, keeping CPU version"
            & $PyExe -m pip install 'onnxruntime>=1.16.0' --prefer-binary --quiet
        }
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

# 删除 pip / setuptools / wheel（嵌入式运行时不需要安装工具）
foreach ($pkg in @("pip", "setuptools", "wheel", "_distutils_hack")) {
    $pkgPath = Join-Path $siteDir $pkg
    if (Test-Path $pkgPath) {
        Remove-Item $pkgPath -Recurse -Force -ErrorAction SilentlyContinue
        Write-Info "  已删除 $pkg"
    }
    # 也删除对应的 .dist-info
    $distInfo = @(Get-ChildItem $siteDir -Filter "${pkg}-*.dist-info" -Directory -ErrorAction SilentlyContinue)
    foreach ($d in $distInfo) { Remove-Item $d.FullName -Recurse -Force -ErrorAction SilentlyContinue }
}

# 删除 Scripts 下的 pip / wheel 可执行文件
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

# Check whether this model is already cached (HF hub structure)
$modelKey  = "models--Systran--faster-whisper-$WhisperModel"
$modelSnap = Join-Path $WModelCacheHF "hub\$modelKey\snapshots"
$alreadyCached = (Test-Path $modelSnap) -and ((Get-ChildItem $modelSnap -ErrorAction SilentlyContinue).Count -gt 0)

if ($alreadyCached) {
    Write-Ok "Whisper 模型已在缓存中，跳过下载：$modelKey"
} else {
    Write-Info "缓存未命中，开始下载（首次约需几分钟）..."

    $env:HF_HOME     = $WModelCacheHF
    $env:HF_ENDPOINT = $HF_MIRROR

    # Write Python download script to a temp file to avoid heredoc parsing issues
    $dlPy = Join-Path $env:TEMP "wh_dl_$PID.py"
    @'
import os, sys
os.environ["HF_HOME"]     = "__HFDIR__"
os.environ["HF_ENDPOINT"] = "__HFEP__"
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
try:
    from faster_whisper import WhisperModel
    print("  Downloading Whisper model __MODEL__ ...")
    m = WhisperModel("__MODEL__", device="cpu", compute_type="int8")
    print("  Whisper model ready.")
    del m
except Exception as e:
    print("  [warn] " + str(e), file=sys.stderr)
    print("  Model will be downloaded automatically on first service start.")
'@ | Set-Content $dlPy -Encoding UTF8

    $hfEscaped = $WModelCacheHF -replace '\\', '\\\\'
    (Get-Content $dlPy -Raw -Encoding UTF8) `
        -replace '__HFDIR__', $hfEscaped `
        -replace '__HFEP__',  $HF_MIRROR `
        -replace '__MODEL__', $WhisperModel |
        Set-Content $dlPy -Encoding UTF8

    & $PyExe $dlPy
    Remove-Item $dlPy -Force -ErrorAction SilentlyContinue
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
        $snapHash   = $latestSnap.Name
        $destSnap   = Join-Path $hfPackDir "hub\$modelKey\snapshots\$snapHash"
        $destRefs   = Join-Path $hfPackDir "hub\$modelKey\refs"
        New-Item -ItemType Directory -Force -Path $destSnap | Out-Null
        New-Item -ItemType Directory -Force -Path $destRefs | Out-Null
        Write-Info "打包 Whisper 模型到 models\hf\ (只含 snapshot，无 blobs 副本)..."
        robocopy $latestSnap.FullName $destSnap /E /NFL /NDL /NJH /NJS /NC /NS /NP | Out-Null
        # 写 refs/main 让 huggingface_hub 能按 revision 定位
        Set-Content (Join-Path $destRefs "main") $snapHash -Encoding ASCII -NoNewline
        $modelFiles = @(Get-ChildItem $destSnap -Recurse -File -ErrorAction SilentlyContinue)
        if ($modelFiles.Count -gt 0) {
            $modelSizeMB = [int](($modelFiles | Measure-Object -Property Length -Sum).Sum / 1MB)
            Write-Ok "Whisper 模型已打包（$($modelFiles.Count) 个文件，${modelSizeMB} MB）"
        } else {
            Write-Warn "snapshot 目录为空，目标机器首次启动将尝试联网下载"
        }
    } else {
        Write-Warn "找不到 snapshot 子目录，服务首次启动时将自动下载"
    }
} else {
    Write-Warn "缓存中未找到模型文件，服务首次启动时将自动下载"
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
    # Use the standard HF model name — HF_HOME in start_whisper.bat points
    # to the bundled models\hf\ cache, so no network access is needed.
    $cfg.whisper.model  = $WhisperModel
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
setlocal enabledelayedexpansion
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
:: 将 HF_HOME 指向包内缓存，禁止联网（模型已离线打包到 models\hf\）
set HF_HOME=%~dp0models\hf
set HF_HUB_OFFLINE=1
set HF_DATASETS_OFFLINE=1
cd /d "%~dp0"
:: 将 nvidia pip 包的 DLL 目录加入 PATH，确保 ctranslate2 能加载 cublas64_12.dll 等
:: (no-op if directory doesn't exist — safe for CPU-only packages)
for /d %%P in ("%~dp0python\Lib\site-packages\nvidia\*") do (
    if exist "%%P\bin\" set "PATH=%%P\bin;!PATH!"
)
title 语音识别服务 (Whisper) - ws://127.0.0.1:8766
echo.
echo  ================================================
echo    语音识别服务 (Whisper)   端口 8766
echo    Whisper 模型: $WhisperModel
echo    Press Ctrl+C 停止服务
echo  ================================================
echo.
echo y | python\python.exe server_whisper.py
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
echo y | python\python.exe server.py
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
set HF_HUB_OFFLINE=1
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
  - 若使用 CUDA 模式且未打包 NVIDIA 库 (BundleNvidiaCuda=false):
    需在目标机器安装 NVIDIA CUDA Toolkit 12
    下载: https://developer.nvidia.com/cuda-downloads

目录说明:
  python\               Python $PY_VER 嵌入式运行时 + 所有依赖包
  models\whisper-$WhisperModel\  Whisper 模型文件（已展开，直接加载）
  models\$CN_MODEL\     Vosk 中文语音模型
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
