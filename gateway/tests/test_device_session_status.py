from datetime import UTC, datetime

from companion_gateway.device.models import DeviceHello
from companion_gateway.device.session import (
    DevicePhase,
    DeviceSession,
    DeviceSessionRegistry,
)


NOW = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)


def hello() -> DeviceHello:
    return DeviceHello.model_validate(
        {
            "type": "hello",
            "version": 1,
            "transport": "websocket",
            "features": {"mcp": True},
            "audio_params": {
                "format": "opus",
                "sample_rate": 16_000,
                "channels": 1,
                "frame_duration": 60,
            },
        }
    )


def make_session(device_id: str, client_id: str) -> DeviceSession:
    return DeviceSession.create(
        device_id=device_id,
        client_id=client_id,
        hello=hello(),
        clock=lambda: NOW,
    )


def test_registry_returns_offline_snapshot_for_unknown_device() -> None:
    status = DeviceSessionRegistry().status("dev-living-room")

    assert status.device_id == "dev-living-room"
    assert status.status == "offline"
    assert status.session_id is None
    assert status.connected_at is None
    assert status.last_seen_at is None
    assert status.phase is None
    assert status.audio_frames_received == 0


def test_registry_snapshot_copies_active_session_state() -> None:
    registry = DeviceSessionRegistry()
    session = make_session("dev-living-room", "xiaozhi")
    registry.connect(session)

    initial = registry.status("dev-living-room")
    session.phase = DevicePhase.LISTENING
    session.listening_mode = "manual"
    session.audio_frames_received = 2
    current = registry.status("dev-living-room")

    assert initial.status == "online"
    assert initial.phase is DevicePhase.IDLE
    assert initial.audio_frames_received == 0
    assert current.session_id == session.session_id
    assert current.phase is DevicePhase.LISTENING
    assert current.listening_mode == "manual"
    assert current.audio_frames_received == 2


def test_registry_disconnect_of_replaced_session_keeps_new_session_online() -> None:
    registry = DeviceSessionRegistry()
    first = make_session("dev-living-room", "first")
    second = make_session("dev-living-room", "second")

    registry.connect(first)
    registry.connect(second)
    registry.disconnect(first)

    status = registry.status("dev-living-room")

    assert status.status == "online"
    assert status.session_id == second.session_id
