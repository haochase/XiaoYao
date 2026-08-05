from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from secrets import compare_digest
from threading import RLock
from uuid import uuid4

from companion_gateway.device.models import AbortControl, DeviceHello, ListenControl


Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(UTC)


class DevicePhase(StrEnum):
    IDLE = "idle"
    LISTENING = "listening"
    SPEAKING = "speaking"
    CLOSED = "closed"


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

    def touch(self, *, clock: Clock = _utc_now) -> None:
        self.last_seen_at = clock()

    def apply_listen(self, control: ListenControl) -> None:
        if control.session_id not in (None, self.session_id):
            raise InvalidDevicePhase("listen message has the wrong session_id")

        if control.state == "start":
            if self.phase is not DevicePhase.IDLE:
                raise InvalidDevicePhase(
                    f"cannot start listening while {self.phase.value}"
                )
            self.phase = DevicePhase.LISTENING
            return

        if control.state == "stop":
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

    def accept_audio_frame(self) -> None:
        if self.phase is not DevicePhase.LISTENING:
            raise InvalidDevicePhase(
                f"cannot accept audio frame while {self.phase.value}"
            )
        self.audio_frames_received += 1

    def apply_abort(self, control: AbortControl) -> None:
        if control.session_id not in (None, self.session_id):
            raise InvalidDevicePhase("abort message has the wrong session_id")
        if self.phase is DevicePhase.CLOSED:
            raise InvalidDevicePhase("cannot abort a closed session")
        self.phase = DevicePhase.IDLE

    def start_speaking(self) -> None:
        if self.phase is not DevicePhase.IDLE:
            raise InvalidDevicePhase(
                f"cannot start speaking while {self.phase.value}"
            )
        self.phase = DevicePhase.SPEAKING

    def stop_speaking(self) -> None:
        if self.phase is not DevicePhase.SPEAKING:
            raise InvalidDevicePhase(
                f"cannot stop speaking while {self.phase.value}"
            )
        self.phase = DevicePhase.IDLE

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


def redact_device_id(device_id: str) -> str:
    digest = sha256(device_id.encode("utf-8")).hexdigest()[:12]
    return f"device:{digest}"
