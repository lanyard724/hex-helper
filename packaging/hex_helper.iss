; Inno Setup 6 script for HexHelper Commercial Windows Installer (kept in sync with hex_helper_installer.spec)
#ifndef MyAppVersion
  #define MyAppVersion "1.0.0"
#endif

#define MyAppName "HexHelper"
#define MyAppPublisher "HexHelper"
#define MyAppExeName "HexHelper.exe"

[Setup]
AppId={{8D4F61C2-9A2E-4E7C-8B41-4F3C2E9D1A01}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist
OutputBaseFilename=HexHelper_Setup_v{#MyAppVersion}_win64
SetupIconFile=..\src\hex_helper\assets\hex_helper.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2/normal
SolidCompression=yes
WizardStyle=modern

[Languages]
; Official Inno Setup 6 Chinese Simplified translation (fall back to compiler:Default.isl if omitted in portable setups)
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务:"; Flags: unchecked

[Files]
Source: "..\dist\HexHelper\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Parameters: "--preflight-check-only"; Flags: runhidden waituntilterminated
Filename: "{app}\{#MyAppExeName}"; Description: "立即启动 {#MyAppName}"; Flags: nowait postinstall skipifsilent
