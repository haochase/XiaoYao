from __future__ import annotations

from collections.abc import Mapping
import os
from pathlib import Path
import stat


_OFFICIAL_WRAPPER = """#!/bin/sh
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
export QWORK_SHIM_ROUTE="dws"
case "$PROCESSOR_ARCHITECTURE" in
  ARM64) exec "$SCRIPT_DIR/ext/cli-common-shim-windows-arm64.exe" "$@" ;;
  *)     exec "$SCRIPT_DIR/ext/cli-common-shim-windows-amd64.exe" "$@" ;;
esac
"""
_REPARSE_POINT_ATTRIBUTE = 0x400
_WINDOWS_SHIMS = {
    "AMD64": "cli-common-shim-windows-amd64.exe",
    "ARM64": "cli-common-shim-windows-arm64.exe",
}


def _is_regular_non_reparse(path: Path) -> bool:
    try:
        details = path.lstat()
    except OSError:
        return False
    attributes = getattr(details, "st_file_attributes", 0)
    return (
        stat.S_ISREG(details.st_mode)
        and not path.is_symlink()
        and attributes & _REPARSE_POINT_ATTRIBUTE == 0
    )


def _has_reparse_component(path: Path) -> bool:
    current = path
    while current != current.parent:
        try:
            details = current.lstat()
        except OSError:
            return True
        if current.is_symlink() or (
            getattr(details, "st_file_attributes", 0)
            & _REPARSE_POINT_ATTRIBUTE
        ):
            return True
        current = current.parent
    return False


def resolve_dws_launch(
    dws_path: Path,
    *,
    environ: Mapping[str, str] | None = None,
    official_bin: Path | None = None,
    processor_architecture: str | None = None,
) -> tuple[Path, dict[str, str]]:
    if not dws_path.is_absolute():
        raise ValueError("dws_path_not_absolute")
    if not _is_regular_non_reparse(dws_path) or _has_reparse_component(dws_path):
        raise ValueError("dws_path_not_regular_file")

    expected_bin = official_bin or (Path.home() / ".qwenworkcn" / "bin")
    child_env = dict(environ if environ is not None else os.environ)
    child_env.pop("COMPANION_DWS_SYNC_TOKEN", None)
    if os.path.normcase(str(dws_path)) != os.path.normcase(
        str(expected_bin / "dws")
    ):
        if dws_path.suffix.lower() != ".exe":
            raise ValueError("dws_path_not_supported")
        return dws_path, child_env

    try:
        wrapper = dws_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise ValueError("dws_official_wrapper_invalid") from None
    if wrapper.replace("\r\n", "\n") != _OFFICIAL_WRAPPER:
        raise ValueError("dws_official_wrapper_invalid")

    architecture = (
        processor_architecture
        or child_env.get("PROCESSOR_ARCHITECTURE", "")
    ).upper()
    shim_name = _WINDOWS_SHIMS.get(architecture)
    if shim_name is None:
        raise ValueError("dws_processor_architecture_unsupported")
    shim = expected_bin / "ext" / shim_name
    if not _is_regular_non_reparse(shim) or _has_reparse_component(shim):
        raise ValueError("dws_official_shim_invalid")

    child_env["QWORK_SHIM_ROUTE"] = "dws"
    return shim, child_env
