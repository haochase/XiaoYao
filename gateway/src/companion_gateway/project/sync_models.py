from __future__ import annotations

import math
import re
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from companion_gateway.project.models import ProjectContextPackage


_PROJECT_OR_SYNC_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


class SyncSourceType(StrEnum):
    DOCUMENT = "document"
    MEETING_NOTE = "meeting_note"
    TASK = "task"
    CALENDAR = "calendar"


class SourceSyncStatus(StrEnum):
    ACTIVE = "active"
    STALE = "stale"
    DELETED = "deleted"
    REVOKED = "revoked"
    FAILED = "failed"


class SourceErrorType(StrEnum):
    NETWORK_TIMEOUT = "network_timeout"
    PERMISSION_DENIED = "permission_denied"
    NODE_NOT_FOUND = "node_not_found"
    INVALID_PAYLOAD = "invalid_payload"
    AUTHENTICATION_FAILED = "authentication_failed"
    RATE_LIMITED = "rate_limited"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    UNKNOWN = "unknown"


class ProjectSyncOutcome(StrEnum):
    APPLIED = "applied"
    UNCHANGED = "unchanged"
    DEGRADED = "degraded"
    REJECTED = "rejected"
    FAILED = "failed"


class ProjectSyncHealth(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    STALE = "stale"
    CLOCK_UNTRUSTED = "clock_untrusted"


class RetrievalRequestStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    EXPIRED = "expired"


def _require_non_blank(value: str, field_name: str) -> str:
    if not value.strip():
        raise ValueError(f"{field_name} must not be blank")
    return value


def _require_aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


def _require_project_or_sync_id(value: str, field_name: str) -> str:
    _require_non_blank(value, field_name)
    if _PROJECT_OR_SYNC_ID_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field_name} is invalid")
    return value


def _require_sha256(value: str, field_name: str) -> str:
    if _SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a lowercase SHA-256 hash")
    return value


class EvidenceChunk(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    chunk_id: str = Field(pattern=r"[0-9a-f]{64}")
    source_id: str = Field(min_length=1, max_length=256)
    source_version: str = Field(min_length=1, max_length=256)
    ordinal: int = Field(ge=0)
    heading_path: tuple[str, ...] = ()
    text: str = Field(min_length=1, max_length=1200)
    start_offset: int = Field(ge=0)
    end_offset: int = Field(gt=0)
    content_hash: str = Field(pattern=r"[0-9a-f]{64}")

    _chunk_id = field_validator("chunk_id")(
        lambda value: _require_sha256(value, "chunk_id")
    )
    _source_id = field_validator("source_id")(
        lambda value: _require_non_blank(value, "source_id")
    )
    _source_version = field_validator("source_version")(
        lambda value: _require_non_blank(value, "source_version")
    )
    _content_hash = field_validator("content_hash")(
        lambda value: _require_sha256(value, "content_hash")
    )

    @model_validator(mode="after")
    def validate_offsets(self) -> "EvidenceChunk":
        if self.end_offset <= self.start_offset:
            raise ValueError("end_offset must be greater than start_offset")
        return self


class SourceSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_type: SyncSourceType
    source_id: str = Field(min_length=1, max_length=256)
    source_title: str = Field(min_length=1, max_length=512)
    source_url: str = Field(min_length=1, max_length=2048)
    source_version: str | None = Field(default=None, max_length=256)
    source_time: datetime | None = None
    fetched_at: datetime
    permission_scope: str = Field(min_length=1, max_length=256)
    permission_hash: str = Field(pattern=r"[0-9a-f]{64}")
    status: SourceSyncStatus
    chunks: tuple[EvidenceChunk, ...] = ()
    content_hash: str | None = Field(default=None, pattern=r"[0-9a-f]{64}")
    error_type: SourceErrorType | None = None
    retryable: bool | None = None
    retry_after_seconds: float | None = Field(default=None, ge=0)

    _source_id = field_validator("source_id")(
        lambda value: _require_non_blank(value, "source_id")
    )
    _permission_hash = field_validator("permission_hash")(
        lambda value: _require_sha256(value, "permission_hash")
    )
    _content_hash = field_validator("content_hash")(
        lambda value: _require_sha256(value, "content_hash")
        if value is not None
        else value
    )
    _source_time = field_validator("source_time")(
        lambda value: _require_aware(value, "source_time")
        if value is not None
        else value
    )
    _fetched_at = field_validator("fetched_at")(
        lambda value: _require_aware(value, "fetched_at")
    )

    @field_validator("retry_after_seconds")
    @classmethod
    def validate_retry_after_seconds(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("retry_after_seconds must be finite")
        return value

    @model_validator(mode="after")
    def validate_status_fields(self) -> "SourceSnapshot":
        if self.status is SourceSyncStatus.ACTIVE:
            if (
                self.source_version is None
                or self.source_time is None
                or self.content_hash is None
            ):
                raise ValueError(
                    "active source requires source_version, source_time, and "
                    "content_hash"
                )
            if (
                self.error_type is not None
                or self.retryable is not None
                or self.retry_after_seconds is not None
            ):
                raise ValueError("active source forbids error and retry fields")
            if self.source_type in {
                SyncSourceType.DOCUMENT,
                SyncSourceType.MEETING_NOTE,
            } and not self.chunks:
                raise ValueError(
                    "active document and meeting_note sources require chunks"
                )
        elif self.status is SourceSyncStatus.FAILED:
            if self.chunks or self.content_hash is not None:
                raise ValueError("failed source forbids chunks and content_hash")
            self._require_error_and_retry_fields("failed")
        elif self.status is SourceSyncStatus.STALE:
            if self.chunks:
                raise ValueError("stale source forbids chunks")
            if self.source_version is None or self.content_hash is None:
                raise ValueError(
                    "stale source requires source_version and content_hash"
                )
            self._require_error_and_retry_fields("stale")
        else:
            if (
                self.chunks
                or self.content_hash is not None
                or self.error_type is not None
                or self.retryable is not None
                or self.retry_after_seconds is not None
            ):
                raise ValueError(
                    "deleted and revoked sources forbid content and error fields"
                )

        if (
            self.retry_after_seconds is not None
            and self.retryable is not True
        ):
            raise ValueError("retry_after_seconds requires retryable=true")
        for item in self.chunks:
            if item.source_id != self.source_id:
                raise ValueError("all chunks must use the snapshot source_id")
            if item.source_version != self.source_version:
                raise ValueError("all chunks must use the snapshot source_version")
        return self

    def _require_error_and_retry_fields(self, status: str) -> None:
        if self.error_type is None or self.retryable is None:
            raise ValueError(f"{status} source requires error_type and retryable")


class SourceTombstone(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_type: SyncSourceType
    source_id: str = Field(min_length=1, max_length=256)
    status: Literal[SourceSyncStatus.DELETED, SourceSyncStatus.REVOKED]
    occurred_at: datetime
    permission_scope: str = Field(min_length=1, max_length=256)

    _source_id = field_validator("source_id")(
        lambda value: _require_non_blank(value, "source_id")
    )
    _occurred_at = field_validator("occurred_at")(
        lambda value: _require_aware(value, "occurred_at")
    )


class SourceState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str = Field(min_length=1, max_length=128)
    source_type: SyncSourceType
    source_id_hash: str = Field(pattern=r"[0-9a-f]{64}")
    source_version: str | None = Field(max_length=256)
    content_hash: str | None = Field(pattern=r"[0-9a-f]{64}")
    permission_hash: str = Field(pattern=r"[0-9a-f]{64}")
    status: SourceSyncStatus
    last_attempt_at: datetime
    last_success_at: datetime | None
    last_error_type: SourceErrorType | None

    _project_id = field_validator("project_id")(
        lambda value: _require_project_or_sync_id(value, "project_id")
    )
    _source_id_hash = field_validator("source_id_hash")(
        lambda value: _require_sha256(value, "source_id_hash")
    )
    _content_hash = field_validator("content_hash")(
        lambda value: _require_sha256(value, "content_hash")
        if value is not None
        else value
    )
    _permission_hash = field_validator("permission_hash")(
        lambda value: _require_sha256(value, "permission_hash")
    )
    _last_attempt_at = field_validator("last_attempt_at")(
        lambda value: _require_aware(value, "last_attempt_at")
    )
    _last_success_at = field_validator("last_success_at")(
        lambda value: _require_aware(value, "last_success_at")
        if value is not None
        else value
    )

    @model_validator(mode="after")
    def validate_lifecycle(self) -> "SourceState":
        if (
            self.last_success_at is not None
            and self.last_success_at > self.last_attempt_at
        ):
            raise ValueError("last_success_at must not be later than last_attempt_at")
        if self.status is SourceSyncStatus.ACTIVE and (
            self.last_success_at is None
            or self.source_version is None
            or self.content_hash is None
        ):
            raise ValueError(
                "active source state requires last_success_at, source_version, and "
                "content_hash"
            )
        if self.status in {SourceSyncStatus.FAILED, SourceSyncStatus.STALE} and (
            self.last_error_type is None
        ):
            raise ValueError("failed and stale source states require last_error_type")
        if self.status in {SourceSyncStatus.DELETED, SourceSyncStatus.REVOKED} and (
            self.content_hash is not None
        ):
            raise ValueError("deleted and revoked source states forbid content_hash")
        return self


class SyncAudit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sync_id: str = Field(min_length=1, max_length=128)
    project_id: str = Field(min_length=1, max_length=128)
    started_at: datetime
    finished_at: datetime
    outcome: ProjectSyncOutcome
    source_counts_by_status: dict[SourceSyncStatus, int]
    chunk_count: int = Field(ge=0)
    duration_ms: int = Field(ge=0)
    error_type: str | None = Field(max_length=128)

    _sync_id = field_validator("sync_id")(
        lambda value: _require_project_or_sync_id(value, "sync_id")
    )
    _project_id = field_validator("project_id")(
        lambda value: _require_project_or_sync_id(value, "project_id")
    )
    _started_at = field_validator("started_at")(
        lambda value: _require_aware(value, "started_at")
    )
    _finished_at = field_validator("finished_at")(
        lambda value: _require_aware(value, "finished_at")
    )

    @model_validator(mode="after")
    def validate_audit(self) -> "SyncAudit":
        if self.finished_at < self.started_at:
            raise ValueError("finished_at must not be earlier than started_at")
        if any(count < 0 for count in self.source_counts_by_status.values()):
            raise ValueError("source counts must not be negative")
        return self


class RetrievalSourceBaseline(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_version: str = Field(min_length=1, max_length=256)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    chunk_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class RetrievalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str = Field(min_length=1, max_length=256)
    project_id: str = Field(min_length=1, max_length=128)
    query_hash: str = Field(pattern=r"[0-9a-f]{64}")
    source_id_hashes: tuple[str, ...] = Field(min_length=1, max_length=30)
    request_epoch: int = Field(default=1, ge=1)
    baseline_generation_id: str | None = Field(default=None, max_length=128)
    baseline_content_hash: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    baseline_source_cursor: int | None = Field(default=None, ge=1)
    baseline_sources: tuple[RetrievalSourceBaseline, ...] = Field(
        default=(),
        max_length=30,
    )
    status: RetrievalRequestStatus
    created_at: datetime
    expires_at: datetime
    lease_expires_at: datetime | None = None
    attempt_count: int = Field(default=0, ge=0)
    completed_at: datetime | None = None

    _request_id = field_validator("request_id")(
        lambda value: _require_non_blank(value, "request_id")
    )
    _project_id = field_validator("project_id")(
        lambda value: _require_project_or_sync_id(value, "project_id")
    )
    _query_hash = field_validator("query_hash")(
        lambda value: _require_sha256(value, "query_hash")
    )
    _baseline_generation_id = field_validator("baseline_generation_id")(
        lambda value: _require_non_blank(value, "baseline_generation_id")
        if value is not None
        else value
    )
    _created_at = field_validator("created_at")(
        lambda value: _require_aware(value, "created_at")
    )
    _expires_at = field_validator("expires_at")(
        lambda value: _require_aware(value, "expires_at")
    )
    _lease_expires_at = field_validator("lease_expires_at")(
        lambda value: _require_aware(value, "lease_expires_at")
        if value is not None
        else value
    )
    _completed_at = field_validator("completed_at")(
        lambda value: _require_aware(value, "completed_at")
        if value is not None
        else value
    )

    @field_validator("source_id_hashes")
    @classmethod
    def validate_source_id_hashes(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            _require_sha256(value, "source_id_hash")
        return values

    @model_validator(mode="after")
    def validate_request_lifecycle(self) -> "RetrievalRequest":
        if self.expires_at <= self.created_at:
            raise ValueError("expires_at must be later than created_at")
        if len(set(self.source_id_hashes)) != len(self.source_id_hashes):
            raise ValueError("source_id_hashes must be unique")
        baseline = (
            self.baseline_generation_id,
            self.baseline_content_hash,
            self.baseline_source_cursor,
        )
        has_baseline = any(item is not None for item in baseline)
        if has_baseline and (
            not all(item is not None for item in baseline)
            or not self.baseline_sources
        ):
            raise ValueError("retrieval baseline fields must be supplied together")
        if self.baseline_sources and not has_baseline:
            raise ValueError("retrieval baseline fields must be supplied together")
        baseline_hashes = [item.source_id_hash for item in self.baseline_sources]
        if (
            len(baseline_hashes) != len(set(baseline_hashes))
            or (
                self.baseline_sources
                and set(baseline_hashes) != set(self.source_id_hashes)
            )
        ):
            raise ValueError("retrieval baselines must match requested sources")
        if self.status is RetrievalRequestStatus.COMPLETED:
            if self.completed_at is None:
                raise ValueError("completed request requires completed_at")
        elif self.completed_at is not None:
            raise ValueError("non-completed request forbids completed_at")
        if self.status is RetrievalRequestStatus.IN_PROGRESS:
            if self.lease_expires_at is None or self.attempt_count < 1:
                raise ValueError("in-progress request requires an active lease")
            if self.lease_expires_at > self.expires_at:
                raise ValueError("retrieval lease cannot exceed request expiry")
        elif self.lease_expires_at is not None:
            raise ValueError("non-active request forbids a retrieval lease")
        return self


class ClaimedRetrievalRequest(RetrievalRequest):
    lease_token: str = Field(
        pattern=r"^[A-Za-z0-9_-]{32,128}$",
    )


class RetrievalCompletionClaim(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str = Field(min_length=1, max_length=256)
    request_epoch: int = Field(ge=1)
    attempt_count: int = Field(ge=1)
    lease_token: str = Field(pattern=r"^[A-Za-z0-9_-]{32,128}$")

    _request_id = field_validator("request_id")(
        lambda value: _require_non_blank(value, "request_id")
    )


class ProjectSyncStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str = Field(min_length=1, max_length=128)
    health: ProjectSyncHealth
    sources: tuple[SourceState, ...]
    last_success_at: datetime | None
    next_sync_before: datetime | None

    _project_id = field_validator("project_id")(
        lambda value: _require_project_or_sync_id(value, "project_id")
    )
    _last_success_at = field_validator("last_success_at")(
        lambda value: _require_aware(value, "last_success_at")
        if value is not None
        else value
    )
    _next_sync_before = field_validator("next_sync_before")(
        lambda value: _require_aware(value, "next_sync_before")
        if value is not None
        else value
    )


class SyncResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sync_id: str = Field(min_length=1, max_length=128)
    outcome: Literal[
        ProjectSyncOutcome.APPLIED,
        ProjectSyncOutcome.UNCHANGED,
        ProjectSyncOutcome.DEGRADED,
    ]
    project_status: ProjectSyncHealth
    accepted_sources: int = Field(ge=0)
    failed_sources: int = Field(ge=0)
    generation_id: str | None = Field(max_length=128)
    next_sync_before: datetime

    _sync_id = field_validator("sync_id")(
        lambda value: _require_project_or_sync_id(value, "sync_id")
    )
    _generation_id = field_validator("generation_id")(
        lambda value: _require_non_blank(value, "generation_id")
        if value is not None
        else value
    )
    _next_sync_before = field_validator("next_sync_before")(
        lambda value: _require_aware(value, "next_sync_before")
    )


class SyncEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    sync_id: str
    project_id: str
    generated_at: datetime
    source_cursor: int = Field(ge=1)
    content_hash: str = Field(pattern=r"[0-9a-f]{64}")
    producer: Literal["qwenwork-dws"]
    context: ProjectContextPackage
    sources: tuple[SourceSnapshot, ...] = Field(max_length=30)
    tombstones: tuple[SourceTombstone, ...] = ()
    completed_retrieval_request_ids: tuple[str, ...] = ()
    completed_retrieval_claims: tuple[RetrievalCompletionClaim, ...] = ()

    _sync_id = field_validator("sync_id")(
        lambda value: _require_project_or_sync_id(value, "sync_id")
    )
    _project_id = field_validator("project_id")(
        lambda value: _require_project_or_sync_id(value, "project_id")
    )
    _generated_at = field_validator("generated_at")(
        lambda value: _require_aware(value, "generated_at")
    )
    _content_hash = field_validator("content_hash")(
        lambda value: _require_sha256(value, "content_hash")
    )

    @field_validator("completed_retrieval_request_ids")
    @classmethod
    def validate_completed_request_ids(
        cls, values: tuple[str, ...]
    ) -> tuple[str, ...]:
        for value in values:
            _require_non_blank(value, "completed_retrieval_request_id")
            if len(value) > 256:
                raise ValueError(
                    "completed_retrieval_request_id must be at most 256 characters"
                )
        return values

    @model_validator(mode="after")
    def validate_envelope(self) -> "SyncEnvelope":
        if self.context.project_id != self.project_id:
            raise ValueError("context project_id must equal envelope project_id")
        expected_scope = self.context.permission_scope
        if any(source.permission_scope != expected_scope for source in self.sources):
            raise ValueError("all source permission scopes must equal context scope")
        if any(
            tombstone.permission_scope != expected_scope
            for tombstone in self.tombstones
        ):
            raise ValueError("all tombstone permission scopes must equal context scope")

        source_identities = {
            (source.source_type, source.source_id) for source in self.sources
        }
        if len(source_identities) != len(self.sources):
            raise ValueError("source identities must be unique")
        tombstone_identities = {
            (tombstone.source_type, tombstone.source_id)
            for tombstone in self.tombstones
        }
        if len(tombstone_identities) != len(self.tombstones):
            raise ValueError("tombstone identities must be unique")
        if source_identities & tombstone_identities:
            raise ValueError("source and tombstone identities must be disjoint")
        if (
            len(set(self.completed_retrieval_request_ids))
            != len(self.completed_retrieval_request_ids)
        ):
            raise ValueError("completed retrieval request IDs must be unique")
        claim_ids = [item.request_id for item in self.completed_retrieval_claims]
        if len(claim_ids) != len(set(claim_ids)):
            raise ValueError("completed retrieval claims must be unique")
        return self
