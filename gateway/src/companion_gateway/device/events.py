from collections import deque
from dataclasses import dataclass
from threading import RLock

from companion_gateway.device.models import DeviceControl
from companion_gateway.device.session import DeviceSession, redact_device_id


class DeviceBackpressure(RuntimeError):
    pass


@dataclass(frozen=True)
class ReceivedAudioFrame:
    session_id: str
    device_ref: str
    payload: bytes


@dataclass(frozen=True)
class ReceivedControl:
    session_id: str
    device_ref: str
    control: DeviceControl


class BoundedDeviceEventSink:
    def __init__(self, *, capacity: int = 128) -> None:
        if capacity < 1:
            raise ValueError("capacity must be positive")
        self._capacity = capacity
        self._audio: deque[ReceivedAudioFrame] = deque()
        self._controls: deque[ReceivedControl] = deque()
        self._lock = RLock()

    def on_audio(self, session: DeviceSession, payload: bytes) -> None:
        with self._lock:
            if len(self._audio) >= self._capacity:
                raise DeviceBackpressure("device audio buffer is full")
            self._audio.append(
                ReceivedAudioFrame(
                    session_id=session.session_id,
                    device_ref=redact_device_id(session.device_id),
                    payload=bytes(payload),
                )
            )

    def on_control(
        self,
        session: DeviceSession,
        control: DeviceControl,
    ) -> None:
        with self._lock:
            if len(self._controls) >= self._capacity:
                raise DeviceBackpressure("device control buffer is full")
            self._controls.append(
                ReceivedControl(
                    session_id=session.session_id,
                    device_ref=redact_device_id(session.device_id),
                    control=control,
                )
            )

    def audio_snapshot(self) -> list[ReceivedAudioFrame]:
        with self._lock:
            return list(self._audio)

    def control_snapshot(self) -> list[ReceivedControl]:
        with self._lock:
            return list(self._controls)
