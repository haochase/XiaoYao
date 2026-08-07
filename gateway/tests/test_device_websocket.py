from collections.abc import Iterator
from hashlib import sha256

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from companion_gateway.api import create_app
from companion_gateway.audio.bridge import AudioBridge, AudioFrameRejected, Pcm16Mono
from companion_gateway.device.events import BoundedDeviceEventSink
from companion_gateway.device.transport import DeviceTransport
from companion_gateway.settings import Settings
from companion_gateway.voice.delivery import DeviceVoiceDeliveryService
from companion_gateway.voice.minicpm_o import ModelRuntimeError
from companion_gateway.voice.runtime import FakeModelRuntime
from companion_gateway.voice.service import VoiceTurnService


DEVICE_ID = "dev-test"
CLIENT_ID = "client-test"
DEVICE_TOKEN = "local-test-token"


def hello_payload() -> dict[str, object]:
    return {
        "type": "hello",
        "version": 1,
        "transport": "websocket",
        "features": {"mcp": True},
        "audio_params": {
            "format": "opus",
            "sample_rate": 16000,
            "channels": 1,
            "frame_duration": 60,
        },
    }


def websocket_headers(
    *,
    token: str = DEVICE_TOKEN,
    protocol_version: str = "1",
) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Protocol-Version": protocol_version,
        "Device-Id": DEVICE_ID,
        "Client-Id": CLIENT_ID,
    }


class VoiceLoopCodec:
    def decode_uplink(self, payload: bytes) -> Pcm16Mono:
        return Pcm16Mono(sample_rate=16_000, payload=b"\x01\x00" * 960)

    def encode_downlink(self, pcm: Pcm16Mono) -> bytes:
        assert pcm.sample_rate == 24_000
        return b"voice-loop-opus"


class RejectingVoiceLoopCodec(VoiceLoopCodec):
    def decode_uplink(self, payload: bytes) -> Pcm16Mono:
        raise AudioFrameRejected("malformed opus")


class UnavailableRuntime:
    def respond(self, pcm: Pcm16Mono):
        raise ModelRuntimeError("MiniCPM-o request failed")


@pytest.fixture
def app_and_sink(tmp_path):
    sink = BoundedDeviceEventSink(capacity=8)
    settings = Settings(
        database_path=tmp_path / "device-websocket.db",
        device_token_hashes={
            DEVICE_ID: sha256(DEVICE_TOKEN.encode("utf-8")).hexdigest()
        },
        device_hello_timeout_seconds=1.0,
        device_audio_frame_max_bytes=4096,
    )
    return create_app(settings, device_event_sink=sink), sink


@pytest.fixture
def client(app_and_sink) -> Iterator[TestClient]:
    app, _ = app_and_sink
    with TestClient(app) as test_client:
        yield test_client


def test_invalid_device_token_is_rejected(client: TestClient) -> None:
    with pytest.raises(WebSocketDisconnect) as disconnected:
        with client.websocket_connect(
            "/v1/devices/ws",
            headers=websocket_headers(token="wrong-token"),
        ):
            pass

    assert disconnected.value.code == 1008


def test_missing_device_token_is_rejected(client: TestClient) -> None:
    headers = websocket_headers()
    del headers["Authorization"]

    with pytest.raises(WebSocketDisconnect) as disconnected:
        with client.websocket_connect(
            "/v1/devices/ws",
            headers=headers,
        ):
            pass

    assert disconnected.value.code == 1008


def test_unsupported_protocol_version_is_rejected(client: TestClient) -> None:
    with pytest.raises(WebSocketDisconnect) as disconnected:
        with client.websocket_connect(
            "/v1/devices/ws",
            headers=websocket_headers(protocol_version="2"),
        ):
            pass

    assert disconnected.value.code == 1002


def test_valid_hello_receives_xiaozhi_server_hello(client: TestClient) -> None:
    with client.websocket_connect(
        "/v1/devices/ws",
        headers=websocket_headers(),
    ) as websocket:
        websocket.send_json(hello_payload())
        response = websocket.receive_json()

    assert response["type"] == "hello"
    assert response["transport"] == "websocket"
    assert response["session_id"].startswith("ses_")
    assert response["audio_params"] == {
        "format": "opus",
        "sample_rate": 24000,
        "channels": 1,
        "frame_duration": 60,
    }


def test_active_device_receives_tts_control_and_binary_audio(
    client: TestClient,
    app_and_sink,
) -> None:
    app, _ = app_and_sink
    opus_frame = b"outbound-opus"

    with client.websocket_connect(
        "/v1/devices/ws",
        headers=websocket_headers(),
    ) as websocket:
        websocket.send_json(hello_payload())
        server_hello = websocket.receive_json()
        app.state.device_transport.send_tts(
            server_hello["session_id"],
            opus_frame,
        )

        assert websocket.receive_json() == {
            "type": "tts",
            "state": "start",
            "session_id": server_hello["session_id"],
        }
        assert websocket.receive_bytes() == opus_frame
        assert websocket.receive_json() == {
            "type": "tts",
            "state": "stop",
            "session_id": server_hello["session_id"],
        }


def test_active_device_receives_a_multi_frame_tts_stream(
    client: TestClient,
    app_and_sink,
) -> None:
    app, _ = app_and_sink

    with client.websocket_connect(
        "/v1/devices/ws",
        headers=websocket_headers(),
    ) as websocket:
        websocket.send_json(hello_payload())
        server_hello = websocket.receive_json()
        app.state.device_transport.send_tts_stream(
            server_hello["session_id"],
            (b"first-opus", b"second-opus"),
        )

        assert websocket.receive_json()["state"] == "start"
        assert websocket.receive_bytes() == b"first-opus"
        assert websocket.receive_bytes() == b"second-opus"
        assert websocket.receive_json()["state"] == "stop"


def test_replacement_session_keeps_the_new_tts_transport(
    client: TestClient,
    app_and_sink,
) -> None:
    app, _ = app_and_sink

    with client.websocket_connect(
        "/v1/devices/ws",
        headers=websocket_headers(),
    ) as first_socket:
        first_socket.send_json(hello_payload())
        first_socket.receive_json()

        with client.websocket_connect(
            "/v1/devices/ws",
            headers=websocket_headers(),
        ) as second_socket:
            second_socket.send_json(hello_payload())
            second_hello = second_socket.receive_json()
            app.state.device_transport.send_tts(
                second_hello["session_id"],
                b"replacement-opus",
            )

            assert second_socket.receive_json()["state"] == "start"
            assert second_socket.receive_bytes() == b"replacement-opus"
            assert second_socket.receive_json()["state"] == "stop"


def test_uplink_audio_can_complete_an_injected_fake_voice_turn(tmp_path) -> None:
    transport = DeviceTransport()
    bridge = AudioBridge(
        codec=VoiceLoopCodec(),
        model_sample_rate=16_000,
        queue_capacity=1,
    )
    runtime = FakeModelRuntime(
        response_text="我在这里，慢慢说。",
        response_pcm=Pcm16Mono(sample_rate=16_000, payload=b"\x02\x00" * 960),
    )
    voice_delivery = DeviceVoiceDeliveryService(
        voice_turn_service=VoiceTurnService(
            audio_bridge=bridge,
            model_runtime=runtime,
        ),
        device_transport=transport,
    )
    app = create_app(
        Settings(
            database_path=tmp_path / "voice-loop.db",
            device_token_hashes={
                DEVICE_ID: sha256(DEVICE_TOKEN.encode("utf-8")).hexdigest()
            },
        ),
        device_transport=transport,
        voice_delivery_service=voice_delivery,
    )

    with TestClient(app) as client:
        with client.websocket_connect(
            "/v1/devices/ws",
            headers=websocket_headers(),
        ) as websocket:
            websocket.send_json(hello_payload())
            server_hello = websocket.receive_json()
            websocket.send_json(
                {
                    "type": "listen",
                    "state": "start",
                    "session_id": server_hello["session_id"],
                }
            )
            websocket.send_bytes(b"inbound-opus")
            websocket.send_json(
                {
                    "type": "listen",
                    "state": "stop",
                    "session_id": server_hello["session_id"],
                }
            )

            assert websocket.receive_json() == {
                "type": "tts",
                "state": "start",
                "session_id": server_hello["session_id"],
            }
            assert websocket.receive_bytes() == b"voice-loop-opus"
            assert websocket.receive_json() == {
                "type": "tts",
                "state": "stop",
                "session_id": server_hello["session_id"],
            }


def test_voice_turn_waits_for_listen_stop_before_model_response(tmp_path) -> None:
    transport = DeviceTransport()
    bridge = AudioBridge(
        codec=VoiceLoopCodec(),
        model_sample_rate=16_000,
        queue_capacity=8,
    )
    runtime = FakeModelRuntime(
        response_text="鎴戝湪杩欓噷锛屾參鎱㈣銆?",
        response_pcm=Pcm16Mono(sample_rate=16_000, payload=b"\x02\x00" * 960),
    )
    app = create_app(
        Settings(
            database_path=tmp_path / "voice-stop-boundary.db",
            device_token_hashes={
                DEVICE_ID: sha256(DEVICE_TOKEN.encode("utf-8")).hexdigest()
            },
        ),
        device_transport=transport,
        voice_delivery_service=DeviceVoiceDeliveryService(
            voice_turn_service=VoiceTurnService(
                audio_bridge=bridge,
                model_runtime=runtime,
            ),
            device_transport=transport,
        ),
    )

    with TestClient(app) as client:
        with client.websocket_connect(
            "/v1/devices/ws",
            headers=websocket_headers(),
        ) as websocket:
            websocket.send_json(hello_payload())
            server_hello = websocket.receive_json()
            websocket.send_json(
                {
                    "type": "listen",
                    "state": "start",
                    "session_id": server_hello["session_id"],
                }
            )
            websocket.send_bytes(b"first-opus")
            websocket.send_bytes(b"second-opus")

            assert runtime.received_inputs == []

            websocket.send_json(
                {
                    "type": "listen",
                    "state": "stop",
                    "session_id": server_hello["session_id"],
                }
            )
            assert websocket.receive_json()["state"] == "start"
            assert websocket.receive_bytes() == b"voice-loop-opus"
            assert websocket.receive_json()["state"] == "stop"

    assert len(runtime.received_inputs) == 1
    assert runtime.received_inputs[0].sample_count == 1_920


def test_model_unavailable_returns_a_retryable_device_error(tmp_path) -> None:
    transport = DeviceTransport()
    bridge = AudioBridge(
        codec=VoiceLoopCodec(),
        model_sample_rate=16_000,
        queue_capacity=8,
    )
    app = create_app(
        Settings(
            database_path=tmp_path / "voice-model-error.db",
            device_token_hashes={
                DEVICE_ID: sha256(DEVICE_TOKEN.encode("utf-8")).hexdigest()
            },
        ),
        device_transport=transport,
        voice_delivery_service=DeviceVoiceDeliveryService(
            voice_turn_service=VoiceTurnService(
                audio_bridge=bridge,
                model_runtime=UnavailableRuntime(),
            ),
            device_transport=transport,
        ),
    )

    with TestClient(app) as client:
        with client.websocket_connect(
            "/v1/devices/ws",
            headers=websocket_headers(),
        ) as websocket:
            websocket.send_json(hello_payload())
            server_hello = websocket.receive_json()
            websocket.send_json(
                {
                    "type": "listen",
                    "state": "start",
                    "session_id": server_hello["session_id"],
                }
            )
            websocket.send_bytes(b"inbound-opus")
            websocket.send_json(
                {
                    "type": "listen",
                    "state": "stop",
                    "session_id": server_hello["session_id"],
                }
            )
            error = websocket.receive_json()

    assert error["type"] == "error"
    assert error["code"] == "model_unavailable"
    assert error["retryable"] is True


def test_injected_voice_turn_rejects_malformed_opus_without_server_error(
    tmp_path,
) -> None:
    transport = DeviceTransport()
    bridge = AudioBridge(
        codec=RejectingVoiceLoopCodec(),
        model_sample_rate=16_000,
        queue_capacity=1,
    )
    runtime = FakeModelRuntime(
        response_text="我在这里，慢慢说。",
        response_pcm=Pcm16Mono(sample_rate=16_000, payload=b"\x02\x00" * 960),
    )
    app = create_app(
        Settings(
            database_path=tmp_path / "voice-loop-error.db",
            device_token_hashes={
                DEVICE_ID: sha256(DEVICE_TOKEN.encode("utf-8")).hexdigest()
            },
        ),
        device_transport=transport,
        voice_delivery_service=DeviceVoiceDeliveryService(
            voice_turn_service=VoiceTurnService(
                audio_bridge=bridge,
                model_runtime=runtime,
            ),
            device_transport=transport,
        ),
    )

    with TestClient(app) as client:
        with client.websocket_connect(
            "/v1/devices/ws",
            headers=websocket_headers(),
        ) as websocket:
            websocket.send_json(hello_payload())
            server_hello = websocket.receive_json()
            websocket.send_json(
                {
                    "type": "listen",
                    "state": "start",
                    "session_id": server_hello["session_id"],
                }
            )
            websocket.send_bytes(b"malformed-opus")
            error = websocket.receive_json()

    assert error["code"] == "audio_decode_failed"
    assert error["retryable"] is False


def test_listening_binary_frame_reaches_bounded_sink(
    client: TestClient,
    app_and_sink,
) -> None:
    _, sink = app_and_sink
    opus_frame = b"\xf8\xff\xfe"

    with client.websocket_connect(
        "/v1/devices/ws",
        headers=websocket_headers(),
    ) as websocket:
        websocket.send_json(hello_payload())
        server_hello = websocket.receive_json()
        websocket.send_json(
            {
                "type": "listen",
                "state": "start",
                "mode": "manual",
                "session_id": server_hello["session_id"],
            }
        )
        websocket.send_bytes(opus_frame)
        websocket.send_json(
            {
                "type": "listen",
                "state": "stop",
                "session_id": server_hello["session_id"],
            }
        )
        websocket.send_bytes(b"late-frame")
        error = websocket.receive_json()

    frames = sink.audio_snapshot()
    assert [frame.payload for frame in frames] == [opus_frame]
    assert error["type"] == "error"
    assert error["code"] == "audio_not_allowed"
    assert error["retryable"] is False
    assert error["trace_id"].startswith("trc_")


def test_oversized_audio_frame_is_rejected(
    client: TestClient,
    app_and_sink,
) -> None:
    _, sink = app_and_sink

    with client.websocket_connect(
        "/v1/devices/ws",
        headers=websocket_headers(),
    ) as websocket:
        websocket.send_json(hello_payload())
        server_hello = websocket.receive_json()
        websocket.send_json(
            {
                "type": "listen",
                "state": "start",
                "session_id": server_hello["session_id"],
            }
        )
        websocket.send_bytes(b"x" * 4097)
        error = websocket.receive_json()

    assert sink.audio_snapshot() == []
    assert error["code"] == "audio_frame_too_large"


def test_empty_audio_frame_is_rejected(client: TestClient) -> None:
    with client.websocket_connect(
        "/v1/devices/ws",
        headers=websocket_headers(),
    ) as websocket:
        websocket.send_json(hello_payload())
        server_hello = websocket.receive_json()
        websocket.send_json(
            {
                "type": "listen",
                "state": "start",
                "session_id": server_hello["session_id"],
            }
        )
        websocket.send_bytes(b"")
        error = websocket.receive_json()

    assert error["code"] == "audio_frame_empty"


def test_abort_stops_accepting_audio(client: TestClient) -> None:
    with client.websocket_connect(
        "/v1/devices/ws",
        headers=websocket_headers(),
    ) as websocket:
        websocket.send_json(hello_payload())
        server_hello = websocket.receive_json()
        websocket.send_json(
            {
                "type": "listen",
                "state": "start",
                "session_id": server_hello["session_id"],
            }
        )
        websocket.send_json(
            {
                "type": "abort",
                "session_id": server_hello["session_id"],
                "reason": "wake_word_detected",
            }
        )
        websocket.send_bytes(b"late-frame")
        error = websocket.receive_json()

    assert error["code"] == "audio_not_allowed"


def test_malformed_hello_is_rejected(client: TestClient) -> None:
    with pytest.raises(WebSocketDisconnect) as disconnected:
        with client.websocket_connect(
            "/v1/devices/ws",
            headers=websocket_headers(),
        ) as websocket:
            websocket.send_json({"type": "hello", "version": 1})
            websocket.receive_json()

    assert disconnected.value.code == 1003


def test_disconnect_removes_active_session(client: TestClient) -> None:
    with client.websocket_connect(
        "/v1/devices/ws",
        headers=websocket_headers(),
    ) as websocket:
        websocket.send_json(hello_payload())
        websocket.receive_json()
        assert client.app.state.device_sessions.get(DEVICE_ID) is not None

    assert client.app.state.device_sessions.get(DEVICE_ID) is None


def test_one_hundred_reconnects_leave_no_active_session(
    client: TestClient,
) -> None:
    for _ in range(100):
        with client.websocket_connect(
            "/v1/devices/ws",
            headers=websocket_headers(),
        ) as websocket:
            websocket.send_json(hello_payload())
            websocket.receive_json()

    assert client.app.state.device_sessions.get(DEVICE_ID) is None
