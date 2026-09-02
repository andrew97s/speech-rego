#Requires -RunAsAdministrator
$ErrorActionPreference = "Stop"
# nssm 常把提示写到 stderr；Stop 下会变成 RemoteException，故丢弃 stderr。
$Root = $PSScriptRoot
$ServiceName = '@@SERVICENAME@@'
$Nssm = Join-Path $Root "nssm.exe"
if (-not (Test-Path $Nssm)) { throw "缺少 nssm.exe" }
Unblock-File -LiteralPath $Nssm -ErrorAction SilentlyContinue
Write-Host "停止服务: $ServiceName"
& $Nssm stop $ServiceName 2>$null
Start-Sleep -Seconds 2
Write-Host "移除服务注册..."
& $Nssm remove $ServiceName confirm 2>$null
Write-Host "完成（安装文件仍在: $Root，可自行删除目录）。"
