param([Parameter(Mandatory=$true)][string]$Installer, [Parameter(Mandatory=$true)][string]$PayloadManifest,
      [string]$PreviousInstaller, [string]$TargetMetadata)
$ErrorActionPreference = 'Stop'
# This exercises real per-user installer registration and uninstall. Restrict it
# to an ephemeral GitHub runner, never silently run it on someone's workstation.
if ($env:GITHUB_ACTIONS -ne 'true' -or -not $env:RUNNER_TEMP) { throw 'Installer smoke test requires an ephemeral GitHub Actions Windows runner.' }
$Installer = [IO.Path]::GetFullPath($Installer)
$Manifest = Get-Content -LiteralPath $PayloadManifest -Raw | ConvertFrom-Json
$Id = [Guid]::NewGuid().ToString('N')
$InstallDir = Join-Path $env:RUNNER_TEMP "XASS install проверка $Id"
$Markers = @()
try {
    foreach ($Folder in @('XASS', 'XASS.Native')) {
        $Directory = Join-Path $env:LOCALAPPDATA $Folder
        New-Item -ItemType Directory -Path $Directory -Force | Out-Null
        $Marker = Join-Path $Directory "native-installer-test-$Id.txt"
        [IO.File]::WriteAllText($Marker, $Id)
        $Markers += $Marker
    }
    foreach ($Pass in 1..2) {
        $Process = Start-Process -FilePath $Installer -ArgumentList @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/NOICONS', "/DIR=`"$InstallDir`"", "/LOG=`"$(Join-Path $env:RUNNER_TEMP "xass-install-$Id-$Pass.log")`"") -Wait -PassThru
        if ($Process.ExitCode -ne 0) { throw "Installer pass $Pass failed: $($Process.ExitCode)" }
        foreach ($File in $Manifest.files) {
            $Installed = Join-Path $InstallDir $File.path
            if (-not (Test-Path -LiteralPath $Installed -PathType Leaf)) { throw "Installed file missing: $($File.path)" }
            if ((Get-FileHash -LiteralPath $Installed -Algorithm SHA256).Hash.ToLowerInvariant() -ne $File.sha256) { throw "Installed file hash mismatch: $($File.path)" }
        }
        foreach ($Marker in $Markers) {
            if ([IO.File]::ReadAllText($Marker) -ne $Id) { throw 'Upgrade changed a user-data marker.' }
        }
        & (Join-Path $InstallDir 'runtime\XASS.NativeHelper.exe') --health-check
        if ($LASTEXITCODE -ne 0) { throw 'Installed companion failed its health check.' }
        & python -I -X utf8 -B (Join-Path $PSScriptRoot 'test_frozen_runtime.py') --installed $InstallDir
        if ($LASTEXITCODE -ne 0) { throw 'Installed companion role/protocol checks failed.' }
        # Catch missing WinUI/runtime startup dependencies in the installed layout.
        # This is process initialization only, not visual/UI/audio acceptance.
        $App = Start-Process -FilePath (Join-Path $InstallDir 'Xass.Native.exe') -WorkingDirectory $InstallDir -PassThru
        try {
            Start-Sleep -Seconds 8
            $App.Refresh()
            if ($App.HasExited) { throw "Installed WinUI application exited during startup: $($App.ExitCode)" }
        } finally {
            if (-not $App.HasExited) { $App.Kill($true); $App.WaitForExit() }
            $App.Dispose()
        }
    }
    & python -I -X utf8 -B (Join-Path $PSScriptRoot 'test_installed_updater.py') --installed $InstallDir --installer $Installer --report (Join-Path (Split-Path -Parent $PSScriptRoot) 'artifacts\windows-native-smoke.json')
    if ($LASTEXITCODE -ne 0) { throw 'Real installed updater/rollback smoke failed.' }
    if ($PreviousInstaller) {
        if (-not $TargetMetadata) { throw 'Migration smoke requires verified target metadata.' }
        $Process = Start-Process -FilePath (Join-Path $InstallDir 'unins000.exe') -ArgumentList @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART') -WindowStyle Hidden -Wait -PassThru
        if ($Process.ExitCode -ne 0) { throw 'Pre-migration uninstall failed.' }
        $Process = Start-Process -FilePath ([IO.Path]::GetFullPath($PreviousInstaller)) -ArgumentList @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/NOICONS', "/DIR=`"$InstallDir`"") -WindowStyle Hidden -Wait -PassThru
        if ($Process.ExitCode -ne 0) { throw 'Previous native channel install failed.' }
        & python -I -X utf8 -B (Join-Path $PSScriptRoot 'test_installed_updater.py') --installed $InstallDir --installer $Installer --target-metadata $TargetMetadata --report (Join-Path (Split-Path -Parent $PSScriptRoot) 'artifacts\windows-native-migration-smoke.json')
        if ($LASTEXITCODE -ne 0) { throw 'Native test-to-stable migration/rollback smoke failed.' }
    }
    $Uninstaller = Join-Path $InstallDir 'unins000.exe'
    $Process = Start-Process -FilePath $Uninstaller -ArgumentList @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART') -Wait -PassThru
    if ($Process.ExitCode -ne 0) { throw 'Native test uninstall failed.' }
    if (Test-Path -LiteralPath (Join-Path $InstallDir 'Xass.Native.exe')) { throw 'Native executable survived uninstall.' }
    foreach ($Marker in $Markers) {
        if ([IO.File]::ReadAllText($Marker) -ne $Id) { throw 'Uninstall changed a user-data marker.' }
    }
    Write-Output 'Installer smoke checks passed: fresh install, same-version upgrade, full payload hashes, bundled runtime, WinUI startup, uninstall and preserved data.'
} finally {
    foreach ($Marker in $Markers) {
        if (Test-Path -LiteralPath $Marker) { Remove-Item -LiteralPath $Marker -Force }
    }
}
