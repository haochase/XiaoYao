from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from companion_gateway.project.index import (
    EvidenceSource,
    ProjectEvidenceIndex,
    ProjectRuntimeSnapshot,
    ProjectSnapshotRegistry,
)
from companion_gateway.project.models import (
    AnswerKind,
    DecisionCard,
    EvidenceRef,
    ProjectContextPackage,
)
from companion_gateway.project.service import (
    ProjectContextUnavailable,
    ProjectMemoryService,
)
from companion_gateway.project.sync_models import (
    EvidenceChunk,
    RetrievalRequest,
    SourceErrorType,
    SourceState,
    SourceSyncStatus,
    SyncSourceType,
)
from companion_gateway.project.sync_repository import ProjectSyncRepository
from companion_gateway.project.sync_service import ProjectSourceUnavailable


NOW = datetime(2026, 9, 5, 8, 0, tzinfo=UTC)
PROJECT_ID = "project-1"
PERMISSION_SCOPE = "project:demo"


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def evidence_source(
    source_id: str,
    *,
    source_type: SyncSourceType = SyncSourceType.DOCUMENT,
) -> EvidenceSource:
    return EvidenceSource(
        source_type=source_type,
        source_id=source_id,
        source_id_hash=digest(source_id),
        source_title=f"{source_id} 标题",
        source_url=f"https://example.invalid/{source_id}",
        source_version="v1",
        source_time=NOW,
        permission_hash=digest(f"permission:{source_id}"),
        content_hash=digest(f"content:{source_id}"),
    )


def source_ref(source: EvidenceSource, *, excerpt: str = "已验证来源") -> EvidenceRef:
    return EvidenceRef(
        source_type=source.source_type.value,
        source_id=source.source_id,
        source_title=source.source_title,
        source_url=source.source_url,
        source_time=source.source_time,
        excerpt=excerpt,
        permission_scope=PERMISSION_SCOPE,
    )


def source_state(
    source: EvidenceSource,
    *,
    status: SourceSyncStatus = SourceSyncStatus.ACTIVE,
) -> SourceState:
    return SourceState(
        project_id=PROJECT_ID,
        source_type=source.source_type,
        source_id_hash=source.source_id_hash,
        source_version=source.source_version,
        content_hash=source.content_hash,
        permission_hash=source.permission_hash,
        status=status,
        last_attempt_at=NOW,
        last_success_at=NOW,
        last_error_type=(
            SourceErrorType.UNKNOWN
            if status in {SourceSyncStatus.STALE, SourceSyncStatus.FAILED}
            else None
        ),
    )


def evidence_chunk(
    source: EvidenceSource,
    text: str,
    *,
    ordinal: int = 0,
) -> EvidenceChunk:
    return EvidenceChunk(
        chunk_id=digest(f"{source.source_id}:{ordinal}:{text}"),
        source_id=source.source_id,
        source_version=source.source_version,
        ordinal=ordinal,
        heading_path=("资料",),
        text=text,
        start_offset=ordinal * 100,
        end_offset=ordinal * 100 + len(text),
        content_hash=digest(text),
    )


def project_snapshot(
    *,
    sources: tuple[EvidenceSource, ...],
    chunks: tuple[EvidenceChunk, ...],
    source_statuses: dict[str, SourceSyncStatus] | None = None,
    decision: DecisionCard | None = None,
) -> ProjectRuntimeSnapshot:
    refs = tuple(source_ref(source) for source in sources)
    context = ProjectContextPackage(
        project_id=PROJECT_ID,
        project_name="会锚项目",
        generated_at=NOW,
        source_refs=refs,
        active_decisions=(decision,) if decision is not None else (),
        permission_scope=PERMISSION_SCOPE,
        freshness_seconds=300,
    )
    states = tuple(
        source_state(
            source,
            status=(source_statuses or {}).get(
                source.source_id,
                SourceSyncStatus.ACTIVE,
            ),
        )
        for source in sources
    )
    index = ProjectEvidenceIndex(context=context, sources=sources, chunks=chunks)
    return ProjectRuntimeSnapshot(
        project_id=PROJECT_ID,
        generation_id="generation-1",
        context=context,
        source_states=states,
        sources=sources,
        chunks=chunks,
        evidence_index=index,
    )


class RecordingSourcePolicy:
    def __init__(self, errors: dict[str, str] | None = None) -> None:
        self.errors = errors or {}
        self.calls: list[tuple[str, tuple[EvidenceRef, ...], datetime]] = []

    def require_sources_fresh(
        self,
        project_id: str,
        source_refs: tuple[EvidenceRef, ...],
        *,
        now: datetime,
    ) -> None:
        self.calls.append((project_id, source_refs, now))
        for item in source_refs:
            if label := self.errors.get(item.source_id):
                raise ProjectSourceUnavailable(label)


class RecordingRetrievalWriter:
    def __init__(self) -> None:
        self.requests: dict[str, RetrievalRequest] = {}

    def save_retrieval_request(
        self,
        request: RetrievalRequest,
    ) -> RetrievalRequest:
        stored = self.requests.setdefault(request.request_id, request)
        if (
            stored.project_id != request.project_id
            or stored.query_hash != request.query_hash
            or stored.source_id_hashes != request.source_id_hashes
        ):
            raise RuntimeError("retrieval_request_conflict")
        return stored


def decision_for(source: EvidenceSource) -> DecisionCard:
    return DecisionCard(
        decision_id="decision-1",
        project_id=PROJECT_ID,
        topic="终端方案",
        decision_text="采用方案 B",
        rationale="交付风险更低",
        owner="owner-1",
        decided_at=NOW,
        source_refs=(source_ref(source),),
        status="active",
        confidence=0.92,
    )


def integrated_service(
    snapshot: ProjectRuntimeSnapshot,
    *,
    policy: RecordingSourcePolicy,
    retrieval_writer: object,
) -> ProjectMemoryService:
    registry = ProjectSnapshotRegistry()
    registry.swap(PROJECT_ID, snapshot)
    service = ProjectMemoryService(
        clock=lambda: NOW,
        source_policy=policy,
        snapshot_reader=registry,
        retrieval_writer=retrieval_writer,
    )
    service.replace_context(snapshot.context)
    return service


def repository_at(tmp_path: Path) -> ProjectSyncRepository:
    repository = ProjectSyncRepository(tmp_path / "sync-query.db")
    repository.initialize()
    return repository


def test_query_integration_dependencies_must_be_supplied_together() -> None:
    with pytest.raises(ValueError, match="dependencies"):
        ProjectMemoryService(source_policy=RecordingSourcePolicy())


def test_stale_task_source_does_not_block_decision_answer(tmp_path: Path) -> None:
    document = evidence_source("document-1")
    task = evidence_source("task-1", source_type=SyncSourceType.TASK)
    decision = decision_for(document)
    snapshot = project_snapshot(
        sources=(document, task),
        chunks=(evidence_chunk(document, "终端方案采用方案 B。"),),
        source_statuses={"task-1": SourceSyncStatus.STALE},
        decision=decision,
    )
    policy = RecordingSourcePolicy(errors={"task-1": "source_stale"})
    service = integrated_service(
        snapshot,
        policy=policy,
        retrieval_writer=repository_at(tmp_path),
    )

    answer = service.answer(
        PROJECT_ID,
        "终端方案是什么",
        kind=AnswerKind.DECISION_CHECK,
    )

    assert answer.text == "当前有效决策：采用方案 B"
    assert policy.calls == [(PROJECT_ID, decision.source_refs, NOW)]


@pytest.mark.parametrize(
    ("source_error", "expected_error"),
    [
        ("source_stale", "source_stale"),
        ("clock_untrusted", "source_stale"),
        ("source_unavailable", "source_unavailable"),
        ("project_not_synced", "source_unavailable"),
    ],
)
def test_structured_answer_maps_source_policy_errors(
    tmp_path: Path,
    source_error: str,
    expected_error: str,
) -> None:
    document = evidence_source("document-1")
    snapshot = project_snapshot(
        sources=(document,),
        chunks=(evidence_chunk(document, "终端方案采用方案 B。"),),
        decision=decision_for(document),
    )
    service = integrated_service(
        snapshot,
        policy=RecordingSourcePolicy(errors={document.source_id: source_error}),
        retrieval_writer=repository_at(tmp_path),
    )

    with pytest.raises(ProjectContextUnavailable, match=expected_error):
        service.answer(PROJECT_ID, "终端方案", kind=AnswerKind.FACT)


def test_suggestion_keeps_prefix_and_checks_its_actual_source(tmp_path: Path) -> None:
    document = evidence_source("document-1")
    decision = decision_for(document)
    snapshot = project_snapshot(
        sources=(document,),
        chunks=(evidence_chunk(document, "终端方案采用方案 B。"),),
        decision=decision,
    )
    policy = RecordingSourcePolicy()
    service = integrated_service(
        snapshot,
        policy=policy,
        retrieval_writer=repository_at(tmp_path),
    )

    answer = service.answer(PROJECT_ID, "终端方案", kind=AnswerKind.SUGGESTION)

    assert answer.text == "建议参考当前决策：采用方案 B"
    assert policy.calls == [(PROJECT_ID, decision.source_refs, NOW)]


def test_unstructured_fact_uses_first_index_hit(tmp_path: Path) -> None:
    document = evidence_source("document-1")
    chunk = evidence_chunk(document, "方案 B 通过降低外部依赖来控制交付风险。")
    snapshot = project_snapshot(sources=(document,), chunks=(chunk,))
    policy = RecordingSourcePolicy()
    service = integrated_service(
        snapshot,
        policy=policy,
        retrieval_writer=repository_at(tmp_path),
    )

    answer = service.answer(
        PROJECT_ID,
        "方案 B 为什么能控制交付风险",
        kind=AnswerKind.FACT,
    )

    expected_hit = snapshot.evidence_index.search(
        "方案 B 为什么能控制交付风险",
        allowed_source_hashes=frozenset({document.source_id_hash}),
        source_types=frozenset(
            {SyncSourceType.DOCUMENT, SyncSourceType.MEETING_NOTE}
        ),
        limit=5,
    )[0]
    assert answer.text == chunk.text
    assert answer.source_refs == (source_ref(document, excerpt=chunk.text),)
    assert answer.confidence == min(0.95, 0.5 + expected_hit.score / 4000)
    assert policy.calls[-1][1] == answer.source_refs


def test_missing_evidence_creates_one_idempotent_retrieval_request(
    tmp_path: Path,
) -> None:
    meeting = evidence_source(
        "meeting-1",
        source_type=SyncSourceType.MEETING_NOTE,
    )
    document = evidence_source("document-1")
    task = evidence_source("task-1", source_type=SyncSourceType.TASK)
    stale_document = evidence_source("document-stale")
    snapshot = project_snapshot(
        sources=(meeting, document, task, stale_document),
        chunks=(),
        source_statuses={"document-stale": SourceSyncStatus.STALE},
    )
    policy = RecordingSourcePolicy(errors={"document-stale": "source_stale"})
    repository = RecordingRetrievalWriter()
    service = integrated_service(
        snapshot,
        policy=policy,
        retrieval_writer=repository,
    )

    for _ in range(2):
        with pytest.raises(ProjectContextUnavailable, match="evidence_pending"):
            service.answer(PROJECT_ID, "为什么选择方案B", kind=AnswerKind.FACT)

    requests = tuple(repository.requests.values())
    assert len(requests) == 1
    request = requests[0]
    assert request.query_hash == digest("为什么选择方案b")
    assert request.source_id_hashes == tuple(
        sorted((document.source_id_hash, meeting.source_id_hash))
    )
    request_material = "\0".join(
        (
            PROJECT_ID,
            snapshot.generation_id,
            request.query_hash,
            *request.source_id_hashes,
        )
    )
    assert request.request_id == f"ret_{digest(request_material)[:32]}"
    assert request.created_at == NOW
    assert request.expires_at == NOW + timedelta(seconds=1800)


def test_missing_evidence_without_usable_document_source_fails_stale(
    tmp_path: Path,
) -> None:
    document = evidence_source("document-stale")
    snapshot = project_snapshot(
        sources=(document,),
        chunks=(),
        source_statuses={document.source_id: SourceSyncStatus.STALE},
    )
    repository = repository_at(tmp_path)
    service = integrated_service(
        snapshot,
        policy=RecordingSourcePolicy(errors={document.source_id: "source_stale"}),
        retrieval_writer=repository,
    )

    with pytest.raises(ProjectContextUnavailable, match="source_stale"):
        service.answer(PROJECT_ID, "为什么选择方案B", kind=AnswerKind.FACT)

    assert repository.list_retrieval_requests(PROJECT_ID) == ()
