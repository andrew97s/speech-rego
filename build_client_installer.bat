@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo.
echo  Windows 客户端安装包构建
echo  输出: dist\SpeechReco-Client-Setup.exe
echo  安装后: Windows 服务 SpeechRecoClient（开机自启）
echo          Web http://127.0.0.1:9400/index.html
echo          WebSocket ws://127.0.0.1:8766
echo.
echo  需要: Inno Setup 6  https://jrsoftware.org/isdl.php
echo.

PowerShell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\ensure_utf8_bom.ps1" -Path "%~dp0build_offline.ps1"
PowerShell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\ensure_utf8_bom.ps1" -Path "%~dp0build_client_installer.ps1"
PowerShell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_client_installer.ps1" %*

if errorlevel 1 (
    echo.
    echo 构建失败。
    pause
    exit /b 1
)
echo.
pause
