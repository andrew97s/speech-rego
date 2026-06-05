#Requires -Version 5.1
<#
.SYNOPSIS
    离线部署包构建脚本
    Builds a fully self-contained portable folder — copy it to any Windows
    machine and run start.bat without any prior installation.

.DESCRIPTION
    输出文件夹包含:
      python\      Python 3.11 嵌入式运行时 + 所有 pip 依赖
      models\      Whisper 模型 HuggingFace 本地缓存 + Sherpa KWS 模型
      *.py         应用程序源码（server.py / engine.py 等）
      config.json  配置文件（已自动调整路径）
      start.bat / check_env.bat

    技术栈: Sherpa KWS 唤醒 + Silero VAD 判停 + faster-whisper ASR

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

.PARAMETER IncludeKWS
    是否安装 sherpa-onnx 并下载 Sherpa KWS 模型（默认: $true）
    别名: IncludeOWW（兼容旧参数名）

.PARAMETER BundleNvidiaCuda
    是否将 nvidia-cublas / nvidia-cudnn 等 CUDA 运行库打包进部署包（默认: $false）
    关闭可节省约 800 MB；目标机器需自行安装 NVIDIA CUDA Toolkit 12
    启用可完全离线使用 CUDA：-BundleNvidiaCuda $true

.PARAMETER OutputDir
    输出目录（默认: .\dist\SpeechReco-Offline）

.EXAMPLE
    .\build_offline.ps1 -WhisperModel tiny -GPU none
        # 约最小体积：CPU + tiny 模型
    .\build_offline.ps1 -WhisperModel small -GPU cuda -IncludeKWS $false
        # 不打包 Sherpa KWS（无唤醒词功能）
    .\build_offline.ps1 -PruneUnusedDeps $false
        # 保留 pip 拉取的全部依赖（调试用）
    .\build_offline.ps1 -WhisperModel small -GPU cuda -OutputDir D:\deploy
#>

param(
    [string] $WhisperModel    = "small",
    [ValidateSet("none","cuda","dml","auto")]
    [string] $GPU             = "auto",
    [Alias("IncludeOWW")]
    [bool]   $IncludeKWS      = $true,
    [bool]   $BundleNvidiaCuda = $false, # 是否打包 nvidia-* CUDA 运行库（约 800MB）；false=目标机需自行安装 CUDA Toolkit
    [bool]   $PruneUnusedDeps  = $true,  # 构建后卸载 onnxruntime / hf_xet / HF CLI 等运行时不需要的包
    [string] $OutputDir       = ""
)

if ($PSBoundParameters.ContainsKey("IncludeOWW")) {
    $IncludeKWS = [bool]$IncludeOWW
}

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

# pip 常把 WARNING 写到 stderr；PowerShell 会误报 NativeCommandError。只看 exit code。
function Invoke-EmbeddedPip {
    param(
        [Parameter(Mandatory)][string[]]$PipArgs,
        [switch]$Quiet
    )
    $args = @('-m', 'pip') + $PipArgs + '--no-warn-script-location'
    if ($Quiet) { $args += '--quiet' }
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $PyExe @args 2>&1 | Out-Null
        return [int]$LASTEXITCODE
    } finally {
        $ErrorActionPreference = $prevEap
    }
}

# 校验下载文件存在且体积合理；过小/损坏则删除并返回 $false
function Test-DownloadComplete {
    param(
        [Parameter(Mandatory)][string]$Path,
        [long]$MinBytes = 1MB
    )
    if (-not (Test-Path -LiteralPath $Path)) { return $false }
    try {
        $len = (Get-Item -LiteralPath $Path).Length
    } catch {
        return $false
    }
    if ($len -lt $MinBytes) {
        Write-Warn "  文件过小或未完成 ($len bytes < $MinBytes)，删除: $Path"
        Remove-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
        return $false
    }
    return $true
}

# 多 URL + 重试下载；curl 失败时回退 Invoke-WebRequest
function Invoke-RobustDownload {
    param(
        [Parameter(Mandatory)][string[]]$Urls,
        [Parameter(Mandatory)][string]$OutFile,
        [long]$MinBytes = 1MB,
        [int]$MaxRetries = 3
    )
    $dir = Split-Path -Parent $OutFile
    if ($dir -and -not (Test-Path $dir)) {
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
    }
    if (Test-DownloadComplete -Path $OutFile -MinBytes $MinBytes) {
        $mb = [int]((Get-Item -LiteralPath $OutFile).Length / 1MB)
        Write-Info "使用已缓存文件：$OutFile (${mb} MB)"
        return $true
    }
    Remove-Item -LiteralPath $OutFile -Force -ErrorAction SilentlyContinue

    foreach ($url in $Urls) {
        for ($attempt = 1; $attempt -le $MaxRetries; $attempt++) {
            Write-Info "  下载 ($attempt/$MaxRetries): $url"
            Remove-Item -LiteralPath $OutFile -Force -ErrorAction SilentlyContinue
            $ok = $false
            $prevEap = $ErrorActionPreference
            $ErrorActionPreference = 'Continue'
            try {
                if (Get-Command curl.exe -ErrorAction SilentlyContinue) {
                    & curl.exe -L --fail --retry 2 --retry-delay 3 `
                        --connect-timeout 30 --max-time 7200 `
                        -o $OutFile $url 2>&1 | Out-Null
                    if ($LASTEXITCODE -eq 0 -and (Test-DownloadComplete -Path $OutFile -MinBytes $MinBytes)) {
                        $ok = $true
                    } else {
                        Remove-Item -LiteralPath $OutFile -Force -ErrorAction SilentlyContinue
                    }
                }
                if (-not $ok) {
                    Invoke-WebRequest -Uri $url -OutFile $OutFile -UseBasicParsing -TimeoutSec 7200
                    if (Test-DownloadComplete -Path $OutFile -MinBytes $MinBytes) {
                        $ok = $true
                    } else {
                        Remove-Item -LiteralPath $OutFile -Force -ErrorAction SilentlyContinue
                    }
                }
            } catch {
                Write-Warn "  下载失败: $_"
                Remove-Item -LiteralPath $OutFile -Force -ErrorAction SilentlyContinue
            } finally {
                $ErrorActionPreference = $prevEap
            }
            if ($ok) {
                $mb = [int]((Get-Item -LiteralPath $OutFile).Length / 1MB)
                Write-Ok "  下载完成 (${mb} MB)"
                return $true
            }
            if ($attempt -lt $MaxRetries) {
                $wait = 5 * $attempt
                Write-Info "  ${wait}s 后重试..."
                Start-Sleep -Seconds $wait
            }
        }
    }
    return $false
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
$SherpaKwsCacheDir = Join-Path $CacheDir "sherpa-kws"
$SHERPA_KWS_NAME   = "sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20"
$SHERPA_KWS_URL    = "https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/$SHERPA_KWS_NAME.tar.bz2"
# GitHub 直连不稳定时优先镜像（国内常见 reset/超时）
$SHERPA_KWS_URLS   = @(
    "https://ghfast.top/$SHERPA_KWS_URL",
    "https://mirror.ghproxy.com/$SHERPA_KWS_URL",
    $SHERPA_KWS_URL
)
$SHERPA_KWS_TAR_MIN_BYTES = 5MB
$PipCacheDir   = Join-Path $CacheDir "pip-cache"   # pip wheel 缓存（自动被 pip 使用）

$PY_VER     = "3.11.9"
$PY_ZIP     = "python-$PY_VER-embed-amd64.zip"
$PY_URL     = "https://www.python.org/ftp/python/$PY_VER/$PY_ZIP"
$GETPIP_URL = "https://bootstrap.pypa.io/get-pip.py"
$HF_MIRROR  = "https://hf-mirror.com"    # faster in China

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
Write-Host "  包含 KWS     : $IncludeKWS"
Write-Host "  打包 NVIDIA   : $BundleNvidiaCuda"
Write-Host $border -ForegroundColor Cyan

# ── STEP 1: Output directory ───────────────────────────────────────────────────
Write-Step 1 "准备输出目录"

# Ensure persistent cache dirs exist (never removed)
foreach ($d in @($CacheDir, $WModelCacheHF, $SherpaKwsCacheDir, $PipCacheDir)) {
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
    'zhconv>=1.4.3',
    'pypinyin>=0.49.0',
    'silero-vad>=5.1.0,<6',
    'onnxruntime>=1.16.0'
)

foreach ($pkg in $pkgs) {
    Write-Info "  pip install $pkg"
    $rc = Invoke-EmbeddedPip @('install', $pkg, '--prefer-binary')
    if ($rc -ne 0) {
        Write-Warn "安装 $pkg 时出错（继续）"
    }
}

# Silero VAD（Whisper 录音结束判停）
Write-Info "  verify silero-vad + onnxruntime"
$svCheck = Join-Path $env:TEMP "silero_check_$PID.py"
@'
import sys
sys.path.insert(0, r'__SCRIPT_DIR__')
from speech_vad import create_silero_vad
s = create_silero_vad(0.5)
assert s is not None, "create_silero_vad returned None"
print("silero ok")
'@.Replace('__SCRIPT_DIR__', ($ScriptDir -replace '\\', '/')) |
    Set-Content $svCheck -Encoding ASCII
& $PyExe $svCheck
if ($LASTEXITCODE -ne 0) {
    Remove-Item $svCheck -Force -ErrorAction SilentlyContinue
    Write-Fail "Silero VAD 校验失败，请确认 silero-vad 与 onnxruntime 已安装。"
}
Remove-Item $svCheck -Force -ErrorAction SilentlyContinue
Write-Ok "Silero VAD 已校验"

# sherpa-onnx: KeywordSpotter 唤醒
if ($IncludeKWS) {
    Write-Info "  pip install sherpa-onnx>=1.10.0"
    $rc = Invoke-EmbeddedPip @('install', 'sherpa-onnx>=1.10.0', '--prefer-binary')
    if ($rc -ne 0) {
        Write-Warn "  sherpa-onnx 安装失败，唤醒词功能将不可用"
    } else {
        Write-Ok "  sherpa-onnx 安装成功"
    }
    Write-Info "  pip install sentencepiece (text2token 依赖)"
    $rc = Invoke-EmbeddedPip @('install', 'sentencepiece>=0.2.0', '--prefer-binary')
    if ($rc -ne 0) {
        Write-Warn "  sentencepiece 安装失败，运行时生成唤醒词 keywords 将不可用"
    } else {
        Write-Ok "  sentencepiece 安装成功"
    }
} else {
    Write-Info "  跳过 sherpa-onnx（IncludeKWS=False）"
}

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
                $rc = Invoke-EmbeddedPip @(
                    'install', $nvp, '--prefer-binary',
                    '--index-url', 'https://pypi.tuna.tsinghua.edu.cn/simple'
                )
                if ($rc -ne 0) { Write-Warn "    $nvp 安装失败" }
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
    Write-Info "  裁剪冗余依赖（HF 下载加速 / CLI 等；保留 onnxruntime 供 Silero VAD）..."
    $pipMod = Join-Path $siteDir "pip"
    if (Test-Path $pipMod) {
        $prevEap = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            foreach ($pipName in @(
                    'onnxruntime-gpu', 'onnxruntime-directml',
                    'hf-xet', 'onnx'
                )) {
                & $PyExe -m pip uninstall $pipName -y --quiet 2>&1 | Out-Null
            }
        } finally {
            $ErrorActionPreference = $prevEap
        }
    }
    $pruneDirs = @(
        'hf_xet', 'typer', 'rich', 'pygments', 'shellingham',
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
    # 验证核心 import
    $verifyPy = Join-Path $env:TEMP "speech_reco_verify_$PID.py"
    @'
import sys
mods = ("websockets", "sounddevice", "numpy", "faster_whisper", "ctranslate2", "zhconv", "pypinyin")
failed = []
for m in mods:
    try:
        __import__(m)
    except Exception as e:
        failed.append("%s: %s" % (m, e))
try:
    from speech_vad import create_silero_vad
    if create_silero_vad(0.5) is None:
        failed.append("silero: create_silero_vad returned None")
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

# 删除 pip / wheel；保留 setuptools（部分包运行时可能需要 pkg_resources）
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

# ── STEP 6: Sherpa KWS model ───────────────────────────────────────────────────
if ($IncludeKWS) {
    Write-Step 6 "下载 Sherpa KWS 模型（$SHERPA_KWS_NAME）"
    $sherpaOutDir = Join-Path $ModelsDir "sherpa-kws\$SHERPA_KWS_NAME"
    $sherpaCached = Join-Path $SherpaKwsCacheDir $SHERPA_KWS_NAME
    $tokensCached = Join-Path $sherpaCached "tokens.txt"

    if (-not (Test-Path $tokensCached)) {
        $tarName = "$SHERPA_KWS_NAME.tar.bz2"
        $tarPath = Join-Path $SherpaKwsCacheDir $tarName

        if (-not (Test-DownloadComplete -Path $tarPath -MinBytes $SHERPA_KWS_TAR_MIN_BYTES)) {
            Write-Info "Sherpa KWS 模型包未缓存或文件不完整，开始下载..."
            $dlOk = Invoke-RobustDownload `
                -Urls $SHERPA_KWS_URLS `
                -OutFile $tarPath `
                -MinBytes $SHERPA_KWS_TAR_MIN_BYTES `
                -MaxRetries 3
            if (-not $dlOk) {
                Write-Fail @"
Sherpa KWS 模型下载失败（已尝试 GitHub 直连与镜像）。

请手动下载后放到下列路径，再重新运行 build_offline.bat：
  $tarPath

直链：
  $($SHERPA_KWS_URLS[0])

镜像示例（浏览器或下载工具）：
  $($SHERPA_KWS_URLS[1])
"@
            }
        } else {
            $mb = [int]((Get-Item -LiteralPath $tarPath).Length / 1MB)
            Write-Info "使用本地缓存：$tarPath (${mb} MB)"
        }

        Write-Info "解压 Sherpa KWS 模型..."
        $extractPy = Join-Path $env:TEMP "sherpa_extract_$PID.py"
        @'
import os, sys, tarfile
tar_path = r"__TAR__"
out_dir = r"__OUT__"
if not os.path.isfile(tar_path):
    print("ERROR: tar missing:", tar_path, file=sys.stderr)
    sys.exit(1)
try:
    with tarfile.open(tar_path, "r:bz2") as tf:
        tf.extractall(out_dir)
except Exception as e:
    print("ERROR: extract failed:", e, file=sys.stderr)
    sys.exit(2)
tokens = os.path.join(out_dir, "__NAME__", "tokens.txt")
if not os.path.isfile(tokens):
    print("ERROR: tokens.txt not found:", tokens, file=sys.stderr)
    sys.exit(3)
print("extract ok:", tokens)
'@.Replace('__TAR__', ($tarPath -replace '\\', '/')).
            Replace('__OUT__', ($SherpaKwsCacheDir -replace '\\', '/')).
            Replace('__NAME__', $SHERPA_KWS_NAME) |
            Set-Content $extractPy -Encoding ASCII
        & $PyExe $extractPy
        $extractRc = $LASTEXITCODE
        Remove-Item $extractPy -Force -ErrorAction SilentlyContinue
        if ($extractRc -ne 0) {
            Write-Warn "解压失败，删除可能损坏的 tar 包以便下次重下..."
            Remove-Item -LiteralPath $tarPath -Force -ErrorAction SilentlyContinue
            Write-Fail "Sherpa KWS 解压失败（exit=$extractRc）。请检查网络后重试，或手动解压到：`n  $SherpaKwsCacheDir"
        }
        if (-not (Test-Path $tokensCached)) {
            Write-Fail "Sherpa KWS 解压后未找到 tokens.txt：$tokensCached"
        }
        Write-Ok "Sherpa KWS 模型已缓存"
    } else {
        Write-Ok "Sherpa KWS 模型已在缓存中，跳过下载"
    }

    New-Item -ItemType Directory -Force -Path $sherpaOutDir | Out-Null
    robocopy $sherpaCached $sherpaOutDir /E /NFL /NDL /NJH /NJS /NC /NS /NP | Out-Null
    Write-Ok "Sherpa KWS 模型已打包到 models\sherpa-kws\$SHERPA_KWS_NAME"

    # 预生成默认唤醒词 keywords.txt（小智）
    $kwCache = Join-Path $sherpaOutDir ".keywords-cache"
    New-Item -ItemType Directory -Force -Path $kwCache | Out-Null
    $kwGenPy = Join-Path $env:TEMP "sherpa_kwgen_$PID.py"
    @'
import sys
sys.path.insert(0, r"__SCRIPT_DIR__")
from pathlib import Path
from wake_detectors import build_sherpa_keywords_file, resolve_sherpa_model_paths
base = Path(r"__OUT__")
cfg = {
    "model_dir": str(base),
    "chunk_size": 8,
    "use_int8": True,
    "tokens_type": "phone+ppinyin",
    "lexicon": "en.phone",
}
paths = resolve_sherpa_model_paths(cfg, base.parent.parent)
out = build_sherpa_keywords_file(["小智"], cfg, paths, cache_dir=Path(r"__KW_CACHE__"))
print("keywords:", out)
'@.Replace('__SCRIPT_DIR__', ($ScriptDir -replace '\\', '/')).
        Replace('__OUT__', ($sherpaOutDir -replace '\\', '/')).
        Replace('__KW_CACHE__', ($kwCache -replace '\\', '/')) |
        Set-Content $kwGenPy -Encoding ASCII
    & $PyExe $kwGenPy
    if ($LASTEXITCODE -ne 0) {
        Write-Warn "默认 keywords.txt 生成失败（首次启动时会重试 text2token）"
    } else {
        Write-Ok "默认唤醒词 keywords 已预生成"
    }
    Remove-Item $kwGenPy -Force -ErrorAction SilentlyContinue
} else {
    Write-Step 6 "跳过 Sherpa KWS 模型（IncludeKWS=False，无唤醒词功能）"
}

# ── STEP 7: Application files ──────────────────────────────────────────────────
Write-Step 7 "复制应用程序源文件"

$appFiles = @(
    "server.py", "engine.py", "whisper_local.py", "speech_vad.py",
    "text_postprocess.py", "wake_config.py", "wake_detectors.py", "wake_gating.py",
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
    $cfg.whisper.model = [string]$WhisperModel
    if ($cfg.PSObject.Properties.Name -contains 'asr') {
        $cfg.PSObject.Properties.Remove('asr')
    }
    if ($cfg.wake_word.PSObject.Properties.Name -contains 'mode') {
        $cfg.wake_word.PSObject.Properties.Remove('mode')
    }
    $cfg.port = 8765
    $json = $cfg | ConvertTo-Json -Depth 12
    $utf8 = New-Object System.Text.UTF8Encoding $true
    [System.IO.File]::WriteAllText($cfgOut, $json, $utf8)
    Write-Ok "config.json 已写入 whisper.model=$WhisperModel"
} catch {
    Write-Warn "config.json 自动更新失败：$_"
}

# ── STEP 8: Launch scripts ─────────────────────────────────────────────────────
Write-Step 8 "生成启动脚本"

# start.bat (ASCII only -- cmd.exe breaks on UTF-8 BOM / UTF-8 comments)
Write-CmdBat (Join-Path $OutputDir "start.bat") @"
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
title Speech Reco ws://127.0.0.1:8765
echo.
echo  ================================================
echo    Speech Recognition  port 8765
echo    Model: $WhisperModel
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

Write-Ok "start.bat / check_env.bat 已生成"

# STEP 9: README (from UTF-8 template file -- avoids Chinese here-strings in this .ps1)
$buildTime    = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
$readmeOut    = Join-Path $OutputDir "README.txt"
$readmeTpl    = Join-Path $ScriptDir "installer_assets\offline_package_README.txt"
$utf8NoBom    = New-Object System.Text.UTF8Encoding $false
if (Test-Path -LiteralPath $readmeTpl) {
    $readmeText = [System.IO.File]::ReadAllText($readmeTpl, $utf8NoBom)
    $readmeText = $readmeText.Replace("{WhisperModel}", $WhisperModel).
        Replace("{GPU}", $GPU).Replace("{PY_VER}", $PY_VER).
        Replace("{BuildTime}", $buildTime)
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
Write-Host "    3. 运行 start.bat 启动服务"
Write-Host ""
