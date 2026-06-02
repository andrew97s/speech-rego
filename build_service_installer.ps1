#Requires -Version 5.1
<#
.SYNOPSIS
    将 build_offline.ps1 产出的离线包打成「Windows 服务安装包」ZIP。

    本脚本须以 UTF-8 带 BOM 保存；否则在中文 Windows 上 powershell -File 可能按系统编码误读并报语法错。

.DESCRIPTION
    ZIP 内容与离线包一致，并额外包含：
      - nssm.exe（Non-Sucking Service Manager，用于注册 Windows 服务）
      - start_svc.bat（由包内 start.bat 去掉 pause 等交互行生成，供服务无人值守运行）
      - install_service.ps1 / uninstall_service.ps1（管理员运行）
      - SERVICE_README.txt

    安装后服务会执行 start_svc.bat（与 start.bat 相同环境与启动逻辑）。
    NSSM 配置为进程退出后自动重启（AppExit Default Restart），实现持续保障。

.PARAMETER OfflinePackageDir
    build_offline 输出目录（默认 .\dist\SpeechReco-Offline）

.PARAMETER OutputZip
    生成的 ZIP 路径（默认 .\dist\SpeechReco-Offline-WindowsService.zip）

.PARAMETER ServiceName
    Windows 服务短名称（默认 SpeechRecoWhisper）

.PARAMETER DisplayName
    服务显示名（默认 语音识别服务 (Whisper)）

.PARAMETER NssmZipUrl
    nssm 发布包下载地址（需可访问网络；离线环境请先用 -NssmZipPath）

.PARAMETER NssmZipPath
    本地 nssm-2.24.zip 路径；若已指定则不会下载

.PARAMETER SkipZip
    若指定，仅输出到 -StagingDir 目录而不打 ZIP

.PARAMETER StagingDir
    SkipZip 或调试时的暂存目录（默认 .\dist\SpeechReco-ServiceStaging）

.PARAMETER SkipSetupExe
    不尝试用 ps2exe 编译 SpeechRecoServiceSetup.exe / SpeechRecoWebSetup.exe（仍会生成 InstallService.bat + LaunchInstall.ps1 + WebSetup.ps1 等）。

.PARAMETER PublicPackageUrl
    若填写公网 ZIP 直链（https），将写入 SpeechRecoWebSetup.exe 内作默认下载地址；仍可被同目录 PackageUrl.txt 覆盖。
#>

param(
    [string] $OfflinePackageDir = "",
    [string] $OutputZip         = "",
    [string] $ServiceName       = "SpeechRecoWhisper",
    [string] $DisplayName       = "语音识别服务 (Whisper)",
    [string] $NssmZipUrl        = "https://nssm.cc/release/nssm-2.24.zip",
    [string] $NssmZipPath       = "",
    [switch] $SkipZip,
    [string] $StagingDir        = "",
    [switch] $SkipSetupExe,
    [string] $PublicPackageUrl  = ""
)

$tryCompileSetupExe = -not $SkipSetupExe.IsPresent

$ErrorActionPreference = "Stop"
$ProgressPreference    = "SilentlyContinue"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$null = & chcp 65001 2>&1

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $OfflinePackageDir) {
    $OfflinePackageDir = Join-Path $ScriptDir "dist\SpeechReco-Offline"
}
if (-not $OutputZip) {
    $OutputZip = Join-Path $ScriptDir "dist\SpeechReco-Offline-WindowsService.zip"
}
if (-not $StagingDir) {
    $StagingDir = Join-Path $ScriptDir "dist\SpeechReco-ServiceStaging"
}

function Write-Step { param($msg) Write-Host "`n$msg" -ForegroundColor Cyan }
function Write-Ok   { param($msg) Write-Host "  [OK] $msg"   -ForegroundColor Green }
function Write-Warn { param($msg) Write-Host " [!!] $msg"    -ForegroundColor Yellow }
function Write-Fail { param($msg) Write-Host "  [XX] $msg"   -ForegroundColor Red; exit 1 }

# ── Validate offline package ─────────────────────────────────────────────────
Write-Step "[1/7] 检查离线包目录"
if (-not (Test-Path $OfflinePackageDir)) {
    Write-Fail "找不到离线包: $OfflinePackageDir`n请先运行 build_offline.ps1"
}
$need = @("python\python.exe", "server.py", "start.bat")
foreach ($rel in $need) {
    $p = Join-Path $OfflinePackageDir $rel
    if (-not (Test-Path $p)) { Write-Fail "离线包不完整，缺少: $rel" }
}
Write-Ok "离线包就绪: $OfflinePackageDir"

# ── Obtain nssm.exe ────────────────────────────────────────────────────────
Write-Step "[2/7] 准备 nssm.exe"
$cacheDir = Join-Path $ScriptDir ".offline-cache"
$null = New-Item -ItemType Directory -Path $cacheDir -Force
$nssmZipCache = Join-Path $cacheDir "nssm-2.24.zip"
$nssmExeSrc  = $null

if ($NssmZipPath) {
    if (-not (Test-Path $NssmZipPath)) { Write-Fail "找不到本地 NSSM ZIP: $NssmZipPath" }
    $zipLocal = $NssmZipPath
} else {
    $zipLocal = $nssmZipCache
    if (-not (Test-Path $zipLocal)) {
        Write-Host "      正在下载 NSSM ..." -ForegroundColor Gray
        try {
            Invoke-WebRequest -Uri $NssmZipUrl -OutFile $zipLocal -UseBasicParsing
        } catch {
            Write-Fail "下载 NSSM 失败: $_`n可手动下载后使用 -NssmZipPath 指定 zip 路径。"
        }
    }
}

$extractTmp = Join-Path $cacheDir "nssm-extract-tmp"
if (Test-Path $extractTmp) { Remove-Item $extractTmp -Recurse -Force }
Expand-Archive -LiteralPath $zipLocal -DestinationPath $extractTmp -Force
$candidate = Join-Path $extractTmp "nssm-2.24\win64\nssm.exe"
if (-not (Test-Path $candidate)) {
    $found = Get-ChildItem -Path $extractTmp -Filter "nssm.exe" -Recurse -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -match '\\win64\\' } | Select-Object -First 1
    if ($found) { $candidate = $found.FullName }
}
if (-not (Test-Path $candidate)) {
    Write-Fail "在 NSSM ZIP 中未找到 win64\nssm.exe"
}
$nssmExeSrc = $candidate
Write-Ok "nssm: $nssmExeSrc"

# ── Stage tree ───────────────────────────────────────────────────────────────
Write-Step "[3/7] 复制离线包到暂存目录"
if (Test-Path $StagingDir) { Remove-Item $StagingDir -Recurse -Force }
$null = New-Item -ItemType Directory -Path $StagingDir -Force
robocopy $OfflinePackageDir $StagingDir /MIR /NFL /NDL /NJH /NJS /NC /NS /NP | Out-Null
if ($LASTEXITCODE -ge 8) { Write-Fail "robocopy 失败，退出码: $LASTEXITCODE" }
Write-Ok "已镜像到: $StagingDir"

# ── start_svc.bat (strip interactive tail from start.bat) ─────────────────────
Write-Step "[4/7] 生成 start_svc.bat"
$batPath = Join-Path $StagingDir "start.bat"
$lines   = Get-Content -LiteralPath $batPath -Encoding UTF8
$outLines = foreach ($line in $lines) {
    $t = $line.Trim()
    if ($t -eq "pause") { continue }
    if ($t -match '^echo\s+服务已停止') { continue }
    # 服务在 Session 0 运行：title 无控制台易异常；echo y| 管道在无人值守下不必要
    if ($t -match '^(?i)title\s') { continue }
    # 含空格安装路径：必须用 "%~dp0python\python.exe" 形式；兼容旧 bat（无引号）与新 bat（已带引号）
    if ($t -match '^(?i)echo\s+y\s+\|\s+(?:"%~dp0python\\python\.exe"\s+"%~dp0server\.py"|python\\python\.exe\s+server\.py)$') {
        '"%~dp0python\python.exe" "%~dp0server.py"'
        continue
    }
    $line
}
$svcBat = Join-Path $StagingDir "start_svc.bat"
$outLines | Set-Content -LiteralPath $svcBat -Encoding UTF8
Write-Ok "已写入: start_svc.bat"

Copy-Item -LiteralPath $nssmExeSrc -Destination (Join-Path $StagingDir "nssm.exe") -Force
Write-Ok "已复制 nssm.exe 到包根目录"
Remove-Item $extractTmp -Recurse -Force -ErrorAction SilentlyContinue

# ── Installer scripts from templates (avoids giant here-strings / parser issues) ─
$tplDir      = Join-Path $ScriptDir "service_installer"
$installTpl  = Join-Path $tplDir "install_service.template.ps1"
$uninstallTpl = Join-Path $tplDir "uninstall_service.template.ps1"
$readmeTpl   = Join-Path $tplDir "SERVICE_README.template.txt"
$launchTpl   = Join-Path $tplDir "LaunchInstall.ps1"
$webSetupTpl = Join-Path $tplDir "WebSetup.template.ps1"
$urlHintTpl  = Join-Path $tplDir "PackageUrl.txt.example"
foreach ($p in @($installTpl, $uninstallTpl, $readmeTpl, $launchTpl, $webSetupTpl, $urlHintTpl)) {
    if (-not (Test-Path $p)) { Write-Fail "缺少模板文件: $p" }
}

$sq = { param($s) $s.Replace("'", "''") }
$svcNameEsc  = & $sq $ServiceName
$dispNameEsc = & $sq $DisplayName

$installPs1 = (Get-Content -LiteralPath $installTpl -Raw -Encoding UTF8).
    Replace("@@SERVICENAME@@", $svcNameEsc).
    Replace("@@DISPLAYNAME@@", $dispNameEsc)

$uninstallPs1 = (Get-Content -LiteralPath $uninstallTpl -Raw -Encoding UTF8).
    Replace("@@SERVICENAME@@", $svcNameEsc)

$readme = (Get-Content -LiteralPath $readmeTpl -Raw -Encoding UTF8).
    Replace("@@SERVICENAME@@", $ServiceName)

Set-Content -Path (Join-Path $StagingDir "install_service.ps1")   -Value $installPs1   -Encoding UTF8
Set-Content -Path (Join-Path $StagingDir "uninstall_service.ps1") -Value $uninstallPs1 -Encoding UTF8
Set-Content -Path (Join-Path $StagingDir "SERVICE_README.txt")   -Value $readme      -Encoding UTF8
Write-Ok "已写入 install_service.ps1 / uninstall_service.ps1 / SERVICE_README.txt"

# ── 双击安装：LaunchInstall.ps1 + InstallService.bat + WebSetup.ps1 + 公网示例 ─
Write-Step "[5/7] 生成本地/公网安装入口（bat、LaunchInstall、WebSetup.ps1）"
$launchDst = Join-Path $StagingDir "LaunchInstall.ps1"
Copy-Item -LiteralPath $launchTpl -Destination $launchDst -Force
$utf8BomEnc = New-Object System.Text.UTF8Encoding $true
[System.IO.File]::WriteAllText($launchDst, (Get-Content -LiteralPath $launchDst -Raw -Encoding UTF8), $utf8BomEnc)

# Bat content: string array + join (no @" "@ here-strings; keep this file UTF-8 with BOM for -File).
$batInstall = @(
    '@echo off'
    'chcp 65001 >nul'
    'cd /d "%~dp0"'
    'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0LaunchInstall.ps1"'
    'if errorlevel 1 pause'
) -join "`r`n"
Set-Content -Path (Join-Path $StagingDir "InstallService.bat") -Value $batInstall -Encoding ASCII

# 不用嵌套 " ' " 拼接，避免脚本被存成无 BOM UTF-8 时 -File 误码后报「字符串缺少终止符」
$sqc = [string][char]39
if ([string]::IsNullOrWhiteSpace($PublicPackageUrl)) {
    $embedTok = '$null'
} else {
    $u = $PublicPackageUrl.Trim()
    $embedTok = $sqc + ($u.Replace($sqc, $sqc + $sqc)) + $sqc
}
$webPsStaging = Join-Path $StagingDir "WebSetup.ps1"
$webBody = (Get-Content -LiteralPath $webSetupTpl -Raw -Encoding UTF8).
    Replace("@@EMBED@@", $embedTok).
    Replace("@@SERVICENAME@@", $svcNameEsc)
Set-Content -Path $webPsStaging -Value $webBody -Encoding UTF8
[System.IO.File]::WriteAllText($webPsStaging, (Get-Content -LiteralPath $webPsStaging -Raw -Encoding UTF8), $utf8BomEnc)

Copy-Item -LiteralPath $urlHintTpl -Destination (Join-Path $StagingDir "PackageUrl.txt.example") -Force
Write-Ok "已写入 LaunchInstall.ps1 / InstallService.bat / WebSetup.ps1 / PackageUrl.txt.example"

$setupExeName     = "SpeechRecoServiceSetup.exe"
$webSetupExeName  = "SpeechRecoWebSetup.exe"
$setupExeStaging     = Join-Path $StagingDir $setupExeName
$webSetupExeStaging  = Join-Path $StagingDir $webSetupExeName

Write-Step "[6/7] 用 ps2exe 编译 Setup.exe（本地安装 + 公网下载安装）"
if ($tryCompileSetupExe) {
    $ps2 = Get-Module -ListAvailable -Name ps2exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($ps2) {
        try {
            Import-Module ps2exe -Force -ErrorAction Stop
            if (-not (Get-Command Invoke-ps2exe -ErrorAction SilentlyContinue)) {
                throw "ps2exe 模块中未找到 Invoke-ps2exe"
            }
            if (Test-Path $setupExeStaging)    { Remove-Item $setupExeStaging -Force }
            if (Test-Path $webSetupExeStaging) { Remove-Item $webSetupExeStaging -Force }
            Invoke-ps2exe -inputFile $launchDst -outputFile $setupExeStaging `
                -title "Speech Reco 服务安装（本地）" -requireAdmin -ErrorAction Stop
            Write-Ok "已生成 $setupExeName"
            Invoke-ps2exe -inputFile $webPsStaging -outputFile $webSetupExeStaging `
                -title "Speech Reco 服务安装（下载公网包）" -requireAdmin -noConsole -STA -ErrorAction Stop
            Write-Ok "已生成 $webSetupExeName（双击从公网下载 ZIP 并安装服务）"
        } catch {
            Write-Warn "编译 Setup.exe 失败（仍可使用 InstallService.bat / WebSetup.ps1）：$_"
        }
    } else {
        Write-Warn "未安装 ps2exe 模块，已跳过 exe。构建机执行: Install-Module ps2exe -Scope CurrentUser -Force 后重跑本脚本。"
    }
} elseif ($SkipSetupExe) {
    Write-Host "      已跳过 exe 编译（-SkipSetupExe）" -ForegroundColor DarkGray
}

# ── ZIP ──────────────────────────────────────────────────────────────────────
if (-not $SkipZip) {
    Write-Step "[7/7] 压缩为 ZIP"
    $zipParent = Split-Path -Parent $OutputZip
    if ($zipParent -and -not (Test-Path $zipParent)) {
        New-Item -ItemType Directory -Path $zipParent -Force | Out-Null
    }
    if (Test-Path $OutputZip) { Remove-Item $OutputZip -Force }
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [System.IO.Compression.ZipFile]::CreateFromDirectory(
        $StagingDir,
        $OutputZip,
        [System.IO.Compression.CompressionLevel]::Optimal,
        $false
    )
    $mb = [math]::Round((Get-Item $OutputZip).Length / 1MB, 1)
    Write-Ok "ZIP 已生成: $OutputZip (${mb} MB)"
    $zipDir = Split-Path -Parent $OutputZip
    if (Test-Path $setupExeStaging) {
        $exeCopy = Join-Path $zipDir $setupExeName
        Copy-Item -LiteralPath $setupExeStaging -Destination $exeCopy -Force
        Write-Ok "已复制 $setupExeName 到: $exeCopy"
    }
    if (Test-Path $webSetupExeStaging) {
        $webCopy = Join-Path $zipDir $webSetupExeName
        Copy-Item -LiteralPath $webSetupExeStaging -Destination $webCopy -Force
        Write-Ok "已复制 $webSetupExeName 到: $webCopy（可单独分发：用户放 PackageUrl.txt 或你用 -PublicPackageUrl 构建）"
    }
} else {
    Write-Step "[7/7] 已跳过 ZIP（Staging: $StagingDir）"
}

Write-Host ""
Write-Host "  构建完成。" -ForegroundColor Green
Write-Host "  - 已解压场景: 双击 InstallService.bat 或 SpeechRecoServiceSetup.exe" -ForegroundColor Green
Write-Host "  - 公网场景: 上传本 ZIP 得直链后，分发 SpeechRecoWebSetup.exe + PackageUrl.txt（或构建时 -PublicPackageUrl）" -ForegroundColor Green
Write-Host ""
