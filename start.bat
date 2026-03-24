@echo off
:: ─────────────────────────────────────────────────────────────────
::  语音识别服务 — 启动脚本
::  Speech Recognition Service — Start Script
:: ─────────────────────────────────────────────────────────────────

title 语音识别服务 / Speech Recognition Service

cd /d "%~dp0"

:: ── Check virtual environment ─────────────────────────────────────
if not exist ".venv\Scripts\python.exe" (
    echo.
    echo  [错误] 未找到虚拟环境 .venv
    echo  [ERROR] Virtual environment not found.
    echo.
    echo  请先运行 install.bat 完成安装。
    echo  Please run install.bat first.
    echo.
    pause
    exit /b 1
)

:: ── Check model ───────────────────────────────────────────────────
.venv\Scripts\python.exe -c ^
  "import json,pathlib,sys; c=json.loads(pathlib.Path('config.json').read_text(encoding='utf-8')); p=c['asr']['model_path']; sys.exit(0 if pathlib.Path(p).exists() else 1)" ^
  2>nul
if errorlevel 1 (
    echo.
    echo  [警告] 未找到 ASR 模型文件。
    echo  [WARN]  ASR model not found.
    echo.
    echo  请运行 install.bat 下载模型，或检查 config.json 中的 model_path。
    echo  Run install.bat to download models, or check model_path in config.json.
    echo.
    pause
    exit /b 1
)

:: ── Detect acceleration mode (informational) ─────────────────────
.venv\Scripts\python.exe -c ^
  "import onnxruntime as o; p=[x for x in o.get_available_providers() if x!='CPUExecutionProvider']; print('  Acceleration: '+', '.join(p) if p else '  Acceleration: CPU only')" ^
  2>nul

echo.
echo  ════════════════════════════════════════════════
echo    语音识别 WebSocket 服务
echo    Speech Recognition WebSocket Service
echo  ════════════════════════════════════════════════
echo    按 Ctrl+C 停止  /  Press Ctrl+C to stop
echo  ════════════════════════════════════════════════
echo.

.venv\Scripts\python.exe server.py

echo.
echo  服务已停止。/ Service stopped.
pause
