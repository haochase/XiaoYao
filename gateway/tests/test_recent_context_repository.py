from __future__ import annotations

from datetime import UTC, datetime, timedelta

from companion_gateway.domain.recent_context import RecentChannelMessage
from companion_gateway.storage.sqlite import SQLiteTaskRepository


NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)


def message(
    external_id: str,
    content: str,
    *,
    subject_id: str = "voice-user",
    created_at: datetime = NOW,
    channel: str = "feishu",
) -> RecentChannelMessage:
    return RecentChannelMessage(
        message_id=f"recent-{external_id}",
        subject_id=subject_id,
        channel=channel,
        external_message_id=external_id,
        content=content,
        created_at=created_at,
        expires_at=created_at + timedelta(days=7),
    )


def test_recent_messages_persist_across_repository_reopen(tmp_path) -> None:
    path = tmp_path / "recent-context.db"
    repository = SQLiteTaskRepository(path)
    repository.initialize()
    stored = repository.upsert_recent_message(message("m-1", "今天上午去做了体检"))

    reopened = SQLiteTaskRepository(path)
    reopened.initialize()

    assert reopened.list_recent_messages(
        subject_id="voice-user",
        now=NOW,
        limit=20,
        max_bytes=4096,
    ) == [stored]


def test_recent_messages_are_idempotent_and_subject_scoped(tmp_path) -> None:
    repository = SQLiteTaskRepository(tmp_path / "recent-context.db")
    repository.initialize()
    first = message("m-1", "第一条")
    repository.upsert_recent_message(first)
    duplicate = repository.upsert_recent_message(message("m-1", "不应覆盖"))
    repository.upsert_recent_message(message("m-2", "其他用户", subject_id="other"))

    assert duplicate == first
    assert repository.list_recent_messages(
        subject_id="voice-user", now=NOW, limit=20, max_bytes=4096
    ) == [first]
    assert repository.delete_recent_messages(subject_id="voice-user") == 1
    assert repository.list_recent_messages(
        subject_id="other", now=NOW, limit=20, max_bytes=4096
    ) == [message("m-2", "其他用户", subject_id="other")]


def test_recent_messages_filter_expired_and_bound_count_and_utf8_bytes(tmp_path) -> None:
    repository = SQLiteTaskRepository(tmp_path / "recent-context.db")
    repository.initialize()
    repository.upsert_recent_message(
        message("expired", "过期", created_at=NOW - timedelta(days=7))
    )
    for index in range(3):
        repository.upsert_recent_message(message(f"m-{index}", f"第{index}条"))
    repository.upsert_recent_message(message("emoji", "体检" + "🙂" * 20))

    bounded = repository.list_recent_messages(
        subject_id="voice-user",
        now=NOW,
        limit=2,
        max_bytes=len("第0条".encode()) + len("第1条".encode()),
    )

    assert [item.external_message_id for item in bounded] == ["m-0", "m-1"]
    assert repository.purge_expired_recent_messages(now=NOW) == 1
