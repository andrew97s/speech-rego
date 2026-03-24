@echo off
title Speech Recognition WebSocket Service

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo.
    echo  [ERROR] Virtual environment not found.
    echo  Please run install.bat first.
    echo.
    pause
    exit /b 1
)

echo.
echo  ================================================
echo   Speech Recognition WebSocket Service
echo  ================================================
echo   Press Ctrl+C to stop
echo  ================================================
echo.

.venv\Scripts\python.exe server.py

echo.
echo  Service stopped.
pause
