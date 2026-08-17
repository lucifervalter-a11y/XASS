param(
    [switch]$SkipToolInstall
)

$ErrorActionPreference = "Stop"
$ClientRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent $ClientRoot
$VersionData = Get-Content (Join-Path $ClientRoot "version.json") -Raw | ConvertFrom-Json
$Version = [string]$VersionData.version
$Revision = (& git -C $RepoRoot rev-parse HEAD 2>$null)
if (-not $Revision) { $Revision = "local-build" }

$BuildVenv = Join-Path $ClientRoot ".build-venv"
$BuildPython = Join-Path $BuildVenv "Scripts\python.exe"
function Test-XassPython([string]$Candidate) {
    if (-not $Candidate -or -not (Test-Path $Candidate)) { return $false }
    try {
        & $Candidate -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" 2>$null
        return $LASTEXITCODE -eq 0
    } catch {
        return $false
    }
}
$BuildPythonReady = Test-XassPython $BuildPython
if (-not $BuildPythonReady) {
    if (Test-Path $BuildVenv) {
        Remove-Item -LiteralPath $BuildVenv -Recurse -Force
    }
    $SeedPython = $null
    $SeedCandidates = @(
        (Join-Path $RepoRoot ".venv\Scripts\python.exe"),
        (Join-Path $ClientRoot ".venv\Scripts\python.exe"),
        (Get-Command py.exe -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -ErrorAction SilentlyContinue),
        (Get-Command python.exe -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -ErrorAction SilentlyContinue)
    ) | Where-Object { $_ -and (Test-Path $_) }
    foreach ($Candidate in $SeedCandidates) {
        if (Test-XassPython $Candidate) {
            $SeedPython = $Candidate
            break
        }
    }
    if (-not $SeedPython) { throw "Python 3.12 is required to build XASS" }
    & $SeedPython -m venv $BuildVenv
}
& $BuildPython -m pip install --disable-pip-version-check --quiet -r (Join-Path $ClientRoot "requirements.txt") -r (Join-Path $ClientRoot "build-requirements.txt")

$PackagingRoot = Join-Path $ClientRoot "packaging"
$BuildInfo = Join-Path $PackagingRoot "build-info.generated.json"
$VersionInfo = Join-Path $PackagingRoot "version-info.generated.txt"
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$BuildInfoJson = @{
    version = $Version
    revision = $Revision
    distribution = "installer"
} | ConvertTo-Json
[System.IO.File]::WriteAllText($BuildInfo, $BuildInfoJson, $Utf8NoBom)

$VersionParts = @($Version.Split('.') | ForEach-Object { [int]$_ })
while ($VersionParts.Count -lt 4) { $VersionParts += 0 }
$VersionTuple = "($($VersionParts[0]), $($VersionParts[1]), $($VersionParts[2]), $($VersionParts[3]))"
$VersionInfoText = @"
VSVersionInfo(
  ffi=FixedFileInfo(filevers=$VersionTuple, prodvers=$VersionTuple, mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[StringFileInfo([StringTable('040904B0', [
    StringStruct('CompanyName', 'XASS'),
    StringStruct('FileDescription', 'XASS Windows Agent'),
    StringStruct('FileVersion', '$Version'),
    StringStruct('InternalName', 'XASS'),
    StringStruct('LegalCopyright', 'Copyright XASS'),
    StringStruct('OriginalFilename', 'XASS.exe'),
    StringStruct('ProductName', 'XASS'),
    StringStruct('ProductVersion', '$Version')
  ])]), VarFileInfo([VarStruct('Translation', [1033, 1200])])]
)
"@
[System.IO.File]::WriteAllText($VersionInfo, $VersionInfoText, $Utf8NoBom)

$DistRoot = Join-Path $ClientRoot "dist"
$WorkRoot = Join-Path $ClientRoot "build"
& $BuildPython -m PyInstaller `
    --noconfirm `
    --clean `
    --onedir `
    --name XASS `
    --hide-console hide-early `
    --icon (Join-Path $ClientRoot "assets\xass.ico") `
    --version-file $VersionInfo `
    --add-data "$(Join-Path $ClientRoot 'version.json');." `
    --add-data "$BuildInfo;." `
    --add-data "$(Join-Path $ClientRoot 'assets\xass.ico');assets" `
    --add-data "$(Join-Path $ClientRoot 'assets\xass-icon.png');assets" `
    --collect-data tkinterdnd2 `
    --hidden-import pystray._win32 `
    --distpath $DistRoot `
    --workpath $WorkRoot `
    --specpath $WorkRoot `
    (Join-Path $ClientRoot "desktop_app.py")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }

$UpdaterWorkRoot = Join-Path $WorkRoot "updater"
& $BuildPython -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --name XASSUpdater `
    --hide-console hide-early `
    --icon (Join-Path $ClientRoot "assets\xass.ico") `
    --version-file $VersionInfo `
    --distpath (Join-Path $DistRoot "XASS") `
    --workpath $UpdaterWorkRoot `
    --specpath $UpdaterWorkRoot `
    (Join-Path $ClientRoot "installer_helper.py")
if ($LASTEXITCODE -ne 0) { throw "XASSUpdater build failed with exit code $LASTEXITCODE" }

$MigratorWorkRoot = Join-Path $WorkRoot "migrator"
& $BuildPython -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --name XASSMigrator `
    --hide-console hide-early `
    --icon (Join-Path $ClientRoot "assets\xass.ico") `
    --version-file $VersionInfo `
    --distpath (Join-Path $DistRoot "XASS") `
    --workpath $MigratorWorkRoot `
    --specpath $MigratorWorkRoot `
    (Join-Path $ClientRoot "legacy_migration.py")
if ($LASTEXITCODE -ne 0) { throw "XASSMigrator build failed with exit code $LASTEXITCODE" }

$IsccCandidates = @(
    (Get-Command ISCC.exe -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -ErrorAction SilentlyContinue),
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
) | Where-Object { $_ -and (Test-Path $_) }
$Iscc = $IsccCandidates | Select-Object -First 1
if (-not $Iscc -and -not $SkipToolInstall) {
    $Winget = Get-Command winget.exe -ErrorAction SilentlyContinue
    if (-not $Winget) { throw "Inno Setup is missing and winget is unavailable" }
        & $Winget.Source install --id JRSoftware.InnoSetup --exact --source winget --silent --accept-package-agreements --accept-source-agreements
    $Iscc = @(
        "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
    ) | Where-Object { Test-Path $_ } | Select-Object -First 1
}
if (-not $Iscc) { throw "Inno Setup compiler ISCC.exe was not found" }

$InstallerOut = Join-Path $DistRoot "installer"
New-Item -ItemType Directory -Path $InstallerOut -Force | Out-Null
& $Iscc "/DXassVersion=$Version" "/DSourceDir=$(Join-Path $DistRoot 'XASS')" "/DOutputDir=$InstallerOut" (Join-Path $PackagingRoot "XASS.iss")
if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed with exit code $LASTEXITCODE" }

$Installer = Join-Path $InstallerOut "XASS-Setup.exe"
$Hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $Installer).Hash.ToLowerInvariant()
$InstallerMetadata = @{
    version = $Version
    revision = $Revision
    sha256 = $Hash
    size = (Get-Item -LiteralPath $Installer).Length
    built_at = [DateTime]::UtcNow.ToString("o")
} | ConvertTo-Json
[System.IO.File]::WriteAllText((Join-Path $InstallerOut "XASS-Setup.json"), $InstallerMetadata, $Utf8NoBom)

Write-Output "Built $Installer"
