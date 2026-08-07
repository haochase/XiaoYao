from __future__ import annotations

import struct
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass, replace
from threading import RLock
from typing import Protocol


UPLINK_SAMPLE_RATE = 16_000
DOWNLINK_SAMPLE_RATE = 24_000


class AudioFrameRejected(ValueError):
    pass


class AudioQueueFull(RuntimeError):
    pass


@dataclass(frozen=True)
class AudioMetrics:
    duration_ms: float
    duration_error_ms: float
    peak_abs: int
    non_silent_ratio: float


@dataclass(frozen=True)
class Pcm16Mono:
    sample_rate: int
    payload: bytes
    duration_error_ms: float = 0.0

    def __post_init__(self) -> None:
        if self.sample_rate < 1:
            raise AudioFrameRejected("sample_rate must be positive")
        if not self.payload:
            raise AudioFrameRejected("pcm payload is empty")
        if len(self.payload) % 2 != 0:
            raise AudioFrameRejected("pcm payload must contain int16 samples")

    @property
    def sample_count(self) -> int:
        return len(self.payload) // 2

    @property
    def duration_ms(self) -> float:
        return self.sample_count * 1_000 / self.sample_rate

    @property
    def metrics(self) -> AudioMetrics:
        samples = struct.unpack(f"<{self.sample_count}h", self.payload)
        non_silent = sum(sample != 0 for sample in samples)
        return AudioMetrics(
            duration_ms=self.duration_ms,
            duration_error_ms=self.duration_error_ms,
            peak_abs=max(abs(sample) for sample in samples),
            non_silent_ratio=non_silent / self.sample_count,
        )


class OpusCodec(Protocol):
    def decode_uplink(self, payload: bytes) -> Pcm16Mono: ...

    def encode_downlink(self, pcm: Pcm16Mono) -> bytes: ...


def resample_pcm16_mono(pcm: Pcm16Mono, *, target_sample_rate: int) -> Pcm16Mono:
    if target_sample_rate < 1:
        raise AudioFrameRejected("target_sample_rate must be positive")
    if pcm.sample_rate == target_sample_rate:
        return pcm

    source_samples = struct.unpack(f"<{pcm.sample_count}h", pcm.payload)
    target_count = round(pcm.sample_count * target_sample_rate / pcm.sample_rate)
    target_samples: list[int] = []
    for target_index in range(target_count):
        source_position = target_index * pcm.sample_rate / target_sample_rate
        left_index = int(source_position)
        right_index = min(left_index + 1, pcm.sample_count - 1)
        fraction = source_position - left_index
        interpolated = source_samples[left_index] + (
            source_samples[right_index] - source_samples[left_index]
        ) * fraction
        target_samples.append(round(interpolated))

    result = Pcm16Mono(
        sample_rate=target_sample_rate,
        payload=struct.pack(f"<{target_count}h", *target_samples),
    )
    return replace(
        result,
        duration_error_ms=result.duration_ms - pcm.duration_ms,
    )


class AudioBridge:
    def __init__(
        self,
        *,
        codec: OpusCodec,
        model_sample_rate: int,
        queue_capacity: int,
        max_uplink_frame_bytes: int = 4_096,
    ) -> None:
        if model_sample_rate < 1:
            raise ValueError("model_sample_rate must be positive")
        if queue_capacity < 1:
            raise ValueError("queue_capacity must be positive")
        if max_uplink_frame_bytes < 1:
            raise ValueError("max_uplink_frame_bytes must be positive")
        self._codec = codec
        self._model_sample_rate = model_sample_rate
        self._queue_capacity = queue_capacity
        self._max_uplink_frame_bytes = max_uplink_frame_bytes
        self._uplink_queue: deque[Pcm16Mono] = deque()
        self._lock = RLock()

    def decode_uplink(self, payload: bytes) -> Pcm16Mono:
        if not payload:
            raise AudioFrameRejected("opus frame is empty")
        if len(payload) > self._max_uplink_frame_bytes:
            raise AudioFrameRejected("opus frame is too large")
        with self._lock:
            if len(self._uplink_queue) >= self._queue_capacity:
                raise AudioQueueFull("decoded audio queue is full")

        decoded = self._codec.decode_uplink(bytes(payload))
        if decoded.sample_rate != UPLINK_SAMPLE_RATE:
            raise AudioFrameRejected(
                f"uplink codec returned {decoded.sample_rate} Hz PCM, expected "
                f"{UPLINK_SAMPLE_RATE} Hz"
            )
        model_pcm = resample_pcm16_mono(
            decoded,
            target_sample_rate=self._model_sample_rate,
        )
        with self._lock:
            if len(self._uplink_queue) >= self._queue_capacity:
                raise AudioQueueFull("decoded audio queue is full")
            self._uplink_queue.append(model_pcm)
        return model_pcm

    def pop_uplink(self) -> Pcm16Mono | None:
        with self._lock:
            if not self._uplink_queue:
                return None
            return self._uplink_queue.popleft()

    def drain_uplink(self) -> tuple[Pcm16Mono, ...]:
        with self._lock:
            frames = tuple(self._uplink_queue)
            self._uplink_queue.clear()
            return frames

    def queued_uplink(self) -> Iterator[Pcm16Mono]:
        with self._lock:
            return iter(tuple(self._uplink_queue))

    def encode_downlink(self, model_pcm: Pcm16Mono) -> bytes:
        if model_pcm.sample_rate != self._model_sample_rate:
            raise AudioFrameRejected(
                f"model PCM is {model_pcm.sample_rate} Hz, expected "
                f"{self._model_sample_rate} Hz"
            )
        device_pcm = resample_pcm16_mono(
            model_pcm,
            target_sample_rate=DOWNLINK_SAMPLE_RATE,
        )
        return self._codec.encode_downlink(device_pcm)
