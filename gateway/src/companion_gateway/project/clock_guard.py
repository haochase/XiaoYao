from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from threading import RLock
from typing import Literal

from companion_gateway.project.sync_repository import ProjectSyncRepository


CLOCK_ROLLBACK_THRESHOLD_SECONDS = 300.0


@dataclass(frozen=True)
class ClockCheckResult:
    immediate_sync_required: bool
    clock_untrusted: bool
    reason: Literal["normal", "resume_detected", "clock_rollback"]


class ProjectClockGuard:
    def __init__(
        self,
        repository: ProjectSyncRepository,
        *,
        sync_interval_seconds: float,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if (
            not isinstance(sync_interval_seconds, (int, float))
            or isinstance(sync_interval_seconds, bool)
            or not math.isfinite(sync_interval_seconds)
            or sync_interval_seconds <= 0
        ):
            raise ValueError("sync_interval_seconds_invalid")
        if not callable(monotonic):
            raise ValueError("monotonic_invalid")
        self._repository = repository
        self._sync_interval_seconds = float(sync_interval_seconds)
        self._monotonic = monotonic
        self._last_wall: datetime | None = None
        self._last_monotonic: float | None = None
        self._local_sync_request = False
        self._lock = RLock()

    def check(
        self,
        *,
        wall_now: datetime,
        monotonic_now: float | None = None,
    ) -> ClockCheckResult:
        _require_aware(wall_now)
        sample = self._read_monotonic(monotonic_now)
        shared = self._repository.load_clock_state()
        detected_untrusted = (
            shared.trusted_wall_at is not None
            and (
                shared.trusted_wall_at - wall_now
            ).total_seconds() > CLOCK_ROLLBACK_THRESHOLD_SECONDS
        )
        detected_sync = detected_untrusted
        detected_reason: Literal[
            "normal", "resume_detected", "clock_rollback"
        ] = "clock_rollback" if detected_untrusted else "normal"

        with self._lock:
            if self._last_wall is not None and self._last_monotonic is not None:
                wall_elapsed = (wall_now - self._last_wall).total_seconds()
                monotonic_elapsed = sample - self._last_monotonic
                if wall_elapsed < -CLOCK_ROLLBACK_THRESHOLD_SECONDS:
                    detected_untrusted = True
                    detected_sync = True
                    detected_reason = "clock_rollback"
                elif monotonic_elapsed > 2 * self._sync_interval_seconds:
                    detected_sync = True
                    detected_reason = "resume_detected"
            self._last_wall = wall_now
            self._last_monotonic = sample

        if detected_sync:
            shared = self._repository.mark_clock_state(
                clock_untrusted=detected_untrusted,
                needs_sync=True,
                reason=(
                    "clock_rollback"
                    if detected_untrusted
                    else "resume_detected"
                ),
            )
        else:
            shared = self._repository.load_clock_state()
        with self._lock:
            self._local_sync_request = (
                self._local_sync_request or shared.needs_sync
            )
        reason = shared.reason if shared.needs_sync else detected_reason
        return ClockCheckResult(
            immediate_sync_required=shared.needs_sync,
            clock_untrusted=shared.clock_untrusted,
            reason=reason,
        )

    def reset_local(self, *, wall_now: datetime, monotonic_now: float) -> None:
        _require_aware(wall_now)
        sample = _validate_monotonic(monotonic_now)
        with self._lock:
            self._last_wall = wall_now
            self._last_monotonic = sample
            self._local_sync_request = False

    def consume_local_sync_request(self) -> bool:
        with self._lock:
            pending = self._local_sync_request
            self._local_sync_request = False
            return pending

    def _read_monotonic(self, value: float | None) -> float:
        return _validate_monotonic(
            self._monotonic() if value is None else value
        )


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("wall_now_must_be_aware")


def _validate_monotonic(value: float) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError("monotonic_invalid")
    return float(value)


__all__ = [
    "CLOCK_ROLLBACK_THRESHOLD_SECONDS",
    "ClockCheckResult",
    "ProjectClockGuard",
]
