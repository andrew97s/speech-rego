@echo off
chcp 65001 >nul
setlocal
title 构建 Windows 服务安装包

echo.
echo  将 build_offline 产出的离线目录打成「含 NSSM + 安装脚本 + 双击安装」的 ZIP
echo  默认离线目录: dist\SpeechReco-Offline
echo  默认输出 ZIP: dist\SpeechReco-Offline-WindowsService.zip
echo  可选: 生成 Setup.exe 需在构建机安装 ps2exe 模块:
echo        Install-Module ps2exe -Scope CurrentUser -Force
echo  若不需要 exe:  PowerShell -File build_service_installer.ps1 -SkipSetupExe
echo  公网 ZIP 直链嵌入 WebSetup.exe:
echo        PowerShell -File build_service_installer.ps1 -PublicPackageUrl "https://.../xxx.zip"
echo.

PowerShell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_service_installer.ps1" %*

echo.
pause
