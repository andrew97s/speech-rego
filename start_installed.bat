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

:: Check ASR model; if missing, offer auto-download
:check_model
"%~dp0python\python.exe" -c ^
  "import json,pathlib,sys; app=pathlib.Path(sys.executable).parent.parent; c=json.loads((app/'config.json').read_text(encoding='utf-8')); p=c['asr']['model_path']; sys.exit(0 if (app/p).exists() else 1)" ^
  2>nul

if errorlevel 1 (
    echo.
    echo  [WARN] ASR model not found.
    echo.
    echo  Options:
    echo    1 - Download Chinese model now  (~42 MB)
    echo    2 - Download English model now  (~40 MB)
    echo    Q - Quit
    echo.
    choice /C 12Q /N /M "  Choice [1/2/Q]: "
    if errorlevel 3 exit /b 0
    if errorlevel 2 (
        echo.
        echo  Downloading English model...
        "%~dp0python\python.exe" "%~dp0download_model.py" en
        if errorlevel 1 (
            echo.
            echo  [ERROR] Download failed. Check your internet connection and try again.
            pause
            exit /b 1
        )
        goto check_model
    )
    if errorlevel 1 (
        echo.
        echo  Downloading Chinese model...
        "%~dp0python\python.exe" "%~dp0download_model.py" cn
        if errorlevel 1 (
            echo.
            echo  [ERROR] Download failed. Check your internet connection and try again.
            pause
            exit /b 1
        )
        goto check_model
    )
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
