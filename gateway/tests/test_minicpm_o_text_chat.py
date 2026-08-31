from __future__ import annotations

import json
from datetime import UTC, datetime
from urllib.error import HTTPError

from companion_gateway.chat import minicpm_o as minicpm_chat
from companion_gateway.chat.minicpm_o import MinicpmOTextChatRuntime
from companion_gateway.chat.service import TextChatTurn


class FakeResponse:
    def __init__(self, payload: dict[str, object], *, status: int = 200) -> None:
        self.status = status
        self._payload = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        return None

    def read(self) -> bytes:
        return self._payload


def chat_response(text: str) -> dict[str, object]:
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


def test_minicpm_o_text_chat_sends_bounded_history_and_returns_plain_text(monkeypatch) -> None:
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
        return FakeResponse(chat_response("我是小瑶，现在可以和你聊天。"))

    monkeypatch.setattr(minicpm_chat, "urlopen", fake_urlopen)
    runtime = MinicpmOTextChatRuntime(
        openai_base_url="https://minicpm.example.test/v1",
        api_key="example-token",
        timeout_seconds=7.5,
    )

    reply = runtime.respond(
        "你是谁？",
        history=(
            TextChatTurn(role="user", content="你好"),
            TextChatTurn(role="assistant", content="你好，我是小瑶。"),
        ),
    )

    assert reply == "我是小瑶，现在可以和你聊天。"
    assert requests[0]["url"] == (
        "https://minicpm.example.test/v1/chat/completions"
    )
    assert requests[0]["timeout"] == 7.5
    assert requests[0]["authorization"] == "Bearer example-token"
    assert requests[0]["body"]["model"] == "MiniCPM-O-4.5-9B"
    assert requests[0]["body"]["messages"][1:] == [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "你好，我是小瑶。"},
        {"role": "user", "content": "你是谁？"},
    ]
    assert requests[0]["body"]["thinking"] == {"type": "disabled"}


def test_minicpm_o_text_chat_retries_429_and_5xx(monkeypatch) -> None:
    attempts = 0
    delays: list[float] = []

    def fake_urlopen(request, *, timeout):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise HTTPError(request.full_url, 429, "limited", {}, None)
        if attempts == 2:
            return FakeResponse({}, status=503)
        return FakeResponse(chat_response("重试成功"))

    monkeypatch.setattr(minicpm_chat, "urlopen", fake_urlopen)
    monkeypatch.setattr(minicpm_chat.time, "sleep", delays.append)
    runtime = MinicpmOTextChatRuntime(
        openai_base_url="https://minicpm.example.test/v1",
        api_key="example-token",
        max_retries=2,
        retry_backoff_seconds=0.25,
    )

    assert runtime.respond("测试") == "重试成功"
    assert attempts == 3
    assert delays == [0.25, 0.5]


def test_minicpm_o_text_chat_adds_gateway_agent_context_only_to_the_system_message(monkeypatch) -> None:
    requests: list[dict[str, object]] = []

    def fake_urlopen(request, *, timeout):
        requests.append(json.loads(request.data))
        return FakeResponse(chat_response("active reply"))

    monkeypatch.setattr(minicpm_chat, "urlopen", fake_urlopen)
    runtime = MinicpmOTextChatRuntime(
        openai_base_url="https://minicpm.example.test/v1",
        api_key="example-token",
    )

    assert runtime.respond(
        "continue",
        agent_context="Gateway active mode: companion. Continue safely.",
    ) == "active reply"
    assert "Gateway active mode: companion. Continue safely." in requests[0]["messages"][0]["content"]
    assert requests[0]["messages"][1:] == [{"role": "user", "content": "continue"}]


def test_minicpm_o_text_chat_injects_authoritative_gateway_time(monkeypatch) -> None:
    requests: list[dict[str, object]] = []

    def fake_urlopen(request, *, timeout):
        requests.append(json.loads(request.data))
        return FakeResponse(chat_response("收到"))

    monkeypatch.setattr(minicpm_chat, "urlopen", fake_urlopen)
    runtime = MinicpmOTextChatRuntime(
        openai_base_url="https://minicpm.example.test/v1",
        api_key="example-token",
        clock=lambda: datetime(2026, 8, 31, 16, 5, tzinfo=UTC),
    )

    assert runtime.respond("今天几点提醒我喝水") == "收到"
    assert "2026-09-01T00:05:00+08:00" in requests[0]["messages"][0]["content"]
