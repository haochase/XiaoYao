from datetime import UTC, datetime, timedelta

import pytest

from companion_gateway.project.models import (
    AnswerKind,
    ConflictStatus,
    DecisionCard,
    DecisionStatus,
    EvidenceRef,
    ProjectAnswer,
    ProjectContextPackage,
)
from companion_gateway.project.service import (
    ProjectContextUnavailable,
    ProjectMemoryService,
)


NOW = datetime(2026, 9, 4, 8, 0, tzinfo=UTC)


def source(source_id: str = "meeting-1") -> EvidenceRef:
    return EvidenceRef(
        source_type="meeting_note",
        source_id=source_id,
        source_title="方案评审会",
        source_url=f"https://example.invalid/{source_id}",
        source_time=NOW,
        excerpt="会议决定采用方案 B。",
        permission_scope="project:star-retail",
    )


def decision(text: str = "采用方案 B") -> DecisionCard:
    return DecisionCard(
        decision_id="decision-1",
        project_id="project-1",
        topic="终端方案",
        decision_text=text,
        rationale="交付风险更低",
        owner="owner-1",
        decided_at=NOW,
        source_refs=(source(),),
        status=DecisionStatus.ACTIVE,
        confidence=0.92,
    )


def context(*, generated_at: datetime = NOW, decisions: tuple[DecisionCard, ...] = ()) -> ProjectContextPackage:
    return ProjectContextPackage(
        project_id="project-1",
        project_name="星河零售终端升级项目",
        generated_at=generated_at,
        source_refs=(source(),),
        active_decisions=decisions,
        permission_scope="project:star-retail",
        freshness_seconds=300,
    )


def test_fact_answer_uses_fresh_context_and_returns_sources() -> None:
    service = ProjectMemoryService(clock=lambda: NOW)
    service.replace_context(context(decisions=(decision(),)))

    answer = service.answer(
        "project-1",
        "终端方案",
        kind=AnswerKind.FACT,
        now=NOW + timedelta(seconds=30),
    )

    assert isinstance(answer, ProjectAnswer)
    assert answer.kind is AnswerKind.FACT
    assert answer.text == "采用方案 B"
    assert answer.source_refs[0].source_id == "meeting-1"


def test_answer_rejects_expired_context_instead_of_using_stale_facts() -> None:
    service = ProjectMemoryService(clock=lambda: NOW)
    service.replace_context(context(decisions=(decision(),)))

    with pytest.raises(ProjectContextUnavailable, match="context_expired"):
        service.answer(
            "project-1",
            "终端方案",
            kind=AnswerKind.FACT,
            now=NOW + timedelta(seconds=301),
        )


def test_answer_requires_a_matching_active_decision() -> None:
    service = ProjectMemoryService(clock=lambda: NOW)
    service.replace_context(context(decisions=(decision(),)))

    with pytest.raises(ProjectContextUnavailable, match="source_not_found"):
        service.answer(
            "project-1",
            "完全没有来源的问题",
            kind=AnswerKind.FACT,
            now=NOW,
        )


def test_conflict_candidate_is_idempotent_and_starts_proposed() -> None:
    service = ProjectMemoryService(clock=lambda: NOW)
    service.replace_context(context(decisions=(decision(),)))

    first, created = service.propose_conflict(
        "project-1",
        decision_id="decision-1",
        observed_text="改用方案 A",
        reason="与当前有效方案 B 不一致",
        evidence_refs=(source("meeting-2"),),
        now=NOW,
    )
    second, duplicate = service.propose_conflict(
        "project-1",
        decision_id="decision-1",
        observed_text="改用方案 A",
        reason="与当前有效方案 B 不一致",
        evidence_refs=(source("meeting-2"),),
        now=NOW,
    )

    assert created is True
    assert duplicate is False
    assert first == second
    assert first.status is ConflictStatus.PROPOSED
    assert first.project_id == "project-1"


def test_rejecting_conflict_keeps_current_decision_active() -> None:
    service = ProjectMemoryService(clock=lambda: NOW)
    service.replace_context(context(decisions=(decision(),)))
    candidate, _ = service.propose_conflict(
        "project-1",
        decision_id="decision-1",
        observed_text="改用方案 A",
        reason="与当前有效方案 B 不一致",
        evidence_refs=(source("meeting-2"),),
        now=NOW,
    )

    reviewed = service.review_conflict(
        candidate.candidate_id,
        reviewer_id="owner-1",
        action="reject",
        change_reason="复核后仍采用方案 B",
        now=NOW + timedelta(minutes=1),
    )

    assert reviewed.status is ConflictStatus.REJECTED
    assert service.current_decision("project-1", "decision-1", now=NOW).decision_text == "采用方案 B"


def test_approving_conflict_creates_active_version_two_only_after_review() -> None:
    service = ProjectMemoryService(clock=lambda: NOW)
    service.replace_context(context(decisions=(decision(),)))
    candidate, _ = service.propose_conflict(
        "project-1",
        decision_id="decision-1",
        observed_text="改用方案 A",
        reason="供应商交期发生变化",
        evidence_refs=(source("meeting-2"),),
        now=NOW,
    )

    with pytest.raises(ValueError, match="new_decision_text"):
        service.review_conflict(
            candidate.candidate_id,
            reviewer_id="owner-1",
            action="accept",
            change_reason="供应商交期发生变化",
            now=NOW + timedelta(minutes=1),
        )

    reviewed, version = service.review_conflict(
        candidate.candidate_id,
        reviewer_id="owner-1",
        action="accept",
        new_decision_text="采用方案 A",
        change_reason="供应商交期发生变化",
        evidence_refs=(source("meeting-2"),),
        now=NOW + timedelta(minutes=1),
    )

    assert reviewed.status is ConflictStatus.ACCEPTED
    assert version.version == 2
    assert version.replaces_version == 1
    assert version.status is DecisionStatus.ACTIVE
    assert version.approved_by == "owner-1"
    assert service.current_decision("project-1", "decision-1", now=NOW).decision_text == "采用方案 A"


def test_conflict_rejects_evidence_from_another_permission_scope() -> None:
    service = ProjectMemoryService(clock=lambda: NOW)
    service.replace_context(context(decisions=(decision(),)))
    foreign = source("meeting-foreign").model_copy(
        update={"permission_scope": "project:other"}
    )

    with pytest.raises(RuntimeError, match="source_scope_mismatch"):
        service.propose_conflict(
            "project-1",
            decision_id="decision-1",
            observed_text="改用方案 A",
            reason="来源不属于当前项目",
            evidence_refs=(foreign,),
            now=NOW,
        )
