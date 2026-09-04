from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from companion_gateway.project.models import (
    AnswerKind,
    DecisionCard,
    DecisionStatus,
    DecisionVersion,
    EvidenceRef,
    ProjectAnswer,
    ProjectContextPackage,
)


NOW = datetime(2026, 9, 4, 8, 0, tzinfo=UTC)


def source(**overrides: object) -> EvidenceRef:
    data: dict[str, object] = {
        "source_type": "meeting_note",
        "source_id": "meeting-2026-08-12",
        "source_title": "方案评审会",
        "source_url": "https://example.invalid/meeting-2026-08-12",
        "source_time": NOW,
        "excerpt": "会议决定采用方案 B。",
        "permission_scope": "project:star-retail",
    }
    data.update(overrides)
    return EvidenceRef(**data)


def decision(**overrides: object) -> DecisionCard:
    data: dict[str, object] = {
        "decision_id": "decision-1",
        "project_id": "project-1",
        "topic": "终端方案",
        "decision_text": "采用方案 B",
        "rationale": "交付风险更低",
        "owner": "owner-1",
        "decided_at": NOW,
        "source_refs": (source(),),
        "status": DecisionStatus.ACTIVE,
        "confidence": 0.92,
    }
    data.update(overrides)
    return DecisionCard(**data)


def test_evidence_ref_accepts_bounded_source_contract() -> None:
    item = source()

    assert item.source_type == "meeting_note"
    assert item.source_id == "meeting-2026-08-12"
    assert item.source_time == NOW


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_id", ""),
        ("source_title", " "),
        ("source_url", ""),
        ("excerpt", ""),
        ("permission_scope", ""),
    ],
)
def test_evidence_ref_rejects_blank_required_values(
    field: str, value: str
) -> None:
    with pytest.raises(ValidationError):
        source(**{field: value})


def test_evidence_ref_rejects_naive_time_and_unknown_fields() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        source(source_time=datetime(2026, 9, 4, 8, 0))

    with pytest.raises(ValidationError):
        source(untrusted_text="do not store")


def test_decision_card_defaults_to_explicit_active_contract() -> None:
    item = decision()

    assert item.status is DecisionStatus.ACTIVE
    assert item.source_refs == (source(),)
    assert item.confidence == 0.92


@pytest.mark.parametrize("confidence", [-0.1, 1.1])
def test_decision_card_rejects_out_of_range_confidence(confidence: float) -> None:
    with pytest.raises(ValidationError):
        decision(confidence=confidence)


def test_decision_version_requires_approval_for_active_transition() -> None:
    pending = DecisionVersion(
        decision_id="decision-1",
        version=2,
        replaces_version=1,
        change_reason="成本条件变化",
        proposed_by="agent-1",
        status=DecisionStatus.PROPOSED,
        evidence_refs=(source(source_id="meeting-2026-09-04"),),
    )

    assert pending.status is DecisionStatus.PROPOSED
    assert pending.approved_by is None

    with pytest.raises(ValidationError, match="approved"):
        DecisionVersion(
            decision_id="decision-1",
            version=2,
            replaces_version=1,
            change_reason="成本条件变化",
            proposed_by="agent-1",
            status=DecisionStatus.ACTIVE,
            evidence_refs=(source(),),
        )


def test_context_package_requires_single_project_scope_and_freshness() -> None:
    package = ProjectContextPackage(
        project_id="project-1",
        project_name="星河零售终端升级项目",
        generated_at=NOW,
        source_refs=(source(),),
        active_decisions=(decision(),),
        open_actions=("补充方案 B 的成本测算",),
        current_risks=("供应商交期未确认",),
        next_meeting="2026-09-05 10:00",
        permission_scope="project:star-retail",
        freshness_seconds=300,
    )

    assert package.project_id == "project-1"
    assert package.active_decisions[0].project_id == package.project_id

    with pytest.raises(ValidationError, match="same project"):
        ProjectContextPackage(
            project_id="project-2",
            project_name="错误项目",
            generated_at=NOW,
            source_refs=(source(),),
            active_decisions=(decision(),),
            permission_scope="project:other",
        )


def test_context_package_rejects_foreign_nested_decision_sources() -> None:
    foreign_source = source(permission_scope="project:other")
    foreign_decision = decision(source_refs=(foreign_source,))

    with pytest.raises(ValidationError, match="decision sources"):
        ProjectContextPackage(
            project_id="project-1",
            project_name="星河零售终端升级项目",
            generated_at=NOW,
            source_refs=(source(),),
            active_decisions=(foreign_decision,),
            permission_scope="project:star-retail",
        )


def test_answer_kind_is_explicitly_bounded() -> None:
    assert set(AnswerKind) == {
        AnswerKind.FACT,
        AnswerKind.CURRENT_STATE,
        AnswerKind.SUGGESTION,
        AnswerKind.DECISION_CHECK,
    }


def test_fact_answer_requires_source_refs() -> None:
    with pytest.raises(ValidationError, match="source_refs"):
        ProjectAnswer(kind=AnswerKind.FACT, text="无来源的事实")
