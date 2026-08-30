from __future__ import annotations

import json
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from companion_gateway.api import create_app
from companion_gateway.channels.feishu_chat import FeishuInboundText
from companion_gateway.chat.service import FeishuChatService
from companion_gateway.settings import Settings


class Runtime:
    def respond(self, text: str, *, history=()) -> str:
        return "已收到"


def inbound(text: str = "我今天上午去做了体检") -> FeishuInboundText:
    return FeishuInboundText(
        message_id="om-checkup",
        chat_id="oc-chat",
        sender_open_id="ou-owner",
        sender_type="user",
        chat_type="p2p",
        message_type="text",
        text=text,
    )


def enabled_settings(tmp_path) -> Settings:
    return Settings(
        database_path=tmp_path / "cross-channel.db",
        recent_context_enabled=True,
        subject_id="voice-user",
        recent_context_retention_days=7,
        recent_context_max_messages=20,
        recent_context_max_bytes=4096,
    )


def test_feishu_message_is_available_to_voice_provider_after_reopen(tmp_path) -> None:
    app = create_app(enabled_settings(tmp_path))
    chat = FeishuChatService(
        owner_open_id="ou-owner",
        runtime=Runtime(),
        recent_context=app.state.recent_context_service,
    )

    assert chat.handle(inbound()) == "已收到"
    assert "体检" in app.state.recent_context_service.build_context()

    reopened = create_app(enabled_settings(tmp_path))
    assert "体检" in reopened.state.recent_context_service.build_context(
        now=datetime(2026, 8, 30, 12, tzinfo=UTC)
    )


def test_context_clear_endpoint_is_local_only_and_removes_messages(tmp_path) -> None:
    app = create_app(enabled_settings(tmp_path))
    chat = FeishuChatService(
        owner_open_id="ou-owner",
        runtime=Runtime(),
        recent_context=app.state.recent_context_service,
    )
    chat.handle(inbound())

    with TestClient(app) as client:
        assert client.get("/v1/demo/status").json()["recent_context_count"] == 1
        cleared = client.post("/v1/context/clear")
        assert cleared.status_code == 200
        assert cleared.json() == {"deleted": 1}
        assert app.state.recent_context_service.build_context() == ""

    with TestClient(app, client=("192.0.2.10", 50000)) as client:
        assert client.post("/v1/context/clear").status_code == 403


def test_cross_channel_demo_status_does_not_expose_message_or_identity(tmp_path) -> None:
    app = create_app(enabled_settings(tmp_path))
    chat = FeishuChatService(
        owner_open_id="ou-owner",
        runtime=Runtime(),
        recent_context=app.state.recent_context_service,
    )
    chat.handle(inbound())

    with TestClient(app) as client:
        payload = client.get("/v1/demo/status").json()

    assert payload["recent_context_enabled"] is True
    assert payload["recent_context_count"] == 1
    serialized = json.dumps(payload)
    assert "体检" not in serialized
    assert "ou-owner" not in serialized
