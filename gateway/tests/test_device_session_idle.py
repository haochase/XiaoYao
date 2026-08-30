from __future__ import annotations

import asyncio

from companion_gateway.device.idle import ConversationIdleController


def test_idle_controller_invokes_callback_for_current_generation() -> None:
    async def scenario() -> None:
        events: list[int] = []

        async def on_timeout(generation: int) -> None:
            events.append(generation)

        controller = ConversationIdleController(
            timeout_seconds=0.01,
            on_timeout=on_timeout,
        )
        controller.arm(3)
        await asyncio.sleep(0.03)

        assert events == [3]
        assert controller.is_current(3) is True

    asyncio.run(scenario())


def test_idle_controller_replacing_generation_cancels_old_task() -> None:
    async def scenario() -> None:
        events: list[int] = []

        async def on_timeout(generation: int) -> None:
            events.append(generation)

        controller = ConversationIdleController(
            timeout_seconds=0.02,
            on_timeout=on_timeout,
        )
        controller.arm(1)
        await asyncio.sleep(0.005)
        controller.arm(2)
        await asyncio.sleep(0.03)

        assert events == [2]
        assert controller.is_current(1) is False

    asyncio.run(scenario())


def test_idle_controller_cancel_prevents_callback_and_cleans_task() -> None:
    async def scenario() -> None:
        called = False

        async def on_timeout(generation: int) -> None:
            nonlocal called
            called = True

        controller = ConversationIdleController(
            timeout_seconds=0.01,
            on_timeout=on_timeout,
        )
        controller.arm(1)
        controller.cancel()
        await asyncio.sleep(0.02)

        assert called is False
        assert controller.is_current(1) is False

    asyncio.run(scenario())
