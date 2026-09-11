from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Protocol

from companion_gateway.project.protection import ContentProtector
from tools.dws_sync import state_lock
from tools.dws_sync.orchestrator import run_once
from tools.dws_sync.runtime import RUNTIME_NAME, require_local


SESSION_DURATION_SECONDS = 7200
RENEWAL_INTERVAL_SECONDS = 300
RUN_CLAIM_SECONDS = 900
MAX_SESSION_STATE_BYTES = 65_536

_SCHEMA_VERSION = 1
_LOCK_KEY = "meeting-session"
_REPARSE_POINT = 0x400
_CODE = re.compile(r"[a-z][a-z0-9_-]{0,127}\Z")
_STATE_KEYS = frozenset(
    {
        "schema_version",
        "status",
        "started_at",
        "expires_at",
        "next_due_at",
        "claim_started_at",
        "claim_expires_at",
        "stage",
        "error_type",
        "release_status",
    }
)
_SESSION_STATUSES = frozenset(
    {"active", "attention_required", "stopped", "expired"}
)


class RunOnce(Protocol):
    def __call__(self, root: Path, protector: ContentProtector) -> object: ...


@dataclass(frozen=True)
class SessionResult:
    status: str
    started_at: str | None = None
    expires_at: str | None = None
    next_due_at: str | None = None
    stage: str | None = None
    error_type: str | None = None
    release_status: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "started_at": self.started_at,
            "expires_at": self.expires_at,
            "next_due_at": self.next_due_at,
            "stage": self.stage,
            "error_type": self.error_type,
            "release_status": self.release_status,
        }


def session_state_path(root: Path) -> Path:
    require_local(root)
    path = root / RUNTIME_NAME / "meeting-session.json"
    require_local(path)
    return path


def _now(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("session_state_invalid")
    return value.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _read_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not 1 <= len(value) <= 64:
        raise ValueError("session_state_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("session_state_invalid") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("session_state_invalid")
    return parsed.astimezone(UTC)


def _read_code(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _CODE.fullmatch(value) is None:
        raise ValueError("session_state_invalid")
    return value


def _safe_code(value: object, fallback: str | None = None) -> str | None:
    if isinstance(value, str) and _CODE.fullmatch(value) is not None:
        return value
    return fallback


def _safe_regular(info: os.stat_result) -> bool:
    return (
        stat.S_ISREG(info.st_mode)
        and not stat.S_ISLNK(info.st_mode)
        and not getattr(info, "st_file_attributes", 0) & _REPARSE_POINT
        and info.st_nlink == 1
        and info.st_size <= MAX_SESSION_STATE_BYTES
    )


def _ensure_storage(root: Path) -> Path:
    path = session_state_path(root)
    directory = path.parent
    try:
        directory.mkdir(parents=True, exist_ok=True)
        require_local(directory)
        details = directory.lstat()
    except (OSError, ValueError):
        raise ValueError("session_state_write_failed") from None
    if (
        not stat.S_ISDIR(details.st_mode)
        or stat.S_ISLNK(details.st_mode)
        or getattr(details, "st_file_attributes", 0) & _REPARSE_POINT
    ):
        raise ValueError("session_state_write_failed")
    return path


def _validate_state(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict) or frozenset(payload) != _STATE_KEYS:
        raise ValueError("session_state_invalid")
    if (
        type(payload["schema_version"]) is not int
        or payload["schema_version"] != _SCHEMA_VERSION
    ):
        raise ValueError("session_state_invalid")
    status = payload["status"]
    if not isinstance(status, str) or status not in _SESSION_STATUSES:
        raise ValueError("session_state_invalid")

    started_at = _read_timestamp(payload["started_at"])
    expires_at = _read_timestamp(payload["expires_at"])
    if expires_at != started_at + timedelta(seconds=SESSION_DURATION_SECONDS):
        raise ValueError("session_state_invalid")

    next_due_value = payload["next_due_at"]
    next_due_at = (
        None if next_due_value is None else _read_timestamp(next_due_value)
    )
    claim_started_value = payload["claim_started_at"]
    claim_expires_value = payload["claim_expires_at"]
    if (claim_started_value is None) != (claim_expires_value is None):
        raise ValueError("session_state_invalid")
    claim_started_at = (
        None if claim_started_value is None else _read_timestamp(claim_started_value)
    )
    claim_expires_at = (
        None if claim_expires_value is None else _read_timestamp(claim_expires_value)
    )
    if claim_started_at is not None:
        if claim_expires_at != claim_started_at + timedelta(seconds=RUN_CLAIM_SECONDS):
            raise ValueError("session_state_invalid")
        if claim_started_at < started_at:
            raise ValueError("session_state_invalid")

    if status == "active":
        if next_due_at is None:
            raise ValueError("session_state_invalid")
        if next_due_at < started_at:
            raise ValueError("session_state_invalid")
    elif next_due_at is not None or claim_started_at is not None:
        raise ValueError("session_state_invalid")

    return {
        "schema_version": _SCHEMA_VERSION,
        "status": status,
        "started_at": _timestamp(started_at),
        "expires_at": _timestamp(expires_at),
        "next_due_at": None if next_due_at is None else _timestamp(next_due_at),
        "claim_started_at": (
            None if claim_started_at is None else _timestamp(claim_started_at)
        ),
        "claim_expires_at": (
            None if claim_expires_at is None else _timestamp(claim_expires_at)
        ),
        "stage": _read_code(payload["stage"]),
        "error_type": _read_code(payload["error_type"]),
        "release_status": _read_code(payload["release_status"]),
    }


def _read_state(path: Path) -> dict[str, object] | None:
    try:
        require_local(path)
        before = path.lstat()
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        raise ValueError("session_state_invalid") from None
    if not _safe_regular(before):
        raise ValueError("session_state_invalid")
    try:
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if not _safe_regular(opened):
                raise ValueError("session_state_invalid")
            raw = stream.read(MAX_SESSION_STATE_BYTES + 1)
        after = path.lstat()
    except ValueError:
        raise
    except OSError:
        raise ValueError("session_state_invalid") from None
    if (
        len(raw) > MAX_SESSION_STATE_BYTES
        or not _safe_regular(after)
        or not os.path.samestat(before, after)
        or not os.path.samestat(opened, after)
    ):
        raise ValueError("session_state_invalid")
    try:
        return _validate_state(json.loads(raw.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise ValueError("session_state_invalid") from None


def _write_state(path: Path, payload: dict[str, object]) -> None:
    try:
        checked = _validate_state(payload)
        raw = json.dumps(
            checked,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError):
        raise ValueError("session_state_write_failed") from None
    if len(raw) > MAX_SESSION_STATE_BYTES:
        raise ValueError("session_state_write_failed")

    temporary: str | None = None
    try:
        require_local(path)
        if path.exists() and not _safe_regular(path.lstat()):
            raise ValueError("session_state_write_failed")
        descriptor, temporary = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
            staged = os.fstat(stream.fileno())
        if not _safe_regular(staged):
            raise ValueError("session_state_write_failed")
        require_local(path)
        os.replace(temporary, path)
        temporary = None
        published = path.lstat()
        if not _safe_regular(published) or not os.path.samestat(staged, published):
            raise ValueError("session_state_write_failed")
    except ValueError:
        raise
    except OSError:
        raise ValueError("session_state_write_failed") from None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _state_result(state: dict[str, object]) -> SessionResult:
    return SessionResult(
        status=str(state["status"]),
        started_at=str(state["started_at"]),
        expires_at=str(state["expires_at"]),
        next_due_at=(
            None if state["next_due_at"] is None else str(state["next_due_at"])
        ),
        stage=None if state["stage"] is None else str(state["stage"]),
        error_type=(
            None if state["error_type"] is None else str(state["error_type"])
        ),
        release_status=(
            None
            if state["release_status"] is None
            else str(state["release_status"])
        ),
    )


def _failed(error_type: str) -> SessionResult:
    return SessionResult("failed", error_type=error_type)


def _clear_claim(state: dict[str, object]) -> None:
    state["claim_started_at"] = None
    state["claim_expires_at"] = None


def _stop_expired(state: dict[str, object]) -> None:
    state["status"] = "expired"
    state["next_due_at"] = None
    _clear_claim(state)


def _new_state(current: datetime) -> dict[str, object]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "status": "active",
        "started_at": _timestamp(current),
        "expires_at": _timestamp(
            current + timedelta(seconds=SESSION_DURATION_SECONDS)
        ),
        "next_due_at": _timestamp(current),
        "claim_started_at": None,
        "claim_expires_at": None,
        "stage": None,
        "error_type": None,
        "release_status": None,
    }


def _check_clock(state: dict[str, object], current: datetime) -> None:
    if current < _read_timestamp(state["started_at"]):
        raise ValueError("session_state_invalid")


def _expire_if_needed(
    path: Path,
    state: dict[str, object],
    current: datetime,
) -> dict[str, object]:
    if (
        state["status"] in {"active", "attention_required"}
        and current >= _read_timestamp(state["expires_at"])
    ):
        _stop_expired(state)
        _write_state(path, state)
    return state


def _claim(state: dict[str, object], current: datetime) -> str:
    claim_started_at = _timestamp(current)
    state["claim_started_at"] = claim_started_at
    state["claim_expires_at"] = _timestamp(
        current + timedelta(seconds=RUN_CLAIM_SECONDS)
    )
    return claim_started_at


def _lock(root: Path):
    path = _ensure_storage(root)
    lock_path = path.parent / f"{hashlib.sha256(_LOCK_KEY.encode()).hexdigest()}.lock"
    try:
        require_local(lock_path)
        if lock_path.exists() and not _safe_regular(lock_path.lstat()):
            raise ValueError("session_state_write_failed")
    except (OSError, ValueError):
        raise ValueError("session_state_write_failed") from None
    return state_lock.acquire_state_lock(
        path,
        _LOCK_KEY,
        root=path.parent,
    )


def _failure_from_exception(error: ValueError) -> SessionResult:
    return _failed(
        "session_state_invalid"
        if str(error) == "session_state_invalid"
        else "session_state_write_failed"
    )


def _complete_claim(
    root: Path,
    result: object,
    claim_started_at: str,
    *,
    now: Callable[[], datetime],
) -> SessionResult:
    try:
        current = _now(now)
        with _lock(root):
            path = _ensure_storage(root)
            state = _read_state(path)
            if state is None:
                raise ValueError("session_state_invalid")
            _check_clock(state, current)
            state = _expire_if_needed(path, state, current)
            if (
                state["status"] != "active"
                or state["claim_started_at"] != claim_started_at
            ):
                return _state_result(state)

            status = getattr(result, "status", None)
            stage = _safe_code(getattr(result, "stage", None), "session")
            error_type = _safe_code(getattr(result, "error_type", None))
            release_status = _safe_code(getattr(result, "release_status", None))
            _clear_claim(state)
            state["stage"] = stage
            state["release_status"] = release_status
            if status == "awaiting_artifact":
                state["status"] = "attention_required"
                state["next_due_at"] = None
                state["error_type"] = None
            else:
                state["status"] = "active"
                state["next_due_at"] = _timestamp(
                    current + timedelta(seconds=RENEWAL_INTERVAL_SECONDS)
                )
                state["error_type"] = (
                    None
                    if status in {"completed", "coalesced"}
                    else error_type or "sync_failed"
                )
            _write_state(path, state)
            persisted = _state_result(state)
            if status not in {"completed", "coalesced", "awaiting_artifact"}:
                return SessionResult(
                    status="failed",
                    started_at=persisted.started_at,
                    expires_at=persisted.expires_at,
                    next_due_at=persisted.next_due_at,
                    stage=persisted.stage,
                    error_type=persisted.error_type,
                    release_status=persisted.release_status,
                )
            return persisted
    except ValueError as error:
        return _failure_from_exception(error)


def _run_claim(
    root: Path,
    protector: ContentProtector,
    claim_started_at: str,
    *,
    now: Callable[[], datetime],
    run_once_func: RunOnce,
) -> SessionResult:
    try:
        result = run_once_func(root, protector)
    except Exception:
        result = type(
            "FailedRun",
            (),
            {
                "status": "failed",
                "stage": "session",
                "error_type": "sync_failed",
                "release_status": None,
            },
        )()
    return _complete_claim(root, result, claim_started_at, now=now)


def start_session(
    root: Path,
    protector: ContentProtector,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    run_once_func: RunOnce = run_once,
) -> SessionResult:
    try:
        current = _now(now)
        with _lock(root):
            path = _ensure_storage(root)
            state = _read_state(path)
            if state is not None:
                _check_clock(state, current)
                state = _expire_if_needed(path, state, current)
                if state["status"] in {"active", "attention_required"}:
                    return _state_result(state)
            state = _new_state(current)
            claim_started_at = _claim(state, current)
            _write_state(path, state)
    except ValueError as error:
        return _failure_from_exception(error)
    return _run_claim(
        root,
        protector,
        claim_started_at,
        now=now,
        run_once_func=run_once_func,
    )


def tick_session(
    root: Path,
    protector: ContentProtector,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    run_once_func: RunOnce = run_once,
) -> SessionResult:
    try:
        current = _now(now)
        with _lock(root):
            path = _ensure_storage(root)
            state = _read_state(path)
            if state is None:
                return SessionResult("inactive")
            _check_clock(state, current)
            state = _expire_if_needed(path, state, current)
            if state["status"] != "active":
                return _state_result(state)
            claim_expires_at = state["claim_expires_at"]
            if (
                claim_expires_at is not None
                and _read_timestamp(claim_expires_at) > current
            ):
                return _state_result(state)
            next_due_at = _read_timestamp(state["next_due_at"])
            if current < next_due_at:
                return _state_result(state)
            claim_started_at = _claim(state, current)
            _write_state(path, state)
    except ValueError as error:
        return _failure_from_exception(error)
    return _run_claim(
        root,
        protector,
        claim_started_at,
        now=now,
        run_once_func=run_once_func,
    )


def session_status(
    root: Path,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> SessionResult:
    try:
        current = _now(now)
        with _lock(root):
            path = _ensure_storage(root)
            state = _read_state(path)
            if state is None:
                return SessionResult("inactive")
            _check_clock(state, current)
            return _state_result(_expire_if_needed(path, state, current))
    except ValueError as error:
        return _failure_from_exception(error)


def stop_session(
    root: Path,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> SessionResult:
    try:
        current = _now(now)
        with _lock(root):
            path = _ensure_storage(root)
            state = _read_state(path)
            if state is None:
                return SessionResult("inactive")
            _check_clock(state, current)
            state = _expire_if_needed(path, state, current)
            if state["status"] not in {"stopped", "expired"}:
                state["status"] = "stopped"
                state["next_due_at"] = None
                _clear_claim(state)
                _write_state(path, state)
            return _state_result(state)
    except ValueError as error:
        return _failure_from_exception(error)


def resume_session(
    root: Path,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> SessionResult:
    try:
        current = _now(now)
        with _lock(root):
            path = _ensure_storage(root)
            state = _read_state(path)
            if state is None:
                return _failed("session_resume_invalid")
            _check_clock(state, current)
            state = _expire_if_needed(path, state, current)
            if state["status"] != "attention_required":
                if state["status"] == "expired":
                    return _state_result(state)
                return _failed("session_resume_invalid")
            state["status"] = "active"
            state["next_due_at"] = _timestamp(current)
            _clear_claim(state)
            state["stage"] = None
            state["error_type"] = None
            state["release_status"] = None
            _write_state(path, state)
            return _state_result(state)
    except ValueError as error:
        return _failure_from_exception(error)
