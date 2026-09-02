@echo off
:: 离线部署包构建器（Windows 客户端）
::
:: 用法:
::   build_offline.bat
::   PowerShell -File build_offline.ps1 -Force -GPU none

chcp 65001 >nul
setlocal

title 离线部署包构建 - Speech Client

echo.
echo  离线部署包构建器 / Offline Deployment Builder
echo  客户端: Sherpa KWS + fsmn-vad + 远程 FunASR
echo  端口: WebSocket 8766 / Web 9400
echo.

PowerShell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\ensure_utf8_bom.ps1" -Path "%~dp0build_offline.ps1"
PowerShell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_offline.ps1" -Force -GPU none

echo.
pause
