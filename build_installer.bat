@echo off
:: Speech Recognition Service - Windows EXE Installer Build Script
:: Output: dist\SpeechRecoService-Setup.exe
::
:: Requirements: Windows 10/11 x64, internet connection
::
:: Usage:
::   build_installer.bat
::   build_installer.bat --bundle-models
::   build_installer.bat --bundle-models --gpu cuda
::   build_installer.bat --bundle-models --gpu dml
::
:: Options:
::   --bundle-models : embed ASR models in installer (~200 MB, fully offline)
::   --gpu cuda      : pre-install onnxruntime-gpu  (NVIDIA CUDA 12+)
::   --gpu dml       : pre-install onnxruntime-directml (any DirectX 12 GPU)
::   --gpu none      : CPU only (default; GPU selectable during install)

chcp 65001 >nul
setlocal EnableDelayedExpansion

set BUILD_DIR=%~dp0build
set DIST_DIR=%~dp0dist
set PYTHON_EMBED_DIR=%BUILD_DIR%\python
set MODELS_DIR=%BUILD_DIR%\models
set BUNDLE_MODELS=0
set GPU_MODE=none

:: Parse arguments
:parse_args
if "%~1"=="" goto args_done
if /i "%~1"=="--bundle-models" ( set BUNDLE_MODELS=1 & shift & goto parse_args )
if /i "%~1"=="--gpu"           ( set GPU_MODE=%~2  & shift & shift & goto parse_args )
shift
goto parse_args
:args_done

:: Banner (Chinese OK here - after chcp 65001)
echo.
echo  +========================================================+
echo  ^|  语音识别服务 - Windows EXE 安装包构建脚本           ^|
echo  ^|  Speech Recognition Service -- EXE Installer Builder  ^|
echo  +========================================================+
echo.
echo  Bundle models : %BUNDLE_MODELS%
echo  GPU mode      : %GPU_MODE%
echo.

:: Versions and URLs
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

:: Step 1: Prepare directories
echo [1/8] Preparing build directories...
if exist "%BUILD_DIR%" rmdir /s /q "%BUILD_DIR%"
mkdir "%PYTHON_EMBED_DIR%" 2>nul
mkdir "%MODELS_DIR%"       2>nul
mkdir "%DIST_DIR%"         2>nul

:: Step 2: Download and extract Python embeddable
echo [2/8] Downloading Python %PY_VER% embeddable...
if not exist "%TEMP%\%PY_ZIP%" (
    curl -L --progress-bar -o "%TEMP%\%PY_ZIP%" "%PY_URL%"
    if errorlevel 1 ( echo ERROR: Failed to download Python. & goto :error )
)
echo       Extracting...
powershell -NoProfile -Command ^
    "Expand-Archive -Path '%TEMP%\%PY_ZIP%' -DestinationPath '%PYTHON_EMBED_DIR%' -Force"

:: Enable site-packages in the embedded Python
:: (the _pth file restricts imports by default)
echo Lib\site-packages >> "%PYTHON_EMBED_DIR%\python311._pth"
echo import site       >> "%PYTHON_EMBED_DIR%\python311._pth"

:: Step 3: Install pip into embedded Python
echo [3/8] Installing pip...
curl -L --progress-bar -o "%PYTHON_EMBED_DIR%\get-pip.py" "%GETPIP_URL%"
"%PYTHON_EMBED_DIR%\python.exe" "%PYTHON_EMBED_DIR%\get-pip.py" --quiet
if errorlevel 1 ( echo ERROR: pip install failed. & goto :error )

set PIP="%PYTHON_EMBED_DIR%\python.exe" -m pip

:: Step 4: Install Python packages
echo [4/8] Installing Python packages...
%PIP% install --quiet ^
    "websockets>=12.0" ^
    "sounddevice>=0.4.6" ^
    "numpy>=1.24.0,<2.0.0" ^
    "vosk>=0.3.45" ^
    "openwakeword>=0.6.0"
if errorlevel 1 ( echo ERROR: Package install failed. & goto :error )

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

:: Pre-download openwakeword models
echo       Pre-downloading openwakeword models...
"%PYTHON_EMBED_DIR%\python.exe" -c ^
    "import warnings; warnings.filterwarnings('ignore'); import openwakeword; openwakeword.utils.download_models()" ^
    2>nul

:: Step 5: Download ASR models (optional)
echo [5/8] ASR models...
if "%BUNDLE_MODELS%"=="1" (
    call :download_model "%CN_MODEL%" "%CN_URL%"
    call :download_model "%EN_MODEL%" "%EN_URL%"
) else (
    echo       Skipping model bundle ^(use --bundle-models to include^).
    echo       Models will be downloaded during installation.
)

:: Step 6: Copy application source files
echo [6/8] Copying application files...
copy /y "%~dp0server.py"           "%BUILD_DIR%\server.py"           >nul
copy /y "%~dp0engine.py"           "%BUILD_DIR%\engine.py"           >nul
copy /y "%~dp0index.html"          "%BUILD_DIR%\index.html"          >nul
copy /y "%~dp0config.json"         "%BUILD_DIR%\config.json"         >nul
copy /y "%~dp0list_devices.py"     "%BUILD_DIR%\list_devices.py"     >nul
copy /y "%~dp0verify_install.py"   "%BUILD_DIR%\verify_install.py"   >nul
copy /y "%~dp0download_model.py"   "%BUILD_DIR%\download_model.py"   >nul
copy /y "%~dp0install_gpu.py"      "%BUILD_DIR%\install_gpu.py"      >nul
copy /y "%~dp0start_installed.bat" "%BUILD_DIR%\start.bat"           >nul

:: Step 7: Check / download Inno Setup 6
echo [7/8] Checking Inno Setup 6...
if not exist "%INNO_EXE%" (
    echo       Not found. Downloading Inno Setup 6...
    curl -L --progress-bar -o "%TEMP%\innosetup.exe" "%INNO_URL%"
    echo       Installing silently...
    "%TEMP%\innosetup.exe" /VERYSILENT /NORESTART
    if not exist "%INNO_EXE%" (
        echo ERROR: Inno Setup install failed.
        echo        Download manually: https://jrsoftware.org/isdl.php
        goto :error
    )
    echo       Inno Setup installed.
) else (
    echo       Inno Setup found.
)

:: Step 8: Compile the installer
echo [8/8] Compiling EXE installer...
set ISS_FLAGS=/DSourceDir="%~dp0" /DBuildDir="%BUILD_DIR%" /DDistDir="%DIST_DIR%"
if "%BUNDLE_MODELS%"=="1" set ISS_FLAGS=%ISS_FLAGS% /DBundleModels=1

"%INNO_EXE%" %ISS_FLAGS% "%~dp0installer.iss"
if errorlevel 1 ( echo ERROR: Inno Setup compilation failed. & goto :error )

:: Done
echo.
echo  +==========================================================+
echo  ^|  构建成功! Build successful!                            ^|
echo  +==========================================================+
echo.
echo  Output: %DIST_DIR%\SpeechRecoService-Setup.exe
echo.
goto :eof

:: Subroutine: download and extract a model
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
powershell -NoProfile -Command ^
    "Expand-Archive -Path '%MODELS_DIR%\%MNAME%.zip' -DestinationPath '%MODELS_DIR%' -Force"
del "%MODELS_DIR%\%MNAME%.zip" 2>nul
goto :eof

:error
echo.
echo  Build failed. See errors above.
pause
exit /b 1
