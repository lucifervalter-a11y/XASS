; Real Inno process, synthetic payload only. No registry, shortcuts or uninstall
; record: safe to exercise on a workstation inside a fresh temporary directory.
#ifndef FixtureSource
  #error FixtureSource required
#endif
#ifndef FixtureOutput
  #error FixtureOutput required
#endif
[Setup]
AppId=XASS-ISOLATED-ROLLBACK-FIXTURE
AppName=XASS isolated rollback fixture
AppVersion=1.0
DefaultDirName={tmp}\xass-fixture-unused
UsePreviousAppDir=no
DisableDirPage=yes
DisableProgramGroupPage=yes
Uninstallable=no
CreateUninstallRegKey=no
PrivilegesRequired=lowest
CloseApplications=no
RestartApplications=no
OutputDir={#FixtureOutput}
OutputBaseFilename=interrupted-fixture
Compression=none
SolidCompression=no
[Files]
Source: "{#FixtureSource}\first.bin"; DestDir: "{app}"; DestName: "Xass.Native.exe"; Flags: ignoreversion; AfterInstall: PauseAfterFirstFile
Source: "{#FixtureSource}\second.bin"; DestDir: "{app}\runtime"; DestName: "XASS.NativeHelper.exe"; Flags: ignoreversion
[Code]
procedure PauseAfterFirstFile;
begin
  SaveStringToFile(ExpandConstant('{app}\halfway.txt'), 'first-file-replaced', False);
  Sleep(60000);
end;
