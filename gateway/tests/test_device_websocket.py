import asyncio
import time
from collections.abc import Iterator
from hashlib import sha256

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import companion_gateway.api as api_module
from companion_gateway.api import create_app
from companion_gateway.audio.bridge import AudioBridge, AudioFrameRejected, Pcm16Mono
from companion_gateway.device.events import BoundedDeviceEventSink
from companion_gateway.device.models import DeviceHello
from companion_gateway.device.session import DeviceSession
from companion_gateway.device.transport import DeviceTransport
from companion_gateway.domain.models import (
    ConfirmationPolicy,
    TaskCreate,
    TaskKind,
    TaskPayload,
    TaskSchedule,
)
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


class SequenceVoiceLoopCodec(VoiceLoopCodec):
    def __init__(self, frames: list[Pcm16Mono]) -> None:
        self._frames = iter(frames)

    def decode_uplink(self, payload: bytes) -> Pcm16Mono:
        return next(self._frames)


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


def test_default_event_sink_does_not_backpressure_long_voice_sessions(
    tmp_path,
) -> None:
    settings = Settings(
        database_path=tmp_path / "default-event-sink.db",
        device_token_hashes={
            DEVICE_ID: sha256(DEVICE_TOKEN.encode("utf-8")).hexdigest()
        },
    )
    app = create_app(settings)
    session = DeviceSession.create(
        device_id=DEVICE_ID,
        client_id=CLIENT_ID,
        hello=DeviceHello.model_validate(hello_payload()),
    )

    for _ in range(256):
        app.state.device_event_sink.on_audio(session, b"opus-frame")


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
    assert response["version"] == 1
    assert response["transport"] == "websocket"
    assert response["session_id"].startswith("ses_")
    assert response["audio_params"] == {
        "format": "opus",
        "sample_rate": 24000,
        "channels": 1,
        "frame_duration": 60,
    }


def test_wake_word_detect_keeps_connection_open_for_listening_audio(
    client: TestClient,
    app_and_sink,
) -> None:
    _, sink = app_and_sink
    opus_frame = b"wake-following-opus"

    with client.websocket_connect(
        "/v1/devices/ws",
        headers=websocket_headers(),
    ) as websocket:
        websocket.send_json(hello_payload())
        server_hello = websocket.receive_json()
        websocket.send_bytes(b"prewake-opus")
        websocket.send_json(
            {
                "type": "listen",
                "state": "detect",
                "session_id": server_hello["session_id"],
                "text": "你好小智",
            }
        )
        websocket.send_json(
            {
                "type": "listen",
                "state": "start",
                "mode": "auto",
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

    controls = sink.control_snapshot()
    assert [control.control.state for control in controls] == ["detect", "start", "stop"]
    assert [frame.payload for frame in sink.audio_snapshot()] == [opus_frame]


def test_disconnect_diagnostics_keep_listen_mode_and_protocol_state(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log_messages: list[str] = []

    def capture_info(message: str, *args: object) -> None:
        log_messages.append(message % args)

    monkeypatch.setattr(api_module.logger, "info", capture_info)

    with client.websocket_connect(
        "/v1/devices/ws",
        headers=websocket_headers(),
    ) as websocket:
        websocket.send_json(hello_payload())
        websocket.receive_json()
        websocket.send_json(
            {"type": "listen", "state": "start", "mode": "manual"}
        )
        websocket.close(code=1001, reason="network-handoff")

    assert any(
        "device_ws_control" in message and "mode=manual" in message
        for message in log_messages
    )
    assert any(
        "device_ws_peer_closed" in message
        and "code=1001" in message
        and "reason_length=15" in message
        and "phase=listening" in message
        and "mode=manual" in message
        for message in log_messages
    )
    assert any(
        "device_ws_closed" in message
        and "phase_before_close=listening" in message
        and "duration_ms=" in message
        and "close_code=1001" in message
        for message in log_messages
    )


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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _ = app_and_sink
    delays: list[float] = []

    async def capture_delay(seconds: float) -> None:
        delays.append(seconds)

    monkeypatch.setattr(api_module, "_sleep_between_tts_frames", capture_delay)

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

    assert delays == [0.06]


def test_abort_interrupts_an_active_tts_stream(
    client: TestClient,
    app_and_sink,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _ = app_and_sink

    async def wait_between_frames(_: float) -> None:
        await asyncio.sleep(0.02)

    monkeypatch.setattr(api_module, "_sleep_between_tts_frames", wait_between_frames)

    with client.websocket_connect(
        "/v1/devices/ws",
        headers=websocket_headers(),
    ) as websocket:
        websocket.send_json(hello_payload())
        server_hello = websocket.receive_json()
        session_id = server_hello["session_id"]
        app.state.device_transport.send_tts_stream(
            session_id,
            (b"first-opus", b"second-opus", b"third-opus"),
        )

        assert websocket.receive_json()["state"] == "start"
        assert websocket.receive_bytes() == b"first-opus"
        websocket.send_json(
            {
                "type": "abort",
                "session_id": session_id,
                "reason": "wake_word_detected",
            }
        )
        time.sleep(0.08)
        app.state.device_transport.send_tts(session_id, b"next-opus")

        assert websocket.receive_json() == {
            "type": "tts",
            "state": "start",
            "session_id": session_id,
        }
        assert websocket.receive_bytes() == b"next-opus"
        assert websocket.receive_json() == {
            "type": "tts",
            "state": "stop",
            "session_id": session_id,
        }


def test_active_device_receives_a_task_notification(
    client: TestClient,
    app_and_sink,
) -> None:
    app, _ = app_and_sink
    task, _ = app.state.task_executor.create_and_schedule(
        TaskCreate(
            actor_id="voice-user",
            target_device_id=DEVICE_ID,
            kind=TaskKind.REMINDER,
            schedule=TaskSchedule(
                at="2026-08-07T20:00:00+08:00",
                timezone="Asia/Shanghai",
            ),
            payload=TaskPayload(text="take medicine"),
            confirmation_policy=ConfirmationPolicy.REQUIRED,
            idempotency_key="notify:websocket:1",
        ),
        trace_id="trace-task-notify",
    )

    with client.websocket_connect(
        "/v1/devices/ws",
        headers=websocket_headers(),
    ) as websocket:
        websocket.send_json(hello_payload())
        server_hello = websocket.receive_json()
        app.state.device_transport.send_task(server_hello["session_id"], task)

        assert websocket.receive_json() == {
            "type": "task",
            "state": "notify",
            "session_id": server_hello["session_id"],
            "task": task.model_dump(mode="json"),
        }


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


def test_auto_voice_turn_processes_after_silence_without_listen_stop(tmp_path) -> None:
    transport = DeviceTransport()
    bridge = AudioBridge(
        codec=VoiceLoopCodec(),
        model_sample_rate=16_000,
        queue_capacity=8,
    )
    runtime = FakeModelRuntime(
        response_text="已收到",
        response_pcm=Pcm16Mono(sample_rate=16_000, payload=b"\x02\x00" * 960),
    )
    app = create_app(
        Settings(
            database_path=tmp_path / "voice-auto-stop.db",
            device_token_hashes={
                DEVICE_ID: sha256(DEVICE_TOKEN.encode("utf-8")).hexdigest()
            },
            device_auto_stop_idle_seconds=0.1,
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
                    "mode": "auto",
                    "session_id": server_hello["session_id"],
                }
            )
            websocket.send_bytes(b"first-opus")
            websocket.send_bytes(b"second-opus")

            assert websocket.receive_json()["state"] == "start"
            assert websocket.receive_bytes() == b"voice-loop-opus"
            assert websocket.receive_json()["state"] == "stop"

    assert len(runtime.received_inputs) == 1
    assert runtime.received_inputs[0].sample_count == 1_920


def test_auto_voice_turn_processes_after_pcm_silence_without_listen_stop(tmp_path) -> None:
    transport = DeviceTransport()
    audible = Pcm16Mono(sample_rate=16_000, payload=b"\x64\x00" * 960)
    silent = Pcm16Mono(sample_rate=16_000, payload=b"\x00\x00" * 960)
    runtime = FakeModelRuntime(
        response_text="已收到",
        response_pcm=Pcm16Mono(sample_rate=16_000, payload=b"\x02\x00" * 960),
    )
    app = create_app(
        Settings(
            database_path=tmp_path / "voice-pcm-silence.db",
            device_token_hashes={
                DEVICE_ID: sha256(DEVICE_TOKEN.encode("utf-8")).hexdigest()
            },
            device_auto_turn_rms_threshold=35.0,
            device_auto_turn_silence_frames=2,
            device_auto_turn_min_speech_frames=1,
        ),
        device_transport=transport,
        voice_delivery_service=DeviceVoiceDeliveryService(
            voice_turn_service=VoiceTurnService(
                audio_bridge=AudioBridge(
                    codec=SequenceVoiceLoopCodec([audible, silent, silent]),
                    model_sample_rate=16_000,
                    queue_capacity=8,
                ),
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
                    "mode": "auto",
                    "session_id": server_hello["session_id"],
                }
            )
            websocket.send_bytes(b"audible-opus")
            websocket.send_bytes(b"silent-opus-1")
            websocket.send_bytes(b"silent-opus-2")

            assert websocket.receive_json()["state"] == "start"
            assert websocket.receive_bytes() == b"voice-loop-opus"
            assert websocket.receive_json()["state"] == "stop"
            websocket.send_bytes(b"tail-opus")

    assert len(runtime.received_inputs) == 1
    assert runtime.received_inputs[0].sample_count == 2_880


def test_auto_voice_turn_processes_confirmed_speech_at_frame_limit(tmp_path) -> None:
    transport = DeviceTransport()
    audible = Pcm16Mono(sample_rate=16_000, payload=b"\x64\x00" * 960)
    runtime = FakeModelRuntime(
        response_text="已收到",
        response_pcm=Pcm16Mono(sample_rate=16_000, payload=b"\x02\x00" * 960),
    )
    app = create_app(
        Settings(
            database_path=tmp_path / "voice-frame-limit.db",
            device_token_hashes={
                DEVICE_ID: sha256(DEVICE_TOKEN.encode("utf-8")).hexdigest()
            },
            device_auto_turn_rms_threshold=35.0,
            device_auto_turn_silence_frames=12,
            device_auto_turn_min_speech_frames=2,
            device_auto_turn_max_frames=3,
        ),
        device_transport=transport,
        voice_delivery_service=DeviceVoiceDeliveryService(
            voice_turn_service=VoiceTurnService(
                audio_bridge=AudioBridge(
                    codec=SequenceVoiceLoopCodec([audible, audible, audible]),
                    model_sample_rate=16_000,
                    queue_capacity=8,
                ),
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
                    "mode": "auto",
                    "session_id": server_hello["session_id"],
                }
            )
            websocket.send_bytes(b"audible-1")
            websocket.send_bytes(b"audible-2")
            websocket.send_bytes(b"audible-3")

            assert websocket.receive_json()["state"] == "start"
            assert websocket.receive_bytes() == b"voice-loop-opus"
            assert websocket.receive_json()["state"] == "stop"

    assert len(runtime.received_inputs) == 1
    assert runtime.received_inputs[0].sample_count == 2_880


@pytest.mark.parametrize("finish_with_stop", [False, True])
def test_auto_voice_turn_rejects_unconfirmed_audio_without_chat(
    tmp_path,
    finish_with_stop: bool,
) -> None:
    transport = DeviceTransport()
    audible = Pcm16Mono(sample_rate=16_000, payload=b"\x64\x00" * 960)
    silent = Pcm16Mono(sample_rate=16_000, payload=b"\x00\x00" * 960)
    runtime = FakeModelRuntime(
        response_text="不应调用聊天模型",
        response_pcm=Pcm16Mono(sample_rate=16_000, payload=b"\x02\x00" * 960),
    )
    app = create_app(
        Settings(
            database_path=tmp_path / f"voice-reject-{finish_with_stop}.db",
            device_token_hashes={
                DEVICE_ID: sha256(DEVICE_TOKEN.encode("utf-8")).hexdigest()
            },
            device_auto_turn_rms_threshold=35.0,
            device_auto_turn_silence_frames=12,
            device_auto_turn_min_speech_frames=2,
            device_auto_turn_max_frames=3,
        ),
        device_transport=transport,
        voice_delivery_service=DeviceVoiceDeliveryService(
            voice_turn_service=VoiceTurnService(
                audio_bridge=AudioBridge(
                    codec=SequenceVoiceLoopCodec([audible, silent, silent]),
                    model_sample_rate=16_000,
                    queue_capacity=8,
                ),
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
                    "mode": "auto",
                    "session_id": server_hello["session_id"],
                }
            )
            websocket.send_bytes(b"noise")
            if finish_with_stop:
                websocket.send_json(
                    {
                        "type": "listen",
                        "state": "stop",
                        "mode": "auto",
                        "session_id": server_hello["session_id"],
                    }
                )
            else:
                websocket.send_bytes(b"silent-1")
                websocket.send_bytes(b"silent-2")

            assert websocket.receive_json()["state"] == "start"
            assert websocket.receive_bytes() == b"voice-loop-opus"
            assert websocket.receive_json()["state"] == "stop"

    assert runtime.received_inputs == []


def test_auto_voice_turn_resets_pcm_endpoint_for_each_listen_start(tmp_path) -> None:
    transport = DeviceTransport()
    audible = Pcm16Mono(sample_rate=16_000, payload=b"\x64\x00" * 960)
    silent = Pcm16Mono(sample_rate=16_000, payload=b"\x00\x00" * 960)
    runtime = FakeModelRuntime(
        response_text="received",
        response_pcm=Pcm16Mono(sample_rate=16_000, payload=b"\x02\x00" * 960),
    )
    app = create_app(
        Settings(
            database_path=tmp_path / "voice-pcm-endpoint-reset.db",
            device_token_hashes={
                DEVICE_ID: sha256(DEVICE_TOKEN.encode("utf-8")).hexdigest()
            },
            device_auto_turn_rms_threshold=35.0,
            device_auto_turn_silence_frames=2,
            device_auto_turn_min_speech_frames=1,
            device_auto_stop_idle_seconds=0.01,
        ),
        device_transport=transport,
        voice_delivery_service=DeviceVoiceDeliveryService(
            voice_turn_service=VoiceTurnService(
                audio_bridge=AudioBridge(
                    codec=SequenceVoiceLoopCodec(
                        [audible, silent, silent, silent, silent]
                    ),
                    model_sample_rate=16_000,
                    queue_capacity=8,
                ),
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
            first_start = {
                "type": "listen",
                "state": "start",
                "mode": "auto",
                "session_id": server_hello["session_id"],
            }
            websocket.send_json(first_start)
            websocket.send_bytes(b"audible-opus")
            websocket.send_bytes(b"silent-opus-1")
            websocket.send_bytes(b"silent-opus-2")

            assert websocket.receive_json()["state"] == "start"
            assert websocket.receive_bytes() == b"voice-loop-opus"
            assert websocket.receive_json()["state"] == "stop"

            websocket.send_json(first_start)
            websocket.send_bytes(b"next-silent-opus-1")
            websocket.send_bytes(b"next-silent-opus-2")
            time.sleep(0.03)
            websocket.send_json(
                {
                    "type": "abort",
                    "session_id": server_hello["session_id"],
                }
            )

    assert len(runtime.received_inputs) == 1


def test_auto_voice_turn_ignores_post_tts_echo_until_stable_silence(tmp_path) -> None:
    transport = DeviceTransport()
    first_speech = Pcm16Mono(sample_rate=16_000, payload=b"\x64\x00" * 960)
    echo = Pcm16Mono(sample_rate=16_000, payload=b"\x50\x00" * 960)
    silent = Pcm16Mono(sample_rate=16_000, payload=b"\x00\x00" * 960)
    next_speech = Pcm16Mono(sample_rate=16_000, payload=b"\xc8\x00" * 960)
    runtime = FakeModelRuntime(
        response_text="received",
        response_pcm=Pcm16Mono(sample_rate=16_000, payload=b"\x02\x00" * 960),
    )
    app = create_app(
        Settings(
            database_path=tmp_path / "voice-post-tts-echo.db",
            device_token_hashes={
                DEVICE_ID: sha256(DEVICE_TOKEN.encode("utf-8")).hexdigest()
            },
            device_auto_turn_rms_threshold=35.0,
            device_auto_turn_silence_frames=2,
            device_auto_turn_min_speech_frames=1,
            device_post_tts_silence_frames=2,
        ),
        device_transport=transport,
        voice_delivery_service=DeviceVoiceDeliveryService(
            voice_turn_service=VoiceTurnService(
                audio_bridge=AudioBridge(
                    codec=SequenceVoiceLoopCodec(
                        [
                            first_speech,
                            silent,
                            silent,
                            echo,
                            echo,
                            silent,
                            silent,
                            next_speech,
                            silent,
                            silent,
                        ]
                    ),
                    model_sample_rate=16_000,
                    queue_capacity=16,
                ),
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
            listen_start = {
                "type": "listen",
                "state": "start",
                "mode": "auto",
                "session_id": server_hello["session_id"],
            }
            websocket.send_json(listen_start)
            websocket.send_bytes(b"first-speech")
            websocket.send_bytes(b"first-silent-1")
            websocket.send_bytes(b"first-silent-2")

            assert websocket.receive_json()["state"] == "start"
            assert websocket.receive_bytes() == b"voice-loop-opus"
            assert websocket.receive_json()["state"] == "stop"

            websocket.send_json(listen_start)
            websocket.send_bytes(b"echo-1")
            websocket.send_bytes(b"echo-2")
            websocket.send_bytes(b"guard-silent-1")
            websocket.send_bytes(b"guard-silent-2")
            assert len(runtime.received_inputs) == 1

            websocket.send_bytes(b"next-speech")
            websocket.send_bytes(b"next-silent-1")
            websocket.send_bytes(b"next-silent-2")

            assert websocket.receive_json()["state"] == "start"
            assert websocket.receive_bytes() == b"voice-loop-opus"
            assert websocket.receive_json()["state"] == "stop"

    assert len(runtime.received_inputs) == 2
    assert runtime.received_inputs[1].payload.startswith(next_speech.payload)


def test_voice_audio_diagnostics_log_only_aggregate_pcm_metrics(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log_messages: list[str] = []

    def capture_info(message: str, *args: object) -> None:
        log_messages.append(message % args)

    monkeypatch.setattr(api_module.logger, "info", capture_info)
    transport = DeviceTransport()
    app = create_app(
        Settings(
            database_path=tmp_path / "voice-metrics.db",
            device_token_hashes={
                DEVICE_ID: sha256(DEVICE_TOKEN.encode("utf-8")).hexdigest()
            },
        ),
        device_transport=transport,
        voice_delivery_service=DeviceVoiceDeliveryService(
            voice_turn_service=VoiceTurnService(
                audio_bridge=AudioBridge(
                    codec=VoiceLoopCodec(),
                    model_sample_rate=16_000,
                    queue_capacity=8,
                ),
                model_runtime=FakeModelRuntime(
                    response_text="已收到",
                    response_pcm=Pcm16Mono(
                        sample_rate=16_000,
                        payload=b"\x02\x00" * 960,
                    ),
                ),
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
                    "mode": "manual",
                    "session_id": server_hello["session_id"],
                }
            )
            websocket.send_bytes(b"metric-opus")

    diagnostic = next(
        message for message in log_messages if "device_ws_audio_metrics" in message
    )
    assert "frames=1" in diagnostic
    assert "rms_min=" in diagnostic
    assert "rms_max=" in diagnostic
    assert "rms_last=" in diagnostic
    assert "metric-opus" not in diagnostic


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
    caplog,
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

    api_module.logger.addHandler(caplog.handler)
    try:
        with TestClient(app) as client:
            caplog.set_level("WARNING", logger=api_module.logger.name)
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
    finally:
        api_module.logger.removeHandler(caplog.handler)

    assert error["code"] == "audio_decode_failed"
    assert error["retryable"] is False
    assert "device_ws_audio_rejected" in caplog.text
    assert "malformed opus" in caplog.text
    assert "malformed-opus" not in caplog.text


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
