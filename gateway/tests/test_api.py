from collections.abc import Iterator
from hashlib import sha256
from pathlib import Path
import wave

import pytest
from fastapi.testclient import TestClient

from companion_gateway.api import create_app, create_default_app
from companion_gateway.audio.bridge import Pcm16Mono, resample_pcm16_mono
from companion_gateway.audio.pyav_opus import PyAvOpusCodec
from companion_gateway.domain.tasks import TaskEventType
from companion_gateway.settings import Settings


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


def test_ota_bootstrap_token_authenticates_device_link(tmp_path) -> None:
    token = "bootstrap-token"
    app = create_app(
        Settings(
            database_path=tmp_path / "ota-device-link.db",
            public_websocket_url="ws://192.0.2.10:8723/v1/devices/ws",
            ota_device_tokens={"device-test": token},
            device_token_hashes={"device-test": sha256(token.encode()).hexdigest()},
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
