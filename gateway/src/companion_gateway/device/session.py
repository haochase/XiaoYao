from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from secrets import compare_digest
from threading import RLock
from typing import Literal
from uuid import uuid4
from functools import wraps

from companion_gateway.device.models import (
    AbortControl,
    DeviceHello,
    ListenControl,
    VadControl,
)


Clock = Callable[[], datetime]


def _with_state_lock(method):
    @wraps(method)
    def locked(self, *args, **kwargs):
        with self._state_lock:
            return method(self, *args, **kwargs)

    return locked


def _utc_now() -> datetime:
    return datetime.now(UTC)


class DevicePhase(StrEnum):
    IDLE = "idle"
    LISTENING = "listening"
    SPEAKING = "speaking"
    CLOSED = "closed"


@dataclass(frozen=True)
class DeviceStatusSnapshot:
    device_id: str
    status: Literal["online", "offline"]
    session_id: str | None = None
    connected_at: datetime | None = None
    last_seen_at: datetime | None = None
    phase: DevicePhase | None = None
    listening_mode: str | None = None
    audio_frames_received: int = 0


class InvalidDevicePhase(ValueError):
    pass


class DeviceAuthenticator:
    def __init__(self, token_hashes: Mapping[str, str]) -> None:
        self._token_hashes = {
            device_id: digest.lower()
            for device_id, digest in token_hashes.items()
        }

    def verify(self, device_id: str, token: str) -> bool:
        expected = self._token_hashes.get(device_id)
        if expected is None or not token:
            return False
        actual = sha256(token.encode("utf-8")).hexdigest()
        return compare_digest(actual, expected)


@dataclass
class DeviceSession:
    device_id: str
    client_id: str
    session_id: str
    hello: DeviceHello
    connected_at: datetime
    last_seen_at: datetime
    phase: DevicePhase = DevicePhase.IDLE
    audio_frames_received: int = 0
    listening_mode: str | None = None
    wake_word_detected: bool = False
    listening_started: bool = False
    auto_turn_finished: bool = False
    _state_lock: RLock = field(
        default_factory=RLock,
        init=False,
        repr=False,
        compare=False,
    )

    @classmethod
    def create(
        cls,
        *,
        device_id: str,
        client_id: str,
        hello: DeviceHello,
        clock: Clock = _utc_now,
    ) -> "DeviceSession":
        now = clock()
        return cls(
            device_id=device_id,
            client_id=client_id,
            session_id=f"ses_{uuid4().hex}",
            hello=hello,
            connected_at=now,
            last_seen_at=now,
        )

    @_with_state_lock
    def touch(self, *, clock: Clock = _utc_now) -> None:
        self.last_seen_at = clock()

    @_with_state_lock
    def apply_listen(self, control: ListenControl) -> None:
        if control.session_id not in (None, self.session_id):
            raise InvalidDevicePhase("listen message has the wrong session_id")

        if control.state == "start":
            if self.phase is not DevicePhase.IDLE:
                raise InvalidDevicePhase(
                    f"cannot start listening while {self.phase.value}"
                )
            self.listening_mode = control.mode
            self.listening_started = True
            self.auto_turn_finished = False
            self.phase = DevicePhase.LISTENING
            return

        if control.state == "stop":
            if self.phase is DevicePhase.IDLE and self.auto_turn_finished:
                return
            if self.phase is not DevicePhase.LISTENING:
                raise InvalidDevicePhase(
                    f"cannot stop listening while {self.phase.value}"
                )
            self.phase = DevicePhase.IDLE
            return

        if self.phase not in (DevicePhase.IDLE, DevicePhase.LISTENING):
            raise InvalidDevicePhase(
                f"cannot report wake word while {self.phase.value}"
            )
        self.wake_word_detected = True
        if control.mode is not None:
            self.listening_mode = control.mode

    @_with_state_lock
    def should_ignore_wake_word_audio(self) -> bool:
        return (
            self.phase is DevicePhase.IDLE
            and not self.wake_word_detected
            and not self.listening_started
        )

    @_with_state_lock
    def should_ignore_auto_turn_tail_audio(self) -> bool:
        return self.phase is DevicePhase.IDLE and self.auto_turn_finished

    @_with_state_lock
    def finish_auto_listening(self) -> bool:
        if self.phase is not DevicePhase.LISTENING or self.listening_mode != "auto":
            return False
        self.phase = DevicePhase.IDLE
        self.auto_turn_finished = True
        return True

    @_with_state_lock
    def apply_vad(self, control: VadControl) -> None:
        if not self.hello.features.vad_events:
            raise InvalidDevicePhase("VAD events were not advertised")
        if control.session_id not in (None, self.session_id):
            raise InvalidDevicePhase("VAD message has the wrong session_id")
        if (
            control.state == "stop"
            and self.phase is DevicePhase.IDLE
            and self.auto_turn_finished
        ):
            return
        if self.phase is not DevicePhase.LISTENING:
            raise InvalidDevicePhase(
                f"cannot report VAD while {self.phase.value}"
            )

    @_with_state_lock
    def accept_audio_frame(self) -> None:
        if self.phase is not DevicePhase.LISTENING:
            raise InvalidDevicePhase(
                f"cannot accept audio frame while {self.phase.value}"
            )
        self.audio_frames_received += 1

    @_with_state_lock
    def apply_abort(self, control: AbortControl) -> None:
        if control.session_id not in (None, self.session_id):
            raise InvalidDevicePhase("abort message has the wrong session_id")
        if self.phase is DevicePhase.CLOSED:
            raise InvalidDevicePhase("cannot abort a closed session")
        self.phase = DevicePhase.IDLE

    @_with_state_lock
    def start_speaking(self) -> None:
        if self.phase is not DevicePhase.IDLE:
            raise InvalidDevicePhase(
                f"cannot start speaking while {self.phase.value}"
            )
        self.phase = DevicePhase.SPEAKING

    @_with_state_lock
    def stop_speaking(self) -> None:
        if self.phase is not DevicePhase.SPEAKING:
            raise InvalidDevicePhase(
                f"cannot stop speaking while {self.phase.value}"
            )
        self.phase = DevicePhase.IDLE

    @_with_state_lock
    def close(self) -> None:
        self.phase = DevicePhase.CLOSED


class DeviceSessionRegistry:
    def __init__(self) -> None:
        self._sessions: dict[str, DeviceSession] = {}
        self._lock = RLock()

    def connect(self, session: DeviceSession) -> DeviceSession | None:
        with self._lock:
            previous = self._sessions.get(session.device_id)
            self._sessions[session.device_id] = session
            return previous

    def disconnect(self, session: DeviceSession) -> None:
        with self._lock:
            current = self._sessions.get(session.device_id)
            if current is session:
                del self._sessions[session.device_id]
            session.close()

    def get(self, device_id: str) -> DeviceSession | None:
        with self._lock:
            return self._sessions.get(device_id)

    def status(self, device_id: str) -> DeviceStatusSnapshot:
        with self._lock:
            session = self._sessions.get(device_id)
            if session is None:
                return DeviceStatusSnapshot(
                    device_id=device_id,
                    status="offline",
                )
            return DeviceStatusSnapshot(
                device_id=session.device_id,
                status="online",
                session_id=session.session_id,
                connected_at=session.connected_at,
                last_seen_at=session.last_seen_at,
                phase=session.phase,
                listening_mode=session.listening_mode,
                audio_frames_received=session.audio_frames_received,
            )


def redact_device_id(device_id: str) -> str:
    digest = sha256(device_id.encode("utf-8")).hexdigest()[:12]
    return f"device:{digest}"
