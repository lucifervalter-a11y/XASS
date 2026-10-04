param(
    [string]$PythonPath = "python.exe",
    [string]$IsccPath,
    [string]$Revision = "local-build",
    [string]$OutputDirectory
)

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $PSScriptRoot
if ($Revision -notmatch '^([0-9a-f]{40}|local-build)$') { throw 'Use a full commit SHA or local-build.' }
if (-not $OutputDirectory) { $OutputDirectory = Join-Path $RepoRoot 'artifacts\windows-installer' }
$OutputDirectory = [IO.Path]::GetFullPath($OutputDirectory)
$Version = [string](Get-Content (Join-Path $RepoRoot 'pc_client\version.json') -Raw | ConvertFrom-Json).version
if ($Version -notmatch '^\d+\.\d+\.\d+$') { throw 'Expected three-component numeric installer version.' }
$SeedPython = (Get-Command $PythonPath -ErrorAction Stop).Source
& $SeedPython -I -c 'import sys, struct; raise SystemExit(0 if sys.version_info[:2] == (3,12) and struct.calcsize("P") == 8 else 1)'
if ($LASTEXITCODE -ne 0) { throw 'Build requires trusted Python 3.12 x64.' }
Get-Command dotnet -ErrorAction Stop | Out-Null
if (-not $IsccPath) {
    $IsccPath = @(
        (Get-Command ISCC.exe -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source),
        "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
    ) | Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Leaf) } | Select-Object -First 1
}
if (-not $IsccPath -or -not (Test-Path -LiteralPath $IsccPath -PathType Leaf)) {
    throw 'Install Inno Setup 6 from its official distribution before building. This script does not install tools.'
}

# New build-only directories prevent old files or local user configuration from
# being swept into a package. No user profile, source checkout or prior build is deleted.
$BuildRoot = Join-Path $RepoRoot ('artifacts\native-installer-build-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $BuildRoot | Out-Null
$Venv = Join-Path $BuildRoot 'venv'
& $SeedPython -I -m venv $Venv
if ($LASTEXITCODE -ne 0) { throw 'Could not create clean installer build environment.' }
$Python = Join-Path $Venv 'Scripts\python.exe'
& $Python -I -m pip install --disable-pip-version-check -r (Join-Path $RepoRoot 'pc_client\requirements.txt') -r (Join-Path $RepoRoot 'pc_client\voice-requirements.txt') -r (Join-Path $RepoRoot 'pc_client\build-requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Pinned runtime/build dependencies could not be installed.' }
& $Python -I -X utf8 -B -m unittest discover -s (Join-Path $PSScriptRoot 'tests') -v
if ($LASTEXITCODE -ne 0) { throw 'Native bridge source tests failed.' }
& dotnet run --project (Join-Path $PSScriptRoot 'Appearance.Tests\Appearance.Tests.csproj') -c Release
if ($LASTEXITCODE -ne 0) { throw 'Appearance persistence/contrast tests failed.' }
& dotnet run --project (Join-Path $PSScriptRoot 'ModelDiscovery.Tests\ModelDiscovery.Tests.csproj') -c Release
if ($LASTEXITCODE -ne 0) { throw 'Local model discovery tests failed.' }
& dotnet run --project (Join-Path $PSScriptRoot 'NativeUpdate.Tests\NativeUpdate.Tests.csproj') -c Release
if ($LASTEXITCODE -ne 0) { throw 'Native updater HTTP deadline tests failed.' }
& dotnet run --project (Join-Path $PSScriptRoot 'AutomaticUpdate.Tests\AutomaticUpdate.Tests.csproj') -c Release
if ($LASTEXITCODE -ne 0) { throw 'Automatic updater consent and rejection safety tests failed.' }

& dotnet run --project (Join-Path $PSScriptRoot 'PairingFlow.Tests\PairingFlow.Tests.csproj') -c Release
if ($LASTEXITCODE -ne 0) { throw 'Pairing import isolation and cancellation tests failed.' }

$Native = Join-Path $BuildRoot 'native'
& dotnet publish (Join-Path $PSScriptRoot 'Xass.Native\Xass.Native.csproj') -c Release -r win-x64 -p:RuntimeIdentifiers=win-x64 -p:Platform=x64 -o $Native
if ($LASTEXITCODE -ne 0) { throw 'WinUI publish failed.' }
$Metadata = Join-Path $BuildRoot 'build-info.json'
$BuildInfo = @{ version = $Version; revision = $Revision; distribution = 'native-test'; local_build = ($Revision -eq 'local-build') } | ConvertTo-Json
[IO.File]::WriteAllText($Metadata, $BuildInfo, (New-Object Text.UTF8Encoding($false)))
$Dist = Join-Path $BuildRoot 'frozen'
$FreezeArguments = @(
    '-I', '-m', 'PyInstaller', '--noconfirm', '--clean', '--onedir', '--console',
    '--name', 'XASS.NativeHelper', '--icon', (Join-Path $RepoRoot 'pc_client\assets\xass.ico'),
    '--paths', $PSScriptRoot, '--paths', (Join-Path $RepoRoot 'pc_client'),
    '--distpath', $Dist, '--workpath', (Join-Path $BuildRoot 'pyinstaller-work'),
    '--specpath', (Join-Path $BuildRoot 'pyinstaller-spec'),
    '--add-data', "$(Join-Path $RepoRoot 'pc_client\version.json');.",
    '--add-data', "$Metadata;.",
    '--add-data', "$(Join-Path $RepoRoot 'pc_client\transcribe_runner.py');.",
    '--add-data', "$(Join-Path $RepoRoot 'pc_client\assets\xass.ico');assets",
    '--add-data', "$(Join-Path $RepoRoot 'pc_client\assets\xass-icon.png');assets"
)
foreach ($Module in @('bridge', 'desktop_bridge', 'desktop_music_service', 'background_agent', 'native_updater', 'installer_process', 'assistant_bridge', 'background_voice_bridge', 'background_voice', 'native_music_ownership', 'native_agent_identity', 'voice_capture', 'voice_assistant', 'client_agent')) {
    $FreezeArguments += @('--hidden-import', $Module)
}
foreach ($Module in @('cryptography', 'miniaudio', 'faster_whisper', 'ctranslate2', 'av', 'tokenizers', 'numpy')) {
    $FreezeArguments += @('--collect-all', $Module)
}
$FreezeArguments += (Join-Path $PSScriptRoot 'native_host.py')
$PreviousHubOffline, $PreviousHubTelemetry = $env:HF_HUB_OFFLINE, $env:HF_HUB_DISABLE_TELEMETRY
try {
    # Dependency import analysis needs no remote model/CLI catalogs.
    $env:HF_HUB_OFFLINE = '1'; $env:HF_HUB_DISABLE_TELEMETRY = '1'
    & $Python @FreezeArguments
    $FreezeExitCode = $LASTEXITCODE
} finally {
    $env:HF_HUB_OFFLINE = $PreviousHubOffline; $env:HF_HUB_DISABLE_TELEMETRY = $PreviousHubTelemetry
}
if ($FreezeExitCode -ne 0) { throw 'Frozen native companion build failed.' }
$Companion = Join-Path $Dist 'XASS.NativeHelper'
& (Join-Path $Companion 'XASS.NativeHelper.exe') --health-check
if ($LASTEXITCODE -ne 0) { throw 'Bundled Python/runtime health check failed; refusing to package.' }
$Licenses = Join-Path $BuildRoot 'licenses'
& $Python -I -B (Join-Path $PSScriptRoot 'collect_runtime_licenses.py') $Licenses
if ($LASTEXITCODE -ne 0) { throw 'Runtime notice collection failed.' }

# Include available .NET and WinAppSDK package notices as well. Their full
# dependency identities also remain in the published .deps.json manifest.
$NugetRoot = if ($env:NUGET_PACKAGES) { $env:NUGET_PACKAGES } else { Join-Path $env:USERPROFILE '.nuget\packages' }
foreach ($Package in @('microsoft.netcore.app.runtime.win-x64', 'microsoft.windowsappsdk', 'microsoft.windowsappsdk.runtime', 'microsoft.windows.sdk.buildtools')) {
    $PackageRoot = Join-Path $NugetRoot $Package
    if (Test-Path -LiteralPath $PackageRoot) {
        Get-ChildItem -LiteralPath $PackageRoot -File -Recurse | Where-Object { $_.Name -match '^(LICENSE|NOTICE|ThirdPartyNotices)' } | ForEach-Object {
            $Relative = [IO.Path]::GetRelativePath($NugetRoot, $_.FullName)
            $Destination = Join-Path $Licenses $Relative
            New-Item -ItemType Directory -Path (Split-Path -Parent $Destination) -Force | Out-Null
            Copy-Item -LiteralPath $_.FullName -Destination $Destination
        }
    }
}
$Payload = Join-Path $BuildRoot 'payload'
& $Python -I -B (Join-Path $PSScriptRoot 'stage_native_package.py') --native $Native --companion $Companion --destination $Payload --licenses $Licenses --revision $Revision
if ($LASTEXITCODE -ne 0) { throw 'Native payload verification failed.' }
New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
& $IsccPath "/DXassVersion=$Version" "/DSourceDir=$Payload" "/DOutputDir=$OutputDirectory" (Join-Path $PSScriptRoot 'packaging\XASS-Native.iss')
if ($LASTEXITCODE -ne 0) { throw 'Inno Setup failed.' }
$Installer = Join-Path $OutputDirectory 'XASS-Native-Test-Setup.exe'
if (-not (Test-Path -LiteralPath $Installer -PathType Leaf)) { throw 'Installer executable not produced.' }
$Hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $Installer).Hash.ToLowerInvariant()
"$Hash  XASS-Native-Test-Setup.exe" | Set-Content -LiteralPath "$Installer.sha256" -Encoding ascii
$InstallerMetadata = @{ version = $Version; revision = $Revision; sha256 = $Hash; bytes = (Get-Item -LiteralPath $Installer).Length;
   distribution = 'native-test'; python_bundled = $true; whisper_runtime_bundled = $true; whisper_model_bundled = $false } |
    ConvertTo-Json
[IO.File]::WriteAllText((Join-Path $OutputDirectory 'XASS-Native-Test-Setup.json'), $InstallerMetadata, (New-Object Text.UTF8Encoding($false)))
$UpdateMetadata = @{ version = $Version; revision = $Revision; sha256 = $Hash; size = (Get-Item -LiteralPath $Installer).Length;
   filename = 'XASS-Native-Test-Setup.exe'; distribution = 'native-test' } |
    ConvertTo-Json
[IO.File]::WriteAllText((Join-Path $OutputDirectory 'native-test-update.json'), $UpdateMetadata, (New-Object Text.UTF8Encoding($false)))
Copy-Item -LiteralPath (Join-Path $Payload 'payload-manifest.json') -Destination $OutputDirectory -Force
Write-Output "Built complete single-download installer: $Installer (SHA256 $Hash)"
