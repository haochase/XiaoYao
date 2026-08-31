[CmdletBinding()]
param(
    [string]$GatewayRoot,
    [string]$PythonPath,
    [ValidateRange(1, 65535)]
    [int]$Port = 8723
)

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
$uvicornExitCode = 0

try {
    & $python -c "import uvicorn; import companion_gateway"
    if ($LASTEXITCODE -ne 0) {
        throw "Python executable at $python does not have gateway dependencies. Install them or specify -PythonPath."
    }

    Push-Location $gatewayDirectory
    try {
        & $python -m uvicorn companion_gateway.api:create_default_app --factory --host 0.0.0.0 --port $Port
        $uvicornExitCode = $LASTEXITCODE
    } finally {
        Pop-Location
    }
} finally {
    $env:PYTHONPATH = $previousPythonPath
}

if ($uvicornExitCode -ne 0) {
    exit $uvicornExitCode
}
