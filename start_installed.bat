@echo off
:: Speech Recognition Service - Installed launcher (uses bundled Python)

chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

title Speech Recognition Service

cd /d "%~dp0"

:: Verify bundled Python
if not exist "%~dp0python\python.exe" (
    echo.
    echo  [ERROR] Bundled Python runtime not found.
    echo  Please reinstall the application.
    echo.
    pause
    exit /b 1
)

:: Verify ASR model
"%~dp0python\python.exe" -c ^
  "import json,pathlib,sys; c=json.loads(pathlib.Path('config.json').read_text(encoding='utf-8')); p=c['asr']['model_path']; sys.exit(0 if pathlib.Path(p).exists() else 1)" ^
  2>nul
if errorlevel 1 (
    echo.
    echo  [WARN] ASR model not found.
    echo  Re-run the installer from the Start Menu to download models, or:
    echo    "%~dp0python\python.exe" download_model.py cn
    echo.
    pause
    exit /b 1
)

:: Show GPU acceleration info
"%~dp0python\python.exe" -c ^
  "import onnxruntime as o; p=[x for x in o.get_available_providers() if x!='CPUExecutionProvider']; print('  GPU: '+', '.join(p) if p else '  GPU: CPU only')" ^
  2>nul

echo.
echo  ================================================
echo    Speech Recognition WebSocket Service
echo    Open index.html to use the control panel
echo    Press Ctrl+C to stop
echo  ================================================
echo.

"%~dp0python\python.exe" server.py

echo.
echo  Service stopped.
pause
