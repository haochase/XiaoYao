from datetime import UTC, datetime, timedelta
import json
import sqlite3

import pytest

from companion_gateway.project.models import (
    ConflictCandidate,
    ConflictStatus,
    DecisionCard,
    DecisionStatus,
    DecisionVersion,
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


def context(decision_text: str = "采用方案 B") -> ProjectContextPackage:
    decision = DecisionCard(
        decision_id="decision-1",
        project_id="project-1",
        topic="终端方案",
        decision_text=decision_text,
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


def test_project_repository_reads_legacy_context_json_without_sourced_facts(
    tmp_path,
) -> None:
    database_path = tmp_path / "project-memory.db"
    repository = ProjectMemoryRepository(database_path)
    repository.initialize()
    payload = context().model_dump(mode="json")
    payload.update(
        open_actions=["补充成本测算"],
        current_risks=["供应商交期未确认"],
        next_meeting="2026-09-05 10:00",
    )
    payload.pop("sourced_actions", None)
    payload.pop("sourced_risks", None)
    payload.pop("sourced_next_meeting", None)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO project_contexts(project_id, payload_json) VALUES (?, ?)",
            ("project-1", json.dumps(payload, ensure_ascii=False)),
        )

    restored = ProjectMemoryRepository(database_path).get_context("project-1")

    assert restored is not None
    assert restored.open_actions == ("补充成本测算",)
    assert restored.current_risks == ("供应商交期未确认",)
    assert restored.next_meeting == "2026-09-05 10:00"
    assert restored.sourced_actions == ()
    assert restored.sourced_risks == ()
    assert restored.sourced_next_meeting is None
    reparsed = ProjectContextPackage.model_validate_json(restored.model_dump_json())
    assert reparsed == restored


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


def test_duplicate_proposal_cannot_overwrite_reviewed_terminal_state(tmp_path) -> None:
    database_path = tmp_path / "project-memory.db"
    first_repository = ProjectMemoryRepository(database_path)
    first_repository.initialize()
    first = ProjectMemoryService(repository=first_repository, clock=lambda: NOW)
    first.replace_context(context())
    proposed, _ = first.propose_conflict(
        "project-1",
        decision_id="decision-1",
        observed_text="改用方案 A",
        reason="供应商交期发生变化",
        evidence_refs=(source("meeting-2"),),
        now=NOW,
    )
    first.review_conflict(
        proposed.candidate_id,
        reviewer_id="owner-1",
        action="reject",
        change_reason="保留原决策",
        now=NOW + timedelta(minutes=1),
    )
    second = ProjectMemoryService(
        repository=ProjectMemoryRepository(database_path),
        clock=lambda: NOW,
    )

    duplicate, created = second.propose_conflict(
        "project-1",
        decision_id="decision-1",
        observed_text="改用方案 A",
        reason="供应商交期发生变化",
        evidence_refs=(source("meeting-2"),),
        now=NOW + timedelta(minutes=2),
    )

    assert created is False
    assert duplicate.status is ConflictStatus.REJECTED
    assert duplicate.reviewed_by == "owner-1"


def test_precached_proposal_refreshes_terminal_state_from_repository(tmp_path) -> None:
    database_path = tmp_path / "project-memory.db"
    repository = ProjectMemoryRepository(database_path)
    repository.initialize()
    first = ProjectMemoryService(repository=repository, clock=lambda: NOW)
    first.replace_context(context())
    proposed, _ = first.propose_conflict(
        "project-1",
        decision_id="decision-1",
        observed_text="改用方案 A",
        reason="供应商交期发生变化",
        evidence_refs=(source("meeting-2"),),
        now=NOW,
    )
    second = ProjectMemoryService(
        repository=ProjectMemoryRepository(database_path),
        clock=lambda: NOW,
    )
    cached, _ = second.propose_conflict(
        "project-1",
        decision_id="decision-1",
        observed_text="改用方案 A",
        reason="供应商交期发生变化",
        evidence_refs=(source("meeting-2"),),
        now=NOW,
    )
    assert cached.status is ConflictStatus.PROPOSED
    first.review_conflict(
        proposed.candidate_id,
        reviewer_id="owner-1",
        action="reject",
        change_reason="保留原决策",
        now=NOW + timedelta(minutes=1),
    )

    refreshed, created = second.propose_conflict(
        "project-1",
        decision_id="decision-1",
        observed_text="改用方案 A",
        reason="供应商交期发生变化",
        evidence_refs=(source("meeting-2"),),
        now=NOW + timedelta(minutes=2),
    )

    assert created is False
    assert refreshed.status is ConflictStatus.REJECTED


def test_service_reloads_context_after_another_instance_accepts_review(tmp_path) -> None:
    database_path = tmp_path / "project-memory.db"
    repository = ProjectMemoryRepository(database_path)
    repository.initialize()
    first = ProjectMemoryService(repository=repository, clock=lambda: NOW)
    first.replace_context(context())
    second = ProjectMemoryService(
        repository=ProjectMemoryRepository(database_path),
        clock=lambda: NOW,
    )
    assert second.answer("project-1", "终端方案", kind="fact", now=NOW).text == "采用方案 B"
    proposed, _ = first.propose_conflict(
        "project-1",
        decision_id="decision-1",
        observed_text="改用方案 A",
        reason="供应商交期发生变化",
        evidence_refs=(source("meeting-2"),),
        now=NOW,
    )
    first.review_conflict(
        proposed.candidate_id,
        reviewer_id="owner-1",
        action="accept",
        new_decision_text="采用方案 A",
        change_reason="供应商交期发生变化",
        evidence_refs=(source("meeting-2"),),
        now=NOW + timedelta(minutes=1),
    )

    answer = second.answer(
        "project-1",
        "终端方案",
        kind="fact",
        now=NOW + timedelta(minutes=2),
    )

    assert answer.text == "采用方案 A"


def test_persistent_context_without_any_version_history_is_rejected(tmp_path) -> None:
    database_path = tmp_path / "project-memory.db"
    repository = ProjectMemoryRepository(database_path)
    repository.initialize()
    repository.save_context(context())
    service = ProjectMemoryService(repository=repository, clock=lambda: NOW)

    with pytest.raises(ProjectMemoryError, match="decision_history_incomplete"):
        service.propose_conflict(
            "project-1",
            decision_id="decision-1",
            observed_text="改用方案 A",
            reason="供应商交期发生变化",
            evidence_refs=(source("meeting-2"),),
            now=NOW,
        )


def test_context_refresh_rejects_out_of_order_package(tmp_path) -> None:
    database_path = tmp_path / "project-memory.db"
    repository = ProjectMemoryRepository(database_path)
    repository.initialize()
    service = ProjectMemoryService(repository=repository, clock=lambda: NOW)
    current = context().model_copy(
        update={
            "generated_at": NOW,
            "open_actions": ("当前行动项",),
        }
    )
    delayed = context().model_copy(
        update={
            "generated_at": NOW - timedelta(minutes=1),
            "open_actions": ("过期行动项",),
        }
    )
    service.replace_context(current)

    with pytest.raises(ProjectMemoryError, match="context_refresh_stale"):
        service.replace_context(delayed)

    stored = repository.get_context("project-1")
    assert stored is not None
    assert stored.open_actions == ("当前行动项",)
    assert stored.generated_at == NOW


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

    commit_review = repository.commit_conflict_review
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

    monkeypatch.setattr(repository, "commit_conflict_review", commit_review)
    reviewed, version = service.review_conflict(
        proposed.candidate_id,
        reviewer_id="owner-1",
        action="accept",
        new_decision_text="采用方案 A",
        change_reason="供应商交期发生变化",
        evidence_refs=(source("meeting-2"),),
        now=NOW + timedelta(minutes=2),
    )
    versions = repository.list_versions("project-1", "decision-1")
    assert reviewed.status is ConflictStatus.ACCEPTED
    assert version.version == 2
    assert [item.version for item in versions] == [1, 2]
    assert [item.status for item in versions] == [
        DecisionStatus.SUPERSEDED,
        DecisionStatus.ACTIVE,
    ]


def test_failed_conflict_insert_does_not_leave_cached_candidate(
    tmp_path,
    monkeypatch,
) -> None:
    repository = ProjectMemoryRepository(tmp_path / "project-memory.db")
    repository.initialize()
    service = ProjectMemoryService(repository=repository, clock=lambda: NOW)
    service.replace_context(context())
    create_conflict = repository.create_conflict

    def fail_create(_candidate):
        raise RuntimeError("simulated_insert_failure")

    monkeypatch.setattr(repository, "create_conflict", fail_create)
    with pytest.raises(RuntimeError, match="simulated_insert_failure"):
        service.propose_conflict(
            "project-1",
            decision_id="decision-1",
            observed_text="改用方案 A",
            reason="供应商交期发生变化",
            evidence_refs=(source("meeting-2"),),
            now=NOW,
        )

    monkeypatch.setattr(repository, "create_conflict", create_conflict)
    _, created = service.propose_conflict(
        "project-1",
        decision_id="decision-1",
        observed_text="改用方案 A",
        reason="供应商交期发生变化",
        evidence_refs=(source("meeting-2"),),
        now=NOW,
    )
    assert created is True


def test_first_context_insert_cannot_be_overwritten_by_another_service(
    tmp_path,
) -> None:
    database_path = tmp_path / "project-memory.db"
    first_repository = ProjectMemoryRepository(database_path)
    first_repository.initialize()
    second_repository = ProjectMemoryRepository(database_path)
    second_repository.initialize()
    first = ProjectMemoryService(repository=first_repository, clock=lambda: NOW)
    second = ProjectMemoryService(repository=second_repository, clock=lambda: NOW)

    first.replace_context(context("采用方案 B"))
    with pytest.raises(ProjectMemoryError, match="decision_change_requires_review"):
        second.replace_context(context("采用方案 A"))

    stored = second_repository.get_context("project-1")
    assert stored is not None
    assert stored.active_decisions[0].decision_text == "采用方案 B"


def test_incomplete_legacy_version_history_is_rejected(tmp_path) -> None:
    database_path = tmp_path / "project-memory.db"
    repository = ProjectMemoryRepository(database_path)
    repository.initialize()
    service = ProjectMemoryService(repository=repository, clock=lambda: NOW)
    service.replace_context(context())
    repository.save_version(
        "project-1",
        DecisionVersion(
            decision_id="decision-1",
            version=1,
            change_reason="旧格式缺少决策正文",
            proposed_by="owner-1",
            approved_by="owner-1",
            approved_at=NOW,
            status=DecisionStatus.ACTIVE,
            evidence_refs=(source(),),
        ),
    )
    reopened = ProjectMemoryService(
        repository=ProjectMemoryRepository(database_path),
        clock=lambda: NOW,
    )

    with pytest.raises(ProjectMemoryError, match="decision_history_incomplete"):
        reopened.propose_conflict(
            "project-1",
            decision_id="decision-1",
            observed_text="改用方案 A",
            reason="供应商交期发生变化",
            evidence_refs=(source("meeting-2"),),
            now=NOW,
        )
