from __future__ import annotations

import asyncio
from concurrent.futures import Future
from dataclasses import dataclass
from threading import RLock


MAX_OPUS_FRAME_BYTES = 4_096
MAX_TTS_FRAMES = 128


class DeviceNotConnected(KeyError):
    pass


class DeviceOutboundBackpressure(RuntimeError):
    pass


@dataclass(frozen=True)
class OutboundTts:
    session_id: str
    opus_frames: tuple[bytes, ...]


@dataclass(frozen=True)
class _OutboundChannel:
    loop: asyncio.AbstractEventLoop
    queue: asyncio.Queue[OutboundTts]


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
        )
        with self._lock:
            self._channels[session_id] = channel

    def unregister(self, session_id: str) -> None:
        with self._lock:
            self._channels.pop(session_id, None)

    async def next_tts(self, session_id: str) -> OutboundTts:
        with self._lock:
            channel = self._channels.get(session_id)
        if channel is None:
            raise DeviceNotConnected(session_id)
        return await channel.queue.get()

    def send_tts(self, session_id: str, opus_frame: bytes) -> None:
        self.send_tts_stream(session_id, (opus_frame,))

    def send_tts_stream(
        self,
        session_id: str,
        opus_frames: tuple[bytes, ...],
    ) -> None:
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

        message = OutboundTts(session_id=session_id, opus_frames=frames)
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None
        if current_loop is channel.loop:
            self._enqueue(channel, message)
            return

        completion: Future[None] = Future()
        channel.loop.call_soon_threadsafe(
            self._enqueue_with_completion,
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
