from datetime import UTC, datetime, timedelta

import pytest

from companion_gateway.project.models import (
    ConflictCandidate,
    ConflictStatus,
    DecisionCard,
    DecisionStatus,
    EvidenceRef,
    ProjectContextPackage,
)
from companion_gateway.project.repository import ProjectMemoryRepository
from companion_gateway.project.service import ProjectMemoryError, ProjectMemoryService


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


def context() -> ProjectContextPackage:
    decision = DecisionCard(
        decision_id="decision-1",
        project_id="project-1",
        topic="终端方案",
        decision_text="采用方案 B",
        rationale="交付风险更低",
        owner="owner-1",
        decided_at=NOW,
        source_refs=(source(),),
        status=DecisionStatus.ACTIVE,
        confidence=0.92,
    )
    return ProjectContextPackage(
        project_id="project-1",
        project_name="星河零售终端升级项目",
        generated_at=NOW,
        source_refs=(source(),),
        active_decisions=(decision,),
        permission_scope="project:star-retail",
    )


def candidate() -> ConflictCandidate:
    return ConflictCandidate(
        candidate_id="conflict-1",
        project_id="project-1",
        decision_id="decision-1",
        observed_text="改用方案 A",
        active_decision_text="采用方案 B",
        reason="供应商交期发生变化",
        source_refs=(source("meeting-2"),),
        created_at=NOW,
    )


def test_project_repository_round_trips_context_and_conflict(tmp_path) -> None:
    database_path = tmp_path / "project-memory.db"
    repository = ProjectMemoryRepository(database_path)
    repository.initialize()
    repository.save_context(context())
    repository.save_conflict(candidate())

    reopened = ProjectMemoryRepository(database_path)
    reopened.initialize()

    assert reopened.get_context("project-1") == context()
    assert reopened.get_conflict("conflict-1") == candidate()


def test_project_memory_service_recovers_context_after_restart(tmp_path) -> None:
    database_path = tmp_path / "project-memory.db"
    first_repository = ProjectMemoryRepository(database_path)
    first_repository.initialize()
    first_service = ProjectMemoryService(
        repository=first_repository,
        clock=lambda: NOW,
    )
    first_service.replace_context(context())

    reopened_repository = ProjectMemoryRepository(database_path)
    reopened_repository.initialize()
    reopened_service = ProjectMemoryService(
        repository=reopened_repository,
        clock=lambda: NOW,
    )

    answer = reopened_service.answer(
        "project-1",
        "终端方案",
        kind="fact",
        now=NOW,
    )

    assert answer.text == "采用方案 B"
    assert answer.source_refs[0].source_id == "meeting-1"


def test_conflict_proposal_remains_idempotent_after_restart(tmp_path) -> None:
    database_path = tmp_path / "project-memory.db"
    first_repository = ProjectMemoryRepository(database_path)
    first_repository.initialize()
    first_service = ProjectMemoryService(
        repository=first_repository,
        clock=lambda: NOW,
    )
    first_service.replace_context(context())
    first, created = first_service.propose_conflict(
        "project-1",
        decision_id="decision-1",
        observed_text="改用方案 A",
        reason="供应商交期发生变化",
        evidence_refs=(source("meeting-2"),),
        now=NOW,
    )

    reopened_repository = ProjectMemoryRepository(database_path)
    reopened_repository.initialize()
    reopened_service = ProjectMemoryService(
        repository=reopened_repository,
        clock=lambda: NOW,
    )
    duplicate, duplicate_created = reopened_service.propose_conflict(
        "project-1",
        decision_id="decision-1",
        observed_text="改用方案 A",
        reason="供应商交期发生变化",
        evidence_refs=(source("meeting-2"),),
        now=NOW,
    )

    assert created is True
    assert duplicate_created is False
    assert duplicate == first


def test_accepted_review_persists_one_consistent_decision_history(tmp_path) -> None:
    database_path = tmp_path / "project-memory.db"
    repository = ProjectMemoryRepository(database_path)
    repository.initialize()
    service = ProjectMemoryService(repository=repository, clock=lambda: NOW)
    service.replace_context(context())
    proposed, _ = service.propose_conflict(
        "project-1",
        decision_id="decision-1",
        observed_text="改用方案 A",
        reason="供应商交期发生变化",
        evidence_refs=(source("meeting-2"),),
        now=NOW,
    )

    service.review_conflict(
        proposed.candidate_id,
        reviewer_id="owner-1",
        action="accept",
        new_decision_text="采用方案 A",
        change_reason="供应商交期发生变化",
        evidence_refs=(source("meeting-2"),),
        now=NOW + timedelta(minutes=1),
    )

    reopened = ProjectMemoryRepository(database_path)
    reopened.initialize()
    stored_context = reopened.get_context("project-1")
    stored_candidate = reopened.get_conflict(proposed.candidate_id)
    versions = reopened.list_versions("project-1", "decision-1")
    assert stored_context is not None
    assert stored_context.active_decisions[0].decision_text == "采用方案 A"
    assert stored_candidate is not None
    assert stored_candidate.status is ConflictStatus.ACCEPTED
    assert [item.status for item in versions] == [
        DecisionStatus.SUPERSEDED,
        DecisionStatus.ACTIVE,
    ]
    assert [item.decision_text for item in versions] == ["采用方案 B", "采用方案 A"]


def test_stale_service_cannot_review_an_already_accepted_conflict(tmp_path) -> None:
    database_path = tmp_path / "project-memory.db"
    repository = ProjectMemoryRepository(database_path)
    repository.initialize()
    service = ProjectMemoryService(repository=repository, clock=lambda: NOW)
    service.replace_context(context())
    proposed, _ = service.propose_conflict(
        "project-1",
        decision_id="decision-1",
        observed_text="改用方案 A",
        reason="供应商交期发生变化",
        evidence_refs=(source("meeting-2"),),
        now=NOW,
    )
    stale_service = ProjectMemoryService(
        repository=ProjectMemoryRepository(database_path),
        clock=lambda: NOW,
    )
    assert stale_service.get_conflict(proposed.candidate_id).status is ConflictStatus.PROPOSED
    service.review_conflict(
        proposed.candidate_id,
        reviewer_id="owner-1",
        action="accept",
        new_decision_text="采用方案 A",
        change_reason="供应商交期发生变化",
        evidence_refs=(source("meeting-2"),),
        now=NOW + timedelta(minutes=1),
    )

    with pytest.raises(ProjectMemoryError, match="conflict_already_reviewed"):
        stale_service.review_conflict(
            proposed.candidate_id,
            reviewer_id="owner-2",
            action="reject",
            change_reason="重复评审",
            now=NOW + timedelta(minutes=2),
        )


def test_failed_atomic_review_keeps_in_memory_state_unchanged(
    tmp_path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "project-memory.db"
    repository = ProjectMemoryRepository(database_path)
    repository.initialize()
    service = ProjectMemoryService(repository=repository, clock=lambda: NOW)
    service.replace_context(context())
    proposed, _ = service.propose_conflict(
        "project-1",
        decision_id="decision-1",
        observed_text="改用方案 A",
        reason="供应商交期发生变化",
        evidence_refs=(source("meeting-2"),),
        now=NOW,
    )

    def fail_review(**_kwargs) -> bool:
        raise RuntimeError("simulated_transaction_failure")

    monkeypatch.setattr(repository, "commit_conflict_review", fail_review)
    with pytest.raises(RuntimeError, match="simulated_transaction_failure"):
        service.review_conflict(
            proposed.candidate_id,
            reviewer_id="owner-1",
            action="accept",
            new_decision_text="采用方案 A",
            change_reason="供应商交期发生变化",
            evidence_refs=(source("meeting-2"),),
            now=NOW + timedelta(minutes=1),
        )

    assert service.get_conflict(proposed.candidate_id).status is ConflictStatus.PROPOSED
    assert service.current_decision(
        "project-1", "decision-1", now=NOW
    ).decision_text == "采用方案 B"
