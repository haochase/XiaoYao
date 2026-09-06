[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$PythonPath,
    [switch]$Check
)

$ErrorActionPreference = "Stop"
$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
if ((Split-Path -Path $projectRoot -Qualifier) -ne "E:") {
    throw "The DWS sync project root must be on the E: drive."
}

$runtimeRunner = Join-Path $projectRoot "tools\dws_sync_runtime.py"
$runtimeRoot = Join-Path $projectRoot ".private\dws-runtime"
$tempRoot = Join-Path $runtimeRoot "tmp"
if (-not (Test-Path -LiteralPath $runtimeRunner -PathType Leaf)) {
    throw "DWS sync runtime was not found at $runtimeRunner."
}
if (-not (Test-Path -LiteralPath $runtimeRoot -PathType Container)) {
    throw "DWS sync runtime directory was not found at $runtimeRoot."
}
if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
    throw "Python executable was not found at $PythonPath."
}

$python = (Resolve-Path -LiteralPath $PythonPath -ErrorAction Stop).Path
$runtimeRunner = (Resolve-Path -LiteralPath $runtimeRunner -ErrorAction Stop).Path
$command = if ($Check) { "check" } else { "serve" }
New-Item -ItemType Directory -Path $tempRoot -Force | Out-Null

$previousTemp = $env:TEMP
$previousTmp = $env:TMP
$locationPushed = $false
$runnerExitCode = 0

try {
    $env:TEMP = $tempRoot
    $env:TMP = $tempRoot
    Push-Location $projectRoot
    $locationPushed = $true
    & $python $runtimeRunner $command
    $runnerExitCode = $LASTEXITCODE
} finally {
    if ($locationPushed) {
        Pop-Location
    }
    if ($null -eq $previousTemp) {
        Remove-Item -LiteralPath Env:TEMP -ErrorAction SilentlyContinue
    } else {
        $env:TEMP = $previousTemp
    }
    if ($null -eq $previousTmp) {
        Remove-Item -LiteralPath Env:TMP -ErrorAction SilentlyContinue
    } else {
        $env:TMP = $previousTmp
    }
}

if ($runnerExitCode -ne 0) {
    exit $runnerExitCode
}
