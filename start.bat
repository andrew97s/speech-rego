@echo off
:: Speech Recognition Service - Start Script (dev venv)

chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

title Speech Client (wake + remote ASR)

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
  "import torch; print('  GPU: '+torch.cuda.get_device_name(0) if torch.cuda.is_available() else '  GPU: CPU only')" ^
  2>nul

echo.
echo  ================================================
echo    Windows wake client  (remote FunASR)
echo    Press Ctrl+C to stop
echo  ================================================
echo.

echo y | .venv\Scripts\python.exe server.py

echo.
echo  Service stopped.
pause
