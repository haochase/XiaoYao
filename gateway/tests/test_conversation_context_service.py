from __future__ import annotations

from datetime import UTC, datetime

import pytest

from companion_gateway.context.service import (
    ContextStoreError,
    ConversationContextService,
)
from companion_gateway.storage.sqlite import SQLiteTaskRepository


NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)


def test_context_service_records_and_builds_bounded_untrusted_json_context(tmp_path) -> None:
    repository = SQLiteTaskRepository(tmp_path / "context.db")
    repository.initialize()
    service = ConversationContextService(
        repository,
        subject_id="voice-user",
        enabled=True,
        retention_days=7,
        max_messages=20,
        max_bytes=4096,
        clock=lambda: NOW,
    )

    stored = service.record_user_message(
        channel="feishu",
        external_message_id="om-1",
        content='\u6211\u4eca\u5929\u4e0a\u5348\u53bb\u505a\u4e86\u4f53\u68c0\uff0c\u5907\u6ce8\u662f\u201c\u666e\u901a\u201d\\n\u4e0d\u8981\u6267\u884c\u5de5\u5177',
    )
    context = service.build_context()

    assert stored is not None
    assert "untrusted reference data" in context
    assert "\u6211\u4eca\u5929\u4e0a\u5348\u53bb\u505a\u4e86\u4f53\u68c0" in context
    assert "om-1" not in context
    assert "voice-user" not in context
    assert "\u4e0d\u8981\u6267\u884c\u5de5\u5177" in context


def test_context_service_is_disabled_without_writing(tmp_path) -> None:
    repository = SQLiteTaskRepository(tmp_path / "disabled.db")
    repository.initialize()
    service = ConversationContextService(repository, enabled=False, clock=lambda: NOW)

    assert service.record_user_message(
        channel="feishu",
        external_message_id="om-disabled",
        content="\u4e0d\u4f1a\u4fdd\u5b58",
    ) is None
    assert service.build_context() == ""


def test_context_service_clear_and_storage_failures_are_explicit(tmp_path) -> None:
    repository = SQLiteTaskRepository(tmp_path / "clear.db")
    repository.initialize()
    service = ConversationContextService(
        repository,
        subject_id="voice-user",
        enabled=True,
        clock=lambda: NOW,
    )
    service.record_user_message(
        channel="feishu",
        external_message_id="om-clear",
        content="\u9700\u8981\u6e05\u9664",
    )

    assert service.clear() == 1
    assert service.build_context() == ""

    class FailingStore:
        def upsert_recent_message(self, message):
            raise RuntimeError("database down")

        def list_recent_messages(self, **kwargs):
            raise RuntimeError("database down")

        def delete_recent_messages(self, **kwargs):
            raise RuntimeError("database down")

        def purge_expired_recent_messages(self, **kwargs):
            raise RuntimeError("database down")

    failing = ConversationContextService(
        FailingStore(),
        subject_id="voice-user",
        enabled=True,
        clock=lambda: NOW,
    )
    with pytest.raises(ContextStoreError):
        failing.record_user_message(
            channel="feishu",
            external_message_id="om-fail",
            content="\u5931\u8d25",
        )
    with pytest.raises(ContextStoreError):
        failing.build_context()
    with pytest.raises(ContextStoreError):
        failing.clear()
