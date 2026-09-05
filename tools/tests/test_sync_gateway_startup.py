import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"


def read_script(name: str) -> str:
    return (SCRIPTS / name).read_text(encoding="utf-8")


def test_sync_runner_defaults_to_loopback_and_dedicated_port() -> None:
    source = read_script("run_xiaoyao_sync.py")

    assert 'default="127.0.0.1"' in source
    assert "default=8731" in source
    assert "companion_gateway.sync_api:create_default_sync_app" in source


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


def test_sync_powershell_runner_delegates_without_reading_environment_files() -> None:
    source = read_script("run-xiaoyao-sync.ps1")
    normalized = source.upper()

    assert "[int]$Port = 8731" in source
    assert 'Join-Path $PSScriptRoot "run_xiaoyao_sync.py"' in source
    assert "--host 127.0.0.1 --port $Port" in source
    assert "UVICORN" not in normalized
    assert "GET-CONTENT" not in normalized
    assert ".ENV" not in normalized


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
