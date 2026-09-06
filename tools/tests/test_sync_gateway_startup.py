import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"


def read_script(name: str) -> str:
    return (SCRIPTS / name).read_text(encoding="utf-8")


def powershell_literal(value: Path | str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def make_fake_sync_project(tmp_path: Path) -> tuple[Path, Path, Path]:
    project = tmp_path / "sync-project"
    scripts = project / "scripts"
    tools = project / "tools"
    scripts.mkdir(parents=True)
    tools.mkdir()
    shutil.copy2(SCRIPTS / "run-xiaoyao-sync.ps1", scripts)
    shutil.copy2(SCRIPTS / "register-xiaoyao-sync-task.ps1", scripts)
    (tools / "dws_sync_runtime.py").write_text("# test double\n", encoding="ascii")
    python = tmp_path / "fake-python.cmd"
    python.write_text(
        "@echo off\r\n"
        "> \"%TASK3_RECORD%\" (\r\n"
        "echo runtime=%~1\r\n"
        "echo command=%~2\r\n"
        "echo temp=%TEMP%\r\n"
        "echo tmp=%TMP%\r\n"
        "echo cwd=%CD%\r\n"
        ")\r\n"
        "exit /b %TASK3_EXIT%\r\n",
        encoding="ascii",
    )
    return project, scripts, python


def mock_windows_identity(script: Path) -> None:
    source = script.read_text(encoding="utf-8")
    identity_call = "[System.Security.Principal.WindowsIdentity]::GetCurrent()"
    assert identity_call in source
    script.write_text(
        source.replace(identity_call, "(Get-MockedWindowsIdentity)"),
        encoding="utf-8",
    )


def test_sync_runner_defaults_to_loopback_and_dedicated_port() -> None:
    source = read_script("run_xiaoyao_sync.py")

    assert 'default="127.0.0.1"' in source
    assert "default=8731" in source
    assert "companion_gateway.sync_api:create_default_sync_app" in source
    assert "proxy_headers=False" in source


def test_device_runner_does_not_reference_sync_api() -> None:
    assert "sync_api" not in read_script("run_xiaoyao_gateway.py")


def test_shared_runner_helper_prepares_paths_without_loading_an_application() -> None:
    source = read_script("gateway_runner_common.py")

    assert "def _prepare_import_paths" in source
    assert "uvicorn" not in source
    assert "companion_gateway" not in source
    assert "load_environment_file" not in source


def test_sync_runner_check_reports_the_fixed_listener_contract() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "run_xiaoyao_sync.py"),
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
    assert result["host"] == "127.0.0.1"
    assert result["port"] == 8731
    assert result["gateway_root"] == str((ROOT / "gateway").resolve())
    assert result["source_available"] is True


def test_sync_runner_rejects_non_loopback_host() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "run_xiaoyao_sync.py"),
            "--gateway-root",
            str(ROOT / "gateway"),
            "--host",
            "0.0.0.0",
            "--check",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert "--host must be exactly 127.0.0.1" in completed.stderr


def test_sync_runner_rejects_non_dedicated_port() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "run_xiaoyao_sync.py"),
            "--gateway-root",
            str(ROOT / "gateway"),
            "--port",
            "8723",
            "--check",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert "--port must be exactly 8731" in completed.stderr


def test_sync_powershell_runner_uses_fixed_private_runtime_contract() -> None:
    source = read_script("run-xiaoyao-sync.ps1")
    normalized = source.upper()

    assert "[Parameter(Mandatory)]" in source
    assert "[string]$PythonPath" in source
    assert "[switch]$Check" in source
    assert "$GatewayRoot" not in source
    assert "$Port" not in source
    assert 'Join-Path $projectRoot "tools\\dws_sync_runtime.py"' in source
    assert 'Join-Path $projectRoot ".private\\dws-runtime"' in source
    assert '$command = if ($Check) { "check" } else { "serve" }' in source
    assert "$env:TEMP = $tempRoot" in source
    assert "$env:TMP = $tempRoot" in source
    assert "Push-Location $projectRoot" in source
    assert "finally" in source
    assert "UVICORN" not in normalized
    assert "GET-CONTENT" not in normalized
    assert ".ENV" not in normalized


def test_sync_powershell_runner_checks_with_e_drive_temp_and_restores_scope(
    tmp_path: Path,
) -> None:
    project, scripts, python = make_fake_sync_project(tmp_path)
    runtime_root = project / ".private" / "dws-runtime"
    runtime_root.mkdir(parents=True)
    record = tmp_path / "runner.txt"
    harness = tmp_path / "runner-harness.ps1"
    harness.write_text(
        f'''$ErrorActionPreference = "Stop"
$env:TASK3_RECORD = {powershell_literal(record)}
$env:TASK3_EXIT = "0"
$env:TEMP = "E:\\original-temp"
$env:TMP = "E:\\original-tmp"
$before = (Get-Location).Path
& {powershell_literal(scripts / "run-xiaoyao-sync.ps1")} -PythonPath {powershell_literal(python)} -Check
[pscustomobject]@{{
    before = $before
    after = (Get-Location).Path
    temp = $env:TEMP
    tmp = $env:TMP
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
    invocation = record.read_text(encoding="utf-8").lower()
    expected_temp = str(runtime_root / "tmp").lower()
    assert "command=check" in invocation
    assert f"temp={expected_temp}" in invocation
    assert f"tmp={expected_temp}" in invocation
    assert f"cwd={str(project).lower()}" in invocation
    assert result["before"] == result["after"]
    assert result["temp"] == "E:\\original-temp"
    assert result["tmp"] == "E:\\original-tmp"


def test_sync_powershell_runner_serves_and_propagates_exit_code(tmp_path: Path) -> None:
    project, scripts, python = make_fake_sync_project(tmp_path)
    (project / ".private" / "dws-runtime").mkdir(parents=True)
    record = tmp_path / "serve.txt"
    environment = os.environ.copy()
    environment.update(TASK3_RECORD=str(record), TASK3_EXIT="37")

    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-File",
            str(scripts / "run-xiaoyao-sync.ps1"),
            "-PythonPath",
            str(python),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert completed.returncode == 37
    assert "command=serve" in record.read_text(encoding="utf-8").lower()


def test_sync_powershell_runner_stops_when_temp_directory_creation_fails(
    tmp_path: Path,
) -> None:
    project, scripts, python = make_fake_sync_project(tmp_path)
    (project / ".private" / "dws-runtime").mkdir(parents=True)
    marker = tmp_path / "python-called.txt"
    harness = tmp_path / "temp-failure-harness.ps1"
    harness.write_text(
        f'''$env:TASK3_RECORD = {powershell_literal(marker)}
$env:TASK3_EXIT = "0"
function New-Item {{ Write-Error "simulated temp creation failure" }}
& {powershell_literal(scripts / "run-xiaoyao-sync.ps1")} -PythonPath {powershell_literal(python)}
''',
        encoding="utf-8",
    )

    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-File", str(harness)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert not marker.exists()


def test_sync_task_registration_whatif_is_a_zero_side_effect_plan(
    tmp_path: Path,
) -> None:
    project, scripts, python = make_fake_sync_project(tmp_path)
    registration = scripts / "register-xiaoyao-sync-task.ps1"
    mock_windows_identity(registration)
    marker = tmp_path / "scheduled-task-called.txt"
    harness = tmp_path / "whatif-harness.ps1"
    harness.write_text(
        f'''$ErrorActionPreference = "Stop"
$env:TASK3_RECORD = {powershell_literal(marker)}
$env:TASK3_EXIT = "0"
function Get-MockedWindowsIdentity {{
    [pscustomobject]@{{ Name = "TEST\\anchor"; User = [pscustomobject]@{{ Value = "S-1-5-21-123" }} }}
}}
function New-ScheduledTaskAction {{ Set-Content {powershell_literal(marker)} "action"; throw "called" }}
function New-ScheduledTaskTrigger {{ Set-Content {powershell_literal(marker)} "trigger"; throw "called" }}
function New-ScheduledTaskSettingsSet {{ Set-Content {powershell_literal(marker)} "settings"; throw "called" }}
function New-ScheduledTaskPrincipal {{ Set-Content {powershell_literal(marker)} "principal"; throw "called" }}
function Register-ScheduledTask {{ Set-Content {powershell_literal(marker)} "register"; throw "called" }}
& {powershell_literal(registration)} -PythonPath {powershell_literal(python)} -WhatIf |
    ConvertTo-Json -Depth 5 -Compress
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
    assert result["task_name"] == "XiaoYao DWS Sync Gateway"
    assert result["project_root"] == str(project.resolve())
    assert result["python_path"] == str(python.resolve())
    assert result["runner_path"] == str((scripts / "run-xiaoyao-sync.ps1").resolve())
    assert result["temp_root"] == str((project / ".private" / "dws-runtime" / "tmp").resolve())
    assert result["endpoint"] == "http://127.0.0.1:8731"
    assert result["credential_scope"] == "CurrentUser"
    assert result["principal_name"] == "TEST\\anchor"
    assert result["principal_sid"] == "S-1-5-21-123"
    assert result["preflight_performed"] is False
    assert result["will_register"] is False
    assert result["device_task_touched"] is False
    assert result["action"]["working_directory"] == str(project.resolve())
    assert result["principal"]["run_level"] == "Limited"
    assert result["trigger"]["user"] == "TEST\\anchor"
    assert result["settings"]["restart_interval"] == "PT1M"
    assert not marker.exists()
    assert not (project / ".private").exists()


def test_sync_task_registration_whatif_uses_real_windows_identity(
    tmp_path: Path,
) -> None:
    project, scripts, python = make_fake_sync_project(tmp_path)
    harness = tmp_path / "real-identity-whatif.ps1"
    harness.write_text(
        f'''$ErrorActionPreference = "Stop"
& {powershell_literal(scripts / "register-xiaoyao-sync-task.ps1")} `
    -PythonPath {powershell_literal(python)} -WhatIf |
    ConvertTo-Json -Depth 5 -Compress
''',
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-File",
            str(harness),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    assert completed.stderr == ""
    result = json.loads(completed.stdout)
    assert result["principal_name"]
    assert result["principal_sid"].startswith("S-")
    assert result["principal"]["user_id"] == result["principal_name"]
    assert result["trigger"]["user"] == result["principal_name"]
    assert not (project / ".private").exists()


def test_sync_task_registration_preflights_before_complete_task_contract(
    tmp_path: Path,
) -> None:
    project, scripts, python = make_fake_sync_project(tmp_path)
    (project / ".private" / "dws-runtime").mkdir(parents=True)
    registration = scripts / "register-xiaoyao-sync-task.ps1"
    mock_windows_identity(registration)
    record = tmp_path / "preflight.txt"
    harness = tmp_path / "register-harness.ps1"
    harness.write_text(
        f'''$ErrorActionPreference = "Stop"
$env:TASK3_RECORD = {powershell_literal(record)}
$env:TASK3_EXIT = "0"
function Get-MockedWindowsIdentity {{
    [pscustomobject]@{{ Name = "TEST\\anchor"; User = [pscustomobject]@{{ Value = "S-1-5-21-123" }} }}
}}
function New-ScheduledTaskAction {{
    param([string]$Execute, [string]$Argument, [string]$WorkingDirectory)
    [pscustomobject]@{{ execute = $Execute; argument = $Argument; working_directory = $WorkingDirectory }}
}}
function New-ScheduledTaskTrigger {{
    param([switch]$AtLogOn, [string]$User)
    [pscustomobject]@{{ at_log_on = [bool]$AtLogOn; user = $User }}
}}
function New-ScheduledTaskSettingsSet {{
    param([switch]$StartWhenAvailable, [int]$RestartCount, [TimeSpan]$RestartInterval,
        [string]$MultipleInstances, [TimeSpan]$ExecutionTimeLimit,
        [switch]$AllowStartIfOnBatteries, [switch]$DontStopIfGoingOnBatteries)
    [pscustomobject]@{{ start_when_available = [bool]$StartWhenAvailable; restart_count = $RestartCount;
        restart_minutes = $RestartInterval.TotalMinutes; multiple_instances = $MultipleInstances;
        execution_seconds = $ExecutionTimeLimit.TotalSeconds;
        allow_battery = [bool]$AllowStartIfOnBatteries; dont_stop_battery = [bool]$DontStopIfGoingOnBatteries }}
}}
function New-ScheduledTaskPrincipal {{
    param([string]$UserId, [string]$LogonType, [string]$RunLevel)
    [pscustomobject]@{{ user_id = $UserId; logon_type = $LogonType; run_level = $RunLevel }}
}}
function Register-ScheduledTask {{
    param([string]$TaskName, $Action, $Trigger, $Settings, $Principal, [string]$Description, [switch]$Force)
    [pscustomobject]@{{ task_name = $TaskName; action = $Action; trigger = $Trigger; settings = $Settings;
        principal = $Principal; force = [bool]$Force; preflight_seen = (Test-Path -LiteralPath {powershell_literal(record)}) }}
}}
& {powershell_literal(registration)} -PythonPath {powershell_literal(python)} |
    ConvertTo-Json -Depth 6 -Compress
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
    expected_arguments = (
        '-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden '
        f'-ExecutionPolicy Bypass -File "{scripts / "run-xiaoyao-sync.ps1"}" '
        f'-PythonPath "{python}"'
    )
    assert result["task_name"] == "XiaoYao DWS Sync Gateway"
    assert result["preflight_seen"] is True
    assert result["force"] is True
    assert Path(result["action"]["execute"]).name.lower() == "powershell.exe"
    assert result["action"]["argument"] == expected_arguments
    assert result["action"]["working_directory"] == str(project.resolve())
    assert result["principal"] == {
        "user_id": "TEST\\anchor",
        "logon_type": "Interactive",
        "run_level": "Limited",
    }
    assert result["trigger"] == {"at_log_on": True, "user": "TEST\\anchor"}
    assert result["settings"] == {
        "start_when_available": True,
        "restart_count": 3,
        "restart_minutes": 1,
        "multiple_instances": "IgnoreNew",
        "execution_seconds": 0,
        "allow_battery": True,
        "dont_stop_battery": True,
    }


def test_sync_task_registration_stops_before_registering_when_preflight_fails(
    tmp_path: Path,
) -> None:
    project, scripts, python = make_fake_sync_project(tmp_path)
    (project / ".private" / "dws-runtime").mkdir(parents=True)
    registration = scripts / "register-xiaoyao-sync-task.ps1"
    mock_windows_identity(registration)
    preflight_record = tmp_path / "failed-preflight.txt"
    registration_marker = tmp_path / "registration-called.txt"
    harness = tmp_path / "failed-preflight-harness.ps1"
    harness.write_text(
        f'''$ErrorActionPreference = "Stop"
$env:TASK3_RECORD = {powershell_literal(preflight_record)}
$env:TASK3_EXIT = "19"
function Get-MockedWindowsIdentity {{
    [pscustomobject]@{{ Name = "TEST\\anchor"; User = [pscustomobject]@{{ Value = "S-1-5-21-123" }} }}
}}
function Register-ScheduledTask {{
    Set-Content {powershell_literal(registration_marker)} "called"
    throw "Register-ScheduledTask must not run after a failed preflight"
}}
& {powershell_literal(registration)} -PythonPath {powershell_literal(python)}
''',
        encoding="utf-8",
    )

    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-File", str(harness)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert preflight_record.exists()
    assert not registration_marker.exists()


def test_sync_task_scripts_do_not_expose_secrets_or_touch_device_task() -> None:
    source = read_script("register-xiaoyao-sync-task.ps1")
    normalized = source.upper()

    assert "[SYSTEM.SECURITY.PRINCIPAL.WINDOWSIDENTITY]::GETCURRENT()" in normalized
    assert "TOKEN" not in normalized
    assert "8723" not in source
    assert "START-SCHEDULEDTASK" not in normalized
    assert "STOP-SCHEDULEDTASK" not in normalized
    assert "UNREGISTER-SCHEDULEDTASK" not in normalized


def test_sync_runtime_check_is_read_only_and_checks_device_route_isolation() -> None:
    source = read_script("check-xiaoyao-sync-runtime.ps1")
    normalized = source.upper()

    assert "GET-NETTCPCONNECTION" in normalized
    assert "http://127.0.0.1:8731/health" in source
    assert "http://127.0.0.1:8731/ready" in source
    assert "http://127.0.0.1:8723/openapi.json" in source
    assert "/v1/projects/{project_id}/sync" in source
    assert "CONVERTFROM-JSON" in normalized
    assert "GET-CONTENT" not in normalized
    assert ".ENV" not in normalized
    assert "WRITE-HOST" not in normalized
    assert "WRITE-OUTPUT" not in normalized
