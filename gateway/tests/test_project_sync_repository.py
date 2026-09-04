import hashlib
import json
import sqlite3
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


def source_id_hash(source_id: str) -> str:
    return hashlib.sha256(source_id.encode()).hexdigest()


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


def sync_commit(
    *,
    cursor: int,
    content_hash: str = HASH_A,
    sync_id: str | None = None,
    package: ProjectContextPackage | None = None,
    snapshots: tuple[SourceSnapshot, ...] | None = None,
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
        source_counts_by_status={item.status: 1 for item in chosen_states},
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


def test_retrieval_requests_use_compare_and_set_transitions(tmp_path: Path) -> None:
    repository = repository_at(tmp_path)
    repository.initialize()
    request = retrieval_request()

    assert repository.save_retrieval_request(request) == request
    assert repository.save_retrieval_request(request) == request
    assert repository.compare_and_set_retrieval_request(
        "project-1",
        "request-1",
        frozenset({RetrievalRequestStatus.PENDING}),
        RetrievalRequestStatus.IN_PROGRESS,
    )
    assert not repository.compare_and_set_retrieval_request(
        "project-1",
        "request-1",
        frozenset({RetrievalRequestStatus.PENDING}),
        RetrievalRequestStatus.EXPIRED,
    )
    in_progress = request.model_copy(update={"status": "in_progress"})
    assert repository.get_retrieval_request("project-1", "request-1") == in_progress
    assert repository.list_retrieval_requests("project-1") == (in_progress,)
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
    request = retrieval_request()
    repository.save_retrieval_request(request)

    with pytest.raises(SyncConflict, match="retrieval_request_conflict"):
        repository.save_retrieval_request(
            request.model_copy(update={"status": RetrievalRequestStatus.IN_PROGRESS})
        )

    assert repository.get_retrieval_request("project-1", "request-1") == request


def test_completed_retrieval_request_commits_with_evidence(tmp_path: Path) -> None:
    repository = repository_at(tmp_path)
    repository.initialize()
    pending = retrieval_request()
    assert repository.save_retrieval_request(pending) == pending
    assert repository.compare_and_set_retrieval_request(
        pending.project_id,
        pending.request_id,
        frozenset({RetrievalRequestStatus.PENDING}),
        RetrievalRequestStatus.IN_PROGRESS,
    )
    request = pending.model_copy(update={"status": RetrievalRequestStatus.IN_PROGRESS})

    repository.commit(
        sync_commit(cursor=1, completed_request_ids=(request.request_id,))
    )

    completed = repository.get_retrieval_request("project-1", request.request_id)
    assert completed is not None
    assert completed.status is RetrievalRequestStatus.COMPLETED
    assert completed.completed_at == NOW + timedelta(seconds=1)
    assert repository.load_active_generation("project-1") is not None


def test_missing_retrieval_evidence_rolls_back_generation_and_request(
    tmp_path: Path,
) -> None:
    repository = repository_at(tmp_path)
    repository.initialize()
    request = retrieval_request(source_id_hashes=("d" * 64,))
    repository.save_retrieval_request(request)
    assert repository.compare_and_set_retrieval_request(
        request.project_id,
        request.request_id,
        frozenset({RetrievalRequestStatus.PENDING}),
        RetrievalRequestStatus.IN_PROGRESS,
    )
    request = request.model_copy(update={"status": RetrievalRequestStatus.IN_PROGRESS})

    with pytest.raises(SyncConflict, match="retrieval_evidence_missing"):
        repository.commit(
            sync_commit(cursor=1, completed_request_ids=(request.request_id,))
        )

    assert repository.load_active_generation("project-1") is None
    assert repository.get_retrieval_request("project-1", request.request_id) == request
    with sqlite3.connect(tmp_path / "project-memory.db") as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM project_source_states"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM project_evidence_chunks"
        ).fetchone() == (0,)


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
        "绝不能明文落盘的正文",
    ):
        assert secret.encode() not in stored_bytes
