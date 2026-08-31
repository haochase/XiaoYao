from datetime import UTC, datetime, timedelta
from itertools import count

import pytest

from companion_gateway.domain.memory import (
    MemoryCandidate,
    MemoryCategory,
    MemoryProposalCandidate,
)
from companion_gateway.memory.service import (
    MemoryConsentRequired,
    MemoryFeatureDisabled,
    MemoryNotFound,
    MemoryOwnershipError,
    MemoryQuotaExceeded,
    MemoryService,
)
from companion_gateway.storage.sqlite import SQLiteTaskRepository


NOW = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)


def candidate(
    value: str = "morning reminders",
    *,
    subject_id: str = "family-1",
    confirmed: bool = True,
    memory_id: str | None = None,
    category: MemoryCategory = MemoryCategory.REMINDER_PREFERENCE,
) -> MemoryCandidate:
    return MemoryCandidate(
        memory_id=memory_id,
        subject_id=subject_id,
        category=category,
        value=value,
        confirmed=confirmed,
    )


def make_service(
    tmp_path,
    *,
    enabled: bool = True,
    retention_days: int = 60,
    quota_bytes: int = 50_000_000,
    proposal_ttl_seconds: int = 600,
    clock=lambda: NOW,
):
    repository = SQLiteTaskRepository(tmp_path / "memory.db")
    repository.initialize()
    ids = count(1)
    return MemoryService(
        repository,
        enabled=enabled,
        retention_days=retention_days,
        quota_bytes=quota_bytes,
        proposal_ttl_seconds=proposal_ttl_seconds,
        clock=clock,
        id_factory=lambda prefix: f"{prefix}-generated-{next(ids)}",
    ), repository


def test_disabled_memory_rejects_reads_and_writes(tmp_path) -> None:
    service, _ = make_service(tmp_path, enabled=False)

    with pytest.raises(MemoryFeatureDisabled):
        service.confirm(candidate(), source="trace-1")
    with pytest.raises(MemoryFeatureDisabled):
        service.list(subject_id="family-1")


def test_unconfirmed_candidate_never_touches_storage(tmp_path) -> None:
    service, repository = make_service(tmp_path)

    with pytest.raises(MemoryConsentRequired):
        service.confirm(candidate(confirmed=False), source="trace-1")

    assert repository.export_memories(subject_id="family-1", now=NOW) == []


def test_confirmed_memory_gets_trace_source_and_two_month_expiry(tmp_path) -> None:
    service, repository = make_service(tmp_path)

    memory = service.confirm(candidate("morning reminders"), source="trace-1")

    assert memory.source == "trace-1"
    assert memory.consent_at == NOW
    assert memory.expires_at == NOW + timedelta(days=60)
    assert repository.get_memory(memory.memory_id) == memory


def test_memory_quota_and_update_ownership_are_enforced(tmp_path) -> None:
    service, repository = make_service(tmp_path, quota_bytes=5)
    first = service.confirm(candidate("hello"), source="trace-1")

    with pytest.raises(MemoryQuotaExceeded):
        service.confirm(candidate("!"), source="trace-2")

    with pytest.raises(MemoryOwnershipError):
        service.confirm(
            candidate("other", subject_id="family-2", memory_id=first.memory_id),
            source="trace-3",
        )

    updated = service.confirm(
        candidate("world", memory_id=first.memory_id),
        source="trace-4",
    )
    assert updated.memory_id == first.memory_id
    assert updated.value == "world"
    assert repository.memory_usage_bytes(subject_id="family-1", now=NOW) == 5


def test_query_delete_export_and_expiry_purge_use_subject_scope(tmp_path) -> None:
    service, _ = make_service(tmp_path, retention_days=1)
    memory = service.confirm(candidate("morning routine"), source="trace-1")
    other = service.confirm(
        candidate("other routine", subject_id="family-2"),
        source="trace-2",
    )

    assert service.list(subject_id="family-1", query="routine") == [memory]
    assert service.export(subject_id="family-1") == [memory]
    assert service.delete(subject_id="family-2", memory_id=memory.memory_id) is False
    assert service.delete(subject_id="family-1", memory_id=memory.memory_id) is True
    assert service.get(subject_id="family-2", memory_id=other.memory_id) == other

    service.confirm(candidate("expires"), source="trace-3")
    assert service.purge(now=NOW + timedelta(days=1)) == 2


def test_proposal_creation_is_pending_only_until_explicit_confirmation(tmp_path) -> None:
    service, repository = make_service(tmp_path)
    proposals = service.propose(
        subject_id="family-1",
        candidates=(
            MemoryProposalCandidate(
                category=MemoryCategory.ADDRESS,
                value="Call me Chase",
            ),
        ),
        source="trace-model",
    )

    assert len(proposals) == 1
    assert proposals[0].source == "trace-model"
    assert service.list_proposals(subject_id="family-1") == proposals
    assert repository.export_memories(subject_id="family-1", now=NOW) == []

    confirmed = service.confirm_proposal(
        subject_id="family-1",
        proposal_id=proposals[0].proposal_id,
        source="trace-user-confirm",
    )
    assert confirmed.value == "Call me Chase"
    assert confirmed.category is MemoryCategory.ADDRESS
    assert confirmed.source == "trace-user-confirm"
    assert service.list_proposals(subject_id="family-1") == []
    assert repository.export_memories(subject_id="family-1", now=NOW) == [confirmed]


def test_proposal_rejection_expiry_and_subject_mismatch_do_not_write_memory(tmp_path) -> None:
    service, repository = make_service(tmp_path, proposal_ttl_seconds=60)
    proposal = service.propose(
        subject_id="family-2",
        candidates=(
            MemoryProposalCandidate(
                category=MemoryCategory.ADDRESS,
                value="Other user",
            ),
        ),
        source="trace-model",
    )[0]

    with pytest.raises(MemoryNotFound):
        service.confirm_proposal(
            subject_id="family-1",
            proposal_id=proposal.proposal_id,
            source="trace-wrong-user",
        )
    assert repository.export_memories(subject_id="family-1", now=NOW) == []
    assert service.reject_proposal(
        subject_id="family-1",
        proposal_id=proposal.proposal_id,
    ) is False
    assert service.reject_proposal(
        subject_id="family-2",
        proposal_id=proposal.proposal_id,
    ) is True

    expired = service.propose(
        subject_id="family-1",
        candidates=(
            MemoryProposalCandidate(
                category=MemoryCategory.ADDRESS,
                value="Expired",
            ),
        ),
        source="trace-model",
        now=NOW,
    )[0]
    with pytest.raises(MemoryNotFound):
        service.confirm_proposal(
            subject_id="family-1",
            proposal_id=expired.proposal_id,
            source="trace-confirm",
            now=NOW + timedelta(seconds=61),
        )
    assert repository.get_memory(expired.proposal_id) is None


def test_proposal_confirmation_preserves_quota_and_pending_row_on_failure(tmp_path) -> None:
    service, repository = make_service(tmp_path, quota_bytes=5)
    existing = service.confirm(
        candidate("hello", category=MemoryCategory.ADDRESS),
        source="trace-existing",
    )
    proposal = service.propose(
        subject_id="family-1",
        candidates=(
            MemoryProposalCandidate(
                category=MemoryCategory.ADDRESS,
                value="!",
            ),
        ),
        source="trace-model",
    )[0]

    with pytest.raises(MemoryQuotaExceeded):
        service.confirm_proposal(
            subject_id="family-1",
            proposal_id=proposal.proposal_id,
            source="trace-confirm",
        )

    assert repository.get_memory(existing.memory_id) == existing
    assert service.list_proposals(subject_id="family-1") == [proposal]


def test_context_is_disabled_or_address_only_and_byte_bounded(tmp_path) -> None:
    disabled, _ = make_service(tmp_path / "disabled", enabled=False)
    assert disabled.build_context(subject_id="family-1") == ""

    service, _ = make_service(tmp_path / "enabled")
    service.confirm(
        candidate("A" * 500, category=MemoryCategory.ADDRESS),
        source="trace-address",
    )
    service.confirm(
        candidate("do not inject", category=MemoryCategory.REMINDER_PREFERENCE),
        source="trace-reminder",
    )

    context = service.build_context(subject_id="family-1")

    assert "A" in context
    assert "do not inject" not in context
    assert len(context.encode("utf-8")) <= 256
