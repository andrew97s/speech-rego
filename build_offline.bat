@echo off
:: 离线部署包构建器启动脚本
:: Offline Deployment Builder Launcher
::
:: 用法 / Usage:
::   build_offline.bat
::   build_offline.bat small none
::   build_offline.bat small cuda
::
::   参数1: Whisper 模型大小  tiny|base|small(默认)|medium|large-v3
::   参数2: GPU 模式          none(默认)|cuda|dml|auto
::   cuda 时会默认打包 nvidia-cublas 等到 python\Lib\site-packages\nvidia\（约 800MB）
::
::   不打包 NVIDIA 库（目标机自装 CUDA）:
::   PowerShell -File build_offline.ps1 -WhisperModel small -GPU cuda -BundleNvidiaCuda:$false

chcp 65001 >nul
setlocal

set MODEL=%~1
set GPU=%~2
if "%MODEL%"=="" set MODEL=small
if "%GPU%"==""   set GPU=auto

title 离线部署包构建 - Whisper:%MODEL% GPU:%GPU%

echo.
echo  离线部署包构建器 / Offline Deployment Builder
echo  Whisper 模型: %MODEL%   GPU 模式: %GPU%
if /i "%GPU%"=="cuda" echo  NVIDIA 运行库: 默认打包进离线包
echo.

:: Ensure UTF-8 BOM so PowerShell 5.1 parses Chinese Windows correctly
PowerShell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\ensure_utf8_bom.ps1" -Path "%~dp0build_offline.ps1"
REM cuda 时由 build_offline.ps1 自动启用 BundleNvidiaCuda（cmd 无法传 $true）
PowerShell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_offline.ps1" ^
    -WhisperModel "%MODEL%" -GPU "%GPU%"

echo.
pause
