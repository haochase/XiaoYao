import asyncio
from datetime import UTC, datetime

from companion_gateway.vision.scheduler import VisionScheduler


def test_vision_scheduler_tick_uses_injected_clock() -> None:
    expected = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)
    calls: list[datetime] = []

    class FakeService:
        def purge(self, *, now: datetime) -> int:
            calls.append(now)
            return 2

    scheduler = VisionScheduler(
        service=FakeService(),
        interval_seconds=86_400,
        clock=lambda: expected,
    )

    assert scheduler.tick() == 2
    assert calls == [expected]


def test_vision_scheduler_start_stop_manage_one_loop() -> None:
    async def scenario() -> None:
        calls = 0

        class FakeService:
            def purge(self, *, now: datetime) -> int:
                nonlocal calls
                calls += 1
                return 0

        scheduler = VisionScheduler(service=FakeService(), interval_seconds=0.01)

        await scheduler.start()
        await scheduler.start()
        await asyncio.sleep(0.03)
        await scheduler.stop()
        await scheduler.stop()

        assert scheduler.is_running is False
        assert calls >= 1

    asyncio.run(scenario())
