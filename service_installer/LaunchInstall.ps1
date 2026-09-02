#Requires -Version 5.1
# 与离线包同目录：双击或由 InstallService.bat 调用；也可编译为 SpeechRecoServiceSetup.exe。
$ErrorActionPreference = "Stop"

function Get-InstallRoot {
    $main = [System.Diagnostics.Process]::GetCurrentProcess().MainModule.FileName
    if ($main -match '(?i)\\(powershell|pwsh)\.exe$') {
        return $PSScriptRoot
    }
    return [System.IO.Path]::GetDirectoryName($main)
}

function Test-IsAdmin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $p  = [Security.Principal.WindowsPrincipal]$id
    return $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

$root = Get-InstallRoot
$installPs1 = Join-Path $root "install_service.ps1"
if (-not (Test-Path $installPs1)) {
    Write-Host "未找到 install_service.ps1，请把本文件放在离线包根目录（与 nssm.exe 同级）。" -ForegroundColor Red
    Read-Host "按 Enter 退出"
    exit 1
}

if (-not (Test-IsAdmin)) {
    $hostExe = [System.Diagnostics.Process]::GetCurrentProcess().MainModule.FileName
    if ($hostExe -match '(?i)\\(powershell|pwsh)\.exe$') {
        $ps1 = if ($PSCommandPath) { $PSCommandPath } else { $MyInvocation.MyCommand.Path }
        Start-Process -FilePath $hostExe -Verb RunAs -WorkingDirectory $root `
            -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $ps1)
    } else {
        Start-Process -LiteralPath $hostExe -Verb RunAs -WorkingDirectory $root
    }
    exit 0
}

Set-Location -LiteralPath $root
& $installPs1
Write-Host ""
Read-Host "安装流程已结束，按 Enter 关闭窗口"
