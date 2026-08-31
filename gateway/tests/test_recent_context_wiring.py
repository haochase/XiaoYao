from __future__ import annotations

from datetime import UTC, datetime

from companion_gateway.api import create_app
from companion_gateway.settings import Settings


class Listener:
    is_available = False
    received_messages = 0
    replied_messages = 0

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    def set_recent_context(self, context) -> None:
        self.recent_context = context


class Delivery:
    def __init__(self) -> None:
        self.provider = None

    def set_task_executor(self, value) -> None:
        return None

    def set_task_service(self, value) -> None:
        return None

    def set_medication_service(self, value) -> None:
        return None

    def set_memory_service(self, value) -> None:
        return None

    def set_agent_context_provider(self, value) -> None:
        return None

    def set_recent_context_provider(self, value) -> None:
        self.provider = value


def test_create_app_wires_one_recent_context_service_to_feishu_and_voice(tmp_path) -> None:
    listener = Listener()
    delivery = Delivery()
    app = create_app(
        Settings(
            database_path=tmp_path / "wiring.db",
            recent_context_enabled=True,
            subject_id="voice-user",
            recent_context_retention_days=7,
            recent_context_max_messages=20,
            recent_context_max_bytes=4096,
        ),
        feishu_chat_listener_factory=lambda _router: listener,
        voice_delivery_service=delivery,
    )

    assert app.state.recent_context_service.enabled is True
    assert listener.recent_context is app.state.recent_context_service
    assert delivery.provider is not None
    assert delivery.provider("voice-user", "living-room") == ""


def test_create_app_keeps_recent_context_disabled_by_default(tmp_path) -> None:
    app = create_app(Settings(database_path=tmp_path / "disabled.db"))

    assert app.state.recent_context_service.enabled is False
