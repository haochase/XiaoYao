from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path

import pytest

from tools.dws_sync import lifecycle


NOW = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)


def test_project_lock_identity_ignores_state_file_and_hides_project_id(
    tmp_path: Path,
) -> None:
    first = lifecycle.project_lock_path(tmp_path, "project-1")
    second = lifecycle.project_lock_path(tmp_path, "project-1")

    assert first == second
    assert first.parent == tmp_path
    assert "project-1" not in first.name


def test_begin_coalesces_concurrent_triggers_once(tmp_path: Path) -> None:
    first = lifecycle.begin_run("project-1", root=tmp_path, now=lambda: NOW)
    second = lifecycle.begin_run("project-1", root=tmp_path, now=lambda: NOW)
    third = lifecycle.begin_run("project-1", root=tmp_path, now=lambda: NOW)

    assert first.status == "started"
    assert first.run_token is not None
    assert len(first.run_token) == 64
    assert all(character in "0123456789abcdef" for character in first.run_token)
    assert second.status == "coalesced"
    assert second.run_token is None
    assert third.status == "coalesced"
    state = json.loads(lifecycle.project_state_path(tmp_path, "project-1").read_text())
    assert state["stage"] == "begun"
    assert state["coalesced"] is True
    assert state["run_token_hash"] != first.run_token


def test_stage_fence_rejects_old_token_after_expired_lease_is_replaced(
    tmp_path: Path,
) -> None:
    old = lifecycle.begin_run("project-1", root=tmp_path, now=lambda: NOW)
    current = lifecycle.begin_run(
        "project-1",
        root=tmp_path,
        now=lambda: NOW + timedelta(hours=1),
    )

    assert current.status == "started"
    assert current.run_token != old.run_token
    with pytest.raises(ValueError, match="^run_token_invalid$"):
        lifecycle.advance_run(
            "project-1",
            old.run_token or "",
            expected="begun",
            target="collected",
            root=tmp_path,
            now=lambda: NOW + timedelta(hours=1),
        )


def test_end_rotates_token_for_one_coalesced_followup(tmp_path: Path) -> None:
    first = lifecycle.begin_run("project-1", root=tmp_path, now=lambda: NOW)
    lifecycle.begin_run("project-1", root=tmp_path, now=lambda: NOW)
    token = first.run_token or ""
    for expected, target in (
        ("begun", "collected"),
        ("collected", "pending"),
        ("pending", "artifact"),
        ("artifact", "pushed"),
    ):
        lifecycle.advance_run(
            "project-1",
            token,
            expected=expected,
            target=target,
            root=tmp_path,
            now=lambda: NOW,
        )

    followup = lifecycle.end_run(
        "project-1", token, root=tmp_path, now=lambda: NOW
    )

    assert followup.status == "rerun"
    assert followup.run_token is not None
    assert followup.run_token != token
    with pytest.raises(ValueError, match="^run_token_invalid$"):
        lifecycle.end_run("project-1", token, root=tmp_path, now=lambda: NOW)


def test_abort_requires_current_token_and_releases_lease(tmp_path: Path) -> None:
    started = lifecycle.begin_run("project-1", root=tmp_path, now=lambda: NOW)

    with pytest.raises(ValueError, match="^run_token_invalid$"):
        lifecycle.abort_run(
            "project-1", "x" * 43, root=tmp_path, now=lambda: NOW
        )
    lifecycle.abort_run(
        "project-1", started.run_token or "", root=tmp_path, now=lambda: NOW
    )
    replacement = lifecycle.begin_run(
        "project-1", root=tmp_path, now=lambda: NOW
    )
    assert replacement.status == "started"
    assert replacement.run_token != started.run_token


def test_lease_time_is_sampled_after_waiting_for_project_lock(
    tmp_path: Path, monkeypatch
) -> None:
    started = lifecycle.begin_run("project-1", root=tmp_path, now=lambda: NOW)
    current = NOW
    original_lock = lifecycle._lock

    @contextmanager
    def delayed_lock(root: Path, project_id: str, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal current
        with original_lock(root, project_id, **kwargs):
            current = NOW + timedelta(hours=1)
            yield

    monkeypatch.setattr(lifecycle, "_lock", delayed_lock)
    with pytest.raises(ValueError, match="^run_token_invalid$"):
        lifecycle.assert_stage(
            "project-1",
            started.run_token or "",
            expected="begun",
            root=tmp_path,
            now=lambda: current,
        )


def test_coalesced_chain_can_rerun_at_most_once(tmp_path: Path) -> None:
    first = lifecycle.begin_run("project-1", root=tmp_path, now=lambda: NOW)
    lifecycle.begin_run("project-1", root=tmp_path, now=lambda: NOW)

    def finish(token: str) -> lifecycle.BeginResult:
        for expected, target in (
            ("begun", "collected"),
            ("collected", "pending"),
            ("pending", "artifact"),
            ("artifact", "pushed"),
        ):
            lifecycle.advance_run(
                "project-1",
                token,
                expected=expected,
                target=target,
                root=tmp_path,
                now=lambda: NOW,
            )
        return lifecycle.end_run(
            "project-1", token, root=tmp_path, now=lambda: NOW
        )

    second = finish(first.run_token or "")
    lifecycle.begin_run("project-1", root=tmp_path, now=lambda: NOW)
    completed = finish(second.run_token or "")
    third = lifecycle.begin_run("project-1", root=tmp_path, now=lambda: NOW)

    assert second.status == "rerun"
    assert completed.status == "completed"
    assert third.status == "started"
