from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

import companion_gateway.project.clock_guard as clock_guard
from companion_gateway.project.clock_guard import ProjectClockGuard
from companion_gateway.project.sync_repository import ProjectSyncRepository


NOW = datetime(2026, 9, 9, 8, 0, tzinfo=UTC)


class AwakeTimeApi:
    def __init__(self, samples: list[float | None]) -> None:
        self._samples = iter(samples)
        self.calls = 0

    def QueryUnbiasedInterruptTime(self, target) -> int:
        self.calls += 1
        sample = next(self._samples)
        if sample is None:
            return 0
        ctypes_target = clock_guard.ctypes.cast(
            target,
            clock_guard.ctypes.POINTER(clock_guard.ctypes.c_ulonglong),
        )
        ctypes_target.contents.value = int(sample * 10_000_000)
        return 1


def _windows_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    awake_api: AwakeTimeApi,
    fallback_samples: list[float],
) -> ProjectClockGuard:
    repository = ProjectSyncRepository(tmp_path / "project-clock.db")
    repository.initialize()
    monkeypatch.setattr(clock_guard.sys, "platform", "win32")
    monkeypatch.setattr(
        clock_guard.ctypes,
        "windll",
        SimpleNamespace(kernel32=awake_api),
        raising=False,
    )
    fallback = iter(fallback_samples)
    monkeypatch.setattr(clock_guard.time, "monotonic", lambda: next(fallback))
    return ProjectClockGuard(
        repository,
        sync_interval_seconds=300,
        monotonic=lambda: 0,
    )


def test_windows_awake_api_failure_rebuilds_baseline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guard = _windows_guard(
        tmp_path,
        monkeypatch,
        AwakeTimeApi([100, 100, 100, None, 100]),
        [0],
    )

    guard.check(wall_now=NOW, monotonic_now=100)
    guard.check(wall_now=NOW + timedelta(seconds=1), monotonic_now=101)
    result = guard.check(
        wall_now=NOW + timedelta(seconds=602),
        monotonic_now=702,
    )

    assert not result.immediate_sync_required
    assert result.reason == "normal"
    recovered = guard.check(
        wall_now=NOW + timedelta(seconds=603),
        monotonic_now=703,
    )

    assert not recovered.immediate_sync_required
    assert recovered.reason == "normal"


def test_windows_awake_api_is_not_selected_after_initial_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    awake_api = AwakeTimeApi([None, 10])
    guard = _windows_guard(
        tmp_path,
        monkeypatch,
        awake_api,
        [100, 701],
    )

    guard.check(wall_now=NOW, monotonic_now=100)
    result = guard.check(
        wall_now=NOW + timedelta(seconds=601),
        monotonic_now=701,
    )

    assert not result.immediate_sync_required
    assert result.reason == "normal"
    assert awake_api.calls == 1


def test_windows_probe_sample_is_not_the_first_awake_baseline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guard = _windows_guard(
        tmp_path,
        monkeypatch,
        AwakeTimeApi([100, 700, 701]),
        [0],
    )

    guard.check(
        wall_now=NOW + timedelta(seconds=600),
        monotonic_now=100,
    )
    result = guard.check(
        wall_now=NOW + timedelta(seconds=1201),
        monotonic_now=701,
    )

    assert result.immediate_sync_required
    assert result.reason == "resume_detected"


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
