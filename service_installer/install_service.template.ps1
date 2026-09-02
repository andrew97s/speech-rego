#Requires -RunAsAdministrator
# 将本 ZIP 解压到目标目录后，在本目录以管理员运行此脚本。
$ErrorActionPreference = "Stop"
# nssm 常把正常提示写到 stderr；在 Stop 下会变成 RemoteException（消息可能只剩乱码一个字母）。下面所有 nssm 调用均丢弃 stderr。
$Root = $PSScriptRoot
$ServiceName = '@@SERVICENAME@@'
$DisplayName = '@@DISPLAYNAME@@'
$Nssm = Join-Path $Root "nssm.exe"
$PyExe = Join-Path $Root "python\python.exe"
$Logs = Join-Path $Root "logs"
$MsCache = Join-Path $Root "models\funasr"

if (-not (Test-Path $Nssm))  { throw "缺少 nssm.exe: $Nssm" }
if (-not (Test-Path $PyExe)) { throw "缺少 python.exe: $PyExe" }

# 从网络 ZIP 解压的文件可能带 Web 标记，执行 nssm 会被系统拦截为「访问被拒绝」
Unblock-File -LiteralPath $Nssm -ErrorAction SilentlyContinue
Unblock-File -LiteralPath $PyExe -ErrorAction SilentlyContinue

New-Item -ItemType Directory -Path $Logs -Force | Out-Null
$stdout = Join-Path $Logs "service_stdout.log"
$stderr = Join-Path $Logs "service_stderr.log"

$existing = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "停止并移除已有服务: $ServiceName"
    & $Nssm stop $ServiceName 2>$null
    Start-Sleep -Seconds 2
    & $Nssm remove $ServiceName confirm 2>$null
    Start-Sleep -Seconds 1
}

# 直接跑 python.exe + AppDirectory，避免 cmd /c "C:\Program Files\..." 被空格拆开。
Write-Host "安装服务: $ServiceName"
& $Nssm install $ServiceName $PyExe 2>$null
& $Nssm set $ServiceName AppParameters "server.py" 2>$null
& $Nssm set $ServiceName AppDirectory $Root 2>$null
& $Nssm set $ServiceName DisplayName $DisplayName 2>$null
& $Nssm set $ServiceName Description "语音识别客户端（唤醒 + 远程 FunASR）；Web 9400 / WebSocket 8766。由 NSSM 托管，退出后自动重启。工作目录: $Root" 2>$null
& $Nssm set $ServiceName Start SERVICE_AUTO_START 2>$null
$envExtra = @(
    "PYTHONIOENCODING=utf-8"
    "PYTHONUTF8=1"
    "MODELSCOPE_CACHE=$MsCache"
    "MODELSCOPE_MODULES_CACHE=$MsCache"
) -join "`n"
& $Nssm set $ServiceName AppEnvironmentExtra $envExtra 2>$null
& $Nssm set $ServiceName AppStdout $stdout 2>$null
& $Nssm set $ServiceName AppStderr $stderr 2>$null
& $Nssm set $ServiceName AppStdoutCreationDisposition 4 2>$null
& $Nssm set $ServiceName AppStderrCreationDisposition 4 2>$null
& $Nssm set $ServiceName AppRotateFiles 1 2>$null
& $Nssm set $ServiceName AppRotateBytes 1048576 2>$null
& $Nssm set $ServiceName AppExit Default Restart 2>$null
& $Nssm set $ServiceName AppRestartDelay 5000 2>$null
& $Nssm set $ServiceName AppThrottle 15000 2>$null

Write-Host "启动服务..."
& $Nssm start $ServiceName 2>$null
Start-Sleep -Seconds 2
Get-Service -Name $ServiceName
Write-Host "`n完成。日志目录: $Logs"
