param(
    [string]$PythonPath,
    [string]$ModelPath
)
$ErrorActionPreference = 'Stop'
$sourceRoot = Split-Path $PSScriptRoot -Parent
$executable = Join-Path $sourceRoot 'artifacts\windows-native\Xass.Native.exe'
if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) {
    throw 'Build the native x64 application first; see windows/VOICE-ASSISTANT.md.'
}
if (-not $PythonPath) {
    $candidate = Join-Path $env:LOCALAPPDATA 'XASS\transcription\python\python.exe'
    if (Test-Path -LiteralPath $candidate -PathType Leaf) { $PythonPath = $candidate }
}
if (-not $ModelPath) {
    $snapshots = Join-Path $env:USERPROFILE '.cache\huggingface\hub\models--Systran--faster-whisper-small\snapshots'
    if (Test-Path -LiteralPath $snapshots -PathType Container) {
        $found = Get-ChildItem -LiteralPath $snapshots -Directory | Sort-Object Name | Where-Object {
            Test-Path -LiteralPath (Join-Path $_.FullName 'model.bin') -PathType Leaf
        } | Select-Object -First 1
        if ($found) { $ModelPath = $found.FullName }
    }
}
$previousPython = $env:XASS_ASSISTANT_PYTHON
$previousModel = $env:XASS_ASSISTANT_MODEL
try {
    if ($PythonPath) { $env:XASS_ASSISTANT_PYTHON = $PythonPath }
    if ($ModelPath) { $env:XASS_ASSISTANT_MODEL = $ModelPath }
    # Interactive launch requested by the person running this script.
    & $executable
} finally {
    $env:XASS_ASSISTANT_PYTHON = $previousPython
    $env:XASS_ASSISTANT_MODEL = $previousModel
}
