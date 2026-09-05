from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from collections.abc import Callable, Mapping
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if __name__ == "__main__":
    sys.path.insert(0, str(ROOT / "gateway" / "src"))
    sys.path.insert(0, str(ROOT))

from tools.dws_sync.launch import resolve_dws_launch
from tools.dws_sync.manifest import DwsManifest
from tools.dws_sync.runner import (
    MAX_DWS_STDOUT_BYTES,
    _bounded_process_output,
    _parse_json_object,
)
from tools.dws_sync.runtime import CONFIG_NAME, TaskConfig, read_object


def _kind(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    return "number"


def summarize_bytes(raw: bytes) -> dict[str, object]:
    bom = "none"
    for prefix, name in (
        (b"\xff\xfe\x00\x00", "utf32le"), (b"\x00\x00\xfe\xff", "utf32be"),
        (b"\xef\xbb\xbf", "utf8"), (b"\xff\xfe", "utf16le"), (b"\xfe\xff", "utf16be"),
    ):
        if raw.startswith(prefix):
            bom = name
            break
    try:
        raw.decode("utf-8")
        utf8_valid = True
    except UnicodeError:
        utf8_valid = False
    kind = "empty" if not raw.strip() else "invalid"
    nested_kind = "not_applicable"
    try:
        value = json.loads(raw)
        kind = _kind(value)
        if isinstance(value, str):
            try:
                nested_kind = _kind(json.loads(value))
            except (ValueError, RecursionError):
                nested_kind = "invalid"
    except (ValueError, UnicodeError, RecursionError):
        pass
    return {
        "bytes": len(raw), "bom": bom, "utf8_valid": utf8_valid,
        "ansi_present": b"\x1b[" in raw, "nul_present": b"\0" in raw,
        "line_count": len(raw.splitlines()), "json_kind": kind,
        "nested_json_kind": nested_kind,
        "production_parser_accepts": _parse_json_object(raw) is not None,
    }


def capture(
    command: list[str],
    environment: Mapping[str, str],
    *,
    popen: Callable = subprocess.Popen,
) -> dict[str, object]:
    process = popen(
        command, shell=False, env=dict(environment),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    errors: list[bytes] = []

    def read_errors() -> None:
        try:
            errors.append(process.stderr.read(MAX_DWS_STDOUT_BYTES + 1))
        except (OSError, ValueError):
            pass

    reader = threading.Thread(target=read_errors, daemon=True)
    reader.start()
    try:
        raw, code = _bounded_process_output(process, timeout_seconds=30.0)
        reader.join(1.0)
        if reader.is_alive() or not errors or len(errors[0]) > MAX_DWS_STDOUT_BYTES:
            raise ValueError("diagnostic_stderr_unavailable")
        return {"returncode": code, "stdout": summarize_bytes(raw),
                "stderr": summarize_bytes(errors[0])}
    finally:
        if reader.is_alive():
            try:
                process.kill()
                process.wait(timeout=1.0)
            except (OSError, subprocess.SubprocessError):
                pass
            reader.join(1.0)
        if not reader.is_alive():
            process.stderr.close()


def main() -> int:
    if not os.environ.get("QODERWORK_SOURCE_CHAT_ID"):
        print(json.dumps({"status": "blocked", "error_type": "qwen_session_required"}))
        return 1
    try:
        config = TaskConfig.model_validate(read_object(ROOT / CONFIG_NAME))
        projects = DwsManifest.load(config.manifest).projects
        project = next(item for item in projects if item.project_id == config.project)
        if len(project.sources) != 1 or project.sources[0].source_type.value != "document":
            raise ValueError("single_document_required")
        executable, environment = resolve_dws_launch(config.dws)
        command = [str(executable), "--profile", project.profile, "doc", "info",
                   "--node", project.sources[0].source_id, "--format", "json"]
        result = capture(command, environment)
    except Exception:
        print(json.dumps({"status": "blocked", "error_type": "stdout_diagnostic_failed"}))
        return 1
    print(json.dumps({"status": "diagnosed", "command": "doc_info", **result}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
