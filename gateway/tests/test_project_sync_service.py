from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from companion_gateway.project.auth import (
    ProjectApiPrincipal,
    ProjectAuthorizationError,
)
from companion_gateway.project.index import ProjectSnapshotRegistry
from companion_gateway.project.models import (
    DecisionCard,
    EvidenceRef,
    ProjectContextPackage,
    SourcedFact,
)
from companion_gateway.project.repository import ProjectMemoryRepository
from companion_gateway.project.protection_state import (
    ProtectionStateError,
    initialize_repository_protection,
)
from companion_gateway.project.sync_models import (
    EvidenceChunk,
    ProjectSyncHealth,
    RetrievalCompletionClaim,
    RetrievalRequest,
    RetrievalRequestStatus,
    SourceErrorType,
    SourceSnapshot,
    SourceSyncStatus,
    SyncEnvelope,
    SyncSourceType,
)
from companion_gateway.project.sync_repository import (
    ProjectSyncRepository,
    SyncCommit,
)
from companion_gateway.project.sync_service import (
    ProjectSourceUnavailable,
    ProjectSyncService,
    ProjectSyncValidationError,
    compute_envelope_content_hash,
)


NOW = datetime(2026, 9, 5, 8, 0, tzinfo=UTC)
PROJECT_ID = "project-1"
PERMISSION_SCOPE = "project:demo"


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


DOCUMENT_REF = EvidenceRef(
    source_type="document",
    source_id="doc-1",
    source_title="Decision document",
    source_url="dingtalk://doc/doc-1",
    source_time=NOW - timedelta(minutes=10),
    excerpt="Use plan B",
    permission_scope=PERMISSION_SCOPE,
)
TASK_REF = EvidenceRef(
    source_type="task",
    source_id="task-1",
    source_title="Delivery task",
    source_url="dingtalk://task/task-1",
    source_time=NOW - timedelta(minutes=5),
    excerpt="Deliver prototype",
    permission_scope=PERMISSION_SCOPE,
)
PRINCIPAL = ProjectApiPrincipal(
    principal_id="qwenwork",
    token_sha256="a" * 64,
    project_ids=frozenset({PROJECT_ID}),
    permission_scopes=frozenset({PERMISSION_SCOPE}),
)


class ReversibleProtector:
    protector_version = "test-reversible-v1"

    def __init__(self) -> None:
        self.protected_plaintexts: list[bytes] = []
        self.fail_protect = False
        self.fail_unprotect = False

    def protect(self, project_id: str, plaintext: bytes) -> bytes:
        assert plaintext
        if self.fail_protect:
            raise RuntimeError("protect_failed")
        self.protected_plaintexts.append(plaintext)
        return b"protected:" + project_id.encode("utf-8") + b":" + plaintext

    def unprotect(self, project_id: str, protected: bytes) -> bytes:
        if self.fail_unprotect:
            raise RuntimeError("unprotect_failed")
        prefix = b"protected:" + project_id.encode("utf-8") + b":"
        if not protected.startswith(prefix):
            raise RuntimeError("wrong_project")
        return protected[len(prefix) :]


class CommitFailingRepository(ProjectSyncRepository):
    def commit(self, candidate: SyncCommit):  # type: ignore[no-untyped-def]
        raise RuntimeError("commit_failed")


def evidence_chunk(
    *,
    source_id: str = "doc-1",
    version: str = "v1",
    ordinal: int = 0,
    text: str = "Use plan B for the terminal rollout.",
) -> EvidenceChunk:
    start_offset = ordinal * 100
    end_offset = start_offset + len(text)
    chunk_id = hashlib.sha256(
        json.dumps(
            {
                "end_offset": end_offset,
                "heading_path": ("Decision",),
                "ordinal": ordinal,
                "source_id": source_id,
                "start_offset": start_offset,
                "text": text,
                "version": version,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return EvidenceChunk(
        chunk_id=chunk_id,
        source_id=source_id,
        source_version=version,
        ordinal=ordinal,
        heading_path=("Decision",),
        text=text,
        start_offset=start_offset,
        end_offset=end_offset,
        content_hash=digest(text),
    )


def active_document(
    *,
    fetched_at: datetime = NOW,
    chunks: tuple[EvidenceChunk, ...] | None = None,
) -> SourceSnapshot:
    source_chunks = chunks or (evidence_chunk(),)
    return SourceSnapshot(
        source_type=SyncSourceType.DOCUMENT,
        source_id="doc-1",
        source_title="Decision document",
        source_url="dingtalk://doc/doc-1",
        source_version="v1",
        source_time=DOCUMENT_REF.source_time,
        fetched_at=fetched_at,
        permission_scope=PERMISSION_SCOPE,
        permission_hash=digest("doc-permission"),
        status=SourceSyncStatus.ACTIVE,
        chunks=source_chunks,
        content_hash=digest("document-content"),
    )


def active_task(*, fetched_at: datetime = NOW) -> SourceSnapshot:
    return SourceSnapshot(
        source_type=SyncSourceType.TASK,
        source_id="task-1",
        source_title="Delivery task",
        source_url="dingtalk://task/task-1",
        source_version="v1",
        source_time=TASK_REF.source_time,
        fetched_at=fetched_at,
        permission_scope=PERMISSION_SCOPE,
        permission_hash=digest("task-permission"),
        status=SourceSyncStatus.ACTIVE,
        chunks=(),
        content_hash=digest("task-content"),
    )


def failed_source(
    source_type: SyncSourceType,
    *,
    fetched_at: datetime,
    error_type: SourceErrorType = SourceErrorType.NETWORK_TIMEOUT,
) -> SourceSnapshot:
    is_document = source_type is SyncSourceType.DOCUMENT
    return SourceSnapshot(
        source_type=source_type,
        source_id="doc-1" if is_document else "task-1",
        source_title="Decision document" if is_document else "Delivery task",
        source_url=(
            "dingtalk://doc/doc-1"
            if is_document
            else "dingtalk://task/task-1"
        ),
        source_version=None,
        source_time=None,
        fetched_at=fetched_at,
        permission_scope=PERMISSION_SCOPE,
        permission_hash=digest(
            "doc-permission" if is_document else "task-permission"
        ),
        status=SourceSyncStatus.FAILED,
        chunks=(),
        content_hash=None,
        error_type=error_type,
        retryable=True,
        retry_after_seconds=30,
    )


def context(*, generated_at: datetime = NOW) -> ProjectContextPackage:
    return ProjectContextPackage(
        project_id=PROJECT_ID,
        project_name="Demo project",
        generated_at=generated_at,
        source_refs=(DOCUMENT_REF,),
        permission_scope=PERMISSION_SCOPE,
        freshness_seconds=300,
    )


def envelope(
    *,
    cursor: int = 1,
    generated_at: datetime = NOW,
    sources: tuple[SourceSnapshot, ...] | None = None,
    completed_ids: tuple[str, ...] = (),
    completed_claims: tuple[RetrievalCompletionClaim, ...] = (),
) -> SyncEnvelope:
    draft = SyncEnvelope(
        schema_version=1,
        sync_id=f"sync-{cursor}",
        project_id=PROJECT_ID,
        generated_at=generated_at,
        source_cursor=cursor,
        content_hash="0" * 64,
        producer="qwenwork-dws",
        context=context(generated_at=generated_at),
        sources=sources or (active_document(fetched_at=generated_at),),
        completed_retrieval_request_ids=completed_ids,
        completed_retrieval_claims=completed_claims,
    )
    return draft.model_copy(
        update={"content_hash": compute_envelope_content_hash(draft)}
    )


def envelope_without_context_refs(**values: object) -> SyncEnvelope:
    draft = envelope(**values)
    draft = draft.model_copy(
        update={"context": draft.context.model_copy(update={"source_refs": ()})}
    )
    return draft.model_copy(
        update={"content_hash": compute_envelope_content_hash(draft)}
    )


def sync_service(
    tmp_path: Path,
    *,
    repository: ProjectSyncRepository | None = None,
    protector: ReversibleProtector | None = None,
    registry: ProjectSnapshotRegistry | None = None,
    source_freshness_seconds: int = 1_800,
    clock_skew_seconds: float = 300.0,
    monotonic=lambda: 100.0,  # noqa: B008
) -> tuple[
    ProjectSyncService,
    ProjectSyncRepository,
    ReversibleProtector,
    ProjectSnapshotRegistry,
]:
    actual_repository = repository or ProjectSyncRepository(
        tmp_path / "project-memory.db"
    )
    actual_repository.initialize()
    actual_protector = protector or ReversibleProtector()
    actual_registry = registry or ProjectSnapshotRegistry()
    return (
        ProjectSyncService(
            actual_repository,
            actual_protector,
            actual_registry,
            source_freshness_seconds=source_freshness_seconds,
            clock_skew_seconds=clock_skew_seconds,
            monotonic=monotonic,
        ),
        actual_repository,
        actual_protector,
        actual_registry,
    )


def test_semantic_hash_ignores_polling_metadata_and_stable_sorts_sources() -> None:
    first = envelope(
        cursor=1,
        sources=(active_document(), active_task()),
    )
    later = envelope(
        cursor=9,
        generated_at=NOW + timedelta(minutes=5),
        sources=(
            active_task(fetched_at=NOW + timedelta(minutes=5)),
            active_document(fetched_at=NOW + timedelta(minutes=5)),
        ),
        completed_ids=("retrieval-1",),
    )

    assert first.content_hash == later.content_hash


def test_semantic_hash_ignores_transient_failure_details() -> None:
    first = envelope(
        sources=(
            active_document(),
            failed_source(SyncSourceType.TASK, fetched_at=NOW),
        )
    )
    later_draft = envelope(
        cursor=2,
        generated_at=NOW + timedelta(minutes=1),
        sources=(
            failed_source(
                SyncSourceType.TASK,
                fetched_at=NOW + timedelta(minutes=1),
                error_type=SourceErrorType.PROVIDER_UNAVAILABLE,
            ),
            active_document(fetched_at=NOW + timedelta(minutes=1)),
        ),
    )

    assert first.content_hash == later_draft.content_hash


def test_apply_rejects_invalid_hash_clock_skew_and_scope(tmp_path: Path) -> None:
    service, _, _, _ = sync_service(tmp_path)
    valid = envelope()

    with pytest.raises(ProjectSyncValidationError, match="content_hash_mismatch"):
        service.apply(
            valid.model_copy(update={"content_hash": "f" * 64}),
            principal=PRINCIPAL,
            now=NOW,
        )
    with pytest.raises(ProjectSyncValidationError, match="clock_skew_exceeded"):
        service.apply(valid, principal=PRINCIPAL, now=NOW + timedelta(seconds=301))

    wrong_scope = ProjectApiPrincipal(
        principal_id="limited",
        token_sha256="b" * 64,
        project_ids=frozenset({PROJECT_ID}),
        permission_scopes=frozenset({"project:other"}),
    )
    with pytest.raises(ProjectAuthorizationError, match="project_scope_denied"):
        service.apply(valid, principal=wrong_scope, now=NOW)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("open_actions", ("legacy action",)),
        ("current_risks", ("legacy risk",)),
        ("next_meeting", "legacy meeting"),
    ],
)
def test_sync_rejects_nonempty_legacy_facts(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    service, repository, _, _ = sync_service(tmp_path)
    candidate = envelope()
    legacy_context = candidate.context.model_copy(update={field: value})
    candidate = candidate.model_copy(update={"context": legacy_context})
    candidate = candidate.model_copy(
        update={"content_hash": compute_envelope_content_hash(candidate)}
    )

    with pytest.raises(
        ProjectSyncValidationError,
        match="context_fact_unreferenced",
    ):
        service.apply(candidate, principal=PRINCIPAL, now=NOW)

    assert repository.load_active_generation(PROJECT_ID) is None


def test_sync_rejects_sourced_fact_with_unmatched_excerpt(tmp_path: Path) -> None:
    service, repository, _, _ = sync_service(tmp_path)
    candidate = envelope()
    fabricated = DOCUMENT_REF.model_copy(
        update={"excerpt": "Fact absent from active evidence"}
    )
    sourced_context = candidate.context.model_copy(
        update={
            "sourced_actions": (
                SourcedFact(
                    text="Fabricated action",
                    source_refs=(fabricated,),
                ),
            )
        }
    )
    candidate = candidate.model_copy(update={"context": sourced_context})
    candidate = candidate.model_copy(
        update={"content_hash": compute_envelope_content_hash(candidate)}
    )

    with pytest.raises(
        ProjectSyncValidationError,
        match="source_excerpt_mismatch",
    ):
        service.apply(candidate, principal=PRINCIPAL, now=NOW)

    assert repository.load_active_generation(PROJECT_ID) is None


def test_sync_rejects_unmatched_top_level_context_reference(tmp_path: Path) -> None:
    service, repository, _, _ = sync_service(tmp_path)
    candidate = envelope()
    invalid_context = candidate.context.model_copy(
        update={
            "source_refs": (
                DOCUMENT_REF.model_copy(update={"excerpt": "Missing excerpt"}),
            )
        }
    )
    candidate = candidate.model_copy(update={"context": invalid_context})
    candidate = candidate.model_copy(
        update={"content_hash": compute_envelope_content_hash(candidate)}
    )

    with pytest.raises(
        ProjectSyncValidationError,
        match="source_excerpt_mismatch",
    ):
        service.apply(candidate, principal=PRINCIPAL, now=NOW)

    assert repository.load_active_generation(PROJECT_ID) is None


def test_sync_rejects_decision_reference_metadata_mismatch(tmp_path: Path) -> None:
    service, repository, _, _ = sync_service(tmp_path)
    candidate = envelope()
    invalid_reference = DOCUMENT_REF.model_copy(
        update={"source_title": "Forged title"}
    )
    decision = DecisionCard(
        decision_id="decision-1",
        project_id=PROJECT_ID,
        topic="terminal plan",
        decision_text="Use plan B",
        rationale="Stable rollout",
        owner="project-owner",
        decided_at=NOW,
        source_refs=(invalid_reference,),
        status="active",
        confidence=0.9,
    )
    invalid_context = candidate.context.model_copy(
        update={"active_decisions": (decision,)}
    )
    candidate = candidate.model_copy(update={"context": invalid_context})
    candidate = candidate.model_copy(
        update={"content_hash": compute_envelope_content_hash(candidate)}
    )

    with pytest.raises(
        ProjectSyncValidationError,
        match="source_ref_mismatch",
    ):
        service.apply(candidate, principal=PRINCIPAL, now=NOW)

    assert repository.load_active_generation(PROJECT_ID) is None


def test_sync_accepts_decision_with_exact_active_reference(tmp_path: Path) -> None:
    service, repository, _, _ = sync_service(tmp_path)
    candidate = envelope()
    decision = DecisionCard(
        decision_id="decision-1",
        project_id=PROJECT_ID,
        topic="terminal plan",
        decision_text="Use plan B",
        rationale="Stable rollout",
        owner="project-owner",
        decided_at=NOW,
        source_refs=(DOCUMENT_REF,),
        status="active",
        confidence=0.9,
    )
    valid_context = candidate.context.model_copy(
        update={"active_decisions": (decision,)}
    )
    candidate = candidate.model_copy(update={"context": valid_context})
    candidate = candidate.model_copy(
        update={"content_hash": compute_envelope_content_hash(candidate)}
    )

    result = service.apply(candidate, principal=PRINCIPAL, now=NOW)

    assert result.outcome == "applied"
    assert repository.load_active_generation(PROJECT_ID) is not None


def test_sync_accepts_first_decision_after_failed_only_history(
    tmp_path: Path,
) -> None:
    service, repository, _, _ = sync_service(tmp_path)
    for cursor in (1, 2):
        observed_at = NOW + timedelta(minutes=cursor - 1)
        failed = envelope(
            cursor=cursor,
            generated_at=observed_at,
            sources=(
                failed_source(
                    SyncSourceType.DOCUMENT,
                    fetched_at=observed_at,
                ),
            ),
        )
        failed = failed.model_copy(
            update={
                "context": failed.context.model_copy(
                    update={"source_refs": ()}
                )
            }
        )
        failed = failed.model_copy(
            update={"content_hash": compute_envelope_content_hash(failed)}
        )
        service.apply(failed, principal=PRINCIPAL, now=observed_at)

    generated_at = NOW + timedelta(minutes=2)
    candidate = envelope(cursor=3, generated_at=generated_at)
    decision = DecisionCard(
        decision_id="decision-1",
        project_id=PROJECT_ID,
        topic="terminal plan",
        decision_text="Use plan B",
        rationale="Stable rollout",
        owner="project-owner",
        decided_at=NOW,
        source_refs=(DOCUMENT_REF,),
        status="active",
        confidence=0.9,
    )
    candidate = candidate.model_copy(
        update={
            "context": candidate.context.model_copy(
                update={"active_decisions": (decision,)}
            )
        }
    )
    candidate = candidate.model_copy(
        update={"content_hash": compute_envelope_content_hash(candidate)}
    )

    result = service.apply(candidate, principal=PRINCIPAL, now=generated_at)

    stored = repository.load_active_generation(PROJECT_ID)
    assert result.outcome == "applied"
    assert stored is not None
    assert stored.context.active_decisions == (decision,)


def test_sync_accepts_sourced_fact_with_active_exact_excerpt(tmp_path: Path) -> None:
    service, repository, _, _ = sync_service(tmp_path)
    candidate = envelope()
    sourced_context = candidate.context.model_copy(
        update={
            "sourced_actions": (
                SourcedFact(
                    text="Confirm the rollout decision",
                    source_refs=(DOCUMENT_REF,),
                ),
            )
        }
    )
    candidate = candidate.model_copy(update={"context": sourced_context})
    candidate = candidate.model_copy(
        update={"content_hash": compute_envelope_content_hash(candidate)}
    )

    result = service.apply(candidate, principal=PRINCIPAL, now=NOW)

    assert result.outcome == "applied"
    stored = repository.load_active_generation(PROJECT_ID)
    assert stored is not None
    assert stored.context.sourced_actions == sourced_context.sourced_actions


def test_apply_rejects_chunk_id_that_does_not_match_content(tmp_path: Path) -> None:
    service, _, _, _ = sync_service(tmp_path)
    valid_chunk = evidence_chunk()
    tampered_chunk = valid_chunk.model_copy(update={"chunk_id": "f" * 64})
    candidate = envelope(
        sources=(active_document(chunks=(tampered_chunk,)),)
    )

    with pytest.raises(ProjectSyncValidationError, match="chunk_id_mismatch"):
        service.apply(candidate, principal=PRINCIPAL, now=NOW)


def test_unchanged_sync_renews_source_without_new_generation(tmp_path: Path) -> None:
    service, repository, _, _ = sync_service(tmp_path)
    first_envelope = envelope(cursor=1)
    second_envelope = envelope(
        cursor=2,
        generated_at=NOW + timedelta(minutes=5),
    )

    first = service.apply(first_envelope, principal=PRINCIPAL, now=NOW)
    second = service.apply(
        second_envelope,
        principal=PRINCIPAL,
        now=NOW + timedelta(minutes=5),
    )

    assert first.outcome == "applied"
    assert second.outcome == "unchanged"
    assert second.generation_id == first.generation_id
    assert repository.load_active_generation(PROJECT_ID).source_cursor == 2
    status = service.status(PROJECT_ID, now=NOW + timedelta(minutes=6))
    assert status.health is ProjectSyncHealth.HEALTHY
    assert status.last_success_at == NOW + timedelta(minutes=5)


def test_unchanged_sync_renews_context_across_multiple_freshness_periods(
    tmp_path: Path,
) -> None:
    service, repository, _, registry = sync_service(
        tmp_path,
        source_freshness_seconds=300,
    )

    first = service.apply(envelope(cursor=1), principal=PRINCIPAL, now=NOW)
    second_at = NOW + timedelta(minutes=5)
    third_at = NOW + timedelta(minutes=10)
    second = service.apply(
        envelope(cursor=2, generated_at=second_at),
        principal=PRINCIPAL,
        now=second_at,
    )
    third = service.apply(
        envelope(cursor=3, generated_at=third_at),
        principal=PRINCIPAL,
        now=third_at,
    )

    stored = repository.load_active_generation(PROJECT_ID)
    runtime = registry.get(PROJECT_ID)
    device_context = ProjectMemoryRepository(
        tmp_path / "project-memory.db"
    ).get_context(PROJECT_ID)
    assert stored is not None
    assert runtime is not None
    assert device_context is not None
    assert first.generation_id == second.generation_id == third.generation_id
    assert stored.context.generated_at == third_at
    assert runtime.context.generated_at == third_at
    assert device_context.generated_at == third_at
    assert service.status(
        PROJECT_ID,
        now=third_at + timedelta(minutes=4, seconds=59),
    ).health is ProjectSyncHealth.HEALTHY


def test_unchanged_failed_source_refreshes_attempt_without_false_success(
    tmp_path: Path,
) -> None:
    service, repository, _, registry = sync_service(tmp_path)
    first_source = failed_source(
        SyncSourceType.DOCUMENT,
        fetched_at=NOW,
        error_type=SourceErrorType.NETWORK_TIMEOUT,
    )
    later_at = NOW + timedelta(minutes=5)
    later_source = failed_source(
        SyncSourceType.DOCUMENT,
        fetched_at=later_at,
        error_type=SourceErrorType.PERMISSION_DENIED,
    )

    service.apply(
        envelope_without_context_refs(cursor=1, sources=(first_source,)),
        principal=PRINCIPAL,
        now=NOW,
    )
    result = service.apply(
        envelope_without_context_refs(
            cursor=2,
            generated_at=later_at,
            sources=(later_source,),
        ),
        principal=PRINCIPAL,
        now=later_at,
    )

    stored = repository.load_active_generation(PROJECT_ID)
    runtime = registry.get(PROJECT_ID)
    assert stored is not None
    assert runtime is not None
    assert result.outcome == "unchanged"
    for state in (stored.source_states[0], runtime.source_states[0]):
        assert state.last_attempt_at == later_at
        assert state.last_error_type is SourceErrorType.PERMISSION_DENIED
        assert state.last_success_at is None


def test_task_failure_does_not_block_fresh_decision_source(tmp_path: Path) -> None:
    service, _, _, _ = sync_service(tmp_path)
    result = service.apply(
        envelope(
            sources=(
                active_document(),
                failed_source(SyncSourceType.TASK, fetched_at=NOW),
            )
        ),
        principal=PRINCIPAL,
        now=NOW,
    )

    assert result.outcome == "degraded"
    assert result.project_status is ProjectSyncHealth.DEGRADED
    assert service.require_sources_fresh(
        PROJECT_ID,
        (DOCUMENT_REF,),
        now=NOW,
    ) is None
    with pytest.raises(ProjectSourceUnavailable, match="source_unavailable"):
        service.require_sources_fresh(PROJECT_ID, (TASK_REF,), now=NOW)


def test_failed_source_retains_last_success_and_decrypted_payload(
    tmp_path: Path,
) -> None:
    service, repository, protector, _ = sync_service(tmp_path)
    first = service.apply(
        envelope(sources=(active_document(), active_task())),
        principal=PRINCIPAL,
        now=NOW,
    )
    restarted, _, _, restarted_registry = sync_service(
        tmp_path,
        repository=repository,
        protector=protector,
        registry=ProjectSnapshotRegistry(),
    )

    result = restarted.apply(
        envelope(
            cursor=2,
            generated_at=NOW + timedelta(minutes=5),
            sources=(
                active_document(fetched_at=NOW + timedelta(minutes=5)),
                failed_source(
                    SyncSourceType.TASK,
                    fetched_at=NOW + timedelta(minutes=5),
                ),
            ),
        ),
        principal=PRINCIPAL,
        now=NOW + timedelta(minutes=5),
    )

    assert result.outcome == "degraded"
    assert result.generation_id != first.generation_id
    snapshot = restarted_registry.get(PROJECT_ID)
    task_state = next(
        item
        for item in snapshot.source_states
        if item.source_type is SyncSourceType.TASK
    )
    assert task_state.last_success_at == NOW
    assert task_state.source_version == "v1"
    assert any(item.source_id == "task-1" for item in snapshot.sources)
    assert restarted.require_sources_fresh(
        PROJECT_ID,
        (TASK_REF,),
        now=NOW + timedelta(minutes=6),
    ) is None


def test_restart_restores_all_active_snapshots_before_status(
    tmp_path: Path,
) -> None:
    service, repository, protector, _ = sync_service(tmp_path)
    service.apply(envelope(), principal=PRINCIPAL, now=NOW)
    restored_registry = ProjectSnapshotRegistry()
    restored, _, _, _ = sync_service(
        tmp_path,
        repository=repository,
        protector=protector,
        registry=restored_registry,
    )

    assert restored.restore_active_projects() == (PROJECT_ID,)
    snapshot = restored_registry.get(PROJECT_ID)
    assert snapshot is not None
    assert snapshot.context == context()
    assert restored.status(PROJECT_ID, now=NOW).health is (
        ProjectSyncHealth.HEALTHY
    )


def test_legacy_active_ciphertext_adopts_identity_after_full_decryption(
    tmp_path: Path,
) -> None:
    service, repository, protector, _ = sync_service(tmp_path)
    service.apply(envelope(), principal=PRINCIPAL, now=NOW)
    identity = digest("legacy-windows-user")

    initialize_repository_protection(
        repository,
        protector,
        identity_digest=identity,
    )

    assert repository.protection_descriptor() == (
        identity,
        protector.protector_version,
    )
    assert repository.load_active_generation(PROJECT_ID) is not None


def test_legacy_active_ciphertext_rejects_identity_when_decryption_fails(
    tmp_path: Path,
) -> None:
    service, repository, protector, _ = sync_service(tmp_path)
    service.apply(envelope(), principal=PRINCIPAL, now=NOW)
    protector.fail_unprotect = True

    with pytest.raises(
        ProtectionStateError,
        match="protection_identity_unverified",
    ):
        initialize_repository_protection(
            repository,
            protector,
            identity_digest=digest("wrong-windows-user"),
        )

    assert repository.protection_descriptor() is None


def test_status_projects_expired_active_source_without_mutating_snapshot(
    tmp_path: Path,
) -> None:
    service, _, _, registry = sync_service(
        tmp_path,
        source_freshness_seconds=600,
    )
    service.apply(envelope(), principal=PRINCIPAL, now=NOW)

    status = service.status(PROJECT_ID, now=NOW + timedelta(seconds=601))

    assert status.health is ProjectSyncHealth.STALE
    assert status.sources[0].status is SourceSyncStatus.STALE
    assert registry.get(PROJECT_ID).source_states[0].status is SourceSyncStatus.ACTIVE
    with pytest.raises(ProjectSourceUnavailable, match="source_stale"):
        service.require_sources_fresh(
            PROJECT_ID,
            (DOCUMENT_REF,),
            now=NOW + timedelta(seconds=601),
        )


def test_future_client_fetched_at_never_extends_gateway_freshness(
    tmp_path: Path,
) -> None:
    service, repository, _, _ = sync_service(
        tmp_path,
        source_freshness_seconds=600,
    )
    client_future = NOW + timedelta(minutes=5)

    service.apply(
        envelope(sources=(active_document(fetched_at=client_future),)),
        principal=PRINCIPAL,
        now=NOW,
    )

    stored = repository.load_active_generation(PROJECT_ID)
    assert stored is not None
    assert stored.source_states[0].last_attempt_at == NOW
    assert stored.source_states[0].last_success_at == NOW
    assert service.status(
        PROJECT_ID,
        now=NOW + timedelta(seconds=601),
    ).health is ProjectSyncHealth.STALE


def test_old_client_fetched_at_remains_valid_provenance(tmp_path: Path) -> None:
    service, repository, _, _ = sync_service(tmp_path)

    result = service.apply(
        envelope(
            sources=(
                active_document(fetched_at=NOW - timedelta(days=1)),
            )
        ),
        principal=PRINCIPAL,
        now=NOW,
    )

    assert result.outcome == "applied"
    stored = repository.load_active_generation(PROJECT_ID)
    assert stored is not None
    assert stored.source_states[0].last_success_at == NOW


def test_resume_detection_requests_one_immediate_sync(tmp_path: Path) -> None:
    service, _, _, _ = sync_service(tmp_path)

    first = service.recheck_clock(wall_now=NOW, monotonic_now=100)
    resumed = service.recheck_clock(
        wall_now=NOW + timedelta(seconds=601),
        monotonic_now=701,
    )

    assert first.reason == "normal"
    assert not first.immediate_sync_required
    assert resumed.reason == "resume_detected"
    assert resumed.immediate_sync_required
    assert not resumed.clock_untrusted
    assert service.consume_immediate_sync_request()
    assert not service.consume_immediate_sync_request()


@pytest.mark.parametrize("elapsed_monotonic", [1.0, 601.0])
def test_resume_blocks_facts_until_project_resync(
    tmp_path: Path, elapsed_monotonic: float
) -> None:
    sample = {"value": 100.0}
    service, _, _, _ = sync_service(
        tmp_path, monotonic=lambda: sample["value"]
    )
    service.apply(envelope(), principal=PRINCIPAL, now=NOW)
    resumed_at = NOW + timedelta(seconds=601)
    sample["value"] += elapsed_monotonic
    service.recheck_clock(wall_now=resumed_at)

    assert service.status(PROJECT_ID, now=resumed_at).health is (
        ProjectSyncHealth.STALE
    )
    with pytest.raises(ProjectSourceUnavailable, match="source_stale"):
        service.require_sources_fresh(PROJECT_ID, (DOCUMENT_REF,), now=resumed_at)

    service.apply(
        envelope(cursor=2, generated_at=resumed_at),
        principal=PRINCIPAL,
        now=resumed_at,
    )
    service.require_sources_fresh(PROJECT_ID, (DOCUMENT_REF,), now=resumed_at)


def test_clock_rollback_threshold_is_independent_of_envelope_skew(
    tmp_path: Path,
) -> None:
    service, _, _, _ = sync_service(tmp_path, clock_skew_seconds=0)
    service.recheck_clock(wall_now=NOW, monotonic_now=100)

    small_rollback = service.recheck_clock(
        wall_now=NOW - timedelta(seconds=1),
        monotonic_now=101,
    )
    large_rollback = service.recheck_clock(
        wall_now=NOW - timedelta(seconds=302),
        monotonic_now=102,
    )

    assert small_rollback.reason == "normal"
    assert not small_rollback.clock_untrusted
    assert not small_rollback.immediate_sync_required
    assert large_rollback.reason == "clock_rollback"
    assert large_rollback.clock_untrusted
    assert large_rollback.immediate_sync_required


def test_failed_resume_sync_keeps_project_recovery_pending(tmp_path: Path) -> None:
    service, repository, _, _ = sync_service(tmp_path)
    service.apply(envelope(), principal=PRINCIPAL, now=NOW)
    resumed_at = NOW + timedelta(seconds=601)
    result = service.apply(
        envelope_without_context_refs(
            cursor=2,
            generated_at=resumed_at,
            sources=(failed_source(SyncSourceType.DOCUMENT, fetched_at=resumed_at),),
        ),
        principal=PRINCIPAL,
        now=resumed_at,
    )

    assert result.failed_sources == 1
    assert repository.project_requires_clock_resync(PROJECT_ID)
    assert result.project_status is ProjectSyncHealth.STALE
    with pytest.raises(ProjectSourceUnavailable, match="source_stale"):
        service.require_sources_fresh(PROJECT_ID, (DOCUMENT_REF,), now=resumed_at)


def test_future_success_timestamp_is_not_fresh(tmp_path: Path) -> None:
    service, _, _, _ = sync_service(tmp_path)
    service.apply(envelope(), principal=PRINCIPAL, now=NOW)
    with pytest.raises(ProjectSourceUnavailable, match="source_stale"):
        service.require_sources_fresh(
            PROJECT_ID, (DOCUMENT_REF,), now=NOW - timedelta(seconds=1)
        )


def test_clock_rollback_fails_closed_until_successful_sync(tmp_path: Path) -> None:
    service, _, _, _ = sync_service(tmp_path, monotonic=lambda: 100.0)
    service.apply(envelope(), principal=PRINCIPAL, now=NOW)
    service.recheck_clock(
        wall_now=NOW + timedelta(seconds=500),
        monotonic_now=600,
    )

    rollback_time = NOW + timedelta(seconds=199)
    rolled_back = service.recheck_clock(
        wall_now=rollback_time,
        monotonic_now=601,
    )

    assert rolled_back.reason == "clock_rollback"
    assert rolled_back.clock_untrusted
    assert rolled_back.immediate_sync_required
    assert service.status(PROJECT_ID, now=rollback_time).health is (
        ProjectSyncHealth.CLOCK_UNTRUSTED
    )
    with pytest.raises(ProjectSourceUnavailable, match="clock_untrusted"):
        service.require_sources_fresh(
            PROJECT_ID,
            (DOCUMENT_REF,),
            now=rollback_time,
        )

    recovered_at = rollback_time + timedelta(seconds=1)
    service.apply(
        envelope(cursor=2, generated_at=recovered_at),
        principal=PRINCIPAL,
        now=recovered_at,
    )

    assert service.status(PROJECT_ID, now=recovered_at).health is (
        ProjectSyncHealth.HEALTHY
    )
    assert not service.consume_immediate_sync_request()


def test_retrieval_completion_is_committed_with_available_evidence(
    tmp_path: Path,
) -> None:
    service, repository, _, _ = sync_service(tmp_path)
    service.apply(envelope(cursor=1), principal=PRINCIPAL, now=NOW)
    repository.save_retrieval_request(
        RetrievalRequest(
            request_id="retrieval-1",
            project_id=PROJECT_ID,
            query_hash=digest("why plan B"),
            source_id_hashes=(digest("doc-1"),),
            status=RetrievalRequestStatus.PENDING,
            created_at=NOW - timedelta(minutes=1),
            expires_at=NOW + timedelta(minutes=10),
        )
    )
    claimed = repository.claim_retrieval_requests(
        PROJECT_ID,
        now=NOW + timedelta(seconds=1),
        lease_seconds=300,
    )[0]
    updated_at = NOW + timedelta(minutes=1)

    service.apply(
        envelope(
            cursor=2,
            generated_at=updated_at,
            sources=(
                active_document(
                    fetched_at=updated_at,
                    chunks=(
                        evidence_chunk(
                            text="Use plan B with newly retrieved evidence.",
                        ),
                    ),
                ),
            ),
            completed_claims=(
                RetrievalCompletionClaim(
                    request_id=claimed.request_id,
                    request_epoch=claimed.request_epoch,
                    attempt_count=claimed.attempt_count,
                    lease_token=claimed.lease_token,
                ),
            ),
        ),
        principal=PRINCIPAL,
        now=updated_at,
    )

    request = repository.get_retrieval_request(PROJECT_ID, "retrieval-1")
    assert request.status is RetrievalRequestStatus.COMPLETED
    assert request.completed_at == updated_at


def test_protection_failure_prevents_commit_and_snapshot_swap(tmp_path: Path) -> None:
    protector = ReversibleProtector()
    protector.fail_protect = True
    service, repository, _, registry = sync_service(
        tmp_path,
        protector=protector,
    )

    with pytest.raises(RuntimeError, match="protect_failed"):
        service.apply(envelope(), principal=PRINCIPAL, now=NOW)

    assert repository.load_active_generation(PROJECT_ID) is None
    assert registry.get(PROJECT_ID) is None


def test_commit_failure_prevents_snapshot_swap(tmp_path: Path) -> None:
    repository = CommitFailingRepository(tmp_path / "project-memory.db")
    service, _, _, registry = sync_service(tmp_path, repository=repository)

    with pytest.raises(RuntimeError, match="commit_failed"):
        service.apply(envelope(), principal=PRINCIPAL, now=NOW)

    assert registry.get(PROJECT_ID) is None


def test_retained_payload_decryption_failure_prevents_commit(tmp_path: Path) -> None:
    service, repository, protector, registry = sync_service(tmp_path)
    first = service.apply(
        envelope(sources=(active_document(), active_task())),
        principal=PRINCIPAL,
        now=NOW,
    )
    protector.fail_unprotect = True

    with pytest.raises(RuntimeError, match="unprotect_failed"):
        service.apply(
            envelope(
                cursor=2,
                generated_at=NOW + timedelta(minutes=5),
                sources=(
                    active_document(fetched_at=NOW + timedelta(minutes=5)),
                    failed_source(
                        SyncSourceType.TASK,
                        fetched_at=NOW + timedelta(minutes=5),
                    ),
                ),
            ),
            principal=PRINCIPAL,
            now=NOW + timedelta(minutes=5),
        )

    assert repository.load_active_generation(PROJECT_ID).generation_id == (
        first.generation_id
    )
    assert registry.get(PROJECT_ID).generation_id == first.generation_id
