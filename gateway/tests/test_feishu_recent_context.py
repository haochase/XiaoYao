from __future__ import annotations

from dataclasses import dataclass

from companion_gateway.channels.feishu_chat import FeishuInboundText
from companion_gateway.chat.service import FeishuChatService
from companion_gateway.context.service import ContextStoreError


class Runtime:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def respond(self, text: str, *, history=()) -> str:
        self.calls.append(text)
        return "已收到"


@dataclass
class RecordingContext:
    records: list[tuple[str, str, str]]
    cleared: int = 0
    fail_record: bool = False

    def record_user_message(self, *, channel, external_message_id, content):
        if self.fail_record:
            raise ContextStoreError("store down")
        self.records.append((channel, external_message_id, content))

    def clear(self) -> int:
        self.cleared += 1
        return 1


def inbound(
    *,
    message_id: str = "om-1",
    sender_open_id: str = "ou-owner",
    chat_type: str = "p2p",
    message_type: str = "text",
    text: str = "我今天上午去做了体检",
) -> FeishuInboundText:
    return FeishuInboundText(
        message_id=message_id,
        chat_id="oc-chat",
        sender_open_id=sender_open_id,
        sender_type="user",
        chat_type=chat_type,
        message_type=message_type,
        text=text,
    )


def test_feishu_user_message_is_persisted_once_after_validation() -> None:
    context = RecordingContext(records=[])
    service = FeishuChatService(
        owner_open_id="ou-owner",
        runtime=Runtime(),
        recent_context=context,
    )

    assert service.handle(inbound()) == "已收到"
    assert service.handle(inbound()) is None
    assert context.records == [("feishu", "om-1", "我今天上午去做了体检")]


def test_invalid_feishu_messages_are_not_persisted() -> None:
    context = RecordingContext(records=[])
    service = FeishuChatService(
        owner_open_id="ou-owner",
        runtime=Runtime(),
        recent_context=context,
    )

    assert service.handle(inbound(sender_open_id="ou-other")) is None
    assert service.handle(inbound(message_id="om-group", chat_type="group")) is None
    assert service.handle(inbound(message_id="om-image", message_type="image")) is None
    assert context.records == []


def test_clear_shared_context_clears_memory_and_persistent_context() -> None:
    context = RecordingContext(records=[])
    runtime = Runtime()
    service = FeishuChatService(
        owner_open_id="ou-owner",
        runtime=runtime,
        recent_context=context,
    )

    assert service.handle(inbound(text="清除共享上下文")) == "已清除共享近期上下文。"
    assert context.cleared == 1
    assert runtime.calls == []


def test_recent_context_write_failure_does_not_block_feishu_reply() -> None:
    context = RecordingContext(records=[], fail_record=True)
    runtime = Runtime()
    service = FeishuChatService(
        owner_open_id="ou-owner",
        runtime=runtime,
        recent_context=context,
    )

    assert service.handle(inbound()) == "已收到"
    assert runtime.calls == ["我今天上午去做了体检"]
