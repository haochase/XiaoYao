import hashlib
import json
import subprocess
from types import SimpleNamespace


from tools.ascend_runtime_probe import CommandResult, main, probe_runtime, run_command


def _package_versions(monkeypatch, values: dict[str, str | None]) -> None:
    import tools.ascend_runtime_probe as runtime_probe

    def fake_version(name: str) -> str:
        value = values[name]
        if value is None:
            raise runtime_probe.metadata.PackageNotFoundError(name)
        return value

    monkeypatch.setattr(runtime_probe.metadata, "version", fake_version)


def _all_packages(monkeypatch) -> None:
    _package_versions(
        monkeypatch,
        {"torch": "2.6.0", "torch-npu": "2.6.0", "transformers": "4.52.0"},
    )


def test_probe_requires_npu_without_retaining_raw_command_output(
    tmp_path, monkeypatch
) -> None:
    _all_packages(monkeypatch)

    def fake_runner(command: list[str]) -> CommandResult:
        assert command == ["npu-smi", "info"]
        return CommandResult(
            available=True,
            exit_code=0,
            stdout_sha256=hashlib.sha256(b"raw-secret-output").hexdigest(),
        )

    result = probe_runtime(tmp_path, require_npu=True, command_runner=fake_runner)

    assert result["status"] == "ready"
    assert result["npu"]["available"] is True
    assert result["npu"]["exit_code"] == 0
    assert "raw-secret-output" not in json.dumps(result)
    assert len(result["npu"]["stdout_sha256"]) == 64


def test_probe_blocks_when_npu_smi_is_missing(tmp_path, monkeypatch) -> None:
    _all_packages(monkeypatch)

    result = probe_runtime(
        tmp_path,
        require_npu=True,
        command_runner=lambda _: CommandResult(False, None, None),
    )

    assert result["status"] == "blocked"
    assert result["npu"] == {
        "available": False,
        "exit_code": None,
        "stdout_sha256": None,
        "error_class": None,
    }


def test_probe_blocks_for_nonzero_npu_smi_exit(tmp_path, monkeypatch) -> None:
    _all_packages(monkeypatch)

    result = probe_runtime(
        tmp_path,
        require_npu=True,
        command_runner=lambda _: CommandResult(True, 3, "a" * 64),
    )

    assert result["status"] == "blocked"
    assert result["npu"]["exit_code"] == 3


def test_probe_blocks_for_an_unwritable_workspace(tmp_path, monkeypatch) -> None:
    _all_packages(monkeypatch)
    workspace_file = tmp_path / "workspace-file"
    workspace_file.write_text("not a directory", encoding="utf-8")

    result = probe_runtime(
        workspace_file,
        require_npu=False,
        command_runner=lambda _: CommandResult(False, None, None),
    )

    assert result["status"] == "blocked"
    assert result["workspace"]["writable"] is False


def test_probe_reports_missing_package_versions_in_local_mode(
    tmp_path, monkeypatch
) -> None:
    _package_versions(
        monkeypatch,
        {"torch": "2.6.0", "torch-npu": None, "transformers": None},
    )

    result = probe_runtime(
        tmp_path,
        require_npu=False,
        command_runner=lambda _: CommandResult(False, None, None),
    )

    assert result["status"] == "ready"
    assert result["packages"] == {
        "torch": "2.6.0",
        "torch-npu": None,
        "transformers": None,
    }


def test_probe_allows_local_mode_without_an_npu(tmp_path, monkeypatch) -> None:
    _all_packages(monkeypatch)

    result = probe_runtime(
        tmp_path,
        require_npu=False,
        command_runner=lambda _: CommandResult(False, None, None),
    )

    assert result["status"] == "ready"
    assert result["npu"]["available"] is False


def test_run_command_hashes_stdout_and_discards_its_value(monkeypatch) -> None:
    import tools.ascend_runtime_probe as runtime_probe

    monkeypatch.setattr(runtime_probe.shutil, "which", lambda _: "/usr/bin/npu-smi")
    monkeypatch.setattr(
        runtime_probe.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="raw-secret-output"),
    )

    result = run_command(["npu-smi", "info"])

    assert result == CommandResult(
        available=True,
        exit_code=0,
        stdout_sha256=hashlib.sha256(b"raw-secret-output").hexdigest(),
    )


def test_run_command_reports_timeout_by_exception_class(monkeypatch) -> None:
    import tools.ascend_runtime_probe as runtime_probe

    monkeypatch.setattr(runtime_probe.shutil, "which", lambda _: "/usr/bin/npu-smi")

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs["timeout"])

    monkeypatch.setattr(runtime_probe.subprocess, "run", timeout)

    result = run_command(["npu-smi", "info"])

    assert result == CommandResult(True, None, None, "TimeoutExpired")


def test_cli_returns_nonzero_when_required_npu_is_unavailable(monkeypatch, tmp_path) -> None:
    import tools.ascend_runtime_probe as runtime_probe

    monkeypatch.setattr(
        runtime_probe,
        "probe_runtime",
        lambda *args, **kwargs: {
            "schema_version": 1,
            "status": "blocked",
            "system": {},
            "workspace": {},
            "npu": {},
            "packages": {},
            "environment": {},
        },
    )

    assert main(["--workspace", str(tmp_path), "--require-npu", "--json"]) == 1
