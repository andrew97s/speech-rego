#Requires -Version 5.1
<#
.SYNOPSIS
    离线部署包构建脚本（Windows 客户端：唤醒 + VAD + 远程 FunASR）
    Builds a fully self-contained portable folder — copy it to any Windows
    machine and run start.bat without any prior installation.

.DESCRIPTION
    输出文件夹包含:
      python\      Python 3.11 嵌入式运行时 + 所有 pip 依赖
      models\      Sherpa KWS 唤醒模型 + FunASR fsmn-vad
      *.py         应用程序源码（server.py / engine.py 等）
      config.json  配置文件（WebSocket 8766，Web 9400）
      start.bat / check_env.bat

    技术栈: Sherpa KWS 唤醒 + fsmn-vad 判停 + 远程 Fun-ASR-Nano

    目标机器系统要求:
      - Windows 10 Build 1809+ / Windows 11 (x64)
      - Visual C++ 2015-2022 Redistributable (x64)
        如未安装: https://aka.ms/vs/17/release/vc_redist.x64.exe

.PARAMETER GPU
    GPU 加速模式 (默认: none)。客户端只跑 VAD，一般用 CPU。
    none  — 纯 CPU（兼容所有机器）
    cuda  — NVIDIA CUDA（需目标机器有 CUDA 12）
    dml   — DirectML（本客户端不使用）

.PARAMETER IncludeKWS
    是否安装 sherpa-onnx 并下载 Sherpa KWS 模型（默认: $true）
    别名: IncludeOWW（兼容旧参数名）

.PARAMETER Force
    输出目录已存在时直接覆盖，不询问。

.PARAMETER OutputDir
    输出目录（默认: .\dist\SpeechReco-Offline）

.EXAMPLE
    .\build_offline.ps1
    .\build_offline.ps1 -Force -GPU none
    .\build_offline.ps1 -OutputDir D:\deploy
#>

param(
    [string] $WhisperModel    = "small",
    [ValidateSet("none","cuda","dml","auto")]
    [string] $GPU             = "none",
    [Alias("IncludeOWW")]
    [bool]   $IncludeKWS      = $true,
    [bool]   $BundleNvidiaCuda = $false,
    [bool]   $PruneUnusedDeps  = $true,
    [switch] $Force,
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
        [switch]$Quiet,
        [switch]$ShowOnError
    )
    $args = @('-m', 'pip') + $PipArgs + @(
        '--no-warn-script-location',
        '--retries', '15',
        '--timeout', '120'
    )
    if ($Quiet) { $args += '--quiet' }
    $log = Join-Path $env:TEMP ("sr_pip_{0}.log" -f $PID)
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $PyExe @args *> $log
        $rc = [int]$LASTEXITCODE
        if ($rc -ne 0 -and ($ShowOnError -or -not $Quiet)) {
            Write-Warn "pip 退出码 $rc，末尾输出："
            if (Test-Path -LiteralPath $log) {
                Get-Content -LiteralPath $log -Tail 25 -ErrorAction SilentlyContinue |
                    ForEach-Object { Write-Host "      $_" }
            }
        }
        return $rc
    } finally {
        $ErrorActionPreference = $prevEap
    }
}

function Install-FunasrStack {
    Write-Info "  pip install funasr modelscope（网络中断会自动重试）"
    $attempts = @(
        @{ Label = "清华镜像"; Extra = @() },
        @{ Label = "清华镜像(清缓存重试)"; Extra = @('--no-cache-dir') },
        @{ Label = "官方 PyPI"; Extra = @('--index-url', 'https://pypi.org/simple', '--trusted-host', 'pypi.org') }
    )
    foreach ($a in $attempts) {
        Write-Info "  尝试 $($a.Label) ..."
        $rc = Invoke-EmbeddedPip (@('install', 'funasr>=1.1.6', 'modelscope', '--prefer-binary') + $a.Extra) -ShowOnError
        if ($rc -eq 0) { return $true }
    }

    Write-Warn "带依赖安装失败，改为 funasr --no-deps + 运行时必需包（跳过需编译的 umap 等）"
    $rc = Invoke-EmbeddedPip @('install', 'funasr>=1.1.6', '--no-deps', '--prefer-binary') -ShowOnError
    if ($rc -ne 0) { return $false }

    $core = @(
        'modelscope',
        'scipy',
        'librosa',
        'soundfile>=0.12.1',
        'PyYAML>=5.1.2',
        'tqdm',
        'requests',
        'regex',
        'omegaconf>=2.0',
        'hydra-core>=1.3.2',
        'huggingface_hub',
        'safetensors',
        'tiktoken',
        'sentencepiece',
        'kaldiio>=2.17.0',
        'jieba',
        'jamo',
        'jaconv',
        'rapidfuzz>=3.0.0',
        'tensorboardX',
        'oss2'
    )
    foreach ($pkg in $core) {
        $rc = Invoke-EmbeddedPip @('install', $pkg, '--prefer-binary')
        if ($rc -ne 0) { Write-Warn "  可选/依赖 $pkg 安装失败（继续）" }
    }
    return $true
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
$SHERPA_KWS_NAME   = "sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01"
$SHERPA_KWS_URL    = "https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/$SHERPA_KWS_NAME.tar.bz2"
# GitHub 直连不稳定时优先镜像（国内常见 reset/超时）
$SHERPA_KWS_URLS   = @(
    "https://ghfast.top/$SHERPA_KWS_URL",
    "https://mirror.ghproxy.com/$SHERPA_KWS_URL",
    $SHERPA_KWS_URL
)
$SHERPA_KWS_TAR_MIN_BYTES = 3MB
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
Write-Host "  客户端        : Sherpa KWS + fsmn-vad + 远程 FunASR"
Write-Host "  WebSocket     : 8766"
Write-Host "  Web UI        : 9400"
Write-Host "  GPU 模式      : $GPU"
Write-Host "  包含 KWS     : $IncludeKWS"
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
$env:PIP_DEFAULT_TIMEOUT = "180"

if (Test-Path $OutputDir) {
    if (-not $Force) {
        $ans = Read-Host "  '$OutputDir' 已存在，是否覆盖重建? [y/N]"
        if ($ans -notmatch "^[yY]") { Write-Host "已取消。"; exit 0 }
    }
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
    'zhconv>=1.4.3',
    'pypinyin>=0.49.0',
    'transformers>=4.45.0'
)

foreach ($pkg in $pkgs) {
    Write-Info "  pip install $pkg"
    $rc = Invoke-EmbeddedPip @('install', $pkg, '--prefer-binary')
    if ($rc -ne 0) {
        Write-Warn "安装 $pkg 时出错（继续）"
    }
}

# CPU torch first so funasr does not pull a CUDA wheel
Write-Info "  pip install torch+cpu / torchaudio+cpu"
$rc = Invoke-EmbeddedPip @(
    'install', 'torch', 'torchaudio', '--prefer-binary',
    '--index-url', 'https://download.pytorch.org/whl/cpu'
)
if ($rc -ne 0) {
    Write-Warn "PyTorch CPU 官方源失败，改走清华镜像"
    $rc = Invoke-EmbeddedPip @('install', 'torch', 'torchaudio', '--prefer-binary')
    if ($rc -ne 0) { Write-Fail "torch / torchaudio 安装失败" }
}
Write-Ok "torch / torchaudio 已安装"

if (-not (Install-FunasrStack)) {
    Write-Fail "funasr 安装失败（镜像下载中断时可再跑一次 build_offline.ps1 -Force）"
}

Write-Info "  verify funasr + torch"
$faCheck = Join-Path $env:TEMP "funasr_check_$PID.py"
@'
import funasr, torch
print("funasr", getattr(funasr, "__version__", "?"))
print("torch", torch.__version__, "cuda", torch.cuda.is_available())
'@ | Set-Content $faCheck -Encoding ASCII
& $PyExe $faCheck
if ($LASTEXITCODE -ne 0) {
    Remove-Item $faCheck -Force -ErrorAction SilentlyContinue
    Write-Fail "funasr/torch 校验失败"
}
Remove-Item $faCheck -Force -ErrorAction SilentlyContinue
Write-Ok "funasr / torch 已校验"

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
        Write-Info "  dml 模式：客户端 VAD 走 FunASR/torch CPU，无需 DirectML"
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
    Write-Info "  裁剪冗余依赖（HF CLI 等；保留 funasr / torch / modelscope）..."
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
        'annotated_doc', 'markdown_it', 'mdurl'
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
mods = ("websockets", "sounddevice", "numpy", "funasr", "torch", "torchaudio", "zhconv", "pypinyin")
failed = []
for m in mods:
    try:
        __import__(m)
    except Exception as e:
        failed.append("%s: %s" % (m, e))
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

# ── STEP 5: FunASR fsmn-vad (client VAD only; no Fun-ASR-Nano) ──────────────
Write-Step 5 "打包 fsmn-vad 模型"
$vadRel = "models\funasr\models\iic--speech_fsmn_vad_zh-cn-16k-common-pytorch"
$vadSrc = Join-Path $ScriptDir $vadRel
$vadDst = Join-Path $OutputDir $vadRel
if (Test-Path (Join-Path $vadSrc "model.pt")) {
    New-Item -ItemType Directory -Force -Path $vadDst | Out-Null
    robocopy $vadSrc $vadDst /E /NFL /NDL /NJH /NJS /NC /NS /NP | Out-Null
    if ($LASTEXITCODE -ge 8) { Write-Fail "复制 fsmn-vad 失败，robocopy 退出码: $LASTEXITCODE" }
    $vadMB = [int]((Get-ChildItem $vadDst -Recurse -File | Measure-Object -Property Length -Sum).Sum / 1MB)
    Write-Ok "fsmn-vad 已打包（${vadMB} MB）"
} else {
    Write-Warn "源目录没有 fsmn-vad 权重: $vadSrc"
    Write-Warn "安装后首次启动会从 ModelScope 下载 fsmn-vad"
}

# ── STEP 6: Sherpa KWS model ───────────────────────────────────────────────────
if ($IncludeKWS) {
    Write-Step 6 "下载 Sherpa KWS 模型（$SHERPA_KWS_NAME）"
    $sherpaOutDir = Join-Path $ModelsDir "sherpa-kws\$SHERPA_KWS_NAME"
    $sherpaCached = Join-Path $SherpaKwsCacheDir $SHERPA_KWS_NAME
    $tokensCached = Join-Path $sherpaCached "tokens.txt"
    $localSherpa = Join-Path $ScriptDir "models\sherpa-kws\$SHERPA_KWS_NAME"
    if ((-not (Test-Path $tokensCached)) -and (Test-Path (Join-Path $localSherpa "tokens.txt"))) {
        Write-Info "使用仓库内已有 Sherpa KWS 模型"
        New-Item -ItemType Directory -Force -Path $sherpaCached | Out-Null
        robocopy $localSherpa $sherpaCached /E /NFL /NDL /NJH /NJS /NC /NS /NP | Out-Null
    }

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

    # 预生成默认唤醒词 keywords.txt（智安）
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
    "chunk_size": 16,
    "epoch_tag": "epoch-12-avg-2",
    "use_int8": True,
    "tokens_type": "ppinyin",
    "lexicon": "",
}
paths = resolve_sherpa_model_paths(cfg, base.parent.parent)
out = build_sherpa_keywords_file(["智安"], cfg, paths, cache_dir=Path(r"__KW_CACHE__"))
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
    "server.py", "engine.py", "funasr_asr.py", "remote_asr.py", "speech_vad.py",
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
    $cfg.host = "127.0.0.1"
    $cfg.port = 8766
    $cfg.http_port = 9400
    if ($cfg.funasr) {
        $cfg.funasr.device = "cpu"
        $cfg.funasr.cache_dir = "models/funasr"
    }
    if ($cfg.PSObject.Properties.Name -contains 'asr') {
        $cfg.PSObject.Properties.Remove('asr')
    }
    if ($cfg.wake_word.PSObject.Properties.Name -contains 'mode') {
        $cfg.wake_word.PSObject.Properties.Remove('mode')
    }
    $json = $cfg | ConvertTo-Json -Depth 12
    $utf8 = New-Object System.Text.UTF8Encoding $true
    [System.IO.File]::WriteAllText($cfgOut, $json, $utf8)
    Write-Ok "config.json 已写入 port=8766 http_port=9400 funasr.device=cpu"
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
set "MODELSCOPE_CACHE=%~dp0models\funasr"
set "MODELSCOPE_MODULES_CACHE=%~dp0models\funasr"
cd /d "%~dp0."
title Speech Client  ws://127.0.0.1:8766  http://127.0.0.1:9400
echo.
echo  ================================================
echo    Speech client  (wake + remote FunASR)
echo    WebSocket : ws://127.0.0.1:8766
echo    Web UI    : http://127.0.0.1:9400/index.html
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
set "MODELSCOPE_CACHE=%~dp0models\funasr"
cd /d "%~dp0."
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
    $readmeText = $readmeText.Replace("{WhisperModel}", "remote-funasr").
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
Write-Host "    1. 将 '$OutputDir' 整个文件夹复制到目标机器，或继续运行 build_client_installer.bat"
Write-Host "    2. 运行 check_env.bat 验证环境"
Write-Host "    3. 运行 start.bat 启动（控制台），或安装为 Windows 服务"
Write-Host "    Web UI     http://127.0.0.1:9400/index.html"
Write-Host "    WebSocket  ws://127.0.0.1:8766"
Write-Host ""
