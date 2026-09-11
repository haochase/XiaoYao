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
    DecisionStatus,
    DecisionVersion,
    EvidenceRef,
    HumanApprovalRef,
    ProjectContextPackage,
)
from companion_gateway.project.repository import ProjectMemoryRepository
from companion_gateway.project.service import ProjectMemoryService
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
        "project_name": "小千项目",
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


def source_backed_reference(snapshot: SourceSnapshot) -> EvidenceRef:
    assert snapshot.source_time is not None
    return EvidenceRef(
        source_type=snapshot.source_type,
        source_id=snapshot.source_id,
        source_title=snapshot.source_title,
        source_url=snapshot.source_url,
        source_time=snapshot.source_time,
        excerpt=snapshot.chunks[0].text,
        permission_scope=snapshot.permission_scope,
    )


def source_backed_decision(
    decision_id: str,
    snapshot: SourceSnapshot,
) -> DecisionCard:
    return DecisionCard(
        decision_id=decision_id,
        project_id="project-1",
        topic=f"topic-{decision_id}",
        decision_text=f"decision-{decision_id}",
        rationale="source-backed",
        owner="owner-1",
        decided_at=NOW,
        source_refs=(source_backed_reference(snapshot),),
        status="active",
        confidence=0.9,
    )


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
    completion_claims: tuple[object, ...] = (),
    outcome: str | None = None,
) -> SyncCommit:
    chosen_snapshots = snapshots if snapshots is not None else (active_snapshot(),)
    chosen_states = states if states is not None else (source_state(),)
    identifier = sync_id or f"sync-{cursor}"
    envelope_values: dict[str, object] = {
        "schema_version": 1,
        "sync_id": identifier,
        "project_id": "project-1",
        "generated_at": NOW + timedelta(minutes=cursor - 1),
        "source_cursor": cursor,
        "content_hash": content_hash,
        "producer": "qwenwork-dws",
        "context": package or context(generated_at=NOW + timedelta(minutes=cursor - 1)),
        "sources": chosen_snapshots,
        "tombstones": tombstones,
        "completed_retrieval_request_ids": completed_request_ids,
    }
    if completion_claims:
        envelope_values["completed_retrieval_claims"] = completion_claims
    envelope = SyncEnvelope(**envelope_values)
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


def decision_split_state(
    tmp_path: Path,
) -> tuple[ProjectSyncRepository, ProjectMemoryService, ProjectContextPackage]:
    repository = repository_at(tmp_path)
    repository.initialize()
    initial = context()
    repository.commit(sync_commit(cursor=1, package=initial))
    memory = ProjectMemoryService(
        repository=ProjectMemoryRepository(tmp_path / "project-memory.db"),
        clock=lambda: NOW,
    )
    candidate, _ = memory.propose_conflict_from_statement(
        "project-1",
        "发布方案改成方案 A",
        proposed_decision_text="采用方案 A",
        now=NOW,
    )
    memory.review_conflict(
        candidate.candidate_id,
        reviewer_id="owner-1",
        action="accept",
        change_reason="负责人批准组合决策",
        now=NOW,
    )
    source = active_snapshot()
    first = source_backed_decision("decision-split-1", source)
    second = source_backed_decision("decision-split-2", source)
    three_card_context = initial.model_copy(
        update={
            "generated_at": NOW + timedelta(minutes=1),
            "active_decisions": (
                initial.active_decisions[0],
                first,
                second,
            ),
        }
    )
    repository.commit(
        sync_commit(
            cursor=2,
            content_hash=HASH_B,
            package=three_card_context,
        )
    )
    active = repository.load_active_generation("project-1")
    assert active is not None
    return repository, memory, active.context


def replace_split_context(
    tmp_path: Path,
    package: ProjectContextPackage,
    *,
    update_project_context: bool = True,
    update_generation_context: bool = True,
) -> None:
    payload = json.dumps(
        package.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    with sqlite3.connect(tmp_path / "project-memory.db") as connection:
        if update_project_context:
            connection.execute(
                "UPDATE project_contexts SET payload_json = ? WHERE project_id = ?",
                (payload, "project-1"),
            )
        if update_generation_context:
            connection.execute(
                """
                UPDATE project_sync_generations SET context_json = ?
                WHERE project_id = ? AND generation_id = ?
                """,
                (payload, "project-1", "generation-2"),
            )


def database_dump(database_path: Path) -> tuple[str, ...]:
    with sqlite3.connect(database_path) as connection:
        return tuple(connection.iterdump())


def failed_only_sync_commit(
    *,
    cursor: int,
    content_hash: str = HASH_A,
    outcome: str | None = None,
    permission_scope: str = "project:demo",
    package: ProjectContextPackage | None = None,
) -> SyncCommit:
    observed_at = NOW + timedelta(minutes=cursor - 1)
    failed_state = source_state(
        source_version=None,
        content_hash=None,
        status=SourceSyncStatus.FAILED,
        last_attempt_at=observed_at,
        last_success_at=None,
        last_error_type=SourceErrorType.NETWORK_TIMEOUT,
    )
    failed_snapshot = active_snapshot(
        source_version="unavailable",
        source_time=None,
        fetched_at=observed_at,
        permission_scope=permission_scope,
        status=SourceSyncStatus.FAILED,
        chunks=(),
        content_hash=None,
        error_type=SourceErrorType.NETWORK_TIMEOUT,
        retryable=True,
    )
    return sync_commit(
        cursor=cursor,
        content_hash=content_hash,
        package=package
        or context(
            generated_at=observed_at,
            source_refs=(),
            active_decisions=(),
            permission_scope=permission_scope,
        ),
        snapshots=(failed_snapshot,),
        states=(failed_state,),
        protected_sources=(),
        protected_chunks=(),
        outcome=outcome,
    )


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
    assert pending.baseline_content_hash is None
    assert pending.baseline_source_cursor is None
    assert pending.baseline_sources == ()
    assert pending.lease_expires_at is None
    assert completed is not None
    assert completed.status is RetrievalRequestStatus.COMPLETED
    assert completed.baseline_generation_id is None
    assert completed.baseline_content_hash is None
    assert completed.baseline_source_cursor is None
    assert completed.baseline_sources == ()


def test_initialize_serializes_schema_migrations_and_records_version(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "project-memory.db"
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def initialize() -> None:
        try:
            barrier.wait(timeout=5)
            ProjectSyncRepository(database_path).initialize()
        except BaseException as exc:
            errors.append(exc)

    workers = [threading.Thread(target=initialize) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)

    assert all(not worker.is_alive() for worker in workers)
    assert errors == []
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT schema_version FROM project_sync_schema WHERE singleton_id = 1"
        ).fetchone() == (3,)


def test_protected_rows_persist_and_validate_protector_version(tmp_path: Path) -> None:
    repository = repository_at(tmp_path)
    repository.initialize()
    repository.configure_protection(HASH_A, "test-protector-v1")
    repository.commit(
        sync_commit(
            cursor=1,
            protected_sources=(
                protected_source(protector_version="test-protector-v1"),
            ),
            protected_chunks=(
                protected_chunk(protector_version="test-protector-v1"),
            ),
        )
    )
    with sqlite3.connect(tmp_path / "project-memory.db") as connection:
        assert connection.execute(
            "SELECT protector_version FROM project_source_states"
        ).fetchone() == ("test-protector-v1",)
        assert connection.execute(
            "SELECT protector_version FROM project_evidence_chunks"
        ).fetchone() == ("test-protector-v1",)
        connection.execute(
            "UPDATE project_evidence_chunks SET protector_version = 'wrong-v2'"
        )

    with pytest.raises(SyncConflict, match="protected_row_version_mismatch"):
        repository.load_active_generation("project-1")


def test_initialize_backfills_protector_version_only_for_legacy_schema(
    tmp_path: Path,
) -> None:
    repository = repository_at(tmp_path)
    repository.initialize()
    repository.configure_protection(HASH_A, "test-protector-v1")
    repository.commit(sync_commit(cursor=1))
    database_path = tmp_path / "project-memory.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE project_source_states SET protector_version = NULL"
        )
        connection.execute(
            "UPDATE project_evidence_chunks SET protector_version = NULL"
        )
        connection.execute(
            "UPDATE project_sync_schema SET schema_version = 1"
        )

    reopened = ProjectSyncRepository(database_path)
    reopened.initialize()
    reopened.configure_protection(HASH_A, "test-protector-v1")

    stored = reopened.load_active_generation("project-1")
    assert stored is not None
    assert stored.protected_sources[0].protector_version == "test-protector-v1"
    assert stored.protected_chunks[0].protector_version == "test-protector-v1"


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


def test_seen_source_version_cannot_become_head_again(
    tmp_path: Path,
) -> None:
    repository = repository_at(tmp_path)
    repository.initialize()
    repository.commit(sync_commit(cursor=1, content_hash=HASH_A))
    version_two = active_document_records(
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
            snapshots=(version_two[0],),
            states=(version_two[1],),
            protected_sources=(version_two[2],),
            protected_chunks=(version_two[3],),
        )
    )
    reused_version = active_document_records(
        source_id="real-source-id",
        source_version="v1",
        source_content_hash=HASH_B,
        chunk_id=HASH_C,
        chunk_content_hash=HASH_B,
        observed_at=NOW + timedelta(minutes=2),
    )

    with pytest.raises(SyncConflict, match="source_version_rollback"):
        repository.commit(
            sync_commit(
                cursor=3,
                content_hash=HASH_C,
                snapshots=(reused_version[0],),
                states=(reused_version[1],),
                protected_sources=(reused_version[2],),
                protected_chunks=(reused_version[3],),
            )
        )


def test_same_source_version_and_content_allows_later_source_time(
    tmp_path: Path,
) -> None:
    repository = repository_at(tmp_path)
    repository.initialize()
    repository.commit(sync_commit(cursor=1, content_hash=HASH_A))
    later = active_document_records(
        source_id="real-source-id",
        source_version="v1",
        source_content_hash=HASH_B,
        chunk_id=HASH_C,
        chunk_content_hash=HASH_B,
        observed_at=NOW + timedelta(minutes=1),
    )

    result = repository.commit(
        sync_commit(
            cursor=2,
            content_hash=HASH_B,
            snapshots=(later[0],),
            states=(later[1],),
            protected_sources=(later[2],),
            protected_chunks=(later[3],),
        )
    )

    assert result.outcome == "applied"


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


@pytest.mark.parametrize("outcome,next_hash", [("unchanged", HASH_A), ("applied", HASH_B)])
def test_reviewed_decision_survives_an_unchanged_source_renewal(
    tmp_path: Path,
    outcome: str,
    next_hash: str,
) -> None:
    repository = repository_at(tmp_path)
    repository.initialize()
    repository.commit(sync_commit(cursor=1))
    memory = ProjectMemoryService(
        repository=ProjectMemoryRepository(tmp_path / "project-memory.db"),
        clock=lambda: NOW,
    )
    candidate, _ = memory.propose_conflict_from_statement(
        "project-1",
        "发布方案改成方案 A",
        proposed_decision_text="采用方案 A",
        now=NOW,
    )
    _, version = memory.review_conflict(
        candidate.candidate_id,
        reviewer_id="owner-1",
        action="accept",
        change_reason="供应风险变化",
        now=NOW,
    )

    result = repository.commit(
        sync_commit(
            cursor=2,
            content_hash=next_hash,
            outcome=outcome,
            states=(
                source_state(
                    last_attempt_at=NOW + timedelta(minutes=1),
                    last_success_at=NOW + timedelta(minutes=1),
                ),
            ),
        )
    )

    stored = repository.load_active_generation("project-1")
    assert result.outcome == outcome
    assert stored is not None
    assert stored.context.active_decisions[0].decision_text == "采用方案 A"
    assert stored.context.active_decisions[0].source_refs == ()
    assert stored.context.active_decisions[0].approval_ref == version.approval_ref


def test_sync_commit_rejects_external_approval_before_overlay(tmp_path: Path) -> None:
    from companion_gateway.project.models import HumanApprovalRef

    repository = repository_at(tmp_path)
    repository.initialize()
    original = sync_commit(cursor=1)
    decision = original.envelope.context.active_decisions[0]
    approval = HumanApprovalRef(
        candidate_id="fake", reviewer_id="fake", approved_at=NOW,
        reason="fake", decision_text=decision.decision_text,
        permission_scope=original.envelope.context.permission_scope,
    )
    forged = original.envelope.context.model_copy(update={"active_decisions": (
        decision.model_copy(update={"approval_ref": approval}),
    )})
    with pytest.raises(ValueError, match="external_approval_forbidden"):
        repository.commit(replace(original, envelope=original.envelope.model_copy(update={"context": forged})))
    assert repository.load_active_generation("project-1") is None


def test_review_interleaved_with_sync_preserves_refreshed_context(tmp_path: Path, monkeypatch) -> None:
    repository = repository_at(tmp_path)
    repository.initialize()
    initial = context()
    another = initial.active_decisions[0].model_copy(
        update={"decision_id": "decision-2", "topic": "预算方案"}
    )
    initial = initial.model_copy(update={"active_decisions": (*initial.active_decisions, another)})
    repository.commit(sync_commit(cursor=1, package=initial))
    memory_repository = ProjectMemoryRepository(tmp_path / "project-memory.db")
    memory = ProjectMemoryService(repository=memory_repository, clock=lambda: NOW)
    candidate, _ = memory.propose_conflict_from_statement(
        "project-1", "发布方案改成方案 A", proposed_decision_text="采用方案 A", now=NOW,
    )
    refreshed_at = NOW + timedelta(minutes=1)
    refreshed = initial.model_copy(update={
        "project_name": "最新项目名称", "generated_at": refreshed_at,
        "freshness_seconds": 900,
    })
    commit = memory_repository.commit_conflict_review

    def refresh_before_commit(**kwargs):
        result = repository.commit(sync_commit(
            cursor=2, content_hash=HASH_B, package=refreshed,
            states=(source_state(last_attempt_at=refreshed_at, last_success_at=refreshed_at),),
        ))
        assert result.outcome == "applied"
        return commit(**kwargs)

    monkeypatch.setattr(memory_repository, "commit_conflict_review", refresh_before_commit)
    _, version = memory.review_conflict(
        candidate.candidate_id, reviewer_id="owner-1", action="accept",
        change_reason="负责人确认", now=refreshed_at,
    )
    stored = memory_repository.get_context("project-1")
    assert stored.project_name == refreshed.project_name
    assert stored.generated_at == refreshed_at
    assert stored.freshness_seconds == 900
    assert stored.active_decisions[1] == another
    assert stored.active_decisions[0].approval_ref == version.approval_ref
    assert stored.active_decisions[0].source_refs == ()

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


def test_commit_allows_first_sourced_decision_after_failed_only_history(
    tmp_path: Path,
) -> None:
    repository = repository_at(tmp_path)
    repository.initialize()
    repository.commit(failed_only_sync_commit(cursor=1))
    repository.commit(
        failed_only_sync_commit(cursor=2, outcome="unchanged")
    )
    database_path = tmp_path / "project-memory.db"
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM project_source_versions WHERE project_id = ?",
            ("project-1",),
        ).fetchone() == (0,)
    success_at = NOW + timedelta(minutes=2, seconds=1)
    active = active_document_records(
        source_id="real-source-id",
        source_version="v1",
        source_content_hash=HASH_B,
        chunk_id=HASH_C,
        chunk_content_hash=HASH_B,
        observed_at=success_at,
    )
    decision = source_backed_decision("decision-1", active[0])
    sourced_context = context(
        generated_at=NOW + timedelta(minutes=2),
        source_refs=(),
        active_decisions=(decision,),
    )

    result = repository.commit(
        sync_commit(
            cursor=3,
            content_hash=HASH_B,
            package=sourced_context,
            snapshots=(active[0],),
            states=(active[1],),
            protected_sources=(active[2],),
            protected_chunks=(active[3],),
        )
    )

    stored = repository.load_active_generation("project-1")
    assert result.outcome == "applied"
    assert stored is not None
    assert stored.context.active_decisions == (decision,)
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM project_source_versions WHERE project_id = ?",
            ("project-1",),
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM project_versions WHERE project_id = ?",
            ("project-1",),
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM project_conflicts",
        ).fetchone() == (0,)


def test_commit_rejects_initial_decision_without_failed_generation(
    tmp_path: Path,
) -> None:
    repository = repository_at(tmp_path)
    repository.initialize()
    ProjectMemoryRepository(tmp_path / "project-memory.db").save_context(
        context(source_refs=(), active_decisions=())
    )
    assert repository.load_active_generation("project-1") is None
    success_at = NOW + timedelta(seconds=1)
    active = active_document_records(
        source_id="real-source-id",
        source_version="v1",
        source_content_hash=HASH_B,
        chunk_id=HASH_C,
        chunk_content_hash=HASH_B,
        observed_at=success_at,
    )

    with pytest.raises(SyncConflict, match="decision_change_requires_review"):
        repository.commit(
            sync_commit(
                cursor=1,
                snapshots=(active[0],),
                states=(active[1],),
                protected_sources=(active[2],),
                protected_chunks=(active[3],),
            )
        )


def test_commit_rejects_same_id_decision_change_after_source_success(
    tmp_path: Path,
) -> None:
    repository = repository_at(tmp_path)
    repository.initialize()
    repository.commit(sync_commit(cursor=1))

    with pytest.raises(SyncConflict, match="decision_change_requires_review"):
        repository.commit(
            sync_commit(
                cursor=2,
                package=context(
                    active_decisions=(
                        context().active_decisions[0].model_copy(
                            update={"decision_text": "改用方案 A"}
                        ),
                    )
                ),
            )
        )


@pytest.mark.parametrize(
    "added_ids",
    [
        ("decision-2",),
        ("decision-2", "decision-3"),
    ],
)
def test_commit_allows_pure_source_backed_decision_additions(
    tmp_path: Path,
    added_ids: tuple[str, ...],
) -> None:
    repository = repository_at(tmp_path)
    repository.initialize()
    snapshot = active_snapshot()
    original = context(source_refs=(), active_decisions=())
    repository.commit(
        sync_commit(cursor=1, package=original, snapshots=(snapshot,))
    )
    additions = tuple(
        source_backed_decision(decision_id, snapshot) for decision_id in added_ids
    )
    candidate = original.model_copy(
        update={
            "generated_at": NOW + timedelta(minutes=1),
            "active_decisions": additions,
        }
    )

    result = repository.commit(
        sync_commit(
            cursor=2,
            content_hash=HASH_B,
            package=candidate,
            snapshots=(snapshot,),
        )
    )

    stored = repository.load_active_generation("project-1")
    assert result.outcome == "applied"
    assert stored is not None
    assert stored.context.active_decisions == additions
    memory = ProjectMemoryRepository(tmp_path / "project-memory.db")
    for decision_id in added_ids:
        assert [
            version.version
            for version in memory.list_versions("project-1", decision_id)
        ] == [1]


def test_commit_rejects_source_backed_addition_with_duplicate_normalized_topic(
    tmp_path: Path,
) -> None:
    repository = repository_at(tmp_path)
    repository.initialize()
    snapshot = active_snapshot()
    existing = source_backed_decision("hardware-1", snapshot).model_copy(
        update={
            "topic": "Hardware Plan",
            "decision_text": "Use the audio board for the hardware rollout.",
        }
    )
    original = context(source_refs=(), active_decisions=(existing,))
    repository.commit(
        sync_commit(cursor=1, package=original, snapshots=(snapshot,))
    )
    addition = source_backed_decision("hardware-2", snapshot).model_copy(
        update={
            "topic": "  hardware\tplan  ",
            "decision_text": "Use the microphone board for the next demo.",
        }
    )
    candidate = original.model_copy(
        update={
            "generated_at": NOW + timedelta(minutes=1),
            "active_decisions": (existing, addition),
        }
    )

    with pytest.raises(SyncConflict, match="decision_change_requires_review"):
        repository.commit(
            sync_commit(
                cursor=2,
                content_hash=HASH_B,
                package=candidate,
                snapshots=(snapshot,),
            )
        )


def test_commit_rejects_source_backed_addition_when_text_contains_active_decision(
    tmp_path: Path,
) -> None:
    repository = repository_at(tmp_path)
    repository.initialize()
    snapshot = active_snapshot()
    existing = source_backed_decision("hardware-1", snapshot).model_copy(
        update={
            "topic": "hardware baseline",
            "decision_text": "Use ESP32-S3 audio board with ES7210 microphone",
        }
    )
    original = context(source_refs=(), active_decisions=(existing,))
    repository.commit(
        sync_commit(cursor=1, package=original, snapshots=(snapshot,))
    )
    addition = source_backed_decision("hardware-2", snapshot).model_copy(
        update={
            "topic": "hardware regeneration",
            "decision_text": (
                "Recommended build: use esp32-s3 audio board with "
                "\nes7210 microphone for the voice demo."
            ),
        }
    )
    candidate = original.model_copy(
        update={
            "generated_at": NOW + timedelta(minutes=1),
            "active_decisions": (existing, addition),
        }
    )

    with pytest.raises(SyncConflict, match="decision_change_requires_review"):
        repository.commit(
            sync_commit(
                cursor=2,
                content_hash=HASH_B,
                package=candidate,
                snapshots=(snapshot,),
            )
        )


def test_commit_allows_source_backed_addition_when_contained_text_is_too_short(
    tmp_path: Path,
) -> None:
    repository = repository_at(tmp_path)
    repository.initialize()
    snapshot = active_snapshot()
    existing = source_backed_decision("hardware-1", snapshot).model_copy(
        update={"topic": "hardware baseline", "decision_text": "ESP32"}
    )
    original = context(source_refs=(), active_decisions=(existing,))
    repository.commit(
        sync_commit(cursor=1, package=original, snapshots=(snapshot,))
    )
    addition = source_backed_decision("hardware-2", snapshot).model_copy(
        update={
            "topic": "voice board",
            "decision_text": "Use ESP32-S3 for the next voice prototype.",
        }
    )
    candidate = original.model_copy(
        update={
            "generated_at": NOW + timedelta(minutes=1),
            "active_decisions": (existing, addition),
        }
    )

    result = repository.commit(
        sync_commit(
            cursor=2,
            content_hash=HASH_B,
            package=candidate,
            snapshots=(snapshot,),
        )
    )

    stored = repository.load_active_generation("project-1")
    assert result.outcome == "applied"
    assert stored is not None
    assert stored.context.active_decisions == (existing, addition)


def test_commit_allows_independent_hardware_and_reminder_additions(
    tmp_path: Path,
) -> None:
    repository = repository_at(tmp_path)
    repository.initialize()
    snapshot = active_snapshot()
    hardware = source_backed_decision("hardware-1", snapshot).model_copy(
        update={
            "topic": "hardware rollout",
            "decision_text": "Use the ESP32-S3 audio board for the demo.",
        }
    )
    original = context(source_refs=(), active_decisions=(hardware,))
    repository.commit(
        sync_commit(cursor=1, package=original, snapshots=(snapshot,))
    )
    reminder = source_backed_decision("reminder-1", snapshot).model_copy(
        update={
            "topic": "customer review reminder",
            "decision_text": "Send a reminder before the next customer review.",
        }
    )
    follow_up = source_backed_decision("follow-up-1", snapshot).model_copy(
        update={
            "topic": "release owner",
            "decision_text": "Assign the release checklist to the hardware lead.",
        }
    )
    candidate = original.model_copy(
        update={
            "generated_at": NOW + timedelta(minutes=1),
            "active_decisions": (hardware, reminder, follow_up),
        }
    )

    result = repository.commit(
        sync_commit(
            cursor=2,
            content_hash=HASH_B,
            package=candidate,
            snapshots=(snapshot,),
        )
    )

    stored = repository.load_active_generation("project-1")
    assert result.outcome == "applied"
    assert stored is not None
    assert stored.context.active_decisions == (hardware, reminder, follow_up)


def test_commit_preserves_reviewed_v2_while_adding_source_backed_v1(
    tmp_path: Path,
) -> None:
    repository = repository_at(tmp_path)
    repository.initialize()
    snapshot = active_snapshot()
    original_decision = source_backed_decision("decision-1", snapshot)
    original = context(
        source_refs=(),
        active_decisions=(original_decision,),
    )
    repository.commit(
        sync_commit(cursor=1, package=original, snapshots=(snapshot,))
    )
    memory_repository = ProjectMemoryRepository(tmp_path / "project-memory.db")
    memory = ProjectMemoryService(repository=memory_repository, clock=lambda: NOW)
    conflict, _ = memory.propose_conflict_from_statement(
        "project-1",
        "decision-1 must change",
        proposed_decision_text="reviewed decision-1",
        now=NOW,
    )
    _, reviewed = memory.review_conflict(
        conflict.candidate_id,
        reviewer_id="owner-1",
        action="accept",
        change_reason="human review",
        now=NOW,
    )
    addition = source_backed_decision("decision-2", snapshot)
    candidate = original.model_copy(
        update={
            "generated_at": NOW + timedelta(minutes=1),
            "active_decisions": (original_decision, addition),
        }
    )

    repository.commit(
        sync_commit(
            cursor=2,
            content_hash=HASH_B,
            package=candidate,
            snapshots=(snapshot,),
        )
    )

    stored = repository.load_active_generation("project-1")
    assert stored is not None
    decisions = {item.decision_id: item for item in stored.context.active_decisions}
    assert decisions["decision-1"].approval_ref == reviewed.approval_ref
    assert decisions["decision-1"].source_refs == ()
    assert decisions["decision-2"] == addition
    assert [
        version.version
        for version in memory_repository.list_versions("project-1", "decision-1")
    ] == [1, 2]
    assert [
        version.version
        for version in memory_repository.list_versions("project-1", "decision-2")
    ] == [1]


@pytest.mark.parametrize(
    "invalid_kind",
    ["missing_refs", "non_active", "duplicate_id"],
)
def test_commit_rejects_invalid_pure_additions(
    tmp_path: Path,
    invalid_kind: str,
) -> None:
    repository = repository_at(tmp_path)
    repository.initialize()
    snapshot = active_snapshot()
    original = context(source_refs=(), active_decisions=())
    first = repository.commit(
        sync_commit(cursor=1, package=original, snapshots=(snapshot,))
    )
    addition = source_backed_decision("decision-2", snapshot)
    if invalid_kind == "missing_refs":
        addition = addition.model_copy(update={"source_refs": ()})
    elif invalid_kind == "non_active":
        addition = addition.model_copy(update={"status": "proposed"})
    candidate_decisions = (
        (addition, addition)
        if invalid_kind == "duplicate_id"
        else (addition,)
    )
    candidate = original.model_copy(
        update={
            "generated_at": NOW + timedelta(minutes=1),
            "active_decisions": candidate_decisions,
        }
    )

    with pytest.raises(SyncConflict, match="decision_change_requires_review"):
        repository.commit(
            sync_commit(
                cursor=2,
                content_hash=HASH_B,
                package=candidate,
                snapshots=(snapshot,),
            )
        )

    stored = repository.load_active_generation("project-1")
    assert stored is not None
    assert stored.generation_id == first.generation_id
    assert stored.context == original
    assert ProjectMemoryRepository(
        tmp_path / "project-memory.db"
    ).list_versions("project-1", "decision-2") == []


def test_commit_rejects_source_backed_addition_with_mismatched_reference(
    tmp_path: Path,
) -> None:
    repository = repository_at(tmp_path)
    repository.initialize()
    snapshot = active_snapshot()
    original = context(source_refs=(), active_decisions=())
    first = repository.commit(
        sync_commit(cursor=1, package=original, snapshots=(snapshot,))
    )
    addition = source_backed_decision("decision-2", snapshot).model_copy(
        update={
            "source_refs": (
                source_backed_reference(snapshot).model_copy(
                    update={"source_title": "forged"}
                ),
            )
        }
    )
    candidate = original.model_copy(
        update={
            "generated_at": NOW + timedelta(minutes=1),
            "active_decisions": (addition,),
        }
    )

    with pytest.raises(SyncConflict, match="context_conflict"):
        repository.commit(
            sync_commit(
                cursor=2,
                content_hash=HASH_B,
                package=candidate,
                snapshots=(snapshot,),
            )
        )

    stored = repository.load_active_generation("project-1")
    assert stored is not None
    assert stored.generation_id == first.generation_id
    assert stored.context == original


def test_commit_rejects_reusing_legacy_tombstoned_decision_id(
    tmp_path: Path,
) -> None:
    repository = repository_at(tmp_path)
    repository.initialize()
    snapshot = active_snapshot()
    original_decision = source_backed_decision("decision-1", snapshot)
    original = context(
        source_refs=(),
        active_decisions=(original_decision,),
    )
    repository.commit(
        sync_commit(cursor=1, package=original, snapshots=(snapshot,))
    )
    legacy_context = original.model_copy(
        update={
            "generated_at": NOW + timedelta(minutes=1),
            "active_decisions": (),
        }
    )
    database_path = tmp_path / "project-memory.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE project_contexts SET payload_json = ? WHERE project_id = ?",
            (legacy_context.model_dump_json(), "project-1"),
        )
        connection.execute(
            """
            UPDATE project_sync_generations
            SET context_json = ?
            WHERE project_id = ?
            """,
            (legacy_context.model_dump_json(), "project-1"),
        )
    before = repository.load_active_generation("project-1")
    assert before is not None
    versions_before = ProjectMemoryRepository(database_path).list_versions(
        "project-1", "decision-1"
    )
    reused = source_backed_decision("decision-1", snapshot).model_copy(
        update={"decision_text": "new decision text"}
    )
    candidate = legacy_context.model_copy(
        update={
            "generated_at": NOW + timedelta(minutes=2),
            "active_decisions": (reused,),
        }
    )

    with pytest.raises(SyncConflict, match="decision_change_requires_review"):
        repository.commit(
            sync_commit(
                cursor=2,
                content_hash=HASH_B,
                package=candidate,
                snapshots=(snapshot,),
            )
        )

    stored = repository.load_active_generation("project-1")
    assert stored is not None
    assert stored.generation_id == before.generation_id
    assert stored.source_cursor == before.source_cursor
    assert stored.context == legacy_context
    assert ProjectMemoryRepository(database_path).list_versions(
        "project-1", "decision-1"
    ) == versions_before
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM project_sync_audits WHERE project_id = ?",
            ("project-1",),
        ).fetchone() == (1,)


def test_commit_rejects_source_backed_addition_with_external_approval(
    tmp_path: Path,
) -> None:
    repository = repository_at(tmp_path)
    repository.initialize()
    snapshot = active_snapshot()
    original = context(source_refs=(), active_decisions=())
    first = repository.commit(
        sync_commit(cursor=1, package=original, snapshots=(snapshot,))
    )
    addition = source_backed_decision("decision-2", snapshot)
    approval = HumanApprovalRef(
        candidate_id="candidate-2",
        reviewer_id="owner-1",
        approved_at=NOW,
        reason="external approval",
        decision_text=addition.decision_text,
        permission_scope="project:demo",
    )
    candidate = original.model_copy(
        update={
            "generated_at": NOW + timedelta(minutes=1),
            "active_decisions": (
                addition.model_copy(update={"approval_ref": approval}),
            ),
        }
    )

    with pytest.raises(ValueError, match="external_approval_forbidden"):
        repository.commit(
            sync_commit(
                cursor=2,
                content_hash=HASH_B,
                package=candidate,
                snapshots=(snapshot,),
            )
        )

    stored = repository.load_active_generation("project-1")
    assert stored is not None
    assert stored.generation_id == first.generation_id


def test_commit_rejects_initial_decision_without_active_success(
    tmp_path: Path,
) -> None:
    repository = repository_at(tmp_path)
    repository.initialize()
    repository.commit(failed_only_sync_commit(cursor=1))

    with pytest.raises(SyncConflict, match="context_conflict"):
        repository.commit(
            failed_only_sync_commit(
                cursor=2,
                content_hash=HASH_B,
                package=context(
                    generated_at=NOW + timedelta(minutes=1),
                ),
            )
        )


def test_commit_rejects_initial_decision_without_current_success(
    tmp_path: Path,
) -> None:
    repository = repository_at(tmp_path)
    repository.initialize()
    repository.commit(failed_only_sync_commit(cursor=1))
    earlier_success = active_document_records(
        source_id="real-source-id",
        source_version="v1",
        source_content_hash=HASH_B,
        chunk_id=HASH_C,
        chunk_content_hash=HASH_B,
        observed_at=NOW,
    )

    with pytest.raises(SyncConflict, match="context_conflict"):
        repository.commit(
            sync_commit(
                cursor=2,
                content_hash=HASH_B,
                snapshots=(earlier_success[0],),
                states=(earlier_success[1],),
                protected_sources=(earlier_success[2],),
                protected_chunks=(earlier_success[3],),
            )
        )


def test_commit_rejects_initial_decision_with_permission_change(
    tmp_path: Path,
) -> None:
    repository = repository_at(tmp_path)
    repository.initialize()
    repository.commit(failed_only_sync_commit(cursor=1))
    other_reference = evidence_ref(permission_scope="project:other")
    other_decision = context().active_decisions[0].model_copy(
        update={"source_refs": (other_reference,)}
    )
    other_context = context(
        generated_at=NOW + timedelta(minutes=1),
        permission_scope="project:other",
        source_refs=(other_reference,),
        active_decisions=(other_decision,),
    )
    success_at = NOW + timedelta(minutes=1, seconds=1)
    active = active_document_records(
        source_id="real-source-id",
        source_version="v1",
        source_content_hash=HASH_B,
        chunk_id=HASH_C,
        chunk_content_hash=HASH_B,
        observed_at=success_at,
    )

    with pytest.raises(SyncConflict, match="permission_conflict"):
        repository.commit(
            sync_commit(
                cursor=2,
                content_hash=HASH_B,
                package=other_context,
                snapshots=(
                    active[0].model_copy(
                        update={"permission_scope": "project:other"}
                    ),
                ),
                states=(active[1],),
                protected_sources=(active[2],),
                protected_chunks=(active[3],),
            )
        )


def test_same_failed_cursor_and_hash_cannot_bootstrap_decisions(
    tmp_path: Path,
) -> None:
    repository = repository_at(tmp_path)
    repository.initialize()
    repository.commit(failed_only_sync_commit(cursor=1))
    repository.commit(
        failed_only_sync_commit(cursor=2, outcome="unchanged")
    )
    success_at = NOW + timedelta(minutes=1, seconds=1)
    active = active_document_records(
        source_id="real-source-id",
        source_version="v1",
        source_content_hash=HASH_B,
        chunk_id=HASH_C,
        chunk_content_hash=HASH_B,
        observed_at=success_at,
    )

    with pytest.raises(SyncConflict, match="decision_change_requires_review"):
        repository.commit(
            sync_commit(
                cursor=2,
                content_hash=HASH_A,
                snapshots=(active[0],),
                states=(active[1],),
                protected_sources=(active[2],),
                protected_chunks=(active[3],),
            )
        )


def test_commit_rejects_removing_decision_when_its_source_is_revoked(
    tmp_path: Path,
) -> None:
    snapshot = active_snapshot()
    reference = source_backed_reference(snapshot)
    decision = context().active_decisions[0].model_copy(
        update={"source_refs": (reference,)}
    )
    original = context(
        source_refs=(reference,),
        active_decisions=(decision,),
    )
    repository = repository_at(tmp_path)
    repository.initialize()
    repository.commit(
        sync_commit(cursor=1, package=original, snapshots=(snapshot,))
    )
    revoked_at = NOW + timedelta(minutes=1)

    with pytest.raises(SyncConflict, match="decision_change_requires_review"):
        repository.commit(
            sync_commit(
                cursor=2,
                content_hash=HASH_B,
                package=context(
                    generated_at=revoked_at,
                    source_refs=(),
                    active_decisions=(),
                ),
                snapshots=(),
                tombstones=(
                    SourceTombstone(
                        source_type=SyncSourceType.DOCUMENT,
                        source_id="real-source-id",
                        status=SourceSyncStatus.REVOKED,
                        occurred_at=revoked_at,
                        permission_scope="project:demo",
                    ),
                ),
                states=(
                    source_state(
                        source_version=None,
                        content_hash=None,
                        status=SourceSyncStatus.REVOKED,
                        last_attempt_at=revoked_at,
                        last_success_at=None,
                    ),
                ),
                protected_sources=(),
                protected_chunks=(),
            )
        )

    stored = repository.load_active_generation("project-1")
    assert stored is not None
    assert stored.context == original


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


def completion_claim(claimed) -> object:  # type: ignore[no-untyped-def]
    from companion_gateway.project.sync_models import RetrievalCompletionClaim

    return RetrievalCompletionClaim(
        request_id=claimed.request_id,
        request_epoch=claimed.request_epoch,
        attempt_count=claimed.attempt_count,
        lease_token=claimed.lease_token,
    )


def request_without_token(claimed) -> RetrievalRequest:  # type: ignore[no-untyped-def]
    return RetrievalRequest.model_validate(
        claimed.model_dump(exclude={"lease_token"})
    )


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
    repository.save_retrieval_request(retrieval_request())
    request = repository.claim_retrieval_requests(
        "project-1", now=NOW + timedelta(seconds=1), lease_seconds=300
    )[0]
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
                completion_claims=(completion_claim(request),),
            )
        )


def test_metadata_only_source_refresh_cannot_complete_retrieval(
    tmp_path: Path,
) -> None:
    repository = repository_at(tmp_path)
    repository.initialize()
    repository.commit(sync_commit(cursor=1, content_hash=HASH_A))
    repository.save_retrieval_request(retrieval_request())
    request = repository.claim_retrieval_requests(
        "project-1", now=NOW + timedelta(seconds=1), lease_seconds=300
    )[0]
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
                completion_claims=(completion_claim(request),),
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


def test_retrieval_request_rejects_changed_expected_generation(
    tmp_path: Path,
) -> None:
    repository = repository_at(tmp_path)
    repository.initialize()
    repository.commit(sync_commit(cursor=1, content_hash=HASH_A))

    with pytest.raises(SyncConflict, match="retrieval_generation_changed"):
        repository.save_retrieval_request(
            retrieval_request(),
            expected_generation_id="generation-stale",
        )

    assert repository.list_retrieval_requests("project-1") == ()


def test_unchanged_generation_cannot_complete_pending_retrieval(
    tmp_path: Path,
) -> None:
    repository = repository_at(tmp_path)
    repository.initialize()
    repository.commit(sync_commit(cursor=1, content_hash=HASH_A))
    repository.save_retrieval_request(retrieval_request())
    request = repository.claim_retrieval_requests(
        "project-1", now=NOW + timedelta(seconds=1), lease_seconds=300
    )[0]

    with pytest.raises(SyncConflict, match="retrieval_evidence_missing"):
        repository.commit(
            sync_commit(
                cursor=2,
                content_hash=HASH_A,
                completion_claims=(completion_claim(request),),
                outcome="unchanged",
            )
        )

    active = repository.load_active_generation("project-1")
    assert active is not None
    assert active.source_cursor == 1
    assert repository.get_retrieval_request(
        "project-1", request.request_id
    ) == request_without_token(request)


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
    repository.save_retrieval_request(
        retrieval_request(
            source_id_hashes=(
                source_id_hash("real-source-id"),
                source_id_hash("other-source-id"),
            )
        )
    )
    request = repository.claim_retrieval_requests(
        "project-1", now=NOW + timedelta(seconds=1), lease_seconds=300
    )[0]

    with pytest.raises(SyncConflict, match="retrieval_evidence_missing"):
        repository.commit(
            sync_commit(
                cursor=2,
                content_hash=HASH_B,
                completion_claims=(completion_claim(request),),
            )
        )

    active = repository.load_active_generation("project-1")
    assert active is not None
    assert active.source_cursor == 1
    assert repository.get_retrieval_request(
        "project-1", request.request_id
    ) == request_without_token(request)


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
    listed = repository.list_retrieval_requests("project-1")
    assert listed == (
        RetrievalRequest.model_validate(
            reclaimed.model_dump(exclude={"lease_token"})
        ),
    )
    with pytest.raises(ValueError, match="completed"):
        repository.compare_and_set_retrieval_request(
            "project-1",
            "request-1",
            frozenset({RetrievalRequestStatus.IN_PROGRESS}),
            RetrievalRequestStatus.COMPLETED,
            completed_at=NOW,
        )


def test_retrieval_claim_uses_fencing_token_and_rejects_stale_lease(
    tmp_path: Path,
) -> None:
    repository = repository_at(tmp_path)
    repository.initialize()
    repository.commit(sync_commit(cursor=1, content_hash=HASH_A))
    repository.save_retrieval_request(retrieval_request())
    first = repository.claim_retrieval_requests(
        "project-1",
        now=NOW + timedelta(seconds=1),
        lease_seconds=60,
    )[0]
    second = repository.claim_retrieval_requests(
        "project-1",
        now=NOW + timedelta(seconds=62),
        lease_seconds=60,
    )[0]
    assert first.request_epoch == second.request_epoch == 1
    assert first.attempt_count == 1
    assert second.attempt_count == 2
    assert first.lease_token != second.lease_token
    updated = active_document_records(
        source_id="real-source-id",
        source_version="v2",
        source_content_hash=HASH_D,
        chunk_id=HASH_E,
        chunk_content_hash=HASH_F,
        observed_at=NOW + timedelta(minutes=1),
    )

    with pytest.raises(SyncConflict, match="retrieval_claim_invalid"):
        repository.commit(
            sync_commit(
                cursor=2,
                content_hash=HASH_B,
                snapshots=(updated[0],),
                states=(updated[1],),
                protected_sources=(updated[2],),
                protected_chunks=(updated[3],),
                completion_claims=(completion_claim(first),),
            )
        )

    result = repository.commit(
        sync_commit(
            cursor=2,
            content_hash=HASH_B,
            snapshots=(updated[0],),
            states=(updated[1],),
            protected_sources=(updated[2],),
            protected_chunks=(updated[3],),
            completion_claims=(completion_claim(second),),
        )
    )
    assert result.completed_retrieval_request_ids == ("request-1",)


def test_retrieval_completion_requires_claim_and_unexpired_lease(
    tmp_path: Path,
) -> None:
    repository = repository_at(tmp_path)
    repository.initialize()
    repository.commit(sync_commit(cursor=1, content_hash=HASH_A))
    repository.save_retrieval_request(retrieval_request())
    updated = active_document_records(
        source_id="real-source-id",
        source_version="v2",
        source_content_hash=HASH_D,
        chunk_id=HASH_E,
        chunk_content_hash=HASH_F,
        observed_at=NOW + timedelta(minutes=1),
    )

    with pytest.raises(SyncConflict, match="retrieval_claim_required"):
        repository.commit(
            sync_commit(
                cursor=2,
                content_hash=HASH_B,
                snapshots=(updated[0],),
                states=(updated[1],),
                protected_sources=(updated[2],),
                protected_chunks=(updated[3],),
                completed_request_ids=("request-1",),
            )
        )

    claimed = repository.claim_retrieval_requests(
        "project-1",
        now=NOW + timedelta(seconds=1),
        lease_seconds=30,
    )[0]
    expired_candidate = sync_commit(
        cursor=2,
        content_hash=HASH_B,
        snapshots=(updated[0],),
        states=(updated[1],),
        protected_sources=(updated[2],),
        protected_chunks=(updated[3],),
        completion_claims=(completion_claim(claimed),),
    )
    expired_candidate = replace(
        expired_candidate,
        audit=expired_candidate.audit.model_copy(
            update={
                "started_at": NOW + timedelta(seconds=31),
                "finished_at": NOW + timedelta(seconds=31),
            }
        ),
    )
    with pytest.raises(SyncConflict, match="retrieval_claim_expired"):
        repository.commit(expired_candidate)


def test_expired_deterministic_retrieval_requeues_with_new_epoch(
    tmp_path: Path,
) -> None:
    repository = repository_at(tmp_path)
    repository.initialize()
    repository.commit(sync_commit(cursor=1, content_hash=HASH_A))
    first = repository.save_retrieval_request(
        retrieval_request(expires_at=NOW + timedelta(minutes=1))
    )
    assert repository.claim_retrieval_requests(
        "project-1",
        now=NOW + timedelta(minutes=2),
        lease_seconds=60,
    ) == ()

    renewed = repository.save_retrieval_request(
        retrieval_request(
            created_at=NOW + timedelta(minutes=2),
            expires_at=NOW + timedelta(minutes=32),
        ),
        expected_generation_id="generation-1",
    )

    assert renewed.request_id == first.request_id
    assert renewed.request_epoch == first.request_epoch + 1
    assert renewed.status is RetrievalRequestStatus.PENDING
    assert renewed.attempt_count == 0
    assert renewed.lease_expires_at is None
    claimed = repository.claim_retrieval_requests(
        "project-1",
        now=NOW + timedelta(minutes=2, seconds=1),
        lease_seconds=300,
    )[0]
    assert claimed.request_epoch == renewed.request_epoch
    updated = active_document_records(
        source_id="real-source-id",
        source_version="v2",
        source_content_hash=HASH_D,
        chunk_id=HASH_E,
        chunk_content_hash=HASH_F,
        observed_at=NOW + timedelta(minutes=3),
    )

    result = repository.commit(
        sync_commit(
            cursor=2,
            content_hash=HASH_B,
            snapshots=(updated[0],),
            states=(updated[1],),
            protected_sources=(updated[2],),
            protected_chunks=(updated[3],),
            completion_claims=(completion_claim(claimed),),
        )
    )
    assert result.completed_retrieval_request_ids == (renewed.request_id,)


def test_same_cursor_rejects_different_completion_claims(tmp_path: Path) -> None:
    repository = repository_at(tmp_path)
    repository.initialize()
    repository.commit(sync_commit(cursor=1, content_hash=HASH_A))
    first_request = repository.save_retrieval_request(retrieval_request())
    second_request = repository.save_retrieval_request(
        retrieval_request(request_id="request-2", query_hash=HASH_B)
    )
    claims = repository.claim_retrieval_requests(
        "project-1",
        now=NOW + timedelta(seconds=1),
        lease_seconds=300,
    )
    claimed = {item.request_id: item for item in claims}
    updated = active_document_records(
        source_id="real-source-id",
        source_version="v2",
        source_content_hash=HASH_D,
        chunk_id=HASH_E,
        chunk_content_hash=HASH_F,
        observed_at=NOW + timedelta(minutes=1),
    )
    first_commit = sync_commit(
        cursor=2,
        content_hash=HASH_B,
        snapshots=(updated[0],),
        states=(updated[1],),
        protected_sources=(updated[2],),
        protected_chunks=(updated[3],),
        completion_claims=(completion_claim(claimed[first_request.request_id]),),
    )
    repository.commit(first_commit)

    assert repository.commit(first_commit).outcome == "applied"
    with pytest.raises(SyncConflict, match="completion_claims_conflict"):
        repository.commit(
            sync_commit(
                cursor=2,
                content_hash=HASH_B,
                snapshots=(updated[0],),
                states=(updated[1],),
                protected_sources=(updated[2],),
                protected_chunks=(updated[3],),
                completion_claims=(
                    completion_claim(claimed[second_request.request_id]),
                ),
            )
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
    repository.save_retrieval_request(retrieval_request())
    request = repository.claim_retrieval_requests(
        "project-1", now=NOW + timedelta(seconds=1), lease_seconds=300
    )[0]

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
            completion_claims=(completion_claim(request),),
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
    repository.save_retrieval_request(retrieval_request())
    request = repository.claim_retrieval_requests(
        "project-1", now=NOW + timedelta(seconds=1), lease_seconds=300
    )[0]
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
                completion_claims=(completion_claim(request),),
            )
        )

    active = repository.load_active_generation("project-1")
    assert active is not None
    assert active.source_cursor == 1
    assert repository.get_retrieval_request(
        "project-1", request.request_id
    ) == request_without_token(request)
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


def test_decision_version_requires_exactly_one_migration_basis() -> None:
    migrated = DecisionVersion(
        decision_id="decision-1",
        version=3,
        replaces_version=2,
        change_reason="拆分组合决策",
        decision_text="采用方案 A",
        proposed_by="reviewer-1",
        status=DecisionStatus.SUPERSEDED,
        migration_audit_id="mig-1",
    )

    assert migrated.approved_by is None
    assert migrated.approved_at is None
    assert migrated.evidence_refs == ()
    assert migrated.approval_ref is None
    with pytest.raises(ValueError, match="migration audit"):
        DecisionVersion.model_validate(
            {**migrated.model_dump(), "status": DecisionStatus.ACTIVE}
        )
    with pytest.raises(ValueError, match="exactly one"):
        DecisionVersion.model_validate(
            {**migrated.model_dump(), "evidence_refs": (evidence_ref(),)}
        )


def test_decision_split_preview_is_redacted_and_token_is_stable(
    tmp_path: Path,
) -> None:
    repository, _, active_context = decision_split_state(tmp_path)

    first = repository.preview_decision_split_migration("project-1")
    second = repository.preview_decision_split_migration("project-1")

    assert first == second
    assert first.project_id == "project-1"
    assert first.active_generation_id == "generation-2"
    assert first.legacy.decision_id == "decision-1"
    assert first.legacy.version == 2
    assert first.retirement_version == 3
    assert tuple(item.decision_id for item in first.replacements) == (
        "decision-split-1",
        "decision-split-2",
    )
    assert all(item.version == 1 for item in first.replacements)
    assert len(first.precondition_token) == 64
    assert first.before_context_hash != first.after_context_hash
    serialized = first.model_dump_json()
    assert active_context.active_decisions[0].topic in serialized
    assert "decision_text" not in serialized
    assert "source_id" not in serialized
    assert "reviewer" not in serialized


def test_decision_split_apply_preserves_history_and_audits_context_change(
    tmp_path: Path,
) -> None:
    repository, _, _ = decision_split_state(tmp_path)
    memory = ProjectMemoryRepository(tmp_path / "project-memory.db")
    before_versions = memory.list_versions("project-1", "decision-1")
    preview = repository.preview_decision_split_migration("project-1")

    audit = repository.apply_decision_split_migration(
        project_id="project-1",
        precondition_token=preview.precondition_token,
        reviewer_id="reviewer-1",
        reason="将组合决策拆分为两个独立来源决策",
        migrated_at=NOW + timedelta(minutes=2),
    )

    active = repository.load_active_generation("project-1")
    stored_context = memory.get_context("project-1")
    versions = memory.list_versions("project-1", "decision-1")
    assert active is not None
    assert stored_context == active.context
    assert tuple(item.decision_id for item in active.context.active_decisions) == (
        "decision-split-1",
        "decision-split-2",
    )
    assert versions[:2] == before_versions
    assert [item.version for item in versions] == [1, 2, 3]
    assert versions[-1].status is DecisionStatus.SUPERSEDED
    assert versions[-1].replaces_version == 2
    assert versions[-1].decision_text == before_versions[-1].decision_text
    assert versions[-1].migration_audit_id == audit.migration_id
    assert versions[-1].evidence_refs == ()
    assert versions[-1].approval_ref is None
    assert versions[-1].approved_by is None
    assert versions[-1].approved_at is None
    assert audit.project_id == "project-1"
    assert audit.legacy_decision_id == "decision-1"
    assert audit.base_version == 2
    assert audit.retirement_version == 3
    assert audit.active_generation_id == "generation-2"
    assert audit.reviewer_id == "reviewer-1"
    assert audit.reason == "将组合决策拆分为两个独立来源决策"
    assert audit.migrated_at == NOW + timedelta(minutes=2)
    assert audit.replacement_decision_ids == (
        "decision-split-1",
        "decision-split-2",
    )
    assert audit.before_context_hash == preview.before_context_hash
    assert audit.after_context_hash == preview.after_context_hash
    assert repository.get_decision_split_migration_audit(audit.migration_id) == audit


def test_decision_split_apply_replay_is_idempotent(tmp_path: Path) -> None:
    repository, _, _ = decision_split_state(tmp_path)
    preview = repository.preview_decision_split_migration("project-1")
    arguments = {
        "project_id": "project-1",
        "precondition_token": preview.precondition_token,
        "reviewer_id": "reviewer-1",
        "reason": "拆分组合决策",
        "migrated_at": NOW + timedelta(minutes=2),
    }

    first = repository.apply_decision_split_migration(**arguments)
    replayed = repository.apply_decision_split_migration(**arguments)

    assert replayed == first
    with sqlite3.connect(tmp_path / "project-memory.db") as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM project_decision_migration_audits"
        ).fetchone() == (1,)
        assert connection.execute(
            """
            SELECT COUNT(*) FROM project_versions
            WHERE project_id = 'project-1'
              AND decision_id = 'decision-1'
              AND version = 3
            """
        ).fetchone() == (1,)


@pytest.mark.parametrize(
    "invalid_state",
    [
        "card_count",
        "legacy_version",
        "legacy_linkage",
        "replacement_version",
        "replacement_linkage",
        "replacement_status",
        "replacement_source",
        "duplicate_topic",
    ],
)
def test_decision_split_preview_rejects_invalid_candidate_states(
    tmp_path: Path,
    invalid_state: str,
) -> None:
    repository, _, active_context = decision_split_state(tmp_path)
    memory = ProjectMemoryRepository(tmp_path / "project-memory.db")
    if invalid_state == "card_count":
        replace_split_context(
            tmp_path,
            active_context.model_copy(
                update={"active_decisions": active_context.active_decisions[:2]}
            ),
        )
    elif invalid_state == "legacy_version":
        with sqlite3.connect(tmp_path / "project-memory.db") as connection:
            connection.execute(
                """
                DELETE FROM project_versions
                WHERE project_id = ? AND decision_id = ? AND version = 2
                """,
                ("project-1", "decision-1"),
            )
    elif invalid_state == "legacy_linkage":
        legacy = memory.list_versions("project-1", "decision-1")[-1]
        memory.save_version(
            "project-1",
            legacy.model_copy(update={"replaces_version": None}),
        )
    elif invalid_state == "replacement_version":
        replacement = memory.list_versions("project-1", "decision-split-1")[-1]
        extra = replacement.model_copy(
            update={
                "version": 2,
                "replaces_version": 1,
                "status": DecisionStatus.SUPERSEDED,
            }
        )
        memory.save_version("project-1", extra)
    elif invalid_state == "replacement_linkage":
        replacement = memory.list_versions("project-1", "decision-split-1")[-1]
        memory.save_version(
            "project-1",
            replacement.model_copy(update={"replaces_version": 1}),
        )
    elif invalid_state == "replacement_status":
        changed = active_context.active_decisions[1].model_copy(
            update={"status": DecisionStatus.PROPOSED}
        )
        replace_split_context(
            tmp_path,
            active_context.model_copy(
                update={
                    "active_decisions": (
                        active_context.active_decisions[0],
                        changed,
                        active_context.active_decisions[2],
                    )
                }
            ),
        )
    elif invalid_state == "replacement_source":
        replacement = memory.list_versions("project-1", "decision-split-1")[-1]
        approval = HumanApprovalRef(
            candidate_id="approved-replacement",
            reviewer_id="owner-1",
            approved_at=NOW,
            reason=replacement.change_reason,
            decision_text=replacement.decision_text,
            permission_scope="project:demo",
        )
        approved = replacement.model_copy(
            update={
                "evidence_refs": (),
                "approval_ref": approval,
                "approved_by": approval.reviewer_id,
                "approved_at": approval.approved_at,
            }
        )
        memory.save_version("project-1", approved)
    else:
        duplicate_topic = active_context.active_decisions[1].topic.upper()
        changed = active_context.active_decisions[2].model_copy(
            update={"topic": f" {duplicate_topic} "}
        )
        replace_split_context(
            tmp_path,
            active_context.model_copy(
                update={
                    "active_decisions": (
                        active_context.active_decisions[0],
                        active_context.active_decisions[1],
                        changed,
                    )
                }
            ),
        )

    with pytest.raises(SyncConflict, match="decision_split_candidate_unavailable"):
        repository.preview_decision_split_migration("project-1")


def test_decision_split_preview_rejects_context_divergence(tmp_path: Path) -> None:
    repository, _, active_context = decision_split_state(tmp_path)
    replace_split_context(
        tmp_path,
        active_context.model_copy(update={"project_name": "不同的项目上下文"}),
        update_project_context=False,
    )

    with pytest.raises(SyncConflict, match="decision_split_context_diverged"):
        repository.preview_decision_split_migration("project-1")


def test_decision_split_preview_rejects_legacy_proposed_conflict(
    tmp_path: Path,
) -> None:
    repository, memory, _ = decision_split_state(tmp_path)
    memory.propose_conflict_from_statement(
        "project-1",
        "发布方案再改成方案 C",
        proposed_decision_text="采用方案 C",
        now=NOW + timedelta(minutes=2),
    )

    with pytest.raises(SyncConflict, match="decision_split_candidate_unavailable"):
        repository.preview_decision_split_migration("project-1")


@pytest.mark.parametrize(
    "corruption",
    ["missing_legacy_v1", "sql_payload_version_mismatch", "extra_replacement_version"],
)
def test_decision_split_rejects_corrupt_version_history_for_preview_and_apply(
    tmp_path: Path,
    corruption: str,
) -> None:
    repository, _, _ = decision_split_state(tmp_path)
    preview = repository.preview_decision_split_migration("project-1")
    database_path = tmp_path / "project-memory.db"
    with sqlite3.connect(database_path) as connection:
        if corruption == "missing_legacy_v1":
            connection.execute(
                """
                DELETE FROM project_versions
                WHERE project_id = ? AND decision_id = ? AND version = 1
                """,
                ("project-1", "decision-1"),
            )
        elif corruption == "sql_payload_version_mismatch":
            connection.execute(
                """
                UPDATE project_versions SET version = 7
                WHERE project_id = ? AND decision_id = ? AND version = 2
                """,
                ("project-1", "decision-1"),
            )
        else:
            payload = connection.execute(
                """
                SELECT payload_json FROM project_versions
                WHERE project_id = ? AND decision_id = ? AND version = 1
                """,
                ("project-1", "decision-split-1"),
            ).fetchone()[0]
            connection.execute(
                """
                INSERT INTO project_versions(
                    project_id, decision_id, version, payload_json
                ) VALUES (?, ?, ?, ?)
                """,
                ("project-1", "decision-split-1", 2, payload),
            )
    corrupted = database_dump(database_path)

    with pytest.raises(SyncConflict, match="decision_split_candidate_unavailable"):
        repository.preview_decision_split_migration("project-1")
    with pytest.raises(SyncConflict, match="decision_split_candidate_unavailable"):
        repository.apply_decision_split_migration(
            project_id="project-1",
            precondition_token=preview.precondition_token,
            reviewer_id="reviewer-1",
            reason="拆分组合决策",
            migrated_at=NOW + timedelta(minutes=2),
        )

    assert database_dump(database_path) == corrupted


@pytest.mark.parametrize(
    ("trigger_table", "trigger_event"),
    [
        ("project_versions", "INSERT"),
        ("project_decision_migration_audits", "INSERT"),
        ("project_contexts", "UPDATE"),
        ("project_sync_generations", "UPDATE"),
    ],
)
def test_decision_split_failure_rolls_back_every_step(
    tmp_path: Path,
    trigger_table: str,
    trigger_event: str,
) -> None:
    repository, _, _ = decision_split_state(tmp_path)
    preview = repository.preview_decision_split_migration("project-1")
    database_path = tmp_path / "project-memory.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            f"""
            CREATE TRIGGER fail_decision_split_step
            BEFORE {trigger_event} ON {trigger_table}
            BEGIN
                SELECT RAISE(ABORT, 'simulated_decision_split_failure');
            END
            """
        )
    before = database_dump(database_path)

    with pytest.raises(
        sqlite3.IntegrityError,
        match="simulated_decision_split_failure",
    ):
        repository.apply_decision_split_migration(
            project_id="project-1",
            precondition_token=preview.precondition_token,
            reviewer_id="reviewer-1",
            reason="拆分组合决策",
            migrated_at=NOW + timedelta(minutes=2),
        )

    assert database_dump(database_path) == before


def test_decision_split_apply_rejects_stale_token_without_writes(
    tmp_path: Path,
) -> None:
    repository, _, _ = decision_split_state(tmp_path)
    database_path = tmp_path / "project-memory.db"
    before = database_dump(database_path)

    with pytest.raises(SyncConflict, match="decision_split_precondition_stale"):
        repository.apply_decision_split_migration(
            project_id="project-1",
            precondition_token="0" * 64,
            reviewer_id="reviewer-1",
            reason="拆分组合决策",
            migrated_at=NOW + timedelta(minutes=2),
        )

    assert database_dump(database_path) == before


def test_first_two_card_sync_after_migration_is_applied_then_unchanged(
    tmp_path: Path,
) -> None:
    repository, _, _ = decision_split_state(tmp_path)
    preview = repository.preview_decision_split_migration("project-1")
    repository.apply_decision_split_migration(
        project_id="project-1",
        precondition_token=preview.precondition_token,
        reviewer_id="reviewer-1",
        reason="拆分组合决策",
        migrated_at=NOW + timedelta(minutes=2),
    )
    migrated = repository.load_active_generation("project-1")
    assert migrated is not None
    refreshed_context = migrated.context.model_copy(
        update={"generated_at": NOW + timedelta(minutes=3)}
    )

    first = repository.commit(
        sync_commit(
            cursor=3,
            content_hash=HASH_C,
            package=refreshed_context,
        )
    )
    second = repository.commit(
        sync_commit(
            cursor=4,
            content_hash=HASH_C,
            package=refreshed_context.model_copy(
                update={"generated_at": NOW + timedelta(minutes=4)}
            ),
            outcome="unchanged",
        )
    )

    assert first.outcome == "applied"
    assert second.outcome == "unchanged"
    active = repository.load_active_generation("project-1")
    assert active is not None
    assert active.source_cursor == 4
    assert tuple(item.decision_id for item in active.context.active_decisions) == (
        "decision-split-1",
        "decision-split-2",
    )


def test_sync_and_decision_split_migration_serialize_without_lost_update(
    tmp_path: Path,
) -> None:
    repository, _, active_context = decision_split_state(tmp_path)
    preview = repository.preview_decision_split_migration("project-1")
    source_backed_legacy = context().active_decisions[0]
    incoming_context = active_context.model_copy(
        update={
            "generated_at": NOW + timedelta(minutes=3),
            "active_decisions": (
                source_backed_legacy,
                *active_context.active_decisions[1:],
            ),
        }
    )
    incoming_sync = sync_commit(
        cursor=3,
        content_hash=HASH_C,
        package=incoming_context,
    )
    barrier = threading.Barrier(2)
    results: list[tuple[str, object]] = []

    def migrate() -> None:
        barrier.wait(timeout=5)
        try:
            results.append(
                (
                    "migration",
                    repository.apply_decision_split_migration(
                        project_id="project-1",
                        precondition_token=preview.precondition_token,
                        reviewer_id="reviewer-1",
                        reason="拆分组合决策",
                        migrated_at=NOW + timedelta(minutes=2),
                    ),
                )
            )
        except BaseException as exc:
            results.append(("migration_error", exc))

    def synchronize() -> None:
        barrier.wait(timeout=5)
        try:
            results.append(("sync", repository.commit(incoming_sync)))
        except BaseException as exc:
            results.append(("sync_error", exc))

    workers = [threading.Thread(target=migrate), threading.Thread(target=synchronize)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)

    assert all(not worker.is_alive() for worker in workers)
    successes = [name for name, _ in results if not name.endswith("_error")]
    errors = [value for name, value in results if name.endswith("_error")]
    assert len(successes) == 1
    assert len(errors) == 1
    assert isinstance(errors[0], SyncConflict)
    active = repository.load_active_generation("project-1")
    assert active is not None
    with sqlite3.connect(tmp_path / "project-memory.db") as connection:
        audit_count = connection.execute(
            "SELECT COUNT(*) FROM project_decision_migration_audits"
        ).fetchone()[0]
    if successes == ["migration"]:
        assert len(active.context.active_decisions) == 2
        assert active.generation_id == "generation-2"
        assert audit_count == 1
    else:
        assert len(active.context.active_decisions) == 3
        assert active.generation_id == "generation-3"
        assert audit_count == 0


def test_review_and_decision_split_migration_serialize_without_lost_update(
    tmp_path: Path,
) -> None:
    repository, memory, _ = decision_split_state(tmp_path)
    preview = repository.preview_decision_split_migration("project-1")
    conflict, _ = memory.propose_conflict_from_statement(
        "project-1",
        "发布方案改成方案 C",
        proposed_decision_text="采用方案 C",
        now=NOW + timedelta(minutes=2),
    )
    barrier = threading.Barrier(2)
    results: list[tuple[str, object]] = []

    def migrate() -> None:
        barrier.wait(timeout=5)
        try:
            results.append(
                (
                    "migration",
                    repository.apply_decision_split_migration(
                        project_id="project-1",
                        precondition_token=preview.precondition_token,
                        reviewer_id="migration-reviewer",
                        reason="拆分组合决策",
                        migrated_at=NOW + timedelta(minutes=3),
                    ),
                )
            )
        except BaseException as exc:
            results.append(("migration_error", exc))

    def review() -> None:
        barrier.wait(timeout=5)
        try:
            results.append(
                (
                    "review",
                    memory.review_conflict(
                        conflict.candidate_id,
                        reviewer_id="decision-reviewer",
                        action="accept",
                        change_reason="批准方案 C",
                        now=NOW + timedelta(minutes=3),
                    ),
                )
            )
        except BaseException as exc:
            results.append(("review_error", exc))

    workers = [threading.Thread(target=migrate), threading.Thread(target=review)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)

    assert all(not worker.is_alive() for worker in workers)
    assert [name for name, _ in results if name == "review"] == ["review"]
    migration_errors = [
        value for name, value in results if name == "migration_error"
    ]
    assert len(migration_errors) == 1
    assert isinstance(migration_errors[0], SyncConflict)
    context_after = memory.get_context("project-1")
    versions = ProjectMemoryRepository(
        tmp_path / "project-memory.db"
    ).list_versions("project-1", "decision-1")
    assert context_after.active_decisions[0].decision_text == "采用方案 C"
    assert versions[-1].version == 3
    assert versions[-1].status is DecisionStatus.ACTIVE
    with sqlite3.connect(tmp_path / "project-memory.db") as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM project_decision_migration_audits"
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
