from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
from typing import Literal
from typing import Iterator

from tools.dws_sync import state_lock


LEASE_SECONDS = 1800
MAX_LIFECYCLE_STATE_BYTES = 65_536
_PROJECT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_STAGES = ("begun", "collected", "pending", "artifact", "pushed")
Stage = Literal["begun", "collected", "pending", "artifact", "pushed"]


@dataclass(frozen=True)
class BeginResult:
    status: Literal["started", "coalesced", "rerun", "completed"]
    run_token: str | None


def _project_key(project_id: str) -> str:
    if _PROJECT_ID.fullmatch(project_id) is None:
        raise ValueError("project_id_invalid")
    return hashlib.sha256(project_id.encode("utf-8")).hexdigest()


def project_lock_path(root: Path, project_id: str) -> Path:
    return root / f"{_project_key(project_id)}.lock"


def project_state_path(root: Path, project_id: str) -> Path:
    return root / f"{_project_key(project_id)}.json"


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _read_state(path: Path, project_id: str) -> dict[str, object] | None:
    if not path.exists():
        return None
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_LIFECYCLE_STATE_BYTES + 1)
        if len(raw) > MAX_LIFECYCLE_STATE_BYTES:
            raise ValueError("lifecycle_state_invalid")
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("lifecycle_state_invalid") from None
    required = {
        "schema_version",
        "project_key",
        "active",
        "run_token_hash",
        "stage",
        "lease_expires_at",
        "coalesced",
        "rerun_used",
    }
    legacy = required - {"rerun_used"}
    if not isinstance(payload, dict) or frozenset(payload) not in {
        frozenset(required),
        frozenset(legacy),
    }:
        raise ValueError("lifecycle_state_invalid")
    payload.setdefault("rerun_used", False)
    if payload["schema_version"] != 1 or payload["project_key"] != _project_key(
        project_id
    ):
        raise ValueError("lifecycle_state_invalid")
    if any(
        not isinstance(payload[field], bool)
        for field in ("active", "coalesced", "rerun_used")
    ):
        raise ValueError("lifecycle_state_invalid")
    if payload["stage"] not in (*_STAGES, "completed", "aborted"):
        raise ValueError("lifecycle_state_invalid")
    return payload


def _write_state(path: Path, payload: dict[str, object]) -> None:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        raise ValueError("private_file_write_failed") from None


def _read_now(now: Callable[[], datetime]) -> datetime:
    value = now()
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("now_not_timezone_aware")
    return value.astimezone(UTC)


def _expires_at(now: datetime) -> str:
    return (now + timedelta(seconds=LEASE_SECONDS)).isoformat()


def _is_live(state: dict[str, object], now: datetime) -> bool:
    if state["active"] is not True:
        return False
    raw_expiry = state["lease_expires_at"]
    if not isinstance(raw_expiry, str):
        raise ValueError("lifecycle_state_invalid")
    try:
        expiry = datetime.fromisoformat(raw_expiry.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("lifecycle_state_invalid") from None
    if expiry.tzinfo is None or expiry.utcoffset() is None:
        raise ValueError("lifecycle_state_invalid")
    return expiry > now


def _new_state(
    project_id: str,
    token: str,
    now: datetime,
    *,
    rerun_used: bool = False,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "project_key": _project_key(project_id),
        "active": True,
        "run_token_hash": _token_hash(token),
        "stage": "begun",
        "lease_expires_at": _expires_at(now),
        "coalesced": False,
        "rerun_used": rerun_used,
    }


def _lock(
    root: Path, project_id: str, *, timeout: float = state_lock.MAX_LOCK_WAIT_SECONDS
):  # type: ignore[no-untyped-def]
    return state_lock.acquire_state_lock(
        project_state_path(root, project_id),
        project_id,
        root=root,
        timeout=timeout,
    )


def begin_run(
    project_id: str,
    *,
    root: Path = state_lock.PRIVATE_LOCK_ROOT,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> BeginResult:
    with _lock(root, project_id):
        current = _read_now(now)
        path = project_state_path(root, project_id)
        state = _read_state(path, project_id)
        if state is not None and _is_live(state, current):
            if state["coalesced"] is not True:
                state["coalesced"] = True
                _write_state(path, state)
            return BeginResult("coalesced", None)
        token = secrets.token_hex(32)
        _write_state(path, _new_state(project_id, token, current))
        return BeginResult("started", token)


def _validated_state(
    path: Path, project_id: str, token: str, now: datetime
) -> dict[str, object]:
    state = _read_state(path, project_id)
    if state is None or not _is_live(state, now):
        raise ValueError("run_token_invalid")
    stored_hash = state["run_token_hash"]
    if not isinstance(stored_hash, str) or not hmac.compare_digest(
        stored_hash, _token_hash(token)
    ):
        raise ValueError("run_token_invalid")
    return state


def assert_stage(
    project_id: str,
    token: str,
    *,
    expected: Stage,
    root: Path = state_lock.PRIVATE_LOCK_ROOT,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> None:
    with _lock(root, project_id):
        current = _read_now(now)
        state = _validated_state(
            project_state_path(root, project_id), project_id, token, current
        )
        if state["stage"] != expected:
            raise ValueError("run_stage_invalid")


def assert_manual_allowed(
    project_id: str,
    *,
    root: Path = state_lock.PRIVATE_LOCK_ROOT,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> None:
    with _lock(root, project_id):
        current = _read_now(now)
        state = _read_state(project_state_path(root, project_id), project_id)
        if state is not None and _is_live(state, current):
            raise ValueError("lifecycle_active")


@contextmanager
def manual_guard(
    project_id: str,
    *,
    root: Path = state_lock.PRIVATE_LOCK_ROOT,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    timeout: float = state_lock.MAX_LOCK_WAIT_SECONDS,
) -> Iterator[None]:
    with _lock(root, project_id, timeout=timeout):
        current = _read_now(now)
        state = _read_state(project_state_path(root, project_id), project_id)
        if state is not None and _is_live(state, current):
            raise ValueError("lifecycle_active")
        yield


def advance_run(
    project_id: str,
    token: str,
    *,
    expected: Stage,
    target: Stage,
    root: Path = state_lock.PRIVATE_LOCK_ROOT,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> None:
    if _STAGES.index(target) != _STAGES.index(expected) + 1:
        raise ValueError("run_stage_invalid")
    with _lock(root, project_id):
        current = _read_now(now)
        path = project_state_path(root, project_id)
        state = _validated_state(path, project_id, token, current)
        if state["stage"] != expected:
            raise ValueError("run_stage_invalid")
        state["stage"] = target
        state["lease_expires_at"] = _expires_at(current)
        _write_state(path, state)


@contextmanager
def stage_guard(
    project_id: str,
    token: str,
    *,
    expected: Stage,
    target: Stage,
    root: Path = state_lock.PRIVATE_LOCK_ROOT,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> Iterator[None]:
    if _STAGES.index(target) != _STAGES.index(expected) + 1:
        raise ValueError("run_stage_invalid")
    with _lock(root, project_id):
        current = _read_now(now)
        path = project_state_path(root, project_id)
        state = _validated_state(path, project_id, token, current)
        if state["stage"] != expected:
            raise ValueError("run_stage_invalid")
        yield
        state["stage"] = target
        state["lease_expires_at"] = _expires_at(_read_now(now))
        _write_state(path, state)


def end_run(
    project_id: str,
    token: str,
    *,
    root: Path = state_lock.PRIVATE_LOCK_ROOT,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> BeginResult:
    with _lock(root, project_id):
        current = _read_now(now)
        path = project_state_path(root, project_id)
        state = _validated_state(path, project_id, token, current)
        if state["stage"] != "pushed":
            raise ValueError("run_stage_invalid")
        if state["coalesced"] is True and state["rerun_used"] is False:
            replacement = secrets.token_hex(32)
            _write_state(
                path,
                _new_state(
                    project_id, replacement, current, rerun_used=True
                ),
            )
            return BeginResult("rerun", replacement)
        state.update(
            active=False,
            run_token_hash=None,
            stage="completed",
            lease_expires_at=None,
            coalesced=False,
        )
        _write_state(path, state)
        return BeginResult("completed", None)


def abort_run(
    project_id: str,
    token: str,
    *,
    root: Path = state_lock.PRIVATE_LOCK_ROOT,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> None:
    with _lock(root, project_id):
        current = _read_now(now)
        path = project_state_path(root, project_id)
        state = _validated_state(path, project_id, token, current)
        state.update(
            active=False,
            run_token_hash=None,
            stage="aborted",
            lease_expires_at=None,
            coalesced=False,
        )
        _write_state(path, state)
