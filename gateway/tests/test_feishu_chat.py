from __future__ import annotations

import asyncio
from dataclasses import dataclass
import warnings

from companion_gateway.channels.feishu_chat import (
    FeishuChatListener,
    FeishuInboundText,
    create_feishu_chat_listener,
)
from companion_gateway.chat.service import FeishuChatService, TextChatTurn
from companion_gateway.voice.minicpm_o import ModelRuntimeError


class RecordingRuntime:
    def __init__(self, replies: list[str] | None = None) -> None:
        self.replies = iter(replies or ["小瑶回复"])
        self.calls: list[tuple[str, tuple[TextChatTurn, ...]]] = []

    def respond(
        self,
        text: str,
        *,
        history: tuple[TextChatTurn, ...] = (),
    ) -> str:
        self.calls.append((text, history))
        return next(self.replies)


def inbound(
    *,
    message_id: str = "om_1",
    sender_open_id: str = "ou_owner",
    chat_type: str = "p2p",
    message_type: str = "text",
    text: str = "你好",
) -> FeishuInboundText:
    return FeishuInboundText(
        message_id=message_id,
        chat_id="oc_chat",
        sender_open_id=sender_open_id,
        sender_type="user",
        chat_type=chat_type,
        message_type=message_type,
        text=text,
    )


def test_chat_service_allows_only_owner_private_text_and_deduplicates() -> None:
    runtime = RecordingRuntime()
    service = FeishuChatService(owner_open_id="ou_owner", runtime=runtime)

    assert service.handle(inbound()) == "小瑶回复"
    assert service.handle(inbound()) is None
    assert service.handle(inbound(message_id="om_2", sender_open_id="ou_other")) is None
    assert service.handle(inbound(message_id="om_3", chat_type="group")) is None
    assert service.handle(inbound(message_id="om_4", message_type="image")) is None
    assert [call[0] for call in runtime.calls] == ["你好"]


def test_chat_service_keeps_bounded_context_and_can_clear_it() -> None:
    runtime = RecordingRuntime(["第一条回复", "第二条回复", "清空后的回复"])
    service = FeishuChatService(
        owner_open_id="ou_owner",
        runtime=runtime,
        max_history_turns=1,
    )

    assert service.handle(inbound(message_id="om_1", text="第一条")) == "第一条回复"
    assert service.handle(inbound(message_id="om_2", text="第二条")) == "第二条回复"
    assert runtime.calls[1][1] == (
        TextChatTurn(role="user", content="第一条"),
        TextChatTurn(role="assistant", content="第一条回复"),
    )
    assert service.handle(inbound(message_id="om_3", text="清除上下文")) == "已清除本次飞书对话上下文。"
    assert service.handle(inbound(message_id="om_4", text="重新开始")) == "清空后的回复"
    assert runtime.calls[2][1] == ()


@dataclass
class FakeSdkMessage:
    message_id: str = "om_sdk"
    chat_id: str = "oc_sdk"
    sender_id: str = "ou_owner"
    sender_type: str = "user"
    chat_type: str = "p2p"
    raw_content_type: str = "text"
    content_text: str = "测试"


class SendResult:
    success = True
    error = None


class FakeChannel:
    def __init__(self) -> None:
        self.handlers = {}
        self.started = 0
        self.stopped = 0
        self.replies: list[tuple[FakeSdkMessage, dict[str, str]]] = []

    def on(self, name, handler):
        self.handlers[name] = handler

    async def start_background(self, *, timeout):
        self.started += 1

    async def stop_background(self):
        self.stopped += 1

    async def reply(self, message, response):
        self.replies.append((message, response))
        return SendResult()


def test_listener_starts_maps_message_and_replies() -> None:
    async def scenario() -> None:
        channel = FakeChannel()
        service = FeishuChatService(
            owner_open_id="ou_owner",
            runtime=RecordingRuntime(["真实回复"]),
        )
        listener = FeishuChatListener(
            channel=channel,
            service=service,
            startup_timeout_seconds=3,
        )

        await listener.start()
        await channel.handlers["message"](FakeSdkMessage())
        await listener.stop()

        assert channel.started == 1
        assert channel.stopped == 1
        assert channel.replies == [(FakeSdkMessage(), {"text": "真实回复"})]
        assert listener.received_messages == 1
        assert listener.replied_messages == 1

    asyncio.run(scenario())


def test_listener_returns_redacted_fallback_when_model_fails() -> None:
    class FailingRuntime:
        def respond(self, text, *, history=()):
            raise ModelRuntimeError("private upstream details")

    async def scenario() -> None:
        channel = FakeChannel()
        listener = FeishuChatListener(
            channel=channel,
            service=FeishuChatService(
                owner_open_id="ou_owner",
                runtime=FailingRuntime(),
            ),
        )

        await channel.handlers["message"](FakeSdkMessage())

        assert channel.replies == [
            (FakeSdkMessage(), {"text": "小瑶暂时无法回复，请稍后再试。"})
        ]

    asyncio.run(scenario())


def test_listener_isolates_initial_connection_failure() -> None:
    class FailingChannel(FakeChannel):
        async def start_background(self, *, timeout):
            self.started += 1
            raise RuntimeError("private credential or network details")

    async def scenario() -> None:
        channel = FailingChannel()
        listener = FeishuChatListener(
            channel=channel,
            service=FeishuChatService(
                owner_open_id="ou_owner",
                runtime=RecordingRuntime(),
            ),
        )

        await listener.start()

        assert listener.is_available is False
        assert channel.started == 1

    asyncio.run(scenario())


def test_factory_configures_single_owner_dm_policy() -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        listener = create_feishu_chat_listener(
            app_id="cli_test_app",
            app_secret="secret_test_value",
            owner_open_id="ou_owner",
            runtime=RecordingRuntime(),
            history_turns=4,
            startup_timeout_seconds=3,
        )

    policy = listener._channel.get_policy()

    assert policy.dm_policy == "allowlist"
    assert policy.allow_from == ["ou_owner"]
    assert policy.sender_identity_fields == ["open_id"]
    assert policy.group_policy == "disabled"
    assert not [item for item in caught if item.category is DeprecationWarning]
