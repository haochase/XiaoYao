from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest
from pydantic import ValidationError

import companion_gateway.device.models as device_models
from companion_gateway.device.events import (
    BoundedDeviceEventSink,
    DeviceBackpressure,
)
from companion_gateway.device.models import AbortControl, DeviceHello, ListenControl
from companion_gateway.device.session import (
    DeviceAuthenticator,
    DevicePhase,
    DeviceSession,
    DeviceSessionRegistry,
    InvalidDevicePhase,
    redact_device_id,
)


def hello_payload() -> dict[str, object]:
    return {
        "type": "hello",
        "version": 1,
        "transport": "websocket",
        "features": {"mcp": True, "future_capability": True},
        "audio_params": {
            "format": "opus",
            "sample_rate": 16000,
            "channels": 1,
            "frame_duration": 60,
        },
    }


def test_device_hello_accepts_xiaozhi_v1_and_ignores_future_fields() -> None:
    payload = hello_payload()
    payload["future_field"] = {"safe_to_ignore": True}

    hello = DeviceHello.model_validate(payload)

    assert hello.version == 1
    assert hello.audio_params.sample_rate == 16000
    assert hello.audio_params.frame_duration == 60


def test_device_hello_parses_vad_event_capability() -> None:
    payload = hello_payload()
    payload["features"]["vad_events"] = True

    hello = DeviceHello.model_validate(payload)

    assert hello.features.vad_events is True


def test_vad_control_accepts_only_speech_boundaries() -> None:
    start = device_models.VadControl.model_validate(
        {"type": "vad", "state": "start", "session_id": "ses_1"}
    )

    assert start.state == "start"
    with pytest.raises(ValidationError):
        device_models.VadControl.model_validate({"type": "vad", "state": "pause"})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("format", "pcm"),
        ("sample_rate", 24000),
        ("channels", 2),
        ("frame_duration", 20),
    ],
)
def test_device_hello_rejects_unsupported_audio_contract(
    field: str,
    value: object,
) -> None:
    payload = hello_payload()
    payload["audio_params"][field] = value

    with pytest.raises(ValidationError):
        DeviceHello.model_validate(payload)


def test_listen_control_uses_native_xiaozhi_shape() -> None:
    message = ListenControl.model_validate(
        {
            "type": "listen",
            "state": "start",
            "mode": "manual",
            "session_id": "ses_1",
        }
    )

    assert message.state == "start"
    assert message.mode == "manual"


def test_authenticator_compares_token_against_sha256_digest() -> None:
    token = "local-test-token"
    authenticator = DeviceAuthenticator(
        {"dev-test": sha256(token.encode("utf-8")).hexdigest()}
    )

    assert authenticator.verify("dev-test", token) is True
    assert authenticator.verify("dev-test", "wrong-token") is False
    assert authenticator.verify("unknown-device", token) is False


def test_session_rejects_audio_until_listening() -> None:
    session = DeviceSession.create(
        device_id="dev-test",
        client_id="client-test",
        hello=DeviceHello.model_validate(hello_payload()),
    )

    with pytest.raises(InvalidDevicePhase, match="audio frame"):
        session.accept_audio_frame()

    session.apply_listen(
        ListenControl.model_validate({"type": "listen", "state": "start"})
    )
    session.accept_audio_frame()

    assert session.phase is DevicePhase.LISTENING
    assert session.audio_frames_received == 1


def test_session_tracks_connection_and_last_activity_time() -> None:
    connected_at = datetime(2026, 8, 5, 8, 0, tzinfo=UTC)
    later = connected_at + timedelta(seconds=5)
    session = DeviceSession.create(
        device_id="dev-test",
        client_id="client-test",
        hello=DeviceHello.model_validate(hello_payload()),
        clock=lambda: connected_at,
    )

    session.touch(clock=lambda: later)

    assert session.connected_at == connected_at
    assert session.last_seen_at == later


def test_speaking_state_has_explicit_start_and_stop() -> None:
    session = DeviceSession.create(
        device_id="dev-test",
        client_id="client-test",
        hello=DeviceHello.model_validate(hello_payload()),
    )

    session.start_speaking()
    assert session.phase is DevicePhase.SPEAKING

    session.stop_speaking()
    assert session.phase is DevicePhase.IDLE

    with pytest.raises(InvalidDevicePhase):
        session.stop_speaking()


def test_session_stop_returns_to_idle_and_rejects_more_audio() -> None:
    session = DeviceSession.create(
        device_id="dev-test",
        client_id="client-test",
        hello=DeviceHello.model_validate(hello_payload()),
    )
    session.apply_listen(
        ListenControl.model_validate({"type": "listen", "state": "start"})
    )
    session.apply_listen(
        ListenControl.model_validate({"type": "listen", "state": "stop"})
    )

    assert session.phase is DevicePhase.IDLE
    with pytest.raises(InvalidDevicePhase):
        session.accept_audio_frame()


def test_auto_listening_session_can_finish_only_once() -> None:
    session = DeviceSession.create(
        device_id="dev-test",
        client_id="client-test",
        hello=DeviceHello.model_validate(hello_payload()),
    )
    session.apply_listen(
        ListenControl.model_validate(
            {"type": "listen", "state": "start", "mode": "auto"}
        )
    )

    assert session.finish_auto_listening() is True
    assert session.phase is DevicePhase.IDLE
    assert session.finish_auto_listening() is False


def test_session_accepts_vad_only_when_capability_was_advertised() -> None:
    legacy_session = DeviceSession.create(
        device_id="dev-test",
        client_id="client-test",
        hello=DeviceHello.model_validate(hello_payload()),
    )
    legacy_session.apply_listen(
        ListenControl.model_validate(
            {"type": "listen", "state": "start", "mode": "auto"}
        )
    )
    vad_start = device_models.VadControl.model_validate(
        {"type": "vad", "state": "start"}
    )

    with pytest.raises(InvalidDevicePhase, match="not advertised"):
        legacy_session.apply_vad(vad_start)

    capable_payload = hello_payload()
    capable_payload["features"]["vad_events"] = True
    capable_session = DeviceSession.create(
        device_id="dev-test",
        client_id="client-test",
        hello=DeviceHello.model_validate(capable_payload),
    )
    capable_session.apply_listen(
        ListenControl.model_validate(
            {"type": "listen", "state": "start", "mode": "auto"}
        )
    )

    capable_session.apply_vad(vad_start)


def test_auto_turn_finished_session_ignores_tail_audio() -> None:
    session = DeviceSession.create(
        device_id="dev-test",
        client_id="client-test",
        hello=DeviceHello.model_validate(hello_payload()),
    )
    session.apply_listen(
        ListenControl.model_validate(
            {"type": "listen", "state": "start", "mode": "auto"}
        )
    )
    assert session.finish_auto_listening() is True

    assert session.should_ignore_auto_turn_tail_audio() is True


def test_abort_returns_listening_session_to_idle() -> None:
    session = DeviceSession.create(
        device_id="dev-test",
        client_id="client-test",
        hello=DeviceHello.model_validate(hello_payload()),
    )
    session.apply_listen(
        ListenControl.model_validate({"type": "listen", "state": "start"})
    )

    session.apply_abort(
        AbortControl.model_validate(
            {"type": "abort", "reason": "wake_word_detected"}
        )
    )

    assert session.phase is DevicePhase.IDLE


def test_bounded_sink_rejects_new_frame_without_dropping_old_frame() -> None:
    sink = BoundedDeviceEventSink(capacity=1)
    session = DeviceSession.create(
        device_id="dev-test",
        client_id="client-test",
        hello=DeviceHello.model_validate(hello_payload()),
    )

    sink.on_audio(session, b"first")
    with pytest.raises(DeviceBackpressure, match="buffer is full"):
        sink.on_audio(session, b"second")

    assert [frame.payload for frame in sink.audio_snapshot()] == [b"first"]


def test_registry_only_removes_the_matching_active_session() -> None:
    registry = DeviceSessionRegistry()
    first = DeviceSession.create(
        device_id="dev-test",
        client_id="client-1",
        hello=DeviceHello.model_validate(hello_payload()),
    )
    second = DeviceSession.create(
        device_id="dev-test",
        client_id="client-2",
        hello=DeviceHello.model_validate(hello_payload()),
    )

    assert registry.connect(first) is None
    assert registry.connect(second) is first
    registry.disconnect(first)

    assert registry.get("dev-test") is second
    registry.disconnect(second)
    assert registry.get("dev-test") is None


def test_mac_shaped_device_id_is_redacted() -> None:
    device_id = "AA:BB:CC:DD:EE:FF"

    redacted = redact_device_id(device_id)

    assert device_id not in redacted
    assert "AA:BB" not in redacted
