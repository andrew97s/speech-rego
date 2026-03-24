@echo off
:: One-click installer launcher
:: Double-click this file to start installation

title Speech Recognition Service - Installer

echo.
echo Starting installer...
echo If prompted by User Account Control, click Yes.
echo.

PowerShell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"

echo.
pause
