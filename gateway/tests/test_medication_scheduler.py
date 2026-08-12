import asyncio

from companion_gateway.medication.scheduler import MedicationScheduler


def test_medication_scheduler_start_stop_manage_one_loop() -> None:
    async def scenario() -> None:
        calls = 0

        class FakeService:
            def tick(self, **_kwargs):
                nonlocal calls
                calls += 1

        scheduler = MedicationScheduler(service=FakeService(), interval_seconds=0.01)

        await scheduler.start()
        await scheduler.start()
        await asyncio.sleep(0.03)
        await scheduler.stop()
        await scheduler.stop()

        assert scheduler.is_running is False
        assert calls >= 1

    asyncio.run(scenario())
