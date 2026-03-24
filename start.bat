@echo off
:: Speech Recognition Service - Start Script (dev venv)

chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

title Speech Recognition Service

cd /d "%~dp0"

:: Check virtual environment
if not exist ".venv\Scripts\python.exe" (
    echo.
    echo  [ERROR] Virtual environment not found.
    echo  Please run install.bat first.
    echo.
    pause
    exit /b 1
)

:: Check ASR model
.venv\Scripts\python.exe -c ^
  "import json,pathlib,sys; c=json.loads(pathlib.Path('config.json').read_text(encoding='utf-8')); p=c['asr']['model_path']; sys.exit(0 if pathlib.Path(p).exists() else 1)" ^
  2>nul
if errorlevel 1 (
    echo.
    echo  [WARN] ASR model not found.
    echo  Run install.bat to download models, or check model_path in config.json.
    echo.
    pause
    exit /b 1
)

:: Show GPU acceleration info
.venv\Scripts\python.exe -c ^
  "import onnxruntime as o; p=[x for x in o.get_available_providers() if x!='CPUExecutionProvider']; print('  GPU: '+', '.join(p) if p else '  GPU: CPU only')" ^
  2>nul

echo.
echo  ================================================
echo    Speech Recognition WebSocket Service
echo    Press Ctrl+C to stop
echo  ================================================
echo.

.venv\Scripts\python.exe server.py

echo.
echo  Service stopped.
pause
