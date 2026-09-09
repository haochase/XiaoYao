from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from companion_gateway.project.clock_guard import ProjectClockGuard
from companion_gateway.project.sync_repository import ProjectSyncRepository


NOW = datetime(2026, 9, 9, 8, 0, tzinfo=UTC)


def test_long_awake_interval_is_normal_idle(tmp_path: Path) -> None:
    repository = ProjectSyncRepository(tmp_path / "project-clock.db")
    repository.initialize()
    guard = ProjectClockGuard(repository, sync_interval_seconds=300)

    guard.check(wall_now=NOW, monotonic_now=100, awake_now=100)
    result = guard.check(
        wall_now=NOW + timedelta(seconds=601),
        monotonic_now=701,
        awake_now=701,
    )

    assert not result.immediate_sync_required
    assert not result.clock_untrusted
    assert result.reason == "normal"


def test_elapsed_gap_without_matching_awake_time_is_resume(tmp_path: Path) -> None:
    repository = ProjectSyncRepository(tmp_path / "project-clock.db")
    repository.initialize()
    guard = ProjectClockGuard(repository, sync_interval_seconds=300)

    guard.check(wall_now=NOW, monotonic_now=100, awake_now=100)
    result = guard.check(
        wall_now=NOW + timedelta(seconds=601),
        monotonic_now=701,
        awake_now=101,
    )

    assert result.immediate_sync_required
    assert not result.clock_untrusted
    assert result.reason == "resume_detected"


def test_clock_rollback_remains_untrusted(tmp_path: Path) -> None:
    repository = ProjectSyncRepository(tmp_path / "project-clock.db")
    repository.initialize()
    guard = ProjectClockGuard(repository, sync_interval_seconds=300)

    guard.check(wall_now=NOW, monotonic_now=100, awake_now=100)
    result = guard.check(
        wall_now=NOW - timedelta(seconds=301),
        monotonic_now=101,
        awake_now=101,
    )

    assert result.immediate_sync_required
    assert result.clock_untrusted
    assert result.reason == "clock_rollback"


@pytest.mark.parametrize("value", [True, -1, float("nan"), float("inf")])
def test_awake_time_rejects_invalid_samples(tmp_path: Path, value: float) -> None:
    repository = ProjectSyncRepository(tmp_path / "project-clock.db")
    repository.initialize()
    guard = ProjectClockGuard(repository, sync_interval_seconds=300)

    with pytest.raises(ValueError, match="^awake_time_invalid$"):
        guard.check(wall_now=NOW, monotonic_now=100, awake_now=value)


def test_reset_local_replaces_the_awake_time_baseline(tmp_path: Path) -> None:
    repository = ProjectSyncRepository(tmp_path / "project-clock.db")
    repository.initialize()
    guard = ProjectClockGuard(repository, sync_interval_seconds=300)

    guard.check(wall_now=NOW, monotonic_now=100, awake_now=100)
    guard.reset_local(
        wall_now=NOW + timedelta(seconds=601),
        monotonic_now=701,
        awake_now=700,
    )
    result = guard.check(
        wall_now=NOW + timedelta(seconds=1202),
        monotonic_now=1302,
        awake_now=701,
    )

    assert result.immediate_sync_required
    assert result.reason == "resume_detected"
