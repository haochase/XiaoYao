from __future__ import annotations

import base64
import json
import logging
import wave
from datetime import UTC, datetime
from io import BytesIO
from urllib.error import HTTPError

import pytest

from companion_gateway.audio.bridge import Pcm16Mono
from companion_gateway.domain.memory import MemoryCategory
from companion_gateway.voice import mimo_v25
from companion_gateway.voice.mimo_v25 import MimoV25Runtime, ModelRuntimeError


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


def fixed_clock() -> datetime:
    return datetime(2026, 8, 13, 4, 34, tzinfo=UTC)


def test_mimo_runtime_enables_info_diagnostics() -> None:
    assert mimo_v25.logger.level == logging.INFO
    assert mimo_v25.logger.name == "uvicorn.error"


def chat_payload(content: str) -> bytes:
    return json.dumps(
        {
            "choices": [{"message": {"role": "assistant", "content": content}}]
        }
    ).encode()


def tts_payload(audio: bytes = b"\x02\x00" * 1_440) -> bytes:
    return json.dumps(
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "audio": {"data": base64.b64encode(audio).decode()},
                    }
                }
            ]
        }
    ).encode()


def test_mimo_runtime_sends_audio_to_chat_then_tts(monkeypatch) -> None:
    requests: list[dict[str, object]] = []
    responses = iter(
        [
            FakeResponse(chat_payload('{"reply":"MiMo 在这里。","task":null}')),
            FakeResponse(tts_payload()),
        ]
    )

    def fake_urlopen(request, *, timeout):
        requests.append(
            {
                "url": request.full_url,
                "timeout": timeout,
                "body": json.loads(request.data),
                "api_key": request.get_header("Api-key"),
            }
        )
        return next(responses)

    monkeypatch.setattr(mimo_v25, "urlopen", fake_urlopen)
    runtime = MimoV25Runtime(
        openai_base_url="https://token-plan-cn.xiaomimimo.com/v1",
        api_key="example-token",
        timeout_seconds=7.5,
    )

    response = runtime.respond(input_pcm())

    assert response.text == "MiMo 在这里。"
    assert response.pcm == Pcm16Mono(sample_rate=24_000, payload=b"\x02\x00" * 1_440)
    assert [request["url"] for request in requests] == [
        "https://token-plan-cn.xiaomimimo.com/v1/chat/completions",
        "https://token-plan-cn.xiaomimimo.com/v1/chat/completions",
    ]
    assert all(request["api_key"] == "example-token" for request in requests)
    audio_part = requests[0]["body"]["messages"][1]["content"][0]
    assert audio_part["type"] == "input_audio"
    assert audio_part["input_audio"]["data"].startswith("data:audio/wav;base64,")
    audio_data = audio_part["input_audio"]["data"].split(",", 1)[1]
    with wave.open(BytesIO(base64.b64decode(audio_data)), "rb") as wav:
        assert wav.getframerate() == 16_000
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
    assert requests[1]["body"]["model"] == "mimo-v2.5-tts"
    assert requests[1]["body"]["audio"] == {
        "format": "pcm16",
        "voice": "mimo_default",
    }


def test_mimo_runtime_returns_structured_intent_without_early_tts(
    monkeypatch,
) -> None:
    requests: list[dict[str, object]] = []

    def fake_urlopen(request, *, timeout):
        requests.append(json.loads(request.data))
        return FakeResponse(
            chat_payload(
                '{"reply":"模型可能答错时间。","task":null,'
                '"action":null,"intent":{"type":"current_time"}}'
            )
        )

    monkeypatch.setattr(mimo_v25, "urlopen", fake_urlopen)
    runtime = MimoV25Runtime(
        openai_base_url="https://token-plan-cn.xiaomimimo.com/v1",
        api_key="example-token",
        clock=fixed_clock,
    )

    response = runtime.respond(input_pcm())

    assert response.intent is not None
    assert response.intent.type == "current_time"
    assert response.pcm is None
    assert len(requests) == 1
    system_prompt = requests[0]["messages"][0]["content"]
    assert "intent" in system_prompt
    assert "current_time" in system_prompt


@pytest.mark.parametrize("reply_fragment", ["", '"reply":"",'])
def test_mimo_runtime_accepts_intent_without_model_reply(
    monkeypatch,
    reply_fragment: str,
) -> None:
    requests: list[dict[str, object]] = []

    def fake_urlopen(request, *, timeout):
        requests.append(json.loads(request.data))
        return FakeResponse(
            chat_payload(
                "{" + reply_fragment
                + '"task":null,"action":null,'
                '"intent":{"type":"current_time"}}'
            )
        )

    monkeypatch.setattr(mimo_v25, "urlopen", fake_urlopen)
    runtime = MimoV25Runtime(
        openai_base_url="https://token-plan-cn.xiaomimimo.com/v1",
        api_key="example-token",
    )

    response = runtime.respond(input_pcm())

    assert response.intent is not None
    assert response.intent.type == "current_time"
    assert response.pcm is None
    assert len(requests) == 1


def test_mimo_runtime_rejects_invalid_structured_intent(monkeypatch) -> None:
    monkeypatch.setattr(
        mimo_v25,
        "urlopen",
        lambda request, *, timeout: FakeResponse(
            chat_payload(
                '{"reply":"未知意图","task":null,'
                '"action":null,"intent":{"type":"weather"}}'
            )
        ),
    )
    runtime = MimoV25Runtime(
        openai_base_url="https://token-plan-cn.xiaomimimo.com/v1",
        api_key="example-token",
    )

    with pytest.raises(ModelRuntimeError, match="intent is invalid"):
        runtime.respond(input_pcm())


def test_mimo_runtime_injects_current_shanghai_time(monkeypatch) -> None:
    requests: list[dict[str, object]] = []

    def fake_urlopen(request, *, timeout):
        body = json.loads(request.data)
        requests.append(body)
        if body["model"] == "mimo-v2.5":
            return FakeResponse(chat_payload('{"reply":"ok","task":null}'))
        return FakeResponse(tts_payload())

    monkeypatch.setattr(mimo_v25, "urlopen", fake_urlopen)
    runtime = MimoV25Runtime(
        openai_base_url="https://token-plan-cn.xiaomimimo.com/v1",
        api_key="example-token",
        clock=fixed_clock,
    )

    runtime.respond(input_pcm())

    system_prompt = requests[0]["messages"][0]["content"]
    assert "2026-08-13T12:34:00+08:00" in system_prompt
    assert "Asia/Shanghai" in system_prompt
    assert "match the user's requested precision exactly" in system_prompt
    assert "only hour and minute" in system_prompt


def test_mimo_runtime_logs_stage_durations_without_content(monkeypatch) -> None:
    responses = iter(
        [
            FakeResponse(chat_payload('{"reply":"private reply","task":null}')),
            FakeResponse(tts_payload()),
        ]
    )
    log_messages: list[str] = []
    timestamps = iter([10.0, 12.5, 12.5, 14.0])

    monkeypatch.setattr(
        mimo_v25,
        "urlopen",
        lambda request, *, timeout: next(responses),
    )
    monkeypatch.setattr(mimo_v25.time, "perf_counter", lambda: next(timestamps))
    monkeypatch.setattr(
        mimo_v25.logger,
        "info",
        lambda message, *args: log_messages.append(message % args),
    )
    runtime = MimoV25Runtime(
        openai_base_url="https://token-plan-cn.xiaomimimo.com/v1",
        api_key="example-token",
        clock=fixed_clock,
    )

    runtime.respond(input_pcm())

    assert log_messages == [
        "mimo_voice_turn_completed model=mimo-v2.5 chat_duration_ms=2500 "
        "tts_duration_ms=1500 total_duration_ms=4000 output_audio_ms=60"
    ]
    assert "private reply" not in log_messages[0]


def test_mimo_runtime_synthesizes_reminder_text_with_tts(monkeypatch) -> None:
    requests: list[dict[str, object]] = []

    def fake_urlopen(request, *, timeout):
        requests.append(json.loads(request.data))
        return FakeResponse(tts_payload())

    monkeypatch.setattr(mimo_v25, "urlopen", fake_urlopen)
    runtime = MimoV25Runtime(
        openai_base_url="https://token-plan-cn.xiaomimimo.com/v1",
        api_key="example-token",
    )

    pcm = runtime.synthesize("请按时服药")

    assert pcm == Pcm16Mono(sample_rate=24_000, payload=b"\x02\x00" * 1_440)
    assert requests == [
        {
            "model": "mimo-v2.5-tts",
            "messages": [{"role": "assistant", "content": "请按时服药"}],
            "audio": {"format": "pcm16", "voice": "mimo_default"},
            "stream": False,
        }
    ]


def test_mimo_runtime_extracts_validated_task(monkeypatch) -> None:
    task = {
        "actor_id": "voice-user",
        "target_device_id": "living-room",
        "kind": "reminder",
        "schedule": {"at": "2026-08-09T20:00:00+08:00", "timezone": "Asia/Shanghai"},
        "payload": {"text": "take medicine"},
        "confirmation_policy": "required",
        "idempotency_key": "voice:mimo:1",
    }
    monkeypatch.setattr(
        mimo_v25,
        "urlopen",
        lambda request, *, timeout: (
            FakeResponse(chat_payload(json.dumps({"reply": "好的。", "task": task})))
            if json.loads(request.data)["model"] == "mimo-v2.5"
            else FakeResponse(tts_payload())
        ),
    )
    runtime = MimoV25Runtime(
        openai_base_url="https://token-plan-cn.xiaomimimo.com/v1",
        api_key="example-token",
    )

    response = runtime.respond(input_pcm())

    assert response.task is not None
    assert response.task.target_device_id == "living-room"


def test_mimo_runtime_extracts_validated_medication_action(monkeypatch) -> None:
    monkeypatch.setattr(
        mimo_v25,
        "urlopen",
        lambda request, *, timeout: (
            FakeResponse(
                chat_payload(
                    json.dumps(
                        {
                            "reply": "好的，已记录。",
                            "task": None,
                            "action": {
                                "type": "acknowledge_medication_occurrence",
                                "occurrence_id": "med-occurrence-1",
                            },
                        },
                        ensure_ascii=False,
                    )
                )
            )
            if json.loads(request.data)["model"] == "mimo-v2.5"
            else FakeResponse(tts_payload())
        ),
    )
    runtime = MimoV25Runtime(
        openai_base_url="https://token-plan-cn.xiaomimimo.com/v1",
        api_key="example-token",
    )

    response = runtime.respond(input_pcm())

    assert response.action is not None
    assert response.action.type == "acknowledge_medication_occurrence"
    assert response.action.occurrence_id == "med-occurrence-1"


def test_mimo_runtime_extracts_valid_memory_proposals_and_ignores_invalid_items(
    monkeypatch,
) -> None:
    payload = {
        "reply": "好的",
        "task": None,
        "memory_proposals": [
            {"category": "address", "value": "Call me Chase"},
            {"category": "not_allowed", "value": "ignored"},
            {"category": "routine_preference", "value": ""},
            {"category": "approved_fact", "value": "Approved fact"},
            {"category": "reminder_preference", "value": "too many"},
        ],
    }
    monkeypatch.setattr(
        mimo_v25,
        "urlopen",
        lambda request, *, timeout: (
            FakeResponse(chat_payload(json.dumps(payload)))
            if json.loads(request.data)["model"] == "mimo-v2.5"
            else FakeResponse(tts_payload())
        ),
    )
    runtime = MimoV25Runtime(
        openai_base_url="https://token-plan-cn.xiaomimimo.com/v1",
        api_key="example-token",
    )

    response = runtime.respond(input_pcm())

    assert [item.category for item in response.memory_proposals] == [
        MemoryCategory.ADDRESS,
        MemoryCategory.APPROVED_FACT,
        MemoryCategory.REMINDER_PREFERENCE,
    ]


def test_mimo_runtime_preserves_empty_context_and_appends_bounded_context(
    monkeypatch,
) -> None:
    requests: list[dict[str, object]] = []

    def fake_urlopen(request, *, timeout):
        body = json.loads(request.data)
        requests.append(body)
        if body["model"] == "mimo-v2.5":
            return FakeResponse(chat_payload('{"reply":"ok","task":null}'))
        return FakeResponse(tts_payload())

    monkeypatch.setattr(mimo_v25, "urlopen", fake_urlopen)
    baseline = MimoV25Runtime(
        openai_base_url="https://token-plan-cn.xiaomimi.test/v1",
        api_key="example-token",
        clock=fixed_clock,
    )
    baseline.respond(input_pcm())
    baseline_system = requests[0]["messages"][0]["content"]

    requests.clear()
    empty_context = MimoV25Runtime(
        openai_base_url="https://token-plan-cn.xiaomimi.test/v1",
        api_key="example-token",
        clock=fixed_clock,
    )
    empty_context.set_memory_context("")
    empty_context.respond(input_pcm())
    assert requests[0]["messages"][0]["content"] == baseline_system

    requests.clear()
    with_context = MimoV25Runtime(
        openai_base_url="https://token-plan-cn.xiaomimi.test/v1",
        api_key="example-token",
        clock=fixed_clock,
    )
    with_context.set_memory_context("\nApproved user preference: preferred form of address: Chase")
    with_context.respond(input_pcm())
    assert requests[0]["messages"][0]["content"].endswith(
        "preferred form of address: Chase"
    )


def test_mimo_runtime_appends_gateway_agent_context_without_changing_response_schema(
    monkeypatch,
) -> None:
    requests: list[dict[str, object]] = []

    def fake_urlopen(request, *, timeout):
        body = json.loads(request.data)
        requests.append(body)
        if body["model"] == "mimo-v2.5":
            return FakeResponse(
                chat_payload(
                    '{"reply":"继续练习。","task":null,"action":null,"intent":null}'
                )
            )
        return FakeResponse(tts_payload())

    monkeypatch.setattr(mimo_v25, "urlopen", fake_urlopen)
    runtime = MimoV25Runtime(
        openai_base_url="https://token-plan-cn.xiaomimi.test/v1",
        api_key="example-token",
        clock=fixed_clock,
    )
    runtime.set_agent_context("\nGateway-owned active English practice context.")

    response = runtime.respond(input_pcm())

    system = requests[0]["messages"][0]["content"]
    assert "Gateway-owned active English practice context." in system
    assert "keys reply, task, action, and intent" in system
    assert response.task is None
    assert response.action is None
    assert response.intent is None


def test_mimo_memory_proposal_prompt_is_opt_in(monkeypatch) -> None:
    requests: list[dict[str, object]] = []

    def fake_urlopen(request, *, timeout):
        body = json.loads(request.data)
        requests.append(body)
        if body["model"] == "mimo-v2.5":
            return FakeResponse(chat_payload('{"reply":"ok","task":null}'))
        return FakeResponse(tts_payload())

    monkeypatch.setattr(mimo_v25, "urlopen", fake_urlopen)
    disabled = MimoV25Runtime(
        openai_base_url="https://token-plan-cn.xiaomimi.test/v1",
        api_key="example-token",
    )
    disabled.respond(input_pcm())
    disabled_system = requests[0]["messages"][0]["content"]

    requests.clear()
    enabled = MimoV25Runtime(
        openai_base_url="https://token-plan-cn.xiaomimi.test/v1",
        api_key="example-token",
        memory_proposals_enabled=True,
    )
    enabled.respond(input_pcm())
    enabled_system = requests[0]["messages"][0]["content"]

    assert "memory_proposals" not in disabled_system
    assert "memory_proposals" in enabled_system


def test_mimo_runtime_rejects_unknown_voice_action(monkeypatch) -> None:
    monkeypatch.setattr(
        mimo_v25,
        "urlopen",
        lambda request, *, timeout: FakeResponse(
            chat_payload(
                json.dumps(
                    {
                        "reply": "ignored",
                        "task": None,
                        "action": {"type": "send_feishu_message"},
                    }
                )
            )
        ),
    )
    runtime = MimoV25Runtime(
        openai_base_url="https://token-plan-cn.xiaomimimo.com/v1",
        api_key="example-token",
    )

    with pytest.raises(ModelRuntimeError, match="action"):
        runtime.respond(input_pcm())


def test_mimo_runtime_rejects_non_16khz_input() -> None:
    runtime = MimoV25Runtime(
        openai_base_url="https://token-plan-cn.xiaomimimo.com/v1",
        api_key="example-token",
    )

    with pytest.raises(ModelRuntimeError, match="16000"):
        runtime.respond(Pcm16Mono(sample_rate=24_000, payload=b"\x00\x00" * 10))


def test_mimo_runtime_maps_transport_failure(monkeypatch) -> None:
    def fail_urlopen(request, *, timeout):
        raise OSError("connection refused")

    monkeypatch.setattr(mimo_v25, "urlopen", fail_urlopen)
    runtime = MimoV25Runtime(
        openai_base_url="https://token-plan-cn.xiaomimimo.com/v1",
        api_key="example-token",
    )

    with pytest.raises(ModelRuntimeError, match="request failed"):
        runtime.respond(input_pcm())


def test_mimo_runtime_retries_429_and_5xx_with_exponential_backoff(
    monkeypatch,
) -> None:
    responses = iter(
        [
            HTTPError(
                "https://token-plan.example.test/v1/chat/completions",
                429,
                "rate limited",
                hdrs=None,
                fp=BytesIO(),
            ),
            FakeResponse(b"", status=503),
            FakeResponse(chat_payload('{"reply":"retry ok","task":null}')),
            FakeResponse(tts_payload()),
        ]
    )
    requests: list[object] = []
    delays: list[float] = []

    def fake_urlopen(request, *, timeout):
        requests.append(request)
        response = next(responses)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(mimo_v25, "urlopen", fake_urlopen)
    monkeypatch.setattr(mimo_v25.time, "sleep", delays.append)
    runtime = MimoV25Runtime(
        openai_base_url="https://token-plan-cn.xiaomimo.test/v1",
        api_key="example-token",
        max_retries=3,
        retry_backoff_seconds=0.25,
    )

    response = runtime.respond(input_pcm())

    assert response.text == "retry ok"
    assert len(requests) == 4
    assert delays == [0.25, 0.5]


def test_mimo_runtime_does_not_retry_non_transient_http_errors(monkeypatch) -> None:
    requests: list[object] = []
    delays: list[float] = []

    def fake_urlopen(request, *, timeout):
        requests.append(request)
        return FakeResponse(b"", status=401)

    monkeypatch.setattr(mimo_v25, "urlopen", fake_urlopen)
    monkeypatch.setattr(mimo_v25.time, "sleep", delays.append)
    runtime = MimoV25Runtime(
        openai_base_url="https://token-plan-cn.xiaomimo.test/v1",
        api_key="example-token",
        max_retries=3,
    )

    with pytest.raises(ModelRuntimeError, match="HTTP 401"):
        runtime.respond(input_pcm())

    assert len(requests) == 1
    assert delays == []


def test_mimo_runtime_stops_after_configured_retry_budget(monkeypatch) -> None:
    requests: list[object] = []
    delays: list[float] = []

    def fake_urlopen(request, *, timeout):
        requests.append(request)
        return FakeResponse(b"", status=500)

    monkeypatch.setattr(mimo_v25, "urlopen", fake_urlopen)
    monkeypatch.setattr(mimo_v25.time, "sleep", delays.append)
    runtime = MimoV25Runtime(
        openai_base_url="https://token-plan-cn.xiaomimo.test/v1",
        api_key="example-token",
        max_retries=2,
        retry_backoff_seconds=0.5,
    )

    with pytest.raises(ModelRuntimeError, match="HTTP 500"):
        runtime.respond(input_pcm())

    assert len(requests) == 3
    assert delays == [0.5, 1.0]


def test_mimo_runtime_disables_reasoning_for_voice_chat(monkeypatch) -> None:
    requests: list[dict[str, object]] = []
    responses = iter(
        [
            FakeResponse(chat_payload('{"reply":"voice ok","task":null}')),
            FakeResponse(tts_payload()),
        ]
    )

    def fake_urlopen(request, *, timeout):
        requests.append(json.loads(request.data))
        return next(responses)

    monkeypatch.setattr(mimo_v25, "urlopen", fake_urlopen)
    runtime = MimoV25Runtime(
        openai_base_url="https://token-plan-cn.xiaomimimo.com/v1",
        api_key="example-token",
    )

    runtime.respond(input_pcm())

    assert requests[0]["thinking"] == {"type": "disabled"}
