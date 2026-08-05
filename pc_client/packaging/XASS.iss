#ifndef XassVersion
  #define XassVersion "0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\dist\XASS"
#endif
#ifndef OutputDir
  #define OutputDir "..\dist\installer"
#endif

#define AppGuid "{{B9284FD3-A46F-4DB8-9F1C-FA9144A290B8}"

[Setup]
AppId={#AppGuid}
AppName=XASS
AppVersion={#XassVersion}
AppVerName=XASS {#XassVersion}
AppPublisher=XASS
AppPublisherURL=https://github.com/lucifervalter-a11y/XASS
AppSupportURL=https://github.com/lucifervalter-a11y/XASS/issues
DefaultDirName={localappdata}\Programs\XASS
DefaultGroupName=XASS
DisableDirPage=yes
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir={#OutputDir}
OutputBaseFilename=XASS-Setup
SetupIconFile=..\assets\xass.ico
UninstallDisplayIcon={app}\XASS.exe
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=force
RestartApplications=no
ChangesAssociations=yes
UsePreviousTasks=yes
VersionInfoVersion={#XassVersion}.0
VersionInfoCompany=XASS
VersionInfoDescription=XASS Windows Agent Installer
VersionInfoProductName=XASS
VersionInfoProductVersion={#XassVersion}

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Создать ярлык на рабочем столе"; GroupDescription: "Ярлыки:"; Flags: unchecked
Name: "autostart"; Description: "Запускать XASS при входе в Windows"; GroupDescription: "Фоновая работа:"; Flags: checkedonce

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\XASS"; Filename: "{app}\XASS.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\XASS"; Filename: "{app}\XASS.exe"; WorkingDir: "{app}"; Tasks: desktopicon
Name: "{userstartup}\XASS"; Filename: "{app}\XASS.exe"; Parameters: "--minimized"; WorkingDir: "{app}"; Tasks: autostart

[Registry]
Root: HKCU; Subkey: "Software\Classes\.xass"; ValueType: string; ValueName: ""; ValueData: "XASS.Connection"; Flags: uninsdeletevalue
Root: HKCU; Subkey: "Software\Classes\XASS.Connection"; ValueType: string; ValueName: ""; ValueData: "Файл подключения XASS"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\XASS.Connection\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{app}\XASS.exe,0"
Root: HKCU; Subkey: "Software\Classes\XASS.Connection\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\XASS.exe"" ""%1"""

[Run]
Filename: "{app}\XASS.exe"; Description: "Запустить XASS"; WorkingDir: "{app}"; Flags: nowait postinstall skipifsilent
