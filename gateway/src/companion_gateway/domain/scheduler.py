from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import uuid4

from companion_gateway.domain.executor import TaskExecutor
from companion_gateway.domain.models import TaskRecord


Clock = Callable[[], datetime]
TraceFactory = Callable[[], str]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _new_trace_id() -> str:
    return f"trc_task_tick_{uuid4().hex}"


class TaskScheduler:
    """Opt-in single-process loop around the idempotent task executor."""

    def __init__(
        self,
        *,
        executor: TaskExecutor,
        deliver: Callable[[TaskRecord], bool],
        interval_seconds: float,
        clock: Clock = _utc_now,
        trace_factory: TraceFactory = _new_trace_id,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self._executor = executor
        self._deliver = deliver
        self._interval_seconds = interval_seconds
        self._clock = clock
        self._trace_factory = trace_factory
        self._task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event | None = None

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def tick(self, *, now: datetime | None = None) -> list[TaskRecord]:
        current = now or self._clock()
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("scheduler clock must return timezone-aware datetime")
        return self._executor.execute_due(
            now=current,
            deliver=self._deliver,
            trace_id=self._trace_factory(),
        )

    async def start(self) -> None:
        if self.is_running:
            return
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._run(self._stop_event))

    async def stop(self) -> None:
        task = self._task
        stop_event = self._stop_event
        if task is None or stop_event is None:
            return
        stop_event.set()
        await task
        self._task = None
        self._stop_event = None

    async def _run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            await asyncio.to_thread(self.tick)
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=self._interval_seconds,
                )
            except asyncio.TimeoutError:
                continue
