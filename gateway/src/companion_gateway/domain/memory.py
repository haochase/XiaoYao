from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from companion_gateway.domain.models import ContentText, Identifier


class MemoryCategory(StrEnum):
    ADDRESS = "address"
    REMINDER_PREFERENCE = "reminder_preference"
    ROUTINE_PREFERENCE = "routine_preference"
    APPROVED_FACT = "approved_fact"


def _require_timezone_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value


class MemoryProposalCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    category: MemoryCategory
    value: ContentText


class MemoryCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    memory_id: Identifier | None = None
    subject_id: Identifier
    category: MemoryCategory
    value: ContentText
    confirmed: bool = False


class Memory(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    memory_id: Identifier
    subject_id: Identifier
    category: MemoryCategory
    value: ContentText
    source: Identifier
    created_at: datetime
    expires_at: datetime
    consent_at: datetime

    _validate_created_at = field_validator("created_at")(_require_timezone_aware)
    _validate_expires_at = field_validator("expires_at")(_require_timezone_aware)
    _validate_consent_at = field_validator("consent_at")(_require_timezone_aware)

    @model_validator(mode="after")
    def validate_expiry_window(self) -> "Memory":
        if self.expires_at <= self.created_at:
            raise ValueError("expires_at must be later than created_at")
        return self


class PendingMemoryProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    proposal_id: Identifier
    subject_id: Identifier
    category: MemoryCategory
    value: ContentText
    source: Identifier
    created_at: datetime
    expires_at: datetime

    _validate_created_at = field_validator("created_at")(_require_timezone_aware)
    _validate_expires_at = field_validator("expires_at")(_require_timezone_aware)

    @model_validator(mode="after")
    def validate_expiry_window(self) -> "PendingMemoryProposal":
        if self.expires_at <= self.created_at:
            raise ValueError("expires_at must be later than created_at")
        return self


class MemoryStore(Protocol):
    def upsert_memory(self, memory: Memory) -> Memory: ...

    def get_memory(self, memory_id: str) -> Memory | None: ...

    def list_memories(
        self,
        *,
        subject_id: str,
        query: str | None = None,
        limit: int | None = None,
        now: datetime,
    ) -> list[Memory]: ...

    def delete_memory(self, *, subject_id: str, memory_id: str) -> bool: ...

    def export_memories(
        self,
        *,
        subject_id: str,
        now: datetime,
    ) -> list[Memory]: ...

    def purge_expired(self, *, now: datetime) -> int: ...

    def memory_usage_bytes(self, *, subject_id: str, now: datetime) -> int: ...

    def create_memory_proposal(
        self, proposal: PendingMemoryProposal
    ) -> PendingMemoryProposal: ...

    def get_memory_proposal(self, proposal_id: str) -> PendingMemoryProposal | None: ...

    def list_memory_proposals(
        self, *, subject_id: str, now: datetime
    ) -> list[PendingMemoryProposal]: ...

    def delete_memory_proposal(self, *, subject_id: str, proposal_id: str) -> bool: ...

    def consume_memory_proposal(
        self,
        *,
        subject_id: str,
        proposal_id: str,
        memory: Memory,
        now: datetime,
    ) -> Memory | None: ...


def utc_now() -> datetime:
    return datetime.now(UTC)
