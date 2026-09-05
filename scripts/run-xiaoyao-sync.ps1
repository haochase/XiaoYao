[CmdletBinding()]
param(
    [string]$GatewayRoot,
    [string]$PythonPath,
    [ValidateRange(1, 65535)]
    [int]$Port = 8731
)

if ($Port -ne 8731) {
    throw "Sync port must be exactly 8731."
}
if ([string]::IsNullOrWhiteSpace($GatewayRoot)) {
    $GatewayRoot = Join-Path $PSScriptRoot "..\gateway"
}
if (-not (Test-Path -LiteralPath $GatewayRoot -PathType Container)) {
    throw "Gateway root was not found at $GatewayRoot. Specify -GatewayRoot explicitly."
}
$gatewayDirectory = (Resolve-Path -LiteralPath $GatewayRoot).Path

if ([string]::IsNullOrWhiteSpace($PythonPath)) {
    $pythonCommand = Get-Command python -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -eq $pythonCommand) {
        throw "Python was not found. Specify -PythonPath with the gateway Python executable."
    }
    $PythonPath = $pythonCommand.Source
}
if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
    throw "Python executable was not found at $PythonPath. Specify -PythonPath explicitly."
}
$python = (Resolve-Path -LiteralPath $PythonPath).Path

$sourceDirectory = Join-Path $gatewayDirectory "src"
$projectRoot = Split-Path $PSScriptRoot -Parent
$vendorSitePackages = Join-Path $projectRoot ".vendor\python-site"
$previousPythonPath = $env:PYTHONPATH
$pythonPathEntries = @($sourceDirectory)
if (Test-Path -LiteralPath $vendorSitePackages -PathType Container) {
    $pythonPathEntries += $vendorSitePackages
}
if ($previousPythonPath) {
    $pythonPathEntries += $previousPythonPath
}
$env:PYTHONPATH = $pythonPathEntries -join ";"
$pythonRunnerPath = Join-Path $PSScriptRoot "run_xiaoyao_sync.py"
$runnerExitCode = 0

try {
    & $python $pythonRunnerPath --gateway-root $gatewayDirectory --host 127.0.0.1 --port $Port
    $runnerExitCode = $LASTEXITCODE
} finally {
    $env:PYTHONPATH = $previousPythonPath
}

if ($runnerExitCode -ne 0) {
    exit $runnerExitCode
}
