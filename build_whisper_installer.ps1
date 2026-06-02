#Requires -Version 5.1
<#
.SYNOPSIS
    构建 Whisper 离线包 Windows 安装程序（Inno Setup）：解压到指定目录、注册服务、现代向导 UI、支持重复安装与卸载清理。

.DESCRIPTION
    1. 调用 build_service_installer.ps1 生成暂存目录（离线包 + nssm + start_svc.bat + install/uninstall 脚本）。
       服务实际执行的是 start_svc.bat（由 start.bat 自动生成的无人值守版，与控制台版环境一致）。
    2. 生成 Inno 向导位图（installer_assets）。
    3. 编译 installer_whisper.iss -> dist\SpeechReco-Whisper-Setup.exe

.PARAMETER ReuseStaging
    若已手动准备好暂存目录，跳过 build_service_installer（需配合 -StagingDir 指向已有完整目录）。

.PARAMETER OfflinePackageDir
    build_offline.ps1 输出目录（默认 .\dist\SpeechReco-Offline）

.PARAMETER StagingDir
    服务版暂存目录（默认 .\dist\SpeechReco-InnoStaging）

.PARAMETER InnoPath
    ISCC.exe 路径；若为空则在默认路径查找。

.EXAMPLE
    .\build_whisper_installer.ps1
    .\build_whisper_installer.ps1 -OfflinePackageDir .\dist\SpeechReco-Offline
#>

param(
    [switch] $ReuseStaging,
    [string] $OfflinePackageDir = "",
    [string] $StagingDir        = "",
    [string] $InnoPath          = ""
)

$ErrorActionPreference = "Stop"
$ProgressPreference    = "SilentlyContinue"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$null = & chcp 65001 2>&1

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $OfflinePackageDir) {
    $OfflinePackageDir = Join-Path $ScriptDir "dist\SpeechReco-Offline"
}
if (-not $StagingDir) {
    $StagingDir = Join-Path $ScriptDir "dist\SpeechReco-InnoStaging"
}

function Write-Step { param($n, $msg) Write-Host "`n[$n] $msg" -ForegroundColor Cyan }
function Write-Ok   { param($msg) Write-Host "  [OK] $msg"   -ForegroundColor Green }
function Write-Fail { param($msg) Write-Host "  [XX] $msg"   -ForegroundColor Red; exit 1 }

# ── Wizard bitmaps (ASCII status strings: UTF-8 no-BOM breaks PS 5.1 parsing) ─
Write-Step "1/3" "Generate Inno wizard bitmaps (installer_assets)"
$gen = Join-Path $ScriptDir "installer_assets\generate_wizard_images.ps1"
if (-not (Test-Path $gen)) { Write-Fail "Missing: $gen" }
& $gen -OutDir (Join-Path $ScriptDir "installer_assets")
Write-Ok "wizard-large.bmp / wizard-small.bmp"

# ── Stage service bundle ───────────────────────────────────────────────────
if (-not $ReuseStaging) {
    Write-Step "2/3" "Stage service bundle (nssm, start_svc, scripts)"
    $bsi = Join-Path $ScriptDir "build_service_installer.ps1"
    if (-not (Test-Path $bsi)) { Write-Fail "Missing: $bsi" }
    & $bsi -OfflinePackageDir $OfflinePackageDir -StagingDir $StagingDir -SkipZip -SkipSetupExe
    if (-not $?) { Write-Fail "build_service_installer.ps1 failed." }
    Write-Ok $StagingDir
} else {
    Write-Step "2/3" "Reuse existing staging (-ReuseStaging)"
    if (-not (Test-Path $StagingDir)) { Write-Fail "Staging dir missing: $StagingDir" }
    $need = @("nssm.exe", "install_service.ps1", "start_svc.bat", "python\python.exe")
    foreach ($rel in $need) {
        if (-not (Test-Path (Join-Path $StagingDir $rel))) {
            Write-Fail "Staging incomplete, missing: $rel"
        }
    }
    Write-Ok ("Using staging: " + $StagingDir)
}

# ── Inno Setup ───────────────────────────────────────────────────────────────
Write-Step "3/3" "Compile Inno Setup"
$iscc = $InnoPath
if (-not $iscc) {
    $candidates = @(
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
    )
    foreach ($c in $candidates) {
        if (Test-Path $c) { $iscc = $c; break }
    }
}
if ([string]::IsNullOrWhiteSpace($iscc) -or -not (Test-Path -LiteralPath $iscc)) {
    Write-Fail "ISCC.exe not found. Install Inno Setup 6 from https://jrsoftware.org/isdl.php or pass -InnoPath."
}

$iss  = Join-Path $ScriptDir "installer_whisper.iss"
if (-not (Test-Path -LiteralPath $StagingDir)) {
    Write-Fail ("Staging dir not found for ISCC: " + $StagingDir)
}
$stagingAbs = (Resolve-Path -LiteralPath $StagingDir).Path
& $iscc "/DStagingDir=$stagingAbs" $iss
if (-not $?) { Write-Fail ("ISCC failed with exit " + $LASTEXITCODE) }

$out = Join-Path $ScriptDir "dist\SpeechReco-Whisper-Setup.exe"
if (Test-Path -LiteralPath $out) {
    $mb = [math]::Round((Get-Item -LiteralPath $out).Length / 1MB, 1)
    Write-Host ""
    Write-Host ('  OK: ' + $out + ' (' + $mb.ToString() + ' MB)') -ForegroundColor Green
} else {
    Write-Fail ('Output missing: ' + $out)
}
