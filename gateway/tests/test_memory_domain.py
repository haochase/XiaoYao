from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from companion_gateway.domain.memory import (
    Memory,
    MemoryCandidate,
    MemoryCategory,
    MemoryProposalCandidate,
    PendingMemoryProposal,
)


NOW = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)


def test_memory_models_accept_approved_profile_data() -> None:
    candidate = MemoryCandidate(
        subject_id="family-1",
        category=MemoryCategory.REMINDER_PREFERENCE,
        value="morning reminders are preferred",
        confirmed=True,
    )
    memory = Memory(
        memory_id="mem-1",
        subject_id=candidate.subject_id,
        category=candidate.category,
        value=candidate.value,
        source="trc-1",
        created_at=NOW,
        expires_at=NOW + timedelta(days=60),
        consent_at=NOW,
    )

    assert candidate.confirmed is True
    assert memory.category is MemoryCategory.REMINDER_PREFERENCE
    assert memory.expires_at == NOW + timedelta(days=60)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("subject_id", ""),
        ("value", ""),
        ("source", ""),
    ],
)
def test_memory_rejects_blank_identifiers_and_content(field: str, value: str) -> None:
    data = {
        "memory_id": "mem-1",
        "subject_id": "family-1",
        "category": MemoryCategory.APPROVED_FACT,
        "value": "approved fact",
        "source": "trc-1",
        "created_at": NOW,
        "expires_at": NOW + timedelta(days=1),
        "consent_at": NOW,
    }
    data[field] = value

    with pytest.raises(ValidationError):
        Memory.model_validate(data)


def test_memory_rejects_unknown_category_and_fields() -> None:
    with pytest.raises(ValidationError):
        MemoryCandidate.model_validate(
            {
                "subject_id": "family-1",
                "category": "medical_history",
                "value": "not allowed",
                "confirmed": True,
            }
        )

    with pytest.raises(ValidationError):
        MemoryCandidate.model_validate(
            {
                "subject_id": "family-1",
                "category": MemoryCategory.APPROVED_FACT,
                "value": "approved fact",
                "confirmed": True,
                "raw_transcript": "do not store",
            }
        )


def test_memory_rejects_naive_timestamps_and_invalid_expiry() -> None:
    with pytest.raises(ValidationError):
        Memory(
            memory_id="mem-1",
            subject_id="family-1",
            category=MemoryCategory.APPROVED_FACT,
            value="approved fact",
            source="trc-1",
            created_at=datetime(2026, 8, 11, 12, 0),
            expires_at=NOW + timedelta(days=1),
            consent_at=NOW,
        )


def test_memory_proposal_candidate_accepts_only_bounded_profile_values() -> None:
    proposal = MemoryProposalCandidate(
        category=MemoryCategory.ADDRESS,
        value="Call me Chase",
    )

    assert proposal.category is MemoryCategory.ADDRESS
    assert proposal.value == "Call me Chase"

    with pytest.raises(ValidationError):
        MemoryProposalCandidate.model_validate(
            {
                "category": MemoryCategory.ADDRESS,
                "value": "Call me Chase",
                "subject_id": "should be gateway-owned",
            }
        )


def test_pending_memory_proposal_requires_aware_expiring_timestamps() -> None:
    pending = PendingMemoryProposal(
        proposal_id="prop-1",
        subject_id="voice-user",
        category=MemoryCategory.ADDRESS,
        value="Call me Chase",
        source="trace-1",
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=10),
    )

    assert pending.expires_at == NOW + timedelta(minutes=10)

    with pytest.raises(ValidationError):
        PendingMemoryProposal(
            proposal_id="prop-1",
            subject_id="voice-user",
            category=MemoryCategory.ADDRESS,
            value="Call me Chase",
            source="trace-1",
            created_at=datetime(2026, 8, 11, 12, 0),
            expires_at=NOW + timedelta(minutes=10),
        )

    with pytest.raises(ValidationError):
        Memory(
            memory_id="mem-1",
            subject_id="family-1",
            category=MemoryCategory.APPROVED_FACT,
            value="approved fact",
            source="trc-1",
            created_at=NOW,
            expires_at=NOW,
            consent_at=NOW,
        )
