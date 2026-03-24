@echo off
:: ═══════════════════════════════════════════════════════════════════════
::  语音识别服务 — Windows EXE 安装包构建脚本
::  Speech Recognition Service — EXE Installer Build Script
::
::  运行要求 / Requirements:
::    - Windows 10/11  x64
::    - Internet connection (downloads Inno Setup + Python embeddable)
::
::  输出 / Output:
::    dist\SpeechRecoService-Setup.exe
::
::  用法 / Usage:
::    build_installer.bat [--bundle-models] [--gpu cuda|dml|none]
::      --bundle-models   把语音模型打入安装包（离线安装，包体 ~200MB）
::      --gpu cuda        打包时预装 onnxruntime-gpu (CUDA 12+)
::      --gpu dml         打包时预装 onnxruntime-directml
::      --gpu none        仅 CPU（默认，安装时可选 GPU 升级）
::
::  示例 / Examples:
::    build_installer.bat                        纯 CPU，安装时下载模型
::    build_installer.bat --bundle-models        纯 CPU，模型打入包
::    build_installer.bat --bundle-models --gpu cuda   CUDA + 模型全包
:: ═══════════════════════════════════════════════════════════════════════

chcp 65001 >nul
setlocal EnableDelayedExpansion

set BUILD_DIR=%~dp0build
set DIST_DIR=%~dp0dist
set PYTHON_EMBED_DIR=%BUILD_DIR%\python
set MODELS_DIR=%BUILD_DIR%\models
set BUNDLE_MODELS=0
set GPU_MODE=none

:: ── Parse arguments ────────────────────────────────────────────────────────
:parse_args
if "%~1"=="" goto args_done
if /i "%~1"=="--bundle-models" ( set BUNDLE_MODELS=1 & shift & goto parse_args )
if /i "%~1"=="--gpu"           ( set GPU_MODE=%~2  & shift & shift & goto parse_args )
shift
goto parse_args
:args_done

:: ── Banner ─────────────────────────────────────────────────────────────────
echo.
echo  ╔══════════════════════════════════════════════════════╗
echo  ║  语音识别服务 EXE 安装包构建脚本                    ║
echo  ║  Speech Recognition Service — Installer Builder     ║
echo  ╚══════════════════════════════════════════════════════╝
echo.
echo  Bundle models : %BUNDLE_MODELS%
echo  GPU mode      : %GPU_MODE%
echo.

:: ── Versions / URLs ────────────────────────────────────────────────────────
set PY_VER=3.11.9
set PY_ZIP=python-%PY_VER%-embed-amd64.zip
set PY_URL=https://www.python.org/ftp/python/%PY_VER%/%PY_ZIP%
set GETPIP_URL=https://bootstrap.pypa.io/get-pip.py
set INNO_URL=https://files.jrsoftware.org/is/6/innosetup-6.3.3.exe
set INNO_EXE=C:\Program Files (x86)\Inno Setup 6\ISCC.exe

set CN_MODEL=vosk-model-small-cn-0.22
set EN_MODEL=vosk-model-small-en-us-0.15
set CN_URL=https://alphacephei.com/vosk/models/%CN_MODEL%.zip
set EN_URL=https://alphacephei.com/vosk/models/%EN_MODEL%.zip

:: ── Prepare directories ────────────────────────────────────────────────────
echo [1/8] Preparing build directories...
if exist "%BUILD_DIR%" rmdir /s /q "%BUILD_DIR%"
mkdir "%PYTHON_EMBED_DIR%" 2>nul
mkdir "%MODELS_DIR%" 2>nul
mkdir "%DIST_DIR%" 2>nul

:: ── Download + extract Python embeddable ──────────────────────────────────
echo [2/8] Downloading Python %PY_VER% embeddable...
if not exist "%TEMP%\%PY_ZIP%" (
    curl -L --progress-bar -o "%TEMP%\%PY_ZIP%" "%PY_URL%"
    if errorlevel 1 ( echo ERROR: Failed to download Python. & goto :error )
)
echo       Extracting...
powershell -NoProfile -Command "Expand-Archive -Path '%TEMP%\%PY_ZIP%' -DestinationPath '%PYTHON_EMBED_DIR%' -Force"

:: Enable site-packages in embedded Python (required for pip-installed packages)
:: The _pth file must be edited to include ".\Lib\site-packages" and uncomment "import site"
echo Lib\site-packages >> "%PYTHON_EMBED_DIR%\python311._pth"
echo import site       >> "%PYTHON_EMBED_DIR%\python311._pth"

:: ── Install pip ────────────────────────────────────────────────────────────
echo [3/8] Installing pip into embedded Python...
curl -L --progress-bar -o "%PYTHON_EMBED_DIR%\get-pip.py" "%GETPIP_URL%"
"%PYTHON_EMBED_DIR%\python.exe" "%PYTHON_EMBED_DIR%\get-pip.py" --quiet
if errorlevel 1 ( echo ERROR: pip installation failed. & goto :error )

set PIP="%PYTHON_EMBED_DIR%\python.exe" -m pip

:: ── Install Python packages ────────────────────────────────────────────────
echo [4/8] Installing Python packages...
%PIP% install --quiet ^
    "websockets>=12.0" ^
    "sounddevice>=0.4.6" ^
    "numpy>=1.24.0,<2.0.0" ^
    "vosk>=0.3.45" ^
    "openwakeword>=0.6.0"
if errorlevel 1 ( echo ERROR: Package installation failed. & goto :error )

:: Replace onnxruntime with GPU variant if requested
if /i "%GPU_MODE%"=="cuda" (
    echo       Switching to onnxruntime-gpu [CUDA]...
    %PIP% uninstall onnxruntime -y --quiet 2>nul
    %PIP% install --quiet "onnxruntime-gpu>=1.17.0"
    if errorlevel 1 (
        echo  WARN: onnxruntime-gpu failed, keeping CPU version.
        %PIP% install --quiet "onnxruntime>=1.16.0"
    )
) else if /i "%GPU_MODE%"=="dml" (
    echo       Switching to onnxruntime-directml [DirectML]...
    %PIP% uninstall onnxruntime -y --quiet 2>nul
    %PIP% install --quiet "onnxruntime-directml>=1.17.0"
    if errorlevel 1 (
        echo  WARN: onnxruntime-directml failed, keeping CPU version.
        %PIP% install --quiet "onnxruntime>=1.16.0"
    )
)

:: Pre-download openwakeword models into the embedded Python's appdata
echo       Pre-downloading openwakeword models...
"%PYTHON_EMBED_DIR%\python.exe" -c ^
    "import warnings; warnings.filterwarnings('ignore'); import openwakeword; openwakeword.utils.download_models()" ^
    2>nul

:: ── Download ASR models (optional bundle) ─────────────────────────────────
echo [5/8] ASR models...
if "%BUNDLE_MODELS%"=="1" (
    call :download_model "%CN_MODEL%" "%CN_URL%"
    call :download_model "%EN_MODEL%" "%EN_URL%"
) else (
    echo       Skipping model bundle ^(--bundle-models not set^).
    echo       Models will be downloaded during installation.
)

:: ── Copy application source files ─────────────────────────────────────────
echo [6/8] Copying application files...
copy /y "%~dp0server.py"       "%BUILD_DIR%\server.py"       >nul
copy /y "%~dp0engine.py"       "%BUILD_DIR%\engine.py"       >nul
copy /y "%~dp0index.html"      "%BUILD_DIR%\index.html"      >nul
copy /y "%~dp0config.json"     "%BUILD_DIR%\config.json"     >nul
copy /y "%~dp0list_devices.py" "%BUILD_DIR%\list_devices.py" >nul
copy /y "%~dp0verify_install.py" "%BUILD_DIR%\verify_install.py" >nul
copy /y "%~dp0start_installed.bat" "%BUILD_DIR%\start.bat"   >nul

:: ── Inno Setup check / download ────────────────────────────────────────────
echo [7/8] Checking Inno Setup 6...
if not exist "%INNO_EXE%" (
    echo       Inno Setup not found. Downloading installer...
    curl -L --progress-bar -o "%TEMP%\innosetup.exe" "%INNO_URL%"
    echo       Installing Inno Setup silently...
    "%TEMP%\innosetup.exe" /VERYSILENT /NORESTART
    if not exist "%INNO_EXE%" (
        echo ERROR: Inno Setup installation failed.
        echo        Download manually from: https://jrsoftware.org/isdl.php
        goto :error
    )
    echo       Inno Setup installed.
) else (
    echo       Inno Setup found.
)

:: ── Compile installer ──────────────────────────────────────────────────────
echo [8/8] Compiling EXE installer...
set ISS_FLAGS=/DSourceDir="%~dp0" /DBuildDir="%BUILD_DIR%" /DDistDir="%DIST_DIR%"
if "%BUNDLE_MODELS%"=="1" set ISS_FLAGS=%ISS_FLAGS% /DBundleModels=1
"%INNO_EXE%" %ISS_FLAGS% "%~dp0installer.iss"
if errorlevel 1 ( echo ERROR: Inno Setup compilation failed. & goto :error )

:: ── Done ───────────────────────────────────────────────────────────────────
echo.
echo  ╔══════════════════════════════════════════════════════╗
echo  ║  构建成功! / Build successful!                       ║
echo  ╚══════════════════════════════════════════════════════╝
echo.
echo  Output: %DIST_DIR%\SpeechRecoService-Setup.exe
echo.
goto :eof

:: ── Subroutine: download and extract a model ──────────────────────────────
:download_model
set MNAME=%~1
set MURL=%~2
set MDEST=%MODELS_DIR%\%MNAME%
if exist "%MDEST%" (
    echo       Already have: %MNAME%
    goto :eof
)
echo       Downloading %MNAME%...
curl -L --progress-bar -o "%MODELS_DIR%\%MNAME%.zip" "%MURL%"
if errorlevel 1 ( echo  WARN: Failed to download %MNAME%. & goto :eof )
echo       Extracting %MNAME%...
powershell -NoProfile -Command "Expand-Archive -Path '%MODELS_DIR%\%MNAME%.zip' -DestinationPath '%MODELS_DIR%' -Force"
del "%MODELS_DIR%\%MNAME%.zip" 2>nul
goto :eof

:error
echo.
echo  Build failed. See errors above.
pause
exit /b 1
