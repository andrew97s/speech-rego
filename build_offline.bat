@echo off
:: 离线部署包构建器启动脚本
:: Offline Deployment Builder Launcher
::
:: 用法 / Usage:
::   build_offline.bat
::   build_offline.bat small none
::   build_offline.bat base cuda
::
::   参数1: Whisper 模型大小  tiny|base|small(默认)|medium|large-v3
::   参数2: GPU 模式          none(默认)|cuda|dml
::
::   或者直接运行 PowerShell 脚本以获得更多选项:
::   PowerShell -File build_offline.ps1 -WhisperModel small -GPU none

chcp 65001 >nul
setlocal

set MODEL=%~1
set GPU=%~2
if "%MODEL%"=="" set MODEL=small
if "%GPU%"==""   set GPU=none

title 离线部署包构建 - Whisper:%MODEL% GPU:%GPU%

echo.
echo  离线部署包构建器 / Offline Deployment Builder
echo  Whisper 模型: %MODEL%   GPU 模式: %GPU%
echo.

PowerShell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_offline.ps1" ^
    -WhisperModel "%MODEL%" ^
    -GPU "%GPU%"

echo.
pause
