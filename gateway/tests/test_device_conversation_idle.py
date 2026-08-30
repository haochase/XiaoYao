from __future__ import annotations

from hashlib import sha256

from fastapi.testclient import TestClient

from companion_gateway.api import create_app
from companion_gateway.device.models import DeviceHello
from companion_gateway.device.session import DevicePhase, DeviceSession
from companion_gateway.voice.runtime import VoiceIntent
from companion_gateway.audio.bridge import AudioBridge, Pcm16Mono
from companion_gateway.voice.runtime import ModelResponse
from companion_gateway.voice.service import VoiceTurnService
from companion_gateway.settings import Settings


def hello() -> DeviceHello:
    return DeviceHello.model_validate(
        {
            "type": "hello",
            "version": 1,
            "transport": "websocket",
            "audio_params": {
                "format": "opus",
                "sample_rate": 16000,
                "channels": 1,
                "frame_duration": 60,
            },
        }
    )


def test_session_idle_generation_replaces_and_cancels_previous_generation() -> None:
    session = DeviceSession.create(
        device_id="dev-test",
        client_id="client-test",
        hello=hello(),
    )

    first = session.arm_conversation_idle()
    second = session.arm_conversation_idle()

    assert second > first
    assert session.is_conversation_idle_current(second) is True
    assert session.is_conversation_idle_current(first) is False
    session.cancel_conversation_idle()
    assert session.is_conversation_idle_current(second) is False
    assert session.phase is DevicePhase.IDLE


def test_voice_intent_accepts_explicit_end_conversation() -> None:
    intent = VoiceIntent.model_validate({"type": "end_conversation"})

    assert intent.type == "end_conversation"


class EndIntentRuntime:
    def respond(self, pcm: Pcm16Mono) -> ModelResponse:
        return ModelResponse(
            text="",
            pcm=None,
            intent=VoiceIntent(type="end_conversation"),
        )

    def synthesize(self, text: str) -> Pcm16Mono:
        return Pcm16Mono(sample_rate=16_000, payload=b"\x00\x00" * 960)


class EndIntentCodec:
    def decode_uplink(self, payload: bytes) -> Pcm16Mono:
        return Pcm16Mono(sample_rate=16_000, payload=b"\x00\x00" * 960)

    def encode_downlink(self, pcm: Pcm16Mono) -> bytes:
        return b"tts"


def test_voice_turn_marks_end_intent_and_uses_fixed_closing_text() -> None:
    service = VoiceTurnService(
        audio_bridge=AudioBridge(
            codec=EndIntentCodec(),
            model_sample_rate=16_000,
            queue_capacity=1,
        ),
        model_runtime=EndIntentRuntime(),
    )
    service.accept_opus_uplink(b"input")

    turn = service.process_next_input()

    assert turn is not None
    assert turn.end_conversation is True
    assert turn.response_text == "好的，先休息一下。"


def test_session_idle_generation_is_armed_and_cancelled_explicitly() -> None:
    session = DeviceSession.create(
        device_id="dev-test",
        client_id="client-test",
        hello=hello(),
    )

    generation = session.arm_conversation_idle()

    assert session.conversation_idle_armed is True
    assert session.is_conversation_idle_current(generation) is True
    session.cancel_conversation_idle()
    assert session.conversation_idle_armed is False
    assert session.is_conversation_idle_current(generation) is False


def test_websocket_closes_after_tts_stop_and_idle_window(tmp_path) -> None:
    device_id = "idle-device"
    token = "idle-token"
    app = create_app(
        Settings(
            database_path=tmp_path / "idle-websocket.db",
            device_token_hashes={
                device_id: sha256(token.encode("utf-8")).hexdigest()
            },
            device_conversation_idle_timeout_seconds=0.01,
            device_continuous_conversation_enabled=True,
        )
    )

    with TestClient(app) as client:
        with client.websocket_connect(
            "/v1/devices/ws",
            headers={
                "Authorization": f"Bearer {token}",
                "Protocol-Version": "1",
                "Device-Id": device_id,
                "Client-Id": "idle-client",
            },
        ) as websocket:
            websocket.send_json(
                {
                    "type": "hello",
                    "version": 1,
                    "transport": "websocket",
                    "audio_params": {
                        "format": "opus",
                        "sample_rate": 16000,
                        "channels": 1,
                        "frame_duration": 60,
                    },
                }
            )
            server_hello = websocket.receive_json()
            app.state.device_transport.send_tts_stream(
                server_hello["session_id"],
                (b"tts-opus",),
            )
            assert websocket.receive_json()["state"] == "start"
            assert websocket.receive_bytes() == b"tts-opus"
            assert websocket.receive_json()["state"] == "stop"
            disconnected = websocket.receive()

    assert disconnected["type"] == "websocket.close"
    assert disconnected["code"] == 1000
    assert disconnected["reason"] == "conversation_idle"
