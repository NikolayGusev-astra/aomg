; AOMG installer: AOMG.exe + mcp-proxy.exe рядом, config.example.yaml,
; ярлыки в меню Пуск, автозапуск записи в реестре (опционально, чекбокс).
; Собирается: ISCC.exe installer.iss  (пути к exe — dist/)

#define MyAppName "AOMG"
#define MyAppVersion "0.1.1"
#define MyAppPublisher "NikolayGusev-astra"
#define MyAppURL "https://github.com/NikolayGusev-astra/aomg"
#define MyAppExeName "AOMG.exe"

[Setup]
AppId={{8A4E2C1D-7B33-4F0E-9A21-AOMG00000001}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}/issues
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
; всегда per-user: HKCU-autostart честный, config/index рядом с exe
; пишутся без запроса админа; {autopf} при этом = {localappdata}\Programs
PrivilegesRequired=lowest
; трей-приложение: после установки сразу запускать не заставляем,
; юзер сам решает (страница финальная с чекбоксом)
LicenseFile=LICENSE
OutputBaseFilename=AOMG-setup-{#MyAppVersion}
OutputDir=dist\installer
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#MyAppExeName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; \
    GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "autostart"; Description: "Запускать AOMG при входе в Windows"; \
    Flags: unchecked

[Files]
Source: "dist\AOMG.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "dist\mcp-proxy.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "config.example.yaml"; DestDir: "{app}"; Flags: ignoreversion
Source: "README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "LICENSE"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; \
    ValueType: string; ValueName: "AOMG"; \
    ValueData: """{app}\{#MyAppExeName}"" --config ""{app}\config.yaml"""; \
    Tasks: autostart; Flags: uninsdeletevalue

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; \
    Flags: nowait postinstall skipifsilent unchecked
