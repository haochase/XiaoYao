from __future__ import annotations

import json
from urllib.error import HTTPError

from companion_gateway.chat import mimo as mimo_chat
from companion_gateway.chat.mimo import MimoTextChatRuntime
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


def test_mimo_text_chat_sends_bounded_history_and_returns_plain_text(monkeypatch) -> None:
    requests: list[dict[str, object]] = []

    def fake_urlopen(request, *, timeout):
        requests.append(
            {
                "url": request.full_url,
                "timeout": timeout,
                "body": json.loads(request.data),
                "api_key": request.get_header("Api-key"),
            }
        )
        return FakeResponse(chat_response("我是小瑶，现在可以和你聊天。"))

    monkeypatch.setattr(mimo_chat, "urlopen", fake_urlopen)
    runtime = MimoTextChatRuntime(
        openai_base_url="https://token-plan-cn.xiaomimimo.com/v1",
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
        "https://token-plan-cn.xiaomimimo.com/v1/chat/completions"
    )
    assert requests[0]["timeout"] == 7.5
    assert requests[0]["api_key"] == "example-token"
    assert requests[0]["body"]["model"] == "mimo-v2.5"
    assert requests[0]["body"]["messages"][1:] == [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "你好，我是小瑶。"},
        {"role": "user", "content": "你是谁？"},
    ]
    assert requests[0]["body"]["thinking"] == {"type": "disabled"}


def test_mimo_text_chat_retries_429_and_5xx(monkeypatch) -> None:
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

    monkeypatch.setattr(mimo_chat, "urlopen", fake_urlopen)
    monkeypatch.setattr(mimo_chat.time, "sleep", delays.append)
    runtime = MimoTextChatRuntime(
        openai_base_url="https://token-plan-cn.xiaomimimo.com/v1",
        api_key="example-token",
        max_retries=2,
        retry_backoff_seconds=0.25,
    )

    assert runtime.respond("测试") == "重试成功"
    assert attempts == 3
    assert delays == [0.25, 0.5]
