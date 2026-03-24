@echo off
:: Speech Recognition Service - Installer Launcher

chcp 65001 >nul

title Speech Recognition Service - Installer

:: Require x64 OS
if /i "%PROCESSOR_ARCHITECTURE%"=="x86" (
    if "%PROCESSOR_ARCHITEW6432%"=="" (
        echo.
        echo  [ERROR] 32-bit Windows is not supported.
        echo  Please use 64-bit Windows 10 or 11.
        echo.
        pause
        exit /b 1
    )
)

:: Elevate to Administrator (needed for VC++ redist silent install)
net session >nul 2>&1
if %errorLevel% neq 0 (
    echo.
    echo  Requesting administrator privileges...
    echo.
    powershell -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

:: Run the PowerShell installer
PowerShell -NoProfile -ExecutionPolicy Bypass -InputFormat Text -File "%~dp0install.ps1"

echo.
pause
