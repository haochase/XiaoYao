from datetime import UTC, datetime, timedelta

from companion_gateway.domain.memory import (
    Memory,
    MemoryCategory,
    PendingMemoryProposal,
)
from companion_gateway.storage.sqlite import SQLiteTaskRepository


NOW = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)


def make_memory(
    memory_id: str,
    *,
    subject_id: str = "family-1",
    value: str = "morning reminders",
    expires_at: datetime | None = None,
) -> Memory:
    return Memory(
        memory_id=memory_id,
        subject_id=subject_id,
        category=MemoryCategory.REMINDER_PREFERENCE,
        value=value,
        source="trc-memory",
        created_at=NOW,
        expires_at=expires_at or NOW + timedelta(days=60),
        consent_at=NOW,
    )


def make_proposal(
    proposal_id: str,
    *,
    subject_id: str = "family-1",
    value: str = "Call me Chase",
    expires_at: datetime | None = None,
) -> PendingMemoryProposal:
    return PendingMemoryProposal(
        proposal_id=proposal_id,
        subject_id=subject_id,
        category=MemoryCategory.ADDRESS,
        value=value,
        source="trace-proposal",
        created_at=NOW,
        expires_at=expires_at or NOW + timedelta(minutes=10),
    )


def test_memory_storage_is_idempotent_and_survives_reopen(tmp_path) -> None:
    database_path = tmp_path / "memory.db"
    repository = SQLiteTaskRepository(database_path)
    repository.initialize()
    repository.initialize()
    stored = repository.upsert_memory(make_memory("mem-1"))

    reopened = SQLiteTaskRepository(database_path)
    reopened.initialize()

    assert reopened.get_memory("mem-1") == stored
    assert reopened.list_memories(subject_id="family-1", now=NOW) == [stored]


def test_memory_query_export_delete_and_subject_isolation(tmp_path) -> None:
    repository = SQLiteTaskRepository(tmp_path / "memory.db")
    repository.initialize()
    first = make_memory("mem-1", value="morning reminders")
    second = make_memory(
        "mem-2",
        subject_id="family-2",
        value="evening reminders",
    )
    repository.upsert_memory(first)
    repository.upsert_memory(second)

    assert repository.list_memories(
        subject_id="family-1",
        query="morning",
        now=NOW,
    ) == [first]
    assert repository.export_memories(subject_id="family-1", now=NOW) == [first]
    assert repository.delete_memory(subject_id="family-2", memory_id="mem-1") is False
    assert repository.delete_memory(subject_id="family-1", memory_id="mem-1") is True
    assert repository.get_memory("mem-1") is None
    assert repository.get_memory("mem-2") == second


def test_memory_expiry_filter_and_purge_are_deterministic(tmp_path) -> None:
    repository = SQLiteTaskRepository(tmp_path / "memory.db")
    repository.initialize()
    active = make_memory("mem-active")
    expired = make_memory(
        "mem-expired",
        value="old preference",
        expires_at=NOW + timedelta(seconds=1),
    )
    repository.upsert_memory(active)
    repository.upsert_memory(expired)
    later = NOW + timedelta(seconds=2)

    assert repository.list_memories(subject_id="family-1", now=later) == [active]
    assert repository.memory_usage_bytes(subject_id="family-1", now=later) == len(
        active.value.encode("utf-8")
    )
    assert repository.purge_expired(now=later) == 1
    assert repository.get_memory("mem-expired") is None


def test_memory_upsert_replaces_same_id_and_counts_utf8_value_bytes(tmp_path) -> None:
    repository = SQLiteTaskRepository(tmp_path / "memory.db")
    repository.initialize()
    original = make_memory("mem-1", value="早晨")
    updated = make_memory("mem-1", value="晚上")
    repository.upsert_memory(original)
    repository.upsert_memory(updated)

    assert repository.get_memory("mem-1") == updated
    assert repository.memory_usage_bytes(subject_id="family-1", now=NOW) == len(
        "晚上".encode("utf-8")
    )


def test_pending_memory_proposals_persist_filter_and_isolate_subjects(tmp_path) -> None:
    database_path = tmp_path / "memory.db"
    repository = SQLiteTaskRepository(database_path)
    repository.initialize()
    first = make_proposal("prop-1")
    other = make_proposal("prop-2", subject_id="family-2", value="Other")
    expired = make_proposal(
        "prop-expired",
        expires_at=NOW + timedelta(seconds=1),
    )
    repository.create_memory_proposal(first)
    repository.create_memory_proposal(other)
    repository.create_memory_proposal(expired)

    reopened = SQLiteTaskRepository(database_path)
    reopened.initialize()
    later = NOW + timedelta(seconds=2)

    assert reopened.list_memory_proposals(subject_id="family-1", now=later) == [first]
    assert reopened.get_memory_proposal("prop-2") == other
    assert reopened.delete_memory_proposal(
        subject_id="family-2",
        proposal_id="prop-1",
    ) is False
    assert reopened.delete_memory_proposal(
        subject_id="family-1",
        proposal_id="prop-1",
    ) is True
    assert reopened.get_memory_proposal("prop-1") is None


def test_consuming_pending_proposal_inserts_memory_and_deletes_pending_row(tmp_path) -> None:
    repository = SQLiteTaskRepository(tmp_path / "memory.db")
    repository.initialize()
    proposal = make_proposal("prop-1")
    repository.create_memory_proposal(proposal)
    memory = make_memory("mem-from-proposal", value=proposal.value)

    consumed = repository.consume_memory_proposal(
        subject_id="family-1",
        proposal_id="prop-1",
        memory=memory,
        now=NOW,
    )

    assert consumed == memory
    assert repository.get_memory_proposal("prop-1") is None
    assert repository.get_memory(memory.memory_id) == memory


def test_expired_or_cross_subject_consume_does_not_write_memory(tmp_path) -> None:
    repository = SQLiteTaskRepository(tmp_path / "memory.db")
    repository.initialize()
    expired = make_proposal(
        "prop-expired",
        expires_at=NOW + timedelta(seconds=1),
    )
    repository.create_memory_proposal(expired)
    expired_memory = make_memory("mem-expired", value=expired.value)

    assert repository.consume_memory_proposal(
        subject_id="family-1",
        proposal_id="prop-expired",
        memory=expired_memory,
        now=NOW + timedelta(seconds=2),
    ) is None
    assert repository.get_memory("mem-expired") is None

    other = make_proposal("prop-other", subject_id="family-2", value="Other")
    repository.create_memory_proposal(other)
    other_memory = make_memory("mem-other", subject_id="family-1", value="Other")
    assert repository.consume_memory_proposal(
        subject_id="family-1",
        proposal_id="prop-other",
        memory=other_memory,
        now=NOW,
    ) is None
    assert repository.get_memory("mem-other") is None
