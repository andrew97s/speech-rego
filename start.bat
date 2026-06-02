@echo off
:: Speech Recognition Service - Start Script (dev venv)

chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

title Speech Recognition Service

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo.
    echo  [ERROR] Virtual environment not found.
    echo  Please run install.bat first.
    echo.
    pause
    exit /b 1
)

.venv\Scripts\python.exe -c ^
  "import onnxruntime as o; p=[x for x in o.get_available_providers() if x!='CPUExecutionProvider']; print('  GPU: '+', '.join(p) if p else '  GPU: CPU only')" ^
  2>nul

echo.
echo  ================================================
echo    Speech Recognition WebSocket Service
echo    Press Ctrl+C to stop
echo  ================================================
echo.

echo y | .venv\Scripts\python.exe server.py

echo.
echo  Service stopped.
pause
