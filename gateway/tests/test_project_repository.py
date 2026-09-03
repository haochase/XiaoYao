from datetime import UTC, datetime

from companion_gateway.project.models import (
    ConflictCandidate,
    DecisionCard,
    DecisionStatus,
    EvidenceRef,
    ProjectContextPackage,
)
from companion_gateway.project.repository import ProjectMemoryRepository
from companion_gateway.project.service import ProjectMemoryService


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
