import asyncio
import logging
import re
import threading
from datetime import UTC, datetime

import pytest

from companion_gateway.meeting.scheduler import MeetingScheduler


NOW = datetime(2026, 8, 27, 1, 0, tzinfo=UTC)


class RecordingService:
    def __init__(self) -> None:
        self.calls: list[tuple[datetime, str]] = []

    def tick(self, *, now: datetime, trace_id: str):
        self.calls.append((now, trace_id))
        return "tick-result"


@pytest.mark.parametrize("interval_seconds", [0, -1])
def test_scheduler_requires_a_positive_interval(interval_seconds) -> None:
    with pytest.raises(ValueError, match="positive"):
        MeetingScheduler(
            service=RecordingService(),
            interval_seconds=interval_seconds,
        )


def test_tick_uses_the_injected_clock_and_a_unique_trace_id() -> None:
    service = RecordingService()
    scheduler = MeetingScheduler(
        service=service,
        interval_seconds=1,
        clock=lambda: NOW,
    )

    result = scheduler.tick()

    assert result == "tick-result"
    assert service.calls[0][0] == NOW
    assert re.fullmatch(r"trc_meeting_tick_[0-9a-f]{32}", service.calls[0][1])


def test_tick_rejects_a_naive_clock_value() -> None:
    scheduler = MeetingScheduler(
        service=RecordingService(),
        interval_seconds=1,
        clock=lambda: datetime(2026, 8, 27, 1, 0),
    )

    with pytest.raises(ValueError, match="timezone-aware"):
        scheduler.tick()


def test_scheduler_start_stop_manage_one_threaded_loop() -> None:
    async def scenario() -> None:
        main_thread_id = threading.get_ident()
        tick_thread_ids: list[int] = []
        ticked = threading.Event()

        class ThreadRecordingService:
            def tick(self, **_kwargs):
                tick_thread_ids.append(threading.get_ident())
                ticked.set()

        scheduler = MeetingScheduler(
            service=ThreadRecordingService(),
            interval_seconds=0.01,
            clock=lambda: NOW,
        )

        await scheduler.start()
        await scheduler.start()
        for _ in range(100):
            if ticked.is_set():
                break
            await asyncio.sleep(0.005)
        await scheduler.stop()
        await scheduler.stop()

        assert scheduler.is_running is False
        assert tick_thread_ids
        assert all(thread_id != main_thread_id for thread_id in tick_thread_ids)

    asyncio.run(scenario())


def test_scheduler_survives_one_service_exception_and_logs_class_only(
    caplog,
) -> None:
    async def scenario() -> None:
        recovered = threading.Event()

        class SensitiveTickError(RuntimeError):
            pass

        class FlakyService:
            def __init__(self) -> None:
                self.calls = 0

            def tick(self, **_kwargs):
                self.calls += 1
                if self.calls == 1:
                    raise SensitiveTickError(
                        "raw-owner-id raw-meeting-content raw-device-id"
                    )
                recovered.set()

        service = FlakyService()
        scheduler = MeetingScheduler(
            service=service,
            interval_seconds=0.01,
            clock=lambda: NOW,
        )

        with caplog.at_level(
            logging.WARNING,
            logger="companion_gateway.meeting.scheduler",
        ):
            await scheduler.start()
            for _ in range(100):
                if recovered.is_set():
                    break
                await asyncio.sleep(0.005)
            await scheduler.stop()

        assert recovered.is_set()
        assert service.calls >= 2

    asyncio.run(scenario())

    messages = [record.getMessage() for record in caplog.records]
    assert messages == [
        "meeting_tick_failed error_type=SensitiveTickError"
    ]
    joined = " ".join(messages)
    assert "raw-owner-id" not in joined
    assert "raw-meeting-content" not in joined
    assert "raw-device-id" not in joined
