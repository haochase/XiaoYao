[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$PythonPath,
    [switch]$WhatIf
)

$ErrorActionPreference = "Stop"
$taskName = "XiaoYao DWS Sync Gateway"
$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
if ((Split-Path -Path $projectRoot -Qualifier) -ne "E:") {
    throw "The DWS sync project root must be on the E: drive."
}

$runnerPath = Join-Path $projectRoot "scripts\run-xiaoyao-sync.ps1"
if (-not (Test-Path -LiteralPath $runnerPath -PathType Leaf)) {
    throw "DWS sync runner was not found at $runnerPath."
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
$powershellExecutable = (Resolve-Path -LiteralPath $powershellExecutable -ErrorAction Stop).Path
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
$principalName = $identity.Name
$principalSid = $identity.User.Value
$tempRoot = Join-Path (Join-Path $projectRoot ".private\dws-runtime") "tmp"
$taskArguments = "-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$runnerPath`" -PythonPath `"$python`""

$taskPlan = [pscustomobject]@{
    task_name = $taskName
    project_root = $projectRoot
    python_path = $python
    runner_path = $runnerPath
    temp_root = $tempRoot
    endpoint = "http://127.0.0.1:8731"
    credential_scope = "CurrentUser"
    principal_name = $principalName
    principal_sid = $principalSid
    action = [pscustomobject]@{
        execute = $powershellExecutable
        arguments = $taskArguments
        working_directory = $projectRoot
    }
    principal = [pscustomobject]@{
        user_id = $principalName
        sid = $principalSid
        logon_type = "Interactive"
        run_level = "Limited"
    }
    trigger = [pscustomobject]@{
        type = "AtLogOn"
        user = $principalName
    }
    settings = [pscustomobject]@{
        start_when_available = $true
        restart_count = 3
        restart_interval = "PT1M"
        multiple_instances = "IgnoreNew"
        execution_time_limit = "PT0S"
        allow_start_if_on_batteries = $true
        dont_stop_if_going_on_batteries = $true
    }
    preflight_performed = $false
    will_register = $false
    device_task_touched = $false
}

if ($WhatIf) {
    return $taskPlan
}

& $runnerPath -PythonPath $python -Check
if ($LASTEXITCODE -ne 0) {
    throw "DWS sync runner preflight failed with exit code $LASTEXITCODE."
}

$taskAction = New-ScheduledTaskAction `
    -Execute $powershellExecutable `
    -Argument $taskArguments `
    -WorkingDirectory $projectRoot
$taskTrigger = New-ScheduledTaskTrigger -AtLogOn -User $principalName
$taskSettings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
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
    -Description "Starts the XiaoYao DWS sync gateway after current-user sign-in." `
    -Force
