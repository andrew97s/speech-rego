@echo off
chcp 65001 >nul
echo.
echo  Download and package Whisper large-v3 into offline dist
echo  This may take 20-60 minutes (~3GB)
echo.
cd /d "%~dp0"
call build_offline.bat large-v3 cuda
pause
