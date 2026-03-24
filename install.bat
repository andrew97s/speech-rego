@echo off
:: ─────────────────────────────────────────────────────────────────
::  语音识别服务 — 安装启动器
::  Speech Recognition Service — Installer Launcher
::
::  Double-click this file to install.
::  管理员权限用于安装 VC++ 运行时（如未安装）。
:: ─────────────────────────────────────────────────────────────────

title Speech Recognition Service — Installer

:: ── Quick sanity: make sure we are on x64 ─────────────────────────
if /i "%PROCESSOR_ARCHITECTURE%" == "x86" (
    if "%PROCESSOR_ARCHITEW6432%" == "" (
        echo.
        echo  [ERROR] 32-bit Windows is not supported.
        echo          Please use a 64-bit version of Windows 10/11.
        echo.
        pause
        exit /b 1
    )
)

:: ── Elevate to Administrator if needed ───────────────────────────
:: (required for VC++ redist silent install)
net session >nul 2>&1
if %errorLevel% neq 0 (
    echo.
    echo  Requesting administrator privileges...
    echo  如弹出 UAC 对话框，请点击"是"。
    echo.
    powershell -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

:: ── Run the PowerShell installer ─────────────────────────────────
PowerShell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"

echo.
pause
