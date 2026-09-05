from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping
from pathlib import Path
import subprocess
import threading
import time
from typing import Any

from companion_gateway.project.sync_models import SourceErrorType


MAX_DWS_STDOUT_BYTES = 2_097_152
_PROCESS_CLEANUP_TIMEOUT_SECONDS = 1.0
_FORBIDDEN_FLAGS = {
    "--client-id",
    "--client-secret",
    "--format",
    "--profile",
    "--token",
    "--yes",
}
_SHELL_COMPONENTS = {
    "&",
    "&&",
    "(",
    ")",
    ";",
    "<",
    "<<",
    ">",
    ">>",
    "|",
    "||",
    "2>",
    "2>&1",
}


def _is_forbidden_flag(value: str) -> bool:
    return any(
        value == flag or value.startswith(f"{flag}=")
        for flag in _FORBIDDEN_FLAGS
    )


def _reject_non_finite_constant(_value: str) -> None:
    raise ValueError("non-finite JSON constant")


class DwsReadError(Exception):
    def __init__(
        self,
        error_type: SourceErrorType | str,
        retryable: bool,
        retry_after_seconds: float | None = None,
    ) -> None:
        try:
            normalized = SourceErrorType(error_type)
        except ValueError:
            normalized = SourceErrorType.UNKNOWN
        if retry_after_seconds is not None:
            if (
                not retryable
                or not math.isfinite(retry_after_seconds)
                or retry_after_seconds < 0
            ):
                retry_after_seconds = None
        self.error_type = normalized
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds
        super().__init__(normalized.value)


def normalized_read_error(
    response: Mapping[str, object],
    *,
    fallback: SourceErrorType = SourceErrorType.INVALID_PAYLOAD,
    fallback_retryable: bool = False,
) -> DwsReadError:
    error_value = response.get("error")
    error = error_value if isinstance(error_value, Mapping) else response
    candidates = (
        error.get("error_type"),
        error.get("errorType"),
        error.get("reason"),
        error.get("type"),
        error.get("code"),
        response.get("code"),
    )
    error_type = fallback
    for candidate in candidates:
        mapped = _map_error_type(candidate)
        if mapped is not None:
            error_type = mapped
            break

    retry_value = error.get("retryable", response.get("retryable"))
    retryable = retry_value if isinstance(retry_value, bool) else fallback_retryable
    retry_after = error.get(
        "retry_after_seconds",
        response.get("retry_after_seconds"),
    )
    if isinstance(retry_after, bool) or not isinstance(retry_after, (int, float)):
        retry_after_seconds = None
    else:
        retry_after_seconds = float(retry_after)
    return DwsReadError(error_type, retryable, retry_after_seconds)


def _map_error_type(value: object) -> SourceErrorType | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower().replace("-", "_")
    try:
        return SourceErrorType(normalized)
    except ValueError:
        pass
    aliases = (
        (
            ("permission", "nopermission", "forbidden"),
            SourceErrorType.PERMISSION_DENIED,
        ),
        (("not_found", "notfound", "deleted"), SourceErrorType.NODE_NOT_FOUND),
        (("auth", "unauthorized", "token"), SourceErrorType.AUTHENTICATION_FAILED),
        (("rate", "too_many"), SourceErrorType.RATE_LIMITED),
        (("timeout", "timed_out"), SourceErrorType.NETWORK_TIMEOUT),
        (("invalid", "malformed"), SourceErrorType.INVALID_PAYLOAD),
        (("unavailable", "service_error"), SourceErrorType.PROVIDER_UNAVAILABLE),
    )
    for fragments, error_type in aliases:
        if any(fragment in normalized for fragment in fragments):
            return error_type
    return None


class DwsCommandRunner:
    def __init__(
        self,
        dws_path: Path,
        *,
        profile: str,
        timeout_seconds: float = 30.0,
        popen: Callable[..., Any] = subprocess.Popen,
    ) -> None:
        if not dws_path.is_absolute():
            raise ValueError("dws_path_not_absolute")
        if not dws_path.exists() or not dws_path.is_file():
            raise ValueError("dws_path_not_regular_file")
        if not profile.strip():
            raise ValueError("dws_profile_invalid")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("dws_timeout_invalid")
        self._dws_path = dws_path
        self._profile = profile
        self._timeout_seconds = timeout_seconds
        self._popen = popen

    def run(self, args: tuple[str, ...]) -> dict[str, object]:
        if (
            not args
            or any(not isinstance(item, str) or not item for item in args)
            or any(_is_forbidden_flag(item) for item in args)
            or any(item in _SHELL_COMPONENTS for item in args)
        ):
            raise ValueError("dws_args_invalid")
        command = [
            str(self._dws_path),
            "--profile",
            self._profile,
            *args,
            "--format",
            "json",
        ]
        run_error: DwsReadError | None = None
        try:
            process = self._popen(
                command,
                shell=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError):
            run_error = DwsReadError(SourceErrorType.PROVIDER_UNAVAILABLE, False)
        if run_error is not None:
            raise run_error

        stdout, returncode = _bounded_process_output(
            process,
            timeout_seconds=self._timeout_seconds,
        )
        payload = _parse_json_object(stdout)
        if returncode == 0:
            if payload is None:
                raise DwsReadError(SourceErrorType.INVALID_PAYLOAD, False)
            return payload
        if payload is not None:
            raise normalized_read_error(
                payload,
                fallback=SourceErrorType.PROVIDER_UNAVAILABLE,
            )
        raise DwsReadError(SourceErrorType.PROVIDER_UNAVAILABLE, False)


def _bounded_process_output(
    process: Any,
    *,
    timeout_seconds: float,
) -> tuple[bytes, int]:
    stream = getattr(process, "stdout", None)
    output: list[bytes] = []
    read_failed = threading.Event()
    reader: threading.Thread | None = None

    def read_stdout() -> None:
        try:
            value = stream.read(MAX_DWS_STDOUT_BYTES + 1)
        except Exception:
            read_failed.set()
            return
        if isinstance(value, bytes):
            output.append(value)
        else:
            read_failed.set()

    try:
        if stream is None or not callable(getattr(stream, "read", None)):
            raise DwsReadError(SourceErrorType.PROVIDER_UNAVAILABLE, False)
        started_at = time.monotonic()
        reader = threading.Thread(target=read_stdout, daemon=True)
        reader.start()
        reader.join(timeout_seconds)
        if reader.is_alive():
            raise DwsReadError(SourceErrorType.NETWORK_TIMEOUT, True)
        if read_failed.is_set() or not output:
            raise DwsReadError(SourceErrorType.PROVIDER_UNAVAILABLE, False)
        if len(output[0]) > MAX_DWS_STDOUT_BYTES:
            raise DwsReadError(SourceErrorType.INVALID_PAYLOAD, False)

        remaining = max(0.0, timeout_seconds - (time.monotonic() - started_at))
        wait_error: DwsReadError | None = None
        try:
            returncode = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            wait_error = DwsReadError(SourceErrorType.NETWORK_TIMEOUT, True)
        except (OSError, subprocess.SubprocessError):
            wait_error = DwsReadError(
                SourceErrorType.PROVIDER_UNAVAILABLE,
                False,
            )
        if wait_error is not None:
            raise wait_error
        if isinstance(returncode, bool) or not isinstance(returncode, int):
            raise DwsReadError(SourceErrorType.PROVIDER_UNAVAILABLE, False)
    except BaseException:
        _terminate_process(process, stream=stream, reader=reader)
        raise
    _close_stdout(stream)
    return output[0], returncode


def _terminate_process(
    process: Any,
    *,
    stream: Any,
    reader: threading.Thread | None,
) -> None:
    try:
        process.kill()
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        process.wait(timeout=_PROCESS_CLEANUP_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError):
        pass
    if reader is not None and reader.is_alive():
        try:
            reader.join(_PROCESS_CLEANUP_TIMEOUT_SECONDS)
        except (KeyboardInterrupt, RuntimeError):
            pass
    if reader is None or not reader.is_alive():
        _close_stdout(stream)


def _close_stdout(stream: Any) -> None:
    close = getattr(stream, "close", None)
    if not callable(close):
        return
    try:
        close()
    except (OSError, ValueError):
        pass


def _parse_json_object(value: object) -> dict[str, object] | None:
    if not isinstance(value, (str, bytes, bytearray)):
        return None
    try:
        payload = json.loads(value, parse_constant=_reject_non_finite_constant)
    except (
        json.JSONDecodeError,
        UnicodeDecodeError,
        RecursionError,
        ValueError,
    ):
        return None
    if not isinstance(payload, dict):
        return None
    return payload
