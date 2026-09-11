[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$PythonPath,
    [switch]$WhatIf
)

$ErrorActionPreference = "Stop"
$taskName = "Xiaoqian DWS Meeting Session Renewal"
$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
if ((Split-Path -Path $projectRoot -Qualifier) -ne "E:") {
    throw "The Xiaoqian project root must be on the E: drive."
}

$runnerPath = Join-Path $projectRoot "scripts\run-xiaoqian-session-renewal.ps1"
if (-not (Test-Path -LiteralPath $runnerPath -PathType Leaf)) {
    throw "Session renewal runner was not found at $runnerPath."
}
if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
    throw "Python executable was not found at $PythonPath."
}

$runnerPath = (Resolve-Path -LiteralPath $runnerPath -ErrorAction Stop).Path
$python = (Resolve-Path -LiteralPath $PythonPath -ErrorAction Stop).Path
$powershellExecutable = (
    Get-Command powershell.exe -CommandType Application -ErrorAction Stop |
        Select-Object -First 1 -ExpandProperty Source
)
$powershellExecutable = (
    Resolve-Path -LiteralPath $powershellExecutable -ErrorAction Stop
).Path
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
$principalName = $identity.Name
$principalSid = $identity.User.Value
$firstRun = (Get-Date).AddMinutes(1)
$interval = New-TimeSpan -Minutes 5
$duration = New-TimeSpan -Days 3650
$executionLimit = New-TimeSpan -Minutes 20
$taskArguments = "-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$runnerPath`" -PythonPath `"$python`""

$taskPlan = [pscustomobject]@{
    task_name = $taskName
    project_root = $projectRoot
    python_path = $python
    runner_path = $runnerPath
    interval_seconds = [int]$interval.TotalSeconds
    duration_seconds = [int64]$duration.TotalSeconds
    execution_limit_seconds = [int]$executionLimit.TotalSeconds
    multiple_instances = "IgnoreNew"
    credential_scope = "CurrentUser"
    logon_type = "Interactive"
    run_level = "Limited"
    start_when_available = $false
    will_register = $false
}

if ($WhatIf) {
    return $taskPlan
}

& $runnerPath -PythonPath $python -Check
if ($LASTEXITCODE -ne 0) {
    throw "Session renewal runner preflight failed with exit code $LASTEXITCODE."
}

$taskAction = New-ScheduledTaskAction `
    -Execute $powershellExecutable `
    -Argument $taskArguments `
    -WorkingDirectory $projectRoot
$taskTrigger = New-ScheduledTaskTrigger -Once `
    -At $firstRun `
    -RepetitionInterval $interval `
    -RepetitionDuration $duration
$taskSettings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit $executionLimit `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries
$taskPrincipal = New-ScheduledTaskPrincipal `
    -UserId $principalName `
    -LogonType Interactive `
    -RunLevel Limited

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $taskAction `
    -Trigger $taskTrigger `
    -Settings $taskSettings `
    -Principal $taskPrincipal `
    -Description "Runs the Xiaoqian meeting-session renewal tick every five minutes." `
    -Force
