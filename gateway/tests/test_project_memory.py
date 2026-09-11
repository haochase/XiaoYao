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
    ProjectMemoryError,
    ProjectMemoryService,
)


NOW = datetime(2026, 9, 4, 8, 0, tzinfo=UTC)


def test_conflict_persists_normalized_proposed_decision_text() -> None:
    service = ProjectMemoryService(clock=lambda: NOW)
    service.replace_context(context(decisions=(decision(),)))
    candidate, created = service.propose_conflict_from_statement(
        "project-1", "把终端方案改为方案 A",
        proposed_decision_text="采用方案 A", now=NOW,
    )
    assert created is True
    assert candidate.observed_text == "把终端方案改为方案 A"
    assert candidate.proposed_decision_text == "采用方案 A"
    assert candidate.source_refs == decision().source_refs


def test_conflict_identity_includes_normalized_proposal() -> None:
    service = ProjectMemoryService(clock=lambda: NOW)
    service.replace_context(context(decisions=(decision(),)))
    first, _ = service.propose_conflict_from_statement(
        "project-1", "把终端方案换一下",
        proposed_decision_text="采用方案 A", now=NOW,
    )
    second, created = service.propose_conflict_from_statement(
        "project-1", "把终端方案换一下",
        proposed_decision_text="采用方案 C", now=NOW,
    )
    assert created is True
    assert first.candidate_id != second.candidate_id


def test_accept_uses_candidate_proposal_and_human_approval_evidence() -> None:
    service = ProjectMemoryService(clock=lambda: NOW)
    service.replace_context(context(decisions=(decision(),)))
    candidate, _ = service.propose_conflict_from_statement(
        "project-1", "把终端方案换一下", proposed_decision_text="采用方案 A", now=NOW,
    )
    reviewed, version = service.review_conflict(
        candidate.candidate_id, reviewer_id="owner-1", action="accept",
        change_reason="负责人确认", now=NOW + timedelta(minutes=1),
    )
    approval = version.approval_ref
    assert reviewed.status is ConflictStatus.ACCEPTED
    assert approval.candidate_id == candidate.candidate_id
    assert approval.reviewer_id == "owner-1"
    assert approval.approved_at == NOW + timedelta(minutes=1)
    assert approval.decision_text == version.decision_text == "采用方案 A"
    assert version.evidence_refs == ()
    active = service.current_decision("project-1", "decision-1", now=NOW)
    assert active.source_refs == ()
    assert active.approval_ref == approval


def test_human_approval_does_not_extend_project_context_freshness() -> None:
    service = ProjectMemoryService(clock=lambda: NOW)
    service.replace_context(context(decisions=(decision(),)))
    candidate, _ = service.propose_conflict_from_statement(
        "project-1", "切换终端方案", proposed_decision_text="采用方案 A", now=NOW,
    )
    service.review_conflict(
        candidate.candidate_id, reviewer_id="owner-1", action="accept",
        change_reason="确认", now=NOW + timedelta(minutes=4),
    )
    with pytest.raises(ProjectContextUnavailable, match="context_expired"):
        service.answer("project-1", "终端方案", kind=AnswerKind.FACT, now=NOW + timedelta(minutes=6))


def test_reviewed_decision_can_be_proposed_and_approved_again() -> None:
    service = ProjectMemoryService(clock=lambda: NOW)
    service.replace_context(context(decisions=(decision(),)))
    for text in ("采用方案 A", "采用方案 B"):
        candidate, _ = service.propose_conflict_from_statement(
            "project-1", "把终端方案换一下", proposed_decision_text=text, now=NOW,
        )
        _, version = service.review_conflict(
            candidate.candidate_id, reviewer_id="owner-1", action="accept",
            change_reason="负责人确认", now=NOW,
        )
        assert version.decision_text == text
        assert version.evidence_refs == ()
    assert version.version == 3
    assert candidate.source_refs == ()
    assert candidate.active_approval_ref.decision_text == "采用方案 A"
    assert candidate.active_approval_ref != version.approval_ref


def test_direct_proposal_cannot_supply_an_unrelated_conflict_basis() -> None:
    service = ProjectMemoryService(clock=lambda: NOW)
    service.replace_context(context(decisions=(decision(),)))
    with pytest.raises(ProjectMemoryError, match="conflict_basis_mismatch"):
        service.propose_conflict(
            "project-1", decision_id="decision-1", observed_text="切换终端方案",
            proposed_decision_text="采用方案 A", reason="确认切换",
            evidence_refs=(source("meeting-other"),), now=NOW,
        )


@pytest.mark.parametrize("field,value", [("new_decision_text", "注入方案"), ("evidence_refs", [])])
def test_review_schema_rejects_client_decision_and_evidence(field, value) -> None:
    from companion_gateway.api import ConflictReviewRequest
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="Extra inputs"):
        ConflictReviewRequest.model_validate({"action": "accept", "change_reason": "确认", field: value})


@pytest.mark.parametrize("kind", [AnswerKind.FACT, AnswerKind.CURRENT_STATE, AnswerKind.DECISION_CHECK])
def test_fact_answers_accept_approval_but_reject_no_evidence(kind) -> None:
    from companion_gateway.project.models import HumanApprovalRef

    approval = HumanApprovalRef(
        candidate_id="candidate", reviewer_id="owner", approved_at=NOW,
        reason="approved", decision_text="proposal", permission_scope="project:star-retail",
    )
    answer = ProjectAnswer(kind=kind, text="proposal", approval_ref=approval)
    assert answer.source_refs == ()
    with pytest.raises(ValueError, match="source_refs"):
        ProjectAnswer(kind=kind, text="no evidence")


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


def approved_combined_service(
    *approved_texts: str,
    source_decisions: tuple[DecisionCard, ...] = (),
) -> tuple[ProjectMemoryService, tuple[DecisionCard, ...]]:
    candidates = tuple(
        decision(f"待批准的组合决策{index}").model_copy(
            update={
                "decision_id": f"decision-approved-{index}",
                "topic": f"待批准组合{index}",
                "source_refs": (source(f"meeting-approved-{index}"),),
            }
        )
        for index, _text in enumerate(approved_texts, start=1)
    )
    service = ProjectMemoryService(clock=lambda: NOW)
    service.replace_context(context(decisions=(*candidates, *source_decisions)))
    for candidate, text in zip(candidates, approved_texts, strict=True):
        conflict, _ = service.propose_conflict_from_statement(
            "project-1",
            candidate.topic,
            proposed_decision_text=text,
            now=NOW,
        )
        service.review_conflict(
            conflict.candidate_id,
            reviewer_id="owner-1",
            action="accept",
            change_reason="负责人批准组合决策",
            now=NOW,
        )
    return (
        service,
        tuple(
            service.current_decision("project-1", item.decision_id, now=NOW)
            for item in candidates
        ),
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


def test_fact_answer_matches_project_topic_inside_a_natural_question() -> None:
    service = ProjectMemoryService(clock=lambda: NOW)
    service.replace_context(context(decisions=(decision(),)))

    answer = service.answer(
        "project-1",
        "终端方案是什么",
        kind=AnswerKind.FACT,
        now=NOW,
    )

    assert answer.text == "采用方案 B"
    assert answer.source_refs == (source(),)


def test_answer_rejects_a_single_generic_chinese_fragment_overlap() -> None:
    project_decision = decision().model_copy(
        update={"topic": "桌面终端硬件选型"}
    )
    service = ProjectMemoryService(clock=lambda: NOW)
    service.replace_context(context(decisions=(project_decision,)))

    with pytest.raises(ProjectContextUnavailable, match="source_not_found"):
        service.answer(
            "project-1",
            "这个方案怎么样",
            kind=AnswerKind.DECISION_CHECK,
            now=NOW,
        )


def test_approved_decision_clause_answers_a_strong_query() -> None:
    service, (approved,) = approved_combined_service(
        "桌面终端采用固定方案；会前提醒默认提前10分钟。"
    )

    answer = service.answer(
        "project-1",
        "会前提醒默认提前多久",
        kind=AnswerKind.DECISION_CHECK,
        now=NOW,
    )

    assert answer.text == f"当前有效决策：{approved.decision_text}"
    assert answer.approval_ref == approved.approval_ref


@pytest.mark.parametrize("query", ("当前方案",))
def test_approved_decision_clause_rejects_short_weak_queries(query: str) -> None:
    service, _ = approved_combined_service(
        "桌面终端采用固定方案；会前提醒默认提前10分钟。"
    )

    with pytest.raises(ProjectContextUnavailable, match="source_not_found"):
        service.answer(
            "project-1",
            query,
            kind=AnswerKind.DECISION_CHECK,
            now=NOW,
        )


@pytest.mark.parametrize("query", ("提醒", "会前提醒"))
def test_approved_decision_clause_does_not_downgrade_exact_substrings(
    monkeypatch: pytest.MonkeyPatch,
    query: str,
) -> None:
    service, _ = approved_combined_service(
        "桌面终端采用固定方案；会前提醒默认提前10分钟。"
    )
    monkeypatch.setattr(
        ProjectMemoryService,
        "_matches_exactly",
        staticmethod(lambda _decision, _query: False),
    )

    with pytest.raises(ProjectContextUnavailable, match="source_not_found"):
        service.answer(
            "project-1",
            query,
            kind=AnswerKind.DECISION_CHECK,
            now=NOW,
        )


def test_tied_approved_decision_clause_candidates_fail_closed() -> None:
    service, _ = approved_combined_service(
        "桌面终端采用固定方案；会前提醒默认提前10分钟。",
        "桌面终端采用固定方案；会前提醒默认提前10分钟。",
    )

    with pytest.raises(ProjectContextUnavailable, match="source_not_found"):
        service.answer(
            "project-1",
            "会前提醒默认提前多久",
            kind=AnswerKind.DECISION_CHECK,
            now=NOW,
        )


def test_source_backed_decision_does_not_use_approved_clause_fallback() -> None:
    source_backed = decision(
        "桌面终端采用固定方案；会前提醒默认提前10分钟。"
    ).model_copy(update={"topic": "人工批准组合"})
    service = ProjectMemoryService(clock=lambda: NOW)
    service.replace_context(context(decisions=(source_backed,)))

    with pytest.raises(ProjectContextUnavailable, match="source_not_found"):
        service.answer(
            "project-1",
            "会前提醒默认提前多久",
            kind=AnswerKind.DECISION_CHECK,
            now=NOW,
        )


def test_topic_fragment_match_wins_over_approved_clause_match() -> None:
    topic_match = decision("主题匹配优先").model_copy(
        update={
            "decision_id": "decision-topic-match",
            "topic": "会前提醒默认提前设置",
            "source_refs": (source("meeting-topic-match"),),
        }
    )
    service, _ = approved_combined_service(
        "桌面终端采用固定方案；会前提醒默认提前10分钟。",
        source_decisions=(topic_match,),
    )

    answer = service.answer(
        "project-1",
        "会前提醒默认提前多久",
        kind=AnswerKind.DECISION_CHECK,
        now=NOW,
    )

    assert answer.text == "当前有效决策：主题匹配优先"
    assert answer.source_refs == (source("meeting-topic-match"),)


def test_exact_decision_wins_over_an_earlier_fragment_candidate() -> None:
    fragment_candidate = decision("采用方案 A").model_copy(
        update={
            "decision_id": "decision-fragment",
            "topic": "移动终端软件选型",
            "source_refs": (source("meeting-fragment"),),
        }
    )
    exact_candidate = decision().model_copy(
        update={
            "decision_id": "decision-exact",
            "source_refs": (source("meeting-exact"),),
        }
    )
    service = ProjectMemoryService(clock=lambda: NOW)
    service.replace_context(
        context(decisions=(fragment_candidate, exact_candidate))
    )

    answer = service.answer(
        "project-1",
        "终端方案是什么",
        kind=AnswerKind.DECISION_CHECK,
        now=NOW,
    )

    assert answer.text == "当前有效决策：采用方案 B"
    assert answer.source_refs == (source("meeting-exact"),)


def test_tied_fragment_candidates_fail_closed() -> None:
    first = decision("采用方案 A").model_copy(
        update={
            "decision_id": "decision-first",
            "topic": "移动终端软件选型",
            "source_refs": (source("meeting-first"),),
        }
    )
    second = decision().model_copy(
        update={
            "decision_id": "decision-second",
            "topic": "桌面终端硬件选型",
            "source_refs": (source("meeting-second"),),
        }
    )
    service = ProjectMemoryService(clock=lambda: NOW)
    service.replace_context(context(decisions=(first, second)))

    with pytest.raises(ProjectContextUnavailable, match="source_not_found"):
        service.answer(
            "project-1",
            "终端方案是什么",
            kind=AnswerKind.DECISION_CHECK,
            now=NOW,
        )


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
        proposed_decision_text="采用方案 A",
        reason="与当前有效方案 B 不一致",
        evidence_refs=(source(),),
        now=NOW,
    )
    second, duplicate = service.propose_conflict(
        "project-1",
        decision_id="decision-1",
        observed_text="改用方案 A",
        proposed_decision_text="采用方案 A",
        reason="与当前有效方案 B 不一致",
        evidence_refs=(source(),),
        now=NOW,
    )

    assert created is True
    assert duplicate is False
    assert first == second
    assert first.status is ConflictStatus.PROPOSED
    assert first.project_id == "project-1"


def test_conflict_statement_matching_the_active_decision_is_rejected() -> None:
    service = ProjectMemoryService(clock=lambda: NOW)
    service.replace_context(context(decisions=(decision(),)))

    with pytest.raises(ProjectMemoryError, match="statement_matches_active_decision"):
        service.propose_conflict_from_statement(
            "project-1",
            "终端方案继续采用方案 B",
            proposed_decision_text="采用方案 B",
            now=NOW,
        )


def test_conflict_uses_the_explicit_proposal_instead_of_sentence_markers() -> None:
    service = ProjectMemoryService(clock=lambda: NOW)
    service.replace_context(context(decisions=(decision(),)))

    candidate, created = service.propose_conflict_from_statement(
        "project-1",
        "终端方案继续用方案 A",
        proposed_decision_text="采用方案 A",
        now=NOW,
    )

    assert created is True
    assert candidate.status is ConflictStatus.PROPOSED

    with pytest.raises(ProjectMemoryError, match="statement_matches_active_decision"):
        service.propose_conflict_from_statement(
            "project-1",
            "不要改为方案 A，继续采用方案 B",
            proposed_decision_text="采用方案 B",
            now=NOW,
        )


@pytest.mark.parametrize(
    "proposed_decision_text",
    ("不采用方案 B", "采用方案 B 并增加离线推理"),
)
def test_conflict_does_not_treat_negation_or_added_constraints_as_equivalent(
    proposed_decision_text: str,
) -> None:
    service = ProjectMemoryService(clock=lambda: NOW)
    service.replace_context(context(decisions=(decision(),)))

    candidate, created = service.propose_conflict_from_statement(
        "project-1",
        proposed_decision_text,
        proposed_decision_text=proposed_decision_text,
        now=NOW,
    )

    assert created is True
    assert candidate.status is ConflictStatus.PROPOSED


def test_rejecting_conflict_keeps_current_decision_active() -> None:
    service = ProjectMemoryService(clock=lambda: NOW)
    service.replace_context(context(decisions=(decision(),)))
    candidate, _ = service.propose_conflict(
        "project-1",
        decision_id="decision-1",
        observed_text="改用方案 A",
        proposed_decision_text="采用方案 A",
        reason="与当前有效方案 B 不一致",
        evidence_refs=(source(),),
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
        proposed_decision_text="采用方案 A",
        reason="供应商交期发生变化",
        evidence_refs=(source(),),
        now=NOW,
    )


    reviewed, version = service.review_conflict(
        candidate.candidate_id,
        reviewer_id="owner-1",
        action="accept",
        change_reason="供应商交期发生变化",
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
            proposed_decision_text="采用方案 A",
            reason="来源不属于当前项目",
            evidence_refs=(foreign,),
            now=NOW,
        )


def test_stale_conflict_cannot_overwrite_a_newer_decision() -> None:
    service = ProjectMemoryService(clock=lambda: NOW)
    service.replace_context(context(decisions=(decision(),)))
    first, _ = service.propose_conflict(
        "project-1",
        decision_id="decision-1",
        observed_text="改用方案 A",
        proposed_decision_text="采用方案 A",
        reason="供应商交期发生变化",
        evidence_refs=(source(),),
        now=NOW,
    )
    stale, _ = service.propose_conflict(
        "project-1",
        decision_id="decision-1",
        observed_text="改用方案 C",
        proposed_decision_text="采用方案 C",
        reason="另一项条件发生变化",
        evidence_refs=(source(),),
        now=NOW,
    )
    service.review_conflict(
        first.candidate_id,
        reviewer_id="owner-1",
        action="accept",
        change_reason="供应商交期发生变化",
        now=NOW + timedelta(minutes=1),
    )

    with pytest.raises(RuntimeError, match="conflict_stale"):
        service.review_conflict(
            stale.candidate_id,
            reviewer_id="owner-1",
            action="accept",
            change_reason="另一项条件发生变化",
            now=NOW + timedelta(minutes=2),
        )

    assert service.current_decision(
        "project-1", "decision-1", now=NOW
    ).decision_text == "采用方案 A"


def test_stale_conflict_uses_base_version_even_when_text_is_unchanged() -> None:
    service = ProjectMemoryService(clock=lambda: NOW)
    service.replace_context(context(decisions=(decision(),)))
    stale, _ = service.propose_conflict(
        "project-1",
        decision_id="decision-1",
        observed_text="改用方案 C",
        proposed_decision_text="采用方案 C",
        reason="旧候选",
        evidence_refs=(source(),),
        now=NOW,
    )
    same_text, _ = service.propose_conflict(
        "project-1",
        decision_id="decision-1",
        observed_text="维持方案 B",
        proposed_decision_text="采用方案 B",
        reason="补充了新的决策依据",
        evidence_refs=(source(),),
        now=NOW,
    )
    service.review_conflict(
        same_text.candidate_id,
        reviewer_id="owner-1",
        action="accept",
        change_reason="补充了新的决策依据",
        now=NOW + timedelta(minutes=1),
    )

    with pytest.raises(RuntimeError, match="conflict_stale"):
        service.review_conflict(
            stale.candidate_id,
            reviewer_id="owner-1",
            action="accept",
            change_reason="旧候选",
            now=NOW + timedelta(minutes=2),
        )


def test_same_observation_can_create_a_new_candidate_for_a_new_base_version() -> None:
    service = ProjectMemoryService(clock=lambda: NOW)
    service.replace_context(context(decisions=(decision(),)))
    first, _ = service.propose_conflict(
        "project-1",
        decision_id="decision-1",
        observed_text="改用方案 A",
        proposed_decision_text="采用方案 A",
        reason="供应商交期发生变化",
        evidence_refs=(source(),),
        now=NOW,
    )
    service.review_conflict(
        first.candidate_id,
        reviewer_id="owner-1",
        action="accept",
        change_reason="供应商交期发生变化",
        now=NOW + timedelta(minutes=1),
    )

    second, created = service.propose_conflict(
        "project-1",
        decision_id="decision-1",
        observed_text="改用方案 A",
        proposed_decision_text="采用方案 A",
        reason="供应商交期发生变化",
        evidence_refs=(),
        now=NOW + timedelta(minutes=2),
    )

    assert created is True
    assert second.base_version == 2
    assert second.candidate_id != first.candidate_id


def test_context_refresh_cannot_replace_an_active_decision() -> None:
    service = ProjectMemoryService(clock=lambda: NOW)
    service.replace_context(context(decisions=(decision(),)))
    changed = context(decisions=(decision("采用方案 A"),))

    with pytest.raises(RuntimeError, match="decision_change_requires_review"):
        service.replace_context(changed)

    assert service.current_decision(
        "project-1", "decision-1", now=NOW
    ).decision_text == "采用方案 B"


def test_context_rejects_future_generated_at() -> None:
    service = ProjectMemoryService(clock=lambda: NOW)

    with pytest.raises(RuntimeError, match="context_from_future"):
        service.replace_context(
            context(
                generated_at=NOW + timedelta(seconds=31),
                decisions=(decision(),),
            )
        )
