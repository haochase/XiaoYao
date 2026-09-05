import hashlib
import json
import sqlite3
import threading
from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from companion_gateway.project.models import (
    DecisionCard,
    EvidenceRef,
    ProjectContextPackage,
)
from companion_gateway.project.sync_models import (
    EvidenceChunk,
    RetrievalRequest,
    RetrievalRequestStatus,
    SourceErrorType,
    SourceSnapshot,
    SourceState,
    SourceSyncStatus,
    SourceTombstone,
    SyncAudit,
    SyncEnvelope,
    SyncSourceType,
)
from companion_gateway.project.sync_repository import (
    ProtectedChunkRecord,
    ProtectedSourceRecord,
    ProjectSyncRepository,
    SyncCommit,
    SyncConflict,
)


NOW = datetime(2026, 9, 5, 8, 0, tzinfo=UTC)
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
HASH_D = "d" * 64
HASH_E = "e" * 64
HASH_F = "f" * 64


def source_id_hash(source_id: str) -> str:
    return hashlib.sha256(source_id.encode()).hexdigest()


def _datetime_text_for_test(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def evidence_ref(**updates: object) -> EvidenceRef:
    values: dict[str, object] = {
        "source_type": SyncSourceType.DOCUMENT,
        "source_id": HASH_A,
        "source_title": "受控来源",
        "source_url": "local://protected-source",
        "source_time": NOW,
        "excerpt": "受控摘要",
        "permission_scope": "project:demo",
    }
    values.update(updates)
    return EvidenceRef(**values)


def context(**updates: object) -> ProjectContextPackage:
    reference = evidence_ref()
    decision = DecisionCard(
        decision_id="decision-1",
        project_id="project-1",
        topic="发布方案",
        decision_text="采用方案 B",
        rationale="风险更低",
        owner="owner-1",
        decided_at=NOW,
        source_refs=(reference,),
        status="active",
        confidence=0.9,
    )
    values: dict[str, object] = {
        "project_id": "project-1",
        "project_name": "会锚项目",
        "generated_at": NOW,
        "source_refs": (reference,),
        "active_decisions": (decision,),
        "permission_scope": "project:demo",
    }
    values.update(updates)
    return ProjectContextPackage(**values)


def chunk(**updates: object) -> EvidenceChunk:
    values: dict[str, object] = {
        "chunk_id": HASH_C,
        "source_id": "real-source-id",
        "source_version": "v1",
        "ordinal": 0,
        "heading_path": ("私密章节",),
        "text": "绝不能明文落盘的正文",
        "start_offset": 0,
        "end_offset": 11,
        "content_hash": HASH_B,
    }
    values.update(updates)
    return EvidenceChunk(**values)


def active_snapshot(**updates: object) -> SourceSnapshot:
    values: dict[str, object] = {
        "source_type": SyncSourceType.DOCUMENT,
        "source_id": "real-source-id",
        "source_title": "真实项目标题",
        "source_url": "https://example.invalid/private-document",
        "source_version": "v1",
        "source_time": NOW,
        "fetched_at": NOW,
        "permission_scope": "project:demo",
        "permission_hash": HASH_A,
        "status": "active",
        "chunks": (chunk(),),
        "content_hash": HASH_B,
    }
    values.update(updates)
    return SourceSnapshot(**values)


def source_state(**updates: object) -> SourceState:
    values: dict[str, object] = {
        "project_id": "project-1",
        "source_type": "document",
        "source_id_hash": source_id_hash("real-source-id"),
        "source_version": "v1",
        "content_hash": HASH_B,
        "permission_hash": HASH_A,
        "status": "active",
        "last_attempt_at": NOW,
        "last_success_at": NOW,
        "last_error_type": None,
    }
    values.update(updates)
    return SourceState(**values)


def protected_source(**updates: object) -> ProtectedSourceRecord:
    values: dict[str, object] = {
        "source_type": SyncSourceType.DOCUMENT,
        "source_id_hash": source_id_hash("real-source-id"),
        "protected_source_id": b"cipher-source-id",
        "protected_title": b"cipher-title",
        "protected_url": b"cipher-url",
        "source_version": "v1",
        "source_time": NOW,
        "permission_hash": HASH_A,
        "content_hash": HASH_B,
    }
    values.update(updates)
    return ProtectedSourceRecord(**values)


def protected_chunk(**updates: object) -> ProtectedChunkRecord:
    values: dict[str, object] = {
        "chunk_id": HASH_C,
        "source_type": SyncSourceType.DOCUMENT,
        "source_id_hash": source_id_hash("real-source-id"),
        "source_version": "v1",
        "ordinal": 0,
        "protected_heading_path": b"cipher-heading",
        "protected_text": b"cipher-text",
        "start_offset": 0,
        "end_offset": 11,
        "content_hash": HASH_B,
    }
    values.update(updates)
    return ProtectedChunkRecord(**values)


def active_document_records(
    *,
    source_id: str,
    source_version: str,
    source_content_hash: str,
    chunk_id: str,
    chunk_content_hash: str,
    observed_at: datetime,
) -> tuple[
    SourceSnapshot,
    SourceState,
    ProtectedSourceRecord,
    ProtectedChunkRecord,
]:
    item = chunk(
        chunk_id=chunk_id,
        source_id=source_id,
        source_version=source_version,
        content_hash=chunk_content_hash,
    )
    snapshot = active_snapshot(
        source_id=source_id,
        source_title="另一个真实标题",
        source_url="https://example.invalid/another-private-document",
        source_version=source_version,
        source_time=observed_at,
        fetched_at=observed_at,
        chunks=(item,),
        content_hash=source_content_hash,
    )
    state = source_state(
        source_id_hash=source_id_hash(source_id),
        source_version=source_version,
        content_hash=source_content_hash,
        last_attempt_at=observed_at,
        last_success_at=observed_at,
    )
    source_record = protected_source(
        source_id_hash=source_id_hash(source_id),
        protected_source_id=f"cipher-id-{source_version}".encode(),
        protected_title=f"cipher-title-{source_version}".encode(),
        protected_url=f"cipher-url-{source_version}".encode(),
        source_version=source_version,
        source_time=observed_at,
        content_hash=source_content_hash,
    )
    chunk_record = protected_chunk(
        chunk_id=chunk_id,
        source_id_hash=source_id_hash(source_id),
        source_version=source_version,
        protected_heading_path=f"cipher-heading-{source_version}".encode(),
        protected_text=f"cipher-text-{source_version}".encode(),
        content_hash=chunk_content_hash,
    )
    return snapshot, state, source_record, chunk_record


def sync_commit(
    *,
    cursor: int,
    content_hash: str = HASH_A,
    sync_id: str | None = None,
    package: ProjectContextPackage | None = None,
    snapshots: tuple[SourceSnapshot, ...] | None = None,
    tombstones: tuple[SourceTombstone, ...] = (),
    states: tuple[SourceState, ...] | None = None,
    protected_sources: tuple[ProtectedSourceRecord, ...] | None = None,
    protected_chunks: tuple[ProtectedChunkRecord, ...] | None = None,
    completed_request_ids: tuple[str, ...] = (),
    outcome: str | None = None,
) -> SyncCommit:
    chosen_snapshots = snapshots if snapshots is not None else (active_snapshot(),)
    chosen_states = states if states is not None else (source_state(),)
    identifier = sync_id or f"sync-{cursor}"
    envelope = SyncEnvelope(
        schema_version=1,
        sync_id=identifier,
        project_id="project-1",
        generated_at=NOW + timedelta(minutes=cursor - 1),
        source_cursor=cursor,
        content_hash=content_hash,
        producer="qwenwork-dws",
        context=package or context(generated_at=NOW + timedelta(minutes=cursor - 1)),
        sources=chosen_snapshots,
        tombstones=tombstones,
        completed_retrieval_request_ids=completed_request_ids,
    )
    failed_count = sum(
        item.status in {SourceSyncStatus.FAILED, SourceSyncStatus.STALE}
        for item in chosen_states
    )
    chosen_outcome = outcome or ("degraded" if failed_count else "applied")
    audit = SyncAudit(
        sync_id=identifier,
        project_id="project-1",
        started_at=envelope.generated_at,
        finished_at=envelope.generated_at + timedelta(seconds=1),
        outcome=chosen_outcome,
        source_counts_by_status=dict(Counter(item.status for item in chosen_states)),
        chunk_count=sum(len(item.chunks) for item in chosen_snapshots),
        duration_ms=1_000,
        error_type=None,
    )
    return SyncCommit(
        envelope=envelope,
        generation_id=f"generation-{cursor}",
        source_states=chosen_states,
        protected_sources=(
            protected_sources
            if protected_sources is not None
            else (protected_source(),)
        ),
        protected_chunks=(
            protected_chunks
            if protected_chunks is not None
            else (protected_chunk(),)
        ),
        audit=audit,
    )


def repository_at(tmp_path: Path) -> ProjectSyncRepository:
    return ProjectSyncRepository(tmp_path / "project-memory.db")


def test_initialize_upgrades_clock_state_before_using_high_watermark(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "project-memory.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE project_sync_clock_state (
                singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
                trusted_wall_at TEXT,
                clock_untrusted INTEGER NOT NULL,
                needs_sync INTEGER NOT NULL,
                reason TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO project_sync_clock_state(
                singleton_id, trusted_wall_at, clock_untrusted,
                needs_sync, reason
            ) VALUES (1, '2026-09-05T08:00:00Z', 0, 1, 'resume_detected')
            """
        )

    repository = repository_at(tmp_path)
    repository.initialize()

    state = repository.load_clock_state()
    assert state.trusted_wall_at == NOW
    assert state.last_observed_wall_at is None
    assert state.needs_sync
    assert state.reason == "resume_detected"


def test_initialize_migrates_retrievals_without_per_source_baselines(
    tmp_path: Path,
) -> None:
    repository = repository_at(tmp_path)
    repository.initialize()
    database_path = tmp_path / "project-memory.db"
    with sqlite3.connect(database_path) as connection:
        values = (
            "project-1",
            HASH_A,
            json.dumps([source_id_hash("real-source-id")]),
            "generation-1",
            HASH_B,
            1,
            _datetime_text_for_test(NOW),
            _datetime_text_for_test(NOW + timedelta(hours=1)),
        )
        connection.execute(
            """
            INSERT INTO project_retrieval_requests(
                request_id, project_id, query_hash, source_id_hashes_json,
                baseline_generation_id, baseline_content_hash,
                baseline_source_cursor, baseline_sources_json, status,
                created_at, expires_at, lease_expires_at, attempt_count,
                completed_at
            ) VALUES ('legacy-pending', ?, ?, ?, ?, ?, ?, NULL, 'in_progress',
                      ?, ?, ?, 1, NULL)
            """,
            (*values, _datetime_text_for_test(NOW + timedelta(minutes=5))),
        )
        connection.execute(
            """
            INSERT INTO project_retrieval_requests(
                request_id, project_id, query_hash, source_id_hashes_json,
                baseline_generation_id, baseline_content_hash,
                baseline_source_cursor, baseline_sources_json, status,
                created_at, expires_at, lease_expires_at, attempt_count,
                completed_at
            ) VALUES ('legacy-completed', ?, ?, ?, ?, ?, ?, NULL, 'completed',
                      ?, ?, NULL, 1, ?)
            """,
            (*values, _datetime_text_for_test(NOW + timedelta(minutes=1))),
        )

    repository.initialize()

    pending = repository.get_retrieval_request("project-1", "legacy-pending")
    completed = repository.get_retrieval_request(
        "project-1", "legacy-completed"
    )
    assert pending is not None
    assert pending.status is RetrievalRequestStatus.EXPIRED
    assert pending.baseline_generation_id is None
    assert pending.baseline_sources == ()
    assert pending.lease_expires_at is None
    assert completed is not None
    assert completed.status is RetrievalRequestStatus.COMPLETED
    assert completed.baseline_generation_id is None
    assert completed.baseline_sources == ()


def test_protection_descriptor_persists_and_rejects_identity_or_version_change(
    tmp_path: Path,
) -> None:
    identity = "a" * 64
    repository = repository_at(tmp_path)
    repository.initialize()
    repository.configure_protection(identity, "test-protector-v1")

    reopened = repository_at(tmp_path)
    reopened.initialize()
    reopened.configure_protection(identity, "test-protector-v1")
    assert reopened.protection_descriptor() == (
        identity,
        "test-protector-v1",
    )

    with pytest.raises(SyncConflict, match="protection_identity_mismatch"):
        reopened.configure_protection("b" * 64, "test-protector-v1")
    with pytest.raises(SyncConflict, match="protection_version_mismatch"):
        reopened.configure_protection(identity, "test-protector-v2")


def test_active_ciphertext_without_protection_descriptor_fails_closed(
    tmp_path: Path,
) -> None:
    repository = repository_at(tmp_path)
    repository.initialize()
    repository.commit(sync_commit(cursor=1))

    reopened = repository_at(tmp_path)
    reopened.initialize()
    with pytest.raises(SyncConflict, match="protection_metadata_missing"):
        reopened.configure_protection("a" * 64, "test-protector-v1")


def test_commit_promotes_one_generation_atomically(tmp_path: Path) -> None:
    repository = repository_at(tmp_path)
    repository.initialize()

    result = repository.commit(sync_commit(cursor=1))

    stored = repository.load_active_generation("project-1")
    assert result.outcome == "applied"
    assert stored is not None
    assert stored.source_cursor == 1
    assert stored.context == context()
    assert stored.protected_sources == (protected_source(),)
    assert stored.protected_chunks == (protected_chunk(),)

    expected_context_json = json.dumps(
        context().model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    with sqlite3.connect(tmp_path / "project-memory.db") as connection:
        assert connection.execute(
            "SELECT context_json, created_at FROM project_sync_generations"
        ).fetchone() == (expected_context_json, "2026-09-05T08:00:01Z")


def test_commit_rejects_inconsistent_protected_record_versions(
    tmp_path: Path,
) -> None:
    repository = repository_at(tmp_path)
    repository.initialize()

    with pytest.raises(ValueError, match="protected_source_mismatch"):
        repository.commit(
            sync_commit(
                cursor=1,
                protected_sources=(protected_source(source_version="v2"),),
            )
        )

    assert repository.load_active_generation("project-1") is None


def test_commit_rejects_missing_protected_chunks(tmp_path: Path) -> None:
    repository = repository_at(tmp_path)
    repository.initialize()

    with pytest.raises(ValueError, match="protected_chunk_missing"):
        repository.commit(sync_commit(cursor=1, protected_chunks=()))

    assert repository.load_active_generation("project-1") is None


def test_same_cursor_with_different_hash_is_rejected(tmp_path: Path) -> None:
    repository = repository_at(tmp_path)
    repository.initialize()
    repository.commit(sync_commit(cursor=1, content_hash=HASH_A))

    with pytest.raises(SyncConflict, match="cursor_content_conflict"):
        repository.commit(
            sync_commit(cursor=1, content_hash=HASH_B, sync_id="sync-conflict")
        )


def test_same_cursor_and_hash_is_idempotent(tmp_path: Path) -> None:
    repository = repository_at(tmp_path)
    repository.initialize()
    first = repository.commit(sync_commit(cursor=1))

    retried = repository.commit(sync_commit(cursor=1))

    assert retried.outcome == "applied"
    assert retried.generation_id == first.generation_id


def test_higher_cursor_rejects_source_version_and_time_rollback(
    tmp_path: Path,
) -> None:
    repository = repository_at(tmp_path)
    repository.initialize()
    current = active_document_records(
        source_id="real-source-id",
        source_version="v2",
        source_content_hash=HASH_D,
        chunk_id=HASH_E,
        chunk_content_hash=HASH_F,
        observed_at=NOW + timedelta(minutes=1),
    )
    repository.commit(
        sync_commit(
            cursor=1,
            snapshots=(current[0],),
            states=(current[1],),
            protected_sources=(current[2],),
            protected_chunks=(current[3],),
        )
    )
    stale = active_document_records(
        source_id="real-source-id",
        source_version="v1",
        source_content_hash=HASH_B,
        chunk_id=HASH_C,
        chunk_content_hash=HASH_B,
        observed_at=NOW,
    )

    with pytest.raises(SyncConflict, match="source_version_rollback"):
        repository.commit(
            sync_commit(
                cursor=2,
                content_hash=HASH_B,
                snapshots=(stale[0],),
                states=(stale[1],),
                protected_sources=(stale[2],),
                protected_chunks=(stale[3],),
            )
        )


def test_higher_cursor_rejects_same_source_version_with_new_content(
    tmp_path: Path,
) -> None:
    repository = repository_at(tmp_path)
    repository.initialize()
    repository.commit(sync_commit(cursor=1, content_hash=HASH_A))
    conflicting = active_document_records(
        source_id="real-source-id",
        source_version="v1",
        source_content_hash=HASH_D,
        chunk_id=HASH_E,
        chunk_content_hash=HASH_F,
        observed_at=NOW + timedelta(minutes=1),
    )

    with pytest.raises(SyncConflict, match="source_version_conflict"):
        repository.commit(
            sync_commit(
                cursor=2,
                content_hash=HASH_B,
                snapshots=(conflicting[0],),
                states=(conflicting[1],),
                protected_sources=(conflicting[2],),
                protected_chunks=(conflicting[3],),
            )
        )


@pytest.mark.parametrize(
    "package",
    [
        context(
            active_decisions=(
                context().active_decisions[0].model_copy(
                    update={"decision_text": "同游标篡改决策"}
                ),
            )
        ),
        context(
            permission_scope="project:other",
            source_refs=(),
            active_decisions=(),
        ),
    ],
)
def test_same_cursor_and_hash_still_checks_context_conflicts(
    tmp_path: Path,
    package: ProjectContextPackage,
) -> None:
    repository = repository_at(tmp_path)
    repository.initialize()
    repository.commit(sync_commit(cursor=1))
    snapshots = None
    expected = "decision_change_requires_review"
    if package.permission_scope == "project:other":
        snapshots = (active_snapshot(permission_scope="project:other"),)
        expected = "permission_conflict"

    with pytest.raises(SyncConflict, match=expected):
        repository.commit(
            sync_commit(cursor=1, package=package, snapshots=snapshots)
        )


def test_greater_cursor_with_same_hash_refreshes_without_new_generation(
    tmp_path: Path,
) -> None:
    repository = repository_at(tmp_path)
    repository.initialize()
    first = repository.commit(sync_commit(cursor=1))
    refreshed_at = NOW + timedelta(minutes=1)

    result = repository.commit(
        sync_commit(
            cursor=2,
            states=(
                source_state(
                    last_attempt_at=refreshed_at,
                    last_success_at=refreshed_at,
                ),
            ),
            outcome="unchanged",
        )
    )

    stored = repository.load_active_generation("project-1")
    assert stored is not None
    assert result.outcome == "unchanged"
    assert result.generation_id == first.generation_id
    assert stored.source_cursor == 2
    assert stored.source_states[0].last_success_at == refreshed_at
    with sqlite3.connect(tmp_path / "project-memory.db") as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM project_sync_generations"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT outcome FROM project_sync_audits WHERE sync_id = 'sync-2'"
        ).fetchone() == ("unchanged",)

    retried = repository.commit(
        sync_commit(
            cursor=2,
            states=(
                source_state(
                    last_attempt_at=refreshed_at,
                    last_success_at=refreshed_at,
                ),
            ),
            outcome="unchanged",
        )
    )
    assert retried.outcome == "unchanged"


@pytest.mark.parametrize(
    ("package", "message"),
    [
        (
            context(
                active_decisions=(
                    context().active_decisions[0].model_copy(
                        update={"decision_text": "改用方案 A"}
                    ),
                )
            ),
            "decision_change_requires_review",
        ),
        (
            context(
                permission_scope="project:other",
                source_refs=(),
                active_decisions=(),
            ),
            "permission_conflict",
        ),
    ],
)
def test_commit_rejects_silent_decision_or_permission_changes(
    tmp_path: Path,
    package: ProjectContextPackage,
    message: str,
) -> None:
    repository = repository_at(tmp_path)
    repository.initialize()
    repository.commit(sync_commit(cursor=1))

    snapshots = None
    if message == "permission_conflict":
        snapshots = (active_snapshot(permission_scope="project:other"),)
    with pytest.raises(SyncConflict, match=message):
        repository.commit(
            sync_commit(cursor=2, package=package, snapshots=snapshots)
        )


@pytest.mark.parametrize("status", ["failed", "stale"])
def test_failed_and_stale_sources_keep_old_chunks_and_last_success(
    tmp_path: Path,
    status: str,
) -> None:
    repository = repository_at(tmp_path)
    repository.initialize()
    repository.commit(sync_commit(cursor=1))
    attempt = NOW + timedelta(minutes=1)
    failed_state = source_state(
        status=status,
        last_attempt_at=attempt,
        last_success_at=attempt,
        last_error_type=SourceErrorType.NETWORK_TIMEOUT,
    )
    snapshot_updates: dict[str, object] = {
        "status": status,
        "chunks": (),
        "error_type": SourceErrorType.NETWORK_TIMEOUT,
        "retryable": True,
    }
    if status == "failed":
        snapshot_updates["content_hash"] = None
    failed_snapshot = active_snapshot(**snapshot_updates)

    repository.commit(
        sync_commit(
            cursor=2,
            content_hash=HASH_B,
            snapshots=(failed_snapshot,),
            states=(failed_state,),
            protected_sources=(),
            protected_chunks=(),
        )
    )

    stored = repository.load_active_generation("project-1")
    assert stored is not None
    assert stored.protected_chunks == (protected_chunk(),)
    assert stored.source_states[0].last_success_at == NOW
    assert stored.source_states[0].status.value == status


@pytest.mark.parametrize(
    "final_status",
    [SourceSyncStatus.FAILED, SourceSyncStatus.STALE],
)
def test_complete_failed_generation_can_reuse_content_again(
    tmp_path: Path,
    final_status: SourceSyncStatus,
) -> None:
    repository = repository_at(tmp_path)
    repository.initialize()
    other_v1 = active_document_records(
        source_id="other-source-id",
        source_version="v1",
        source_content_hash=HASH_D,
        chunk_id="1" * 64,
        chunk_content_hash="2" * 64,
        observed_at=NOW,
    )
    repository.commit(
        sync_commit(
            cursor=1,
            snapshots=(active_snapshot(), other_v1[0]),
            states=(source_state(), other_v1[1]),
            protected_sources=(protected_source(), other_v1[2]),
            protected_chunks=(protected_chunk(), other_v1[3]),
        )
    )
    failed_at = NOW + timedelta(minutes=1)
    failed_snapshot = active_snapshot(
        fetched_at=failed_at,
        status="failed",
        chunks=(),
        content_hash=None,
        error_type=SourceErrorType.NETWORK_TIMEOUT,
        retryable=True,
    )
    failed_state = source_state(
        status="failed",
        content_hash=None,
        last_attempt_at=failed_at,
        last_success_at=None,
        last_error_type=SourceErrorType.NETWORK_TIMEOUT,
    )
    other_v2 = active_document_records(
        source_id="other-source-id",
        source_version="v2",
        source_content_hash=HASH_E,
        chunk_id="3" * 64,
        chunk_content_hash="4" * 64,
        observed_at=failed_at,
    )
    repository.commit(
        sync_commit(
            cursor=2,
            content_hash=HASH_D,
            snapshots=(failed_snapshot, other_v2[0]),
            states=(failed_state, other_v2[1]),
            protected_sources=(other_v2[2],),
            protected_chunks=(other_v2[3],),
        )
    )

    final_at = NOW + timedelta(minutes=2)
    final_state_updates: dict[str, object] = {
        "status": final_status,
        "last_attempt_at": final_at,
        "last_success_at": None,
        "last_error_type": SourceErrorType.NETWORK_TIMEOUT,
    }
    final_snapshot_updates: dict[str, object] = {
        "fetched_at": final_at,
        "status": final_status,
        "chunks": (),
        "error_type": SourceErrorType.NETWORK_TIMEOUT,
        "retryable": True,
    }
    if final_status is SourceSyncStatus.FAILED:
        final_state_updates["content_hash"] = None
        final_snapshot_updates["content_hash"] = None
    final_state = source_state(**final_state_updates)
    final_snapshot = active_snapshot(**final_snapshot_updates)
    other_v3 = active_document_records(
        source_id="other-source-id",
        source_version="v3",
        source_content_hash=HASH_F,
        chunk_id="5" * 64,
        chunk_content_hash="6" * 64,
        observed_at=final_at,
    )

    result = repository.commit(
        sync_commit(
            cursor=3,
            content_hash=HASH_E,
            snapshots=(final_snapshot, other_v3[0]),
            states=(final_state, other_v3[1]),
            protected_sources=(other_v3[2],),
            protected_chunks=(other_v3[3],),
        )
    )

    stored = repository.load_active_generation("project-1")
    assert stored is not None
    inherited = next(
        item
        for item in stored.source_states
        if item.source_id_hash == source_id_hash("real-source-id")
    )
    assert result.outcome == "degraded"
    assert inherited.status is final_status
    assert inherited.source_version == "v1"
    assert inherited.content_hash == HASH_B
    assert inherited.last_success_at == NOW
    assert protected_source() in stored.protected_sources
    assert protected_chunk() in stored.protected_chunks


@pytest.mark.parametrize("status", [SourceSyncStatus.FAILED, SourceSyncStatus.STALE])
def test_snapshot_and_state_permission_hashes_must_match(
    tmp_path: Path,
    status: SourceSyncStatus,
) -> None:
    repository = repository_at(tmp_path)
    repository.initialize()
    state_updates: dict[str, object] = {
        "status": status,
        "last_error_type": SourceErrorType.PERMISSION_DENIED,
    }
    snapshot_updates: dict[str, object] = {
        "permission_hash": HASH_D,
        "status": status,
        "chunks": (),
        "error_type": SourceErrorType.PERMISSION_DENIED,
        "retryable": False,
    }
    if status is SourceSyncStatus.FAILED:
        state_updates.update({"content_hash": None, "last_success_at": None})
        snapshot_updates["content_hash"] = None

    with pytest.raises(SyncConflict, match="source_snapshot_conflict"):
        repository.commit(
            sync_commit(
                cursor=1,
                snapshots=(active_snapshot(**snapshot_updates),),
                states=(source_state(**state_updates),),
                protected_sources=(),
                protected_chunks=(),
            )
        )

    assert repository.load_active_generation("project-1") is None


@pytest.mark.parametrize(
    "state_updates",
    [
        {"source_version": "v2"},
        {"content_hash": HASH_C},
    ],
)
def test_stale_snapshot_version_and_hash_must_match_state(
    tmp_path: Path,
    state_updates: dict[str, object],
) -> None:
    repository = repository_at(tmp_path)
    repository.initialize()
    state_updates.update(
        {
            "status": SourceSyncStatus.STALE,
            "last_error_type": SourceErrorType.NETWORK_TIMEOUT,
        }
    )
    stale_snapshot = active_snapshot(
        status="stale",
        chunks=(),
        error_type=SourceErrorType.NETWORK_TIMEOUT,
        retryable=True,
    )

    with pytest.raises(SyncConflict, match="source_snapshot_conflict"):
        repository.commit(
            sync_commit(
                cursor=1,
                snapshots=(stale_snapshot,),
                states=(source_state(**state_updates),),
                protected_sources=(),
                protected_chunks=(),
            )
        )

    assert repository.load_active_generation("project-1") is None


def test_first_failed_source_is_stored_without_protected_content(
    tmp_path: Path,
) -> None:
    repository = repository_at(tmp_path)
    repository.initialize()
    failed_state = source_state(
        source_version=None,
        content_hash=None,
        status="failed",
        last_success_at=None,
        last_error_type=SourceErrorType.NETWORK_TIMEOUT,
    )
    failed_snapshot = active_snapshot(
        source_version="unavailable",
        source_time=None,
        status="failed",
        chunks=(),
        content_hash=None,
        error_type=SourceErrorType.NETWORK_TIMEOUT,
        retryable=True,
    )

    result = repository.commit(
        sync_commit(
            cursor=1,
            snapshots=(failed_snapshot,),
            states=(failed_state,),
            protected_sources=(),
            protected_chunks=(),
        )
    )

    stored = repository.load_active_generation("project-1")
    assert stored is not None
    assert result.outcome == "degraded"
    assert stored.source_states == (failed_state,)
    assert stored.protected_sources == ()
    assert stored.protected_chunks == ()


@pytest.mark.parametrize(
    "next_status",
    [SourceSyncStatus.FAILED, SourceSyncStatus.STALE],
)
def test_failed_or_stale_source_cannot_reuse_empty_failed_state(
    tmp_path: Path,
    next_status: SourceSyncStatus,
) -> None:
    repository = repository_at(tmp_path)
    repository.initialize()
    failed_state = source_state(
        source_version=None,
        content_hash=None,
        status="failed",
        last_success_at=None,
        last_error_type=SourceErrorType.NETWORK_TIMEOUT,
    )
    failed_snapshot = active_snapshot(
        source_version="unavailable",
        source_time=None,
        status="failed",
        chunks=(),
        content_hash=None,
        error_type=SourceErrorType.NETWORK_TIMEOUT,
        retryable=True,
    )
    repository.commit(
        sync_commit(
            cursor=1,
            snapshots=(failed_snapshot,),
            states=(failed_state,),
            protected_sources=(),
            protected_chunks=(),
        )
    )
    next_state_updates: dict[str, object] = {
        "status": next_status,
        "last_success_at": None,
        "last_error_type": SourceErrorType.NETWORK_TIMEOUT,
    }
    next_snapshot_updates: dict[str, object] = {
        "status": next_status,
        "chunks": (),
        "error_type": SourceErrorType.NETWORK_TIMEOUT,
        "retryable": True,
    }
    if next_status is SourceSyncStatus.FAILED:
        next_state_updates["content_hash"] = None
        next_snapshot_updates["content_hash"] = None
    next_state = source_state(**next_state_updates)
    next_snapshot = active_snapshot(**next_snapshot_updates)

    with pytest.raises(SyncConflict, match="source_reuse_conflict"):
        repository.commit(
            sync_commit(
                cursor=2,
                content_hash=HASH_B,
                snapshots=(next_snapshot,),
                states=(next_state,),
                protected_sources=(),
                protected_chunks=(),
            )
        )


def test_failed_source_reuse_rejects_time_rollback(tmp_path: Path) -> None:
    repository = repository_at(tmp_path)
    repository.initialize()
    repository.commit(sync_commit(cursor=1))
    failed_state = source_state(
        status="failed",
        content_hash=None,
        last_attempt_at=NOW - timedelta(seconds=1),
        last_success_at=None,
        last_error_type=SourceErrorType.NETWORK_TIMEOUT,
    )
    failed_snapshot = active_snapshot(
        status="failed",
        chunks=(),
        content_hash=None,
        error_type=SourceErrorType.NETWORK_TIMEOUT,
        retryable=True,
    )

    with pytest.raises(SyncConflict, match="source_reuse_conflict"):
        repository.commit(
            sync_commit(
                cursor=2,
                content_hash=HASH_B,
                snapshots=(failed_snapshot,),
                states=(failed_state,),
                protected_sources=(),
                protected_chunks=(),
            )
        )


def test_stale_source_reuse_rejects_permission_hash_change(
    tmp_path: Path,
) -> None:
    repository = repository_at(tmp_path)
    repository.initialize()
    repository.commit(sync_commit(cursor=1))
    stale_state = source_state(
        permission_hash=HASH_D,
        status="stale",
        last_error_type=SourceErrorType.PERMISSION_DENIED,
    )
    stale_snapshot = active_snapshot(
        permission_hash=HASH_D,
        status="stale",
        chunks=(),
        error_type=SourceErrorType.PERMISSION_DENIED,
        retryable=False,
    )

    with pytest.raises(SyncConflict, match="source_reuse_conflict"):
        repository.commit(
            sync_commit(
                cursor=2,
                content_hash=HASH_B,
                snapshots=(stale_snapshot,),
                states=(stale_state,),
                protected_sources=(),
                protected_chunks=(),
            )
        )


@pytest.mark.parametrize(
    "corruption_sql",
    [
        "UPDATE project_source_states SET protected_title = X''",
        "UPDATE project_evidence_chunks SET protected_text = X''",
    ],
)
def test_failed_source_reuse_rejects_incomplete_protected_payload(
    tmp_path: Path,
    corruption_sql: str,
) -> None:
    repository = repository_at(tmp_path)
    repository.initialize()
    repository.commit(sync_commit(cursor=1))
    with sqlite3.connect(tmp_path / "project-memory.db") as connection:
        connection.execute(corruption_sql)
    failed_state = source_state(
        status="failed",
        content_hash=None,
        last_success_at=None,
        last_error_type=SourceErrorType.NETWORK_TIMEOUT,
    )
    failed_snapshot = active_snapshot(
        status="failed",
        chunks=(),
        content_hash=None,
        error_type=SourceErrorType.NETWORK_TIMEOUT,
        retryable=True,
    )

    with pytest.raises(SyncConflict, match="source_reuse_conflict"):
        repository.commit(
            sync_commit(
                cursor=2,
                content_hash=HASH_B,
                snapshots=(failed_snapshot,),
                states=(failed_state,),
                protected_sources=(),
                protected_chunks=(),
            )
        )


def test_failed_task_reuse_rejects_missing_protected_source(tmp_path: Path) -> None:
    repository = repository_at(tmp_path)
    repository.initialize()
    task_snapshot = active_snapshot(source_type="task", chunks=())
    task_state = source_state(source_type="task")
    repository.commit(
        sync_commit(
            cursor=1,
            snapshots=(task_snapshot,),
            states=(task_state,),
            protected_sources=(
                protected_source(source_type=SyncSourceType.TASK),
            ),
            protected_chunks=(),
        )
    )
    with sqlite3.connect(tmp_path / "project-memory.db") as connection:
        connection.execute(
            "UPDATE project_source_states SET protected_source_id = NULL"
        )
    failed_state = source_state(
        source_type="task",
        status="failed",
        content_hash=None,
        last_success_at=None,
        last_error_type=SourceErrorType.NETWORK_TIMEOUT,
    )
    failed_snapshot = active_snapshot(
        source_type="task",
        status="failed",
        chunks=(),
        content_hash=None,
        error_type=SourceErrorType.NETWORK_TIMEOUT,
        retryable=True,
    )

    with pytest.raises(SyncConflict, match="source_reuse_conflict"):
        repository.commit(
            sync_commit(
                cursor=2,
                content_hash=HASH_B,
                snapshots=(failed_snapshot,),
                states=(failed_state,),
                protected_sources=(),
                protected_chunks=(),
            )
        )


def retrieval_request(**updates: object) -> RetrievalRequest:
    values: dict[str, object] = {
        "request_id": "request-1",
        "project_id": "project-1",
        "query_hash": HASH_A,
        "source_id_hashes": (source_id_hash("real-source-id"),),
        "status": "pending",
        "created_at": NOW,
        "expires_at": NOW + timedelta(hours=1),
    }
    values.update(updates)
    return RetrievalRequest(**values)


def test_retrieval_request_captures_active_generation_baseline(
    tmp_path: Path,
) -> None:
    repository = repository_at(tmp_path)
    repository.initialize()
    repository.commit(sync_commit(cursor=1, content_hash=HASH_A))

    saved = repository.save_retrieval_request(retrieval_request())

    assert saved.baseline_generation_id == "generation-1"
    assert saved.baseline_content_hash == HASH_A
    assert saved.baseline_source_cursor == 1
    assert len(saved.baseline_sources) == 1
    baseline = saved.baseline_sources[0]
    assert baseline.source_id_hash == source_id_hash("real-source-id")
    assert baseline.source_version == "v1"
    assert baseline.content_hash == HASH_B
    assert len(baseline.chunk_fingerprint) == 64


def test_unrelated_source_change_cannot_complete_retrieval(
    tmp_path: Path,
) -> None:
    repository = repository_at(tmp_path)
    repository.initialize()
    other = active_document_records(
        source_id="other-source-id",
        source_version="v1",
        source_content_hash=HASH_D,
        chunk_id=HASH_E,
        chunk_content_hash=HASH_F,
        observed_at=NOW,
    )
    repository.commit(
        sync_commit(
            cursor=1,
            content_hash=HASH_A,
            snapshots=(active_snapshot(), other[0]),
            states=(source_state(), other[1]),
            protected_sources=(protected_source(), other[2]),
            protected_chunks=(protected_chunk(), other[3]),
        )
    )
    request = repository.save_retrieval_request(retrieval_request())
    changed_other = active_document_records(
        source_id="other-source-id",
        source_version="v2",
        source_content_hash=HASH_E,
        chunk_id=HASH_F,
        chunk_content_hash=HASH_D,
        observed_at=NOW + timedelta(minutes=1),
    )

    with pytest.raises(SyncConflict, match="retrieval_evidence_missing"):
        repository.commit(
            sync_commit(
                cursor=2,
                content_hash=HASH_B,
                snapshots=(active_snapshot(), changed_other[0]),
                states=(source_state(), changed_other[1]),
                protected_sources=(protected_source(), changed_other[2]),
                protected_chunks=(protected_chunk(), changed_other[3]),
                completed_request_ids=(request.request_id,),
            )
        )


def test_metadata_only_source_refresh_cannot_complete_retrieval(
    tmp_path: Path,
) -> None:
    repository = repository_at(tmp_path)
    repository.initialize()
    repository.commit(sync_commit(cursor=1, content_hash=HASH_A))
    request = repository.save_retrieval_request(retrieval_request())
    metadata_only = active_document_records(
        source_id="real-source-id",
        source_version="v2",
        source_content_hash=HASH_B,
        chunk_id=HASH_D,
        chunk_content_hash=HASH_B,
        observed_at=NOW + timedelta(minutes=1),
    )

    with pytest.raises(SyncConflict, match="retrieval_evidence_missing"):
        repository.commit(
            sync_commit(
                cursor=2,
                content_hash=HASH_C,
                snapshots=(metadata_only[0],),
                states=(metadata_only[1],),
                protected_sources=(metadata_only[2],),
                protected_chunks=(metadata_only[3],),
                completed_request_ids=(request.request_id,),
            )
        )


def test_retrieval_request_rechecks_active_sources_inside_write_transaction(
    tmp_path: Path,
) -> None:
    repository = repository_at(tmp_path)
    repository.initialize()
    repository.commit(sync_commit(cursor=1, content_hash=HASH_A))
    revoked = source_state(
        source_version=None,
        content_hash=None,
        status=SourceSyncStatus.REVOKED,
        last_attempt_at=NOW + timedelta(minutes=1),
        last_success_at=None,
    )
    tombstone = SourceTombstone(
        source_type=SyncSourceType.DOCUMENT,
        source_id="real-source-id",
        status=SourceSyncStatus.REVOKED,
        occurred_at=NOW + timedelta(minutes=1),
        permission_scope="project:demo",
    )
    repository.commit(
        sync_commit(
            cursor=2,
            content_hash=HASH_B,
            snapshots=(),
            tombstones=(tombstone,),
            states=(revoked,),
            protected_sources=(),
            protected_chunks=(),
        )
    )

    with pytest.raises(SyncConflict, match="retrieval_source_unavailable"):
        repository.save_retrieval_request(retrieval_request())


def test_unchanged_generation_cannot_complete_pending_retrieval(
    tmp_path: Path,
) -> None:
    repository = repository_at(tmp_path)
    repository.initialize()
    repository.commit(sync_commit(cursor=1, content_hash=HASH_A))
    request = repository.save_retrieval_request(retrieval_request())

    with pytest.raises(SyncConflict, match="retrieval_evidence_missing"):
        repository.commit(
            sync_commit(
                cursor=2,
                content_hash=HASH_A,
                completed_request_ids=(request.request_id,),
                outcome="unchanged",
            )
        )

    active = repository.load_active_generation("project-1")
    assert active is not None
    assert active.source_cursor == 1
    assert repository.get_retrieval_request(
        "project-1", request.request_id
    ) == request


def test_retrieval_completion_requires_new_evidence_for_every_source(
    tmp_path: Path,
) -> None:
    repository = repository_at(tmp_path)
    repository.initialize()
    other = active_document_records(
        source_id="other-source-id",
        source_version="v1",
        source_content_hash=HASH_D,
        chunk_id=HASH_E,
        chunk_content_hash=HASH_F,
        observed_at=NOW,
    )
    repository.commit(
        sync_commit(
            cursor=1,
            content_hash=HASH_A,
            snapshots=(active_snapshot(), other[0]),
            states=(source_state(), other[1]),
            protected_sources=(protected_source(), other[2]),
            protected_chunks=(protected_chunk(), other[3]),
        )
    )
    request = repository.save_retrieval_request(
        retrieval_request(
            source_id_hashes=(
                source_id_hash("real-source-id"),
                source_id_hash("other-source-id"),
            )
        )
    )

    with pytest.raises(SyncConflict, match="retrieval_evidence_missing"):
        repository.commit(
            sync_commit(
                cursor=2,
                content_hash=HASH_B,
                completed_request_ids=(request.request_id,),
            )
        )

    active = repository.load_active_generation("project-1")
    assert active is not None
    assert active.source_cursor == 1
    assert repository.get_retrieval_request(
        "project-1", request.request_id
    ) == request


def test_retrieval_requests_use_compare_and_set_transitions(tmp_path: Path) -> None:
    repository = repository_at(tmp_path)
    repository.initialize()
    repository.commit(sync_commit(cursor=1))
    request = retrieval_request()

    saved = repository.save_retrieval_request(request)
    assert repository.save_retrieval_request(request) == saved
    claimed = repository.claim_retrieval_requests(
        "project-1",
        now=NOW + timedelta(seconds=1),
        lease_seconds=60,
    )[0]
    assert claimed.status is RetrievalRequestStatus.IN_PROGRESS
    assert claimed.attempt_count == 1
    assert claimed.lease_expires_at == NOW + timedelta(seconds=61)
    assert repository.claim_retrieval_requests(
        "project-1",
        now=NOW + timedelta(seconds=60),
        lease_seconds=60,
    ) == ()
    reclaimed = repository.claim_retrieval_requests(
        "project-1",
        now=NOW + timedelta(seconds=62),
        lease_seconds=60,
    )[0]
    assert reclaimed.status is RetrievalRequestStatus.IN_PROGRESS
    assert reclaimed.attempt_count == 2
    assert reclaimed.lease_expires_at == NOW + timedelta(seconds=122)
    assert repository.list_retrieval_requests("project-1") == (reclaimed,)
    with pytest.raises(ValueError, match="completed"):
        repository.compare_and_set_retrieval_request(
            "project-1",
            "request-1",
            frozenset({RetrievalRequestStatus.IN_PROGRESS}),
            RetrievalRequestStatus.COMPLETED,
            completed_at=NOW,
        )


def test_saving_retrieval_request_cannot_bypass_state_cas(tmp_path: Path) -> None:
    repository = repository_at(tmp_path)
    repository.initialize()
    repository.commit(sync_commit(cursor=1))
    request = retrieval_request()
    saved = repository.save_retrieval_request(request)

    with pytest.raises(SyncConflict, match="retrieval_request_conflict"):
        repository.save_retrieval_request(
            saved.model_copy(
                update={
                    "status": RetrievalRequestStatus.IN_PROGRESS,
                    "lease_expires_at": NOW + timedelta(minutes=1),
                    "attempt_count": 1,
                }
            )
        )

    assert repository.get_retrieval_request("project-1", "request-1") == saved


def test_completed_retrieval_request_commits_with_evidence(tmp_path: Path) -> None:
    repository = repository_at(tmp_path)
    repository.initialize()
    repository.commit(sync_commit(cursor=1, content_hash=HASH_A))
    pending = repository.save_retrieval_request(retrieval_request())
    request = pending

    updated = active_document_records(
        source_id="real-source-id",
        source_version="v2",
        source_content_hash=HASH_D,
        chunk_id=HASH_E,
        chunk_content_hash=HASH_F,
        observed_at=NOW + timedelta(minutes=1),
    )
    repository.commit(
        sync_commit(
            cursor=2,
            content_hash=HASH_B,
            snapshots=(updated[0],),
            states=(updated[1],),
            protected_sources=(updated[2],),
            protected_chunks=(updated[3],),
            completed_request_ids=(request.request_id,),
        )
    )

    completed = repository.get_retrieval_request("project-1", request.request_id)
    assert completed is not None
    assert completed.status is RetrievalRequestStatus.COMPLETED
    assert completed.completed_at == NOW + timedelta(minutes=1, seconds=1)
    assert repository.load_active_generation("project-1") is not None


def test_missing_retrieval_evidence_rolls_back_generation_and_request(
    tmp_path: Path,
) -> None:
    repository = repository_at(tmp_path)
    repository.initialize()
    repository.commit(sync_commit(cursor=1, content_hash=HASH_A))
    request = repository.save_retrieval_request(retrieval_request())
    other = active_document_records(
        source_id="other-source-id",
        source_version="v2",
        source_content_hash=HASH_D,
        chunk_id=HASH_E,
        chunk_content_hash=HASH_F,
        observed_at=NOW + timedelta(minutes=1),
    )

    with pytest.raises(SyncConflict, match="retrieval_evidence_missing"):
        repository.commit(
            sync_commit(
                cursor=2,
                content_hash=HASH_B,
                snapshots=(other[0],),
                states=(other[1],),
                protected_sources=(other[2],),
                protected_chunks=(other[3],),
                completed_request_ids=(request.request_id,),
            )
        )

    active = repository.load_active_generation("project-1")
    assert active is not None
    assert active.source_cursor == 1
    assert repository.get_retrieval_request("project-1", request.request_id) == request
    with sqlite3.connect(tmp_path / "project-memory.db") as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM project_source_states"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM project_evidence_chunks"
        ).fetchone() == (1,)


def test_failure_before_activation_rolls_back_new_generation(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "project-memory.db"
    repository = repository_at(tmp_path)
    repository.initialize()
    repository.commit(sync_commit(cursor=1))

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TRIGGER fail_generation_activation
            BEFORE UPDATE ON project_active_generations
            WHEN NEW.generation_id = 'generation-2'
            BEGIN
                SELECT RAISE(ABORT, 'simulated_activation_failure');
            END
            """
        )
    with pytest.raises(sqlite3.IntegrityError, match="simulated_activation_failure"):
        repository.commit(sync_commit(cursor=2, content_hash=HASH_B))

    reopened = ProjectSyncRepository(database_path)
    reopened.initialize()
    stored = reopened.load_active_generation("project-1")
    assert stored is not None
    assert stored.source_cursor == 1
    assert stored.content_hash == HASH_A


def test_load_active_generation_reads_one_sqlite_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "project-memory.db"
    repository = repository_at(tmp_path)
    repository.initialize()
    repository.commit(sync_commit(cursor=1))
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA journal_mode = WAL").fetchone() == ("wal",)

    reader_started = threading.Event()
    writer_finished = threading.Event()
    writer_errors: list[BaseException] = []
    load_active_row = repository._load_active_row

    def pause_after_generation_read(
        connection: sqlite3.Connection,
        project_id: str,
    ) -> sqlite3.Row | None:
        row = load_active_row(connection, project_id)
        reader_started.set()
        assert writer_finished.wait(timeout=5)
        return row

    def promote_next_generation() -> None:
        try:
            assert reader_started.wait(timeout=5)
            repository_at(tmp_path).commit(
                sync_commit(cursor=2, content_hash=HASH_B)
            )
        except BaseException as error:
            writer_errors.append(error)
        finally:
            writer_finished.set()

    monkeypatch.setattr(repository, "_load_active_row", pause_after_generation_read)
    writer = threading.Thread(target=promote_next_generation)
    writer.start()
    loaded = repository.load_active_generation("project-1")
    writer.join(timeout=5)

    assert not writer.is_alive()
    assert writer_errors == []
    assert loaded is not None
    assert loaded.generation_id == "generation-1"
    assert loaded.source_states == (source_state(),)
    assert loaded.protected_chunks == (protected_chunk(),)
    current = repository_at(tmp_path).load_active_generation("project-1")
    assert current is not None
    assert current.generation_id == "generation-2"


@pytest.mark.parametrize(
    "audit_updates",
    [
        {"source_counts_by_status": {SourceSyncStatus.FAILED: 1}},
        {"chunk_count": 0},
        {"outcome": "degraded"},
    ],
)
def test_audit_mismatch_rolls_back_commit(
    tmp_path: Path,
    audit_updates: dict[str, object],
) -> None:
    repository = repository_at(tmp_path)
    repository.initialize()
    candidate = sync_commit(cursor=1)
    candidate = replace(
        candidate,
        audit=candidate.audit.model_copy(update=audit_updates),
    )

    with pytest.raises(SyncConflict, match="audit_mismatch"):
        repository.commit(candidate)

    assert repository.load_active_generation("project-1") is None
    with sqlite3.connect(tmp_path / "project-memory.db") as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM project_sync_audits"
        ).fetchone() == (0,)


def test_unchanged_audit_outcome_must_match_actual_result(tmp_path: Path) -> None:
    repository = repository_at(tmp_path)
    repository.initialize()
    repository.commit(sync_commit(cursor=1))

    with pytest.raises(SyncConflict, match="audit_mismatch"):
        repository.commit(sync_commit(cursor=2))

    stored = repository.load_active_generation("project-1")
    assert stored is not None
    assert stored.source_cursor == 1
    assert repository.get_retrieval_request("project-1", "request-1") is None


def test_sync_tables_do_not_store_private_source_fields_in_plaintext(
    tmp_path: Path,
) -> None:
    repository = repository_at(tmp_path)
    repository.initialize()
    repository.commit(sync_commit(cursor=1))

    stored_bytes = (tmp_path / "project-memory.db").read_bytes()
    for secret in (
        "real-source-id",
        "真实项目标题",
        "https://example.invalid/private-document",
        "私密章节",
        "绝不能明文落盘的正文",
    ):
        assert secret.encode() not in stored_bytes
