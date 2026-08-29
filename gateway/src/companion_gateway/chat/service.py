from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass
from inspect import Parameter, signature
from threading import Lock
from typing import Literal, Protocol

from companion_gateway.agent.router import AgentCommandRouter
from companion_gateway.context.service import (
    ContextStoreError,
    ConversationContextService,
)


@dataclass(frozen=True)
class TextChatTurn:
    role: Literal["user", "assistant"]
    content: str


class TextChatRuntime(Protocol):
    def respond(
        self,
        text: str,
        *,
        history: tuple[TextChatTurn, ...] = (),
        agent_context: str | None = None,
    ) -> str: ...


class InboundTextMessage(Protocol):
    message_id: str
    chat_id: str
    sender_open_id: str
    sender_type: str
    chat_type: str
    message_type: str
    text: str


class FeishuChatService:
    def __init__(
        self,
        *,
        owner_open_id: str,
        runtime: TextChatRuntime,
        max_history_turns: int = 6,
        dedup_capacity: int = 2_048,
        agent_router: AgentCommandRouter | None = None,
        recent_context: ConversationContextService | None = None,
    ) -> None:
        if not owner_open_id or owner_open_id != owner_open_id.strip():
            raise ValueError("owner_open_id must be a non-empty token")
        if max_history_turns < 1:
            raise ValueError("max_history_turns must be positive")
        if dedup_capacity < 1:
            raise ValueError("dedup_capacity must be positive")
        self._owner_open_id = owner_open_id
        self._runtime = runtime
        self._max_history_turns = max_history_turns
        self._dedup_capacity = dedup_capacity
        self._agent_router = agent_router
        self._recent_context = recent_context
        self._seen_message_ids: OrderedDict[str, None] = OrderedDict()
        self._history: dict[str, deque[TextChatTurn]] = {}
        self._lock = Lock()

    def handle(self, message: InboundTextMessage) -> str | None:
        text = message.text.strip()
        if (
            message.sender_open_id != self._owner_open_id
            or message.sender_type != "user"
            or message.chat_type != "p2p"
            or message.message_type != "text"
            or not text
        ):
            return None

        with self._lock:
            if message.message_id in self._seen_message_ids:
                return None
            self._seen_message_ids[message.message_id] = None
            while len(self._seen_message_ids) > self._dedup_capacity:
                self._seen_message_ids.popitem(last=False)

            if text == "清除上下文":
                self._history.pop(message.chat_id, None)
                return "已清除本次飞书对话上下文。"
            if text == "清除共享上下文":
                self._history.pop(message.chat_id, None)
                if self._recent_context is None:
                    return "共享近期上下文功能未启用。"
                try:
                    self._recent_context.clear()
                except ContextStoreError:
                    return "共享近期上下文清除失败，请稍后重试。"
                return "已清除共享近期上下文。"
            if text == "帮助":
                return "你可以直接和小瑶聊天，发送“清除上下文”可开始新对话。"
            history = tuple(self._history.get(message.chat_id, ()))

        agent_context = ""
        if self._agent_router is not None:
            routed = self._agent_router.handle(
                text=text,
                owner_id=self._owner_open_id,
                chat_id=message.chat_id,
                source_message_id=message.message_id,
            )
            if routed.handled:
                return routed.reply
            active_context = getattr(self._agent_router, "active_context", None)
            if callable(active_context):
                supplied_context = active_context(
                    owner_id=self._owner_open_id,
                    chat_id=message.chat_id,
                )
                if isinstance(supplied_context, str):
                    agent_context = supplied_context.strip()

        if self._recent_context is not None:
            try:
                self._recent_context.record_user_message(
                    channel="feishu",
                    external_message_id=message.message_id,
                    content=text,
                )
            except ContextStoreError:
                # Recent context is an enhancement; it must not block chat.
                pass

        if agent_context and _supports_agent_context(self._runtime):
            reply = self._runtime.respond(
                text,
                history=history,
                agent_context=agent_context,
            ).strip()
        else:
            reply = self._runtime.respond(text, history=history).strip()
        if not reply:
            raise ValueError("text chat runtime returned an empty reply")

        with self._lock:
            conversation = self._history.setdefault(
                message.chat_id,
                deque(maxlen=self._max_history_turns * 2),
            )
            conversation.append(TextChatTurn(role="user", content=text))
            conversation.append(TextChatTurn(role="assistant", content=reply))
        return reply


def _supports_agent_context(runtime: TextChatRuntime) -> bool:
    try:
        parameters = signature(runtime.respond).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.name == "agent_context"
        or parameter.kind is Parameter.VAR_KEYWORD
        for parameter in parameters
    )
