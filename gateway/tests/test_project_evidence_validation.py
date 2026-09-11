from datetime import UTC, datetime

import pytest

from companion_gateway.project.evidence_validation import (
    validate_source_refs,
    validate_sourced_context,
)
from companion_gateway.project.models import (
    DecisionCard,
    EvidenceRef,
    HumanApprovalRef,
    ProjectContextPackage,
)
from companion_gateway.project.service import ProjectMemoryError, ProjectMemoryService
from companion_gateway.project.sync_models import (
    EvidenceChunk,
    SourceSnapshot,
    SourceSyncStatus,
    SyncSourceType,
)


NOW = datetime(2026, 9, 5, 8, tzinfo=UTC)
HASH_A = "a" * 64
HASH_B = "b" * 64


def active_source() -> SourceSnapshot:
    return SourceSnapshot(
        source_type=SyncSourceType.DOCUMENT,
        source_id="doc-1",
        source_title="Decision document",
        source_url="dingtalk://doc/doc-1",
        source_version="v1",
        source_time=NOW,
        fetched_at=NOW,
        permission_scope="project:demo",
        permission_hash=HASH_A,
        status=SourceSyncStatus.ACTIVE,
        chunks=(
            EvidenceChunk(
                chunk_id=HASH_B,
                source_id="doc-1",
                source_version="v1",
                ordinal=0,
                text="Use plan B for the terminal rollout.",
                start_offset=0,
                end_offset=36,
                content_hash=HASH_A,
            ),
        ),
        content_hash=HASH_B,
    )


def source_ref(**updates: object) -> EvidenceRef:
    values: dict[str, object] = {
        "source_type": "document",
        "source_id": "doc-1",
        "source_title": "Decision document",
        "source_url": "dingtalk://doc/doc-1",
        "source_time": NOW,
        "excerpt": "Use plan B",
        "permission_scope": "project:demo",
    }
    values.update(updates)
    return EvidenceRef(**values)


def sourced_context() -> ProjectContextPackage:
    return ProjectContextPackage(
        project_id="project-1",
        project_name="demo",
        generated_at=NOW,
        permission_scope="project:demo",
    )


def approved_context() -> ProjectContextPackage:
    approval = HumanApprovalRef(
        candidate_id="candidate-1", reviewer_id="owner-1", approved_at=NOW,
        reason="human review", decision_text="new decision", permission_scope="project:demo",
    )
    decision = DecisionCard(
        decision_id="decision-1", project_id="project-1", topic="topic",
        decision_text=approval.decision_text, rationale=approval.reason,
        owner="owner-1", decided_at=NOW, source_refs=(), approval_ref=approval,
        status="active", confidence=1,
    )
    return ProjectContextPackage(
        project_id="project-1", project_name="demo", generated_at=NOW,
        active_decisions=(decision,), permission_scope="project:demo",
    )


def test_sourced_context_rejects_external_human_approval() -> None:
    with pytest.raises(ValueError, match="external_approval_forbidden"):
        validate_sourced_context(approved_context(), ())


def test_source_refs_accept_exact_active_source() -> None:
    validate_source_refs(sourced_context(), (active_source(),), (source_ref(),))


@pytest.mark.parametrize(
    ("reference", "source", "expected"),
    [
        (
            source_ref(source_title="Forged title"),
            active_source(),
            "source_ref_mismatch",
        ),
        (
            source_ref(source_id="not-in-envelope"),
            active_source(),
            "source_ref_mismatch",
        ),
        (
            source_ref(excerpt="not in the source"),
            active_source(),
            "source_excerpt_mismatch",
        ),
        (
            source_ref(permission_scope="project:other"),
            active_source(),
            "source_ref_mismatch",
        ),
        (
            source_ref(),
            active_source().model_copy(
                update={
                    "status": SourceSyncStatus.FAILED,
                    "chunks": (),
                    "content_hash": None,
                }
            ),
            "source_ref_mismatch",
        ),
    ],
)
def test_source_refs_reject_noncurrent_or_mismatched_sources(
    reference: EvidenceRef,
    source: SourceSnapshot,
    expected: str,
) -> None:
    with pytest.raises(ValueError, match=expected):
        validate_source_refs(sourced_context(), (source,), (reference,))


def test_replace_context_rejects_external_human_approval() -> None:
    service = ProjectMemoryService(clock=lambda: NOW)
    with pytest.raises(ProjectMemoryError, match="external_approval_forbidden"):
        service.replace_context(approved_context())


@pytest.mark.parametrize("field,value", [
    ("reviewer_id", "other"), ("approved_at", "2026-09-05T08:01:00Z"),
    ("reason", "other"), ("decision_text", "other"),
])
def test_version_rejects_inconsistent_approval(field, value) -> None:
    from companion_gateway.project.models import DecisionVersion

    approval = approved_context().active_decisions[0].approval_ref
    forged = {**approval.model_dump(mode="json"), field: value}
    with pytest.raises(ValueError, match="approval fields mismatch"):
        DecisionVersion(
            decision_id="decision-1", version=2, replaces_version=1,
            change_reason=approval.reason, decision_text=approval.decision_text,
            proposed_by="detector", approved_by=approval.reviewer_id,
            approved_at=approval.approved_at, status="active", approval_ref=forged,
        )


def test_context_rejects_cross_scope_approval() -> None:
    payload = approved_context().model_dump(mode="json")
    payload["active_decisions"][0]["approval_ref"]["permission_scope"] = "other"
    with pytest.raises(ValueError, match="approval permission scope mismatch"):
        ProjectContextPackage.model_validate(payload)


@pytest.mark.parametrize("both", [False, True])
def test_candidate_requires_exactly_one_conflict_basis(both) -> None:
    from companion_gateway.project.models import ConflictCandidate, EvidenceRef

    approval = approved_context().active_decisions[0].approval_ref
    reference = EvidenceRef(
        source_type="document", source_id="doc", source_title="doc",
        source_url="https://example.invalid/doc", source_time=NOW, excerpt="basis",
        permission_scope="project:demo",
    )
    with pytest.raises(ValueError, match="exactly one active decision basis"):
        ConflictCandidate(
            candidate_id="candidate", project_id="project-1", decision_id="decision-1",
            observed_text="change", proposed_decision_text="changed", active_decision_text=approval.decision_text,
            reason="review", created_at=NOW, source_refs=(reference,) if both else (),
            active_approval_ref=approval if both else None,
        )
