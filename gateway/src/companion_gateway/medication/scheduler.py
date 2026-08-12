from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import uuid4

from companion_gateway.domain.medication import MedicationTickResult
from companion_gateway.medication.service import MedicationReminderService


Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(UTC)


class MedicationScheduler:
    def __init__(
        self,
        *,
        service: MedicationReminderService,
        interval_seconds: float,
        clock: Clock = _utc_now,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self._service = service
        self._interval_seconds = interval_seconds
        self._clock = clock
        self._task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event | None = None

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def tick(self, *, now: datetime | None = None) -> MedicationTickResult:
        current = now or self._clock()
        return self._service.tick(
            now=current,
            trace_id=f"trc_medication_tick_{uuid4().hex}",
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
