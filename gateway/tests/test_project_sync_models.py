from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from companion_gateway.project import (
    EvidenceChunk,
    ProjectSyncHealth,
    ProjectSyncOutcome,
    ProjectSyncStatus,
    RetrievalRequest,
    RetrievalRequestStatus,
    SourceErrorType,
    SourceSnapshot,
    SourceState,
    SourceSyncStatus,
    SourceTombstone,
    SyncAudit,
    SyncEnvelope,
    SyncResult,
    SyncSourceType,
)
from companion_gateway.project.models import ProjectContextPackage


NOW = datetime(2026, 9, 5, 8, 0, tzinfo=UTC)
LATER = NOW + timedelta(seconds=1)
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
HASH_D = "d" * 64


def context(**updates: object) -> ProjectContextPackage:
    values: dict[str, object] = {
        "project_id": "project-demo",
        "project_name": "会锚项目",
        "generated_at": NOW,
        "permission_scope": "project:demo",
    }
    values.update(updates)
    return ProjectContextPackage(**values)


def chunk(**updates: object) -> EvidenceChunk:
    values: dict[str, object] = {
        "chunk_id": HASH_A,
        "source_id": "doc-001",
        "source_version": "3",
        "ordinal": 0,
        "heading_path": ("概览",),
        "text": "项目资料正文",
        "start_offset": 0,
        "end_offset": 6,
        "content_hash": HASH_B,
    }
    values.update(updates)
    return EvidenceChunk(**values)


def active_source(index: int = 0, **updates: object) -> SourceSnapshot:
    source_id = updates.get("source_id", f"doc-{index:03d}")
    source_version = updates.get("source_version", "3")
    default_chunks = (
        updates["chunks"]
        if "chunks" in updates
        else (
            chunk(
                source_id=source_id,
                source_version=source_version,
                chunk_id=HASH_C,
            ),
        )
    )
    values: dict[str, object] = {
        "source_type": "document",
        "source_id": source_id,
        "source_title": "测试文档",
        "source_url": f"dingtalk://doc/{source_id}",
        "source_version": source_version,
        "source_time": NOW,
        "fetched_at": NOW,
        "permission_scope": "project:demo",
        "permission_hash": HASH_A,
        "status": "active",
        "chunks": default_chunks,
        "content_hash": HASH_B,
    }
    values.update(updates)
    return SourceSnapshot(**values)


def tombstone(**updates: object) -> SourceTombstone:
    values: dict[str, object] = {
        "source_type": "document",
        "source_id": "deleted-001",
        "status": "deleted",
        "occurred_at": NOW,
        "permission_scope": "project:demo",
    }
    values.update(updates)
    return SourceTombstone(**values)


def source_state(**updates: object) -> SourceState:
    values: dict[str, object] = {
        "project_id": "project-demo",
        "source_type": "document",
        "source_id_hash": HASH_A,
        "source_version": "3",
        "content_hash": HASH_B,
        "permission_hash": HASH_C,
        "status": "active",
        "last_attempt_at": NOW,
        "last_success_at": NOW,
        "last_error_type": None,
    }
    values.update(updates)
    return SourceState(**values)


def audit(**updates: object) -> SyncAudit:
    values: dict[str, object] = {
        "sync_id": "sync-001",
        "project_id": "project-demo",
        "started_at": NOW,
        "finished_at": LATER,
        "outcome": "applied",
        "source_counts_by_status": {SourceSyncStatus.ACTIVE: 1},
        "chunk_count": 1,
        "duration_ms": 1_000,
        "error_type": None,
    }
    values.update(updates)
    return SyncAudit(**values)


def retrieval_request(**updates: object) -> RetrievalRequest:
    values: dict[str, object] = {
        "request_id": "request-001",
        "project_id": "project-demo",
        "query_hash": HASH_A,
        "source_id_hashes": (HASH_B,),
        "status": "pending",
        "created_at": NOW,
        "expires_at": LATER,
        "completed_at": None,
    }
    values.update(updates)
    return RetrievalRequest(**values)


def envelope(**updates: object) -> SyncEnvelope:
    values: dict[str, object] = {
        "schema_version": 1,
        "sync_id": "sync-001",
        "project_id": "project-demo",
        "generated_at": NOW,
        "source_cursor": 1,
        "content_hash": HASH_D,
        "producer": "qwenwork-dws",
        "context": context(),
        "sources": (active_source(),),
    }
    values.update(updates)
    return SyncEnvelope(**values)


def sync_status(**updates: object) -> ProjectSyncStatus:
    values: dict[str, object] = {
        "project_id": "project-demo",
        "health": "healthy",
        "sources": (source_state(),),
        "last_success_at": NOW,
        "next_sync_before": LATER,
    }
    values.update(updates)
    return ProjectSyncStatus(**values)


def sync_result(**updates: object) -> SyncResult:
    values: dict[str, object] = {
        "sync_id": "sync-001",
        "outcome": "applied",
        "project_status": "healthy",
        "accepted_sources": 1,
        "failed_sources": 0,
        "generation_id": "generation-001",
        "next_sync_before": LATER,
    }
    values.update(updates)
    return SyncResult(**values)


def test_sync_enums_are_closed_contracts() -> None:
    assert set(SyncSourceType) == {
        SyncSourceType.DOCUMENT,
        SyncSourceType.MEETING_NOTE,
        SyncSourceType.TASK,
        SyncSourceType.CALENDAR,
    }
    assert set(SourceSyncStatus) == {
        SourceSyncStatus.ACTIVE,
        SourceSyncStatus.STALE,
        SourceSyncStatus.DELETED,
        SourceSyncStatus.REVOKED,
        SourceSyncStatus.FAILED,
    }
    assert set(SourceErrorType) == {
        SourceErrorType.NETWORK_TIMEOUT,
        SourceErrorType.PERMISSION_DENIED,
        SourceErrorType.NODE_NOT_FOUND,
        SourceErrorType.INVALID_PAYLOAD,
        SourceErrorType.AUTHENTICATION_FAILED,
        SourceErrorType.RATE_LIMITED,
        SourceErrorType.PROVIDER_UNAVAILABLE,
        SourceErrorType.UNKNOWN,
    }
    assert set(ProjectSyncOutcome) == {
        ProjectSyncOutcome.APPLIED,
        ProjectSyncOutcome.UNCHANGED,
        ProjectSyncOutcome.DEGRADED,
        ProjectSyncOutcome.REJECTED,
        ProjectSyncOutcome.FAILED,
    }
    assert set(ProjectSyncHealth) == {
        ProjectSyncHealth.HEALTHY,
        ProjectSyncHealth.DEGRADED,
        ProjectSyncHealth.STALE,
        ProjectSyncHealth.CLOCK_UNTRUSTED,
    }
    assert set(RetrievalRequestStatus) == {
        RetrievalRequestStatus.PENDING,
        RetrievalRequestStatus.IN_PROGRESS,
        RetrievalRequestStatus.COMPLETED,
        RetrievalRequestStatus.EXPIRED,
    }


def test_evidence_chunk_is_immutable_and_rejects_invalid_offsets() -> None:
    item = chunk()

    with pytest.raises(ValidationError):
        item.text = "不可修改"
    with pytest.raises(ValueError, match="end_offset"):
        chunk(start_offset=6, end_offset=6)


def test_active_document_and_meeting_note_require_chunks() -> None:
    for source_type in ("document", "meeting_note"):
        with pytest.raises(ValueError, match="chunk"):
            active_source(source_type=source_type, chunks=())


def test_active_task_and_calendar_allow_empty_valid_records() -> None:
    for source_type in ("task", "calendar"):
        item = active_source(source_type=source_type, chunks=())

        assert item.chunks == ()


@pytest.mark.parametrize("field", ["source_version", "source_time", "content_hash"])
def test_active_source_requires_version_time_and_content_hash(field: str) -> None:
    with pytest.raises(ValueError):
        active_source(source_type="task", chunks=(), **{field: None})


def test_failed_source_requires_error_and_forbids_chunks() -> None:
    with pytest.raises(ValueError):
        SourceSnapshot(
            source_type="document",
            source_id="doc-001",
            source_title="测试文档",
            source_url="dingtalk://doc/doc-001",
            source_version="3",
            source_time=NOW,
            fetched_at=NOW,
            permission_scope="project:demo",
            permission_hash="a" * 64,
            status="failed",
            chunks=(chunk(),),
        )

    with pytest.raises(ValueError, match="error"):
        active_source(
            status="failed",
            chunks=(),
            content_hash=None,
            error_type=None,
            retryable=None,
        )


def test_failed_and_stale_sources_restrict_retry_after() -> None:
    for status in ("failed", "stale"):
        values: dict[str, object] = {
            "status": status,
            "chunks": (),
            "error_type": "network_timeout",
            "retryable": False,
            "retry_after_seconds": 0,
        }
        if status == "failed":
            values["content_hash"] = None
        with pytest.raises(ValueError, match="retry"):
            active_source(**values)

    item = active_source(
        status="failed",
        chunks=(),
        content_hash=None,
        error_type="network_timeout",
        retryable=True,
        retry_after_seconds=0,
    )

    assert item.retry_after_seconds == 0


@pytest.mark.parametrize(
    "field",
    ["source_version", "content_hash", "error_type", "retryable"],
)
def test_stale_source_requires_version_content_and_error_fields(field: str) -> None:
    with pytest.raises(ValueError):
        active_source(status="stale", chunks=(), **{field: None})


@pytest.mark.parametrize("status", ["deleted", "revoked"])
def test_deleted_and_revoked_sources_forbid_content_and_error_fields(
    status: str,
) -> None:
    item = active_source(status=status, chunks=(), content_hash=None)

    assert item.status == status

    with pytest.raises(ValueError):
        active_source(status=status, chunks=(), content_hash=HASH_B)


def test_source_snapshot_chunks_must_match_snapshot_identity() -> None:
    with pytest.raises(ValueError, match="source"):
        active_source(chunks=(chunk(source_id="other-doc"),))
    with pytest.raises(ValueError, match="version"):
        active_source(chunks=(chunk(source_id="doc-000", source_version="4"),))


def test_source_tombstone_accepts_only_terminal_source_statuses() -> None:
    assert tombstone().status is SourceSyncStatus.DELETED

    with pytest.raises(ValueError):
        tombstone(status="active")
    with pytest.raises(ValueError, match="timezone"):
        tombstone(occurred_at=NOW.replace(tzinfo=None))


def test_source_state_enforces_lifecycle_fields() -> None:
    with pytest.raises(ValueError, match="last_success"):
        source_state(last_success_at=LATER)
    with pytest.raises(ValueError, match="active"):
        source_state(content_hash=None)
    with pytest.raises(ValueError, match="last_error"):
        source_state(status="failed", last_error_type=None)
    with pytest.raises(ValueError, match="content"):
        source_state(status="deleted", content_hash=HASH_B)


def test_sync_audit_requires_ordered_times_and_non_negative_counts() -> None:
    assert audit().outcome is ProjectSyncOutcome.APPLIED

    with pytest.raises(ValueError, match="finished"):
        audit(finished_at=NOW - timedelta(seconds=1))
    with pytest.raises(ValueError, match="counts"):
        audit(source_counts_by_status={SourceSyncStatus.FAILED: -1})


def test_retrieval_request_requires_unique_sources_and_completed_timestamp() -> None:
    with pytest.raises(ValueError, match="source"):
        retrieval_request(source_id_hashes=(HASH_A, HASH_A))
    with pytest.raises(ValueError, match="expires"):
        retrieval_request(expires_at=NOW)
    with pytest.raises(ValueError, match="completed"):
        retrieval_request(status="completed")
    with pytest.raises(ValueError, match="completed"):
        retrieval_request(completed_at=NOW)

    completed = retrieval_request(status="completed", completed_at=LATER)

    assert completed.completed_at == LATER


def test_project_sync_status_requires_aware_status_timestamps() -> None:
    assert sync_status().health is ProjectSyncHealth.HEALTHY

    with pytest.raises(ValueError, match="timezone"):
        sync_status(last_success_at=NOW.replace(tzinfo=None))


def test_sync_result_accepts_only_returnable_outcomes() -> None:
    assert sync_result().outcome == "applied"

    with pytest.raises(ValueError):
        sync_result(outcome="rejected")
    with pytest.raises(ValueError, match="generation"):
        sync_result(generation_id="")


def test_sync_envelope_rejects_more_than_thirty_sources() -> None:
    with pytest.raises(ValueError, match="at most 30"):
        envelope(sources=tuple(active_source(i) for i in range(31)))


def test_sync_envelope_enforces_project_scope_and_unique_identities() -> None:
    with pytest.raises(ValueError, match="project"):
        envelope(context=context(project_id="project-other"))
    with pytest.raises(ValueError, match="permission"):
        envelope(sources=(active_source(permission_scope="project:other"),))
    with pytest.raises(ValueError, match="unique"):
        envelope(sources=(active_source(), active_source(1, source_id="doc-000")))
    with pytest.raises(ValueError, match="disjoint"):
        envelope(tombstones=(tombstone(source_id="doc-000"),))
    with pytest.raises(ValueError, match="unique"):
        envelope(completed_retrieval_request_ids=("request-001", "request-001"))
