; ═══════════════════════════════════════════════════════════════════════════
;  installer_client.iss — Windows 客户端 + 后台服务（Inno Setup 6）
;
;  Web UI      : http://127.0.0.1:9400/index.html
;  WebSocket   : ws://127.0.0.1:8766
;
;  由 build_client_installer.ps1 调用。
; ═══════════════════════════════════════════════════════════════════════════

#ifndef StagingDir
  #define StagingDir "dist\SpeechReco-InnoStaging"
#endif
#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#ifndef ServiceName
  #define ServiceName "SpeechRecoClient"
#endif

#define AppName       "语音识别客户端"
#define AppNameEn     "Speech Reco Client"
#define AppPublisher  "SpeechRego"
#define AppURL        "https://github.com/andrew97s/speech-rego"

[Setup]
AppId={{A1B2C3D4-E5F6-7890-ABCD-EF1234567890}
AppName={#AppName}
AppVerName={#AppNameEn} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppVersion={#AppVersion}
VersionInfoVersion={#AppVersion}

DefaultDirName={autopf}\SpeechReco-Client
DefaultGroupName={#AppName}
AllowNoIcons=yes
DisableWelcomePage=no
DisableDirPage=no
DisableProgramGroupPage=yes

OutputDir=dist
OutputBaseFilename=SpeechReco-Client-Setup
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
schinese.WelcomeMsg=本向导将安装语音识别客户端（后台常驻 Windows 服务）。%n%n  • Web 控制台  http://127.0.0.1:9400/index.html%n  • WebSocket   ws://127.0.0.1:8766%n  • 本机：Sherpa 唤醒 + fsmn-vad 判停%n  • 识别：提交给 GPU 上的 Fun-ASR-Nano%n%n建议关闭正在使用该目录内文件的程序后继续。
english.WelcomeMsg=This wizard installs the speech recognition client as a Windows service.%n%n  • Web UI     http://127.0.0.1:9400/index.html%n  • WebSocket  ws://127.0.0.1:8766%n  • Local: Sherpa wake word + fsmn-vad%n  • ASR: remote Fun-ASR-Nano on the GPU server%n%nClose programs that might lock files in the target folder, then continue.

schinese.FinishedLabel=安装已完成。若已勾选「注册 Windows 服务」，服务应已启动。%nWeb 控制台：http://127.0.0.1:9400/index.html%nWebSocket：ws://127.0.0.1:8766%n日志：安装目录下的 logs 文件夹。
english.FinishedLabel=Setup finished. If you registered the Windows service, it should be running.%nWeb UI: http://127.0.0.1:9400/index.html%nWebSocket: ws://127.0.0.1:8766%nLogs are in the logs folder under the install directory.

schinese.TasksGroupSvc=服务选项
english.TasksGroupSvc=Service options
schinese.TaskInstallSvc=注册为 Windows 服务并开机自启（推荐；日志写入 logs 目录）
english.TaskInstallSvc=Register as a Windows service and start at boot (recommended; logs in logs\)

schinese.StatusRegistering=正在注册 Windows 服务…
english.StatusRegistering=Registering Windows service...

schinese.OpenWeb=打开 Web 控制台 (端口 9400)
english.OpenWeb=Open Web console (port 9400)

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
Name: "{autoprograms}\{#AppName}\Web 控制台 (9400)"; \
  Filename: "{sys}\cmd.exe"; Parameters: "/c start http://127.0.0.1:9400/index.html"; \
  IconFilename: "{sys}\shell32.dll"; IconIndex: 14
Name: "{autoprograms}\{#AppName}\启动控制台版"; \
  Filename: "{app}\start.bat"; WorkingDir: "{app}"; \
  IconFilename: "{sys}\shell32.dll"; IconIndex: 22
Name: "{autoprograms}\{#AppName}\服务日志 (logs)"; \
  Filename: "{win}\explorer.exe"; Parameters: "{app}\logs"; \
  IconFilename: "{sys}\imageres.dll"; IconIndex: 3; \
  Flags: excludefromshowinnewinstall
Name: "{autoprograms}\{#AppName}\{cm:UninstallProgram,{#AppName}}"; \
  Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; \
  Filename: "{sys}\cmd.exe"; Parameters: "/c start http://127.0.0.1:9400/index.html"; \
  IconFilename: "{sys}\shell32.dll"; IconIndex: 14; Tasks: desktopicon

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
Filename: "{sys}\cmd.exe"; \
  Parameters: "/c start http://127.0.0.1:9400/index.html"; \
  Description: "{cm:OpenWeb}"; \
  Flags: postinstall nowait skipifsilent unchecked; \
  Tasks: installservice

[UninstallRun]
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; \
  Parameters: "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File ""{app}\uninstall_service.ps1"""; \
  RunOnceId: "RemoveSpeechRecoClient"; \
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
