from __future__ import annotations

import asyncio
import logging
import warnings
from dataclasses import dataclass
from typing import Protocol

from companion_gateway.agent.router import AgentCommandRouter
from companion_gateway.chat.service import FeishuChatService, TextChatRuntime
from companion_gateway.context.service import ConversationContextService
from companion_gateway.voice.minicpm_o import ModelRuntimeError


logger = logging.getLogger("uvicorn.error")
_MODEL_FAILURE_REPLY = "小瑶暂时无法回复，请稍后再试。"


@dataclass(frozen=True)
class FeishuInboundText:
    message_id: str
    chat_id: str
    sender_open_id: str
    sender_type: str
    chat_type: str
    message_type: str
    text: str


class FeishuChannel(Protocol):
    def on(self, name: str, handler): ...

    async def start_background(self, *, timeout: float) -> None: ...

    async def stop_background(self) -> None: ...

    async def reply(self, message, response): ...


class FeishuChatListener:
    def __init__(
        self,
        *,
        channel: FeishuChannel,
        service: FeishuChatService,
        startup_timeout_seconds: float = 10.0,
    ) -> None:
        self._channel = channel
        self._service = service
        self._startup_timeout_seconds = startup_timeout_seconds
        self._available = False
        self._received_messages = 0
        self._replied_messages = 0
        self._channel.on("message", self._on_message)

    @property
    def is_available(self) -> bool:
        return self._available

    @property
    def received_messages(self) -> int:
        return self._received_messages

    @property
    def replied_messages(self) -> int:
        return self._replied_messages

    async def start(self) -> None:
        try:
            await self._channel.start_background(
                timeout=self._startup_timeout_seconds,
            )
        except Exception as exc:
            self._available = False
            logger.warning(
                "feishu_chat_start_failed error=%s",
                type(exc).__name__,
            )
            return
        self._available = True
        logger.info("feishu_chat_started")

    async def stop(self) -> None:
        try:
            await self._channel.stop_background()
        finally:
            self._available = False

    def set_recent_context(self, context: ConversationContextService | None) -> None:
        self._service.set_recent_context(context)

    def set_recent_context(self, context: ConversationContextService | None) -> None:
        self._service.set_recent_context(context)

    async def _on_message(self, message) -> None:
        self._received_messages += 1
        inbound = FeishuInboundText(
            message_id=getattr(message, "message_id", ""),
            chat_id=getattr(message, "chat_id", ""),
            sender_open_id=getattr(message, "sender_id", ""),
            sender_type=getattr(message, "sender_type", ""),
            chat_type=getattr(message, "chat_type", ""),
            message_type=getattr(message, "raw_content_type", ""),
            text=getattr(message, "content_text", ""),
        )
        try:
            response = await asyncio.to_thread(self._service.handle, inbound)
        except ModelRuntimeError:
            logger.warning(
                "feishu_chat_model_failed message=%s",
                inbound.message_id,
            )
            response = _MODEL_FAILURE_REPLY
        if response is None:
            return
        result = await self._channel.reply(message, {"text": response})
        if getattr(result, "success", False):
            self._replied_messages += 1
        else:
            logger.warning(
                "feishu_chat_reply_failed message=%s error=%s",
                inbound.message_id,
                type(getattr(result, "error", None)).__name__,
            )


def create_feishu_chat_listener(
    *,
    app_id: str,
    app_secret: str,
    owner_open_id: str,
    runtime: TextChatRuntime,
    history_turns: int = 6,
    startup_timeout_seconds: float = 10.0,
    base_url: str = "https://open.feishu.cn",
    agent_router: AgentCommandRouter | None = None,
    recent_context: ConversationContextService | None = None,
) -> FeishuChatListener:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        from lark_channel import (
            FeishuChannel as SdkFeishuChannel,
            InboundConfig,
            MediaCapabilities,
            PolicyConfig,
        )

    channel = SdkFeishuChannel(
        app_id=app_id,
        app_secret=app_secret,
        domain=base_url,
        policy=PolicyConfig(
            dm_policy="allowlist",
            group_policy="disabled",
            allow_from=[owner_open_id],
            sender_identity_fields=["open_id"],
        ),
        inbound=InboundConfig(
            media_capabilities=MediaCapabilities(
                image=False,
                audio=False,
                video=False,
                file=False,
                sticker=False,
            ),
            drop_self_sent=True,
            include_raw=False,
        ),
    )
    return FeishuChatListener(
        channel=channel,
        service=FeishuChatService(
            owner_open_id=owner_open_id,
            runtime=runtime,
            max_history_turns=history_turns,
            agent_router=agent_router,
            recent_context=recent_context,
        ),
        startup_timeout_seconds=startup_timeout_seconds,
    )
