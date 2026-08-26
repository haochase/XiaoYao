from pathlib import Path
import json
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"


def read_script(name: str) -> str:
    return (SCRIPTS / name).read_text(encoding="utf-8")


def powershell_literal(value: Path | str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def test_gateway_runner_uses_gateway_directory_and_lan_listener() -> None:
    runner = read_script("run-xiaoyao-gateway.ps1")

    assert "[string]$GatewayRoot" in runner
    assert "[string]$PythonPath" in runner
    assert "[int]$Port = 8723" in runner
    assert "$gatewayDirectory" in runner
    assert "if ([string]::IsNullOrWhiteSpace($GatewayRoot))" in runner
    assert "Get-Command python" in runner
    assert "import uvicorn; import companion_gateway" in runner
    assert "does not have gateway dependencies" in runner
    assert "Push-Location $gatewayDirectory" in runner
    assert "--host 0.0.0.0 --port $Port" in runner
    assert "companion_gateway.api:create_default_app --factory" in runner
    assert '$vendorSitePackages = Join-Path $projectRoot ".vendor\\python-site"' in runner
    assert "Test-Path -LiteralPath $vendorSitePackages -PathType Container" in runner


def test_task_registration_supports_dry_run_logon_start_and_restart_policy() -> None:
    registration = read_script("register-xiaoyao-gateway-task.ps1")

    assert "[string]$GatewayRoot" in registration
    assert "[string]$PythonPath" in registration
    assert '[string]$TaskName = "XiaoYao Voice Gateway"' in registration
    assert "[int]$Port = 8723" in registration
    assert "[switch]$WhatIf" in registration
    assert "if ([string]::IsNullOrWhiteSpace($GatewayRoot))" in registration
    assert "if ($WhatIf)" in registration
    assert "Get-Command python" in registration
    assert '$pythonRunnerPath = Join-Path $PSScriptRoot "run_xiaoyao_gateway.py"' in registration
    assert "-Execute $python" in registration
    assert "-WorkingDirectory $gatewayDirectory" in registration
    assert "-GatewayRoot" in registration
    assert "-PythonPath" in registration
    assert "--port $Port" in registration
    assert "--gateway-root" in registration
    assert '$sourceDirectory = Join-Path $gatewayDirectory "src"' in registration
    assert "import uvicorn; import companion_gateway" in registration
    assert "does not have gateway dependencies" in registration
    assert "New-ScheduledTaskTrigger -AtLogOn" in registration
    assert "New-ScheduledTaskSettingsSet" in registration
    assert "-RestartCount 3" in registration
    assert 'RestartInterval = "PT1M"' in registration
    assert "-RestartInterval (New-TimeSpan -Minutes 1)" in registration
    assert "Register-ScheduledTask" in registration


def test_runtime_check_uses_local_health_without_reading_or_printing_secrets() -> None:
    runtime_check = read_script("check-xiaoyao-gateway-runtime.ps1")
    normalized = runtime_check.upper()

    assert '[string]$ExpectedHost = "0.0.0.0"' in runtime_check
    assert "GET-NETTCPCONNECTION" in normalized
    assert "GETHOSTADDRESSES" in normalized
    assert "INVOKE-WEBREQUEST" in normalized
    assert "expected_host_resolved" in runtime_check
    assert "expected_host_listening" in runtime_check
    assert "GET-CONTENT" not in normalized
    assert "COMPANION_MIMO_API_KEY" not in normalized
    assert "COMPANION_OTA_DEVICE_TOKENS" not in normalized


def test_task_registration_passes_timespan_restart_interval_to_scheduled_tasks(
    tmp_path: Path,
) -> None:
    harness = tmp_path / "register-task-harness.ps1"
    harness.write_text(
        f'''$ErrorActionPreference = "Stop"
$registerScript = {powershell_literal(SCRIPTS / "register-xiaoyao-gateway-task.ps1")}
$gatewayRoot = {powershell_literal(ROOT / "gateway")}
$python = {powershell_literal(Path(sys.executable))}

function New-ScheduledTaskAction {{
    param([string]$Execute, [string]$Argument, [string]$WorkingDirectory)
    $global:taskExecute = $Execute
    $global:taskArgument = $Argument
    $global:taskWorkingDirectory = $WorkingDirectory
    [pscustomobject]@{{
        Execute = $Execute
        Argument = $Argument
        WorkingDirectory = $WorkingDirectory
    }}
}}

function New-ScheduledTaskTrigger {{
    param([switch]$AtLogOn, [string]$User)
    [pscustomobject]@{{ AtLogOn = $AtLogOn; User = $User }}
}}

function New-ScheduledTaskSettingsSet {{
    param(
        [int]$RestartCount,
        [TimeSpan]$RestartInterval,
        [switch]$StartWhenAvailable
    )
    $global:restartIntervalType = $RestartInterval.GetType().FullName
    [pscustomobject]@{{ RestartCount = $RestartCount; RestartInterval = $RestartInterval }}
}}

function New-ScheduledTaskPrincipal {{
    param([string]$UserId, [string]$LogonType, [string]$RunLevel)
    [pscustomobject]@{{ UserId = $UserId }}
}}

function Register-ScheduledTask {{
    param(
        [string]$TaskName,
        $Action,
        $Trigger,
        $Settings,
        $Principal,
        [string]$Description,
        [switch]$Force
    )
    $global:registerCalls += 1
}}

$global:registerCalls = 0
& $registerScript -GatewayRoot $gatewayRoot -PythonPath $python | Out-Null
[pscustomobject]@{{
    restart_interval_type = $global:restartIntervalType
    register_calls = $global:registerCalls
    task_execute = $global:taskExecute
    task_argument = $global:taskArgument
    task_working_directory = $global:taskWorkingDirectory
}} | ConvertTo-Json -Compress
''',
        encoding="utf-8",
    )

    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-File", str(harness)],
        check=True,
        capture_output=True,
        text=True,
    )

    result = json.loads(completed.stdout)
    assert result == {
        "restart_interval_type": "System.TimeSpan",
        "register_calls": 1,
        "task_execute": str(Path(sys.executable).resolve()),
        "task_argument": (
            f'"{SCRIPTS / "run_xiaoyao_gateway.py"}" '
            f'--gateway-root "{ROOT / "gateway"}" --port 8723'
        ),
        "task_working_directory": str((ROOT / "gateway").resolve()),
    }


def test_python_gateway_runner_check_uses_project_paths_without_starting_server() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "run_xiaoyao_gateway.py"),
            "--gateway-root",
            str(ROOT / "gateway"),
            "--check",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    result = json.loads(completed.stdout)
    assert result["status"] == "ready"
    assert result["gateway_root"] == str((ROOT / "gateway").resolve())
    assert result["source_available"] is True


def test_gateway_runner_propagates_uvicorn_exit_code(tmp_path: Path) -> None:
    fake_python = tmp_path / "fake-python.cmd"
    fake_python.write_text(
        "@echo off\r\n"
        'if "%~1"=="-c" exit /b 0\r\n'
        'if "%~1"=="-m" if "%~2"=="uvicorn" exit /b 37\r\n'
        "exit /b 99\r\n",
        encoding="ascii",
    )

    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-File",
            str(SCRIPTS / "run-xiaoyao-gateway.ps1"),
            "-GatewayRoot",
            str(ROOT / "gateway"),
            "-PythonPath",
            str(fake_python),
            "-Port",
            "8723",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 37
