from __future__ import annotations

import base64
import json
import struct

import pytest

from companion_gateway.audio.bridge import Pcm16Mono
from companion_gateway.voice import minicpm_o
from companion_gateway.voice.minicpm_o import (
    MinicpmOHttpRuntime,
    ModelRuntimeError,
)


class FakeResponse:
    def __init__(self, payload: bytes, status: int = 200) -> None:
        self._payload = payload
        self.status = status

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        return None

    def read(self) -> bytes:
        return self._payload


def input_pcm() -> Pcm16Mono:
    return Pcm16Mono(sample_rate=16_000, payload=b"\x01\x00" * 960)


def response_payload(*, sample_rate: int = 24_000, include_task: bool = False) -> bytes:
    payload = {
            "text": "I am here.",
            "sample_rate": sample_rate,
            "channels": 1,
            "format": "pcm_s16le",
            "audio_base64": base64.b64encode(b"\x02\x00" * 1_440).decode(),
    }
    if include_task:
        payload["task"] = {
            "actor_id": "voice-user",
            "target_device_id": "living-room",
            "kind": "reminder",
            "schedule": {
                "at": "2026-08-07T20:00:00+08:00",
                "timezone": "Asia/Shanghai",
            },
            "payload": {"text": "take medicine"},
            "confirmation_policy": "required",
            "idempotency_key": "voice:turn:1",
        }
    return json.dumps(payload).encode()


def test_minicpm_o_runtime_posts_pcm_and_decodes_response(monkeypatch) -> None:
    requests: list[dict[str, object]] = []

    def fake_urlopen(request, *, timeout):
        requests.append(
            {
                "url": request.full_url,
                "timeout": timeout,
                "body": json.loads(request.data),
                "authorization": request.get_header("Authorization"),
            }
        )
        return FakeResponse(response_payload())

    monkeypatch.setattr(minicpm_o, "urlopen", fake_urlopen)
    runtime = MinicpmOHttpRuntime(
        endpoint="http://ascend.example.test/v1/infer",
        timeout_seconds=7.5,
    )

    response = runtime.respond(input_pcm())

    assert requests == [
        {
            "url": "http://ascend.example.test/v1/infer",
            "timeout": 7.5,
            "body": {
                "sample_rate": 16_000,
                "channels": 1,
                "format": "pcm_s16le",
                "audio_base64": base64.b64encode(input_pcm().payload).decode(),
            },
            "authorization": None,
        }
    ]
    assert response.text == "I am here."
    assert response.pcm == Pcm16Mono(sample_rate=24_000, payload=b"\x02\x00" * 1_440)


def test_minicpm_o_runtime_rejects_wrong_response_sample_rate(monkeypatch) -> None:
    monkeypatch.setattr(
        minicpm_o,
        "urlopen",
        lambda request, *, timeout: FakeResponse(response_payload(sample_rate=16_000)),
    )
    runtime = MinicpmOHttpRuntime(endpoint="http://ascend.example.test/v1/infer")

    with pytest.raises(ModelRuntimeError, match="sample_rate"):
        runtime.respond(input_pcm())


def test_minicpm_o_runtime_decodes_a_validated_task(monkeypatch) -> None:
    monkeypatch.setattr(
        minicpm_o,
        "urlopen",
        lambda request, *, timeout: FakeResponse(response_payload(include_task=True)),
    )
    runtime = MinicpmOHttpRuntime(endpoint="http://ascend.example.test/v1/infer")

    response = runtime.respond(input_pcm())

    assert response.task is not None
    assert response.task.target_device_id == "living-room"


def test_minicpm_o_runtime_maps_transport_failure(monkeypatch) -> None:
    def fail_urlopen(request, *, timeout):
        raise OSError("connection refused")

    monkeypatch.setattr(minicpm_o, "urlopen", fail_urlopen)
    runtime = MinicpmOHttpRuntime(endpoint="http://ascend.example.test/v1/infer")

    with pytest.raises(ModelRuntimeError, match="request failed"):
        runtime.respond(input_pcm())


def test_minicpm_o_http_runtime_sends_bearer_token(monkeypatch) -> None:
    authorization: list[str | None] = []

    def fake_urlopen(request, *, timeout):
        authorization.append(request.get_header("Authorization"))
        return FakeResponse(response_payload())

    monkeypatch.setattr(minicpm_o, "urlopen", fake_urlopen)
    runtime = MinicpmOHttpRuntime(
        endpoint="http://ascend.example.test/v1/infer",
        auth_token="runtime-token",
    )

    runtime.respond(input_pcm())

    assert authorization == ["Bearer runtime-token"]


class FakeRealtimeSocket:
    def __init__(self, events: list[dict[str, object]]) -> None:
        self._events = iter(events)
        self.sent: list[dict[str, object]] = []
        self.closed = False
        self.headers: list[str] | None = None

    def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))

    def recv(self) -> str:
        return json.dumps(next(self._events))

    def close(self) -> None:
        self.closed = True


def test_minicpm_o_realtime_runtime_completes_one_audio_turn(monkeypatch) -> None:
    audio = base64.b64encode(struct.pack("<3f", 0.25, -0.25, 0.0)).decode()
    socket = FakeRealtimeSocket(
        [
            {"type": "session.queue_done"},
            {"type": "session.created", "session_id": "sess-1"},
            {"type": "response.output.delta", "kind": "text", "text": "Hello"},
            {"type": "response.output.delta", "kind": "audio", "audio": audio},
            {
                "type": "response.output.delta",
                "kind": "task",
                "task": {
                    "actor_id": "voice-user",
                    "target_device_id": "living-room",
                    "kind": "reminder",
                    "schedule": {
                        "at": "2026-08-07T20:00:00+08:00",
                        "timezone": "Asia/Shanghai",
                    },
                    "payload": {"text": "take medicine"},
                    "confirmation_policy": "required",
                    "idempotency_key": "voice:turn:realtime",
                },
            },
            {"type": "response.output.delta", "kind": "listen"},
        ]
    )
    def create_connection(endpoint, *, timeout, header):
        socket.headers = header
        return socket

    monkeypatch.setattr(minicpm_o.websocket, "create_connection", create_connection)
    runtime = minicpm_o.MinicpmORealtimeRuntime(
        endpoint="wss://ascend.example.test/v1/realtime?mode=audio",
        auth_token="runtime-token",
    )

    response = runtime.respond(input_pcm())

    assert response.text == "Hello"
    assert response.pcm.sample_rate == 24_000
    assert response.pcm.sample_count == 3
    assert response.task is not None
    assert response.task.target_device_id == "living-room"
    assert [message["type"] for message in socket.sent] == [
        "session.init",
        "input.append",
        "session.close",
    ]
    assert socket.sent[0]["payload"] == {}
    assert socket.sent[1]["input"]["audio"]
    assert socket.headers == ["Authorization: Bearer runtime-token"]
    assert socket.closed is True
