import argparse
import hashlib
import importlib.metadata as metadata
import json
import os
import platform
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class CommandResult:
    available: bool
    exit_code: int | None
    stdout_sha256: str | None
    error_class: str | None = None


def run_command(command: list[str]) -> CommandResult:
    executable = shutil.which(command[0])
    if executable is None:
        return CommandResult(False, None, None)

    try:
        completed = subprocess.run(
            [executable, *command[1:]],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return CommandResult(True, None, None, type(exc).__name__)

    stdout = completed.stdout
    if isinstance(stdout, str):
        stdout = stdout.encode("utf-8", errors="replace")
    stdout_sha256 = hashlib.sha256(stdout or b"").hexdigest()
    return CommandResult(True, completed.returncode, stdout_sha256)


def _workspace_facts(workspace: Path) -> dict[str, int | bool]:
    try:
        with tempfile.NamedTemporaryFile(dir=workspace, delete=True):
            pass
        writable = True
    except OSError:
        writable = False

    try:
        free_bytes = shutil.disk_usage(workspace).free
    except OSError:
        free_bytes = 0
    return {"writable": writable, "free_bytes": free_bytes}


def _package_version(package: str) -> str | None:
    try:
        return metadata.version(package)
    except metadata.PackageNotFoundError:
        return None


def probe_runtime(
    workspace: Path,
    *,
    require_npu: bool,
    command_runner: Callable[..., CommandResult] = run_command,
) -> dict[str, object]:
    workspace_facts = _workspace_facts(workspace)
    npu = command_runner(["npu-smi", "info"])
    packages = {
        "torch": _package_version("torch"),
        "torch-npu": _package_version("torch-npu"),
        "transformers": _package_version("transformers"),
    }
    npu_ready = not require_npu or (npu.available and npu.exit_code == 0)
    ready = (
        workspace_facts["writable"]
        and npu_ready
    )
    return {
        "schema_version": 1,
        "status": "ready" if ready else "blocked",
        "system": {
            "os": platform.system(),
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
        "workspace": workspace_facts,
        "npu": asdict(npu),
        "packages": packages,
        "environment": {
            "ASCEND_HOME_PATH": bool(os.environ.get("ASCEND_HOME_PATH")),
            "ASCEND_TOOLKIT_HOME": bool(os.environ.get("ASCEND_TOOLKIT_HOME")),
            "LD_LIBRARY_PATH": bool(os.environ.get("LD_LIBRARY_PATH")),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Report sanitized Ascend runtime readiness."
    )
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--require-npu", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    result = probe_runtime(args.workspace, require_npu=args.require_npu)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(result["status"])
    return 0 if result["status"] == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
