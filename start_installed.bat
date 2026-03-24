@echo off
:: ─────────────────────────────────────────────────────────────────────────────
::  语音识别服务 — 已安装版启动器（使用内置 Python）
::  Speech Recognition Service — Installed launcher (uses bundled Python)
:: ─────────────────────────────────────────────────────────────────────────────

chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

cd /d "%~dp0"

:: Verify bundled Python exists
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
    echo  [警告] 未找到 ASR 模型文件。
    echo  [WARN]  ASR model not found.
    echo.
    echo  请通过 "开始菜单 - 语音识别服务" 重新运行安装程序，
    echo  或手动下载模型:
    echo    %~dp0python\python.exe download_model.py cn
    echo.
    pause
    exit /b 1
)

:: Show GPU acceleration info
"%~dp0python\python.exe" -c ^
  "import onnxruntime as o; p=[x for x in o.get_available_providers() if x!='CPUExecutionProvider']; print('  GPU: '+', '.join(p) if p else '  GPU: CPU only')" ^
  2>nul

echo.
echo  ════════════════════════════════════════════════════
echo    语音识别 WebSocket 服务 / Speech Recognition Service
echo  ════════════════════════════════════════════════════
echo    打开 index.html 使用控制台界面
echo    Open index.html to use the control panel
echo    按 Ctrl+C 停止 / Press Ctrl+C to stop
echo  ════════════════════════════════════════════════════
echo.

"%~dp0python\python.exe" server.py

echo.
echo  服务已停止。/ Service stopped.
pause
