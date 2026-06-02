; ═══════════════════════════════════════════════════════════════════════════
;  installer_whisper.iss — Whisper 离线包 + Windows 服务（Inno Setup 6）
;
;  由 build_whisper_installer.ps1 调用，需先执行 build_service_installer 生成暂存目录。
;  编译示例:
;    ISCC.exe /DStagingDir="D:\proj\dist\SpeechReco-InnoStaging" installer_whisper.iss
; ═══════════════════════════════════════════════════════════════════════════

#ifndef StagingDir
  #define StagingDir "dist\SpeechReco-InnoStaging"
#endif
#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#ifndef ServiceName
  #define ServiceName "SpeechRecoWhisper"
#endif

#define AppName       "语音识别 (Whisper)"
#define AppNameEn     "Speech Reco Whisper"
#define AppPublisher  "SpeechRego"
#define AppURL        "https://github.com/andrew97s/speech-rego"

[Setup]
AppId={{C9E8F7A6-5B4D-3210-FEDC-BA9876543210}
AppName={#AppName}
AppVerName={#AppNameEn} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppVersion={#AppVersion}
VersionInfoVersion={#AppVersion}

DefaultDirName={autopf}\SpeechReco-Offline
DefaultGroupName={#AppNameEn}
AllowNoIcons=yes
DisableWelcomePage=no
DisableDirPage=no
DisableProgramGroupPage=yes

OutputDir=dist
OutputBaseFilename=SpeechReco-Whisper-Setup
Compression=lzma2/ultra64
SolidCompression=yes
LZMAUseSeparateProcess=yes

WizardStyle=modern
WizardResizable=yes
WizardImageFile=installer_assets\wizard-large.bmp
WizardSmallImageFile=installer_assets\wizard-small.bmp

CloseApplications=no
MinVersion=10.0.17763
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=dialog

UninstallDisplayName={#AppName}
UninstallDisplayIcon={sys}\shell32.dll,176

[Languages]
Name: "schinese"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
Name: "english";  MessagesFile: "compiler:Default.isl"

[CustomMessages]
schinese.WelcomeMsg=本向导将把 Speech Reco Whisper 离线语音识别服务安装到您选择的目录。%n%n安装内容包括 Python 运行时、Whisper 模型缓存与 WebSocket 服务；可选注册为 Windows 服务并写入运行日志。%n%n建议关闭正在使用该目录内文件的程序后继续。
english.WelcomeMsg=This wizard installs the Speech Reco Whisper offline speech recognition package to the folder you choose.%n%nIt includes the Python runtime, Whisper model cache, and WebSocket server. You can register a Windows service with log files under logs\.%n%nClose programs that might lock files in the target folder, then continue.

schinese.FinishedLabel=安装已完成。若已勾选「注册 Windows 服务」，服务应已启动；日志位于安装目录下的 logs 文件夹。
english.FinishedLabel=Setup finished. If you registered the Windows service, it should be running. Logs are in the logs folder under the install directory.

schinese.TasksGroupSvc=服务选项
english.TasksGroupSvc=Service options
schinese.TaskInstallSvc=注册并启动 Windows 服务（推荐；日志写入 logs 目录）
english.TaskInstallSvc=Register and start the Windows service (recommended; logs in logs\)

schinese.StatusRegistering=正在注册 Windows 服务…
english.StatusRegistering=Registering Windows service...

[Tasks]
Name: "installservice"; Description: "{cm:TaskInstallSvc}"; GroupDescription: "{cm:TasksGroupSvc}"; Flags: checkedonce
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#StagingDir}\*"; DestDir: "{app}"; \
  Flags: ignoreversion recursesubdirs createallsubdirs sortfilesbyextension; \
  Excludes: "config.json"
Source: "{#StagingDir}\config.json"; DestDir: "{app}"; \
  Flags: ignoreversion onlyifdoesntexist

[Icons]
Name: "{autoprograms}\{#AppNameEn}\启动 Whisper (控制台)"; \
  Filename: "{app}\start.bat"; WorkingDir: "{app}"; \
  IconFilename: "{sys}\shell32.dll"; IconIndex: 22
Name: "{autoprograms}\{#AppNameEn}\服务日志 (logs)"; \
  Filename: "{win}\explorer.exe"; Parameters: "{app}\logs"; \
  IconFilename: "{sys}\imageres.dll"; IconIndex: 3; \
  Flags: excludefromshowinnewinstall
Name: "{autoprograms}\{#AppNameEn}\{cm:UninstallProgram,{#AppNameEn}}"; \
  Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppNameEn}"; \
  Filename: "{app}\start.bat"; WorkingDir: "{app}"; \
  IconFilename: "{sys}\shell32.dll"; IconIndex: 22; Tasks: desktopicon

[Registry]
Root: HKLM; Subkey: "Software\{#AppNameEn}"; \
  ValueType: string; ValueName: "InstallPath"; ValueData: "{app}"; Flags: uninsdeletekey
Root: HKLM; Subkey: "Software\{#AppNameEn}"; \
  ValueType: string; ValueName: "Version"; ValueData: "{#AppVersion}"
Root: HKLM; Subkey: "Software\{#AppNameEn}"; \
  ValueType: string; ValueName: "ServiceName"; ValueData: "{#ServiceName}"

[Run]
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; \
  Parameters: "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File ""{app}\install_service.ps1"""; \
  StatusMsg: "{cm:StatusRegistering}"; \
  Flags: waituntilterminated; \
  Tasks: installservice

[UninstallRun]
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; \
  Parameters: "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File ""{app}\uninstall_service.ps1"""; \
  RunOnceId: "RemoveSpeechRecoService"; \
  Flags: waituntilterminated

[UninstallDelete]
Type: filesandordirs; Name: "{app}\logs"
Type: filesandordirs; Name: "{app}\__pycache__"

[Code]

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
  Svc: String;
begin
  Result := '';
  Svc := ExpandConstant('{#ServiceName}');
  Exec(ExpandConstant('{sys}\sc.exe'), 'stop ' + Svc, '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Sleep(2000);
  Exec(ExpandConstant('{sys}\sc.exe'), 'delete ' + Svc, '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Sleep(500);
end;

procedure InitializeWizard;
begin
  WizardForm.WelcomeLabel2.Caption := ExpandConstant('{cm:WelcomeMsg}');
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if CurPageID = wpFinished then
    WizardForm.FinishedLabel.Caption := ExpandConstant('{cm:FinishedLabel}');
end;
