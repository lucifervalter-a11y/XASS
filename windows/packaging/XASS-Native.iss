#ifndef XassVersion
  #define XassVersion "0.21.0"
#endif
#ifndef SourceDir
  #error SourceDir must be the verified staged payload
#endif
#ifndef OutputDir
  #error OutputDir must be specified
#endif

; Deliberately distinct from the stable Python app: test installation cannot
; replace its files, registration, startup task or updater.
[Setup]
AppId={{B4D7E8B9-9C58-4C36-A432-D114393006D8}
AppName=XASS Native Test
AppVersion={#XassVersion}
AppVerName=XASS Native Test {#XassVersion}
AppPublisher=XASS
AppPublisherURL=https://github.com/lucifervalter-a11y/XASS
AppSupportURL=https://github.com/lucifervalter-a11y/XASS/issues
DefaultDirName={localappdata}\Programs\XASS-Native-Test
DefaultGroupName=XASS Native Test
DisableDirPage=yes
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
MinVersion=10.0.17763
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UsePreviousAppDir=yes
UsePreviousTasks=yes
OutputDir={#OutputDir}
OutputBaseFilename=XASS-Native-Test-Setup
SetupIconFile=..\..\pc_client\assets\xass.ico
UninstallDisplayIcon={app}\Xass.Native.exe
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
CloseApplicationsFilter=Xass.Native.exe,XASS.NativeHelper.exe
RestartApplications=no
VersionInfoVersion={#XassVersion}.0
VersionInfoDescription=XASS Native Windows Test Installer
VersionInfoProductName=XASS Native Test
InfoBeforeFile={#SourceDir}\README-FIRST.txt

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Создать ярлык XASS Native Test на рабочем столе"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
; Only installer-managed code directories are cleared to avoid obsolete modules.
Type: filesandordirs; Name: "{app}\runtime"
Type: filesandordirs; Name: "{app}\pc_client"
Type: filesandordirs; Name: "{app}\licenses"

; No user-data deletion. Upgrades replace program files only.
; Uninstall removes tracked program files but retains both native preferences
; (%LOCALAPPDATA%\XASS.Native) and existing agent data (%LOCALAPPDATA%\XASS).
[Icons]
Name: "{group}\XASS Native Test"; Filename: "{app}\Xass.Native.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\XASS Native Test"; Filename: "{app}\Xass.Native.exe"; WorkingDir: "{app}"; Tasks: desktopicon
Name: "{group}\Удалить XASS Native Test"; Filename: "{uninstallexe}"

[Run]
Filename: "{app}\Xass.Native.exe"; Description: "Запустить XASS Native Test"; WorkingDir: "{app}"; Flags: nowait postinstall skipifsilent
