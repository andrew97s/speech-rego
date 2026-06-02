@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo.
echo  Whisper 离线包 — Windows 安装程序构建
echo  输出: dist\SpeechReco-Whisper-Setup.exe
echo.
echo  前提: 已运行 build_offline.bat / build_offline.ps1 生成 dist\SpeechReco-Offline
echo  若尚未构建离线包，请先执行:  build_offline.bat
echo.

PowerShell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_whisper_installer.ps1" %*

if errorlevel 1 (
    echo.
    echo 构建失败。
    pause
    exit /b 1
)
echo.
pause
