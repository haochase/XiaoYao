from collections.abc import Iterator
from datetime import UTC, datetime, time
from hashlib import sha256
import os
from pathlib import Path
import wave

import pytest
from fastapi.testclient import TestClient

import companion_gateway.api as api_module
from companion_gateway.api import create_app, create_default_app
from companion_gateway.audio.bridge import Pcm16Mono, resample_pcm16_mono
from companion_gateway.audio.pyav_opus import PyAvOpusCodec
from companion_gateway.device.models import DeviceHello
from companion_gateway.device.session import DeviceSession
from companion_gateway.device.transport import DeviceOutboundBackpressure
from companion_gateway.domain.models import TaskCreate
from companion_gateway.domain.medication import FeishuSendResult, MedicationPlanCreate
from companion_gateway.domain.tasks import TaskEventType, TaskStatus
from companion_gateway.settings import Settings
from companion_gateway.voice.minicpm_o import (
    MinicpmOHttpRuntime,
    MinicpmORealtimeRuntime,
)


class RecordingReminderVoiceDelivery:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []
        self.task_service = None

    def synthesize_and_send(self, *, session_id: str, text: str) -> None:
        self.messages.append((session_id, text))

    def synthesize_notification_and_send(
        self,
        *,
        session_id: str,
        text: str,
    ) -> None:
        self.messages.append((session_id, text))

    def clear_pending_input(self, *, session_id: str | None = None) -> None:
        return None

    def set_task_executor(self, task_executor) -> None:
        return None

    def set_task_service(self, task_service) -> None:
        self.task_service = task_service

    def set_medication_service(self, medication_service) -> None:
        return None

    def set_memory_service(self, memory_service) -> None:
        return None


def task_payload() -> dict[str, object]:
    return {
        "actor_id": "family-1",
        "target_device_id": "living-room",
        "kind": "reminder",
        "schedule": {
            "at": "2026-08-05T20:00:00+08:00",
            "timezone": "Asia/Shanghai",
        },
        "payload": {"text": "take medicine"},
        "confirmation_policy": "required",
        "idempotency_key": "client:message-1",
    }


@pytest.fixture
def client(tmp_path) -> Iterator[TestClient]:
    app = create_app(Settings(database_path=tmp_path / "api.db"))
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def isolate_local_environment_file(monkeypatch, tmp_path) -> Iterator[None]:
    original = {
        key: value
        for key, value in os.environ.items()
        if key.startswith("COMPANION_")
    }
    monkeypatch.setattr(api_module, "LOCAL_ENV_PATH", tmp_path / "missing.env")
    yield
    for key in tuple(os.environ):
        if key.startswith("COMPANION_"):
            monkeypatch.delenv(key, raising=False)
    for key, value in original.items():
        monkeypatch.setenv(key, value)


def test_health_does_not_claim_dependency_readiness(client: TestClient) -> None:
    health = client.get("/health")
    ready = client.get("/ready")

    assert health.status_code == 200
    assert health.json() == {"status": "ok"}
    assert ready.status_code == 200
    assert ready.json() == {
        "status": "ready",
        "checks": {"database": "ok"},
    }


def test_enabled_memory_scheduler_starts_and_stops_with_app(tmp_path) -> None:
    app = create_app(
        Settings(
            database_path=tmp_path / "memory-scheduler.db",
            memory_enabled=True,
        )
    )

    with TestClient(app):
        assert app.state.memory_scheduler.is_running is True

    assert app.state.memory_scheduler.is_running is False


def test_feishu_chat_listener_starts_and_stops_with_app(tmp_path) -> None:
    class FakeListener:
        def __init__(self) -> None:
            self.started = False
            self.stopped = False

        async def start(self) -> None:
            self.started = True

        async def stop(self) -> None:
            self.stopped = True

    listener = FakeListener()
    app = create_app(
        Settings(database_path=tmp_path / "feishu-chat.db"),
        feishu_chat_listener=listener,
    )

    with TestClient(app):
        assert listener.started is True

    assert listener.stopped is True


def test_feishu_chat_status_reports_configuration_and_availability(tmp_path) -> None:
    class FakeListener:
        is_available = True
        received_messages = 3
        replied_messages = 2

        async def start(self) -> None:
            return None

        async def stop(self) -> None:
            return None

    configured_app = create_app(
        Settings(database_path=tmp_path / "feishu-status.db"),
        feishu_chat_listener=FakeListener(),
    )
    disabled_app = create_app(
        Settings(database_path=tmp_path / "feishu-disabled.db"),
    )

    with TestClient(configured_app) as client:
        assert client.get("/v1/channels/feishu/status").json() == {
            "configured": True,
            "available": True,
            "received_messages": 3,
            "replied_messages": 2,
        }
    with TestClient(disabled_app) as client:
        assert client.get("/v1/channels/feishu/status").json() == {
            "configured": False,
            "available": False,
            "received_messages": 0,
            "replied_messages": 0,
        }


def test_default_app_builds_feishu_chat_listener_when_enabled(
    monkeypatch,
    tmp_path,
) -> None:
    class FakeListener:
        async def start(self) -> None:
            return None

        async def stop(self) -> None:
            return None

    listener = FakeListener()
    captured: dict[str, object] = {}

    def fake_create_feishu_chat_listener(**kwargs):
        captured.update(kwargs)
        return listener

    monkeypatch.setattr(
        api_module,
        "create_feishu_chat_listener",
        fake_create_feishu_chat_listener,
    )
    monkeypatch.setenv("COMPANION_DB_PATH", str(tmp_path / "default-chat.db"))
    monkeypatch.setenv("COMPANION_FEISHU_APP_ID", "cli_test_app")
    monkeypatch.setenv("COMPANION_FEISHU_APP_SECRET", "secret_test_value")
    monkeypatch.setenv("COMPANION_FEISHU_RECEIVER_OPEN_ID", "ou_owner")
    monkeypatch.setenv("COMPANION_MINICPM_O_AUTH_TOKEN", "example-token")
    monkeypatch.setenv("COMPANION_FEISHU_CHAT_ENABLED", "true")

    app = create_default_app()

    assert app.state.feishu_chat_listener is listener
    assert captured["app_id"] == "cli_test_app"
    assert captured["owner_open_id"] == "ou_owner"
    assert captured["history_turns"] == 6


def test_device_status_api_reports_unknown_device_as_offline(
    client: TestClient,
) -> None:
    response = client.get(
        "/v1/devices/dev-living-room/status",
        headers={"X-Trace-Id": "trace-device-status"},
    )

    assert response.status_code == 200
    assert response.headers["X-Trace-Id"] == "trace-device-status"
    assert response.json() == {
        "device": {
            "device_id": "dev-living-room",
            "status": "offline",
            "session_id": None,
            "connected_at": None,
            "last_seen_at": None,
            "phase": None,
            "listening_mode": None,
            "audio_frames_received": 0,
        }
    }


def test_device_status_api_reports_active_phase_and_frame_count(tmp_path) -> None:
    device_id = "dev-living-room"
    token = "device-status-token"
    app = create_app(
        Settings(
            database_path=tmp_path / "device-status.db",
            device_token_hashes={
                device_id: sha256(token.encode("utf-8")).hexdigest()
            },
        )
    )

    with TestClient(app) as test_client:
        with test_client.websocket_connect(
            "/v1/devices/ws",
            headers={
                "Authorization": f"Bearer {token}",
                "Protocol-Version": "1",
                "Device-Id": device_id,
                "Client-Id": "status-client",
            },
        ) as websocket:
            websocket.send_json(
                {
                    "type": "hello",
                    "version": 1,
                    "transport": "websocket",
                    "audio_params": {
                        "format": "opus",
                        "sample_rate": 16_000,
                        "channels": 1,
                        "frame_duration": 60,
                    },
                }
            )
            session_hello = websocket.receive_json()
            websocket.send_json(
                {
                    "type": "listen",
                    "state": "start",
                    "mode": "manual",
                    "session_id": session_hello["session_id"],
                }
            )
            websocket.send_bytes(b"one-opus-frame")

            online = test_client.get(f"/v1/devices/{device_id}/status")

        offline = test_client.get(f"/v1/devices/{device_id}/status")

    body = online.json()["device"]
    assert body["status"] == "online"
    assert body["session_id"] == session_hello["session_id"]
    assert body["phase"] == "listening"
    assert body["listening_mode"] == "manual"
    assert body["audio_frames_received"] == 1
    assert offline.json()["device"]["status"] == "offline"


def test_task_delivery_logs_device_offline(tmp_path, monkeypatch) -> None:
    messages: list[str] = []

    def capture_info(message: str, *args: object) -> None:
        messages.append(message % args)

    monkeypatch.setattr(api_module.logger, "info", capture_info)
    app = create_app(Settings(database_path=tmp_path / "delivery-audit.db"))
    task, _ = app.state.task_executor.create_and_schedule(
        TaskCreate.model_validate(
            {
                **task_payload(),
                "target_device_id": "dev-offline",
                "confirmation_policy": "optional",
            }
        ),
        trace_id="trace-delivery-audit",
    )

    app.state.task_scheduler.tick(now=datetime(2026, 8, 6, 12, tzinfo=UTC))

    assert app.state.service.get_task(task.task_id).status is TaskStatus.PENDING_DELIVERY
    assert any(
        "task_delivery_failed" in message
        and "reason=device_offline" in message
        for message in messages
    )


def test_task_delivery_logs_outbound_backpressure(tmp_path, monkeypatch) -> None:
    messages: list[str] = []

    def capture_info(message: str, *args: object) -> None:
        messages.append(message % args)

    monkeypatch.setattr(api_module.logger, "info", capture_info)
    device_id = "dev-backpressure"
    voice_delivery = RecordingReminderVoiceDelivery()
    app = create_app(
        Settings(database_path=tmp_path / "backpressure.db"),
        voice_delivery_service=voice_delivery,
    )
    app.state.device_sessions.connect(
        DeviceSession.create(
            device_id=device_id,
            client_id="audit-client",
            hello=DeviceHello.model_validate(
                {
                    "type": "hello",
                    "version": 1,
                    "transport": "websocket",
                    "audio_params": {
                        "format": "opus",
                        "sample_rate": 16_000,
                        "channels": 1,
                        "frame_duration": 60,
                    },
                }
            ),
        )
    )

    def fail_synthesize_and_send(*_args, **_kwargs):
        raise DeviceOutboundBackpressure("queue full")

    monkeypatch.setattr(
        voice_delivery,
        "synthesize_notification_and_send",
        fail_synthesize_and_send,
    )
    task, _ = app.state.task_executor.create_and_schedule(
        TaskCreate.model_validate(
            {
                **task_payload(),
                "target_device_id": device_id,
                "confirmation_policy": "optional",
            }
        ),
        trace_id="trace-backpressure-audit",
    )

    app.state.task_scheduler.tick(now=datetime(2026, 8, 6, 12, tzinfo=UTC))

    assert app.state.service.get_task(task.task_id).status is TaskStatus.PENDING_DELIVERY
    assert any(
        "task_delivery_failed" in message
        and "reason=outbound_backpressure" in message
        for message in messages
    )


def test_reminder_delivery_uses_voice_tts_instead_of_task_json(
    tmp_path,
) -> None:
    device_id = "dev-reminder"
    voice_delivery = RecordingReminderVoiceDelivery()
    app = create_app(
        Settings(database_path=tmp_path / "reminder-tts.db"),
        voice_delivery_service=voice_delivery,
    )
    session = DeviceSession.create(
        device_id=device_id,
        client_id="reminder-client",
        hello=DeviceHello.model_validate(
            {
                "type": "hello",
                "version": 1,
                "transport": "websocket",
                "audio_params": {
                    "format": "opus",
                    "sample_rate": 16_000,
                    "channels": 1,
                    "frame_duration": 60,
                },
            }
        ),
    )
    app.state.device_sessions.connect(session)
    task, _ = app.state.task_executor.create_and_schedule(
        TaskCreate.model_validate(
            {
                **task_payload(),
                "target_device_id": device_id,
                "confirmation_policy": "optional",
                "idempotency_key": "reminder:tts:1",
            }
        ),
        trace_id="trace-reminder-tts",
    )
    app.state.task_scheduler.tick(now=datetime(2026, 8, 6, 12, tzinfo=UTC))

    assert voice_delivery.messages == [(session.session_id, "take medicine")]
    assert voice_delivery.task_service is app.state.service
    assert app.state.service.get_task(task.task_id).status is TaskStatus.DELIVERED


def test_medication_plan_delivery_uses_voice_tts(tmp_path) -> None:
    device_id = "medication-device"
    voice_delivery = RecordingReminderVoiceDelivery()
    app = create_app(
        Settings(database_path=tmp_path / "medication-tts.db"),
        voice_delivery_service=voice_delivery,
    )
    session = DeviceSession.create(
        device_id=device_id,
        client_id="medication-client",
        hello=DeviceHello.model_validate(
            {
                "type": "hello",
                "version": 1,
                "transport": "websocket",
                "audio_params": {
                    "format": "opus",
                    "sample_rate": 16_000,
                    "channels": 1,
                    "frame_duration": 60,
                },
            }
        ),
    )
    app.state.device_sessions.connect(session)
    plan, _ = app.state.service.create_medication_plan(
        MedicationPlanCreate(
            actor_id="voice-user",
            target_device_id=device_id,
            reminder_times=(time(20),),
            message="请确认已服药",
        ),
        trace_id="trace-medication-tts",
    )
    due_at = datetime(2026, 8, 6, 12, tzinfo=UTC)

    app.state.medication_service.tick(now=due_at)
    app.state.task_scheduler.tick(now=due_at)

    occurrence = app.state.repository.list_medication_occurrences()[0]
    task = app.state.service.get_task(occurrence.task_id)
    assert plan.target_device_id == device_id
    assert occurrence.task_id is not None
    assert task is not None and task.status is TaskStatus.DELIVERED
    assert voice_delivery.messages == [(session.session_id, "请确认已服药")]


def test_medication_plan_stays_pending_without_voice_runtime(tmp_path, monkeypatch) -> None:
    messages: list[str] = []

    def capture_info(message: str, *args: object) -> None:
        messages.append(message % args)

    monkeypatch.setattr(api_module.logger, "info", capture_info)
    device_id = "medication-no-voice"
    app = create_app(Settings(database_path=tmp_path / "medication-no-voice.db"))
    app.state.device_sessions.connect(
        DeviceSession.create(
            device_id=device_id,
            client_id="medication-client",
            hello=DeviceHello.model_validate(
                {
                    "type": "hello",
                    "version": 1,
                    "transport": "websocket",
                    "audio_params": {
                        "format": "opus",
                        "sample_rate": 16_000,
                        "channels": 1,
                        "frame_duration": 60,
                    },
                }
            ),
        )
    )
    app.state.service.create_medication_plan(
        MedicationPlanCreate(
            actor_id="voice-user",
            target_device_id=device_id,
            reminder_times=(time(20),),
            message="请确认已服药",
        ),
        trace_id="trace-medication-no-voice",
    )
    due_at = datetime(2026, 8, 6, 12, tzinfo=UTC)

    app.state.medication_service.tick(now=due_at)
    app.state.task_scheduler.tick(now=due_at)

    occurrence = app.state.repository.list_medication_occurrences()[0]
    task = app.state.service.get_task(occurrence.task_id)
    assert task is not None and task.status is TaskStatus.PENDING_DELIVERY
    assert any("reason=voice_synthesis_unavailable" in message for message in messages)


def test_ota_bootstrap_returns_an_enrolled_device_configuration(tmp_path) -> None:
    token = "bootstrap-token"
    app = create_app(
        Settings(
            database_path=tmp_path / "ota.db",
            public_websocket_url="ws://192.0.2.10:8723/v1/devices/ws",
            ota_device_tokens={"device-test": token},
            device_token_hashes={"device-test": sha256(token.encode()).hexdigest()},
        )
    )

    with TestClient(app) as test_client:
        response = test_client.post(
            "/v1/ota",
            headers={"Device-Id": "device-test"},
            json={"firmware": {"version": "local"}},
        )

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json() == {
        "websocket": {
            "url": "ws://192.0.2.10:8723/v1/devices/ws",
            "token": token,
            "version": 1,
        }
    }


def test_ota_bootstrap_rejects_missing_or_unknown_device(tmp_path) -> None:
    app = create_app(
        Settings(
            database_path=tmp_path / "ota-reject.db",
            public_websocket_url="ws://192.0.2.10:8723/v1/devices/ws",
            ota_device_tokens={"device-test": "bootstrap-token"},
        )
    )

    with TestClient(app) as test_client:
        missing = test_client.post("/v1/ota")
        unknown = test_client.post(
            "/v1/ota",
            headers={"Device-Id": "unknown-device"},
        )

    assert missing.status_code == 400
    assert unknown.status_code == 403
    assert missing.headers["Cache-Control"] == "no-store"
    assert unknown.headers["Cache-Control"] == "no-store"
    assert "token" not in unknown.text.lower()


def test_ota_bootstrap_is_disabled_without_device_tokens(tmp_path) -> None:
    app = create_app(
        Settings(
            database_path=tmp_path / "ota-disabled.db",
            public_websocket_url=None,
            ota_device_tokens={},
        )
    )

    with TestClient(app) as test_client:
        response = test_client.post(
            "/v1/ota",
            headers={"Device-Id": "device-test"},
        )

    assert response.status_code == 404
    assert response.headers["Cache-Control"] == "no-store"


def test_ota_bootstrap_token_authenticates_device_link(tmp_path) -> None:
    token = "bootstrap-token"
    app = create_app(
        Settings(
            database_path=tmp_path / "ota-device-link.db",
            public_websocket_url="ws://192.0.2.10:8723/v1/devices/ws",
            ota_device_tokens={"device-test": token},
        )
    )

    with TestClient(app) as test_client:
        bootstrap = test_client.post(
            "/v1/ota",
            headers={"Device-Id": "device-test"},
        )
        returned_token = bootstrap.json()["websocket"]["token"]
        with test_client.websocket_connect(
            "/v1/devices/ws",
            headers={
                "Authorization": f"Bearer {returned_token}",
                "Protocol-Version": "1",
                "Device-Id": "device-test",
                "Client-Id": "fixture-client",
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
            response = websocket.receive_json()

    assert response["type"] == "hello"


def test_default_app_enables_fixture_voice_only_when_configured(
    monkeypatch,
    tmp_path,
) -> None:
    fixture_path = (
        Path(__file__).resolve().parents[2]
        / "assets"
        / "audio"
        / "companion-greeting-zh-cn.wav"
    )
    monkeypatch.setenv("COMPANION_DB_PATH", str(tmp_path / "default-app.db"))
    monkeypatch.setenv("COMPANION_DEVICE_TOKEN_HASHES", "{}")
    monkeypatch.setenv("COMPANION_FAKE_VOICE_FIXTURE_PATH", str(fixture_path))

    app = create_default_app()

    assert app.state.voice_delivery_service is not None


def test_default_app_loads_gateway_dotenv_file(tmp_path, monkeypatch) -> None:
    environment_file = tmp_path / ".env"
    database_path = tmp_path / "dotenv-default.db"
    environment_file.write_text(
        "COMPANION_VOICE_RUNTIME=none\n"
        "COMPANION_MINICPM_O_AUTH_TOKEN=dotenv-token\n"
        f"COMPANION_DB_PATH={database_path}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(api_module, "LOCAL_ENV_PATH", environment_file)
    monkeypatch.delenv("COMPANION_VOICE_RUNTIME", raising=False)
    monkeypatch.delenv("COMPANION_MINICPM_O_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("COMPANION_DB_PATH", raising=False)

    app = create_default_app()

    assert app.state.repository._database_path == database_path


def test_default_app_selects_minicpm_o_http_runtime(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("COMPANION_DB_PATH", str(tmp_path / "http-runtime.db"))
    monkeypatch.setenv("COMPANION_VOICE_RUNTIME", "http")
    monkeypatch.setenv(
        "COMPANION_MINICPM_O_ENDPOINT",
        "http://127.0.0.1:9000/v1/infer",
    )

    app = create_default_app()

    delivery = app.state.voice_delivery_service
    assert delivery is not None
    assert isinstance(delivery._voice_turn_service._model_runtime, MinicpmOHttpRuntime)


def test_default_app_passes_minicpm_http_retry_configuration(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("COMPANION_DB_PATH", str(tmp_path / "http-retry-runtime.db"))
    monkeypatch.setenv("COMPANION_VOICE_RUNTIME", "http")
    monkeypatch.setenv(
        "COMPANION_MINICPM_O_ENDPOINT",
        "http://127.0.0.1:9000/v1/infer",
    )
    monkeypatch.setenv("COMPANION_MINICPM_O_MAX_RETRIES", "4")
    monkeypatch.setenv("COMPANION_MINICPM_O_RETRY_BACKOFF_SECONDS", "0.25")

    app = create_default_app()

    runtime = app.state.voice_delivery_service._voice_turn_service._model_runtime
    assert runtime._max_retries == 4
    assert runtime._retry_backoff_seconds == 0.25


def test_default_app_selects_minicpm_o_realtime_runtime(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("COMPANION_DB_PATH", str(tmp_path / "realtime-runtime.db"))
    monkeypatch.setenv("COMPANION_VOICE_RUNTIME", "realtime")
    monkeypatch.setenv(
        "COMPANION_MINICPM_O_ENDPOINT",
        "wss://127.0.0.1:9000/v1/realtime?mode=audio",
    )

    app = create_default_app()

    delivery = app.state.voice_delivery_service
    assert delivery is not None
    assert isinstance(
        delivery._voice_turn_service._model_runtime,
        MinicpmORealtimeRuntime,
    )


def test_default_app_passes_minicpm_o_auth_token_to_runtime(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("COMPANION_DB_PATH", str(tmp_path / "auth-runtime.db"))
    monkeypatch.setenv("COMPANION_VOICE_RUNTIME", "realtime")
    monkeypatch.setenv(
        "COMPANION_MINICPM_O_ENDPOINT",
        "wss://127.0.0.1:9000/v1/realtime?mode=audio",
    )
    monkeypatch.setenv("COMPANION_MINICPM_O_AUTH_TOKEN", "runtime-token")

    app = create_default_app()

    runtime = app.state.voice_delivery_service._voice_turn_service._model_runtime
    assert runtime._auth_token == "runtime-token"


def test_enabled_task_scheduler_notifies_connected_target_device(tmp_path) -> None:
    device_id = "scheduler-device"
    device_token = "scheduler-device-token"
    client_id = "scheduler-client"
    settings = Settings(
        database_path=tmp_path / "scheduler-api.db",
        device_token_hashes={
            device_id: sha256(device_token.encode("utf-8")).hexdigest()
        },
        task_scheduler_enabled=False,
    )
    voice_delivery = RecordingReminderVoiceDelivery()
    app = create_app(settings, voice_delivery_service=voice_delivery)
    command = task_payload()
    command["target_device_id"] = device_id
    command["confirmation_policy"] = "optional"
    task, _ = app.state.task_executor.create_and_schedule(
        TaskCreate.model_validate(command),
        trace_id="trace-scheduler-api",
    )

    with TestClient(app) as client:
        with client.websocket_connect(
            "/v1/devices/ws",
            headers={
                "Authorization": f"Bearer {device_token}",
                "Protocol-Version": "1",
                "Device-Id": device_id,
                "Client-Id": client_id,
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
            session_hello = websocket.receive_json()
            app.state.task_scheduler.tick(
                now=datetime(2026, 8, 6, 12, tzinfo=UTC)
            )

    assert voice_delivery.messages == [
        (session_hello["session_id"], "take medicine")
    ]
    assert app.state.service.get_task(task.task_id).status is TaskStatus.DELIVERED


def test_default_app_fixture_voice_returns_a_full_tts_stream(
    monkeypatch,
    tmp_path,
) -> None:
    fixture_path = (
        Path(__file__).resolve().parents[2]
        / "assets"
        / "audio"
        / "companion-greeting-zh-cn.wav"
    )
    device_id = "fixture-device"
    token = "fixture-device-token"
    monkeypatch.setenv("COMPANION_DB_PATH", str(tmp_path / "default-stream.db"))
    monkeypatch.setenv(
        "COMPANION_DEVICE_TOKEN_HASHES",
        f'{{"{device_id}":"{sha256(token.encode()).hexdigest()}"}}',
    )
    monkeypatch.setenv("COMPANION_VOICE_RUNTIME", "fixture")
    monkeypatch.setenv("COMPANION_FAKE_VOICE_FIXTURE_PATH", str(fixture_path))
    with wave.open(str(fixture_path), "rb") as source:
        input_pcm = Pcm16Mono(
            sample_rate=source.getframerate(),
            payload=source.readframes(960),
        )
    codec = PyAvOpusCodec()
    uplink_packet = codec.encode_downlink(
        resample_pcm16_mono(input_pcm, target_sample_rate=24_000)
    )

    with TestClient(create_default_app()) as test_client:
        with test_client.websocket_connect(
            "/v1/devices/ws",
            headers={
                "Authorization": f"Bearer {token}",
                "Protocol-Version": "1",
                "Device-Id": device_id,
                "Client-Id": "fixture-client",
            },
        ) as websocket:
            websocket.send_json(
                {
                    "type": "hello",
                    "version": 1,
                    "transport": "websocket",
                    "audio_params": {
                        "format": "opus",
                        "sample_rate": 16_000,
                        "channels": 1,
                        "frame_duration": 60,
                    },
                }
            )
            server_hello = websocket.receive_json()
            websocket.send_json(
                {
                    "type": "listen",
                    "state": "start",
                    "session_id": server_hello["session_id"],
                }
            )
            websocket.send_bytes(uplink_packet)
            websocket.send_json(
                {
                    "type": "listen",
                    "state": "stop",
                    "session_id": server_hello["session_id"],
                }
            )

            assert websocket.receive_json()["state"] == "start"
            frames = [websocket.receive_bytes() for _ in range(90)]
            assert websocket.receive_json()["state"] == "stop"

    assert all(0 < len(frame) <= 4_096 for frame in frames)


def test_ready_reports_database_failure_separately(client: TestClient) -> None:
    client.app.state.repository.check = lambda: False

    health = client.get("/health")
    ready = client.get("/ready")

    assert health.status_code == 200
    assert ready.status_code == 503
    assert ready.json() == {
        "status": "not_ready",
        "checks": {"database": "error"},
    }


def test_create_and_get_task(client: TestClient) -> None:
    created = client.post(
        "/v1/tasks",
        json=task_payload(),
        headers={"X-Trace-Id": "trace-create-1"},
    )

    assert created.status_code == 201
    assert created.headers["X-Trace-Id"] == "trace-create-1"
    body = created.json()
    assert body["created"] is True
    assert body["task"]["status"] == "created"
    assert body["task"]["trace_id"] == "trace-create-1"

    fetched = client.get(f"/v1/tasks/{body['task']['task_id']}")
    assert fetched.status_code == 200
    assert fetched.json()["task"] == body["task"]
    assert [event["type"] for event in fetched.json()["events"]] == ["created"]


class FakeMedicationNotifier:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def send_text(self, *, text: str, trace_id: str) -> FeishuSendResult:
        self.calls.append(text)
        return FeishuSendResult(success=True, message_id="om_api_test")


def test_reminder_task_uses_feishu_fallback_when_device_is_offline(tmp_path) -> None:
    notifier = FakeMedicationNotifier()
    app = create_app(
        Settings(
            database_path=tmp_path / "reminder-fallback.db",
            feishu_app_id="cli_test_app",
            feishu_app_secret="secret_test_value",
            feishu_receiver_open_id="ou_test_receiver",
        ),
        medication_notifier=notifier,
    )

    task, _ = app.state.task_executor.create_and_schedule(
        TaskCreate.model_validate(
            {
                **task_payload(),
                "confirmation_policy": "optional",
            }
        ),
        trace_id="trace-reminder-fallback",
    )
    app.state.task_scheduler.tick(now=datetime(2026, 8, 6, 12, tzinfo=UTC))

    assert notifier.calls == ["take medicine"]
    assert app.state.service.get_task(task.task_id).status is TaskStatus.DELIVERED


def test_medication_api_requires_feishu_configuration(client: TestClient) -> None:
    response = client.post(
        "/v1/medication/plans",
        json={
            "actor_id": "voice-user",
            "target_device_id": "living-room",
            "reminder_times": ["08:00"],
            "message": "请确认服药",
        },
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "Feishu fallback is not configured"}


def test_medication_api_plan_lifecycle_and_single_fallback(tmp_path) -> None:
    notifier = FakeMedicationNotifier()
    app = create_app(
        Settings(
            database_path=tmp_path / "medication-api.db",
            feishu_app_id="cli_test_app",
            feishu_app_secret="secret_test_value",
            feishu_receiver_open_id="ou_test_receiver",
        ),
        medication_notifier=notifier,
    )

    with TestClient(app) as test_client:
        created = test_client.post(
            "/v1/medication/plans",
            headers={"X-Trace-Id": "trace-medication-create"},
            json={
                "actor_id": "voice-user",
                "target_device_id": "living-room",
                "reminder_times": ["08:00"],
                "message": "请确认服药",
            },
        )
        assert created.status_code == 201
        plan = created.json()["plan"]
        assert plan["reminder_times"] == ["08:00:00"]

        listed = test_client.get("/v1/medication/plans")
        assert listed.status_code == 200
        assert [item["plan_id"] for item in listed.json()["plans"]] == [
            plan["plan_id"]
        ]

        app.state.medication_scheduler.tick(
            now=datetime(2026, 8, 11, 0, tzinfo=UTC)
        )
        app.state.medication_service.tick(
            now=datetime(2026, 8, 11, 0, 10, tzinfo=UTC),
            trace_id="trace-medication-fallback",
        )
        app.state.medication_service.tick(
            now=datetime(2026, 8, 11, 0, 11, tzinfo=UTC),
            trace_id="trace-medication-fallback-repeat",
        )

        occurrences = test_client.get("/v1/medication/occurrences")
        assert occurrences.status_code == 200
        assert occurrences.json()["occurrences"][0]["feishu_message_id"] == (
            "om_api_test"
        )

        disabled = test_client.post(
            f"/v1/medication/plans/{plan['plan_id']}/disable",
            json={
                "actor_id": "voice-user",
                "target_device_id": "living-room",
            },
        )
        assert disabled.status_code == 200
        assert disabled.json()["plan"]["enabled"] is False
        assert len(notifier.calls) == 1


def test_medication_api_acknowledges_delivered_occurrence(tmp_path) -> None:
    app = create_app(
        Settings(
            database_path=tmp_path / "medication-ack-api.db",
            feishu_app_id="cli_test_app",
            feishu_app_secret="secret_test_value",
            feishu_receiver_open_id="ou_test_receiver",
        ),
        medication_notifier=FakeMedicationNotifier(),
    )
    due = datetime(2026, 8, 11, 0, tzinfo=UTC)

    with TestClient(app) as test_client:
        created = test_client.post(
            "/v1/medication/plans",
            json={
                "actor_id": "voice-user",
                "target_device_id": "living-room",
                "reminder_times": ["08:00"],
                "message": "请确认服药",
            },
        ).json()
        app.state.medication_scheduler.tick(now=due)
        app.state.task_executor.execute_due(
            now=due,
            deliver=lambda _task: True,
            trace_id="trace-device-delivery",
        )
        app.state.medication_service.tick(now=due, trace_id="trace-adopt")
        occurrence = app.state.repository.list_medication_occurrences()[0]

        acknowledged = test_client.post(
            f"/v1/medication/occurrences/{occurrence.occurrence_id}/ack",
            json={
                "actor_id": "voice-user",
                "target_device_id": "living-room",
            },
            headers={"X-Trace-Id": "trace-api-ack"},
        )

    assert acknowledged.status_code == 200
    assert acknowledged.json()["occurrence"]["status"] == "acknowledged"
    assert app.state.repository.get_task(occurrence.task_id).status.value == (
        "acknowledged"
    )


def test_duplicate_idempotency_key_returns_original_task(client: TestClient) -> None:
    first = client.post("/v1/tasks", json=task_payload())
    second = client.post("/v1/tasks", json=task_payload())

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["created"] is False
    assert second.json()["task"]["task_id"] == first.json()["task"]["task_id"]


def test_missing_task_returns_404(client: TestClient) -> None:
    response = client.get("/v1/tasks/not-found")

    assert response.status_code == 404
    assert response.json() == {"detail": "task not found"}


def test_acknowledgement_before_delivery_returns_409(client: TestClient) -> None:
    created = client.post("/v1/tasks", json=task_payload()).json()

    response = client.post(
        f"/v1/tasks/{created['task']['task_id']}/ack",
        json={"reason": "voice_confirmation"},
    )

    assert response.status_code == 409
    assert "cannot apply acknowledged to created" in response.json()["detail"]


def test_confirming_a_required_voice_proposal_schedules_it(client: TestClient) -> None:
    proposal, created = client.app.state.task_executor.create_and_schedule(
        TaskCreate.model_validate(task_payload()),
        trace_id="trace-proposal",
    )

    response = client.post(
        f"/v1/tasks/{proposal.task_id}/confirm",
        json={"reason": "voice_confirmation"},
        headers={"X-Trace-Id": "trace-confirm"},
    )

    assert created is True
    assert proposal.status.value == "awaiting_confirmation"
    assert response.status_code == 200
    assert response.json()["task"]["status"] == "scheduled"
    assert response.json()["event"] == {
        "event_id": response.json()["event"]["event_id"],
        "task_id": proposal.task_id,
        "type": "confirmed",
        "reason": "voice_confirmation",
        "occurred_at": response.json()["event"]["occurred_at"],
        "trace_id": "trace-confirm",
    }


def test_rejecting_a_required_voice_proposal_is_terminal(client: TestClient) -> None:
    proposal, _ = client.app.state.task_executor.create_and_schedule(
        TaskCreate.model_validate(task_payload()),
        trace_id="trace-proposal",
    )

    response = client.post(
        f"/v1/tasks/{proposal.task_id}/reject",
        json={"reason": "voice_rejection"},
    )

    assert response.status_code == 200
    assert response.json()["task"]["status"] == "rejected"
    assert response.json()["event"]["type"] == "rejected"
    assert client.app.state.service.list_due_tasks(
        now=client.app.state.service.get_task(proposal.task_id).created_at,
    ) == []


def test_cancel_created_task_and_reject_second_cancel(client: TestClient) -> None:
    created = client.post("/v1/tasks", json=task_payload()).json()
    task_id = created["task"]["task_id"]

    cancelled = client.post(
        f"/v1/tasks/{task_id}/cancel",
        json={"reason": "creator_cancelled"},
    )
    duplicate = client.post(
        f"/v1/tasks/{task_id}/cancel",
        json={"reason": "creator_cancelled_again"},
    )

    assert cancelled.status_code == 200
    assert cancelled.json()["task"]["status"] == "cancelled"
    assert cancelled.json()["event"]["type"] == "cancelled"
    assert duplicate.status_code == 409


def test_acknowledge_delivered_task(client: TestClient) -> None:
    created = client.post("/v1/tasks", json=task_payload()).json()
    task_id = created["task"]["task_id"]
    repository = client.app.state.repository
    for event_type in (
        TaskEventType.SCHEDULED,
        TaskEventType.DUE,
        TaskEventType.DELIVERING,
        TaskEventType.DELIVERED,
    ):
        client.app.state.service.record_event(
            task_id,
            event_type,
            reason=f"test_{event_type.value}",
            trace_id="trace-delivery",
        )

    response = client.post(
        f"/v1/tasks/{task_id}/ack",
        json={"reason": "voice_confirmation"},
        headers={"X-Trace-Id": "trace-ack"},
    )

    assert repository.get_task(task_id).status.value == "acknowledged"
    assert response.status_code == 200
    assert response.json()["event"]["type"] == "acknowledged"
    assert response.json()["event"]["trace_id"] == "trace-ack"


def test_invalid_task_payload_returns_422(client: TestClient) -> None:
    payload = task_payload()
    payload["schedule"] = {
        "at": "2026-08-05T20:00:00",
        "timezone": "Asia/Shanghai",
    }

    response = client.post("/v1/tasks", json=payload)

    assert response.status_code == 422
