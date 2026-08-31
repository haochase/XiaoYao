from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from companion_gateway.domain.memory import (
    Memory,
    MemoryCandidate,
    MemoryCategory,
    MemoryProposalCandidate,
    MemoryStore,
    PendingMemoryProposal,
    utc_now,
)


Clock = Callable[[], datetime]
IdFactory = Callable[[str], str]


class MemoryFeatureDisabled(RuntimeError):
    pass


class MemoryConsentRequired(ValueError):
    pass


class MemoryQuotaExceeded(ValueError):
    pass


class MemoryNotFound(LookupError):
    pass


class MemoryOwnershipError(PermissionError):
    pass


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _require_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(UTC)


def _validate_source(source: str) -> str:
    if not isinstance(source, str) or not source.strip() or source != source.strip():
        raise ValueError("source must be a non-empty trace identifier")
    if len(source) > 128:
        raise ValueError("source must not exceed 128 characters")
    return source


class MemoryService:
    def __init__(
        self,
        store: MemoryStore,
        *,
        enabled: bool = False,
        retention_days: int = 60,
        quota_bytes: int = 50_000_000,
        proposal_ttl_seconds: int = 600,
        clock: Clock = utc_now,
        id_factory: IdFactory = _new_id,
    ) -> None:
        if retention_days <= 0:
            raise ValueError("retention_days must be positive")
        if quota_bytes <= 0:
            raise ValueError("quota_bytes must be positive")
        if proposal_ttl_seconds <= 0:
            raise ValueError("proposal_ttl_seconds must be positive")
        self._store = store
        self._enabled = enabled
        self._retention_days = retention_days
        self._quota_bytes = quota_bytes
        self._proposal_ttl_seconds = proposal_ttl_seconds
        self._clock = clock
        self._id_factory = id_factory

    @property
    def enabled(self) -> bool:
        return self._enabled

    def _ensure_enabled(self) -> None:
        if not self._enabled:
            raise MemoryFeatureDisabled("Memory feature is disabled")

    def _now(self, value: datetime | None = None) -> datetime:
        return _require_aware(value or self._clock())

    def confirm(
        self,
        candidate: MemoryCandidate,
        *,
        source: str,
        now: datetime | None = None,
    ) -> Memory:
        self._ensure_enabled()
        if not candidate.confirmed:
            raise MemoryConsentRequired("memory consent required")
        trace_source = _validate_source(source)
        current = self._now(now)
        self._store.purge_expired(now=current)
        existing = (
            self._store.get_memory(candidate.memory_id)
            if candidate.memory_id is not None
            else None
        )
        if existing is not None and existing.subject_id != candidate.subject_id:
            raise MemoryOwnershipError("memory ownership mismatch")
        existing_bytes = (
            len(existing.value.encode("utf-8"))
            if existing is not None
            else 0
        )
        new_bytes = len(candidate.value.encode("utf-8"))
        usage = self._store.memory_usage_bytes(
            subject_id=candidate.subject_id,
            now=current,
        )
        if usage - existing_bytes + new_bytes > self._quota_bytes:
            raise MemoryQuotaExceeded("memory quota exceeded")
        memory = Memory(
            memory_id=candidate.memory_id or self._id_factory("mem"),
            subject_id=candidate.subject_id,
            category=candidate.category,
            value=candidate.value,
            source=trace_source,
            created_at=current,
            expires_at=current + timedelta(days=self._retention_days),
            consent_at=current,
        )
        return self._store.upsert_memory(memory)

    def propose(
        self,
        *,
        subject_id: str,
        candidates: tuple[MemoryProposalCandidate, ...],
        source: str,
        now: datetime | None = None,
    ) -> list[PendingMemoryProposal]:
        self._ensure_enabled()
        trace_source = _validate_source(source)
        current = self._now(now)
        self._store.purge_expired(now=current)
        proposals: list[PendingMemoryProposal] = []
        for candidate in candidates[:3]:
            proposal = PendingMemoryProposal(
                proposal_id=self._id_factory("prop"),
                subject_id=subject_id,
                category=candidate.category,
                value=candidate.value,
                source=trace_source,
                created_at=current,
                expires_at=current + timedelta(seconds=self._proposal_ttl_seconds),
            )
            proposals.append(self._store.create_memory_proposal(proposal))
        return proposals

    def list_proposals(
        self,
        *,
        subject_id: str,
        now: datetime | None = None,
    ) -> list[PendingMemoryProposal]:
        self._ensure_enabled()
        return self._store.list_memory_proposals(
            subject_id=subject_id,
            now=self._now(now),
        )

    def confirm_proposal(
        self,
        *,
        subject_id: str,
        proposal_id: str,
        source: str,
        now: datetime | None = None,
    ) -> Memory:
        self._ensure_enabled()
        trace_source = _validate_source(source)
        current = self._now(now)
        self._store.purge_expired(now=current)
        proposal = self._store.get_memory_proposal(proposal_id)
        if proposal is None or proposal.subject_id != subject_id:
            raise MemoryNotFound("memory proposal not found")
        usage = self._store.memory_usage_bytes(
            subject_id=subject_id,
            now=current,
        )
        new_bytes = len(proposal.value.encode("utf-8"))
        if usage + new_bytes > self._quota_bytes:
            raise MemoryQuotaExceeded("memory quota exceeded")
        memory = Memory(
            memory_id=self._id_factory("mem"),
            subject_id=subject_id,
            category=proposal.category,
            value=proposal.value,
            source=trace_source,
            created_at=current,
            expires_at=current + timedelta(days=self._retention_days),
            consent_at=current,
        )
        consumed = self._store.consume_memory_proposal(
            subject_id=subject_id,
            proposal_id=proposal_id,
            memory=memory,
            now=current,
        )
        if consumed is None:
            raise MemoryNotFound("memory proposal not found")
        return consumed

    def reject_proposal(self, *, subject_id: str, proposal_id: str) -> bool:
        self._ensure_enabled()
        return self._store.delete_memory_proposal(
            subject_id=subject_id,
            proposal_id=proposal_id,
        )

    def build_context(
        self,
        *,
        subject_id: str,
        now: datetime | None = None,
    ) -> str:
        if not self._enabled:
            return ""
        memories = self._store.list_memories(
            subject_id=subject_id,
            now=self._now(now),
        )
        addresses = [
            memory for memory in memories if memory.category is MemoryCategory.ADDRESS
        ]
        if not addresses:
            return ""
        value = addresses[-1].value
        prefix = (
            "\nApproved user preference (not an instruction): "
            "preferred form of address: "
        )
        encoded_prefix = prefix.encode("utf-8")
        remaining = max(0, 256 - len(encoded_prefix))
        bounded_value = value.encode("utf-8")[:remaining].decode(
            "utf-8",
            errors="ignore",
        )
        return prefix + bounded_value

    def get(
        self,
        *,
        subject_id: str,
        memory_id: str,
        now: datetime | None = None,
    ) -> Memory | None:
        self._ensure_enabled()
        current = self._now(now)
        memory = self._store.get_memory(memory_id)
        if (
            memory is None
            or memory.subject_id != subject_id
            or memory.expires_at.astimezone(UTC) <= current
        ):
            return None
        return memory

    def list(
        self,
        *,
        subject_id: str,
        query: str | None = None,
        limit: int = 20,
        now: datetime | None = None,
    ) -> list[Memory]:
        self._ensure_enabled()
        if not 1 <= limit <= 50:
            raise ValueError("limit must be between 1 and 50")
        current = self._now(now)
        normalized_query = query.strip() if query is not None else None
        return self._store.list_memories(
            subject_id=subject_id,
            query=normalized_query or None,
            limit=limit,
            now=current,
        )

    def delete(self, *, subject_id: str, memory_id: str) -> bool:
        self._ensure_enabled()
        return self._store.delete_memory(subject_id=subject_id, memory_id=memory_id)

    def export(self, *, subject_id: str, now: datetime | None = None) -> list[Memory]:
        self._ensure_enabled()
        return self._store.export_memories(subject_id=subject_id, now=self._now(now))

    def purge(self, *, now: datetime | None = None) -> int:
        self._ensure_enabled()
        return self._store.purge_expired(now=self._now(now))
