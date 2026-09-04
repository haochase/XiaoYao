from __future__ import annotations

import asyncio
from concurrent.futures import Future, InvalidStateError
from dataclasses import dataclass
from threading import RLock
from typing import Literal

from companion_gateway.domain.models import TaskRecord


MAX_OPUS_FRAME_BYTES = 4_096
MAX_TTS_FRAMES = 128


class DeviceNotConnected(KeyError):
    pass


class DeviceOutboundBackpressure(RuntimeError):
    pass


def complete_delivery(completion: Future[None] | None) -> None:
    if completion is None:
        return
    try:
        completion.set_result(None)
    except InvalidStateError:
        pass


def fail_delivery(
    completion: Future[None] | None,
    error: BaseException,
) -> None:
    if completion is None:
        return
    try:
        completion.set_exception(error)
    except InvalidStateError:
        pass


@dataclass(frozen=True)
class OutboundTts:
    session_id: str
    opus_frames: tuple[bytes, ...]
    purpose: Literal["conversation", "notification"] = "conversation"
    delivery_completion: Future[None] | None = None


@dataclass(frozen=True)
class OutboundTask:
    session_id: str
    task: TaskRecord


@dataclass(frozen=True)
class OutboundControl:
    session_id: str
    payload: dict[str, object]


@dataclass(frozen=True)
class _OutboundChannel:
    loop: asyncio.AbstractEventLoop
    queue: asyncio.Queue[OutboundTts]
    task_queue: asyncio.Queue[OutboundTask]
    control_queue: asyncio.Queue[OutboundControl]


class DeviceTransport:
    """Thread-safe bridge from gateway services to active WebSocket sessions."""

    def __init__(self, *, queue_capacity: int = 8) -> None:
        if queue_capacity < 1:
            raise ValueError("queue_capacity must be positive")
        self._queue_capacity = queue_capacity
        self._channels: dict[str, _OutboundChannel] = {}
        self._lock = RLock()

    def register(self, session_id: str) -> None:
        channel = _OutboundChannel(
            loop=asyncio.get_running_loop(),
            queue=asyncio.Queue(maxsize=self._queue_capacity),
            task_queue=asyncio.Queue(maxsize=self._queue_capacity),
            control_queue=asyncio.Queue(maxsize=self._queue_capacity),
        )
        with self._lock:
            self._channels[session_id] = channel

    def unregister(self, session_id: str) -> None:
        with self._lock:
            channel = self._channels.pop(session_id, None)
        if channel is None:
            return
        while not channel.queue.empty():
            message = channel.queue.get_nowait()
            fail_delivery(
                message.delivery_completion,
                DeviceNotConnected(session_id),
            )

    async def next_tts(self, session_id: str) -> OutboundTts:
        with self._lock:
            channel = self._channels.get(session_id)
        if channel is None:
            raise DeviceNotConnected(session_id)
        return await channel.queue.get()

    async def next_task(self, session_id: str) -> OutboundTask:
        with self._lock:
            channel = self._channels.get(session_id)
        if channel is None:
            raise DeviceNotConnected(session_id)
        return await channel.task_queue.get()

    async def next_outbound(
        self,
        session_id: str,
    ) -> OutboundTts | OutboundTask | OutboundControl:
        with self._lock:
            channel = self._channels.get(session_id)
        if channel is None:
            raise DeviceNotConnected(session_id)
        if not channel.queue.empty():
            return channel.queue.get_nowait()
        if not channel.task_queue.empty():
            return channel.task_queue.get_nowait()
        if not channel.control_queue.empty():
            return channel.control_queue.get_nowait()

        tts_waiter = asyncio.create_task(channel.queue.get())
        task_waiter = asyncio.create_task(channel.task_queue.get())
        control_waiter = asyncio.create_task(channel.control_queue.get())
        waiters = (tts_waiter, task_waiter, control_waiter)
        try:
            done, _ = await asyncio.wait(
                waiters,
                return_when=asyncio.FIRST_COMPLETED,
            )
            selected = next(iter(done))
            for waiter in done:
                if waiter is selected:
                    continue
                message = waiter.result()
                if isinstance(message, OutboundTask):
                    channel.task_queue.put_nowait(message)
                elif isinstance(message, OutboundTts):
                    channel.queue.put_nowait(message)
                else:
                    channel.control_queue.put_nowait(message)
            return selected.result()
        finally:
            for waiter in waiters:
                if not waiter.done():
                    waiter.cancel()
            await asyncio.gather(*waiters, return_exceptions=True)

    def send_tts(self, session_id: str, opus_frame: bytes) -> None:
        self.send_tts_stream(session_id, (opus_frame,))

    def send_tts_stream(
        self,
        session_id: str,
        opus_frames: tuple[bytes, ...],
    ) -> None:
        self._send_tts_stream(
            session_id,
            opus_frames,
            purpose="conversation",
        )

    def send_notification_tts_stream(
        self,
        session_id: str,
        opus_frames: tuple[bytes, ...],
    ) -> Future[None]:
        completion = self._send_tts_stream(
            session_id,
            opus_frames,
            purpose="notification",
        )
        if completion is None:
            raise RuntimeError("notification delivery completion is unavailable")
        return completion

    def _send_tts_stream(
        self,
        session_id: str,
        opus_frames: tuple[bytes, ...],
        *,
        purpose: Literal["conversation", "notification"],
    ) -> Future[None] | None:
        frames = tuple(bytes(frame) for frame in opus_frames)
        if not frames:
            raise ValueError("opus_frames must not be empty")
        if len(frames) > MAX_TTS_FRAMES:
            raise ValueError("opus_frames exceeds the maximum stream length")
        if any(not frame for frame in frames):
            raise ValueError("opus frame must not be empty")
        if any(len(frame) > MAX_OPUS_FRAME_BYTES for frame in frames):
            raise ValueError("opus frame is too large")
        with self._lock:
            channel = self._channels.get(session_id)
        if channel is None:
            raise DeviceNotConnected(session_id)

        delivery_completion: Future[None] | None = (
            Future() if purpose == "notification" else None
        )
        message = OutboundTts(
            session_id=session_id,
            opus_frames=frames,
            purpose=purpose,
            delivery_completion=delivery_completion,
        )
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None
        if current_loop is channel.loop:
            self._enqueue(channel, message)
            return delivery_completion

        completion: Future[None] = Future()
        channel.loop.call_soon_threadsafe(
            self._enqueue_with_completion,
            channel,
            message,
            completion,
        )
        completion.result(timeout=1.0)
        return delivery_completion

    def send_task(self, session_id: str, task: TaskRecord) -> None:
        with self._lock:
            channel = self._channels.get(session_id)
        if channel is None:
            raise DeviceNotConnected(session_id)

        message = OutboundTask(session_id=session_id, task=task)
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None
        if current_loop is channel.loop:
            self._enqueue_task(channel, message)
            return

        completion: Future[None] = Future()
        channel.loop.call_soon_threadsafe(
            self._enqueue_task_with_completion,
            channel,
            message,
            completion,
        )
        completion.result(timeout=1.0)

    def send_control(self, session_id: str, payload: dict[str, object]) -> None:
        with self._lock:
            channel = self._channels.get(session_id)
        if channel is None:
            raise DeviceNotConnected(session_id)
        message = OutboundControl(session_id=session_id, payload=dict(payload))
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None
        if current_loop is channel.loop:
            self._enqueue_control(channel, message)
            return

        completion: Future[None] = Future()
        channel.loop.call_soon_threadsafe(
            self._enqueue_control_with_completion,
            channel,
            message,
            completion,
        )
        completion.result(timeout=1.0)

    @staticmethod
    def _enqueue(channel: _OutboundChannel, message: OutboundTts) -> None:
        if channel.queue.full():
            raise DeviceOutboundBackpressure("device outbound queue is full")
        channel.queue.put_nowait(message)

    def _enqueue_with_completion(
        self,
        channel: _OutboundChannel,
        message: OutboundTts,
        completion: Future[None],
    ) -> None:
        try:
            with self._lock:
                active = self._channels.get(message.session_id)
            if active is not channel:
                raise DeviceNotConnected(message.session_id)
            self._enqueue(channel, message)
        except BaseException as exc:
            completion.set_exception(exc)
        else:
            completion.set_result(None)

    @staticmethod
    def _enqueue_task(
        channel: _OutboundChannel,
        message: OutboundTask,
    ) -> None:
        if channel.task_queue.full():
            raise DeviceOutboundBackpressure("device task queue is full")
        channel.task_queue.put_nowait(message)

    def _enqueue_task_with_completion(
        self,
        channel: _OutboundChannel,
        message: OutboundTask,
        completion: Future[None],
    ) -> None:
        try:
            with self._lock:
                active = self._channels.get(message.session_id)
            if active is not channel:
                raise DeviceNotConnected(message.session_id)
            self._enqueue_task(channel, message)
        except BaseException as exc:
            completion.set_exception(exc)
        else:
            completion.set_result(None)

    @staticmethod
    def _enqueue_control(
        channel: _OutboundChannel,
        message: OutboundControl,
    ) -> None:
        if channel.control_queue.full():
            raise DeviceOutboundBackpressure("device outbound queue is full")
        channel.control_queue.put_nowait(message)

    def _enqueue_control_with_completion(
        self,
        channel: _OutboundChannel,
        message: OutboundControl,
        completion: Future[None],
    ) -> None:
        try:
            with self._lock:
                active = self._channels.get(message.session_id)
            if active is not channel:
                raise DeviceNotConnected(message.session_id)
            self._enqueue_control(channel, message)
        except BaseException as exc:
            completion.set_exception(exc)
        else:
            completion.set_result(None)
