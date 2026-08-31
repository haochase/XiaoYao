from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable


IdleCallback = Callable[[int], Awaitable[None]]
logger = logging.getLogger(__name__)


class ConversationIdleController:
    """Own one cancellable idle timer for a WebSocket conversation."""

    def __init__(self, *, timeout_seconds: float, on_timeout: IdleCallback) -> None:
        if timeout_seconds <= 0:
            raise ValueError("conversation idle timeout must be positive")
        self._timeout_seconds = timeout_seconds
        self._on_timeout = on_timeout
        self._generation: int | None = None
        self._task: asyncio.Task[None] | None = None

    def arm(self, generation: int) -> None:
        if generation < 0:
            raise ValueError("conversation generation must not be negative")
        self.cancel()
        self._generation = generation
        self._task = asyncio.create_task(self._wait(generation))

    def cancel(self) -> None:
        task = self._task
        self._task = None
        self._generation = None
        if task is not None and not task.done():
            task.cancel()

    def is_current(self, generation: int) -> bool:
        return self._generation == generation

    async def _wait(self, generation: int) -> None:
        try:
            await asyncio.sleep(self._timeout_seconds)
            if self.is_current(generation):
                await self._on_timeout(generation)
        except asyncio.CancelledError:
            return
        except Exception:
            logger.warning("conversation_idle_callback_failed", exc_info=True)
