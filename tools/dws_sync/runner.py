from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from functools import partial
import os
from pathlib import Path
import queue
import subprocess
import threading
import time
from typing import Any

from companion_gateway.project.sync_models import SourceErrorType
from tools.dws_sync.core_trust import (
    AuthenticodeDescriptor,
    TrustedDwsCore,
    hold_trusted_dws_core,
    prepare_core_temp_directory,
    read_authenticode,
)
from tools.dws_sync.launch import resolve_dws_launch


MAX_DWS_STDOUT_BYTES = 2_097_152
MAX_DWS_STDERR_BYTES = 65_536
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
_CORE_ENV_ALLOWLIST = frozenset(
    {
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "USERPROFILE",
        "HOMEDRIVE",
        "HOMEPATH",
        "APPDATA",
        "LOCALAPPDATA",
        "PROGRAMDATA",
        "TEMP",
        "TMP",
        "PROCESSOR_ARCHITECTURE",
        "NUMBER_OF_PROCESSORS",
    }
)


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


class HostHandoffRequired(ValueError):
    def __init__(self) -> None:
        super().__init__("host_handoff_required")


def normalized_read_error(
    response: Mapping[str, object],
    *,
    fallback: SourceErrorType = SourceErrorType.INVALID_PAYLOAD,
    fallback_retryable: bool = False,
) -> DwsReadError:
    error_value = response.get("error")
    error = error_value if isinstance(error_value, Mapping) else response
    candidates = (
        error.get("server_error_code"),
        response.get("server_error_code"),
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


def _minimal_core_environment(
    environ: Mapping[str, str],
    *,
    runtime_root: Path,
) -> dict[str, str]:
    selected = {
        key.upper(): value
        for key, value in environ.items()
        if key.upper() in _CORE_ENV_ALLOWLIST - {"TEMP", "TMP"}
        and isinstance(value, str)
        and value
    }
    temp_root = prepare_core_temp_directory(runtime_root)
    selected["TEMP"] = str(temp_root)
    selected["TMP"] = str(temp_root)
    return selected


class DwsCommandRunner:
    def __init__(
        self,
        dws_path: Path,
        *,
        profile: str,
        timeout_seconds: float = 30.0,
        popen: Callable[..., Any] = subprocess.Popen,
        _official_bin: Path | None = None,
        _processor_architecture: str | None = None,
    ) -> None:
        launch_path, launch_env = resolve_dws_launch(
            dws_path,
            official_bin=_official_bin,
            processor_architecture=_processor_architecture,
        )
        self.__initialize(
            launch_path,
            launch_env,
            profile=profile,
            timeout_seconds=timeout_seconds,
            popen=popen,
        )

    def __initialize(
        self,
        launch_path: Path,
        launch_env: Mapping[str, str] | None,
        *,
        profile: str,
        timeout_seconds: float,
        popen: Callable[..., Any],
    ) -> None:
        if not profile.strip():
            raise ValueError("dws_profile_invalid")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("dws_timeout_invalid")
        self._dws_path = launch_path
        self._launch_env = dict(launch_env) if launch_env is not None else None
        self._profile = profile
        self._timeout_seconds = timeout_seconds
        self._popen = popen
        self._core_launch: (
            Callable[[], AbstractContextManager[TrustedDwsCore]] | None
        ) = None

    @classmethod
    def from_official_core(
        cls,
        dws_path: Path,
        *,
        runtime_root: Path,
        profile: str,
        timeout_seconds: float = 30.0,
        popen: Callable[..., Any] = subprocess.Popen,
        _official_bin: Path | None = None,
        _processor_architecture: str | None = None,
        _approvals_path: Path | None = None,
        _signature_reader: Callable[[Path], AuthenticodeDescriptor] | None = None,
    ) -> DwsCommandRunner:
        environ = dict(os.environ)
        core_launch = partial(
            hold_trusted_dws_core,
            dws_path,
            environ=environ,
            official_bin=_official_bin,
            processor_architecture=_processor_architecture,
            approvals_path=_approvals_path,
            signature_reader=_signature_reader or read_authenticode,
        )
        with core_launch() as trusted:
            launch_env = _minimal_core_environment(
                environ,
                runtime_root=runtime_root,
            )
            runner = cls.__new__(cls)
            runner.__initialize(
                trusted.path,
                launch_env,
                profile=profile,
                timeout_seconds=timeout_seconds,
                popen=popen,
            )
        runner._core_launch = core_launch
        return runner

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
            popen_options = {
                "shell": False,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
            }
            if self._launch_env is not None:
                popen_options["env"] = self._launch_env
            if self._core_launch is None:
                process = self._popen(command, **popen_options)
            else:
                with self._core_launch() as trusted:
                    command[0] = str(trusted.path)
                    process = self._popen(command, **popen_options)
        except (OSError, subprocess.SubprocessError):
            run_error = DwsReadError(SourceErrorType.PROVIDER_UNAVAILABLE, False)
        if run_error is not None:
            raise run_error

        stdout, stderr, returncode = _bounded_process_streams(
            process,
            timeout_seconds=self._timeout_seconds,
        )
        if stdout.lstrip().startswith(b"[dws-bash:pending-post-tool-use]:"):
            raise HostHandoffRequired()
        payload = _parse_json_object(stdout)
        if returncode == 0:
            if payload is None:
                raise DwsReadError(SourceErrorType.INVALID_PAYLOAD, False)
            return payload
        if payload is None:
            payload = _parse_json_object(stderr)
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
    stdout, _stderr, returncode = _bounded_process_streams(
        process, timeout_seconds=timeout_seconds, capture_stderr=False
    )
    return stdout, returncode


def _bounded_process_streams(
    process: Any,
    *,
    timeout_seconds: float,
    capture_stderr: bool = True,
) -> tuple[bytes, bytes, int]:
    streams = (getattr(process, "stdout", None),)
    if capture_stderr:
        streams += (getattr(process, "stderr", None),)
    limits = (MAX_DWS_STDOUT_BYTES, MAX_DWS_STDERR_BYTES)
    output: list[bytes] = [b"", b""]
    completed: queue.Queue[tuple[int, bytes | None]] = queue.Queue()
    readers: list[threading.Thread] = []

    def read_stream(index: int) -> None:
        try:
            value = streams[index].read(limits[index] + 1)
        except Exception:
            value = None
        completed.put((index, value if isinstance(value, bytes) else None))

    try:
        if any(not callable(getattr(stream, "read", None)) for stream in streams):
            raise DwsReadError(SourceErrorType.PROVIDER_UNAVAILABLE, False)
        deadline = time.monotonic() + timeout_seconds
        for index in range(len(streams)):
            reader = threading.Thread(target=read_stream, args=(index,), daemon=True)
            reader.start()
            readers.append(reader)
        for _ in streams:
            read_result = None
            try:
                read_result = completed.get(
                    timeout=max(0.0, deadline - time.monotonic())
                )
            except queue.Empty:
                pass
            if read_result is None:
                raise DwsReadError(SourceErrorType.NETWORK_TIMEOUT, True)
            index, value = read_result
            if value is None:
                raise DwsReadError(SourceErrorType.PROVIDER_UNAVAILABLE, False)
            if len(value) > limits[index]:
                raise DwsReadError(SourceErrorType.INVALID_PAYLOAD, False)
            output[index] = value

        remaining = max(0.0, deadline - time.monotonic())
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
        _terminate_process(process, streams=streams, readers=readers)
        raise
    for stream in streams:
        _close_stream(stream)
    return output[0], output[1], returncode


def _terminate_process(
    process: Any,
    *,
    streams: tuple[Any, ...],
    readers: list[threading.Thread],
) -> None:
    deadline = time.monotonic() + _PROCESS_CLEANUP_TIMEOUT_SECONDS
    try:
        process.kill()
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        process.wait(timeout=_PROCESS_CLEANUP_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError):
        pass
    for index, stream in enumerate(streams):
        reader = readers[index] if index < len(readers) else None
        if reader is not None and reader.is_alive():
            try:
                reader.join(max(0.0, deadline - time.monotonic()))
            except (KeyboardInterrupt, RuntimeError):
                pass
        if reader is None or not reader.is_alive():
            _close_stream(stream)


def _close_stream(stream: Any) -> None:
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
