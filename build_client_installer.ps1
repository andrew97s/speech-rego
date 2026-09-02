#Requires -Version 5.1
<#
.SYNOPSIS
    构建 Windows 客户端安装包（Inno Setup）：后台服务 + Web 9400 + WebSocket 8766。

.DESCRIPTION
    1. build_offline.ps1     嵌入式 Python + 客户端源码 + KWS/VAD 模型
    2. build_service_installer.ps1  NSSM + start_svc.bat + 安装脚本
    3. installer_client.iss  -> dist\SpeechReco-Client-Setup.exe

.PARAMETER SkipOffline
    跳过离线包构建，直接使用已有 dist\SpeechReco-Offline。

.PARAMETER ReuseStaging
    跳过服务暂存，直接用已有 StagingDir 编译 Inno。

.EXAMPLE
    .\build_client_installer.ps1
    .\build_client_installer.ps1 -SkipOffline
#>

param(
    [switch] $SkipOffline,
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

Write-Step "1/4" "Generate Inno wizard bitmaps"
$gen = Join-Path $ScriptDir "installer_assets\generate_wizard_images.ps1"
if (-not (Test-Path $gen)) { Write-Fail "Missing: $gen" }
& $gen -OutDir (Join-Path $ScriptDir "installer_assets")
Write-Ok "wizard-large.bmp / wizard-small.bmp"

if (-not $SkipOffline -and -not $ReuseStaging) {
    Write-Step "2/4" "Build offline client package (Python + models)"
    $bo = Join-Path $ScriptDir "build_offline.ps1"
    if (-not (Test-Path $bo)) { Write-Fail "Missing: $bo" }
    & $bo -Force -GPU none -OutputDir $OfflinePackageDir
    if (-not $?) { Write-Fail "build_offline.ps1 failed." }
    Write-Ok $OfflinePackageDir
} else {
    Write-Step "2/4" "Skip offline rebuild"
    if (-not (Test-Path (Join-Path $OfflinePackageDir "server.py"))) {
        Write-Fail "Offline package missing: $OfflinePackageDir"
    }
    Write-Ok $OfflinePackageDir
}

if (-not $ReuseStaging) {
    Write-Step "3/4" "Stage Windows service bundle (NSSM)"
    $bsi = Join-Path $ScriptDir "build_service_installer.ps1"
    if (-not (Test-Path $bsi)) { Write-Fail "Missing: $bsi" }
    & $bsi -OfflinePackageDir $OfflinePackageDir -StagingDir $StagingDir `
        -ServiceName "SpeechRecoClient" -DisplayName "语音识别客户端" `
        -SkipZip -SkipSetupExe
    if (-not $?) { Write-Fail "build_service_installer.ps1 failed." }
    Write-Ok $StagingDir
} else {
    Write-Step "3/4" "Reuse existing staging"
    if (-not (Test-Path (Join-Path $StagingDir "nssm.exe"))) {
        Write-Fail "Staging incomplete: $StagingDir"
    }
    Write-Ok $StagingDir
}

Write-Step "4/4" "Compile Inno Setup"
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

$iss = Join-Path $ScriptDir "installer_client.iss"
$stagingAbs = (Resolve-Path -LiteralPath $StagingDir).Path
& $iscc "/DStagingDir=$stagingAbs" "/DServiceName=SpeechRecoClient" $iss
if (-not $?) { Write-Fail ("ISCC failed with exit " + $LASTEXITCODE) }

$out = Join-Path $ScriptDir "dist\SpeechReco-Client-Setup.exe"
if (Test-Path -LiteralPath $out) {
    $mb = [math]::Round((Get-Item -LiteralPath $out).Length / 1MB, 1)
    Write-Host ""
    Write-Host ('  OK: ' + $out + ' (' + $mb.ToString() + ' MB)') -ForegroundColor Green
    Write-Host "  Web UI     : http://127.0.0.1:9400/index.html" -ForegroundColor Green
    Write-Host "  WebSocket  : ws://127.0.0.1:8766" -ForegroundColor Green
} else {
    Write-Fail ('Output missing: ' + $out)
}
