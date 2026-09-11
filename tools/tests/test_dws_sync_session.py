from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import stat
import threading
from types import SimpleNamespace

import pytest

from tools.dws_sync import session, state_lock


BASE_TIME = datetime(2026, 9, 11, 9, 0, tzinfo=UTC)


class Protector:
    pass


def at(seconds: int) -> Callable[[], datetime]:
    return lambda: BASE_TIME + timedelta(seconds=seconds)


def completed_result() -> SimpleNamespace:
    return SimpleNamespace(
        status="completed",
        stage="end",
        error_type=None,
        release_status=None,
    )


def read_state(root: Path) -> dict[str, object]:
    return json.loads(session.session_state_path(root).read_text(encoding="utf-8"))


def test_start_runs_once_creates_two_hour_session_and_excludes_sensitive_fields(
    tmp_path: Path,
) -> None:
    calls: list[tuple[Path, Protector]] = []

    result = session.start_session(
        tmp_path,
        Protector(),
        now=at(0),
        run_once_func=lambda root, protector: calls.append((root, protector))
        or completed_result(),
    )

    assert result.status == "active"
    assert len(calls) == 1
    assert calls[0][0] == tmp_path
    state = read_state(tmp_path)
    assert state["status"] == "active"
    assert datetime.fromisoformat(str(state["started_at"])) == BASE_TIME
    assert datetime.fromisoformat(str(state["expires_at"])) == BASE_TIME + timedelta(
        seconds=session.SESSION_DURATION_SECONDS
    )
    assert datetime.fromisoformat(str(state["next_due_at"])) == BASE_TIME + timedelta(
        seconds=session.RENEWAL_INTERVAL_SECONDS
    )
    forbidden = {"token", "profile", "source_id", "body", "hash"}
    assert not (set(state) & forbidden)
    assert "project" not in state


def test_active_repeat_start_is_idempotent_and_does_not_run_again(
    tmp_path: Path,
) -> None:
    calls: list[object] = []

    runner = lambda *_args: calls.append(object()) or completed_result()
    session.start_session(tmp_path, Protector(), now=at(0), run_once_func=runner)
    result = session.start_session(
        tmp_path,
        Protector(),
        now=at(1),
        run_once_func=runner,
    )

    assert result.status == "active"
    assert len(calls) == 1


@pytest.mark.parametrize("outcome", ("completed", "coalesced"))
def test_completed_or_coalesced_run_schedules_the_next_renewal(
    tmp_path: Path,
    outcome: str,
) -> None:
    result = session.start_session(
        tmp_path,
        Protector(),
        now=at(0),
        run_once_func=lambda *_args: SimpleNamespace(
            status=outcome,
            stage="end",
            error_type=None,
            release_status=None,
        ),
    )

    assert result.status == "active"
    state = read_state(tmp_path)
    assert state["error_type"] is None
    assert datetime.fromisoformat(str(state["next_due_at"])) == BASE_TIME + timedelta(
        seconds=session.RENEWAL_INTERVAL_SECONDS
    )


def test_tick_is_a_noop_before_due_and_runs_once_when_due(tmp_path: Path) -> None:
    calls: list[object] = []

    runner = lambda *_args: calls.append(object()) or completed_result()
    session.start_session(tmp_path, Protector(), now=at(0), run_once_func=runner)

    early = session.tick_session(
        tmp_path,
        Protector(),
        now=at(299),
        run_once_func=runner,
    )
    due = session.tick_session(tmp_path, Protector(), now=at(300), run_once_func=runner)

    assert early.status == "active"
    assert due.status == "active"
    assert len(calls) == 2


def test_tick_without_a_session_never_runs(tmp_path: Path) -> None:
    result = session.tick_session(
        tmp_path,
        Protector(),
        now=at(0),
        run_once_func=lambda *_args: pytest.fail("inactive session must not run"),
    )

    assert result.status == "inactive"


def test_concurrent_due_ticks_only_allow_one_claimed_run(tmp_path: Path) -> None:
    calls: list[object] = []
    entered = threading.Event()
    release = threading.Event()

    def runner(*_args: object) -> SimpleNamespace:
        calls.append(object())
        if len(calls) == 2:
            entered.set()
            assert release.wait(timeout=5)
        return completed_result()

    session.start_session(tmp_path, Protector(), now=at(0), run_once_func=runner)
    first = threading.Thread(
        target=lambda: session.tick_session(
            tmp_path, Protector(), now=at(300), run_once_func=runner
        )
    )
    first.start()
    assert entered.wait(timeout=5)

    second = session.tick_session(
        tmp_path, Protector(), now=at(300), run_once_func=runner
    )
    release.set()
    first.join(timeout=5)

    assert not first.is_alive()
    assert second.status == "active"
    assert len(calls) == 2


def test_tick_marks_two_hour_session_expired_without_running(tmp_path: Path) -> None:
    calls: list[object] = []

    runner = lambda *_args: calls.append(object()) or completed_result()
    session.start_session(tmp_path, Protector(), now=at(0), run_once_func=runner)
    result = session.tick_session(
        tmp_path,
        Protector(),
        now=at(session.SESSION_DURATION_SECONDS),
        run_once_func=runner,
    )

    assert result.status == "expired"
    assert len(calls) == 1
    assert read_state(tmp_path)["status"] == "expired"


def test_stop_prevents_later_ticks_from_running(tmp_path: Path) -> None:
    calls: list[object] = []

    runner = lambda *_args: calls.append(object()) or completed_result()
    session.start_session(tmp_path, Protector(), now=at(0), run_once_func=runner)
    stopped = session.stop_session(tmp_path, now=at(1))
    ticked = session.tick_session(
        tmp_path,
        Protector(),
        now=at(300),
        run_once_func=runner,
    )

    assert stopped.status == "stopped"
    assert ticked.status == "stopped"
    assert len(calls) == 1


def test_awaiting_artifact_pauses_until_resume_then_schedules_immediate_tick(
    tmp_path: Path,
) -> None:
    calls: list[object] = []

    def runner(*_args: object) -> SimpleNamespace:
        calls.append(object())
        if len(calls) == 1:
            return SimpleNamespace(
                status="awaiting_artifact",
                stage="reuse-artifact",
                error_type=None,
                release_status="aborted",
            )
        return completed_result()

    paused = session.start_session(
        tmp_path,
        Protector(),
        now=at(0),
        run_once_func=runner,
    )
    ticked = session.tick_session(
        tmp_path,
        Protector(),
        now=at(299),
        run_once_func=runner,
    )
    resumed = session.resume_session(tmp_path, now=at(300))
    completed = session.tick_session(
        tmp_path,
        Protector(),
        now=at(300),
        run_once_func=runner,
    )

    assert paused.status == "attention_required"
    assert ticked.status == "attention_required"
    assert resumed.status == "active"
    assert completed.status == "active"
    assert len(calls) == 2


def test_failed_run_persists_sanitized_diagnostics_and_waits_before_retry(
    tmp_path: Path,
) -> None:
    calls: list[object] = []

    def runner(*_args: object) -> SimpleNamespace:
        calls.append(object())
        return SimpleNamespace(
            status="failed",
            stage="push",
            error_type="sync_failed",
            release_status="aborted",
        )

    result = session.start_session(
        tmp_path,
        Protector(),
        now=at(0),
        run_once_func=runner,
    )
    state = read_state(tmp_path)
    early = session.tick_session(
        tmp_path,
        Protector(),
        now=at(299),
        run_once_func=runner,
    )
    due = session.tick_session(tmp_path, Protector(), now=at(300), run_once_func=runner)

    assert result.status == "failed"
    assert result.error_type == "sync_failed"
    assert state["stage"] == "push"
    assert state["error_type"] == "sync_failed"
    assert state["release_status"] == "aborted"
    assert early.status == "active"
    assert due.status == "failed"
    assert len(calls) == 2


def test_resume_rejects_any_state_except_attention_required(tmp_path: Path) -> None:
    calls: list[object] = []

    runner = lambda *_args: calls.append(object()) or completed_result()
    session.start_session(tmp_path, Protector(), now=at(0), run_once_func=runner)

    result = session.resume_session(tmp_path, now=at(1))

    assert result.status == "failed"
    assert result.error_type == "session_resume_invalid"
    assert len(calls) == 1


def test_expired_claim_recovers_after_nine_hundred_seconds(tmp_path: Path) -> None:
    calls: list[object] = []

    def runner(*_args: object) -> SimpleNamespace:
        calls.append(object())
        if len(calls) == 2:
            raise KeyboardInterrupt
        return completed_result()

    session.start_session(tmp_path, Protector(), now=at(0), run_once_func=runner)
    with pytest.raises(KeyboardInterrupt):
        session.tick_session(tmp_path, Protector(), now=at(300), run_once_func=runner)

    waiting = session.tick_session(
        tmp_path,
        Protector(),
        now=at(1199),
        run_once_func=runner,
    )
    recovered = session.tick_session(
        tmp_path,
        Protector(),
        now=at(1200),
        run_once_func=runner,
    )

    assert waiting.status == "active"
    assert recovered.status == "active"
    assert len(calls) == 3


@pytest.mark.parametrize(
    "mutate",
    (
        lambda state: state.update(token="secret"),
        lambda state: state.update(started_at="2026-09-11T09:00:00"),
        lambda state: state.update(
            started_at=(BASE_TIME + timedelta(seconds=1)).isoformat(),
            expires_at=(
                BASE_TIME + timedelta(seconds=1 + session.SESSION_DURATION_SECONDS)
            ).isoformat(),
        ),
    ),
)
def test_invalid_or_rolled_back_state_fails_closed_without_running(
    tmp_path: Path,
    mutate,
) -> None:
    calls: list[object] = []

    runner = lambda *_args: calls.append(object()) or completed_result()
    session.start_session(tmp_path, Protector(), now=at(0), run_once_func=runner)
    state_path = session.session_state_path(tmp_path)
    state = read_state(tmp_path)
    mutate(state)
    state_path.write_text(json.dumps(state), encoding="utf-8")

    result = session.tick_session(
        tmp_path,
        Protector(),
        now=at(0),
        run_once_func=runner,
    )

    assert result.status == "failed"
    assert result.error_type == "session_state_invalid"
    assert len(calls) == 1


def test_oversized_state_fails_closed_without_running(tmp_path: Path) -> None:
    state_path = session.session_state_path(tmp_path)
    state_path.parent.mkdir(parents=True)
    state_path.write_bytes(b"x" * (session.MAX_SESSION_STATE_BYTES + 1))

    result = session.tick_session(
        tmp_path,
        Protector(),
        now=at(0),
        run_once_func=lambda *_args: pytest.fail("invalid state must not run"),
    )

    assert result.status == "failed"
    assert result.error_type == "session_state_invalid"


def test_symlinked_state_fails_closed_without_running(tmp_path: Path) -> None:
    state_path = session.session_state_path(tmp_path)
    state_path.parent.mkdir(parents=True)
    target = tmp_path / "state-target.json"
    target.write_text("{}", encoding="utf-8")
    try:
        state_path.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    result = session.session_status(tmp_path, now=at(0))

    assert result.status == "failed"
    assert result.error_type == "session_state_invalid"


def test_symlinked_session_lock_fails_closed_without_touching_its_target(
    tmp_path: Path,
) -> None:
    state_path = session.session_state_path(tmp_path)
    state_path.parent.mkdir(parents=True)
    lock_path = state_lock._state_lock_path(
        state_path,
        "meeting-session",
        root=state_path.parent,
    )
    target = tmp_path / "lock-target"
    target.write_bytes(b"guard")
    try:
        lock_path.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    result = session.tick_session(
        tmp_path,
        Protector(),
        now=at(0),
        run_once_func=lambda *_args: pytest.fail("unsafe lock must not run"),
    )

    assert result.status == "failed"
    assert result.error_type == "session_state_write_failed"
    assert target.read_bytes() == b"guard"


def test_reparse_session_lock_is_rejected_before_lock_acquisition(
    tmp_path: Path,
    monkeypatch,
) -> None:
    state_path = session.session_state_path(tmp_path)
    state_path.parent.mkdir(parents=True)
    lock_path = state_lock._state_lock_path(
        state_path,
        "meeting-session",
        root=state_path.parent,
    )
    lock_path.write_bytes(b"\0")
    real_lstat = Path.lstat

    def reparse_lstat(path: Path):  # type: ignore[no-untyped-def]
        if path == lock_path:
            return SimpleNamespace(
                st_mode=stat.S_IFREG,
                st_nlink=1,
                st_size=1,
                st_file_attributes=0x400,
            )
        return real_lstat(path)

    monkeypatch.setattr(Path, "lstat", reparse_lstat)
    monkeypatch.setattr(
        session.state_lock,
        "acquire_state_lock",
        lambda *_args, **_kwargs: pytest.fail("unsafe lock must not be opened"),
    )

    result = session.tick_session(tmp_path, Protector(), now=at(0))

    assert result.status == "failed"
    assert result.error_type == "session_state_write_failed"
