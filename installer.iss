; ═══════════════════════════════════════════════════════════════════════════
;  installer.iss — Inno Setup 6 Script
;  语音识别服务 / Speech Recognition Service
;
;  Build with:  build_installer.bat  (sets /DSourceDir /DBuildDir /DDistDir)
;  Or manually: ISCC.exe /DSourceDir=. /DBuildDir=build /DDistDir=dist installer.iss
; ═══════════════════════════════════════════════════════════════════════════

#ifndef SourceDir
  #define SourceDir "."
#endif
#ifndef BuildDir
  #define BuildDir "build"
#endif
#ifndef DistDir
  #define DistDir "dist"
#endif
#ifndef BundleModels
  #define BundleModels "0"
#endif

#define AppName      "语音识别服务"
#define AppNameEn    "Speech Recognition Service"
#define AppVersion   "1.0.0"
#define AppPublisher "SpeechRego"
#define AppURL       "https://github.com/andrew97s/speech-rego"
#define CnModel      "vosk-model-small-cn-0.22"
#define EnModel      "vosk-model-small-en-us-0.15"

; ── Setup section ─────────────────────────────────────────────────────────────
[Setup]
; NOTE: Change AppId GUID if you fork this project.
AppId={{F3A2B1C0-D4E5-4F60-9A7B-8C9D0E1F2A3B}
AppName={#AppName}
AppVerName={#AppNameEn} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppVersion={#AppVersion}

; Install to Program Files\SpeechRecoService
DefaultDirName={autopf}\SpeechRecoService
DefaultGroupName={#AppNameEn}
DisableProgramGroupPage=yes

; Output
OutputDir={#DistDir}
OutputBaseFilename=SpeechRecoService-Setup

; Compression (LZMA2 ultra = best ratio, ~30% smaller)
Compression=lzma2/ultra64
SolidCompression=yes
LZMAUseSeparateProcess=yes

; UI
WizardStyle=modern
WizardResizable=yes
ShowLanguageDialog=yes

; Minimum OS: Windows 10 1809 (Build 17763)
MinVersion=10.0.17763
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

; Privileges: require admin for Program Files install
PrivilegesRequired=admin

; Uninstaller
UninstallDisplayName={#AppName}
UninstallDisplayIcon={app}\python\python.exe

; ── Languages ─────────────────────────────────────────────────────────────────
[Languages]
Name: "schinese"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
Name: "english";  MessagesFile: "compiler:Default.isl"

; ── Custom wizard messages ────────────────────────────────────────────────────
[CustomMessages]
schinese.WelcomeTitle=欢迎安装 {#AppName}
schinese.WelcomeMsg=本安装向导将把【语音识别 WebSocket 服务】安装到您的计算机。%n%n该服务包含：%n  • Vosk 离线语音识别引擎%n  • 唤醒词检测（中文/英文）%n  • WebSocket 服务，可连接任意客户端%n%n建议关闭其他程序后继续。
english.WelcomeTitle=Welcome to {#AppNameEn} Setup
english.WelcomeMsg=This wizard will install the Speech Recognition WebSocket Service.%n%nIncludes:%n  • Vosk offline ASR engine%n  • Wake word detection (Chinese/English)%n  • WebSocket server for any client%n%nClose other applications before continuing.

schinese.GpuPageTitle=选择 GPU 加速模式
schinese.GpuPageDesc=根据您的显卡选择合适的加速方式
schinese.GpuCuda=NVIDIA CUDA（最快，需要 CUDA 驱动已安装）
schinese.GpuDml=DirectML（适用于 NVIDIA / AMD / Intel 核显，无需 CUDA）
schinese.GpuCpu=仅 CPU（所有设备通用，无需显卡驱动）
schinese.GpuNote=注意：Vosk 语音识别引擎为纯 CPU 实现，GPU 仅加速唤醒词检测部分。
schinese.ModelPageTitle=选择语音模型
schinese.ModelCn=中文普通话模型（~42 MB）
schinese.ModelEn=英文模型（~40 MB）
schinese.ModelBoth=两种模型都下载

english.GpuPageTitle=Select GPU Acceleration
english.GpuPageDesc=Choose hardware acceleration based on your GPU
english.GpuCuda=NVIDIA CUDA (fastest, requires CUDA driver)
english.GpuDml=DirectML (works with NVIDIA / AMD / Intel GPU, no CUDA needed)
english.GpuCpu=CPU only (universal, no GPU driver required)
english.GpuNote=Note: Vosk ASR is CPU-only; GPU accelerates wake word detection only.
english.ModelPageTitle=Select ASR Language Models
english.ModelCn=Chinese Mandarin model (~42 MB)
english.ModelEn=English model (~40 MB)
english.ModelBoth=Download both models

; ── Components ────────────────────────────────────────────────────────────────
[Types]
Name: "full";    Description: "{cm:ModelBoth}（离线 / Offline）"
Name: "cn";      Description: "{cm:ModelCn}"
Name: "en";      Description: "{cm:ModelEn}"
Name: "nomodel"; Description: "仅主程序，稍后下载模型 / App only, download models later"
Name: "custom";  Description: "自定义 / Custom"; Flags: iscustom

[Components]
Name: "app";      Description: "主程序 / Main application"; Types: full cn en nomodel custom; Flags: fixed
Name: "model_cn"; Description: "{cm:ModelCn}"; Types: full cn custom
Name: "model_en"; Description: "{cm:ModelEn}"; Types: full en custom

; ── Files ─────────────────────────────────────────────────────────────────────
[Files]
; Bundled Python runtime + all pip packages (no Python installation required)
Source: "{#BuildDir}\python\*"; DestDir: "{app}\python"; \
    Flags: recursesubdirs createallsubdirs; Components: app

; Application source
Source: "{#BuildDir}\server.py";         DestDir: "{app}"; Components: app
Source: "{#BuildDir}\engine.py";         DestDir: "{app}"; Components: app
Source: "{#BuildDir}\index.html";        DestDir: "{app}"; Components: app
Source: "{#BuildDir}\list_devices.py";   DestDir: "{app}"; Components: app
Source: "{#BuildDir}\verify_install.py"; DestDir: "{app}"; Components: app

; config.json — skip if already present (preserve settings across upgrades)
Source: "{#BuildDir}\config.json"; DestDir: "{app}"; \
    Flags: onlyifdoesntexist uninsneveruninstall; Components: app

; Launcher (uses bundled python\)
Source: "{#BuildDir}\start.bat"; DestDir: "{app}"; Components: app

; Chinese model (only if build_installer.bat downloaded it)
#if BundleModels == "1"
Source: "{#BuildDir}\models\{#CnModel}\*"; \
    DestDir: "{app}\models\{#CnModel}"; \
    Flags: recursesubdirs createallsubdirs skipifsourcedoesntexist; \
    Components: model_cn
Source: "{#BuildDir}\models\{#EnModel}\*"; \
    DestDir: "{app}\models\{#EnModel}"; \
    Flags: recursesubdirs createallsubdirs skipifsourcedoesntexist; \
    Components: model_en
#endif

; ── Start Menu & Desktop shortcuts ────────────────────────────────────────────
[Icons]
Name: "{autoprograms}\{#AppNameEn}\启动服务"; \
    Filename: "{app}\start.bat"; WorkingDir: "{app}"; \
    IconFilename: "{sys}\shell32.dll"; IconIndex: 22
Name: "{autoprograms}\{#AppNameEn}\列出麦克风 (list_devices)"; \
    Filename: "{app}\python\python.exe"; Parameters: "list_devices.py"; \
    WorkingDir: "{app}"
Name: "{autoprograms}\{#AppNameEn}\验证安装 (verify)"; \
    Filename: "{app}\python\python.exe"; Parameters: "verify_install.py"; \
    WorkingDir: "{app}"
Name: "{autoprograms}\{#AppNameEn}\{cm:UninstallProgram,{#AppNameEn}}"; \
    Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppNameEn}"; \
    Filename: "{app}\start.bat"; WorkingDir: "{app}"; \
    IconFilename: "{sys}\shell32.dll"; IconIndex: 22; \
    Tasks: desktopicon

; ── Tasks ─────────────────────────────────────────────────────────────────────
[Tasks]
Name: "desktopicon"; \
    Description: "在桌面创建快捷方式 / Create desktop shortcut"; \
    Flags: checkedonce

; ── Registry ──────────────────────────────────────────────────────────────────
[Registry]
; Store install info for verify_install.py and future upgrades
Root: HKLM; Subkey: "Software\SpeechRecoService"; \
    ValueType: string; ValueName: "InstallPath"; ValueData: "{app}"; \
    Flags: uninsdeletekey
Root: HKLM; Subkey: "Software\SpeechRecoService"; \
    ValueType: string; ValueName: "Version"; ValueData: "{#AppVersion}"

; ── Post-install run ──────────────────────────────────────────────────────────
[Run]
; Download CN model if component selected and not bundled
Filename: "{app}\python\python.exe"; \
    Parameters: """{app}\download_model.py"" cn"; \
    WorkingDir: "{app}"; \
    StatusMsg: "正在下载中文语音模型 / Downloading Chinese ASR model..."; \
    Flags: waituntilterminated; \
    Components: model_cn; \
    Check: not DirExists(ExpandConstant('{app}\models\{#CnModel}'))

; Download EN model if component selected and not bundled
Filename: "{app}\python\python.exe"; \
    Parameters: """{app}\download_model.py"" en"; \
    WorkingDir: "{app}"; \
    StatusMsg: "正在下载英文语音模型 / Downloading English ASR model..."; \
    Flags: waituntilterminated; \
    Components: model_en; \
    Check: not DirExists(ExpandConstant('{app}\models\{#EnModel}'))

; Upgrade onnxruntime to GPU variant based on user's choice
Filename: "{app}\python\python.exe"; \
    Parameters: """{app}\install_gpu.py"" {code:GetGpuChoice}"; \
    WorkingDir: "{app}"; \
    StatusMsg: "正在配置 GPU 加速 / Configuring GPU acceleration..."; \
    Flags: waituntilterminated; \
    Check: NeedsGpuUpgrade

; Launch the service after install (optional)
Filename: "{app}\start.bat"; \
    Description: "立即启动服务 / Launch service now"; \
    Flags: postinstall nowait skipifsilent unchecked

; ── Uninstall cleanup ─────────────────────────────────────────────────────────
[UninstallDelete]
Type: filesandordirs; Name: "{app}\models"
Type: filesandordirs; Name: "{app}\__pycache__"
Type: filesandordirs; Name: "{app}\python\Lib\site-packages\vosk\*"

; ═══════════════════════════════════════════════════════════════════════════════
;  Pascal Code — GPU detection, custom pages, download helpers
; ═══════════════════════════════════════════════════════════════════════════════
[Code]

var
  GpuPage: TInputOptionWizardPage;
  HasNvidia: Boolean;
  GpuChoice: String;      // "cuda" | "dml" | "cpu"

// ── Detect NVIDIA GPU via nvidia-smi ────────────────────────────────────────
function DetectNvidia(): Boolean;
var
  ResultCode: Integer;
begin
  Result := Exec(
    ExpandConstant('{sys}\cmd.exe'),
    '/c nvidia-smi --list-gpus >nul 2>&1',
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode
  ) and (ResultCode = 0);
end;

// ── Create custom GPU page ───────────────────────────────────────────────────
procedure InitializeWizard;
begin
  HasNvidia := DetectNvidia();

  GpuPage := CreateInputOptionPage(
    wpSelectComponents,
    CustomMessage('GpuPageTitle'),
    CustomMessage('GpuPageDesc'),
    '',    { ASubCaption }
    True,  { AExclusive — radio buttons, only one choice }
    False  { AWordWrap }
  );

  if HasNvidia then begin
    GpuPage.Add(CustomMessage('GpuCuda'));
    GpuPage.Add(CustomMessage('GpuDml'));
    GpuPage.Add(CustomMessage('GpuCpu'));
    GpuPage.SelectedValueIndex := 0;
  end else begin
    GpuPage.Add(CustomMessage('GpuDml'));
    GpuPage.Add(CustomMessage('GpuCpu'));
    GpuPage.SelectedValueIndex := 0;
  end;

  // Add note label
  with TLabel.Create(GpuPage.Surface) do begin
    Parent  := GpuPage.Surface;
    Caption := CustomMessage('GpuNote');
    Left    := 0;
    Top     := GpuPage.CheckListBox.Top + GpuPage.CheckListBox.Height + 8;
    Width   := GpuPage.SurfaceWidth;
    Font.Color := $00808080;
    WordWrap := True;
    AutoSize := False;
    Height   := 40;
  end;
end;

// ── Resolve GPU choice string after user picks ───────────────────────────────
procedure CurStepChanged(CurStep: TSetupStep);
var
  Idx: Integer;
begin
  if CurStep = ssInstall then begin
    Idx := GpuPage.SelectedValueIndex;
    if HasNvidia then begin
      case Idx of
        0: GpuChoice := 'cuda';
        1: GpuChoice := 'dml';
        else GpuChoice := 'cpu';
      end;
    end else begin
      case Idx of
        0: GpuChoice := 'dml';
        else GpuChoice := 'cpu';
      end;
    end;

  end;
end;

// ── Pass GPU choice as CLI argument to install_gpu.py ────────────────────────
function GetGpuChoice(Param: String): String;
begin
  Result := GpuChoice;
end;

// ── Check if we need to run the GPU upgrade step ──────────────────────────────
function NeedsGpuUpgrade(): Boolean;
begin
  Result := (GpuChoice = 'cuda') or (GpuChoice = 'dml');
end;
