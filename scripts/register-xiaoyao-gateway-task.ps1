[CmdletBinding()]
param(
    [string]$GatewayRoot,
    [string]$PythonPath,
    [string]$TaskName = "XiaoYao Voice Gateway",
    [ValidateRange(1, 65535)]
    [int]$Port = 8723,
    [switch]$WhatIf
)

if ([string]::IsNullOrWhiteSpace($GatewayRoot)) {
    $GatewayRoot = Join-Path $PSScriptRoot "..\gateway"
}
if (-not (Test-Path -LiteralPath $GatewayRoot -PathType Container)) {
    throw "Gateway root was not found at $GatewayRoot. Specify -GatewayRoot explicitly."
}
$gatewayDirectory = (Resolve-Path -LiteralPath $GatewayRoot -ErrorAction Stop).Path
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
$runnerPath = (Resolve-Path (Join-Path $PSScriptRoot "run-xiaoyao-gateway.ps1")).Path
$taskPlan = [pscustomobject]@{
    TaskName = $TaskName
    Trigger = "AtLogOn"
    RestartCount = 3
    RestartInterval = "PT1M"
    Runner = $runnerPath
    GatewayRoot = $gatewayDirectory
    PythonPath = $python
    Port = $Port
}

if ($WhatIf) {
    return $taskPlan
}

$sourceDirectory = Join-Path $gatewayDirectory "src"
$previousPythonPath = $env:PYTHONPATH
$env:PYTHONPATH = if ($previousPythonPath) {
    "$sourceDirectory;$previousPythonPath"
} else {
    $sourceDirectory
}

try {
    & $python -c "import uvicorn; import companion_gateway"
    if ($LASTEXITCODE -ne 0) {
        throw "Python executable at $python does not have gateway dependencies. Install them or specify -PythonPath."
    }

    $taskAction = New-ScheduledTaskAction `
        -Execute "powershell.exe" `
        -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$runnerPath`" -GatewayRoot `"$gatewayDirectory`" -PythonPath `"$python`" -Port $Port"
    $taskTrigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $taskSettings = New-ScheduledTaskSettingsSet `
        -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 1) `
        -StartWhenAvailable
    $taskPrincipal = New-ScheduledTaskPrincipal `
        -UserId $env:USERNAME `
        -LogonType Interactive `
        -RunLevel Limited

    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $taskAction `
        -Trigger $taskTrigger `
        -Settings $taskSettings `
        -Principal $taskPrincipal `
        -Description "Starts the XiaoYao gateway after user sign-in and restarts it after failures." `
        -Force | Out-Null
} finally {
    $env:PYTHONPATH = $previousPythonPath
}

$taskPlan
