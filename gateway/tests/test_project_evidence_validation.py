from datetime import UTC, datetime

import pytest

from companion_gateway.project.evidence_validation import validate_sourced_context
from companion_gateway.project.models import DecisionCard, HumanApprovalRef, ProjectContextPackage
from companion_gateway.project.service import ProjectMemoryError, ProjectMemoryService


NOW = datetime(2026, 9, 5, 8, tzinfo=UTC)


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
